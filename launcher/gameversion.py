# -*- coding: utf-8 -*-
"""从游戏本体（jar）里读版本号 —— 导入版本时不再靠人填。

Mindustry 的 ``mindustry.core.Version`` 启动时读 jar 根目录的
``version.properties``，里面的 ``build`` 就是游戏自己的版本号（146 的 jar
写着 ``build=146``，160.8 写着 ``build=160.8``）。这里做的是同一件事，只是
提前到**导入那一刻** —— 版本号不该由用户凭记忆敲。

分支（MindustryX）不一样：它的 ``version.properties`` 写的是**上游**游戏
版本（X38 的 jar 里是 160.5），发布号在 jar 根的 ``mod.hjson`` 里
（``version: "2026.10.X38"``）。两个都读：有 ``mod.hjson`` 的版本号就用它，
官方版用 ``build``；分支读不到发布号时**留空**（不拿上游版本顶）——
和游戏自己显示的那个版本一致。

三条边界
--------
1. **读不到就返回空版本，让调用方回到手填**。版本号会变成清单文件名和列表
   里那一行，猜错比让用户填一次更贵（同「拿不到基线就退全量，绝不猜」）。
   ★ 分支的发布号（``mod.hjson``）读不到时也**不拿上游 ``build`` 顶** ——
     那会看着像一个很老的版本，排序也跟着错。
2. **只认游戏本体**。jar 里没有 ``mindustry/core/Version.class`` 就不算游戏
   （普通 mod 也可能带 ``mod.hjson``，照着它起名字会得到一个根本启动不了的
   「版本」）。
3. **本模块只 import 标准库**：它坐在最底层（界面和命令行都要用），不能反着
   依赖 ``config`` / ``storage``，否则又是一个环。
"""
from __future__ import annotations

import logging
import re
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 游戏自己认的两个文件（都在 jar 根目录）。
VERSION_PROPERTIES = "version.properties"
MOD_HJSON = "mod.hjson"
# 有它才算「这是游戏本体」，不是普通 mod / 随便一个 jar。
GAME_CLASS = "mindustry/core/Version.class"
# 认出分支的包名前缀（MindustryX 把 loader 放在这个包里）。
FORK_PACKAGE = "mindustryX/"

# 两个文件都很小；读之前先看长度，别让一个坏 jar 让我们解压几百 MB。
_MAX_META_BYTES = 256 * 1024
# 版本号上限和 ``config.sanitize_version_name`` 保持一致（那边也会再挡一次）。
MAX_VERSION_LEN = 40
_FORBIDDEN = set('\\/:*?"<>|')


@dataclass(frozen=True)
class JarInfo:
    """从 jar 里读到的版本信息（全都可能是空串 —— 读不到就是读不到）。"""

    #: 拿来当版本号的那个：分支用 mod.hjson，官方用 version.properties 的 build
    version: str = ""
    #: version.properties 里的 build（分支的 jar 里是**上游**游戏版本）
    base: str = ""
    #: 认出来的类型（``Mindustry`` / ``MindustryX``）；认不出是空串
    vtype: str = ""
    #: version.properties 的 type / modifier（official、release 之类，只用于提示）
    channel: str = ""
    modifier: str = ""
    #: 版本号是从哪个文件读来的（``version.properties`` / ``mod.hjson``）
    source: str = ""
    #: jar 里有没有 ``mindustry/core/Version.class``
    is_game: bool = False

    @property
    def from_fork(self) -> bool:
        """版本号是不是分支自己的发布号（而不是上游游戏版本）。"""
        return self.source == MOD_HJSON and bool(self.base)


def _clean(value: str) -> str:
    """收拾一个从 jar 里读出来的值（版本号要拼进文件名，宁缺毋滥）。"""
    value = re.sub(r"[\x00-\x1f\x7f]", "", value or "").strip().strip("\"'")
    if not value or len(value) > MAX_VERSION_LEN:
        return ""
    if _FORBIDDEN & set(value):
        return ""
    return value


def _unescape(value: str) -> str:
    """``version.properties`` 的转义（构建时间那种 ``20\\:24`` 就这么写）。"""
    return re.sub(r"\\(.)", r"\1", value)


def _parse_properties(text: str) -> dict[str, str]:
    """够用的 Java properties 解析（只认 ``键=值`` 与 ``#`` 注释）。

    ★ 刻意不做完整实现（多行续写、``键:值``）：这两个文件是游戏构建时生成的，
      形状固定；写一套完整解析器反而多一处会错的地方。认不出来就当作没读到，
      调用方回到手填。
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        out[key.strip()] = _unescape(value.strip())
    return out


def _hjson_scalar(text: str, key: str) -> str:
    """取 hjson 的**顶层**标量（``version: "2026.10.X38"``）。

    只看不缩进的行：``mod.hjson`` 里嵌套对象的键都缩进，顶层的没有。
    引号可有可无，行尾的 ``#`` / ``//`` 注释一并去掉。
    """
    for raw in text.splitlines():
        if not raw or raw[0] in " \t#/":        # 缩进 = 嵌套；整行注释
            continue
        head, sep, rest = raw.partition(":")
        if not sep or head.strip() != key:
            continue
        value = rest.strip()
        quoted = re.match(r"""^(["'])(.*?)\1""", value)
        if quoted:
            return quoted.group(2).strip()
        value = re.split(r"\s+(?:#|//)", value, maxsplit=1)[0]
        return value.strip().rstrip(",").strip()
    return ""


def _read_text(zf: zipfile.ZipFile, name: str) -> str:
    """读 jar 里一个小文本文件；太大 / 读不出来都当没读到。"""
    try:
        info = zf.getinfo(name)
        if info.file_size > _MAX_META_BYTES:
            logger.info(f"{name} 异常大（{info.file_size} 字节），不按版本文件读")
            return ""
        return zf.read(info).decode("utf-8", "replace")
    except (
        KeyError,
        OSError,
        RuntimeError,
        EOFError,
        NotImplementedError,      # 不认识的压缩算法
        zipfile.BadZipFile,
        zlib.error,               # 坏 deflate 流
    ) as e:
        logger.info(f"读不出 {name}：{e}")
        return ""


def read_jar_info(jar_path: str | Path) -> JarInfo | None:
    """读一个 jar 的版本信息。

    返回 ``None`` = 这个文件根本打不开（不是 zip / 被占着 / 路径不对）；
    返回 ``JarInfo`` 时看 ``is_game`` 与 ``version`` 是否为空。

    不抛异常：这是导入对话框在「选完文件」那一刻调用的，一个打不开的 jar
    不该把事情炸到界面上 —— 让用户看见一句「读不到，请手填」就够了。
    """
    path = Path(jar_path)
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            is_game = GAME_CLASS in names
            props: dict[str, str] = {}
            mod: dict[str, str] = {}
            if is_game:
                if VERSION_PROPERTIES in names:
                    props = _parse_properties(
                        _read_text(zf, VERSION_PROPERTIES)
                    )
                if MOD_HJSON in names:
                    text = _read_text(zf, MOD_HJSON)
                    mod = {
                        "version": _clean(_hjson_scalar(text, "version")),
                        "name": _hjson_scalar(text, "name"),
                        "minGameVersion": _clean(
                            _hjson_scalar(text, "minGameVersion")
                        ),
                    }
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as e:
        logger.info(f"打不开 jar（{path}）：{e}")
        return None

    if not is_game:
        # 普通 mod / 别的 jar：能读，但不是游戏本体。不给版本号，免得
        # 用户不知不觉导进来一个「看起来有版本、点启动就炸」的东西。
        return JarInfo()

    base = _clean(props.get("build", ""))
    mod_version = mod.get("version", "") if mod else ""

    # 类型：先看分支包名（MindustryX 的 jar 里同时有 mindustry/ 与
    # mindustryX/），再退到 jar 根 mod.hjson 里的 name。
    vtype = "Mindustry"
    if any(n.startswith(FORK_PACKAGE) for n in names) or (
        mod and mod.get("name", "").lower() == "mindustryx"
    ):
        vtype = "MindustryX"

    if mod_version:
        version, source = mod_version, MOD_HJSON
    elif vtype != "Mindustry":
        # 分支的发布号读不到时，**绝不拿上游 build 冒充**（X38 的 build 是
        # 160.5，填进去会看着像个老版本，排序也跟着错）。留空让用户填。
        version, source = "", ""
    else:
        version, source = base, (VERSION_PROPERTIES if base else "")

    return JarInfo(
        version=version,
        base=base or (mod.get("minGameVersion", "") if mod else ""),
        vtype=vtype,
        channel=_clean(props.get("type", "")),
        modifier=_clean(props.get("modifier", "")),
        source=source,
        is_game=True,
    )
