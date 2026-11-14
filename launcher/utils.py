# -*- coding: utf-8 -*-
"""通用工具：SSL 兼容、日志、原子写入、目录压缩、文件下载。"""
import hashlib
import http.client
import json
import logging
import os
import re
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import ghauth

__all__ = [
    "BASE_DIR",
    "DEV_LOG_NAME",
    "LANGUAGE_AUTO",
    "LANGUAGE_CODES",
    "LANGUAGE_DEFAULT",
    "LANGUAGES",
    "LOG_FILE",
    "LOG_NAME",
    "RESOURCE_DIR",
    "UNVERIFIED_SSL_CTX",
    "atomic_write_json",
    "atomic_write_text",
    "download_file",
    "download_tmp_path",
    "file_sha256",
    "logger",
    "normalize_language",
    "parse_bool",
    "resource_path",
    "resolve_log_file",
    "setup_logging",
    "ssl_context",
    "set_ssl_verify",
    "zip_directory",
]

# GitHub 直连在国内常撞上证书吊销检查失败（CRYPT_E_NO_REVOCATION_CHECK），
# 所以默认**不校验**证书（= 本启动器一直以来的行为）。
#
# ⚠️ 以前这里是 ``ssl._create_default_https_context = _create_unverified_context``
# —— 那是**进程级**副作用：会把整个进程（包括别处 import 进来的库）的证书
# 校验一起关掉。现在只把 context 显式传给自己的请求（download_file、
# updates._fetch_release、selfupdate、mirrortest），影响面就只剩这几处。
#
# ★ 但这只是**绕路**，不是根因：用 S302 之类工具的人，根证书只注入到了
#   Windows 系统证书库，而 Python 走 OpenSSL 自己的默认路径，未必读得到
#   —— 表现出来就是「浏览器能上、启动器报证书错误」。所以做成了开关
#   （配置里叫 ``disable_ssl_verify``，默认**开** = 不校验，保持历史行为）：
#   要严格校验的人在设置页把「禁用校验」取消掉即可；要让它认自定义 CA，
#   设 ``SSL_CERT_FILE`` 环境变量即可。
#
# ★ 这里（utils）只用**正向**说法：``set_ssl_verify(True)`` = 校验证书。
#   配置键是反向的（禁用），取反在 config._apply_ssl_verify 一处完成 ——
#   两套说法混着用，将来一定会有人把 True 当成「禁用」。
UNVERIFIED_SSL_CTX = ssl._create_unverified_context()
_VERIFIED_SSL_CTX: ssl.SSLContext | None = None
_ssl_verify = False


def set_ssl_verify(enabled: bool) -> None:
    """开关 HTTPS 证书校验（配置读盘 / 改设置后由 config 同步过来）。

    ★ 参数是**正向**的：True = 校验证书，False = 不校验（默认）。
      配置里的 ``disable_ssl_verify`` 是反的，取反在 config 那边做。
    ★ 只影响**本启动器自己发起**的那几个请求，不动进程级默认上下文。
    """
    global _ssl_verify
    _ssl_verify = bool(enabled)
    logger.info(
        "HTTPS 证书校验已%s" % ("开启" if _ssl_verify else "关闭")
    )  # i18n: keep —— 只进日志的诊断行


def ssl_context() -> ssl.SSLContext:
    """当前该用的 SSL context（按 ``disable_ssl_verify`` 开关，默认不校验）。

    ★ 必须**每次调用时**取，不能在模块顶层把结果存成常量 —— 用户可以在
      设置页里改这个开关，存成常量就得重启才生效。
    """
    global _VERIFIED_SSL_CTX
    if not _ssl_verify:
        return UNVERIFIED_SSL_CTX
    if _VERIFIED_SSL_CTX is None:
        try:
            ctx = ssl.create_default_context()
        except Exception as e:                                  # noqa: BLE001
            # 连默认上下文都建不出来（极罕见）：退回不校验，别让下载直接死
            logger.warning(f"建立证书校验上下文失败（{e}），本次不校验证书")
            return UNVERIFIED_SSL_CTX
        # ★ Windows 上 create_default_context() 只枚举**本机**证书库
        #   （ROOT / CA），不读 OpenSSL 的默认路径 —— 也就是说
        #   ``SSL_CERT_FILE`` 在这儿不会自动生效。S302 这类工具恰恰是把
        #   根证书注入用户证书库，于是「浏览器能上、Python 报证书错误」。
        #   这里显式补一次：环境变量指到哪张证书，就额外信哪张。
        extra_ca = os.environ.get("SSL_CERT_FILE", "").strip()
        if extra_ca and os.path.isfile(extra_ca):
            try:
                ctx.load_verify_locations(cafile=extra_ca)
                logger.info(f"已额外信任 SSL_CERT_FILE 指定的证书: {extra_ca}")
            except (OSError, ssl.SSLError) as e:
                logger.warning(f"加载 SSL_CERT_FILE 失败（{e}），只用系统证书库")
        _VERIFIED_SSL_CTX = ctx
    return _VERIFIED_SSL_CTX

# 实际使用（打包后的 exe）写的日志
LOG_NAME = "launcher.log"
# 源码运行（开发/调试/测试/跑验证脚本）写的日志 —— 单独一个文件，
# 免得测试的噪音把真正用起来时的日志冲掉、或者反过来被占住删不掉。
DEV_LOG_NAME = "launcher.dev.log"

# 显式指定日志文件（测试脚本用它把日志引到临时目录，绝不碰真实日志）。
# 值为路径；相对路径按进程 cwd 解析。
LOG_FILE_ENV = "MDT_LOG_FILE"


# ---- 开关值解析（全项目唯一实现）----------------------------------------
# ★ 放在 utils 而不是 config：config 和 sources 都要用它，而 sources 不能
#   import config（config 要 import sources，会绕成环）。这里谁都 import 得到。
#
# 为什么必须专门解析：``bool("false")`` 在 Python 里是 **True** —— 配置里
# 写成字符串 "false"，开关反而被打开。对 ``close_on_game_exit`` 来说那是
# 「启动器自己关了」，对 ``permanent_delete`` 来说是「删了不能还原」。
TRUE_WORDS = frozenset({"true", "1", "yes", "y", "on", "t"})
FALSE_WORDS = frozenset({"false", "0", "no", "n", "off", "f"})


def parse_bool(raw: Any, default: bool, *, what: str = "开关") -> bool:
    """把配置里的开关值清成真正的 bool。**不抛异常。**

    认：真 bool、0/1、以及 "true/false/yes/no/on/off/y/n/t/f"（不分大小写）。
    其它一律退回默认值 + WARNING（不猜用户的意思）。
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):                     # 0 / 1（JSON 里可能写成数字）
        return bool(raw)
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
    logger.warning(f"配置项 {what}={raw!r} 不是布尔值，按默认 {default} 处理")
    return default


# ---- 启动器自更新的档位（同样放这里，理由见上面的 parse_bool）------------
# 三档：auto = 自动检查+下载，退出时应用；check = 只提示；off = 完全停用。
# ★ 定义在 utils 是因为 **config 和 selfupdate 都要用它**，而 selfupdate
#   要 import config（读镜像、读档位）—— 解析逻辑留在 selfupdate 里就会
#   绕成 config <-> selfupdate 的循环 import。
LAUNCHER_UPDATE_MODES = ("auto", "check", "off")
LAUNCHER_UPDATE_DEFAULT = "auto"


def normalize_launcher_update(
    raw: Any, *, default: str = LAUNCHER_UPDATE_DEFAULT,
    what: str = "launcher_update",
) -> str:
    """把「启动器自更新」的档位清成三档之一。**不抛异常。**

    认不出的一律退回默认 + WARNING（并**点名配置键**，用户得知道去
    config.json 里改哪一行）。按兼容约定：以后要加档位只能往
    ``LAUNCHER_UPDATE_MODES`` 里加，别改现有三个词的含义。
    """
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in LAUNCHER_UPDATE_MODES:
            return word
    logger.warning(
        f"配置项 {what}={raw!r} 不是 {'/'.join(LAUNCHER_UPDATE_MODES)} 之一，"
        f"按默认 {default} 处理"
    )
    return default


# ---- 界面语言（同样放这里，理由见上面的 parse_bool）----------------------
# config 要**强转**这个值（界面选了要存、手改了要清），i18n 要拿它去取译文，
# 而 i18n 只能依赖 utils（它要读语言包文件）—— 定义留在 config 或 i18n
# 都会绕成循环 import。
#
# ★ 显示名用它**自己的语言**写：英文用户永远看到 "English"、中文用户看到
#   「简体中文」。界面语言一旦认错，用户至少还能认出自己那一行 ——
#   要是写成对方的语言，一个只懂英文的人会同时看不懂界面也看不懂选项。
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("zh_CN", "简体中文"),   # i18n: keep —— 语言名一律用「本语言」写，翻过去就认不出了
    ("en_US", "English"),
)
LANGUAGE_CODES = tuple(code for code, _ in LANGUAGES)
# 跟随系统：运行期由 i18n 解析成上面某一个具体 code。
LANGUAGE_AUTO = "auto"
# 认不出配置值时的落点。选中文而不是 auto：本项目的用户与文案都是中文优先，
# 而 auto 在探测失败时最终还是要落回这里，绕一圈没有意义。
LANGUAGE_DEFAULT = "zh_CN"


def normalize_language(
    raw: Any, *, what: str = "language", default: str = LANGUAGE_AUTO,
) -> str:
    """把「界面语言」清成 auto 或受支持的 code 之一。**不抛异常。**

    ★ 认不出的值退 ``auto``（跟随系统），**不是**硬退中文：用户写的可能是
    一门我们还没做的语言，跟随系统至少不会把他锁死在中文界面里。
    ★ WARNING 里点名配置键 —— 与其它枚举项一样，用户得知道去 config.json
    的哪一行改。
    """
    if isinstance(raw, str):
        word = raw.strip()
        if word == LANGUAGE_AUTO or word in LANGUAGE_CODES:
            return word
    logger.warning(
        f"配置项 {what}={raw!r} 不是 {LANGUAGE_AUTO}/{'/'.join(LANGUAGE_CODES)}"
        f" 之一，按 {default} 处理"
    )
    return default


def resolve_log_file(base_dir: Path) -> Path:
    """决定日志写到哪个文件。

    优先级：
      1. 环境变量 ``MDT_LOG_FILE``（测试/冒烟用，可指到临时目录）
      2. 打包后的 exe（真正在用）→ ``<数据根>/launcher.log``
      3. 源码运行（开发/测试）    → ``<数据根>/launcher.dev.log``

    ★ 铁律：**源码/测试运行绝不往 launcher.log 写**。实际使用的日志要能
    干净地反映「用户双击 exe 跑出来的这一次」，不能被开发时的跑动污染。
    """
    override = os.environ.get(LOG_FILE_ENV)
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        return base_dir / LOG_NAME
    return base_dir / DEV_LOG_NAME


def setup_logging(log_file: Path | None = None) -> None:
    """配置根 logger。不给 log_file 就用 ``LOG_FILE``（见 `resolve_log_file`）。

    注意：会清掉已有的 handler —— 重复调用是安全的，但后一次会顶掉前一次。
    """
    log_file = Path(log_file) if log_file else LOG_FILE
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    level = logging.DEBUG if "--debug" in sys.argv else logging.INFO
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    # 万一 MDT_LOG_FILE 指到还没建的目录，别让 import 阶段直接炸
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    fh.setLevel(level)
    root_logger.addHandler(fh)
    # 打包成 windowed exe 后没有控制台，sys.stderr 为 None。此时
    # StreamHandler.emit() 每写一条日志都会抛 AttributeError（被 logging
    # 静默吞掉，但白白付出异常开销）——所以只在有 stderr 时才加。
    if sys.stderr is not None:
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        ch.setLevel(level)
        root_logger.addHandler(ch)

def _app_dir() -> Path:
    """数据根：放 config.json / launcher.log / versions / backups 的地方。

    frozen（打包成 exe）时是 exe 所在目录——用 cwd 不可靠，从快捷方式或
    别的目录启动时 cwd 会变，配置和备份就会散落到意外位置。
    源码运行时保持原来的 Path.cwd() 语义不变。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path.cwd()


BASE_DIR = _app_dir()

# 资源根：jre / Mindustry.json 这类「跟着程序走、不由用户改」的文件。
# onefile 打包后它们被解压到 sys._MEIPASS，不在 BASE_DIR 里。
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", "") or BASE_DIR)

# 本次进程实际写日志的文件（见 resolve_log_file 的规则）。
LOG_FILE = resolve_log_file(BASE_DIR)

setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)


def resource_path(rel: str | Path) -> Path:
    """定位打包资源：优先 exe 旁边的外部副本，回退到包内解压目录。

    外部优先让用户能覆盖——想换 jre 版本，在 exe 旁边放个 jre/ 即可，
    不必重新打包。源码运行时两个根相同，行为与原来一致。
    """
    outer = BASE_DIR / rel
    if outer.exists():
        return outer
    return RESOURCE_DIR / rel


# 所有原子写入串行化。进程内多线程同时 os.replace 同一个目标，
# Windows 会直接甩 WinError 5（拒绝访问）——先在同进程里排好队，
# 外部占用（杀毒扫描等）再交给下面的重试处理。
_WRITE_LOCK = threading.Lock()

def atomic_write_text(
    path: Path, content: str, encoding: str = "utf-8"
) -> None:
    """原子写入文本。

    Windows 上 os.replace 报 WinError 5（拒绝访问）通常是两种情况：
      1. 本进程多个线程同时替换同一个目标；
      2. 目标文件被外部占用（杀毒软件正在扫、编辑器开着等）。
    第 1 种靠 _WRITE_LOCK 排掉，第 2 种靠重试；实在不行退化成
    直接覆盖写——宁可丢掉「原子性」也不能丢掉用户的配置。
    """
    path = Path(path)
    with _WRITE_LOCK:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        tmp_path: Path | None = Path(tmp)
        try:
            with os.fdopen(fd, "w", encoding=encoding) as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            # 临时文件已经落盘，之后只重试「替换」这一步，
            # 不再反复建/删临时文件（那本身又会惊动杀毒软件）。
            last_err: OSError | None = None
            for attempt in range(6):
                try:
                    os.replace(tmp_path, path)
                    if attempt:
                        logger.debug(
                            f"原子写入第 {attempt + 1} 次尝试成功: {path}"
                        )
                    return
                except OSError as e:
                    last_err = e
                    time.sleep(0.08 * (attempt + 1))  # 累计约 1.7 秒
            logger.error(f"原子写入失败 {path}: {last_err}")

            # 兜底：直接覆盖写。有些程序（编辑器、同步盘）会以
            # 「允许写、不允许删除」的方式占用文件，此时 replace 必失败，
            # 但普通写入是通的。
            try:
                with path.open("w", encoding=encoding) as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                logger.warning(f"原子替换长时间失败，已退化为直接写入: {path}")
                return
            except OSError as e2:
                logger.error(f"直接写入也失败 {path}: {e2}")
                raise last_err  # type: ignore[misc]
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(
        path, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def zip_directory(source: Path, dest: Path) -> int:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in source.walk():
            for file in files:
                f = root / file
                zf.write(f, f.relative_to(source))
    size = dest.stat().st_size
    logger.debug(f"压缩目录 {source} -> {dest}，大小 {size} 字节")
    return size

# 下载读块大小。以前是 8 KB（每个块都要过一遍 Python 层的循环，太小），
# 64 KB 与 mirrortest 的口径一致 —— syscall 次数和单次阻塞粒度的折中。
_DOWNLOAD_CHUNK = 64 * 1024


def file_sha256(path: Path) -> str:
    """整文件 sha256（1 MiB 读块）。

    断点续传时没法「边下边算」—— 文件前半段是上一条连接写的，哈希状态
    已经丢了 —— 只能下完从头再算一遍（87 MB 在 SSD 上约 0.3 秒）。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def format_size(num_bytes: float) -> str:
    """字节数 → 人看的字符串（1024 进制）：``3.2 MB`` / ``980 KB`` / ``12 B``。

    ★ 住在这里而不是 mirrortest：下载进度要显示速率，而 mirrortest 反过来
      import utils（依赖方向不能倒）。mirrortest 那边保留同名导出，老的
      ``mirrortest.format_size(...)`` 调用照旧能用。
    """
    value = float(num_bytes)
    unit = "B"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} B"
    if unit == "KB":
        # KB 只可能是 1~1023，整数够用 —— 「980.0 KB」白多一位小数
        return f"{value:.0f} KB"
    return f"{value:.1f} {unit}"


def download_tmp_path(identity: str, suffix: str = ".downloading") -> Path:
    """断点文件的**确定性**路径：同一 ``identity`` 永远得到同一个文件。

    ★ 断点续传的前提是「下一次还找得到上一次的半成品」—— 随机名字
    （mkstemp）做不到。``identity`` 传**原始下载地址**（不带镜像前缀）：
    GitHub release 资产不可变，同一地址就是同一份内容，天然做着「断点
    属于哪个文件」的隔离；拿错文件的残局由 sha256 校验兜底。
    """
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"mdt-dl-{digest}{suffix}"


def _parse_content_range(value: str) -> tuple[int, int]:
    """解析 ``Content-Range: bytes 100-999/1000`` → ``(起点, 总大小)``。

    拿不到（格式不是这个 / 是 ``*``）就返回 ``(-1, 0)``：起点 -1 = 不可信，
    总大小 0 = 不知道。
    """
    m = re.match(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)", value or "")
    if not m:
        return -1, 0
    return int(m.group(1)), (int(m.group(3)) if m.group(3) != "*" else 0)


def _try_unlink(path: Path) -> None:
    """删文件；删不掉就记一条日志（写入方会用覆盖写兜底，绝不让它抛）。"""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"删除文件失败 {path}: {e}")


def download_file(
    url: str,
    dest: Path,
    stop_event: threading.Event | None = None,
    on_progress: Callable[[float, float, float], None] | None = None,
    expected_sha256: str | None = None,
    expected_size: int = 0,
    resume: bool = False,
) -> bool:
    """下载 ``url`` 到 ``dest``；返回是否成功。

    ``resume=True`` 时支持**断点续传**：``dest`` 已有部分内容就先带
    Range 头问一次服务器 ——

      * 206（支持续传）：从断点接着写，进度从断点的百分比起步；
      * 200（不支持 / 或本来就没有断点）：从头下载、覆盖；
      * 416（本地已经 ≥ 远端）：内容对就直接算成功（断点即终点），
        不对就删了重下。

    ★ 失败时默认**保留**已下载的部分（下一个源 / 用户下次重试能接着
    下）—— 只有「已经下满但内容校验不过」「比目标还大」这种没救的情况
    才清掉。
    ``expected_size`` 是「任务期望的总大小」（0 = 不检查），用于核对
    服务器给的确实是我们以为的那个文件：**请求阶段**先比一次（200 收到
    Content-Length / 206 收到 Content-Range），**下完再比一次实际文件
    大小** —— 服务器把连接干净地关掉是不抛异常的，光看 sha 会漏掉
    「没有 sha 的清单」那条路。

    ``on_progress(百分比, 字节/秒, 剩余秒数)`` 只是显示用的，每秒最多一次：

      * 百分比封顶 100（服务器多给也不显示 138%）；
      * 速率是**实测**的（最近两次采样之间真正读到的字节 / 间隔），并用指数
        滑动平均压过抖动 —— 瞬时值会让「剩余时间」在 18 秒和 45 秒之间来回跳；
      * 剩余秒数按 ``总长 - 已下`` 除以平滑后的速率算，0 表示算不出来
        （还不知道总长 / 刚开始还没来得及采样）。

    ★ 它抛异常会被吞掉（记 WARNING）：不许因为界面的问题把 87 MB 的传输作废。
    """
    skip = 0
    if resume:
        try:
            skip = dest.stat().st_size
        except OSError:
            skip = 0

    resp = None
    total_remote = 0
    resumed = False
    # 最多两轮：第一轮带断点（如有），被服务器拒绝就地退成「从头下」
    #（重发一次不带 Range 的请求）。
    for _attempt in range(2):
        headers = {"User-Agent": "Mozilla/5.0"}
        if skip > 0:
            headers["Range"] = f"bytes={skip}-"
        req = urllib.request.Request(url, headers=headers)
        # 装了 gh 且已登录的话，给 GitHub 官方域名的下载带上认证（额度更高、
        # 私有资产也能下）；镜像前缀 / 自定义源这类第三方地址**不带**（白名单
        # 在 ghauth 里）。增强项：失败绝不影响下载本身。
        ghauth.apply_auth(req)
        try:
            resp = urllib.request.urlopen(
                req, timeout=30, context=ssl_context()
            )
        except urllib.error.HTTPError as e:
            if e.code == 416 and skip > 0:
                # 本地比远端还大（或正好相等）—— 断点没得续了。
                # 若大小正好等于目标且内容对得上：断点即终点，直接成功。
                if (
                    expected_size and expected_sha256
                    and skip == expected_size
                    and file_sha256(dest).lower() == expected_sha256.lower()
                ):
                    logger.info(f"本地文件已完整（断点即终点）: {dest}")
                    return True
                logger.warning(f"断点超出远端大小（HTTP 416），删除后重下: {dest}")
                _try_unlink(dest)
                skip = 0
                continue
            logger.error(f"下载失败 HTTP {e.code}: {url}")
            return False
        except (urllib.error.URLError, OSError,
                http.client.HTTPException) as e:
            # ★ 抓的是 HTTPException 这个**基类**：IncompleteRead、
            #   RemoteDisconnected（这两个以前单独列过）之外还有
            #   BadStatusLine / LineTooLong —— 代理或镜像回了非 HTTP 的
            #   东西时抛的就是它们。漏在外面就违反了本函数「返回是否成功」
            #   的约定，一路把异常冒进下载线程。
            logger.error(f"下载异常 {url}: {e}")
            return False

        if resp.status == 206 and skip > 0:
            start, total_remote = _parse_content_range(
                resp.headers.get("Content-Range", "")
            )
            if start != skip:
                logger.warning(
                    f"服务器给的断点位置对不上（{start} ≠ {skip}），"
                    f"弃用本地断点重下: {dest}"
                )
                resp.close()
                _try_unlink(dest)
                skip = 0
                continue
            if expected_size and total_remote and total_remote != expected_size:
                # 服务器上的文件跟任务预期不是一个东西 —— 换源，别浪费带宽
                logger.warning(
                    f"服务器文件大小（{total_remote}）与预期（{expected_size}）"
                    f"不符: {url}"
                )
                resp.close()
                return False
            resumed = True
            break
        if resp.status == 200:
            if skip > 0:
                logger.info(f"服务器不支持断点续传，从头下载: {url}")
                skip = 0
            break
        logger.warning(f"下载失败 HTTP {resp.status}: {url}")
        resp.close()
        return False
    else:
        # 两轮都没拿到可用的响应（416 循环）—— 保险丝
        logger.error(f"下载失败（断点重试次数用尽）: {url}")
        return False

    with resp:
        content_length = int(resp.headers.get("Content-Length", 0) or 0)
        # 200 那条路：服务器不认 Range、准备整份重下。**先核对总长再动磁盘**
        # —— 长度对不上说明这个源给的是别的文件（镜像把错误页当 200 发是
        # 常有的事）。这时候要是照常 "wb" 打开，上一轮镜像攒下的断点就被
        # 清零了，几十 MB 得从头再下。
        if (not resumed and expected_size and content_length
                and content_length != expected_size):
            logger.warning(
                f"这个源给的文件大小（{content_length}）与预期"
                f"（{expected_size}）不符，换下一个（保留断点）: {url}"
            )
            return False
        # 206 时 Content-Length 只是「剩余部分」的长度，总大小以
        # Content-Range 的 Z 为准；200 时 skip 已归零，直接相加。
        # ★ 两样都拿不到（服务器不发 Content-Length、靠关连接结尾）就拿
        #   expected_size 顶 —— 否则 total 恒为 0，进度回调一次都不发，
        #   87 MB 的下载在界面上永远是 0%，看着像卡死。
        total = total_remote or (skip + content_length) or expected_size
        if resumed:
            logger.info(
                f"从断点续传: 已有 {skip} / {total} 字节"
                + (f"（{skip / total * 100:.1f}%）" if total > 0 else "")
            )
        downloaded = skip
        last_update = time.time()
        last_bytes = downloaded          # 上次采样时的字节数（算瞬时速率）
        speed = 0.0                      # 平滑后的速率（字节/秒）
        sha256 = hashlib.sha256()
        try:
            with open(dest, "ab" if resumed else "wb") as f:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        logger.info("下载被用户取消")
                        return False
                    chunk = resp.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if not resumed:
                        # 续传过的文件前半段没经过这里，哈希对不上；
                        # 那时改用 file_sha256() 整文件重算。
                        sha256.update(chunk)
                    now = time.time()
                    span = now - last_update
                    if on_progress and total > 0 and span >= 1.0:
                        # ★ 先记时间/字节，再回调：回调要是每次都炸，也不会
                        #   变成「每读一块炸一次」的日志风暴；速率也不会被
                        #   回调自身的耗时摊薄（那是界面慢，不是网速慢）。
                        inst = (downloaded - last_bytes) / span
                        # 指数滑动平均（α=0.4）：瞬时速率每跳一下，剩余时间
                        # 就跟着在 18 秒 / 45 秒之间来回蹦，比不准更烦人。
                        speed = inst if speed <= 0 else speed * 0.6 + inst * 0.4
                        last_update = now
                        last_bytes = downloaded
                        # 剩余秒数：算不出来就给 0（界面据此不显示这一段）
                        remain = ((total - downloaded) / speed
                                  if speed > 0 else 0.0)
                        try:
                            on_progress(
                                min(100.0, downloaded / total * 100),
                                speed,
                                max(0.0, remain),
                            )
                        except Exception as e:                  # noqa: BLE001
                            # 进度只是**显示**：回调（界面）出错不许把下载
                            # 带崩 —— 否则关窗口时一个 TclError 就能让
                            # 已经下了 80 MB 的传输白费。
                            logger.warning(f"下载进度回调出错（已忽略）: {e}")
        except (urllib.error.URLError, OSError,
                http.client.HTTPException) as e:
            # HTTPException 基类兜住半路冒出来的协议层怪东西。中途断线时
            # 故意**不删**文件 —— 已下部分是下一次的断点
            logger.error(f"下载中断 {url}: {e}")
            return False

    # ---- 大小核对（最后一道）----
    # ★ 「连接干净地关掉」不会抛异常：服务器只发了一半也会走到这里。没有
    #   sha 的清单（老资产 / 自建源）以前会把半截文件当**成功**返回，
    #   靠调用方各自再查一遍；这里统一兜住，让「下载成功」真的成立。
    if expected_size:
        try:
            actual_size = dest.stat().st_size
        except OSError as e:
            logger.error(f"下载后读不到文件大小（{e}）: {dest}")
            return False
        if actual_size != expected_size:
            if actual_size > expected_size:
                # 比目标还大：这份数据修不好（续传的起点会被 416 顶回来）
                logger.error(
                    f"下载的大小不对（预期 {expected_size}，实际 {actual_size}，"
                    f"已删除）: {dest}"
                )
                _try_unlink(dest)
            else:
                # 没下满：留着，下一个源 / 下次重试还能接着下
                logger.error(
                    f"下载不完整（预期 {expected_size}，实际 {actual_size}，"
                    f"保留断点可续传）: {dest}"
                )
            return False

    if expected_sha256:
        actual = file_sha256(dest) if resumed else sha256.hexdigest()
        if actual.lower() != expected_sha256.lower():
            if total > 0 and downloaded >= total:
                # 下满了但内容不对：这份数据没救了，删掉（别让它冒充断点）
                logger.error(
                    f"SHA256 校验失败（文件已下满，已删除）: "
                    f"期望 {expected_sha256}, 实际 {actual}"
                )
                _try_unlink(dest)
            else:
                # 没下满：留着，下一个源 / 下次重试还能续着把它下完
                logger.error(
                    f"SHA256 校验失败（文件不完整，保留断点可续传）: "
                    f"期望 {expected_sha256}, 实际 {actual}"
                )
            return False
    logger.info(f"下载成功: {url} -> {dest}")
    return True
