# -*- coding: utf-8 -*-
"""``--get`` / ``--set``：在控制台里看和改设置，**一个窗口都不建**。

为什么要单开一层「严格校验」
----------------------------
``ConfigManager.set()`` 走的是**读文件容错**那张规格表（``_coerce_global``）：
值写脏了一律「退回默认 + 一条 WARNING」，**绝不抛异常**。那个口径对手改
config.json 是对的（写坏了也不该让启动器起不来），但对命令行是灾难 ——
用户敲 ``--set max_log_files=很多`` 会看到「已保存」，磁盘上却是 20，
退出码还是 0（2026-10-17 实测）。所以这里另立一张**严格表**：

* 每个开放的键都有自己的 parser，坏值抛 ``BadValue``（话术说清该填什么）；
* 写完先**回读内存**（对上 ``set()`` 的整理结果）、再**回读磁盘**
  （对上 ``save()`` —— 它内部把 OSError 吞成日志，不查就发现不了写失败）；
* 任何一项不对 → 整批不落盘、退出码非 0。

开放的键刻意**不含**这两类（宁缺勿滥）：

* ``version_sources`` / ``github_mirror_presets`` —— 数组，命令行里没法
  表达得比手改 JSON 更清楚；
* ``profile.<名>.data_dir`` —— 改了**不会搬文件**，命令行里看不见那句
  「原有存档不会被搬走」的提示，最容易被误解成「已经搬完了」。

``profile.<分类>.<键>`` 的写法
------------------------------
分类名允许出现点（``v1.2``），所以按**最后一个点**切（``rsplit``）；分类名
里如果再有点就不会被认错。分类名带 ``=`` 的极端情况不支持（切不出键值）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from . import applock
from .cli import (
    EXIT_BAD_USAGE,
    EXIT_CONFIG_FAILED,
    EXIT_OK,
    CliRequest,
)
from .cli_info import command_name, load_config, say
from .config import (
    MAX_LOG_KEEP,
    MAX_MIRROR_ROUNDS,
    ConfigManager,
    normalize_mirror,
)
from .i18n import apply_setting, t
from .utils import (
    BASE_DIR,
    FALSE_WORDS,
    LANGUAGE_AUTO,
    LANGUAGE_CODES,
    LAUNCHER_UPDATE_MODES,
    TRUE_WORDS,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------- 可改的键
@dataclass(frozen=True)
class Spec:
    """一个可改的配置项。

    ``label`` 是**设置页那句文案**的 i18n 键 —— 故意复用而不是另写一份：
    用户在界面上看到的是这句话，命令行里再看到一句同义的，只会怀疑是不是
    两个不同的东西。
    """

    scope: str          # "global" | "jvm" | "profile"
    key: str            # config.json 里的键名
    label: str          # 设置页文案（i18n 键）
    kind: str           # bool | int | choice | text | mirror
    lo: int = 0         # int 的下限（含）
    hi: int = 0         # int 的上限（含）
    choices: tuple[str, ...] = ()


# 顺序＝``--get`` 的输出顺序：先「启动器设置」，再 Java，最后当前存档分类。
GLOBAL_SPECS: tuple[Spec, ...] = (
    Spec("global", "hide_on_launch", "settings.hide_on_launch", "bool"),
    Spec("global", "close_on_game_exit", "settings.close_on_exit", "bool"),
    Spec("global", "auto_update", "settings.auto_update", "bool"),
    Spec(
        "global", "launcher_update", "settings.label.launcher_update", "choice",
        choices=LAUNCHER_UPDATE_MODES,
    ),
    Spec(
        "global", "language", "settings.label.language", "choice",
        choices=(LANGUAGE_AUTO, *LANGUAGE_CODES),
    ),
    Spec("global", "github_mirror", "settings.label.mirror", "mirror"),
    Spec(
        "global", "mirror_test_rounds", "settings.label.mirror_rounds", "int",
        1, MAX_MIRROR_ROUNDS,
    ),
    Spec("global", "use_gh_auth", "settings.use_gh_auth", "bool"),
    Spec("global", "extra_vm_args", "settings.label.extra_vm", "text"),
    Spec("global", "extra_program_args", "settings.label.extra_prog", "text"),
    Spec("global", "save_game_log", "settings.save_game_log", "bool"),
    Spec(
        "global", "max_log_files", "settings.label.max_logs", "int",
        1, MAX_LOG_KEEP,
    ),
    Spec("global", "permanent_delete", "settings.permanent_delete", "bool"),
)

#: 只开 ``jre_path``：``main_class`` / ``vm_args`` 改错了是「游戏起不来」，
#: 不该从命令行开这个口子（VM 参数走 extra_vm_args，设置页也是那个）。
JVM_SPECS: tuple[Spec, ...] = (
    Spec("jvm", "jre_path", "settings.label.jre_path", "text"),
)

#: 数值区间跟设置页的数字框一致（1~60 / 1~50），不是随便定的：
#: 命令行和界面入口给出**同一个**合法范围，用户才不会觉得两边打架。
PROFILE_SPECS: tuple[Spec, ...] = (
    Spec("profile", "min_playtime", "settings.label.min_playtime", "int", 1, 60),
    Spec("profile", "auto_backup", "settings.auto_backup", "bool"),
    Spec("profile", "max_backups", "settings.label.max_backups", "int", 1, 50),
)

_GLOBAL_BY_KEY = {s.key: s for s in GLOBAL_SPECS}
_JVM_BY_KEY = {s.key: s for s in JVM_SPECS}
_PROFILE_BY_KEY = {s.key: s for s in PROFILE_SPECS}


# --------------------------------------------------------------------- 异常
class BadKey(Exception):
    """这个键不在开放范围里。"""

    def __init__(self, key: str) -> None:
        self.key = key


class BadProfile(Exception):
    """分类名不存在。"""

    def __init__(self, name: str) -> None:
        self.name = name


class BadValue(Exception):
    """值不合法。``text`` 已经是**给人看的一句话**（本地化过了）。"""

    def __init__(self, text: str) -> None:
        self.text = text


@dataclass(frozen=True)
class Target:
    """一个已经解析好的「哪个键」。"""

    spec: Spec
    profile: str = ""

    @property
    def scope(self) -> str:
        return self.spec.scope

    @property
    def key(self) -> str:
        return self.spec.key


def resolve(raw: str) -> Target:
    """把 ``key`` / ``jvm.key`` / ``profile.<分类>.key`` 解析成 ``Target``。"""
    name = (raw or "").strip()
    if not name:
        raise BadKey(name)
    if name.startswith("jvm."):
        spec = _JVM_BY_KEY.get(name[len("jvm."):])
        if spec is None:
            raise BadKey(name)
        return Target(spec)
    if name.startswith("profile."):
        rest = name[len("profile."):]
        profile, sep, key = rest.rpartition(".")
        spec = _PROFILE_BY_KEY.get(key)
        if not sep or not profile or spec is None:
            raise BadKey(name)
        return Target(spec, profile=profile)
    spec = _GLOBAL_BY_KEY.get(name)
    if spec is None:
        raise BadKey(name)
    return Target(spec)


# ------------------------------------------------------------------ 值处理
def parse_value(spec: Spec, raw: str) -> Any:
    """严格解析 ``raw``。不合格一律 ``BadValue`` —— 绝不「退默认」。

    ``--set k=`` 里的空值按**空字符串**处理（``github_mirror=`` 就是「直连」，
    这是唯一一个「空值有意义」的项；其它文本项留空也是允许的）。
    """
    text = (raw or "").strip()
    if spec.kind == "bool":
        word = text.casefold()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise BadValue(t("cli.set_bad_bool", name=spec.key, value=raw))
    if spec.kind == "int":
        try:
            value = int(text)
        except (TypeError, ValueError):
            raise BadValue(
                t("cli.set_bad_int", name=spec.key, lo=spec.lo, hi=spec.hi, value=raw)
            ) from None
        if value < spec.lo or value > spec.hi:
            raise BadValue(
                t("cli.set_bad_int", name=spec.key, lo=spec.lo, hi=spec.hi, value=raw)
            )
        return value
    if spec.kind == "choice":
        for choice in spec.choices:
            if text.casefold() == choice.casefold():
                return choice
        raise BadValue(
            t(
                "cli.set_bad_choice",
                name=spec.key,
                choices=" / ".join(spec.choices),
                value=raw,
            )
        )
    if spec.kind == "mirror":
        if not text:
            return ""
        normalized = normalize_mirror(text)
        if not normalized:
            raise BadValue(t("cli.set_bad_mirror", name=spec.key, value=raw))
        return normalized
    # text：原样收（首尾空白去掉）。空串是合法值（清空额外参数就是留空）。
    return text


def read_value(config: ConfigManager, target: Target) -> Any:
    if target.scope == "jvm":
        return config.get_jvm(target.key)
    if target.scope == "profile":
        return config.get_profile_setting(target.profile, target.key)
    return config.get(target.key)


def write_value(config: ConfigManager, target: Target, value: Any) -> None:
    if target.scope == "jvm":
        config.set_jvm(target.key, value)
    elif target.scope == "profile":
        config.set_profile_setting(target.profile, target.key, value)
    else:
        config.set(target.key, value)


def _default_of(spec: Spec) -> Any:
    if spec.scope == "jvm":
        return ConfigManager.JVM_DEFAULTS.get(spec.key)
    if spec.scope == "profile":
        return ConfigManager.PROFILE_DEFAULTS.get(spec.key)
    return ConfigManager.GLOBAL_DEFAULTS.get(spec.key)


def show(value: Any) -> str:
    """把配置值写成「用户能照着敲回去」的样子。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value if value else '""'
    return str(value)


def _same(left: Any, right: Any) -> bool:
    """类型也要一致：``True == 1`` 在 Python 里成立，但配置里不是一回事。"""
    return type(left) is type(right) and left == right


# ------------------------------------------------------------------- --get
def _line(spec: Spec, value: Any, *, name: str = "", mark_default: bool = True) -> str:
    """一行「键 = 值 # 设置页那句文案」。

    ``name`` 是**带前缀的完整写法**（``jvm.jre_path`` / ``profile.默认.max_backups``）
    —— 输出必须能直接抄回 ``--set``，所以不能只写裸键名。
    """
    note = t(spec.label)
    suffix = (
        t("cli.get_default_mark")
        if mark_default and _same(value, _default_of(spec))
        else ""
    )
    return t(
        "cli.get_line",
        name=name or spec.key,
        value=show(value),
        label=note,
        mark=suffix,
    )


def render_get(config: ConfigManager, only: str = "") -> tuple[int, str]:
    """``--get`` 的正文。返回 ``(退出码, 文本)``。"""
    if only:
        try:
            target = resolve(only)
        except BadKey:
            return EXIT_BAD_USAGE, t(
                "cli.get_unknown", name=only, cmd=command_name()
            )
        if target.scope == "profile" and target.profile not in config.get_profile_names():
            return EXIT_BAD_USAGE, t(
                "cli.set_profile_missing",
                name=target.profile,
                profiles=_profiles_text(config),
            )
        value = read_value(config, target)
        return EXIT_OK, t(
            "cli.get_one",
            name=only.strip(),
            value=show(value),
            label=t(target.spec.label),
        )

    lines = [t("cli.get_header", path=BASE_DIR / "config.json")]
    for spec in GLOBAL_SPECS:
        lines.append(_line(spec, config.get(spec.key)))
    for spec in JVM_SPECS:
        lines.append(
            _line(spec, config.get_jvm(spec.key), name=f"jvm.{spec.key}")
        )
    current = config.get_current_profile()
    if current and current in config.get_profile_names():
        lines.append("")
        lines.append(t("cli.get_profile_header", name=current))
        for spec in PROFILE_SPECS:
            lines.append(
                _line(
                    spec,
                    config.get_profile_setting(current, spec.key),
                    name=f"profile.{current}.{spec.key}",
                )
            )
    lines.append("")
    lines.append(t("cli.get_footer", cmd=command_name()))
    return EXIT_OK, "\n".join(lines)


def _profiles_text(config: ConfigManager) -> str:
    names = config.get_profile_names()
    return " / ".join(names) if names else t("cli.set_no_profiles")


# ------------------------------------------------------------------- --set
def apply_pairs(
    config: ConfigManager, pairs: tuple[str, ...]
) -> list[tuple[str, Target, Any, Any]]:
    """把一批 ``键=值`` 写进**内存**，返回 ``[(键, 目标, 旧值, 新值)]``。

    任何一项不合格都抛异常（调用方据此整批放弃，磁盘上什么都不变）。
    """
    staged: list[tuple[str, Target, Any, Any]] = []
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        if not sep or not name.strip():
            raise BadValue(t("cli.set_bad_pair", token=pair, cmd=command_name()))
        try:
            target = resolve(name)
        except BadKey:
            raise BadValue(
                t("cli.set_unknown", name=name.strip(), cmd=command_name())
            ) from None
        if target.scope == "profile" and target.profile not in config.get_profile_names():
            raise BadProfile(target.profile)
        value = parse_value(target.spec, raw)
        old = read_value(config, target)
        write_value(config, target, value)
        staged.append((name.strip(), target, old, value))
    return staged


def _read_disk() -> dict:
    """把刚写下去的 config.json **重新读一遍**。

    ★ 必须重新读文件，不能看内存：``ConfigManager.save()`` 把 OSError 吞成
      一条日志（它自己不能因为写盘失败就抛，界面那边受不了），所以「到底
      写进去没有」只有磁盘说了算。
    """
    try:
        raw = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _dig(disk: dict, target: Target) -> Any:
    if target.scope == "jvm":
        section = disk.get("jvm")
        return section.get(target.key) if isinstance(section, dict) else None
    if target.scope == "profile":
        profiles = disk.get("profiles")
        if not isinstance(profiles, dict):
            return None
        profile = profiles.get(target.profile)
        return profile.get(target.key) if isinstance(profile, dict) else None
    return disk.get(target.key)


def do_set(pairs: tuple[str, ...]) -> tuple[int, str]:
    """执行 ``--set``：拿锁 → 读 → 改 → 回读 → 落盘 → 再回读。"""
    lock = applock.acquire(BASE_DIR)
    try:
        if not lock.held:
            if lock.reason == "busy":
                # ★ 不是「权限问题」，而是**有启动器开着**：它退出时会用内存里
                #   的旧配置整个写回，这次改动会被静默吃掉（实测复现）。
                return EXIT_CONFIG_FAILED, t("cli.set_locked", path=lock.path)
            return EXIT_CONFIG_FAILED, t(
                "cli.set_lock_unavailable", path=lock.path, err=lock.reason
            )
        config = load_config()
        try:
            staged = apply_pairs(config, pairs)
        except BadProfile as e:
            return EXIT_BAD_USAGE, t(
                "cli.set_profile_missing", name=e.name, profiles=_profiles_text(config)
            )
        except BadValue as e:
            return EXIT_BAD_USAGE, e.text
        except Exception as e:                                  # noqa: BLE001
            logger.error(f"改配置失败：{e}", exc_info=True)
            return EXIT_CONFIG_FAILED, t("cli.set_failed", err=e)

        # ① 回读内存：set() 的整理规则可能把值改成别的（例如补了 https://）。
        for name, target, _old, value in staged:
            if not _same(read_value(config, target), value):
                return EXIT_CONFIG_FAILED, t("cli.set_verify_failed", name=name)

        config.save()

        # ② 回读磁盘：save() 内部吞 OSError，只能这样确认真的落地了。
        disk = _read_disk()
        for name, target, _old, value in staged:
            if not _same(_dig(disk, target), value):
                return EXIT_CONFIG_FAILED, t("cli.set_verify_failed", name=name)

        items = "\n".join(
            t("cli.set_item", name=name, old=show(old), new=show(new))
            for name, _target, old, new in staged
        )
        return EXIT_OK, t("cli.set_done", count=len(staged), items=items)
    finally:
        lock.release()


# --------------------------------------------------------------------- 入口
def run(request: CliRequest, has_console: bool) -> int:
    """执行 ``--get`` 或 ``--set``，返回退出码（见 ``cli.EXIT_*``）。

    只做**一件**事 —— 优先级在 ``cli.parse_cli`` 里就定好了
    （``--get`` 先于 ``--set``，两者都先于导出）。
    """
    config: ConfigManager | None = None
    try:
        config = load_config()
        apply_setting(config.get("language"))
    except Exception as e:                                      # noqa: BLE001
        logger.warning(f"配置读不出来，按系统语言输出：{e}")

    if request.set_pairs:
        code, text = do_set(tuple(request.set_pairs))
    elif config is None:
        code, text = EXIT_CONFIG_FAILED, t("cli.set_failed", err="config.json")
    else:
        code, text = render_get(config, request.get_key)

    say(text, has_console)
    return code
