# -*- coding: utf-8 -*-
"""Mindustry 启动器入口。

平时就是启动 GUI；此外还兼一个角色 —— **更新执行体**。

为什么需要它：Windows 不允许覆盖正在运行的 exe，所以「自己更新自己」
必须交给一个**外部进程**。做法是把本 exe 复制到 ``%TEMP%``，用
``--apply-update <plan.json>`` 启动那份副本；主程序随即退出，副本
按计划替换 exe / ``_internal/``（失败会回滚），见 ``launcher/selfupdate.py``。

★ 这个分支要**早于 tkinter 的 import** 处理：执行体不该建任何窗口，
  也没必要加载 Tk 那一堆 DLL（省点开销，也少一类出错可能）。
"""
import json
import sys
from pathlib import Path

APPLY_UPDATE_FLAG = "--apply-update"


def run_as_updater(argv: list[str]) -> int:
    """更新执行体：不建窗口，干完就退（退出码见 selfupdate.apply_update_main）。"""
    try:
        plan_path = Path(argv[argv.index(APPLY_UPDATE_FLAG) + 1])
    except (ValueError, IndexError):
        return 2

    from launcher import selfupdate
    from launcher.utils import setup_logging

    # 执行体是在 %TEMP% 里跑的，它算出来的数据根也在那儿 —— 但排查问题
    # 时我们要看的是**目标程序目录**那份日志，所以这里把它重定向过去。
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        app_dir = Path(plan.get("app_dir") or "")
        if app_dir.is_dir():
            setup_logging(app_dir / "update.log")
    except (OSError, json.JSONDecodeError, TypeError):
        # 日志去哪不影响替换本身，这里失败就直接用默认日志位置
        pass

    return selfupdate.apply_update_main(plan_path)


def main() -> None:
    from tkinter import messagebox

    from launcher.gui import MindustryLauncher
    from launcher.utils import logger

    try:
        app = MindustryLauncher()
        app.root.mainloop()
    except Exception as e:
        logger.critical(f"程序崩溃: {e}", exc_info=True)
        messagebox.showerror("错误", f"程序崩溃: {e}")


if __name__ == "__main__":
    if APPLY_UPDATE_FLAG in sys.argv[1:]:
        sys.exit(run_as_updater(sys.argv[1:]))
    main()
