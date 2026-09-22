# -*- coding: utf-8 -*-
"""存档分类：名称校验、数据目录解析、删除（回收站/直接）、配置管理。

★ 配置的**写入口只有一个**（``_coerce_*`` 系列）。不管是「读文件」、
「设置页保存」、「脚本改一项」，都走同一张规格表 —— 否则同一种脏值在
不同入口下会有不同结果（历史上就栽过：界面校验过的那条路会拦，直接改
config.json 的那条路不会）。见 ``GLOBAL_COERCERS`` / ``_coerce_profile``。
"""
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import sources as _sources
from .utils import (
    LAUNCHER_UPDATE_DEFAULT,
    atomic_write_json,
    normalize_launcher_update,
    parse_bool,
)
from .version import CONFIG_VERSION

logger = logging.getLogger(__name__)

PROFILE_NAME_FORBIDDEN = set('\\/:*?"<>|')
PROFILE_NAME_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
DEFAULT_PROFILE_NAME = "默认"

# 版本号同样会直接拼进文件名（versions/manifests/<类型>_<版本>.json），
# 所以也得挡住路径分隔符 —— 否则「版本号」这一栏能写到目录外面去。
VERSION_NAME_FORBIDDEN = set('\\/:*?"<>|')

def sanitize_profile_name(name: str) -> str:
    """校验并规范化存档分类名（分类名会直接作为备份子目录名）。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("存档分类名不能为空")
    if name in {".", ".."}:
        raise ValueError("存档分类名不合法")
    bad = PROFILE_NAME_FORBIDDEN & set(name)
    if bad:
        raise ValueError(
            "存档分类名不能包含这些字符: " + " ".join(sorted(bad))
        )
    if name.endswith("."):
        raise ValueError("存档分类名不能以英文句点结尾")
    if name.upper() in PROFILE_NAME_RESERVED:
        raise ValueError(f"「{name}」是系统保留名，请换一个")
    if len(name) > 40:
        raise ValueError("存档分类名不要超过 40 个字符")
    return name

def sanitize_version_name(name: str) -> str:
    """校验并规范化版本号（它会被拼进清单文件名）。

    不加这一步的话，版本号里的 ``\\`` / ``/`` 会让清单落到 manifests 目录
    之外（实测 ``..\\..\\..\\evil`` 能写到 versions/ 甚至更上层），
    删除版本时同一个拼接方式就变成"按用户输入删任意文件"。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("版本号不能为空")
    bad = VERSION_NAME_FORBIDDEN & set(name)
    if bad:
        raise ValueError(
            "版本号不能包含这些字符: " + " ".join(sorted(bad))
        )
    if name in {".", ".."} or not name.strip("."):
        raise ValueError("版本号不合法")
    if any(ord(c) < 32 for c in name):
        raise ValueError("版本号不能包含控制字符")
    if len(name) > 40:
        raise ValueError("版本号不要超过 40 个字符")
    return name

def roaming_dir() -> Path:
    """游戏存放数据的 %APPDATA% 目录。"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata)
    return Path.home() / "AppData" / "Roaming"

def default_data_dir(name: str) -> Path:
    """新建存档分类的默认数据目录（C 盘）。

    「默认」分类沿用游戏原生目录；其余分类平级放在 Mindustry_Profiles 下，
    这样不会污染游戏自带的那个目录。
    """
    roaming = roaming_dir()
    if name == DEFAULT_PROFILE_NAME:
        return roaming / "Mindustry"
    return roaming / "Mindustry_Profiles" / name

def move_to_recycle_bin(path: Path) -> None:
    """把文件或目录移入 Windows 回收站（可从回收站还原）。

    不使用 shutil.rmtree：删游戏数据目录属于不可逆操作，必须留后悔机会。

    ⚠️ 两个已知边界，别以为「进回收站」是绝对的：
      * 目标所在卷没有回收站（网络盘、非 NTFS、回收站被禁用或超配额）时，
        FOF_ALLOWUNDO 会退化成**永久删除**。所以这里**故意不加**
        FOF_NOCONFIRMATION —— 它会同时让 FOF_WANTNUKEWARNING 失效，
        而后者正是 Windows 用来在「即将永久删除」时弹确认框的开关。
        加了 NOCONFIRMATION 就等于把"静默硬删"这个坑焊死。
      * 路径用 os.path.abspath 而不是 Path.resolve()：后者会把符号链接 /
        junction 解析成它的**目标**，于是"删这个链接"变成"删链接指向的目录"。
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    if os.name != "nt":
        raise OSError("移入回收站仅支持 Windows")

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_ALLOWUNDO = 0x0040  # 关键：进回收站而不是彻底删除
    FOF_NOERRORUI = 0x0400
    # 没进回收站（=即将永久删除）时弹一个确认框，别默默硬删
    FOF_WANTNUKEWARNING = 0x4000

    target = os.path.abspath(str(path))
    # pFrom 需要以双 NUL 结尾：buffer 本身补零，再留一个空位
    buf = ctypes.create_unicode_buffer(len(target) + 2)
    buf.value = target

    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = (
        FOF_ALLOWUNDO | FOF_WANTNUKEWARNING | FOF_NOERRORUI | FOF_SILENT
    )
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None

    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0:
        raise OSError(f"移入回收站失败（错误码 {code}）")
    if op.fAnyOperationsAborted:
        raise OSError("操作已中止，未删除任何内容")

def permanent_delete(path: Path) -> None:
    """直接彻底删除（**不进回收站，不可还原**）。

    ★ 只有把设置项 ``permanent_delete`` 打开（默认**关**）才会走到这里。
    留着这个开关的理由很实在：数据目录动辄几个 GB，走 ``SHFileOperation``
    进回收站是「搬进 ``$Recycle.Bin``」，跨卷时还会退化成**复制一遍再删**，
    磁盘紧张时既慢又白占一份空间。默认仍然走回收站 —— 删错了还有后悔药。

    两条边界跟回收站版本保持一致：

      * 符号链接 / junction 只删**链接本身**，绝不跟进去删目标（和
        ``move_to_recycle_bin`` 用 ``abspath`` 而不是 ``resolve`` 同一个理由）；
      * 删不掉就往上抛 ``OSError``，让调用方去决定是「放弃这次删除」还是报错，
        这里不吞异常。
    """
    path = Path(path)
    if path.is_symlink():
        # ★ 必须先判链接：目录链接的 is_dir() 会跟到目标上，跟进去删就等于
        #   「删这个链接」变成了「删链接指向的目录」。
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()

def delete_path(path: Path, *, permanent: bool = False) -> None:
    """删除文件/目录的统一入口：默认进回收站，``permanent=True`` 才直接删。

    ``permanent`` **一律由调用方从配置项 ``permanent_delete`` 传进来** ——
    这个函数是纯工具，不认识 ConfigManager，别在里头自己读配置（否则测试
    要 patch 的地方又多一处，而且「同一次操作里两处读到的值不一样」这种事
    只能靠运气避免）。
    """
    if permanent:
        logger.warning(f"直接彻底删除（不进回收站）: {path}")
        permanent_delete(path)
    else:
        move_to_recycle_bin(path)

def dir_summary(path: Path) -> str:
    """粗略概览一个目录的内容，避免大目录递归统计造成卡顿。"""
    try:
        items = list(Path(path).iterdir())
    except OSError as e:
        return f"无法读取（{e}）"
    if not items:
        return "空目录"
    dirs = sum(1 for i in items if i.is_dir())
    files = len(items) - dirs
    return f"{dirs} 个文件夹、{files} 个文件（仅顶层）"

# 官方随包发布的默认 JVM 参数（原来放在独立的 Mindustry.json 里）。
DEFAULT_VM_ARGS = [
    "-Dhttps.protocols=TLSv1.2,TLSv1.1,TLSv1",
    "-XX:+ShowCodeDetailsInExceptionMessages",
    "-XX:+UseCompactObjectHeaders",
    "--enable-native-access=ALL-UNNAMED",
]

# GitHub 加速镜像：一个**前缀**，下载 release 资源时直接拼在完整地址前面，
# 例如 https://ghfast.top/https://github.com/Anuken/Mindustry/releases/download/...
# 留空字符串 = 直连 GitHub（国内通常很慢，但至少是个明确的选项）。
DEFAULT_MIRROR = "https://gh.tinylake.top/"
# 设置页「GitHub 镜像」下拉框里的可选项。★ 它**也是 config.json 的一项**
# （``github_mirror_presets``）—— 想加自己的镜像站直接改那份文件即可，
# 不用动代码。改坏了也不至于出事：读的时候逐项过 normalize_mirror，
# 非法项丢掉，整份不是列表就退回这份内置默认（见 normalize_mirror_presets）。
DEFAULT_MIRROR_PRESETS = [
    DEFAULT_MIRROR,
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
]
# 兜个上限：配置文件是给人手改的，写进去几百项只会把下拉框撑爆
MAX_MIRROR_PRESETS = 20

# logs/ 里最多留几份游戏日志（更旧的送回收站，见 gamelog.GameLog._prune）。
# 上限只是防止有人填个天文数字把目录塞爆；下限定在 1，因为 0 会让刚写的
# 那一份当场被自己删掉。
DEFAULT_LOG_KEEP = 20
MAX_LOG_KEEP = 200


def normalize_mirror(raw: Any) -> str:
    """把用户填的镜像地址整理成「能直接拼在下载地址前」的前缀。

    不合法一律退成空串（=直连），**不抛异常**：镜像只是加速手段，
    值脏了最多是下载慢一点，不该让启动器起不来。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if "://" not in s:
        # 只写主机名也认（ghfast.top → https://ghfast.top/）
        s = "https://" + s
    if not s.startswith(("http://", "https://")):
        logger.warning(f"镜像地址 {raw!r} 协议不支持，按「直连 GitHub」处理")
        return ""
    if not s.endswith("/"):
        s += "/"
    host = s.split("://", 1)[1].split("/", 1)[0]
    if not host or "." not in host or any(c.isspace() for c in s):
        logger.warning(f"镜像地址 {raw!r} 不像有效主机名，按「直连 GitHub」处理")
        return ""
    return s


def normalize_mirror_presets(raw: Any) -> list[str]:
    """整理「镜像可选项」列表（config.json 的 ``github_mirror_presets``）。

    ★ 手改配置文件是这项存在的**唯一理由**，所以这里一律宽容：

      * 手滑写成一个字符串（``"https://a.com/"``）也认，当一项处理 ——
        否则会被 Python 拆成一个个字符，列表里冒出 20 个单字符选项；
      * 每项都过一遍 ``normalize_mirror``，不合法的和重复的丢掉；
      * 整份不是列表、或者项全被丢光，就退回内置默认 —— 下拉框空着会
        让人以为「没有可选项」（真要清空，写成列表里只留自己那一项）。
    """
    if isinstance(raw, str):
        raw = [raw]
    items: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            s = normalize_mirror(item)
            if s and s not in items:
                items.append(s)
    if not items:
        logger.warning(f"镜像可选项 {raw!r} 不可用，改用内置默认列表")
        return list(DEFAULT_MIRROR_PRESETS)
    return items[:MAX_MIRROR_PRESETS]


def normalize_log_keep(raw: Any, *, what: str = "max_log_files") -> int:
    """日志保留份数：非整数退回默认，超范围夹到区间内（不抛异常）。

    ``what`` 只是给 WARNING 用的，**默认就填配置键名**：这一项本来就是
    「想加就手改 config.json」的那一类，告警里点名键名，用户才能直接去
    文件里找到是哪一行。跟 ``normalize_bool`` 的 ``what`` 一个路子。
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"配置项 {what}={raw!r} 不是整数，按默认 {DEFAULT_LOG_KEEP} 处理"
        )
        return DEFAULT_LOG_KEEP
    return max(1, min(n, MAX_LOG_KEEP))


# config.json 里记「这份文件是哪一代结构」的键名。
# 缺这个键 = 第 0 代（加迁移链之前的配置），见 ConfigManager._migrate。
CONFIG_VERSION_KEY = "config_version"


def normalize_bool(raw: Any, default: bool, *, what: str = "开关") -> bool:
    """把配置里的开关值清成真正的 bool。

    ★ **不能只靠 ``type(default)(value)``**：``bool("false")`` 是 ``True``
    —— 配置里写成字符串 ``"false"``，开关反而被打开。以前只有
    ``hide_on_launch`` 这类，错了顶多「窗口没藏」，而
    ``close_on_game_exit`` 写错就是**启动器自己关了**，所以这一层不能省。

    认：真 bool、0/1、以及 "true/false/yes/no/on/off/y/n/t/f"（不分大小写）。
    其它一律退回默认值 + WARNING（不猜用户的意思）。

    实现挪到了 ``utils.parse_bool``（``sources.py`` 也要用，而它不能 import
    本模块 —— config 要 import sources，反过来会绕成环）。这里保留原名字与
    签名，调用方和回归测试都还按 ``config.normalize_bool`` 写。
    """
    return parse_bool(raw, default, what=what)



class ConfigManager:
    """启动器配置。

    profiles 里每个存档分类拥有独立设置（数据目录 + 备份策略），
    GLOBAL_DEFAULTS 里的键则是全局共用，与存档无关。

    ★ 游戏启动配置（JVM_DEFAULTS）原先单独放在 exe 旁边的 Mindustry.json 里
    ——那是给官方原生启动器 Mindustry.exe（内嵌 JVM，读 jrePath/classPath/
    mainClass/useZgcIfSupportedOs/vmArgs）用的。我们走的是 ``java.exe -cp
    <运行时组装的 jar>``，用不到其中两个字段，所以合并进来时丢掉了：

      * ``classPath`` —— 官方用它拼 ``-classpath``。我们的 jar 是每次从 CAS
        现场组装的，如果这里再挂一份 ``jre/desktop.jar``，同一个类会出现在
        classpath 里两次，可能撞版本。
      * ``useZgcIfSupportedOs`` —— 想开 ZGC 直接在「额外 JVM 参数」里写
        ``-XX:+UseZGC`` 更直白，也省得再维护一套「系统支不支持」的判断。
    """

    # 全局共用，所有存档分类一致
    GLOBAL_DEFAULTS = {
        "hide_on_launch": True,
        # 游戏退出后顺手把启动器也关掉（自动备份做完才关）。
        # 默认关：启动器默认「留着」，用户想关才勾 —— 这个开关会真的把窗口
        # 关掉，误开了等于「游戏一退启动器就没了」，不适合默认打开。
        "close_on_game_exit": False,
        # 加速下载用的镜像前缀（空 = 直连 GitHub）。以前是个 bool 开关
        # （use_mirror），地址写死在 updates.py 里；现在改成可填的地址，
        # 开关本身就多余了 —— 留空就是不用。
        "github_mirror": DEFAULT_MIRROR,
        # 下拉框里的候选镜像站（改这里就等于改设置页那个列表）。
        # 它跟 github_mirror 一样会被存进 config.json，所以「恢复默认设置」
        # 不会动它 —— 那是用户的候选清单，不是界面上的临时勾选。
        "github_mirror_presets": list(DEFAULT_MIRROR_PRESETS),
        "auto_update": True,
        # ★ 启动器**自身**的更新档位 —— 注意跟上面的 "auto_update" 不是
        #   一回事：那个管「**游戏**版本有没有新版」，这个管「**启动器**自己
        #   有没有新版」。auto = 后台检查 + 下载、退出启动器时自动应用；
        #   check = 只提示；off = 完全停用。
        #   另外「源码运行一律不检查」是硬编码的（见 selfupdate.mode_of），
        #   所以在开发机上再怎么配也不会把自己编的版本顶掉。
        "launcher_update": LAUNCHER_UPDATE_DEFAULT,
        # 自定义启动参数。存字符串（而不是数组）是为了让用户在设置页里
        # 改起来就像改一行命令行；解析见 gamecmd.split_args。
        "extra_vm_args": "",
        "extra_program_args": "",
        # 是否把游戏输出落盘到 logs/game-<时间>.log（窗口里总是能看到）
        "save_game_log": True,
        # logs/ 里最多留几份（更旧的进回收站）
        "max_log_files": DEFAULT_LOG_KEEP,
        # ★ 删除方式：False（默认）= 走回收站（可还原）；True = 直接彻底删。
        # 只作用于**用户文件**那几处删除（删存档数据目录、恢复备份时清空
        # 存档目录、清理旧游戏日志）—— CAS 对象池回收、备份清单超额清理
        # 是内部文件，走的是另一条路（几万个碎片对象进回收站等于灾难），
        # 不受这个开关影响，也没必要受影响。
        "permanent_delete": False,
        # 额外的「从哪儿下游戏版本」来源（数组，默认空 = 只用内置那两个）。
        # ★ 这一项是给**发布之后**留的扩展口：想加一个新版本来源，改
        #   config.json 就行，不用等启动器更新。格式见 sources.py 的
        #   normalize_version_sources（写坏了逐项丢掉，不会让配置失效）。
        #   它不是界面上的东西，所以「恢复默认」不会动它。
        "version_sources": [],
    }
    # 游戏启动配置（原 Mindustry.json）
    JVM_DEFAULTS = {
        "jre_path": "jre",
        "main_class": "mindustry.desktop.DesktopLauncher",
        "vm_args": list(DEFAULT_VM_ARGS),
        "program_args": [],
    }
    # 每个存档分类独立一份
    PROFILE_DEFAULTS = {
        "data_dir": "",
        "min_playtime": 20,
        "max_backups": 20,
        "auto_backup": True,
    }
    DEFAULTS = GLOBAL_DEFAULTS

    # 本版本认识的顶层键（其余的会被**原样保留**并写回，见 load/save）。
    # 这一条是「老启动器别把新配置搞坏」的关键：把 config.json 从新版本
    # 挪回旧版本用，旧版本不认识的新键不能因为「我读不懂」就被删掉。
    KNOWN_TOP_KEYS = frozenset(GLOBAL_DEFAULTS) | frozenset({
        "current_profile", "profiles", "jvm", CONFIG_VERSION_KEY,
    })

    # ★ 已经**废弃**的顶层键：读到直接丢掉，不再写回。
    #   这跟上面「未知键原样保留」是两件事，别混：
    #     * 未知键   —— 可能来自**更新的**版本，保留是为了「过一趟不掉东西」；
    #     * 废弃键   —— 本版本明确不要了（改名/换语义），留着只会让人以为
    #                   它还在起作用，改它却毫无效果。
    OBSOLETE_TOP_KEYS = frozenset({
        # 早期是个 bool 开关「用镜像加速下载」，地址写死在代码里；现在改成
        # 可填的镜像前缀 github_mirror（留空就是直连），开关本身就多余了。
        "use_mirror",
    })

    # ★ 规格表：值需要「不只是按类型强转」的配置项写在这里。
    #   函数签名统一是 ``(raw) -> 清理后的值``，**绝不抛异常**。
    #   没有登记的项走 ``_coerce_global`` 的通用规则。
    GLOBAL_COERCERS: dict[str, Callable[[Any], Any]] = {
        "github_mirror": normalize_mirror,
        "github_mirror_presets": normalize_mirror_presets,
        "max_log_files": normalize_log_keep,
        "version_sources": _sources.normalize_version_sources,
        # 三档枚举（auto/check/off）。解析函数在 utils 而不是 selfupdate ——
        # 那边要 import 本模块，写那边会绕成循环 import。
        "launcher_update": normalize_launcher_update,
    }
    # 字符串型配置项的额外约束：必须真的是字符串（别的类型写了就退默认，
    # 免得 ``str(["a"])`` 变成 "['a']" 这种用户看了莫名其妙的玩意儿）。
    _TEXT_KEYS = frozenset({
        "extra_vm_args", "extra_program_args",
    })

    def __init__(self, config_file: Path) -> None:
        self.file = config_file
        self.data: dict[str, Any] = {
            **self.GLOBAL_DEFAULTS,
            "current_profile": "",
            "profiles": {},
            "jvm": self._normalize_jvm(None),
        }
        # 用可重入锁：load() 会在持锁状态下调用 save()
        self._lock = threading.RLock()
        # 本版本**不认识**的顶层键：读的时候收在这里，写的时候原样带回去。
        # 少了这一条，把 config.json 从新版本挪回旧版本用一次就会被削掉
        # 一半内容（旧版本只认识自己那几个键，save() 一覆盖就没了）。
        self._extras: dict[str, Any] = {}
        # 文件里声明的配置版本（缺省 0）。save() 靠它保证**只升不降**。
        self._file_version = 0
        self.load()

    # ---------- 配置项的整理（所有写入口的唯一实现） ----------

    def _coerce_global(self, key: str, raw: Any) -> Any:
        """把一个全局配置项的原值整理成可信值。**不抛异常。**

        ★ 这张表就是「界面校验过了」和「有人手改了 config.json」两条路的
        交汇点 —— 两条路必须得到同一个结果，否则同一种脏值在一条路上被拦、
        在另一条路上被放过去，症状极难复现。
        """
        default = self.GLOBAL_DEFAULTS[key]
        coercer = self.GLOBAL_COERCERS.get(key)
        if coercer is not None:
            try:
                return coercer(raw)
            except Exception as e:                              # noqa: BLE001
                logger.warning(
                    f"配置项 {key} 整理失败（{e}），改用默认值 {default!r}"
                )
                return default
        if isinstance(default, bool):
            # bool("false") 是 True —— 开关型必须显式判词
            return normalize_bool(raw, default, what=key)
        if key in self._TEXT_KEYS:
            if isinstance(raw, str):
                return raw
            logger.warning(f"配置项 {key}={raw!r} 不是文本，按默认值处理")
            return default
        if isinstance(default, list):
            # 没登记整理函数的列表项：只认数组。``list("abc")`` 会拆成
            # 一堆单字符，正是要避免的事。
            if isinstance(raw, (list, tuple)):
                return list(raw)
            logger.warning(f"配置项 {key} 必须是数组，已改用默认值")
            return list(default)
        if isinstance(default, int):
            try:
                return int(raw)
            except (TypeError, ValueError):
                logger.warning(
                    f"配置项 {key}={raw!r} 不是整数，按默认 {default} 处理"
                )
                return default
        logger.warning(f"配置项 {key}={raw!r} 无法处理，按默认值处理")
        return default

    def _coerce_profile(self, name: str, key: str, raw: Any) -> Any:
        """分类级配置项。**不抛异常**；数值型的上下限在这里统一夹住。

        夹上下限以前只在**读文件**时做（``_normalize_profile``），于是
        「运行期临时改一个 0」能绕过约束 —— ``_cleanup_old_backups`` 是
        ``while len(backups) > max_keep``，写成 0 就是把历史备份一次删光。
        """
        default = self.PROFILE_DEFAULTS[key]
        what = f"分类「{name}」的 {key}"
        if isinstance(default, bool):
            return normalize_bool(raw, default, what=what)
        if isinstance(default, int):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                logger.warning(f"{what}={raw!r} 不是整数，按默认 {default} 处理")
                return default
            if key == "max_backups":
                return max(1, value)
            if key == "min_playtime":
                return max(0, value)
            return value
        if isinstance(raw, str):
            return raw
        logger.warning(f"{what}={raw!r} 不是文本，按默认值处理")
        return default


    def load(self) -> None:
        saved: dict[str, Any] = {}
        rebuilt = False
        try:
            if self.file.exists():
                raw = json.loads(self.file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    saved = raw
                else:
                    # 顶层不是对象（例如被改成了数组/字符串）——以前这里
                    # 会在 saved.get() 上抛 AttributeError，一路冒到
                    # 启动器构造，表现为「一开就崩」。
                    logger.warning(
                        f"配置顶层不是对象（{type(raw).__name__}），按损坏处理"
                    )
                    self._quarantine_bad_config()
                    rebuilt = True
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            logger.warning(f"配置读取失败，使用默认值: {e}")
            self._quarantine_bad_config()
            rebuilt = True
            saved = {}

        # ---- 结构版本迁移（老配置 -> 本版本）----
        # 在**整理各字段之前**跑：迁移函数面对的是文件里的原始结构，
        # 而不是已经被清过的值。见文件末尾的 MIGRATIONS。
        saved, upgraded = self._migrate(saved)

        with self._lock:
            # 不认识的顶层键：原样留着，save() 时带回去。
            # 这一条保证「新版本的配置在旧版本里过一遍」不会掉东西。
            # 已废弃的键（OBSOLETE_TOP_KEYS）例外 —— 那些是明确不要了的。
            self._extras = {
                k: v for k, v in saved.items()
                if k not in self.KNOWN_TOP_KEYS
                and k not in self.OBSOLETE_TOP_KEYS
            }
            if self._extras:
                logger.info(
                    "配置里有本版本不认识的项，会原样保留: "
                    + ", ".join(sorted(self._extras))
                )
            dropped = sorted(set(saved) & self.OBSOLETE_TOP_KEYS)
            if dropped:
                logger.info("配置里已废弃的项，已丢弃: " + ", ".join(dropped))
            for key in self.GLOBAL_DEFAULTS:
                if saved.get(key) is None:
                    continue
                self.data[key] = self._coerce_global(key, saved[key])
            raw_profiles = saved.get("profiles")
            if raw_profiles is not None and not isinstance(raw_profiles, dict):
                logger.warning("配置里的 profiles 不是对象，已忽略并重建")
                raw_profiles = {}
                rebuilt = True
            profiles: dict[str, dict[str, Any]] = {}
            for name, raw in (raw_profiles or {}).items():
                try:
                    clean = sanitize_profile_name(str(name))
                except ValueError as e:
                    logger.warning(f"忽略非法存档分类名「{name}」: {e}")
                    continue
                profiles[clean] = self._normalize_profile(clean, raw)
            if not profiles:
                # 首次运行或配置损坏：建一个指向游戏原生目录的分类
                logger.info(
                    f"没有可用的存档分类，创建「{DEFAULT_PROFILE_NAME}」"
                )
                profiles = {
                    DEFAULT_PROFILE_NAME: self._default_profile(
                        DEFAULT_PROFILE_NAME
                    )
                }
            self.data["profiles"] = profiles
            self.data["jvm"] = self._normalize_jvm(saved.get("jvm"))
            current = str(saved.get("current_profile") or "").strip()
            self.data["current_profile"] = (
                current if current in profiles else next(iter(profiles))
            )
            if not saved or rebuilt:
                self.save()
                logger.info("已生成初始配置文件")
            elif upgraded:
                # 迁移过就把新结构写回去：不然下次启动还得再迁一遍，
                # 而且文件上永远不写 config_version（等于没记录这一代）。
                logger.info("配置已升级，写回新结构")
                self.save()
            else:
                # 新增的配置项（比如 github_mirror_presets）在老配置里没有：
                # 顺手补写一次。「想加自己的镜像站就改 config.json」得先让
                # 那一项**出现在文件里**，否则要等用户哪天点了「保存设置」
                # 它才冒出来。补一次就不再缺，不会每次启动都写盘。
                added = [k for k in self.GLOBAL_DEFAULTS if k not in saved]
                if added:
                    logger.info(f"配置里补上新增项: {', '.join(added)}")
                    self.save()

    def _migrate(self, saved: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """把任意历史版本的配置升到 ``CONFIG_VERSION``。

        返回 ``(整理后的配置, 是否真的升过级)`` —— 第二个值用来决定要不要
        立刻把文件重写一遍（升级完不落盘的话，下次启动会再迁一次）。

        规则（发布之后要守住）：

        * 缺 ``config_version`` = **第 0 代**（加这个字段之前的所有配置）；
        * 只按 ``MIGRATIONS`` 里登记的函数逐级往上走，一级一个函数，
          不做「跳级猜测」；
        * 迁移函数**不许抛异常**（这里还会再包一层）—— 升级配置不该让
          启动器打不开，失败就退回「按原样读」；
        * 文件的版本**比本代码新**：不动它，只把认识的项读出来，
          不认识的键原样保留（见 ``_extras``），并且**不会**把版本号改小
          （见 ``save``）—— 这样「新版本写的配置被旧启动器读过一次」
          也不会掉数据、不会让版本号倒退。
        """
        if not isinstance(saved, dict):                       # pragma: no cover
            return {}, False
        raw_version = saved.get(CONFIG_VERSION_KEY)
        try:
            file_version = int(raw_version)
        except (TypeError, ValueError):
            file_version = 0
        self._file_version = max(0, file_version)
        if file_version > CONFIG_VERSION:
            logger.warning(
                f"配置来自更新的启动器（配置版本 {file_version} > "
                f"{CONFIG_VERSION}）：本次只读认识的项，其余原样保留。"
                "想用全部功能请更新启动器。"
            )
            return saved, False
        upgraded = False
        for version in range(max(0, file_version), CONFIG_VERSION):
            migrate = MIGRATIONS.get(version)
            if migrate is None:
                continue
            logger.info(f"升级配置：版本 {version} -> {version + 1}")
            try:
                result = migrate(saved)
                if isinstance(result, dict):
                    saved = result
                    upgraded = True
            except Exception as e:                              # noqa: BLE001
                logger.error(
                    f"配置迁移 {version} -> {version + 1} 失败（按原样继续）: {e}"
                )
        return saved, upgraded

    def _quarantine_bad_config(self) -> None:
        """坏配置先改名留档，再重建。

        直接把它覆盖掉等于把用户的分类列表和备份策略静默抹了；
        留一份 ``config.json.bad`` 至少还能手动翻出来对照。
        """
        try:
            bad = self.file.with_name(self.file.name + ".bad")
            bad.unlink(missing_ok=True)
            self.file.rename(bad)
            logger.warning(f"损坏的配置已留档为 {bad.name}，将重建默认配置")
        except OSError as e:
            logger.error(f"留档损坏的配置失败: {e}")

    def _normalize_profile(self, name: str, raw: Any) -> dict[str, Any]:
        """清一份分类配置。

        ★ 不认识的子键**原样带着**（跟顶层键一个道理）：分类配置将来可能
        被更新的版本加上新项（备份策略、每个分类的 JVM 参数……），旧版本
        读过一次不能把它们抹掉。
        """
        profile = dict(self.PROFILE_DEFAULTS)
        if isinstance(raw, dict):
            for key, default in self.PROFILE_DEFAULTS.items():
                value = raw.get(key)
                if value is None:
                    continue
                profile[key] = self._coerce_profile(name, key, value)
            for key, value in raw.items():
                if key not in self.PROFILE_DEFAULTS:
                    profile[key] = value
        if not profile["data_dir"]:
            profile["data_dir"] = str(default_data_dir(name))
        profile["data_dir"] = str(Path(profile["data_dir"]).resolve())
        return profile

    def _default_profile(self, name: str) -> dict[str, Any]:
        profile = dict(self.PROFILE_DEFAULTS)
        profile["data_dir"] = str(default_data_dir(name).resolve())
        return profile

    def _normalize_jvm(self, raw: Any) -> dict[str, Any]:
        """把 config.json 里 jvm 段清成一份可信的配置。

        默认值就是官方那份 vmArgs，所以**没有 config.json 也能启动** ——
        以前 Mindustry.json 是必需文件，缺了直接抛 FileNotFoundError。
        """
        jvm: dict[str, Any] = {
            "jre_path": self.JVM_DEFAULTS["jre_path"],
            "main_class": self.JVM_DEFAULTS["main_class"],
            "vm_args": list(DEFAULT_VM_ARGS),
            "program_args": [],
        }
        if raw is None:
            return jvm
        if not isinstance(raw, dict):
            logger.warning(
                f"配置里的 jvm 不是对象（{type(raw).__name__}），已改用默认值"
            )
            return jvm
        for key in ("jre_path", "main_class"):
            value = raw.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                logger.warning(f"jvm.{key}={value!r} 非法，已改用默认值")
                continue
            value = value.strip()
            # 主类名以 - 开头会被 JVM 当成选项解析，等于没给主类
            if key == "main_class" and (value.startswith("-") or " " in value):
                logger.warning(f"jvm.main_class={value!r} 不合法，已改用默认值")
                continue
            jvm[key] = value
        for key in ("vm_args", "program_args"):
            value = raw.get(key)
            if value is None:
                continue
            if isinstance(value, list) and all(
                isinstance(x, str) for x in value
            ):
                jvm[key] = list(value)
            else:
                logger.warning(f"jvm.{key} 必须是字符串数组，已忽略")
        return jvm

    # ---------- 游戏启动配置（jvm 段） ----------

    def get_jvm(self, key: str) -> Any:
        with self._lock:
            return self.data["jvm"].get(key, self.JVM_DEFAULTS.get(key))

    def get_jvm_config(self) -> dict[str, Any]:
        """取整个 jvm 段的副本（启动时用）。"""
        with self._lock:
            return dict(self.data["jvm"])

    def set_jvm(self, key: str, value: Any) -> None:
        if key not in self.JVM_DEFAULTS:
            raise KeyError(f"不是 jvm 配置项: {key}")
        with self._lock:
            self.data["jvm"][key] = value
        logger.debug(f"jvm.{key} 设置为 {value!r}")

    def save(self) -> None:
        """写盘。

        ★ 两个「兼容性」动作都在这里：

        1. 写上 ``config_version`` —— 下次启动时好判断要不要迁移。
           ★ **只升不降**：如果读进来的是更新的版本（比如 99），写回去的
           还是 99 —— 一个旧启动器存一次盘就把版本号改小，会让新启动器
           以为要重跑一遍迁移，那正是最容易把数据搞坏的时刻；
        2. 把 ``_extras``（本版本不认识的顶层键）**原样带回去** ——
           否则「新版本写的配置被旧启动器读过一次」就等于把那几项删了。
        """
        with self._lock:
            payload = dict(self.data)
            payload[CONFIG_VERSION_KEY] = max(
                CONFIG_VERSION, self._file_version
            )
            for key, value in self._extras.items():
                payload.setdefault(key, value)
            try:
                atomic_write_json(self.file, payload)
                logger.debug("配置已保存")
            except OSError as e:
                logger.error(f"保存配置失败: {e}")

    # ---------- 全局设置 ----------
    # 注意：set / set_profile_setting 只改内存，不落盘。
    # 一批改动请在末尾调用一次 save()，避免一次操作写好几次文件
    # （每次写盘都要 fsync，既卡又容易和多开的实例撞车）。

    def get(self, key: str) -> Any:
        return self.data.get(key, self.GLOBAL_DEFAULTS.get(key))

    def set(self, key: str, value: Any) -> None:
        """改一个全局配置项（只改内存，不落盘；末尾统一 save()）。

        ★ 走的是和读文件同一张规格表（``_coerce_global``）：界面传进来的
        值和手改 config.json 的值得到同样的处理结果。以前这里用的是
        ``type(default)(value)``，于是 ``set("max_log_files", "很多份")``
        会直接把 ValueError 抛到调用方（界面线程）上，而
        ``set("github_mirror_presets", "https://a/")`` 会存成一串单字符。
        """
        if key in self.GLOBAL_DEFAULTS:
            with self._lock:
                self.data[key] = self._coerce_global(key, value)
            logger.debug(f"全局配置项 {key} 设置为 {value}")
        else:
            # 不认识的键：以前是**静默忽略**。发布之后这很容易变成
            # 「界面改了但没生效」那种查不出来的问题，所以留一条 WARNING。
            logger.warning(f"忽略未知的全局配置项: {key}")

    # ---------- 存档分类 ----------

    def get_profiles(self) -> dict[str, str]:
        with self._lock:
            return dict(self.data["profiles"])

    def get_profile_names(self) -> list[str]:
        return list(self.get_profiles().keys())

    def get_current_profile(self) -> str:
        with self._lock:
            return self.data["current_profile"]

    def get_profile(self, name: str) -> dict[str, Any]:
        with self._lock:
            return dict(self.data["profiles"][name])

    def get_profile_setting(self, name: str, key: str) -> Any:
        with self._lock:
            return self.data["profiles"][name].get(
                key, self.PROFILE_DEFAULTS.get(key)
            )

    def set_profile_setting(self, name: str, key: str, value: Any) -> None:
        if key not in self.PROFILE_DEFAULTS:
            raise KeyError(f"不是分类级配置项: {key}")
        with self._lock:
            if name not in self.data["profiles"]:
                raise KeyError(name)
            # 同全局项：走统一的规格表（开关型不会被 bool("false") 反过来，
            # 数值型在这里就夹好上下限）。
            self.data["profiles"][name][key] = self._coerce_profile(
                name, key, value
            )
        logger.debug(f"分类「{name}」的 {key} 设置为 {value}")

    def get_save_path(self, name: str) -> str:
        return self.get_profile_setting(name, "data_dir")

    def get_current_save_path(self) -> str:
        return self.get_save_path(self.get_current_profile())

    def add_profile(
        self, name: str, save_path: str, **settings: Any
    ) -> None:
        name = sanitize_profile_name(name)
        profile = self._default_profile(name)
        profile["data_dir"] = str(Path(save_path).resolve())
        for key, value in settings.items():
            if key in self.PROFILE_DEFAULTS:
                # 同样过规格表：新建时传进来的「"false"」不能被当 True
                profile[key] = self._coerce_profile(name, key, value)
            else:
                # 参数名写错（`maxbackups=` 这种）以前是**静默丢掉**，
                # 表现是「明明传了却没生效」。留一条 WARNING。
                logger.warning(f"add_profile 收到不认识的设置项: {key}")
        with self._lock:
            if name in self.data["profiles"]:
                raise ValueError(f"存档分类「{name}」已存在")
            self.data["profiles"][name] = profile
        self.save()
        logger.info(f"新建存档分类「{name}」-> {save_path}")

    def rename_profile(self, old: str, new: str) -> None:
        new = sanitize_profile_name(new)
        if old == new:
            return
        with self._lock:
            if old not in self.data["profiles"]:
                raise KeyError(old)
            if new in self.data["profiles"]:
                raise ValueError(f"存档分类「{new}」已存在")
            self.data["profiles"] = {
                (new if k == old else k): v
                for k, v in self.data["profiles"].items()
            }
            if self.data["current_profile"] == old:
                self.data["current_profile"] = new
        self.save()
        logger.info(f"存档分类重命名:「{old}」->「{new}」")

    def set_profile_path(
        self, name: str, save_path: str, persist: bool = True
    ) -> None:
        """改分类的数据目录。

        persist=False 时只改内存，由调用方在整批改完后统一 save()，
        避免一次操作写好几次文件。
        """
        with self._lock:
            if name not in self.data["profiles"]:
                raise KeyError(name)
            self.data["profiles"][name]["data_dir"] = str(
                Path(save_path).resolve()
            )
        if persist:
            self.save()
        logger.info(f"存档分类「{name}」数据目录改为 {save_path}")

    def set_current_profile(self, name: str) -> None:
        with self._lock:
            if name not in self.data["profiles"]:
                raise KeyError(name)
            self.data["current_profile"] = name
        self.save()
        logger.info(f"切换存档分类 ->「{name}」")

    def remove_profile(self, name: str) -> None:
        with self._lock:
            if name not in self.data["profiles"]:
                raise KeyError(name)
            if len(self.data["profiles"]) <= 1:
                raise ValueError("至少需要保留一个存档分类")
            del self.data["profiles"][name]
            if self.data["current_profile"] == name:
                self.data["current_profile"] = next(iter(self.data["profiles"]))
        self.save()
        logger.info(f"删除存档分类「{name}」")


# ---------------------------------------------------------------------------
# 配置迁移链：``{起始版本: 迁移函数}``。
#
# 什么时候往这里加东西（发布之后请严格遵守）：
#   * 只是**新增**一个带默认值的键 → 什么都不用做（老配置缺这一项会走默认，
#     新配置在旧版本里会被原样保留）；
#   * **改名 / 改含义 / 删键 / 换类型** → ``version.CONFIG_VERSION`` +1，
#     并在下面补一条 ``旧版本号: 函数``；
#   * 函数签名是 ``(整个配置 dict) -> 新的 dict``，**必须只读不改**传入的
#     那个 dict（返回新的一份），而且不许抛异常（load 里还会再兜一层）；
#   * 迁移**只按版本号逐级走**，不猜、不跳级 —— 这样每一级都能单独测。
#
# 例（照着抄）：
#     def _migrate_1_to_2(saved):
#         old = saved.pop("use_mirror", None)      # 旧键
#         saved = dict(saved)
#         saved.setdefault("github_mirror", "https://ghfast.top/" if old else "")
#         return saved
#     MIGRATIONS[1] = _migrate_1_to_2
# ---------------------------------------------------------------------------
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


def _migrate_0_to_1(saved: dict) -> dict:
    """第 0 代（没有 config_version 的配置）升到第 1 代。

    第 1 代的变化：新增 ``version_sources``（额外的「从哪儿下游戏版本」来源）。
    实质上是「补一个空数组」，但**必须**写在迁移里而不是靠默认值 ——
    ``load()`` 的补写逻辑只在「文件里缺这个键」时触发一次，而迁移是
    「老文件明确升级」的语义，两者混起来以后就没法判断一份配置到底是
    「第 1 代但用户删了这一项」还是「第 0 代还没升过」。
    """
    result = dict(saved)
    result.setdefault("version_sources", [])
    return result


MIGRATIONS[0] = _migrate_0_to_1
