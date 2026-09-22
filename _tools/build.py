# -*- coding: utf-8 -*-
"""一键构建 Mindustry 启动器 exe（onedir 模式）。

用法（在项目根执行）：
    python _tools/build.py                    # 只构建，产物留在 dist/
    python _tools/build.py --deploy           # 构建完平铺到一级运行目录
    python _tools/build.py --deploy-to <目录>  # 部署到指定目录
    python _tools/build.py --deploy --prune   # 顺手删掉目标里多出来的陈旧文件
    python _tools/build.py --deploy-only      # **不重打**，只把 dist/ 里现有的产物部署过去

★ 本仓库只放「开发」相关的东西。运行时目录（exe + _internal/ + jre/ + 用户数据）
  在同级的 `runtime/` 下，日常就双击那里的 exe 用。`--deploy` 不带参数时会
  自动认出它（找法见 `_paths.find_runtime_root`：环境变量 `MDT_RUNTIME_DIR`
  → 同级 `runtime/` → 同级里任意一个像运行时的目录），把新 exe 同步过去
  —— 改完代码一条命令到位。

★ 为什么不能随便找个 Python 来跑：
    `Launcher.spec` 会从**解释器的** `sys.base_prefix` 推导
    tcl86t.dll / tk86t.dll / zlib.dll / ffi.dll 这些运行库的位置 ——
    Anaconda 把它们放在 `Library/bin/`，而 PyInstaller 默认只扫
    `DLLs/` 和 cwd，扫不到。用一个不带 tkinter 的解释器构建出来的 exe，
    一启动就**静默崩溃**（windowed 模式连报错都看不见）。
    所以本脚本会先挑一个「`import tkinter` 能成功」的解释器再动手。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 路径一律从 _paths 拿（本文件就在 _tools/ 里，import 得到）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, find_runtime_root   # noqa: E402

SPEC = ROOT / "Launcher.spec"
NAME = "Mindustry启动器"


def _has_tkinter(python: str) -> bool:
    """这个解释器能不能 import tkinter（还要能真正创建 Tk 才作数）。"""
    try:
        r = subprocess.run(
            [python, "-c", "import tkinter; tkinter.Tk()"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _candidates() -> list[str]:
    """按优先级列出可能的构建解释器。

    ★ 为什么列出 conda 的默认安装位置：Anaconda / Miniconda 的 Python 自带
      tkinter，是 Windows 上最省事的构建解释器。下面几个都是安装器的**默认**
      路径（`%ProgramData%` / `%USERPROFILE%` 由系统提供），不是某台机器的
      特例。想让自己的解释器排最前，设环境变量 `MDT_PYTHON` 即可。
    """
    out: list[str] = []
    env = os.environ.get("MDT_PYTHON")
    if env:
        out.append(env)
    out.append(sys.executable)                      # 当前解释器优先
    program_data = os.environ.get("ProgramData") or r"C:\ProgramData"
    for guess in (
        os.path.join(program_data, "anaconda3", "python.exe"),
        os.path.join(program_data, "miniconda3", "python.exe"),
        os.path.expanduser(r"~\anaconda3\python.exe"),
        os.path.expanduser(r"~\miniconda3\python.exe"),
    ):
        if guess not in out:
            out.append(guess)
    return out


def pick_python(verbose: bool = True) -> str:
    tried = []
    for cand in _candidates():
        if not os.path.isfile(cand):
            continue
        if _has_tkinter(cand):
            if verbose:
                ver = subprocess.run(
                    [cand, "-c", "import sys; print(sys.version.split()[0])"],
                    capture_output=True, text=True,
                ).stdout.strip()
                print(f"[解释器] {cand}  (Python {ver})")
            return cand
        tried.append(cand)
    raise SystemExit(
        "找不到「带 tkinter 的 Python」。\n"
        "  已试过:\n" + "\n".join(f"    - {t}" for t in tried) +
        "\n  装上 tkinter 的 Python 后重试；或用 MDT_PYTHON 环境变量指定：\n"
        r'    set MDT_PYTHON=C:\path\to\python.exe'
    )


def check_pyinstaller(python: str) -> None:
    r = subprocess.run(
        [python, "-c", "import PyInstaller, sys; sys.stdout.write(PyInstaller.__version__)"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise SystemExit(
            "这个解释器里没有 PyInstaller。装一下：\n"
            f'    "{python}" -m pip install pyinstaller'
        )
    print(f"[PyInstaller] {r.stdout.strip()}")


def build(python: str, outdir: Path) -> None:
    if not SPEC.is_file():
        raise SystemExit(f"找不到 {SPEC}")
    print(f"[构建] Launcher.spec (onedir) -> {outdir}")
    # 输出目录交给 PyInstaller 自己建。若该目录已存在且文件很多，COLLECT
    # 会先清空它 —— 那一步可能被安全策略拦下（批量删除需确认），构建就
    # 停在中途了。所以要么先手动删掉旧的，要么用 --outdir 换个新目录。
    cmd = [python, "-m", "PyInstaller", "--clean", "--noconfirm",
           "--distpath", str(outdir), str(SPEC)]
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"构建失败（退出码 {r.returncode}）")
    print(f"\n[完成] 产物在 {outdir / NAME}")


def _sync_tree(src: Path, dst: Path, prune: bool = False) -> tuple[int, int, list[str]]:
    """把 src 同步到 dst：只复制新增/变化的文件。

    返回 (复制数, 未变数, 多余文件清单)。

    ★ 为什么不用 shutil.rmtree + copytree：
    `_internal/` 有近千个文件，整目录删掉再重抄会被「批量删除确认」拦下
    （一次删太多文件要人工确认），构建就会停在中途。而且实测大多数情况下
    只有 `base_library.zip` 一个文件变了 —— 全量重抄纯属浪费。
    这里只复制真正不同的，顺便把「目标里多出来的」报出来。
    """
    copied = same = 0
    dst.mkdir(parents=True, exist_ok=True)

    src_files = {}
    for p in src.rglob("*"):
        if p.is_file():
            src_files[p.relative_to(src).as_posix()] = p

    for rel, sp in src_files.items():
        dp = dst / rel
        if dp.is_file() and dp.stat().st_size == sp.stat().st_size:
            # 大小相同再比内容，避免小文件误判
            if hashlib.sha256(dp.read_bytes()).hexdigest() == \
               hashlib.sha256(sp.read_bytes()).hexdigest():
                same += 1
                continue
        dp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, dp)
        copied += 1

    extras = []
    for dp in dst.rglob("*"):
        if dp.is_file() and dp.relative_to(dst).as_posix() not in src_files:
            extras.append(dp.relative_to(dst).as_posix())

    if prune and extras:
        for rel in extras:
            try:
                (dst / rel).unlink()
            except OSError:
                pass
        extras = []

    return copied, same, extras


def deploy(outdir: Path, target: Path | None = None,
           prune: bool = False) -> None:
    """把产物平铺到目标目录（exe + _internal/ 与数据目录同级）。

    默认目标是项目根。用 --deploy-to 可以指到运行时目录（比如 `../runtime`），
    这样「改代码 → 构建 → 直接更新日常用的那份」一条命令走完。
    """
    src = outdir / NAME
    if not src.is_dir():
        raise SystemExit(f"没有产物可部署：{src} 不存在")
    target = Path(target) if target else ROOT
    if not target.is_dir():
        raise SystemExit(f"目标目录不存在：{target}")

    exe = src / f"{NAME}.exe"
    dst_exe = target / f"{NAME}.exe"
    if dst_exe.exists():
        bak = target / f"{NAME}.exe.bak"
        shutil.copy2(dst_exe, bak)
        print(f"[备份] 旧 exe -> {bak}")

    shutil.copy2(exe, dst_exe)
    print(f"[部署] exe -> {dst_exe}")

    internal = src / "_internal"
    if internal.is_dir():
        copied, same, extras = _sync_tree(internal, target / "_internal",
                                          prune=prune)
        print(f"[部署] _internal/  复制 {copied} 个，未变 {same} 个")
        if extras:
            print(f"[注意] 目标里多出 {len(extras)} 个文件（可能是陈旧的）：")
            for rel in extras[:10]:
                print(f"         {rel}")
            if len(extras) > 10:
                print(f"         …… 还有 {len(extras)-10} 个")
            print("       加 --prune 可以顺手删掉它们")

    print(f"[提醒] jre/ 要在 exe 旁边才能运行（本脚本不碰它）")
    if target.resolve() != ROOT.resolve():
        print(f"[提醒] 部署目标不是项目根，而是：{target}")


def main() -> int:
    os.chdir(ROOT)
    argv = sys.argv[1:]
    outdir = ROOT / "dist"
    if "--outdir" in argv:
        outdir = Path(argv[argv.index("--outdir") + 1]).resolve()

    target = None
    if "--deploy-to" in argv:
        target = Path(argv[argv.index("--deploy-to") + 1]).resolve()
    else:
        # 默认找同级的运行时目录（见 _paths.find_runtime_root：先看
        # MDT_RUNTIME_DIR，再看同级那几个约定名）。要 jre/ 在才算数 ——
        # 没有 jre 的目录部署过去也跑不起来。
        guess = find_runtime_root(required=False)
        if guess is not None and (guess / "jre").is_dir():
            target = guess

    print(f"项目根: {ROOT}\n")
    if "--deploy-only" in argv:
        # 复用 dist/ 里已经打好的产物。最典型的场景：构建成功了，但部署那一步
        # 撞上「exe 还开着」（WinError 32：另一个程序正在使用此文件）——
        # 这时没必要再花 40 秒重打一遍，关掉启动器再跑一次本开关即可。
        built = outdir / NAME
        if not built.is_dir():
            print(f"dist 里没有产物：{built}\n先跑一次完整的 python _tools/build.py")
            return 2
        print(f"[跳过构建] 直接部署现有产物：{built}")
        print("（不挑解释器、不查 PyInstaller —— 这两步只跟构建有关）")
    else:
        python = pick_python()
        check_pyinstaller(python)
        print()
        build(python, outdir)
    if (
        "--deploy" in argv
        or "--deploy-to" in argv
        or "--deploy-only" in argv
    ):
        print()
        deploy(outdir, target, prune="--prune" in argv)
    print("\n提示：重打之后建议跑一下 _tools/verify/packed_code_check.py，"
          "确认新代码真的进了 exe（只看「能打开」证明不了）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
