# -*- coding: utf-8 -*-
"""``--list`` / ``--list-backups`` / ``--help``：只输出、**连窗口都不建**的命令。

为什么单独一个模块
------------------
它们和 ``--launch`` 一样属于命令行用法，但比 ``--launch`` 还轻：**根本不需要
界面**。入口 ``MindustryLauncher.main`` 在 ``import tkinter`` **之前**就把它们
交给这里（见那条分支）—— 所以它们不能住在 ``gui_*`` 里：那些模块 import 的
tkinter 正是这里要避开的。

（``--export`` / ``--export-backup`` 也走同一条「不进界面」的路，但会往磁盘写
东西、还慢，所以单独住在 ``cli_export.py``。）

输出口径和 ``--launch`` 完全一致：**接上控制台就写控制台，接不上才弹框**。
``console.attach()`` 由入口调好、结果当参数传进来，这里不自己接（「谁负责接」
只该有一处）—— ``say`` 这个出口也是公开的，``cli_export`` 复用它。

依赖：``config`` / ``storage`` / ``i18n`` / ``console`` —— 全是下层模块。正常
路径上一个 Tk 对象都不建；弹框那条退化路在函数体里才 import tkinter。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import console
from .cli import (
    DELETE_BACKUP_FLAG,
    DELETE_VERSION_FLAG,
    EXIT_BAD_USAGE,
    EXIT_CRASH,
    EXIT_OK,
    EXPORT_BACKUP_FLAG,
    LAUNCH_FLAG,
    SET_FLAG,
    YES_FLAG,
    CliRequest,
)
from .config import ConfigManager
from .i18n import apply_setting, t
from .storage import (
    backup_description,
    backup_stamp,
    list_all_backups,
    open_version_manager,
)
from .utils import BASE_DIR
from .version import APP_NAME, __version__

logger = logging.getLogger(__name__)


def load_config() -> ConfigManager:
    """读配置（只读用途：语言、version_sources）。

    公开名字是因为 ``cli_export`` 也要读同一份 —— 「配置从哪儿读」只该有
    一处，两边各写一遍迟早走岔。
    """
    return ConfigManager(BASE_DIR / "config.json")


def command_name() -> str:
    """帮助里「本程序」怎么写 —— 要让用户能直接复制粘贴。

    打包后是 exe 文件名；源码运行时是 ``python MindustryLauncher.py``
    （只写 ``python`` 的话，用户照着敲只会得到一个空的解释器）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).name
    script = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    return f"python {script}" if script else "python MindustryLauncher.py"


def collect_versions(config: ConfigManager | None = None) -> list[dict]:
    """读运行时目录里的版本列表（不建窗口、不碰 tkinter）。

    ★ 和界面走**同一个**打开方式（``storage.open_version_manager``）——
      两处各写一遍「版本库在数据根的哪儿」，将来改布局漏一处，症状是
      「界面看得见、命令行看不见」，而且只在命令行那条路上复现。
    """
    return open_version_manager(
        BASE_DIR, config if config is not None else load_config()
    ).get_versions()


def render_list(versions: list[dict]) -> str:
    """``--list`` 的正文：一行一个版本名（就是界面列表里那些行）。"""
    names = [str(v.get("name", "")) for v in versions]
    if not names:
        return t("cli.list_empty")
    lines = [t("cli.list_header", count=len(names))]
    lines.extend(f"  {name}" for name in names)
    lines.append("")
    lines.append(t("cli.list_footer"))
    return "\n".join(lines)


def render_backups() -> str:
    """``--list-backups`` 的正文：按分类分组，每行「时间戳 + 备注」。

    时间戳就是 ``--export-backup`` 要填的那个值 —— 用户从这儿复制过去即可。
    分类名和磁盘一致（不查配置）：能列出来的，就一定导得出来。
    """
    groups = list_all_backups(BASE_DIR / "backups")
    total = sum(len(items) for items in groups.values())
    if not total:
        return t("cli.list_backups_empty")

    lines = [t("cli.list_backups_header", count=total)]
    for profile, manifests in groups.items():
        lines.append("")
        lines.append(t("cli.list_backups_profile", name=profile, count=len(manifests)))
        for manifest_path in manifests:
            desc = backup_description(manifest_path)
            stamp = backup_stamp(manifest_path)
            lines.append(
                t("cli.list_backups_line", stamp=stamp, desc=desc)
                if desc else t("cli.list_backups_line_bare", stamp=stamp)
            )
    lines.append("")
    lines.append(t("cli.list_backups_footer"))
    return "\n".join(lines)


def report_bad_usage(request: CliRequest, has_console: bool) -> int:
    """参数写了一半（``--launch`` / ``--export`` / ``--set`` 缺值）：说清楚 + 退出码 2。

    ★ 在**入口**就处理掉、根本不进界面。这类命令要的是「在控制台里把事做完」，
      为一句「你少写了个名字」建一个窗口（再立刻收掉）既不必要，也正是零界面
      约定要避免的 —— 而且会闪一下。
    ★ ``--export-backup`` 缺的是**分类名**不是版本名，话术不一样（见那个分支）；
      ``--set`` 缺的是「键=值」，而且「哪一项不是 键=值」和「一项都没给」
      也要分开说（``bad_token``）—— 前者多半是带空格的参数忘了加引号。
    ★ ``bad_flag == --yes`` 是另一回事：删除**写全了**，只是没确认。这条不许
      说成「参数写坏了」了事，要明确告诉用户「什么都没删，加 --yes 才动」。
    ★ 两个删除目标同时给时 ``bad_note`` 直接指定文案（那条规则没有对应的
      开关名可说，硬套任何一条现成话术都会说错）。
    """
    flag = request.bad_flag or LAUNCH_FLAG
    if request.bad_note:
        key = request.bad_note
    elif flag == YES_FLAG:
        key = "cli.bad_usage_delete"
    elif flag == DELETE_VERSION_FLAG:
        key = "cli.bad_usage_delete_version"
    elif flag == DELETE_BACKUP_FLAG:
        key = "cli.bad_usage_delete_backup"
    elif flag == SET_FLAG and request.bad_token:
        key = "cli.set_bad_pair"
    elif flag == SET_FLAG:
        key = "cli.bad_usage_set"
    elif flag == EXPORT_BACKUP_FLAG:
        key = "cli.bad_usage_backup"
    else:
        key = "cli.bad_usage"
    # 几段文案的占位符不同（cmd / flag / token），多传的会被 format 忽略
    say(
        t(
            key,
            flag=flag,
            cmd=command_name(),
            token=request.bad_token,
        ),
        has_console,
    )
    return EXIT_BAD_USAGE


def render_help() -> str:
    """``--help`` 的正文（用法、版本名怎么写、退出码、cmd 不等待这件事）。"""
    return t("cli.help_body", cmd=command_name(), version=__version__)


def run(request: CliRequest, has_console: bool) -> int:
    """执行 ``--help`` 或 ``--list``，返回退出码（见 ``cli.EXIT_*``）。

    只做**一件**事 —— 优先级在 ``cli.parse_cli`` 里就定好了
    （``--help`` → ``--list``）。
    """
    # ★ 先按配置里的 language 装载语言包 —— 和界面启动时同一步。不这么做的话，
    #   用户把界面设成 English、在控制台敲 --help，看到的还是系统语言（i18n
    #   在 import 时只按系统语言 bootstrap 过一次）。
    #   ★ MDT_LANG 钉死时 apply_setting 是空操作，测试/真机实测不受影响。
    #   ★ 这一步失败**不算事**：--help 是「怎么用」的问题，不该因为 config.json
    #     坏了就答不上来（--list 确实要读配置，那一步失败在下面照常报）。
    config: ConfigManager | None = None
    try:
        config = load_config()
        apply_setting(config.get("language"))
    except Exception as e:                                      # noqa: BLE001
        logger.warning(f"配置读不出来，按系统语言输出：{e}")

    try:
        if request.show_help:
            text = render_help()
            logger.info("命令行 --help：打印用法说明")
        elif request.list_backups:
            text = render_backups()
            logger.info("命令行 --list-backups：列出存档备份")
        else:
            versions = collect_versions(config)
            text = render_list(versions)
            logger.info(f"命令行 --list：列出 {len(versions)} 个版本")
    except Exception as e:                                      # noqa: BLE001
        # 读配置 / 读清单各自都有兜底，这里是最后一道。命令行不该带着异常栈
        # 退出（用户看不懂），说一句人话、把细节留在日志里就够了。
        logger.error(f"命令行信息输出失败：{e}", exc_info=True)
        say(t("cli.info_failed", err=e), has_console)
        return EXIT_CRASH

    say(text, has_console)
    return EXIT_OK


def say(text: str, has_console: bool) -> None:
    """能说给控制台就说控制台；说不了才弹框（见 ``show_in_box``）。

    公开名字是因为 ``cli_export`` 也要用同一个出口 —— 「一句话怎么送到用户
    眼前」只该有一处实现，否则哪天改了弹框行为，只会改到其中一半。
    """
    if has_console:
        console.say(text)
        return
    show_in_box(text)


def show_in_box(text: str) -> None:
    """没有控制台时的退化出口（双击快捷方式带着 ``--help`` / ``--list``）。

    ★ tkinter 只在这条路上 import —— 正常路径（从控制台敲）一个 Tk 都不建。
      这也正是 ``--help`` / ``--list`` 不肯住在 ``gui_*`` 里的原因。
    """
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, text)
        root.destroy()
    except Exception as e:                                      # noqa: BLE001
        logger.error(f"没有控制台，提示框也弹不出来：{e}")
