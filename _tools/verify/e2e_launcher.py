# -*- coding: utf-8 -*-
"""端到端：真的走 _monitor_game 把游戏拉起来，确认新的分段计时落到日志里，
并且游戏能正常起到窗口出现。

跑完自动把游戏关掉、启动器退出，不需要人工点。

★ 脚本会自己把工作目录切到运行时目录。这一步不能省：
  源码运行时数据根是**进程 cwd**（见 launcher/utils._app_dir），版本列表和
  jre/ 都按它去找。从仓库根直接跑会去仓库自己的 versions 找版本（空的），
  并且因为没有 jre/ 直接报「JRE 未找到」退出。
"""
import ctypes
import os
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # _tools/ 共用路径模块
from _paths import DATA, ROOT  # noqa: E402

sys.path.insert(0, str(ROOT))
os.chdir(DATA)

from launcher.gui import MindustryLauncher  # noqa: E402
from launcher.utils import LOG_FILE  # noqa: E402

user32 = ctypes.windll.user32


def game_windows():
    found = []
    PROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
    )

    def cb(hwnd, lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            t = buf.value
            if "Mindustry" in t and "启动器" not in t:
                found.append(t)
        return True

    user32.EnumWindows(PROC(cb), 0)
    return found


def main():
    app = MindustryLauncher()
    app.root.withdraw()          # 不打扰桌面
    versions = app.version_manager.get_versions()
    if not versions:
        print("没有可用版本")
        app.on_closing()
        return 1
    v = versions[0]
    print(f"待测版本: {v['name']}")

    state = {"window_at": None, "done": False, "preheat_ready": None}
    t_start = time.perf_counter()

    def launch():
        app._launching.set()     # 复刻 launch_game 的动作
        app._monitor_game(v)

    def watchdog():
        deadline = time.perf_counter() + 40
        while time.perf_counter() < deadline:
            if state["window_at"] is None and game_windows():
                state["window_at"] = time.perf_counter() - t_start
                # 窗口出来了，再给它两秒，然后收工
                time.sleep(2.0)
                break
            time.sleep(0.05)
        proc = app.current_process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except Exception:
                proc.kill()
        state["done"] = True
        app.root.after(500, app.on_closing)

    def wait_preheat():
        """先等预热就绪，好验证「点启动复用预热」这条路径。
        超时就照常启动（会退回自己拼），只是验证会标失败。"""
        deadline = time.perf_counter() + 30
        while time.perf_counter() < deadline:
            if app._take_preheated_jar(app._preheat_key_of(v)) is not None:
                state["preheat_ready"] = time.perf_counter() - t_start
                break
            time.sleep(0.1)
        threading.Thread(target=launch, daemon=True).start()

    app.root.after(300, lambda: threading.Thread(
        target=wait_preheat, daemon=True).start())
    app.root.after(600, lambda: threading.Thread(
        target=watchdog, daemon=True).start())
    app.root.mainloop()

    print()
    print("=" * 60)
    if state["preheat_ready"] is not None:
        print(f"  预热就绪: {state['preheat_ready']:.2f} s（启动前就已拼好）")
    else:
        print("  预热未在 30 s 内就绪  <<< 预热没生效")
    if state["window_at"] is not None:
        print(f"  游戏窗口出现: {state['window_at']:.2f} s（含等预热的时间）")
    else:
        print("  游戏窗口未出现")
    print("  完成:", state["done"])

    # 从日志里把这次的埋点捞出来。
    # 读的是**本次进程真正写的那份**：源码运行 → launcher.dev.log，
    # 绝不会去碰「实际使用」的 launcher.log（见 launcher/utils.py）。
    print(f"  日志文件: {LOG_FILE}")
    log = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    tail = log.split("启动器初始化完成")[-1]
    preheat = re.findall(r"\[预热\].*", tail)
    jar = re.findall(r"\[JAR\].*", tail)
    phase = re.findall(r"\[启动耗时\].*", tail)
    print()
    for label, hits in (("[预热]", preheat), ("[JAR]", jar),
                        ("[启动耗时]", phase)):
        for h in hits:
            print(f"  {h}")
        if not hits:
            print(f"  {label} 未出现在日志里  <<< 埋点没生效")

    reused = any("复用预热JAR" in p for p in phase)
    print()
    print(f"  点启动时复用了预热结果: {'是' if reused else '否'}")

    # ---- 游戏输出日志（2026-09-18 新增）----
    # 真跑一局来验：以前 stdout/stderr 是直接丢进 DEVNULL 的，这条链只有在
    # 真起过游戏之后才证明得了。
    print()
    for _ in range(60):
        if not app.game_log.active:
            break
        time.sleep(0.1)
    gl = app.game_log
    lines = gl.tail(500)
    markers = ("Mindustry", "Total time to load", "GL]", "RAM]", "JAVA]")
    hit = [ln for ln in lines if any(m in ln for m in markers)]
    log_ok = gl.path is not None and gl.path.is_file() and bool(lines)
    print(f"  游戏输出落盘: {gl.path}")
    print(f"  收到 {len(lines)} 行，其中游戏自己的日志 {len(hit)} 行")
    for h in hit[:5]:
        print(f"    {h[:100]}")
    if not log_ok:
        print("  <<< 输出没有接住（还是 DEVNULL？）")

    ok = (
        bool(preheat)
        and bool(phase)
        and reused
        and state["window_at"] is not None
        and log_ok
        and bool(hit)
    )
    print()
    print("端到端:", "通过 ✓" if ok else "有问题 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
