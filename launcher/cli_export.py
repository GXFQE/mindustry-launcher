# -*- coding: utf-8 -*-
"""``--export`` / ``--export-backup``：把版本或存档备份拼成 jar / zip。

为什么单独一个模块
------------------
和 ``cli_info.py`` 一样，它们**不需要界面**（入口在 ``import tkinter`` 之前
就把它们分派过来了）。但和「只输出」不同，这两个**会往磁盘写东西**、而且慢
（拼一个版本要读几千个 CAS 对象、115 MB 量级），所以不和 ``--help`` /
``--list`` 挤在一个文件里 —— 那个文件的价值就是「一句话的事，秒回」。

两条不能搞混的语义
------------------
* ``--export`` 拼的是**版本** → ``.jar``。用的就是启动器自己启动时拼的那个
  ``build_runtime_jar``（原 jar 的 ``META-INF/MANIFEST.MF`` 带着 ``Main-Class``，
  所以导出的 jar 拿到别的机器上能直接 ``java -jar``）。**压缩方式不同**：
  导出这份 ``ZIP_DEFLATED``（117 MB → 84 MB，多花约 5 s），启动那份 ``ZIP_STORED``
  （每次启动都拼，压了只会更慢）。
* ``--export-backup`` 拼的是**备份** → ``.zip``。**只读 CAS，绝不碰存档目录** ——
  那是 ``restore_backup`` 的事，它有回滚快照那一整套保护。搞混的后果是
  「导出个备份发给朋友」把用户当前存档覆盖掉。

输出口径和别的命令行开关一致：接上控制台就写控制台，接不上才弹框（``say``）。
进度那几行**只说给控制台** —— 没控制台时弹个「正在拼装」的框毫无意义。
"""
from __future__ import annotations

import logging
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import console
from .cli import (
    EXIT_EXPORT_FAILED,
    EXIT_NO_VERSION,
    EXIT_OK,
    CliRequest,
    available_names,
    match_version,
)
from .cli_info import load_config, say
from .config import ConfigManager
from .i18n import apply_setting, t
from .storage import (
    BackupManager,
    CASStore,
    backup_stamp,
    list_all_backups,
    open_version_manager,
)
from .utils import BASE_DIR

logger = logging.getLogger(__name__)

# Windows 文件名里不能出现的字符。导出名是「类型_版本号」「分类_时间戳」拼的，
# 分类名是用户自己起的（可能带空格、斜杠、问号），必须过一遍。
_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, fallback: str = "export") -> str:
    """把任意名字变成能当文件名的样子。

    ★ 这里**不能**用 ``config.sanitize_version_name``：那是**校验**（不合法就
      抛异常，因为版本号要拼进清单文件名、错了会写到目录外）。导出时用户只是
      想把手里这份东西存成文件，分类名叫「旧版 归档」不该让整件事失败 ——
      把非法字符换成 ``_`` 继续走才对。
    """
    cleaned = _BAD_FILENAME.sub("_", (name or "").strip()).strip(". ")
    return cleaned or fallback


def resolve_output(raw: str, default_name: str) -> Path:
    """把 ``--out`` 变成最终的文件路径；已存在就加序号，**绝不覆盖**。

    * 没给 ``--out`` ⇒ 当前目录 + 默认文件名（「一键」的意思就是不用想放哪）；
    * 给的是目录（已存在的目录、或以 ``/`` ``\\`` 结尾）⇒ 目录 + 默认文件名；
    * 否则就当成完整文件路径，用户写什么是什么（不猜他要不要后缀）。

    ★ 「加序号」不是「覆盖」：导出是可重复的产物，但用户可能已经把上一次的
      挪走、改名、发给别人了 —— 覆盖一次就找不回来。这条和项目里「用户数据
      只增不毁」是同一条规矩。
    """
    if raw:
        target = Path(raw).expanduser()
        if raw.endswith(("/", "\\")) or target.is_dir():
            target = target / default_name
    else:
        target = Path.cwd() / default_name

    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    n = 2
    while True:
        candidate = target.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            logger.info(f"目标已存在，改写到 {candidate.name}")
            return candidate
        n += 1


def version_filename(hit: dict[str, Any]) -> str:
    """版本导出的默认文件名：``MindustryX_2025.10.X20.jar``。

    用「类型_版本号」而不是界面上的显示名（``Mindustry v146``）：后者带空格，
    而前者正是清单文件名用的那套拼法，跨平台、跨压缩工具都省事。
    """
    vtype = safe_filename(str(hit.get("type", "")), "version")
    raw = safe_filename(str(hit.get("raw_version", "")), "x")
    return f"{vtype}_{raw}.jar"


def _resolve_profile(base: Path, profile: str) -> Path | None:
    """把分类名解析成目录；越界（``..\\..``）或指到根上返回 None。

    ★ 分类名是命令行传进来的，直接 ``base / profile`` 的话 ``..`` 能逃到
      数据根之外。导出只读不写，但「读任意目录然后打包」同样不该被一个
      参数打开。
    """
    try:
        root = base.resolve()
        target = (base / profile).resolve()
    except OSError:
        return None
    if target == root or not target.is_relative_to(root):
        return None
    return target


def _not_found_text(query: str, versions: list[dict[str, Any]]) -> str:
    """「没有这个版本」时的那段话：点名 + 可用清单（和 ``--launch`` 一个口径）。"""
    names, more = available_names(versions)
    listing = "\n".join(f"  {n}" for n in names)
    if more:
        listing += "\n" + t("cli.more_versions", count=more)
    return t("cli.export_not_found", name=query, versions=listing)


def _export_version(
    config: ConfigManager | None,
    query: str,
    out: str,
    progress: Callable[[str], None],
) -> tuple[int, str]:
    """把一个版本拼成 jar。返回 ``(退出码, 说给用户的那段话)``。"""
    manager = open_version_manager(BASE_DIR, config)
    versions = manager.get_versions()
    hit, candidates = match_version(versions, query)
    if hit is None:
        if candidates:
            listing = "\n".join(f"  {c}" for c in candidates)
            return EXIT_NO_VERSION, t(
                "cli.export_ambiguous", name=query, candidates=listing
            )
        return EXIT_NO_VERSION, _not_found_text(query, versions)

    output = resolve_output(out, version_filename(hit))
    output.parent.mkdir(parents=True, exist_ok=True)
    progress(t("cli.export_version_start", name=hit.get("name", query)))
    started = time.perf_counter()
    try:
        manager.build_runtime_jar(
            str(hit["type"]),
            str(hit["raw_version"]),
            output,
            # 导出用压缩：同样的内容 117 MB → 84 MB，多花约 5 s（一次性，值）。
            # 启动路径那份仍是不压缩的 —— 它每次启动都要拼，见 build_runtime_jar。
            compression=zipfile.ZIP_DEFLATED,
        )
    except Exception as e:                                      # noqa: BLE001
        logger.error(f"导出失败：{e}", exc_info=True)
        return EXIT_EXPORT_FAILED, t("cli.export_failed", err=e)
    seconds = time.perf_counter() - started
    return EXIT_OK, t(
        "cli.export_version_done",
        name=hit.get("name", query),
        path=output,
        size=output.stat().st_size / 1048576,
        seconds=seconds,
    )


def _export_backup(
    config: ConfigManager | None,
    profile: str,
    stamp: str,
    out: str,
    progress: Callable[[str], None],
) -> tuple[int, str]:
    """把一个存档备份拼成 zip。返回 ``(退出码, 说给用户的那段话)``。"""
    base = BASE_DIR / "backups"
    profile_dir = _resolve_profile(base, profile)
    manifests_dir = (profile_dir / "manifests") if profile_dir else None
    if manifests_dir is None or not manifests_dir.is_dir():
        groups = list_all_backups(base)
        listing = "\n".join(f"  {name}" for name in groups)
        return EXIT_NO_VERSION, t(
            "cli.export_profile_missing",
            name=profile,
            profiles=listing or t("cli.list_backups_empty"),
        )

    # 清单名是 backup_<YYYY-MM-DD_HH-MM-SS>[_序号].json，字典序＝时间序
    manifests = sorted(manifests_dir.glob("backup_*.json"), reverse=True)
    if not manifests:
        return EXIT_NO_VERSION, t("cli.export_profile_empty", name=profile)

    if stamp:
        chosen = [p for p in manifests if backup_stamp(p) == stamp]
        if not chosen:
            stamps = "\n".join(f"  {backup_stamp(p)}" for p in manifests)
            return EXIT_NO_VERSION, t(
                "cli.export_stamp_missing",
                profile=profile,
                stamp=stamp,
                stamps=stamps,
            )
        manifest_path = chosen[0]
    else:
        # 省略时间戳 = 该分类**最新**那个（用户说「导出最近的存档」时的意思）
        manifest_path = manifests[0]

    final_stamp = backup_stamp(manifest_path)
    output = resolve_output(
        out, f"{safe_filename(profile)}_{safe_filename(final_stamp)}.zip"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    progress(
        t("cli.export_backup_start", profile=profile, stamp=final_stamp)
    )
    started = time.perf_counter()
    try:
        # ★ 只是拼 zip —— 别改成 restore_backup，那会把用户当前存档覆盖掉
        manager = BackupManager(CASStore(base), config, base, profile)
        manager.export_backup(manifest_path, output)
    except Exception as e:                                      # noqa: BLE001
        logger.error(f"导出备份失败：{e}", exc_info=True)
        return EXIT_EXPORT_FAILED, t("cli.export_failed", err=e)
    seconds = time.perf_counter() - started
    return EXIT_OK, t(
        "cli.export_backup_done",
        profile=profile,
        stamp=final_stamp,
        path=output,
        size=output.stat().st_size / 1048576,
        seconds=seconds,
    )


def run(request: CliRequest, has_console: bool) -> int:
    """执行 ``--export`` 或 ``--export-backup``，返回退出码（见 ``cli.EXIT_*``）。

    只做**一件**事 —— 优先级在 ``cli.parse_cli`` 里就定好了
    （``--export`` 先于 ``--export-backup``，两者都先于 ``--launch``）。
    """
    # 语言和 --help / --list 同一步：用户把界面设成 English，控制台里也该是英文。
    # 读不出来不算事 —— 导出不依赖配置（版本库、备份库都按数据根直接打开）。
    config: ConfigManager | None = None
    try:
        config = load_config()
        apply_setting(config.get("language"))
    except Exception as e:                                      # noqa: BLE001
        logger.warning(f"配置读不出来，按系统语言输出：{e}")

    def progress(text: str) -> None:
        """进度只说给控制台。

        没控制台时（双击快捷方式带着 --export）不该为「正在拼装」弹一个框 ——
        用户要的是最后那个结果。结果由 ``say`` 统一出口（那时才弹框）。
        """
        if has_console:
            console.say(text)

    try:
        if request.export:
            code, text = _export_version(
                config, request.export, request.out, progress
            )
        else:
            code, text = _export_backup(
                config,
                request.export_profile,
                request.export_backup,
                request.out,
                progress,
            )
    except Exception as e:                                      # noqa: BLE001
        # 兜底：命令行不该带着异常栈退出（用户看不懂），说一句人话、
        # 细节留在日志里就够了。
        logger.error(f"命令行导出失败：{e}", exc_info=True)
        code, text = EXIT_EXPORT_FAILED, t("cli.export_failed", err=e)

    say(text, has_console)
    return code
