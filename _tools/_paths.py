# -*- coding: utf-8 -*-
"""_tools 共用：定位「代码根」和「运行时目录」。

本项目把**开发**和**运行**分开：

    <开发目录>/      ← 代码根（本仓库：launcher/、_tools/、spec）
    <运行时目录>/    ← exe、_internal/、jre/、versions/、backups/、config.json、日志

所以脚本必须区分两件事：
    ROOT  —— 代码在哪（导入 launcher 包、找 spec）
    DATA  —— 真实数据在哪（versions/ 的 CAS、jre/、config.json、launcher.log）

找运行时目录的顺序（``find_runtime_root()``）：
    0. 环境变量 ``MDT_RUNTIME_DIR`` 指定的目录（设了就只认它，优先级最高）
    1. 代码根自己（源码与产物混放的老布局，兼容用）
    2. 同级的 ``RUNTIME_DIR_NAMES`` 里那几个名字，按顺序取第一个像运行时的
    3. 同级里任意一个「看起来像运行时」的目录

★ **路径是懒解析的**：import 本模块只算出 ``ROOT``（纯目录判断），
``DATA``/``VERSIONS``/... 这些要等到**真的被访问**时才去找。
这样「刚 clone 下来、还没有运行时目录」时，只想用 ROOT 的脚本
（比如构建）不会在 import 阶段就炸。

用法：
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # _tools/
    from _paths import ROOT, DATA          # DATA 在这一行才去解析
    from _paths import find_runtime_root   # 想自己控制「找不到怎么办」用它
"""
from __future__ import annotations

import os
from pathlib import Path

# 直接指定运行时目录：设了这个就用它，不再猜（找不到会明确报错）。
RUNTIME_DIR_ENV = "MDT_RUNTIME_DIR"

# 同级目录里认这几个名字，按顺序取第一个「看起来像运行时」的。
# `runtime` 是给新用户/新仓库的通用名；`测试版本` 是本项目历史上的约定名，
# 留在这里是为了兼容已有的目录布局（改名请优先用 MDT_RUNTIME_DIR）。
RUNTIME_DIR_NAMES: tuple[str, ...] = ("runtime", "测试版本")


def _find_project_root(start: Path) -> Path:
    """往上找含 launcher/__init__.py 的那一层。"""
    for p in (start, *start.parents):
        if (p / "launcher" / "__init__.py").is_file():
            return p
    raise RuntimeError(f"找不到项目根（从 {start} 往上没看到 launcher/__init__.py）")


# 代码根：本文件在 _tools/ 下，所以取它的上一级开始往上找
ROOT = _find_project_root(Path(__file__).resolve().parent)


def looks_like_runtime(p: Path) -> bool:
    """运行时目录的判据：有真实数据，或至少有打包好的程序。

    ⚠️ 不能只看 `versions/manifests` 目录存不存在 —— `VersionManager.__init__`
    会 `mkdir(parents=True, exist_ok=True)`，跑一次回归就会凭空建出**空**的
    `versions/manifests/`，于是这个判据永远为真。必须要求里面有 .json 清单。
    """
    md = p / "versions" / "manifests"
    if md.is_dir() and any(md.glob("*.json")):
        return True
    if (p / "Mindustry启动器.exe").is_file() and (p / "_internal").is_dir():
        return True
    return False


def find_runtime_root(required: bool = True) -> Path | None:
    """找运行时目录。``required=False`` 时找不到返回 ``None`` 而不是抛异常。

    环境变量 ``MDT_RUNTIME_DIR`` 一旦设了就**只认它**（哪怕它还不像运行时 ——
    用户明确指到哪就是哪，目录不存在才报错）。这样把运行时放到别处、
    或者用一个新的空目录重新开始，都不用改代码。
    """
    env = os.environ.get(RUNTIME_DIR_ENV)
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p
        if not required:
            return None
        raise RuntimeError(
            f"环境变量 {RUNTIME_DIR_ENV} 指向的目录不存在：{p}"
        )

    if looks_like_runtime(ROOT):
        return ROOT

    for name in RUNTIME_DIR_NAMES:
        cand = ROOT.parent / name
        if looks_like_runtime(cand):
            return cand

    if ROOT.parent.is_dir():
        for sib in sorted(ROOT.parent.iterdir()):
            if sib.is_dir() and sib != ROOT and looks_like_runtime(sib):
                return sib

    if not required:
        return None
    tried = "\n".join(
        [f"          {ROOT}"]
        + [f"          {ROOT.parent / n}" for n in RUNTIME_DIR_NAMES]
        + [f"          {ROOT.parent} 下的其它同级目录"]
    )
    raise RuntimeError(
        "找不到运行时目录（要有 versions/manifests 或 exe + _internal/）。\n"
        f"  已找过：\n{tried}\n"
        f"  也可以直接指定：设环境变量 {RUNTIME_DIR_ENV}=<目录>\n"
        "  这些脚本需要一个真实数据目录才能跑。"
    )


# 解析结果缓存（同一个进程里只找一次）
_cache: dict[str, Path] = {}


def data_root() -> Path:
    """运行时目录 = 数据目录（versions/ backups/ jre/ config.json 日志都在这儿）。"""
    if "data" not in _cache:
        _cache["data"] = find_runtime_root()
    return _cache["data"]


def _find_backups_dir(data: Path) -> Path:
    """备份根目录。代码里写的是小写 `backups`（`gui_core.py`），
    但历史上项目里那个目录叫 `Backups`（大写 B）—— Windows 不区分大小写，
    所以两种都可能出现，都要认。"""
    for name in ("Backups", "backups"):
        p = data / name
        if p.is_dir():
            return p
    return data / "Backups"      # 还不存在就按代码里的约定给个默认值


def _derived_paths() -> dict[str, Path]:
    """DATA 下面那些常用子路径（懒解析——只有真被用到才调）。"""
    data = data_root()
    backups = _find_backups_dir(data)
    return {
        "DATA": data,
        "VERSIONS": data / "versions",
        "MANIFESTS": data / "versions" / "manifests",
        "JRE": data / "jre",
        # 日志有两个，别搞混（规则见 launcher/utils.py 的 resolve_log_file）：
        #   LOG     —— **实际使用**（打包后的 exe）写的那份；用户双击 exe 才用它。
        #   DEV_LOG —— **源码/测试**运行写的那份（跑回归、跑脚本、调试看这个）。
        # 两者互不覆盖：测试的噪音不会冲掉真实日志，也就不会出现「测试跑完，
        # 真实日志被占住/被覆盖」的问题。
        # ⚠️ 源码运行时数据根是**进程 cwd**，未必等于 DATA。想拿「刚刚这次运行
        #    真正写的日志」，同进程里读 `launcher.utils.LOG_FILE` 最稳。
        "LOG": data / "launcher.log",
        "DEV_LOG": data / "launcher.dev.log",
        "BACKUPS": backups,
        "BACKUP_OBJECTS": backups / "objects",
    }


_DERIVED_NAMES = frozenset({
    "DATA", "VERSIONS", "MANIFESTS", "JRE", "LOG", "DEV_LOG",
    "BACKUPS", "BACKUP_OBJECTS",
})


def __getattr__(name: str) -> Path:
    """PEP 562：让 ``from _paths import DATA`` 这种老写法继续可用，
    但解析推迟到**真正访问**的那一刻。"""
    if name in _DERIVED_NAMES:
        return _derived_paths()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_DERIVED_NAMES])


def _main() -> int:
    print(f"代码根  ROOT = {ROOT}")
    try:
        data = data_root()
    except RuntimeError as e:
        print(f"运行时  DATA = <未找到>\n{e}")
        return 1
    paths = _derived_paths()
    print(f"运行时  DATA = {data}")
    for key, label in (
        ("VERSIONS", "versions"), ("JRE", "jre"), ("BACKUPS", "backups"),
    ):
        p = paths[key]
        print(f"  {label:<10} = {p}  ({'存在' if p.is_dir() else '缺失'})")
    md = paths["MANIFESTS"]
    n = len(list(md.glob("*.json"))) if md.is_dir() else 0
    print(f"  manifests  = {md}  ({n} 个清单)")
    for key, label, who in (
        ("LOG", "launcher.log", "实际使用（exe）写这份"),
        ("DEV_LOG", "launcher.dev.log", "源码/测试写这份"),
    ):
        p = paths[key]
        print(f"  {label:<16} = {p}  ({'存在' if p.is_file() else '缺失'})"
              f"  ← {who}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
