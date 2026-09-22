# -*- coding: utf-8 -*-
"""把文件/目录移入 Windows 回收站（可还原）。**不硬删**。

整理目录、清理重复文件时用这个，别用 `rm -rf` / `shutil.rmtree`：

    python _tools/recycle.py --list <路径> [<路径> ...]   # 只列出体积，不动手
    python _tools/recycle.py <路径> [<路径> ...]          # 真正移入回收站
    python _tools/recycle.py --from-file <清单.txt>       # 每行一个路径
    python _tools/recycle.py --list --from-file 清单.txt  # 先预演再执行

要点：
- 用 `SHFileOperationW` + `FOF_ALLOWUNDO`，走的是和资源管理器一样的回收站路径。
- `FOF_WANTNUKEWARNING`：万一回收站装不下、只能永久删除时会有提示，
  不会被静默粉碎。
- 跑完复核路径确实消失，并逐个报告成功/失败。

⚠️ 回收站有容量上限（`HKCU\\...\\BitBucket\\Volume\\<卷GUID>\\MaxCapacity`，单位 MB）。
批量删大目录前先看一眼上限和当前占用，否则超过上限的部分会被永久删除。
`mountvol` 可以查盘符对应的卷 GUID。
"""
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path


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
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # 关键：进回收站
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000


def recycle_one(path: Path) -> tuple[bool, str]:
    """单个路径进回收站。返回 (成功, 说明)。"""
    path = Path(path)
    if not path.exists():
        return False, "不存在"
    target = str(path.resolve())
    # pFrom 需要双 NUL 结尾
    buf = ctypes.create_unicode_buffer(len(target) + 2)
    buf.value = target

    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI
                 | FOF_SILENT | FOF_WANTNUKEWARNING)
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None

    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0:
        return False, f"SHFileOperationW 返回 {code}"
    if op.fAnyOperationsAborted:
        return False, "操作被中止（文件太大放不进回收站？）"
    # 复核：路径应该已经消失
    return (not path.exists()), "已移入回收站"


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{n:.0f} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def size_of(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def main() -> int:
    argv = sys.argv[1:]
    dry = "--list" in argv
    argv = [a for a in argv if a != "--list"]

    if "--from-file" in argv:
        i = argv.index("--from-file")
        src = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
        argv += [ln.strip() for ln in src.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]

    targets = [Path(a) for a in argv]
    if not targets:
        print(__doc__)
        return 2

    grand = 0
    rows = []
    for t in targets:
        if not t.exists():
            rows.append((t, 0, "不存在"))
            continue
        s = size_of(t)
        grand += s
        rows.append((t, s, "目录" if t.is_dir() else "文件"))

    print(f"{'路径':<72}{'大小':>11}  类型")
    print("-" * 96)
    for t, s, kind in rows:
        print(f"{str(t):<72}{human(s):>11}  {kind}")
    print("-" * 96)
    print(f"合计 {len(rows)} 项，{human(grand)}")

    if dry:
        print("\n[--list 模式] 没有做任何改动。")
        return 0

    print("\n开始移入回收站……")
    ok = bad = 0
    for t, s, kind in rows:
        if kind == "不存在":
            continue
        good, msg = recycle_one(t)
        print(f"  [{'OK ' if good else '失败'}] {t.name}  {msg}")
        ok += good
        bad += (not good)
    print(f"\n完成：成功 {ok} 项，失败 {bad} 项。全部可从回收站还原。")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
