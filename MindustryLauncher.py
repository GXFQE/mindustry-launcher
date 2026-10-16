# -*- coding: utf-8 -*-
"""Mindustry 启动器入口。

平时就是启动 GUI；此外还兼一个角色 —— **更新执行体**。

为什么需要它：Windows 不允许覆盖正在运行的 exe，所以「自己更新自己」
必须交给一个**外部进程**。做法是把本 exe 复制到 ``%TEMP%``，用
``--apply-update <plan.json>`` 启动那份副本；主程序随即退出，副本
按计划替换 exe / ``_internal/``（失败会回滚），见 ``launcher/selfupdate.py``。

★ 这个分支要**早于 tkinter 的 import** 处理：执行体不该建任何窗口，
  也没必要加载 Tk 那一堆 DLL（省点开销，也少一类出错可能）。

启动器自己的其它命令行参数（目前只有 ``--launch <版本>``，见
``launcher/cli.py``）在这里解析成 ``CliRequest`` 再交给界面 —— 解析规则
和「版本名怎么匹配」都在那边，本文件不掺和。

★ ``--launch`` 是**命令行**用法，和双击那条路有三处不同，都发生在这里：

  1. 先把输出接到**敲命令的那个控制台**（``console.attach()``）——
     窗口子系统的程序默认没有控制台，接不上就退化成弹框；
  2. 退出码有含义（见 ``launcher/cli.py`` 的 ``EXIT_*``），脚本里能用；
  3. 收工前在控制台打**最后一行**「启动器已退出（退出码 N）」——
     cmd 对窗口子系统程序**不等待**（提示符打完命令立刻就回来，
     还排在程序输出的**前面**），没有这一行，程序真正退出时屏幕上
     什么都不会变，用户会以为它卡住了（2026-10-17 实测）。

  界面侧则**全程不露窗口**（见 ``CoreMixin.__init__`` 里那句 withdraw）。
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


def main() -> int:
    from tkinter import messagebox

    from launcher import console
    from launcher.cli import EXIT_CRASH, parse_cli
    from launcher.gui import MindustryLauncher
    from launcher.utils import logger

    # 认不出来的参数一律忽略（--debug 之类的自家兄弟不能被误伤），
    # 所以这里不需要 try/except 包着 —— parse_cli 只返回结构、不抛异常。
    cli_request = parse_cli(sys.argv[1:])
    # 命令行用法（--launch）：先把输出接到**敲命令的那个控制台**上。
    # 打包成窗口子系统之后这个进程天生没有控制台（sys.stdout 是 None，
    # print 被 Python 直接丢掉、还不报错），不接就什么都看不见。
    # 接不上（双击快捷方式、父进程是 GUI 程序）返回 False —— 那种情况下
    # 出了错只能弹框，见 CoreMixin._cli_note。
    has_console = console.attach() if cli_request.used else False
    try:
        app = MindustryLauncher(cli_request=cli_request, console=has_console)
        app.root.mainloop()
        # 命令行用法下这个值就是脚本里 %ERRORLEVEL% / $? 拿到的东西
        # （见 launcher/cli.py 的 EXIT_*）；双击启动那条路永远是 0。
        code = app.exit_code
    except Exception as e:
        logger.critical(f"程序崩溃: {e}", exc_info=True)
        console.say(f"程序崩溃: {e}")
        if not has_console:
            messagebox.showerror("错误", f"程序崩溃: {e}")
        code = EXIT_CRASH
    if cli_request.used:
        # ★ 收尾标记，必须是控制台里的**最后一行**：cmd 对窗口子系统程序
        #   不等待（敲完命令，提示符立刻就回来了，还排在程序输出的前面），
        #   所以程序真正退出时屏幕上**什么都不会变** —— 用户看着「最后一行
        #   后面没有新提示符」会以为它没退 / 卡住了（2026-10-17 实测：翻日志
        #   两个会话其实都已正常收工）。这一行就是「结束了」的信号，顺带把
        #   退出码打出来（没接上控制台时 say 是空操作，只进日志）。
        from launcher.i18n import t

        bye = t("cli.console_bye", code=code)
        console.say(bye)
        logger.info(f"命令行收尾：{bye}")
    return code


if __name__ == "__main__":
    if APPLY_UPDATE_FLAG in sys.argv[1:]:
        sys.exit(run_as_updater(sys.argv[1:]))
    sys.exit(main())
