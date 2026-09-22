#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「发布包」zip —— 解压即用，不需要 Python、不需要单独装 Java。

和 make_source_zip.py 的区别：
    make_source_zip.py  → 给源码，使用者自己构建（要 Python + PyInstaller）
    make_release_zip.py → 给成品，双击就能用（本脚本）

★ 本仓库只放开发相关的东西，运行时产物在**运行时目录**里
  （找法见 `_paths.find_runtime_root`：环境变量 `MDT_RUNTIME_DIR` → 同级
  `runtime/` → 同级里任意一个像运行时的目录）。所以 exe / _internal / jre
  都从那儿取，用 `--runtime-dir` 可以指到别处。

包含：
    Mindustry启动器/
        Mindustry启动器.exe     程序本体
        _internal/              PyInstaller 运行时（993 个文件）
        jre/                    内置 Java（Temurin 25，95 个文件）
        Mindustry.json          启动配置（VM 参数，可改）
        使用说明.txt            面向使用者的说明

不含（刻意排除）：
    launcher/、_tools/          源码与开发脚本
    versions/、backups/         用户数据（几百 MB，是使用者的资产）
    config.json、launcher.log   用户状态
    *.spec、README.md           构建期的东西

一次生成**两个包**：
    Mindustry启动器_发布包_<日期>.zip     完整包，给新用户（解压即用）
    mindustry-launcher-v<版本>-update.zip  精简更新包，给已装旧版的用户自更新

更新包只装「相对上一版真正变动的文件」（exe 几乎每次都变，`_internal/` 只在改依赖时
才变），再往上一版没带 manifest.json 时用 `--baseline-from-zip` 拿它的发布包反推基线。

用法：
    python _tools/make_release_zip.py
    python _tools/make_release_zip.py --runtime-dir D:\\somewhere
    python _tools/make_release_zip.py --outdir D:\\somewhere
    python _tools/make_release_zip.py --baseline "上一版的 manifest.json"
    python _tools/make_release_zip.py --baseline-from-zip "上一版的发布包.zip"
    python _tools/make_release_zip.py --no-update      # 只出完整包
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------
# 项目根：路径一律从 _paths 拿（本文件就在 _tools/ 里，import 得到）。
# 不能用 Path(__file__).parent —— 本脚本在 _tools/ 里，那不是项目根。
# --------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, find_runtime_root   # noqa: E402

# zip 内的一级目录名，也是使用者解压后看到的文件夹名
TOP = "Mindustry启动器"
EXE_NAME = "Mindustry启动器.exe"

# 运行时目录里要打进发布包的东西（相对运行时目录）。
# 注：Mindustry.json 已经并进 config.json 的 jvm 段，不再是独立文件。
INCLUDE_FILES = [
    EXE_NAME,
]
INCLUDE_DIRS = [
    "_internal",
    "jre",
]

# --------------------------------------------------------------------------
# 自更新（启动器自己更新自己）—— 清单 + 精简更新包
# --------------------------------------------------------------------------
# 只有「程序文件」参与自更新：exe + _internal/。
# ★ 刻意**不含 jre/**：它不随版本变，而且用户可能已经自己换过一份
#   （resource_path 是「exe 旁边优先」），更新包去覆盖它只会帮倒忙。
UPDATE_DIRS = ["_internal"]
APP_ID = "mindustry-launcher"
UPDATE_FORMAT = 1
# 本地留档的 manifest（生成下一版的更新包时，拿它当基线算出「哪些文件变了」）
MANIFEST_DIR = ROOT / "_history" / "releases"


def read_version() -> str:
    """从 ``launcher/version.py`` 读版本号。

    不 import 那个模块（免得为了一个字符串把那包拖进来），直接按文本抓 ——
    格式就固定是 ``__version__ = "1.2.3"``（见该文件的模块注释）。
    """
    text = (ROOT / "launcher" / "version.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise SystemExit("从 launcher/version.py 里读不到 __version__")
    return m.group(1)


def _ver_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(text))) or ()


def program_items(base: Path) -> list[tuple[Path, str]]:
    """参与自更新的文件（exe + ``_internal/``），相对路径是**相对运行时目录**。

    这个相对路径就是要写回用户那边的位置（exe 同级），所以两边必须
    用同一套规则 —— 启动器那边是 ``selfupdate._is_replaceable`` 白名单。
    """
    items: list[tuple[Path, str]] = []
    exe = base / EXE_NAME
    if exe.is_file():
        items.append((exe, EXE_NAME))
    for rel in UPDATE_DIRS:
        src = base / rel
        if not src.is_dir():
            continue
        for f in _iter_dir(src):
            items.append((f, f"{rel}/{f.relative_to(src).as_posix()}"))
    return items


def build_manifest(items: list[tuple[Path, str]], version: str) -> dict:
    """给「这一版的程序文件」生成清单（完整包里带一份，同时本地留档）。"""
    return {
        "format": UPDATE_FORMAT,
        "app_id": APP_ID,
        "version": version,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": [
            {"path": rel, "sha256": _sha256(src), "size": src.stat().st_size}
            for src, rel in items
        ],
    }


def manifest_from_release_zip(zip_path: Path) -> dict:
    """从「上一版的发布包 zip」反推程序文件清单，当作基线。

    完整包自 1.0.1 起会带一份 `manifest.json`，正常不该用到这个。但更早
    发出去的包（1.0.0 就是）里没有它 —— 那时还没有自更新功能。这时可以
    退而求其次：直接量上一版发布包里每个文件的 sha256。

    发布包内层级是 `<TOP>/...`，而清单里的路径是**相对安装根**的，所以
    要削掉第一级目录名；只收 exe 与 `_internal/`（与
    `program_items` / 更新器的白名单一致），`jre/`、`使用说明.txt`、
    `LICENSE` 本来就不参与自更新，忽略掉。
    """
    files: list[dict] = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split("/")
            if len(parts) < 2:
                continue
            rel = "/".join(parts[1:])
            if rel != EXE_NAME and not rel.startswith("_internal/"):
                continue
            data = z.read(info)
            files.append({
                "path": rel,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            })
    if not files:
        raise SystemExit(
            f"{zip_path} 里找不到程序文件（exe / _internal/）—— 它不像发布包。"
        )
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", zip_path.name)
    return {
        "format": UPDATE_FORMAT,
        "app_id": APP_ID,
        "version": m.group(1) if m else "unknown",
        "created": f"（由发布包反推：{zip_path.name}）",
        "files": files,
    }


def load_baseline(
    version: str, explicit: Path | None
) -> tuple[dict | None, str]:
    """找「上一版」的 manifest 当基线。返回 ``(manifest, 来源说明)``。

    优先用 ``--baseline`` 明确指定的；否则在本地留档目录里挑**版本号比
    当前小、且最大的**那一份。都找不到就返回 ``(None, ...)`` —— 调用方
    会退化成「全量更新包」（大一点，但一定正确）。
    """
    if explicit is not None:
        try:
            return json.loads(explicit.read_text(encoding="utf-8")), str(explicit)
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"读不了基线 manifest {explicit}：{e}")
    if not MANIFEST_DIR.is_dir():
        return None, "没有本地留档目录"
    current = _ver_tuple(version)
    best: tuple[tuple[int, ...], Path] | None = None
    for path in sorted(MANIFEST_DIR.glob("manifest-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        ver = _ver_tuple(data.get("version") or "")
        if not ver or ver >= current:        # 只认**更旧**的版本当基线
            continue
        if best is None or ver > best[0]:
            best = (ver, path)
    if best is None:
        return None, "本地没有更旧版本的 manifest"
    return json.loads(best[1].read_text(encoding="utf-8")), best[1].name


def write_update_package(
    base: Path,
    out_dir: Path,
    version: str,
    baseline: dict | None,
    level: int,
) -> tuple[Path, int, int]:
    """生成精简更新包。返回 ``(路径, 打包文件数, 原始字节数)``。

    只装**相对基线有变化**的文件（exe 几乎每次都变，``_internal/`` 只在
    改依赖时才变），外加 ``update.json`` 说明要写哪些、删哪些。基线拿不到
    时把所有程序文件都装进去 —— 大一点，但绝不会漏。
    """
    items = program_items(base)
    entries = [
        {"path": rel, "sha256": _sha256(src), "size": src.stat().st_size,
         "_src": src}
        for src, rel in items
    ]
    base_hashes: dict[str, str] = {}
    if baseline:
        for f in baseline.get("files") or []:
            if isinstance(f, dict) and f.get("path"):
                base_hashes[str(f["path"])] = str(f.get("sha256") or "")
    if base_hashes:
        changed = [e for e in entries if base_hashes.get(e["path"]) != e["sha256"]]
        remove = sorted(set(base_hashes) - {e["path"] for e in entries})
    else:
        # 没有基线 → 全量（宁可多传几 MB，也不能少换一个文件）
        changed = list(entries)
        remove = []
    # exe 永远要带上：即使内容没变（理论上不会），少了它这一版就没意义
    if not any(e["path"] == EXE_NAME for e in changed):
        exe_entry = next((e for e in entries if e["path"] == EXE_NAME), None)
        if exe_entry is not None:
            changed.insert(0, exe_entry)

    manifest = {
        "format": UPDATE_FORMAT,
        "app_id": APP_ID,
        "version": version,
        "base_version": str((baseline or {}).get("version") or ""),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": [
            {"path": e["path"], "sha256": e["sha256"], "size": e["size"]}
            for e in changed
        ],
        "remove": remove,
    }
    zip_path = out_dir / f"{APP_ID}-v{version}-update.zip"
    if zip_path.exists():
        zip_path.unlink()
    raw = sum(e["size"] for e in changed)
    with zipfile.ZipFile(
        zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=level
    ) as z:
        z.writestr(
            "update.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        for e in changed:
            z.write(e["_src"], f"files/{e['path']}")

    # 逐条校验：解出来的字节必须跟磁盘一致（跟完整包同一套纪律）
    bad: list[str] = []
    with zipfile.ZipFile(zip_path) as z:
        for e in changed:
            entry = f"files/{e['path']}"
            if entry not in z.namelist():
                bad.append(f"{e['path']}（zip 里没有）")
            elif hashlib.sha256(z.read(entry)).hexdigest() != e["sha256"]:
                bad.append(f"{e['path']}（内容不一致）")
        if json.loads(z.read("update.json").decode("utf-8")) != manifest:
            bad.append("update.json（读回来跟写进去的不一样）")
    if bad:
        raise SystemExit("更新包校验失败：\n  - " + "\n  - ".join(bad))
    return zip_path, len(changed), raw


def default_runtime_dir() -> Path:
    """自动认出运行时目录（找法见 _paths.find_runtime_root）。

    判据是「那儿有 exe 和 _internal」——比只看目录名可靠。
    """
    guess = find_runtime_root(required=False)
    if guess is not None and (guess / EXE_NAME).is_file() \
            and (guess / "_internal").is_dir():
        return guess
    # 退路：如果项目根本身就是运行时目录（源码和产物混放）
    if (ROOT / EXE_NAME).is_file() and (ROOT / "_internal").is_dir():
        return ROOT
    raise SystemExit(
        "找不到运行时目录（要有 exe + _internal/）。\n"
        "  已自动找过同级目录（含 MDT_RUNTIME_DIR 指定的那个）：\n"
        f"    {guess if guess is not None else '（没有任何目录像运行时）'}\n"
        f"    {ROOT}\n"
        "  用 --runtime-dir 明确指定。"
    )


# --------------------------------------------------------------------------
# 使用说明.txt —— 面向使用者，不是给开发者看的（那是 README.md）
# --------------------------------------------------------------------------

USAGE = """Mindustry 启动器 —— 使用说明
============================================================

【这是什么】

一个 Mindustry 多版本启动器，可以同时管理多个游戏版本、按存档分类隔离数据、
自动备份存档。不用安装 Python，也不用单独装 Java —— 都已经打包在这里面了。


【怎么用】

1. 把整个文件夹解压到一个你习惯的位置，比如 D:\\Mindustry。
   建议避开 C:\\Program Files、桌面这类可能没有写权限的位置。

2. 双击 "Mindustry启动器.exe"。

3. 第一次打开时本地还没有游戏版本，点 "检查更新" 下载一个。
   下载走 GitHub，国内网络如果太慢，去 "设置" 里把 "GitHub 镜像" 填上
   一个镜像站（下拉框里给了几个常用的，也可以自己敲地址）；
   留空就是直连 GitHub。

4. 在列表里选中一个版本，点 "启动游戏"。


【数据都放在哪】

全部在 exe 旁边，整个文件夹拷走就能一起带走：

    versions\\       下载的游戏版本（自动去重，相同文件只存一份）
    Backups\\        存档备份
    logs\\           游戏输出日志（每次启动一份；可在设置里关掉）
    config.json     你的设置
    launcher.log     启动器自己的运行日志 —— 出问题时先看这个


【主要功能】

· 存档分类
  不同分类使用完全独立的游戏数据目录。切换分类再启动，游戏读到的
  就是另一套存档，互不干扰。每个分类可以单独设置数据目录、
  最小备份时间、最大备份数量。

· 自动备份
  按设定条件在游戏退出后自动备份存档。默认是「一局玩满 20 分钟才备份，
  最多保留 20 份，超出后删最旧的」。

· 版本管理
  导入本地 jar 包、重命名、删除，都在 "管理版本" 里。

· 预热
  窗口一打开，它就会在后台提前把当前选中版本的游戏文件拼好。
  等你点 "启动游戏" 时直接复用，能省掉几秒等待。

· 启动器自己会更新
  有新版本时，它会在后台悄悄下好，等你下次关掉启动器自动换上 ——
  不用再手动下载解压。只下**这一版真正变了的那几个文件**（通常就是
  主程序本体，一两 MB），所以很快。你的游戏版本、存档备份、设置
  一个字节都不会动，自己换过的 jre 也不会被覆盖。
  想改成「只提示、由我自己决定」，或者彻底关掉，去 "设置" 里改
  "启动器更新"（默认是自动）。


【设置里能改什么】

· 存档分类：分类名称、数据目录、最小备份时间、最大备份数量
· 启动游戏时隐藏窗口；游戏退出后自动关闭启动器（自动备份做完才关）
· 启动时自动检查更新
· 启动器更新 —— 自动帮你把启动器本身升级到新版（默认开；也可以只提示，
  或彻底关闭）
· Java 路径(JRE/JDK) —— JRE 和 JDK 都行，填根目录即可；旁边有 "浏览"
  和 "检测"，"检测" 会真跑一次 java -version 告诉你这份能不能用
· GitHub 镜像 —— 下载加速用，留空＝直连 GitHub
· 额外 JVM 参数 / 额外游戏参数 —— 想调内存、加启动参数写这里
· 把游戏输出保存到 logs\\ 目录、日志保留份数
· 删除文件时直接彻底删除（默认关闭：默认走回收站，删错了能还原）

⚠️ 改完要点右下角的 "保存并返回" 才会生效。


【注意事项】

· 杀毒软件可能误报（PyInstaller 打包的 exe 挺常见），信任一下就行。

· 删除 "存档分类" 会连带处理该分类的数据目录和备份。程序会先把
  要处理的内容列出来让你确认，不会闷声删掉；默认是删进回收站，
  反悔了能还原。想一步删干净，就去设置里打开 "删除文件时直接彻底
  删除" —— 那之后就真的找不回来了。

· 内置的 Java 是 Temurin 25，够用。想换别的版本，把新的 jre 文件夹
  放到 exe 旁边覆盖掉即可，不用重新打包程序；装了 JDK 的话，也可以在
  设置里直接填 JDK 的目录。填错不用怕 —— 启动器会退回内置那份，
  连内置的都没有时，还会去 JAVA_HOME / PATH 里找现成的 Java。

· 启动器升级新版时，只替换程序文件（exe 与 _internal 目录），并且是
  先换 _internal、最后才换 exe。期间不动游戏版本、存档备份、设置文件，
  也不碰 jre 目录。万一替换失败，它会自动退回原来的版本并记一笔到
  launcher.log。

· 数据目录如果指向游戏原生的 Mindustry 目录，删除操作会额外再警告一次。

· 若 "检查更新" 和启动器自身更新都一直失败，多半是连不上 GitHub。
  可以设一个环境变量 MDT_SELFUPDATE_API，把它指向你能用起来的代理地址。

· 出问题时把 launcher.log 一起发出来，启动器每一步做了什么它都记着。


【说明】

本程序按 GNU GPL-3.0 发布，协议全文见随附的 LICENSE 文件。
按"现状"提供，不附带任何担保。

Mindustry 游戏本体的版权归其作者所有，请遵守游戏自身的许可协议。
本程序不修改、也不重新分发游戏本体 —— 它只是从官方发布页下载
可执行文件并启动。
"""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def _iter_dir(base: Path):
    """遍历目录，产出相对 base 的路径（跳过 __pycache__ 与隐藏临时文件）。"""
    for p in sorted(base.rglob("*")):
        if p.is_dir():
            continue
        name = p.name
        if name.endswith((".pyc", ".tmp")) or "__pycache__" in p.parts:
            continue
        if name.startswith(".DS_Store"):
            continue
        yield p


def collect(base: Path) -> list[tuple[Path, str]]:
    """返回 [(磁盘绝对路径, zip 内相对 TOP 的路径)]。base 是运行时目录。"""
    items: list[tuple[Path, str]] = []

    for rel in INCLUDE_FILES:
        src = base / rel
        if not src.is_file():
            raise SystemExit(f"发布包缺少必要文件：{src}")
        items.append((src, rel))

    for rel in INCLUDE_DIRS:
        src = base / rel
        if not src.is_dir():
            raise SystemExit(f"发布包缺少必要目录：{src}")
        for f in _iter_dir(src):
            items.append((f, f"{rel}/{f.relative_to(src).as_posix()}"))

    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="生成可解压即用的发布包 zip")
    ap.add_argument("--outdir", default=None, help="zip 输出到哪个目录（默认项目根）")
    ap.add_argument("--runtime-dir", default=None,
                    help="从哪儿取 exe/_internal/jre（默认自动找运行时目录）")
    ap.add_argument("--name", default=None, help="zip 文件名（默认自动带日期）")
    ap.add_argument("--level", type=int, default=6, help="压缩级别 1-9（默认 6）")
    ap.add_argument("--baseline", default=None, type=Path,
                    help="算更新包差异用的上一版 manifest.json"
                         "（默认自动在 _history/releases/ 里找）")
    ap.add_argument("--baseline-from-zip", default=None, type=Path,
                    help="上一版**发布包 zip** —— 它没带 manifest.json 时用它反推基线")
    ap.add_argument("--baseline-version", default=None,
                    help="配合 --baseline-from-zip：上一版的版本号"
                         "（发布包文件名里没有版本号时用得上）")
    ap.add_argument("--no-update", action="store_true",
                    help="只出完整包，不生成精简更新包")
    args = ap.parse_args()

    base = Path(args.runtime_dir).resolve() if args.runtime_dir \
        else default_runtime_dir()
    out_dir = Path(args.outdir).resolve() if args.outdir else ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = args.name or f"{TOP}_发布包_{date.today():%Y-%m-%d}.zip"
    zip_path = out_dir / zip_name

    print(f"项目根  ：{ROOT}")
    print(f"运行时目录：{base}")
    items = collect(base)

    # ---- 自更新清单 ----
    # 完整包里带一份 manifest.json：下一版生成更新包时，它就是「基线」的来源。
    # ⚠️ 清单只覆盖**程序文件**（exe + _internal/），不含 jre / LICENSE /
    #    使用说明 —— 更新器也只替换那两样，两边的范围必须一致。
    version = read_version()
    manifest = build_manifest(program_items(base), version)
    print(f"版本    ：v{version}（程序文件 {len(manifest['files'])} 个）")

    # 许可协议从**项目根**取（运行时目录里没有这个文件）。GPL-3.0 要求
    # 分发时随附协议全文，所以发布包必须带上它 —— 加进 items，下面的
    # 逐文件校验就一并覆盖了。
    lic = ROOT / "LICENSE"
    if lic.is_file():
        items.append((lic, "LICENSE"))
    else:
        print("[警告] 项目根没有 LICENSE —— 这个包将不带许可协议")

    total_raw = sum(p.stat().st_size for p, _ in items)
    print(f"待打包：{len(items)} 个文件，{_human(total_raw)}")

    # 打之前先确认 exe 里装的真是当前源码 —— 发布包最怕发出去的是旧代码。
    exe = base / EXE_NAME
    print(f"exe    ：{exe.name}  {_human(exe.stat().st_size)}  "
          f"sha256={_sha256(exe)[:16]}…")

    if zip_path.exists():
        zip_path.unlink()

    t0 = time.time()
    with zipfile.ZipFile(
        zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=args.level
    ) as z:
        for src, rel in items:
            z.write(src, f"{TOP}/{rel}")
        # 使用说明是现生成的，直接写进 zip
        z.writestr(f"{TOP}/使用说明.txt", USAGE.encode("utf-8-sig"))
        # 程序文件清单（使用者不用管它，但下一版发版要靠它算差异）
        z.writestr(
            f"{TOP}/manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    took = time.time() - t0
    size = zip_path.stat().st_size
    ratio = (1 - size / total_raw) * 100 if total_raw else 0
    print()
    print(f"写出：{zip_path}")
    print(f"      {_human(size)}（原始 {_human(total_raw)}，压缩掉 {ratio:.0f}%），"
          f"耗时 {took:.1f}s")

    # ---- 校验：zip 里每个条目都必须与磁盘逐字节一致 ----
    # 交付包最常见的翻车方式是"改了东西忘了重新生成"，这一步当场抓住。
    print()
    print("校验（zip 内 vs 磁盘）：")
    bad: list[str] = []
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        for src, rel in items:
            entry = f"{TOP}/{rel}"
            if entry not in z.namelist():
                bad.append(f"{rel}（zip 里没有）")
                continue
            if hashlib.sha256(z.read(entry)).hexdigest() != _sha256(src):
                bad.append(f"{rel}（内容不一致）")

    print(f"  条目数：{len(names)}（含 使用说明.txt）")
    if bad:
        print("  [失败] 以下条目有问题：")
        for b in bad:
            print(f"        - {b}")
        return 1
    print(f"  逐条 sha256：{len(items)}/{len(items)} 全部一致")

    # 留档：下一版算更新包差异时要用它当基线（这个目录不进 git，纯本地档案）
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    archive = MANIFEST_DIR / f"manifest-{version}.json"
    archive.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  清单留档：{archive}")

    if args.no_update:
        print()
        print("发布包已生成（--no-update：没生成更新包）。"
              "使用者解压后双击 exe 即可运行。")
        return 0

    # ---- 精简更新包：只装相对上一版真正变动的文件 ----
    # 显式给了发布包就优先用它反推（比自动翻留档更可信：那是真发出去的东西）；
    # 否则再自动翻本地留档，最后才退化成全量包。
    baseline: dict | None = None
    source = ""
    if args.baseline_from_zip:
        zpath = Path(args.baseline_from_zip)
        if not zpath.is_file():
            raise SystemExit(f"找不到发布包：{zpath}")
        baseline = manifest_from_release_zip(zpath)
        source = f"由发布包反推：{zpath.name}"
        bver = str(args.baseline_version or baseline.get("version") or "")
        if not _ver_tuple(bver):
            # 认不出版本号就不留档 —— 留一份 manifest-unknown.json 只会碍事
            print(f"  [提醒] 认不出上一版的版本号（{zpath.name}），这次不留档；"
                  f"可用 --baseline-version 指定")
        else:
            baseline["version"] = bver
            MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
            dest = MANIFEST_DIR / f"manifest-{bver}.json"
            if dest.exists():
                print(f"  [提醒] 覆盖掉旧的留档 {dest.name}"
                      f"（它不一定对应这一版的发布包）")
            dest.write_text(
                json.dumps(baseline, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  基线已补进留档：{dest}")
    else:
        baseline, source = load_baseline(version, args.baseline)
    upd_path, upd_count, upd_raw = write_update_package(
        base, out_dir, version, baseline, args.level
    )
    base_ver = (baseline or {}).get("version") or "（无）"
    print()
    print(f"更新包：{upd_path.name}")
    print(f"      基线 v{base_ver} ← {source}")
    print(f"      含 {upd_count} 个文件（原始 {_human(upd_raw)}），"
          f"打包后 {_human(upd_path.stat().st_size)}")
    if baseline is None:
        print("      [提醒] 没找到上一版清单，这次按全量打包（能更新，只是大）")
    print("      逐条 sha256 校验通过")
    print()
    print("两个包都已生成：完整包给新用户，更新包给启动器自更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
