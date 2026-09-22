# -*- coding: utf-8 -*-
"""生成「可独立构建」的源码包 zip。

用法（在项目根执行）：
    python _tools/make_source_zip.py                    # 自动命名（带日期）
    python _tools/make_source_zip.py -o 我的名字.zip     # 指定输出名

只收**构建一个 exe 真正需要的**东西：源码 + spec + 构建资源 + 开发工具。
不收用户数据、构建产物、jre、运行时文件 —— 那些要么可重建、要么体积巨大、
要么属于个人数据。

收完后会**逐个文件比对哈希**，确认 zip 内容和源文件一致（防止压缩/写入环节出错）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import sys
import zipfile
from pathlib import Path

# 路径一律从 _paths 拿（本文件就在 _tools/ 里，import 得到）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT   # noqa: E402

TOP = "Mindustry启动器_源码包"
DEFAULT_NAME = "Mindustry启动器_源码包_{date}.zip"

# 要收的顶层条目（文件或目录）
ITEMS = [
    "README.md",              # 说明怎么构建
    "LICENSE",                # 许可协议（GPL-3.0 全文）
    "Launcher_Test_123.py",   # 入口
    "Launcher.spec",          # 打包配置
    "mindustry.ico",          # 构建资源（spec 的 datas + icon 引用）
    "launcher",               # 全部源码
    "_tools",                 # 开发工具（build / verify）
]

# 排除：缓存、备份、编辑器残留
EXCLUDE_DIRS = {"__pycache__", ".workbuddy"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".bak", "~")


def _iter_files(src: Path):
    if src.is_file():
        yield src
        return
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in sorted(filenames):
            if fn.endswith(EXCLUDE_SUFFIXES):
                continue
            yield Path(dirpath) / fn


def main() -> int:
    ap = argparse.ArgumentParser(description="生成可独立构建的源码包 zip")
    ap.add_argument("-o", "--output", help="输出文件名（默认带日期）")
    args = ap.parse_args()

    name = args.output or DEFAULT_NAME.format(
        date=_dt.date.today().isoformat()
    )
    out_zip = ROOT / name

    collected: list[tuple[Path, str]] = []
    for rel in ITEMS:
        src = ROOT / rel
        if not src.exists():
            print(f"  !! 缺失，无法打包: {rel}")
            return 1
        for f in _iter_files(src):
            collected.append((f, f"{TOP}/{f.relative_to(ROOT).as_posix()}"))

    print(f"收集 {len(collected)} 个文件 -> {name}")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f, arc in collected:
            zf.write(f, arc)

    # 回读：完整性 + 逐文件内容一致
    bad: list[str] = []
    with zipfile.ZipFile(out_zip) as zf:
        broken = zf.testzip()
        if broken:
            print(f"  !! 压缩包损坏: {broken}")
            return 1
        names = zf.namelist()
        for n in names:
            src = ROOT / n[len(TOP) + 1:]
            if not src.is_file():
                bad.append(f"{n} 源文件不见了")
                continue
            if hashlib.sha256(zf.read(n)).digest() != \
               hashlib.sha256(src.read_bytes()).digest():
                bad.append(f"{n} 内容不一致")

    if bad:
        print("校验失败：")
        for b in bad:
            print("  !!", b)
        return 1

    print(f"校验通过：{len(names)} 个文件逐字节一致，"
          f"{out_zip.stat().st_size / 1024:.0f} KB")
    print(f"\n产物: {out_zip}")
    print("解压后在其根目录跑 `python _tools/build.py` 即可构建（需带 tkinter 的 Python）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
