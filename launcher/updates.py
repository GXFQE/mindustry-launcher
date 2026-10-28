# -*- coding: utf-8 -*-
"""更新检查与运行时下载。"""
import json
import logging
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config import ConfigManager, normalize_mirror
from .ghauth import apply_auth
from .i18n import t
from .storage import VersionManager
from .utils import (
    UNVERIFIED_SSL_CTX,
    download_file,
    download_tmp_path,
    parse_bool,
)
from .version import USER_AGENT

logger = logging.getLogger(__name__)

# 状态回调不是单纯 (str) -> None：GUI 那边给的是
# ``Mixin.set_status(text, log_level=...)``，下载进度那种每秒一条的消息
# 要显式降到 DEBUG 才不至于把日志淹掉。写死成 Callable[[str], None]
# 会让类型标注和实际契约对不上（换个回调就 TypeError）。
StatusCallback = Callable[..., None]


class UpdateCheckError(RuntimeError):
    """检查更新**根本没查成**：所有来源都失败了（断网 / 限流 / 返回格式不对）。

    ★ 和「所有版本都已是最新」是两回事 —— 混在一起，调用方就会把断网读成
      「不用更新」。
    ★ 界面那条路**不用**它：检查更新失败只在状态栏留一句，不该弹框打断；
      只有命令行（``cli_info`` 的 ``--check-updates``）靠它给非 0 退出码。
      （``get_available_updates(strict=True)`` 才会抛。）
    """


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

    def get_available_updates(
        self, strict: bool = False
    ) -> list[dict[str, Any]]:
        """查有哪些新版本（已经在本地的不算）。

        ``strict=True`` 时，**所有来源都失败**会抛 ``UpdateCheckError`` ——
        命令行靠它区分「真的没有更新」和「根本查不通」。界面不用（见
        ``UpdateCheckError`` 的说明）。
        """
        errors: list[str] = []
        try:
            infos: list[dict] = []
            sources = self.version_manager.sources.all()
            # 多个来源要依次请求，网络不通时每个都要等满一个超时。
            # 中途被叫停（用户关窗口）就别再把后面的也等完了。
            for source in sources:
                if self.stop_event.is_set():
                    return []
                try:
                    info = self._fetch_release(source, strict=strict)
                except Exception as e:                          # noqa: BLE001
                    # 单个来源失败不算整体失败：另一个源可能有结果。
                    errors.append(f"{getattr(source, 'type', '?')}: {e}")
                    continue
                if info is not None:
                    infos.append(info)
            # ★ 一个都没查成才算「查不了」。部分失败仍然有结果可用，这时候
            #   报错等于把另一半的成功也一起扔掉。
            if strict and sources and len(errors) == len(sources):
                raise UpdateCheckError("；".join(errors))
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
        except UpdateCheckError:
            raise
        except Exception as e:
            logger.error(f"检查更新失败: {e}")
            if strict:
                raise UpdateCheckError(str(e)) from e
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
            status(t("updates.all_latest"))
            return
        if silent:
            status(t("updater.found_auto"))
            self.start_download_updates(updates, status_cb)

    def _fetch_release(self, source, strict: bool = False) -> dict | None:
        """按来源定义拉一次 release 信息（见 ``sources.VersionSource``）。

        ``source`` 是 ``VersionSource``：取哪个接口、认哪个资产、要不要
        预发布版，全在那张表里 —— 加来源不用改这个方法。

        ``strict=True`` 时，「查不了」一律抛 ``UpdateCheckError``（没有
        api_url / 断网 / 超时 / 返回的不是数组）；「查到了但没有匹配的资产」
        仍然返回 ``None`` —— 那是一次**成功**的查询，只是这个源没有新版本。
        """
        url = getattr(source, "api_url", "") or ""
        if not url:
            logger.warning(f"来源 {getattr(source, 'type', '?')} 没有 api_url，跳过")
            if strict:
                # ★ 异常正文只放**技术细节**（来源类型），中文那半句在
                #   cli_info 的 t("cli.check_updates_failed") 里 —— 这里再写
                #   一句中文就成了「翻译不了的界面文案」，i18n 门禁也会拦。
                raise UpdateCheckError(
                    f"source {getattr(source, 'type', '?')}: no api_url"
                )
            return None
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT}
            )
            # 装了 gh 且已登录时带上认证（额度 60→5000 次/时）。白名单只放
            # GitHub 官方域名 —— 用户自定义的第三方来源拿不到令牌。
            apply_auth(req)
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
                if strict:
                    # 同上：中文只进日志，异常正文留给 cli_info 包一层翻译。
                    raise UpdateCheckError(
                        f"{url}: not a release array (API rate limit?)"
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
        except (
            urllib.error.URLError,
            OSError,            # 超时是 OSError/SocketTimeout，不是 URLError
            json.JSONDecodeError,
            ValueError,
            KeyError,
        ) as e:
            logger.error(f"获取 {url} 发布信息失败: {e}")
            if strict:
                raise UpdateCheckError(f"{url}：{e}") from e
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
                        t("updater.downloading",
                          type=info["type"], version=info["version"])
                    )
                logger.info(f"开始下载 {info['type']} {info['version']}")
                success = self._download_version(info, status_cb)
                if not success:
                    if status_cb:
                        status_cb(
                            t("updater.download_failed",
                              type=info["type"], version=info["version"])
                        )
                    logger.error(f"下载 {info['type']} {info['version']} 失败")
                    return
                else:
                    logger.info(
                        f"下载并导入 {info['type']} {info['version']} 成功"
                    )
            if status_cb:
                status_cb(t("updater.downloads_done"))
        except Exception as e:
            logger.error(f"下载序列异常: {e}")
            if status_cb:
                status_cb(t("updater.process_error", err=e))
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
        # ★ 断点文件用**确定性路径**（以原始下载地址为身份）：镜像失败换
        #   直连、甚至关掉启动器下次再点，都能接着已下载的部分往下下。
        #   （原先是每次 mkstemp 一个随机名，等于每轮都从零开始。）
        tmp_path = download_tmp_path(
            info["download_url"], suffix=".jar.downloading"
        )
        expect_size = int(info.get("size") or 0)
        final_path = None
        try:
            for url in urls:
                if self.stop_event.is_set():
                    return False

                def progress(pct: float) -> None:
                    if status_cb:
                        status_cb(
                            t("updater.download_progress",
                              type=info["type"], version=info["version"],
                              pct=pct),
                            log_level=logging.DEBUG,
                        )

                ok = download_file(
                    url,
                    tmp_path,
                    self.stop_event,
                    on_progress=progress,
                    expected_sha256=info.get("sha256"),
                    expected_size=expect_size,
                    resume=True,
                )
                if not ok:
                    # ★ 不删已下载的部分：下一个源（或用户下次重试）接着下
                    logger.warning(f"从 {url} 下载失败，尝试下一个源（保留断点）")
                    continue
                actual_size = tmp_path.stat().st_size
                if expect_size > 0 and actual_size != expect_size:
                    # ★ 先取大小再删文件。以前这里是**删完才 stat()**，
                    #   于是这条分支必然抛 FileNotFoundError，被下面的
                    #   except 吞成「下载过程中异常」—— 结果是
                    #   「镜像下到一半的残缺文件」不会去重试下一个源，
                    #   用户看到的是「下载失败」，其实是这里自己绊倒的。
                    #   大小对不上 = 这份数据没救（续不动），删掉重来。
                    tmp_path.unlink(missing_ok=True)
                    logger.warning(
                        f"文件大小不匹配，预期 {info['size']}，实际 {actual_size}"
                    )
                    continue
                if not zipfile.is_zipfile(tmp_path):
                    tmp_path.unlink(missing_ok=True)
                    logger.warning("下载的文件不是有效的 ZIP/JAR")
                    continue
                # 断点文件的名字是 ``xxx.jar.downloading``：用
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
            # ★ 失败时**不再**删断点文件（下一源 / 下次重试要用；真正没救的
            #   情况 —— 大小不符、非 ZIP、校验不过 —— 在上面各自的分支里删）。
            if final_path and final_path.exists():
                final_path.unlink(missing_ok=True)
