# -*- coding: utf-8 -*-
"""``--restore-backup <分类> [<时间戳>]``：把一条存档备份写回存档目录。

为什么单独一个模块
------------------
和 ``cli_delete.py`` / ``cli_import.py`` 同一条路（入口在 ``import tkinter``
之前就分派过来，一个 Tk 对象都不建），但它是这三者里**唯一会覆盖用户数据**的
那个：删除只动「清单」，恢复要把整个存档目录清空再重写。所以宁可再多一个
文件，也不和上面两个挤在一起。

三条规矩
--------
1. **必须 ``--yes``**。命令行没有「你确定吗」那一下，缺了就不执行（解析层
   按参数写坏处理，退出码 2）。这条判断只在 ``cli.parse_cli`` 一处 ——
   这里不重复判断，否则将来加别的写操作就会漏。
2. **目标不许猜**。分类名在 ``list_all_backups()`` 的结果里**查表**，绝不拿
   用户输入去拼路径（``..\\..`` 拼进去等于「按输入覆盖任意目录」）。
3. **和 ``--set`` / ``--delete*`` 拿同一把进程锁**。启动器开着时它的自动备份
   和备份窗口正在读同一个存档目录，撞上轻则「备份到一半」，重则两边互相覆盖。

底层的退路一条不少，全部**复用**界面的 ``BackupManager.restore_backup``：
先把当前存档打成 ``rollback_*.zip``（建不成功就中止、一个文件都不删），
清空旧内容走回收站（除非设置里开了 ``permanent_delete``），中途失败会回滚。
命令行**没有**另写一套恢复逻辑 —— 那种两套实现迟早会走岔，而走岔的代价是
用户的存档。

恢复完**要回读**：清单里写了几个文件，存档目录里就该有几个（少一个就是没
写全）。``restore_backup`` 内部对每个文件都有异常处理，但「什么都没抛、文件
却没落地」这类事在磁盘满 / 杀软拦截时真会发生，命令行不能跟着报成功。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from . import console
from .cli import (
    EXIT_NO_VERSION,
    EXIT_OK,
    EXIT_RESTORE_FAILED,
    CliRequest,
)
from .cli_info import acquire_data_lock, load_config, say
from .config import ConfigManager
from .i18n import apply_setting, t
from .storage import (
    BackupManager,
    CASStore,
    backup_description,
    backup_stamp,
    list_all_backups,
)
from .utils import BASE_DIR

logger = logging.getLogger(__name__)


def _restore_backup(
    config: ConfigManager | None,
    profile: str,
    stamp: str,
    progress: Callable[[str], None],
) -> tuple[int, str]:
    """把一条备份恢复进该分类的存档目录。返回 ``(退出码, 说给用户的话)``。"""
    base = BASE_DIR / "backups"
    groups = list_all_backups(base)
    # ★ 查表，不拼路径：分类名是命令行传进来的，``..\\..`` 拼进路径就成了
    #   「按输入覆盖任意目录」。表里没有的名字一律当「没有这个分类」。
    manifests = groups.get(profile)
    if not manifests:
        listing = "\n".join(f"  {name}" for name in groups)
        return EXIT_NO_VERSION, t(
            "cli.restore_backup_missing",
            name=profile,
            profiles=listing or t("cli.list_backups_empty"),
        )

    if stamp:
        chosen = [p for p in manifests if backup_stamp(p) == stamp]
        if not chosen:
            stamps = "\n".join(f"  {backup_stamp(p)}" for p in manifests)
            return EXIT_NO_VERSION, t(
                "cli.restore_backup_stamp_missing",
                profile=profile,
                stamp=stamp,
                stamps=stamps,
            )
        target = chosen[0]
    else:
        # 省略时间戳 = 该分类**最新**那个（和 --export-backup / --delete-backup
        # 同一个约定）
        target = manifests[0]

    final_stamp = backup_stamp(target)
    desc = backup_description(target)
    if not target.exists():
        # 列表和恢复之间被别的实例 / 资源管理器删掉了
        return EXIT_RESTORE_FAILED, t(
            "cli.restore_missing_now", path=target
        )

    if config is None:
        # 配置读不出来就不知道这个分类的存档目录在哪儿 —— 宁可不做。
        return EXIT_RESTORE_FAILED, t("cli.restore_need_config", name=profile)

    manager = BackupManager(CASStore(base), config, base, profile)
    save_dir = Path(config.get_save_path(profile))
    if desc:
        progress(
            t(
                "cli.restore_start",
                desc=desc,
                profile=profile,
                stamp=final_stamp,
                path=save_dir,
            )
        )
    else:
        progress(
            t(
                "cli.restore_start_bare",
                profile=profile,
                stamp=final_stamp,
                path=save_dir,
            )
        )
    manager.restore_backup(target)

    # ★ 回读：清单里列了几个文件，目录里就该有几个。少一个都算没恢复成功 ——
    #   这时 rollback 快照可能已经被删掉了（restore_backup 成功返回后才会删），
    #   所以只说「没写全」，让用户去存档目录自己看一眼，别报一个假成功。
    expected = _expected_files(target)
    actual = _count_files(save_dir)
    if expected and actual < expected:
        return EXIT_RESTORE_FAILED, t(
            "cli.restore_verify_failed",
            path=save_dir,
            expected=expected,
            actual=actual,
        )
    if desc:
        return EXIT_OK, t(
            "cli.restore_done",
            desc=desc,
            profile=profile,
            stamp=final_stamp,
            path=save_dir,
            count=actual,
        )
    return EXIT_OK, t(
        "cli.restore_done_bare",
        profile=profile,
        stamp=final_stamp,
        path=save_dir,
        count=actual,
    )


def _expected_files(manifest_path: Path) -> int:
    """清单里写的文件数；读不动就返回 0（表示「不校验」，别误判成失败）。"""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.get("files")
        return len(files) if isinstance(files, dict) else 0
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning(f"恢复后读清单失败（跳过文件数校验）: {manifest_path}: {e}")
        return 0


def _count_files(directory: Path) -> int:
    """数一下目录里的普通文件（递归）。目录不在 = 0。"""
    if not directory.is_dir():
        return 0
    try:
        return sum(1 for p in directory.rglob("*") if p.is_file())
    except OSError as e:
        logger.warning(f"统计存档目录失败 {directory}: {e}")
        return 0


def run(request: CliRequest, has_console: bool) -> int:
    """执行恢复，返回退出码（见 ``cli.EXIT_*``）。

    只做**一件**事 —— 优先级在 ``cli.parse_cli`` 里就定好了（``--restore-backup``
    在 ``--import`` 之后、``--delete*`` 之前）。
    """
    # 语言和 --help / --list / --export 同一步：界面设成 English，控制台里也该是英文。
    config: ConfigManager | None = None
    try:
        config = load_config()
        apply_setting(config.get("language"))
    except Exception as e:                                      # noqa: BLE001
        logger.warning(f"配置读不出来，按系统语言输出：{e}")

    def progress(text: str) -> None:
        """进度只说给控制台（同 ``cli_import``）。"""
        if has_console:
            console.say(text)

    lock, code, text = acquire_data_lock()
    if code != EXIT_OK:
        # 启动器开着 / 锁文件不可用 —— 一个文件都不动，说清楚为什么。
        say(text, has_console)
        return code
    try:
        code, text = _restore_backup(
            config, request.restore_profile, request.restore_stamp, progress
        )
    except Exception as e:                                      # noqa: BLE001
        # 兜底：命令行不该带着异常栈退出（用户看不懂），说一句人话、细节留在
        # 日志里就够了。★ 这里**不**替用户「顺手回滚」—— restore_backup
        # 内部已经回滚过了；再动一次只会把回滚好的存档又搅一遍。
        logger.error(f"命令行恢复失败：{e}", exc_info=True)
        code, text = EXIT_RESTORE_FAILED, t("cli.restore_failed", err=e)
    finally:
        lock.release()

    say(text, has_console)
    return code
