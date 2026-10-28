# -*- coding: utf-8 -*-
"""``--import <jar> --as <版本号>``：把一个 jar 收进版本库。

为什么单独一个模块
------------------
和 ``cli_delete.py`` 对称：入口在 ``import tkinter`` 之前就分派过来（一个 Tk
对象都不建），拿同一把进程锁。但**不共用文件** —— 一个是往里加、一个是往外
毁，改错一个分支的代价差得太远（导入错了重导一次就好，删错了只能去回收站
碰运气）。

四条规矩
--------
1. **版本号必须显式给**（``--as``）。不做「从文件名推断」：``Mindustry.jar``
   这种名字里根本没有版本号，猜出来的名字会变成清单文件名和界面上那一行，
   要改还得先删一遍。和项目里「拿不到基线就退全量，绝不猜」是同一条。
2. **同名不覆盖**。清单已存在就退出码 8 并说清楚 —— 命令行没有「要不要覆盖」
   那一下，静默盖掉一个已经装好的版本，用户下次点启动才会发现不对劲（而且
   旧版本的对象会被回收掉，回不去）。
3. **拿 ``--set`` / ``--delete*`` 同一把进程锁**。启动器开着时它的后台回收
   线程会撞上来：``add_version_from_jar`` 是「先把几千个对象写进池子、最后才
   写清单」，中间那段时间这些对象在任何人眼里都是没有引用者的孤儿（池子里的
   宽限期是 600 秒，大 jar 可能超过）。
4. **写完回读**。``add_version_from_jar`` 内部没有任何「写成功了吗」的确认，
   命令行不能跟着报成功 —— 「说了成功、东西不在」是最难查的一类。

进度那几行**只说给控制台**（没控制台时弹个「正在导入」毫无意义），最终那句
结果才走 ``say`` 的统一出口（接不上控制台时弹框）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from . import console
from .cli import (
    DEFAULT_VERSION_TYPE,
    EXIT_BAD_USAGE,
    EXIT_IMPORT_FAILED,
    EXIT_OK,
    CliRequest,
)
from .cli_info import acquire_data_lock, command_name, load_config, say
from .config import ConfigManager, sanitize_version_name
from .i18n import apply_setting, t
from .storage import open_version_manager
from .utils import BASE_DIR

logger = logging.getLogger(__name__)


def _import_version(
    config: ConfigManager | None,
    request: CliRequest,
    progress: Callable[[str], None],
) -> tuple[int, str]:
    """把 jar 收进版本库。返回 ``(退出码, 说给用户的话)``。"""
    # ★ 先校验名字再做任何磁盘操作：版本号 / 类型都会拼进清单文件名，
    #   ``..\..\evil`` 这类输入必须在这里挡住（sanitize_version_name 就是
    #   干这个的，manifest_path 里也会再挡一次）。
    try:
        vtype = sanitize_version_name(
            request.import_type or DEFAULT_VERSION_TYPE
        )
        ver = sanitize_version_name(request.import_name)
    except ValueError as e:
        return EXIT_BAD_USAGE, t("cli.import_bad_name", err=e)

    name = f"{vtype} {ver}"
    jar = Path(request.import_jar).expanduser()
    if not jar.is_file():
        return EXIT_IMPORT_FAILED, t("cli.import_jar_missing", name=name, path=jar)

    manager = open_version_manager(BASE_DIR, config)
    manifest = manager.manifest_path(vtype, ver)
    if manifest.exists():
        # 不覆盖 —— 见模块说明第 2 条。想让位就先 --delete-version，或者换个 --as。
        return EXIT_IMPORT_FAILED, t(
            "cli.import_exists", name=name, path=manifest, cmd=command_name()
        )

    try:
        size_mb = jar.stat().st_size / 1048576
        progress(
            t("cli.import_start", name=name, path=jar, size=size_mb)
        )
        started = time.perf_counter()
        # 原 jar 的 META-INF/MANIFEST.MF（带 Main-Class）当普通文件一起进池子，
        # 启动时拼回去 —— 所以导入的 jar 和官方下载的那份能力完全一样。
        manager.add_version_from_jar(jar, ver, vtype)
    except Exception as e:                                      # noqa: BLE001
        logger.error(f"导入版本失败：{e}", exc_info=True)
        return EXIT_IMPORT_FAILED, t("cli.import_failed", name=name, err=e)

    # ★ 回读：add_version_from_jar 只保证「没抛异常」，清单真落地了没，
    #   得自己看一眼（磁盘满、被别的程序占着都可能是「对象写完了、清单没写成」）。
    if not manifest.exists():
        return EXIT_IMPORT_FAILED, t(
            "cli.import_verify_failed", path=manifest
        )

    # 回收没人引用的对象（和界面的导入任务是同一步）。★ 失败路径不做 ——
    # 坏 jar 通常几十毫秒就报错，为它先扫一遍三万个对象只会让人以为卡住了。
    progress(t("cli.import_gc"))
    removed = manager.garbage_collect()
    seconds = time.perf_counter() - started
    return EXIT_OK, t(
        "cli.import_done",
        name=name,
        size=size_mb,
        seconds=seconds,
        removed=removed,
        cmd=command_name(),
    )


def run(request: CliRequest, has_console: bool) -> int:
    """执行导入，返回退出码（见 ``cli.EXIT_*``）。

    只做**一件**事 —— 优先级在 ``cli.parse_cli`` 里就定好了（``--import`` 在
    ``--export*`` 之后、``--delete*`` 之前）。
    """
    # 语言和 --help / --list / --export 同一步：界面设成 English，控制台里也该是英文。
    config: ConfigManager | None = None
    try:
        config = load_config()
        apply_setting(config.get("language"))
    except Exception as e:                                      # noqa: BLE001
        logger.warning(f"配置读不出来，按系统语言输出：{e}")

    def progress(text: str) -> None:
        """进度只说给控制台（同 ``cli_export``）。"""
        if has_console:
            console.say(text)

    lock, code, text = acquire_data_lock()
    if code != EXIT_OK:
        # 启动器开着 / 锁文件不可用 —— 一个对象都不写，说清楚为什么。
        say(text, has_console)
        return code
    try:
        code, text = _import_version(config, request, progress)
    except Exception as e:                                      # noqa: BLE001
        # 兜底：命令行不该带着异常栈退出（用户看不懂），说一句人话、
        # 细节留在日志里就够了。
        logger.error(f"命令行导入失败：{e}", exc_info=True)
        code, text = EXIT_IMPORT_FAILED, t(
            "cli.import_failed", name=request.import_name, err=e
        )
    finally:
        lock.release()

    say(text, has_console)
    return code
