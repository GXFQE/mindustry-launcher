# -*- coding: utf-8 -*-
"""更新检查与运行时下载。"""
import json
import logging
import os
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import ConfigManager, normalize_mirror
from .storage import VersionManager
from .utils import UNVERIFIED_SSL_CTX, download_file, parse_bool
from .version import USER_AGENT

logger = logging.getLogger(__name__)

# 状态回调不是单纯 (str) -> None：GUI 那边给的是
# ``Mixin.set_status(text, log_level=...)``，下载进度那种每秒一条的消息
# 要显式降到 DEBUG 才不至于把日志淹掉。写死成 Callable[[str], None]
# 会让类型标注和实际契约对不上（换个回调就 TypeError）。
StatusCallback = Callable[..., None]


class UpdateManager:
    def __init__(
        self,
        version_manager: VersionManager,
        config: ConfigManager,
        executor: ThreadPoolExecutor,
        stop_event: threading.Event,
    ) -> None:
        self.version_manager = version_manager
        self.config = config
        self.executor = executor
        self.stop_event = stop_event
        # 「正在下载」的标志。以前 check_updates 用的是 self.current_process
        # 来判断，结果游戏运行时点「检查更新」会弹「下载正在进行」。
        self.downloading = threading.Event()

    def get_available_updates(self) -> list[dict[str, Any]]:
        try:
            infos: list[dict] = []
            # 多个来源要依次请求，网络不通时每个都要等满一个超时。
            # 中途被叫停（用户关窗口）就别再把后面的也等完了。
            for source in self.version_manager.sources.all():
                if self.stop_event.is_set():
                    return []
                info = self._fetch_release(source)
                if info is not None:
                    infos.append(info)
            existing = {
                (v["type"], v["raw_version"])
                for v in self.version_manager.get_versions()
            }
            available = [
                info
                for info in infos
                if (info["type"], info["version"]) not in existing
            ]
            logger.debug(f"发现 {len(available)} 个可用更新")
            return available
        except Exception as e:
            logger.error(f"检查更新失败: {e}")
            return []

    def start_download_updates(
        self,
        updates: list[dict[str, Any]],
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.downloading.set()
        try:
            self.executor.submit(self._download_sequence, updates, status_callback)
        except RuntimeError as e:
            # 线程池已经在关（on_closing 的 shutdown）—— 那就不下载了，
            # 但「正在下载」的牌子必须摘掉，否则退出流程会一直以为还有
            # 下载在跑（close_on_game_exit 会因此不敢关启动器）。
            self.downloading.clear()
            logger.warning(f"无法开始下载（线程池已关闭）: {e}")

    def check_for_updates(
        self,
        silent: bool = False,
        status_callback: StatusCallback | None = None,
    ) -> None:
        """检查更新；silent 模式下发现新版本就直接下载。

        ⚠️ 本方法是**同步**的，内部会发网络请求、可能一直阻塞到超时。
        调用方必须把它放进 daemon 线程里跑，不要丢给 self.executor：
        executor 的线程是非 daemon 的，解释器退出时会 join 它们，
        于是关窗口要一直等到网络超时才真正结束（实测拖了十几秒）。
        """
        self._check_updates(silent, status_callback)

    def _check_updates(
        self, silent: bool, status_cb: StatusCallback | None = None
    ) -> None:
        def status(msg: str) -> None:
            if status_cb:
                status_cb(msg)

        updates = self.get_available_updates()
        if not updates:
            status("✅ 所有版本已是最新")
            return
        if silent:
            status("📥 发现新版本，开始自动下载...")
            self.start_download_updates(updates, status_cb)

    def _fetch_release(self, source) -> dict | None:
        """按来源定义拉一次 release 信息（见 sources.VersionSource）。

        ``source`` 是 ``VersionSource``：取哪个接口、认哪个资产、要不要
        预发布版，全在那张表里 —— 加来源不用改这个方法。
        """
        url = getattr(source, "api_url", "") or ""
        if not url:
            logger.warning(f"来源 {getattr(source, 'type', '?')} 没有 api_url，跳过")
            return None
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT}
            )
            # 超时别给太长：这里卡住的每一秒都会变成「点了检查更新没反应」
            # 或者「关窗口关不掉」。GitHub API 正常一两秒就回。
            with urllib.request.urlopen(
                req, timeout=5, context=UNVERIFIED_SSL_CTX
            ) as resp:
                data = json.loads(resp.read().decode())
            if not isinstance(data, list):
                # GitHub 出错时返回的是个对象（rate limit 之类），不是数组。
                # 不判一下的话下面 for 会把 key 当 release 遍历一遍。
                logger.warning(
                    f"{url} 返回的不是 release 数组（可能是 API 限流）: "
                    f"{str(data)[:120]}"
                )
                return None
            want_prerelease = getattr(source, "prerelease", None)
            for release in data:
                if not isinstance(release, dict):
                    continue
                if (
                    want_prerelease is not None
                    and release.get("prerelease", False) != want_prerelease
                ):
                    continue
                for asset in release.get("assets", []):
                    if not isinstance(asset, dict):
                        continue
                    name = asset.get("name", "")
                    if not source.matches_asset(name):
                        continue
                    tag = str(release.get("tag_name", ""))
                    raw_digest = asset.get("digest", "") or ""
                    expected_sha = (
                        raw_digest[7:]
                        if raw_digest.startswith("sha256:")
                        else ""
                    )
                    info = {
                        "type": source.type,
                        "version": tag.lstrip("v").strip(),
                        "download_url": asset.get("browser_download_url", ""),
                        "size": asset.get("size", 0),
                        "sha256": expected_sha,
                        # 非 GitHub 的源不能拼镜像前缀（见 sources.VersionSource）
                        "use_mirror": parse_bool(
                            getattr(source, "use_mirror", True), True,
                            what=f"来源 {source.type} 的 use_mirror",
                        ),
                    }
                    if not info["download_url"]:
                        continue
                    logger.debug(f"获取到发布信息: {info}")
                    return info
        except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
            logger.error(f"获取 {url} 发布信息失败: {e}")
        return None

    def _download_sequence(
        self,
        updates: list[dict[str, Any]],
        status_cb: StatusCallback | None = None,
    ) -> None:
        try:
            for info in updates:
                if self.stop_event.is_set():
                    break
                if status_cb:
                    status_cb(
                        f"📥 正在下载 {info['type']} {info['version']}..."
                    )
                logger.info(f"开始下载 {info['type']} {info['version']}")
                success = self._download_version(info, status_cb)
                if not success:
                    if status_cb:
                        status_cb(
                            f"❌ {info['type']} {info['version']} 下载失败"
                        )
                    logger.error(f"下载 {info['type']} {info['version']} 失败")
                    return
                else:
                    logger.info(
                        f"下载并导入 {info['type']} {info['version']} 成功"
                    )
            if status_cb:
                status_cb("✅ 所有更新已下载完成")
        except Exception as e:
            logger.error(f"下载序列异常: {e}")
            if status_cb:
                status_cb(f"❌ 更新过程出错: {e}")
        finally:
            # 无论怎么结束都要解锁，否则「检查更新」会被永久挡住
            self.downloading.clear()

    def _download_version(
        self, info: dict, status_cb: StatusCallback | None = None
    ) -> bool:
        urls = []
        # 镜像前缀拼在完整地址前（见 config.normalize_mirror）。填错了也没关系：
        # 拉不动就自动换下一个源，最后一定是直连 GitHub。
        # ★ 只有「本来就在 GitHub 上」的来源才拼镜像（info["use_mirror"]）——
        #   给别的源硬拼前缀只会白白多一次注定失败的尝试（见 sources.py）。
        mirror = normalize_mirror(self.config.get("github_mirror"))
        if mirror and info.get("use_mirror", True):
            urls.append(mirror + info["download_url"])
        urls.append(info["download_url"])
        tmp_path = None
        final_path = None
        try:
            for url in urls:
                if self.stop_event.is_set():
                    return False
                fd, tmp_path = tempfile.mkstemp(suffix=".jar.downloading")
                tmp_path = Path(tmp_path)
                os.close(fd)

                def progress(pct: float) -> None:
                    if status_cb:
                        status_cb(
                            f"📥 下载 {info['type']} {info['version']}: {pct:.1f}%",
                            log_level=logging.DEBUG,
                        )

                ok = download_file(
                    url,
                    tmp_path,
                    self.stop_event,
                    on_progress=progress,
                    expected_sha256=info.get("sha256"),
                )
                if not ok:
                    tmp_path.unlink(missing_ok=True)
                    logger.warning(f"从 {url} 下载失败，尝试下一个源")
                    continue
                actual_size = tmp_path.stat().st_size
                if info.get("size", 0) > 0 and actual_size != info["size"]:
                    # ★ 先取大小再删文件。以前这里是**删完才 stat()**，
                    #   于是这条分支必然抛 FileNotFoundError，被下面的
                    #   except 吞成「下载过程中异常」—— 结果是
                    #   「镜像下到一半的残缺文件」不会去重试下一个源，
                    #   用户看到的是「下载失败」，其实是这里自己绊倒的。
                    tmp_path.unlink(missing_ok=True)
                    logger.warning(
                        f"文件大小不匹配，预期 {info['size']}，实际 {actual_size}"
                    )
                    continue
                if not zipfile.is_zipfile(tmp_path):
                    tmp_path.unlink(missing_ok=True)
                    logger.warning("下载的文件不是有效的 ZIP/JAR")
                    continue
                # mkstemp 给的名字是 ``xxx.jar.downloading``：用
                # with_suffix(".jar") 会得到 ``xxx.jar.jar``（难看但能用），
                # 这里直接把那个后缀切掉。
                final_path = tmp_path.with_name(
                    tmp_path.name[: -len(".downloading")]
                )
                tmp_path.rename(final_path)
                self.version_manager.add_version_from_jar(
                    final_path, info["version"], info["type"]
                )
                return True
            return False
        except Exception as e:
            logger.error(f"下载过程中异常: {e}")
            return False
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            if final_path and final_path.exists():
                final_path.unlink(missing_ok=True)
