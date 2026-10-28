# -*- coding: utf-8 -*-
"""``--delete-version`` / ``--delete-backup``：删掉一个游戏版本或一条存档备份。

为什么单独一个模块
------------------
和 ``cli_export.py`` 走同一条路（入口在 ``import tkinter`` 之前就分派过来，
一个 Tk 对象都不建），但语义相反：导出是**产出**文件，这里是**毁掉**记录。
放在同一个文件里，改错一个分支的代价差得太远 —— 导错了重导一次就好，
删错了只能去回收站碰运气。所以宁可多一个文件。

三条硬规矩
----------
1. **必须 ``--yes``**。命令行没有「你确定吗」那一下，缺了就不执行（解析层
   按参数写坏处理，退出码 2，见 ``cli.parse_cli`` 末尾）。这条不在本模块里
   判断 —— 判断只该有一处，否则将来加一个删除开关就会漏掉。
2. **目标不许猜**。版本走 ``cli.match_version``（撞名就把候选列出来）；
   备份按分类名在 ``list_all_backups()`` 的结果里**查表**，绝不拿用户输入
   去拼路径 —— 那等于「按输入删任意目录」。
3. **和 ``--set`` 同一把进程锁**。启动器开着时拒绝：它内存里还留着版本列表，
   删完再点「启动」只会得到「清单不存在」；备份那边同理，窗口刷新时列表和
   磁盘对不上。

删完之后**都要回读**：``VersionManager.delete_version`` 内部把 ``OSError``
吞成一条日志（GUI 那条路不需要更狠），命令行不能跟着报成功 ——
「说了成功、东西还在」是最难查的一类。
"""
from __future__ import annotations

import logging
from typing import Any

from .cli import (
    EXIT_DELETE_FAILED,
    EXIT_NO_VERSION,
    EXIT_OK,
    CliRequest,
    available_names,
    match_version,
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
    open_version_manager,
)
from .utils import BASE_DIR

logger = logging.getLogger(__name__)


def _not_found_text(query: str, versions: list[dict[str, Any]]) -> str:
    """「没有这个版本」时的那段话（和 ``--export`` 一个口径，话术改成「没有删」）。"""
    names, more = available_names(versions)
    listing = "\n".join(f"  {n}" for n in names)
    if more:
        listing += "\n" + t("cli.more_versions", count=more)
    return t("cli.delete_version_missing", name=query, versions=listing)


def _delete_version(
    config: ConfigManager | None, query: str
) -> tuple[int, str]:
    """删掉一个版本（清单 + 回收孤儿对象）。返回 ``(退出码, 说给用户的话)``。"""
    manager = open_version_manager(BASE_DIR, config)
    versions = manager.get_versions()
    hit, candidates = match_version(versions, query)
    if hit is None:
        if candidates:
            listing = "\n".join(f"  {c}" for c in candidates)
            return EXIT_NO_VERSION, t(
                "cli.delete_version_ambiguous", name=query, candidates=listing
            )
        return EXIT_NO_VERSION, _not_found_text(query, versions)

    name = str(hit.get("name", query))
    vtype = str(hit["type"])
    raw_version = str(hit["raw_version"])
    manifest = manager.manifest_path(vtype, raw_version)

    manager.delete_version(vtype, raw_version)
    # ★ 回读：delete_version 把 OSError 吞成警告日志了（GUI 那边只需要一条
    #   日志），不复核的话这里会对一个没删掉的文件报「已删除」。
    if manifest.exists():
        return EXIT_DELETE_FAILED, t(
            "cli.delete_verify_failed", path=manifest
        )

    # 回收要扫整个对象池（本项目 3 万+ 对象，十几秒）。命令行没有「后台」，
    # 只能等它跑完 —— 退出码代表「做完了」，不能先报成功再偷偷扫。
    removed = manager.garbage_collect()
    return EXIT_OK, t("cli.delete_version_done", name=name, removed=removed)


def _delete_backup(
    config: ConfigManager | None, profile: str, stamp: str
) -> tuple[int, str]:
    """删掉一条存档备份（清单 + 回收孤儿对象）。返回 ``(退出码, 说给用户的话)``。"""
    base = BASE_DIR / "backups"
    groups = list_all_backups(base)
    # ★ 查表，不拼路径：分类名是命令行传进来的，``..\\..`` 拼进路径就成了
    #   「按输入删任意目录」。表里没有的名字一律当「没有这个分类」。
    manifests = groups.get(profile)
    if not manifests:
        listing = "\n".join(f"  {name}" for name in groups)
        return EXIT_NO_VERSION, t(
            "cli.delete_backup_missing",
            name=profile,
            profiles=listing or t("cli.list_backups_empty"),
        )

    if stamp:
        chosen = [p for p in manifests if backup_stamp(p) == stamp]
        if not chosen:
            stamps = "\n".join(f"  {backup_stamp(p)}" for p in manifests)
            return EXIT_NO_VERSION, t(
                "cli.delete_backup_stamp_missing",
                profile=profile,
                stamp=stamp,
                stamps=stamps,
            )
        target = chosen[0]
    else:
        # 省略时间戳 = 该分类**最新**那个（和 --export-backup 同一个约定）
        target = manifests[0]

    final_stamp = backup_stamp(target)
    desc = backup_description(target)
    if not target.exists():
        # 列表和删除之间被别人删掉/移走了（另一个实例、资源管理器）
        return EXIT_DELETE_FAILED, t(
            "cli.delete_verify_failed", path=target
        )

    manager = BackupManager(CASStore(base), config, base, profile)
    removed = manager.delete_backup(target)
    if target.exists():
        return EXIT_DELETE_FAILED, t(
            "cli.delete_verify_failed", path=target
        )
    if desc:
        return EXIT_OK, t(
            "cli.delete_backup_done",
            profile=profile,
            stamp=final_stamp,
            desc=desc,
            removed=removed,
        )
    return EXIT_OK, t(
        "cli.delete_backup_done_bare",
        profile=profile,
        stamp=final_stamp,
        removed=removed,
    )


def run(request: CliRequest, has_console: bool) -> int:
    """执行删除，返回退出码（见 ``cli.EXIT_*``）。

    只做**一件**事 —— 优先级在 ``cli.parse_cli`` 里就定好了
    （``--delete-version`` 先于 ``--delete-backup``，两者都在 ``--export*``
    之后、``--launch`` 之前）。
    """
    # 语言和 --help / --list / --export 同一步：界面设成 English，控制台里也该是英文。
    config: ConfigManager | None = None
    try:
        config = load_config()
        apply_setting(config.get("language"))
    except Exception as e:                                      # noqa: BLE001
        logger.warning(f"配置读不出来，按系统语言输出：{e}")

    lock, code, text = acquire_data_lock()
    if code != EXIT_OK:
        # 启动器开着（或者锁文件不可用）—— 两个字的结论一样：什么都没删。
        say(text, has_console)
        return code
    try:
        try:
            if request.delete_version:
                code, text = _delete_version(
                    config, request.delete_version
                )
            else:
                code, text = _delete_backup(
                    config, request.delete_profile, request.delete_stamp
                )
        except Exception as e:                                  # noqa: BLE001
            # 兜底：命令行不该带着异常栈退出（用户看不懂），说一句人话、
            # 细节留在日志里就够了。
            logger.error(f"命令行删除失败：{e}", exc_info=True)
            code, text = EXIT_DELETE_FAILED, t("cli.delete_failed", err=e)
    finally:
        lock.release()

    say(text, has_console)
    return code
