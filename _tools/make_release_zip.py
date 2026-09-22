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

用法：
    python _tools/make_release_zip.py
    python _tools/make_release_zip.py --runtime-dir D:\\somewhere
    python _tools/make_release_zip.py --outdir D:\\somewhere
"""

from __future__ import annotations

import argparse
import hashlib
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


【设置里能改什么】

· 存档分类：分类名称、数据目录、最小备份时间、最大备份数量
· 启动游戏时隐藏窗口；游戏退出后自动关闭启动器（自动备份做完才关）
· 启动时自动检查更新
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

· 数据目录如果指向游戏原生的 Mindustry 目录，删除操作会额外再警告一次。

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
    print()
    print("发布包已生成。使用者解压后双击 exe 即可运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
