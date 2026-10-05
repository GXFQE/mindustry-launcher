# -*- coding: utf-8 -*-
"""界面文案国际化（i18n）。

为什么要有它
------------
i18n 是**横切关注点** —— 每一条界面文案都要经过它。功能越多、文案越多，
回头补就越贵（要在一堆已经写死的模块里逐个往外拆）。所以它在功能面还收得住
的时候做掉最便宜，这个模块就是那个地基。

三条设计决定
------------
1. **语言包是 JSON，放 ``lang/<code>.json``，走 ``resource_path()``。**
   那条路是「先找 exe 旁边的外部副本，再回退包内」⇒ 把一份同名文件放到 exe
   旁边就能替换翻译，**不必重新打包**（和 ``jre/`` 一个路子，见 utils）。

2. **取词 ``t("a.b.key", **params)``，找不到就逐级回退。**
   回退链：当前语言 → ``LANGUAGE_DEFAULT`` → 直接返回 key 本身并 WARNING。
   ★ **绝不抛异常、绝不返回空串** —— 少一条文案不该让界面崩掉；显示成
   ``main.launch`` 比显示成空白更容易被发现，也让问题自带定位信息。

3. ★ **测试可以用环境变量 ``MDT_LANG`` 把语言钉死。**
   这条是硬要求，不是方便：验证脚本里有 51 处断言直接比中文文案
   （``"正在检测" in app.jre_check_var.get()`` 这种）。不钉死的话，用户把
   界面切成英文后跑冒烟就会红一片 —— 而那**不是回归**，是断言自己没解耦。
   与 ``MDT_LOG_FILE`` 是同一个套路（见 utils.resolve_log_file）。

★ 文案**不要**放在模块级常量里（``LABELS = {"a": "自动"}`` 这种）：
   那是 import 时求值、一次就冻住，之后切语言它不会变。一律放进函数体，
   运行时调 ``t()``。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from .utils import (
    LANGUAGE_AUTO,
    LANGUAGE_CODES,
    LANGUAGE_DEFAULT,
    normalize_language,
    resource_path,
)

logger = logging.getLogger(__name__)

# 语言包所在目录（相对数据根 / 资源根，见 utils.resource_path）。
LANG_SUBDIR = "lang"

# 钉死界面语言的环境变量（测试/冒烟用，见模块 docstring 第 3 条）。
LANG_ENV = "MDT_LANG"

# 当前生效语言、它的语言包、以及兜底语言包。
_current: str = LANGUAGE_DEFAULT
_pack: dict[str, str] = {}
_fallback: dict[str, str] = {}
# 语言是否被 MDT_LANG 钉死（钉死后 apply_setting 不许覆盖它）。
_forced = False
# 已报过的问题（同一个 key 缺一次就够了，别在一次界面刷新里喊几十遍）。
_warned: set[str] = set()


# ---------- 语言包读写 ----------

def _pack_path(code: str):
    """语言包路径。外部（exe 旁边）优先，回退包内 —— 由 resource_path 决定。"""
    return resource_path(f"{LANG_SUBDIR}/{code}.json")


def _load_pack(code: str) -> dict[str, str]:
    """读一份语言包。**不抛异常** —— 读不到就当空的，取词时自然走回退链。"""
    path = _pack_path(code)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(f"语言包缺失：{path}")
        return {}
    except (OSError, ValueError) as e:          # UnicodeDecodeError ⊂ ValueError
        logger.error(f"语言包读不出来 {path}：{e}")
        return {}
    if not isinstance(raw, dict):
        logger.error(f"语言包格式不对（顶层应该是一个对象）：{path}")
        return {}
    # 下划线开头的是**元数据**（_meta 里的说明、语言名之类），不是文案 ——
    # 它们本来就不是字符串，别当成"译者写坏了"来报警。
    items = {k: v for k, v in raw.items()
             if isinstance(k, str) and not k.startswith("_")}
    # 只收「键和值都是字符串」的条目：译者手滑写成数组或嵌套对象时，
    # 别把那种东西塞进界面（t() 会把它 format 进标签里）。
    out = {k: v for k, v in items.items() if isinstance(v, str)}
    dropped = len(items) - len(out)
    if dropped:
        logger.warning(f"语言包 {path} 里有 {dropped} 条的值不是字符串，已忽略")
    return out


def _note_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        logger.warning(msg)


# ---------- 对外 API ----------

def set_language(code: str) -> str:
    """装载一门语言并返回**实际生效**的 code。

    认不出的 code 退回 ``LANGUAGE_DEFAULT``（并点名）—— 与配置层同一条
    容错约定：不猜，但也不炸。
    """
    global _current, _pack, _fallback
    if code not in LANGUAGE_CODES:
        logger.warning(f"语言 {code!r} 不受支持，改用 {LANGUAGE_DEFAULT}")
        code = LANGUAGE_DEFAULT
    _current = code
    _pack = _load_pack(code)
    _fallback = _pack if code == LANGUAGE_DEFAULT else _load_pack(LANGUAGE_DEFAULT)
    _warned.clear()
    logger.info(f"界面语言 = {code}（{len(_pack)} 条文案）")
    return code


def current_language() -> str:
    """当前生效的语言 code（``auto`` 不会出现在这里，它已经被解析掉了）。"""
    return _current


def language_is_forced() -> bool:
    """语言是否被 ``MDT_LANG`` 钉死（验证脚本会问这个来确认隔离生效）。"""
    return _forced


def t(key: str, **params: Any) -> str:
    """取一条界面文案。**不抛异常、不返回空串。**

    ``params`` 喂给 ``str.format()``，所以文案里写 ``{name}`` 这种占位符。
    译文里的占位符集合必须和原始文案一致 —— 由 ``_tools/verify/i18n_check.py``
    盯着（少一个参数是运行时才发现的，那时已经晚了）。
    """
    text = _pack.get(key)
    if text is None:
        text = _fallback.get(key)
        if text is not None:
            _note_once(
                f"语言包 {_current} 缺文案 {key!r}，已回退到 {LANGUAGE_DEFAULT}"
            )
    if text is None:
        _note_once(f"没有任何语言包包含文案 {key!r}（界面会显示这个 key）")
        return key
    if not params:
        return text
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError) as e:
        # 占位符对不上是「改文案/翻译时」的错，不该让用户看到异常弹窗。
        _note_once(f"文案 {key!r} 的占位符对不上（{e}），已按原文显示")
        return text


# ---------- 语言解析 ----------

def _system_primary_language() -> str | None:
    """系统的**主语言**（``"zh"`` / ``"en"`` …）。拿不到返回 None。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from locale import windows_locale
            # ★ 用 GetUserDefaultUILanguage 而不是 locale.getlocale()：前者给的是
            #   **界面语言**，后者反映的是**区域设置**。一个"系统语言英文、区域
            #   留在中国"的人，locale 会说 zh_CN，可他的界面其实是英文。
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            tag = windows_locale.get(langid)
            if tag:
                return tag.split("_")[0].lower()
        except Exception as e:                              # noqa: BLE001
            logger.debug(f"取系统界面语言失败（{e}），改用 locale 兜底")
    try:
        import locale
        tag = locale.getlocale()[0] or locale.getdefaultlocale()[0]
        if tag:
            return str(tag).split("_")[0].lower()
    except Exception as e:                                  # noqa: BLE001
        logger.debug(f"locale 也拿不到语言：{e}")
    return None


def _system_language() -> str:
    """系统语言 → 我们支持的 code 之一；认不出就 ``LANGUAGE_DEFAULT``。"""
    primary = _system_primary_language()
    if primary:
        for code in LANGUAGE_CODES:
            if code.split("_")[0].lower() == primary:
                return code
    return LANGUAGE_DEFAULT


def apply_setting(raw: Any) -> str:
    """按配置里的 ``language`` 装载语言包（``auto`` 时探测系统语言）。

    启动时调一次；用户在设置页改完语言再调一次。

    ★ 若语言被 ``MDT_LANG`` 钉死，这里**什么都不做** —— 那是测试与用户设置
      的解耦开关，用户配置不许越过它（否则用户在 config.json 里存了 en_US，
      验证脚本就会跟着变英文而误报）。
    """
    if _forced:
        return _current
    code = normalize_language(raw)
    if code == LANGUAGE_AUTO:
        code = _system_language()
    return set_language(code)


def _bootstrap() -> None:
    """import 时先按「环境变量 → 系统语言」装载，让界面开箱可用。

    之后 ``apply_setting(用户配置)`` 会覆盖它（除非被钉死）。这样即使有谁
    忘了调 apply_setting，界面也是跟随系统语言的，而不是满屏 key。
    """
    global _forced
    forced = os.environ.get(LANG_ENV, "").strip()
    if forced:
        if forced in LANGUAGE_CODES:
            _forced = True
            logger.info(f"界面语言被 {LANG_ENV}={forced} 钉死（测试/冒烟用）")
            set_language(forced)
            return
        logger.warning(f"{LANG_ENV}={forced!r} 不是受支持的语言，已忽略")
    set_language(_system_language())


_bootstrap()
