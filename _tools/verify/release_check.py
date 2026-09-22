#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布包验证：解到全新空目录 -> 用不相关的 cwd 启动 exe -> 检查自洽性。

回答的问题是「这个 zip 交给别人，真的能直接双击用吗」。光看 zip 内容、
或者只看 exe 体积对不对，都证明不了这件事。必须真解压、真启动。

会验证：
  1. 解压后关键文件/依赖齐全（tcl/tk DLL、_tcl_data、jre/bin/java.exe …）
  2. 预置最新版本清单（见 sandbox_seed.py）—— 不预置的话启动器的
     auto_update 会真去下载 200+ MB，冒烟变成长跑
  3. 用**完全无关的 cwd** 启动，程序不会把文件写到 cwd 里去
     （这是「数据根改造是否正确」的唯一证明方式）
  4. 数据落在 **exe 旁边**：config.json / launcher.log / versions / backups
  5. 进程能活下来（不是启动即崩 —— 无控制台时崩溃很难发现）
  6. 顺带打印 launcher.log 尾部，方便看初始化有没有报错

⚠️ 会真的启动 GUI 窗口（约 10 秒后自动关掉）。别在忙的时候跑。

用完自清理；要保留现场排查就加 --keep。

用法：
    python _tools/verify/release_check.py
    python _tools/verify/release_check.py --zip <path> --seconds 15 --keep
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from sandbox_seed import seed_latest_versions


def _find_project_root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / "launcher" / "__init__.py").is_file():
            return p
    raise RuntimeError(f"找不到项目根（从 {start} 往上没看到 launcher/__init__.py）")


ROOT = _find_project_root(Path(__file__).resolve().parent)

EXE_NAME = "Mindustry启动器.exe"

# 包里必须有的顶层项
# 注：Mindustry.json 已并进 config.json 的 jvm 段，不再是必需文件
REQUIRED_TOP = [EXE_NAME, "_internal", "jre", "使用说明.txt"]

# 少一个都可能在运行期静默崩溃的依赖
REQUIRED_INNER = [
    "_internal/_tkinter.pyd",
    "_internal/tcl86t.dll",
    "_internal/tk86t.dll",
    "_internal/_ctypes.pyd",
    "_internal/_ssl.pyd",
    "_internal/_tcl_data",
    "_internal/_tk_data",
    "jre/bin/java.exe",
    "jre/lib/modules",
]

# 程序应该在 exe 旁边自己建出来的东西。
# ⚠️ backups 是**小写**（代码里是 base_dir / "backups"），别写成 Backups。
EXPECTED_RUNTIME = ["config.json", "launcher.log", "versions", "backups"]


def _latest_zip() -> Path | None:
    cands = sorted(ROOT.glob("*发布包*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="验证发布包能解压即用")
    ap.add_argument("--zip", default=None, help="发布包 zip（默认取最新的一个）")
    ap.add_argument("--seconds", type=float, default=10.0, help="启动后观察多少秒（默认 10）")
    ap.add_argument("--keep", action="store_true", help="保留解压目录，方便排查")
    args = ap.parse_args()

    zip_path = Path(args.zip).resolve() if args.zip else _latest_zip()
    if not zip_path or not zip_path.is_file():
        print("找不到发布包 zip。先跑 python _tools/make_release_zip.py")
        return 2

    work = Path(tempfile.mkdtemp(prefix="mdt_release_check_"))
    proc: subprocess.Popen | None = None
    ok = False
    try:
        print("=" * 64)
        print(f"发布包：{zip_path.name}  ({zip_path.stat().st_size / 1048576:.1f} MB)")
        print("=" * 64)

        print("\n[1] 解压到全新空目录")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(work)
        dirs = [d for d in work.iterdir() if d.is_dir()]
        if not dirs:
            print("  [失败] 解压后没有目录")
            return 1
        pkg = dirs[0]
        n_files = sum(1 for p in pkg.rglob("*") if p.is_file())
        print(f"  包根：{pkg.name}")
        print(f"  文件数：{n_files}")
        for rel in REQUIRED_TOP:
            print(f"  [{'OK ' if (pkg / rel).exists() else '缺失'}] {rel}")

        missing_top = [r for r in REQUIRED_TOP if not (pkg / r).exists()]
        if missing_top:
            print(f"  [失败] 缺少顶层项：{missing_top}")
            return 1

        print("\n[2] 关键依赖检查")
        missing_inner = []
        for rel in REQUIRED_INNER:
            hit = (pkg / rel).exists()
            if not hit:
                missing_inner.append(rel)
            print(f"  [{'OK ' if hit else '缺失'}] {rel}")
        if missing_inner:
            print(f"  [失败] 缺少关键依赖：{missing_inner}")
            return 1

        exe = pkg / EXE_NAME

        print("\n[3] 预置最新版本清单（避免 auto_update 真去下载 200+ MB）")
        # 解压出来的包里 versions/ 是空的，启动器默认开着 auto_update，
        # 不预置就会在后台拉 Mindustry 160.4 + MindustryX 最新版。
        seed_latest_versions(pkg)

        print(f"\n[4] 用「完全无关的 cwd」启动（观察 {args.seconds:.0f} 秒）")
        probe = work / "unrelated_cwd"
        probe.mkdir()
        before = set(os.listdir(probe))
        proc = subprocess.Popen([str(exe)], cwd=str(probe))
        print(f"  cwd = {probe}")
        time.sleep(args.seconds)
        alive = proc.poll() is None
        print(f"  进程存活：{'是' if alive else f'否（退出码 {proc.returncode}）'}")
        leaked = set(os.listdir(probe)) - before
        print(f"  cwd 被写入：{sorted(leaked) if leaked else '无 ✔'}")

        print("\n[5] 数据根是否落在 exe 旁边")
        for name in EXPECTED_RUNTIME:
            p = pkg / name
            info = ""
            if p.is_file():
                info = f"({p.stat().st_size} B)"
            elif p.is_dir():
                info = f"({sum(1 for _ in p.rglob('*'))} 项)"
            print(f"  [{'生成' if p.exists() else '未出现'}] {name} {info}")

        log = pkg / "launcher.log"
        if log.is_file():
            print("\n[6] launcher.log 尾部")
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            for ln in lines[-20:]:
                print("   ", ln)

        ok = alive and not leaked
        print("\n" + "=" * 64)
        print("结论：" + ("通过 ✔ 空环境下能独立运行，数据写在 exe 旁边"
                        if ok else "有问题，见上面输出"))
        print("=" * 64)
    finally:
        if proc is not None:
            _kill(proc)
            print("\n已终止测试进程")
        if args.keep:
            print(f"保留现场：{work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
            print("已清理解压目录")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
