# -*- coding: utf-8 -*-
"""Mindustry 启动器入口。

平时就是启动 GUI；此外还兼一个角色 —— **更新执行体**。

为什么需要它：Windows 不允许覆盖正在运行的 exe，所以「自己更新自己」
必须交给一个**外部进程**。做法是把本 exe 复制到 ``%TEMP%``，用
``--apply-update <plan.json>`` 启动那份副本；主程序随即退出，副本
按计划替换 exe / ``_internal/``（失败会回滚），见 ``launcher/selfupdate.py``。

★ 这个分支要**早于 tkinter 的 import** 处理：执行体不该建任何窗口，
  也没必要加载 Tk 那一堆 DLL（省点开销，也少一类出错可能）。

启动器自己的其它命令行参数（``--launch <版本>`` / ``--list`` / ``--list-backups``
/ ``--help`` / ``--get [<键>]`` / ``--set <键>=<值>`` / ``--export <版本>`` /
``--export-backup <分类> [<时间戳>]`` / ``--out <路径>`` /
``--delete-version <版本>`` / ``--delete-backup <分类> [<时间戳>]`` /
``--yes``，见 ``launcher/cli.py``）
在这里解析成 ``CliRequest`` 再分派 —— 解析规则和「版本名怎么匹配」都在那边，
本文件不掺和。

★ 五类用法，轻重不同：

  * ``--list`` / ``--list-backups`` / ``--help`` / ``--get`` —— **只输出信息**。
    它们在 ``import tkinter`` **之前**就交给 ``launcher/cli_info.py``（``--get``
    在 ``cli_cfg.py``）处理掉：这条路上一个 Tk 对象都不会建（连模块都不 import）。
    读版本列表走 ``storage.open_version_manager``，和界面同一个打开方式。
  * ``--set <键>=<值>`` —— **只改配置**。同样在 import tkinter 之前，但它会先
    拿一把「配置锁」（``applock.py``）：启动器开着时拿不到就拒绝改，因为那个
    实例退出时会用内存里的旧配置整个写回，改动会被静默吃掉。
  * ``--export`` / ``--export-backup`` —— **只产出文件**。同样在 ``import
    tkinter`` 之前交给 ``launcher/cli_export.py``：拼 jar / zip 不需要界面，
    只是慢（版本那边要读几千个 CAS 对象）。
  * ``--delete-version`` / ``--delete-backup`` —— **只删东西**。同样在
    ``import tkinter`` 之前交给 ``launcher/cli_delete.py``，也拿同一把进程锁
    （启动器开着时它内存里的列表还指着要删的记录）。**必须带 ``--yes``**，
    漏写由解析层按参数写坏处理 —— 一条删除都不会执行。
  * ``--launch <版本>`` —— 要拉起游戏，得走完整条启动路径（界面照建、
    只是全程不露）。它和双击那条路有三处不同，都发生在这里：

  1. 先把输出接到**敲命令的那个控制台**（``console.attach()``）——
     窗口子系统的程序默认没有控制台，接不上就退化成弹框；
  2. 退出码有含义（见 ``launcher/cli.py`` 的 ``EXIT_*``），脚本里能用；
  3. 收工前在控制台打**最后一行**「启动器已退出（退出码 N）」——
     cmd 对窗口子系统程序**不等待**（提示符打完命令立刻就回来，
     还排在程序输出的**前面**），没有这一行，程序真正退出时屏幕上
     什么都不会变，用户会以为它卡住了（2026-10-17 实测）。

  界面侧则**全程不露窗口**（见 ``CoreMixin.__init__`` 里那句 withdraw）。

★ 参数写了一半（``--launch`` / ``--export`` / ``--set`` 后面没跟值）**在入口
  就答复掉**，连界面分支都不进 —— 为一句「你少写了个名字」建个窗口再收掉，
  既没必要又会闪一下（见 ``cli_info.report_bad_usage``）。
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
    from launcher import console
    from launcher.cli import EXIT_CRASH, parse_cli
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

    if cli_request.bad_usage:
        # ★ 参数写了一半（--launch / --export 后面没跟值）—— 在**入口**就答复
        #   掉，连界面分支都不进：为一句「你少写了个名字」去建一个窗口再立刻
        #   收掉，既不必要又会闪一下，正是零界面约定要避免的。
        from launcher import cli_info

        code = cli_info.report_bad_usage(cli_request, has_console)
    elif cli_request.info_only:
        # ★ --list / --list-backups / --help：纯输出，**在 import tkinter 之前**
        #   处理掉 —— 这条路上一个 Tk 对象都不会建（cli_info 里连 tkinter 都是
        #   延迟 import 的，只在「没控制台、只能弹框」那条退化路上才用）。
        # ★ 这里**不能直接 return**：下面的收尾标记是命令行用法的最后一行，
        #   四条路（帮助 / 列版本 / 启动成功 / 启动失败）都得走到它 ——
        #   真机实测抓到的：提前 return 会让 `--list` 输出完就没了下文，
        #   用户看不到「启动器已退出（退出码 N）」，跟 `--launch` 的约定不一致。
        from launcher import cli_info

        code = cli_info.run(cli_request, has_console)
    elif cli_request.cfg_only:
        # ★ --get / --set：看和改设置，同样不需要界面。--set 会先拿一把
        #   「配置锁」（见 applock.py）—— 启动器开着时它拿不到，于是拒绝改，
        #   因为那个实例退出时会用内存里的旧配置整个写回、把改动吃掉。
        from launcher import cli_cfg

        code = cli_cfg.run(cli_request, has_console)
    elif cli_request.export_only:
        # ★ --export / --export-backup：和上面同理，也不需要界面。只是这两个
        #   会往磁盘写东西、慢得多（拼版本要读几千个 CAS 对象）。
        from launcher import cli_export

        code = cli_export.run(cli_request, has_console)
    elif cli_request.delete_only:
        # ★ --delete-version / --delete-backup：同样不需要界面。这条路会**删**
        #   磁盘上的记录，所以必须带 --yes（缺了在 parse_cli 里已成 bad_usage，
        #   走上面那条分支），而且和 --set 抢同一把锁。
        from launcher import cli_delete

        code = cli_delete.run(cli_request, has_console)
    else:
        from tkinter import messagebox

        from launcher.gui import MindustryLauncher

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
