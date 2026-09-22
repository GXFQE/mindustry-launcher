# -*- coding: utf-8 -*-
"""版本来源（release source）注册表 —— 想加一个「从哪儿下游戏版本」不用改核心代码。

背景：原先「有哪些来源」是**写死在代码里**的两处 ——
``VersionManager.get_versions`` 里 ``if vtype == "MindustryX"`` 的特判，
和 ``UpdateManager`` 里那两个 ``_fetch_*_release`` 方法。加第三个来源
（比如官方每日构建 ``Anuken/MindustryBuilds``，或者别的分支）要同时改
四处：取版本号、排序、检查更新、下载判定。

现在收敛成一张表：

    VersionSource   —— 一个来源怎么取、怎么认资产、怎么把版本号排序
    DEFAULT_SOURCES —— 内置的两个（行为与加这张表之前**完全一致**）
    load_sources(cfg) —— 内置 + config.json 里 ``version_sources`` 的自定义项

★ 兼容性约定（发布之后要守住）：``VersionSource`` 的字段**只增不改**；
新增字段必须带默认值；``load_sources`` 遇到不认识的键一律忽略而不是报错，
这样「新版本写的 config.json」在旧版本里也能读。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

from .utils import parse_bool

logger = logging.getLogger(__name__)

# 认不出来的版本类型排在这里（数值越大越靠后）。1 是老代码里
# 「非 MindustryX」的默认档位，保持不变 —— 用户自己导入过稀奇类型的话，
# 列表顺序不会因为这次重构而变。
DEFAULT_SORT_RANK = 1


@dataclass(frozen=True)
class VersionSource:
    """一个游戏版本来源。

    ``api_url`` 指向 GitHub 的 releases 接口（也兼容任何返回同结构 JSON
    的地址）。``asset_pattern`` 是**资产名**上的正则：命中第一个就是这一版
    的下载地址。
    """

    type: str                       # 版本类型名，会写进清单的 "type" 字段
    api_url: str
    # 资产名正则（str）或直接给一个可调用对象（内置来源用 lambda 更直白）
    asset_pattern: Any = ""
    # None = 正式版和预发布都要；True/False = 只要预发布 / 只要正式版
    prerelease: bool | None = None
    # 下载是否走「GitHub 镜像前缀」。非 GitHub 的源要设 False，
    # 否则镜像前缀会被拼到一个不相干的地址前面，白白多一次失败尝试。
    use_mirror: bool = True
    # 排序档位（小的在前）；同一档内按版本号从新到旧
    sort_rank: int = DEFAULT_SORT_RANK
    # 把 raw_version 切成可比较数字元组的正则
    version_regex: str = r"\d+"
    # 取第几个捕获组；None = 取所有数字
    version_group: int | None = None
    # 给未来的扩展字段留的位置（不影响相等性）
    extra: dict = field(default_factory=dict)

    def matches_asset(self, name: str) -> bool:
        """这个资产名是不是我们要的那个文件。"""
        if callable(self.asset_pattern):
            try:
                return bool(self.asset_pattern(name))
            except Exception as e:                              # noqa: BLE001
                logger.warning(f"来源 {self.type} 的资产匹配器出错: {e}")
                return False
        if not self.asset_pattern:
            return False
        try:
            return re.search(str(self.asset_pattern), name) is not None
        except re.error as e:
            logger.warning(f"来源 {self.type} 的 asset_pattern 非法: {e}")
            return False

    def parse_version(self, raw: str) -> tuple[int, ...]:
        """把版本号切成可比元组。切不出数字就给 ``(0,)``（排最后）。"""
        try:
            if self.version_group is None:
                nums = tuple(int(x) for x in re.findall(self.version_regex, raw))
            else:
                found = re.search(self.version_regex, raw)
                nums = (int(found.group(self.version_group)),) if found else ()
        except (re.error, ValueError, IndexError) as e:
            logger.warning(f"来源 {self.type} 的 version_regex 用不了: {e}")
            nums = ()
        return nums or (0,)


# ---------------------------------------------------------------------------
# 内置来源。改这里等于改「默认能从哪几个地方下游戏」。
# ★ 顺序/档位与加这张表之前保持一致：MindustryX 在前（rank 0），
#   Mindustry 在后（rank 1）。
# ---------------------------------------------------------------------------
DEFAULT_SOURCES: tuple[VersionSource, ...] = (
    VersionSource(
        type="Mindustry",
        api_url="https://api.github.com/repos/Anuken/Mindustry/releases",
        asset_pattern=lambda name: name == "Mindustry.jar",
        prerelease=None,             # 官方仓库的预发布也收（保持原行为）
        sort_rank=1,
        version_regex=r"\d+",
    ),
    VersionSource(
        type="MindustryX",
        api_url="https://api.github.com/repos/TinyLake/MindustryX/releases",
        asset_pattern=r"Desktop\.jar$",
        prerelease=False,            # 只认正式版（预发布的不稳定，原行为如此）
        sort_rank=0,
        version_regex=r"[Xx](\d+)",
        version_group=1,
    ),
)

# config.json 的 ``version_sources`` 里允许写的键（其它键忽略 + 记一条日志）
_SOURCE_KEYS = {
    "type", "api_url", "asset_pattern", "prerelease", "use_mirror",
    "sort_rank", "version_regex", "version_group",
}


class SourceRegistry:
    """按「版本类型名」查来源；查不到就按通用规则处理（不报错）。"""

    def __init__(self, sources: list[VersionSource] | None = None) -> None:
        self._sources: list[VersionSource] = list(
            DEFAULT_SOURCES if sources is None else sources
        )

    # -- 读 --

    def all(self) -> list[VersionSource]:
        """全部来源（按档位排好，决定界面上「发现新版本」的顺序）。"""
        return sorted(self._sources, key=lambda s: (s.sort_rank, s.type))

    def names(self) -> list[str]:
        return [s.type for s in self.all()]

    def get(self, vtype: str) -> VersionSource | None:
        for s in self._sources:
            if s.type == vtype:
                return s
        return None

    # -- 归类（清单里那个 type 该怎么解析、排多前）--

    def rank_of(self, vtype: str) -> int:
        src = self.get(vtype)
        return src.sort_rank if src is not None else DEFAULT_SORT_RANK

    def parse_version(self, vtype: str, raw: str) -> tuple[int, ...]:
        src = self.get(vtype)
        if src is None:
            # 没登记的类型（用户手写的版本号、以后的第三方构建）：
            # 按「里面出现的所有数字」比大小 —— 跟老代码一个规则
            return VersionSource(type=vtype, api_url="").parse_version(raw)
        return src.parse_version(raw)


def normalize_version_sources(raw: Any) -> list[dict]:
    """整理 config.json 的 ``version_sources``（手改配置的容错层）。

    * 写成单个对象也认（当一项）；
    * 每项必须有 ``type`` 和 ``api_url``，缺了 / 不是字符串就丢掉；
    * ``asset_pattern`` 先编译一次，正则写坏了就丢掉（留着只会在
      检查更新时静默什么都匹配不到）；
    * ``sort_rank`` / ``version_group`` 必须是整数；
    * 不认识的键忽略（前向兼容：新版本写的配置在旧版本里照样能读）。
    """
    if isinstance(raw, dict):
        raw = [raw]
    out: list[dict] = []
    if not isinstance(raw, (list, tuple)):
        if raw is not None:
            logger.warning(f"version_sources 不是数组，已忽略: {raw!r}")
        return out
    for item in raw:
        if not isinstance(item, dict):
            logger.warning(f"忽略无效的版本来源（不是对象）: {item!r}")
            continue
        vtype = item.get("type")
        url = item.get("api_url")
        if not isinstance(vtype, str) or not vtype.strip():
            logger.warning(f"忽略无效的版本来源（缺 type）: {item!r}")
            continue
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            logger.warning(f"忽略无效的版本来源（api_url 不像地址）: {item!r}")
            continue
        clean: dict[str, Any] = {"type": vtype.strip(), "api_url": url.strip()}
        pattern = item.get("asset_pattern")
        if isinstance(pattern, str) and pattern:
            try:
                re.compile(pattern)
            except re.error as e:
                logger.warning(f"忽略无效的版本来源（asset_pattern 非法）: {e}")
                continue
            clean["asset_pattern"] = pattern
        if "prerelease" in item:
            value = item["prerelease"]
            if value is None:
                # null = 正式版和预发布都要（约定，不是「没填」）
                clean["prerelease"] = None
            else:
                clean["prerelease"] = parse_bool(
                    value, True, what=f"来源 {vtype} 的 prerelease"
                )
        if "use_mirror" in item:
            # ★ 不能用 bool(value)：bool("false") 是 True —— 手改配置写
            #   "false" 会把「不走镜像」反过来，白白多一次注定失败的尝试。
            clean["use_mirror"] = parse_bool(
                item["use_mirror"], True, what=f"来源 {vtype} 的 use_mirror"
            )
        for key, default in (("sort_rank", DEFAULT_SORT_RANK),
                             ("version_group", None)):
            if key not in item:
                continue
            value = item[key]
            if value is None and default is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                logger.warning(f"版本来源的 {key} 不是整数，已忽略: {value!r}")
                continue
            clean[key] = value
        if isinstance(item.get("version_regex"), str) and item["version_regex"]:
            try:
                re.compile(item["version_regex"])
            except re.error as e:
                logger.warning(f"忽略无效的 version_regex: {e}")
            else:
                clean["version_regex"] = item["version_regex"]
        unknown = set(item) - _SOURCE_KEYS
        if unknown:
            # 前向兼容：不认识的键留给「更新的版本」用，这里不写回文件，
            # 但也别当错误 —— 这正是「新配置能被旧代码读」的样子。
            logger.info(f"版本来源 {vtype} 有本版本不认识的键: {sorted(unknown)}")
        out.append(clean)
    return out


def build_source(raw: dict) -> VersionSource:
    """把一份已经整理过的 dict 变成 VersionSource。"""
    fields = {k: v for k, v in raw.items() if k in _SOURCE_KEYS}
    return VersionSource(**fields)


def load_sources(config: Any | None = None) -> SourceRegistry:
    """内置来源 + config.json 里的自定义来源（同名的自定义项覆盖内置）。

    传 None 就只给内置的两个 —— 测试和工具脚本可以不带配置直接用。
    """
    sources = list(DEFAULT_SOURCES)
    if config is not None:
        try:
            extra = config.get("version_sources") or []
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"读 version_sources 失败，只用内置来源: {e}")
            extra = []
        for raw in extra:
            if not isinstance(raw, dict) or not raw.get("type"):
                continue
            try:
                src = build_source(raw)
            except TypeError as e:
                logger.warning(f"版本来源字段不合法，已忽略: {e}")
                continue
            for i, builtin in enumerate(sources):
                if builtin.type == src.type:
                    # 覆盖内置那一项：默认值补上，只让用户改他要改的字段
                    sources[i] = replace(
                        builtin,
                        **{k: v for k, v in raw.items()
                           if k in _SOURCE_KEYS and k != "type"},
                    )
                    logger.info(f"版本来源 {src.type} 已被配置覆盖")
                    break
            else:
                sources.append(src)
                logger.info(f"新增版本来源 {src.type} -> {src.api_url}")
    return SourceRegistry(sources)
