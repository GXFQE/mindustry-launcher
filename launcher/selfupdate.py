# -*- coding: utf-8 -*-
"""启动器**自身**的更新：检查 / 下载 / 应用。

跟 ``updates.py`` 的分工要先说清楚：那边管的是「**游戏**版本」（从 Anuken
的仓库下 ``Mindustry.jar``），这边管的是「**启动器**本身」。两者看着像，
其实没有可复用的地方，所以刻意**没**去套 ``sources.VersionSource`` 那张表：

  * 游戏版本来源可以有任意多个，用户还能自己往 config.json 里加；
  * 启动器的来源只有一个（就是本项目自己的 Release），是编译期确定的，
    没有「注册表」的必要。

三段式：

    ① 检查   查 ``releases/latest``，把 tag 跟 ``version.__version__`` 比
    ② 下载   取 ``*-update.zip``（**只含相对上一版真正变动的文件**），
             校验 sha256，解到程序目录下的暂存区
    ③ 应用   以 ``--apply-update <plan.json>`` 二次启动一个 exe 副本当执行体，
             等旧进程退出后按计划替换，失败回滚

为什么更新包只带变动文件：``_internal/`` 有 993 个文件、约 29 MB，而它
**只在改依赖/PyInstaller/Python 版本时才变** —— 平时一次发版变的只有
那个 2.6 MB 的 exe（压缩后 1 MB 上下）。每次都拖着 29 MB 下来，在国内
网络下是几分钟的差别，所以差异由发布侧算好（见 ``_tools/make_release_zip.py``），
本模块只管照着清单替换。

★ 铁律：**只动程序文件**（exe 与 ``_internal/``）。``config.json``、
  ``versions/``、``backups/``、游戏存档**一个字节都不碰** —— 用户数据
  只增不毁，这是发布之后的第一约束。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import normalize_mirror
from .utils import (
    LAUNCHER_UPDATE_MODES,
    UNVERIFIED_SSL_CTX,
    atomic_write_json,
    download_file,
    normalize_launcher_update,
)
from .version import APP_ID, USER_AGENT, __version__

logger = logging.getLogger(__name__)

# ---- 常量 ---------------------------------------------------------------

# 从哪儿查「启动器有没有新版」。写死是故意的：这个地址不属于用户配置的
# 「游戏版本来源」，改它等于换了一个发布方。要换仓库改这一行。
SELF_UPDATE_API = (
    "https://api.github.com/repos/GXFQE/mindustry-launcher/releases/latest"
)

# 只认这个后缀的资产：精简更新包（发布脚本产出）。
# 故意**不**回退到完整包 —— 那个包的结构是「Mindustry启动器/子目录」，
# 解压规则完全不同，硬认会写错地方。没有更新包时按「只能手动更新」处理。
UPDATE_ASSET_SUFFIX = "-update.zip"

# 更新包里的描述文件（相对 zip 根）。
UPDATE_MANIFEST = "update.json"
# 描述文件里 ``files`` 项的前缀目录（zip 内）。
UPDATE_FILES_PREFIX = "files/"

# 描述文件的格式版本。将来结构变了就 +1，本模块按版本决定怎么读。
UPDATE_FORMAT = 1

# 程序目录下放暂存区。★ 必须在**同一个盘**：替换用 ``os.replace``，
# 跨盘会退化成「复制 + 删除」，中途失败就没有原子性可言了。
STAGING_DIR_NAME = ".update"
# 记录「上次更新发生了什么」，启动时读它决定要不要提示用户。
STATE_NAME = "update-state.json"
# 备份后缀：替换前先把原文件改名加这个，成功后再删。
BACKUP_SUFFIX = ".mdt-old"

# 执行体自己那个 exe 副本放哪（%TEMP% 下；用完删不掉，留着等下次清理）。
UPDATER_DIR_NAME = "mdt_updater"

# 环境变量：设成真值就完全停用自更新（开发机上很需要）。
DISABLE_ENV = "MDT_NO_SELFUPDATE"

# 三种档位（存在 config.json 的 ``launcher_update``）：
#   auto  —— 后台自动检查 + 下载，退出启动器时自动应用（默认）
#   check —— 只检查并提示，不下载
#   off   —— 什么都不做
# ★ 三个词与解析逻辑定义在 utils（config 也要用它，放这里会绕成循环 import），
#   这里只是取个短名字方便本模块内部用。
MODE_AUTO, MODE_CHECK, MODE_OFF = LAUNCHER_UPDATE_MODES

# 允许被替换的路径（防止一个被篡改/写坏的更新包往任意位置写文件）。
# 除了 ``_internal/`` 前缀，还允许「当前正在运行的 exe 名」—— 用名字比
# 硬编码更稳（用户真把 exe 改过名也照样能更新）。
INTERNAL_PREFIX = "_internal/"

# 这些文件在程序目录里属于「用户的东西」，任何情况下都不许被更新覆盖。
# 白名单是上面那条前缀判断，这里再列一次是为了留个显式记录。
PROTECTED_NAMES = frozenset({
    "config.json", "launcher.log", "Mindustry.json",
})

StatusCallback = Callable[..., None]


# ---- 版本号比较 ---------------------------------------------------------

def parse_version(text: Any) -> tuple[int, ...]:
    """把版本号切成可比较的数字元组。切不出数字给 ``(0,)``。

    ``"v1.0.10"`` → ``(1, 0, 10)``；``"1.1"`` → ``(1, 1)``。
    元组比较天然满足 ``(1,0,10) > (1,0,9)``（逐位比），不需要补零。
    """
    if not isinstance(text, str):
        text = str(text or "")
    nums = tuple(int(x) for x in re.findall(r"\d+", text))
    return nums or (0,)


def is_newer(remote: Any, local: Any = None) -> bool:
    """远端版本是不是**严格比**本地新。

    ★ 必须是「严格大于」：等于不动作（否则每次启动都重复下载），
    更小更不动作 —— **绝不降级**。开发机上自己编的版本可能比 Release
    还新，这条是防止 Release 把开发版顶回去的最后一道闸。
    """
    remote_v = parse_version(remote)
    local_v = parse_version(local if local is not None else __version__)
    return remote_v > local_v


# ---- 更新计划 -----------------------------------------------------------

@dataclass
class UpdatePlan:
    """「要写哪些文件」的完整描述。由更新包里的 ``update.json`` 决定。"""

    version: str = ""
    base_version: str = ""
    files: list[dict] = field(default_factory=list)   # [{path, sha256, size}]
    remove: list[str] = field(default_factory=list)
    exe_name: str = ""

    def total_size(self) -> int:
        return sum(int(f.get("size") or 0) for f in self.files)


def _safe_rel(rel: Any) -> str | None:
    """把一个来自更新包的相对路径清成安全值；不安全就返回 None。

    挡的是「往程序目录外面写」这类事（``../../Windows/xxx``、绝对路径、
    盘符、UNC）。更新包虽然是本项目自己产的，但它是**从网上下来的**，
    中间可能被镜像站改过 —— 校验 sha256 只能证明「没下坏」，不能证明
    「内容是你想的那份」，所以路径白名单必须自己判。
    """
    if not isinstance(rel, str) or not rel:
        return None
    p = rel.replace("\\", "/").strip()
    if p.startswith("/") or p.startswith("//"):
        return None
    if re.match(r"^[A-Za-z]:", p):                 # C:\ 之类
        return None
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if not parts or any(seg == ".." for seg in parts):
        return None
    return "/".join(parts)


def _is_replaceable(rel: str, exe_name: str) -> bool:
    """这个相对路径允许被更新覆盖吗。

    只有两类：当前 exe 本身、``_internal/`` 里的东西。其余（``jre/``、
    ``config.json``、``versions/`` ……）一律拒绝 —— 更新**不许碰用户数据**，
    也不该去动用户自己换过的 jre。
    """
    if rel in PROTECTED_NAMES:                # 显式再挡一次（前缀判断之外的兜底）
        return False
    if rel.startswith(INTERNAL_PREFIX):
        return True
    if exe_name and rel == exe_name:
        return True
    # 没有给出 exe 名时（测试、或将来别处调用）才放宽到「顶层的 .exe」——
    # 有名字的时候按名字比，不放任任意 exe 写进程序目录。
    return (not exe_name) and "/" not in rel and rel.lower().endswith(".exe")


def parse_manifest(raw: Any, exe_name: str = "") -> UpdatePlan | None:
    """解析更新包里的 ``update.json``。**不抛异常**，认不了就返回 None。

    坏包宁可整份丢掉也不要「读一半」—— 半份计划比没有计划危险得多。
    """
    if not isinstance(raw, dict):
        logger.warning("更新包的 update.json 不是对象，忽略")
        return None
    fmt = raw.get("format")
    if fmt != UPDATE_FORMAT:
        # 比本版本新的格式：老启动器读不懂，直接放弃（用户手动更新即可）
        logger.warning(f"更新包格式 {fmt!r} 本版本读不了（支持 {UPDATE_FORMAT}）")
        return None
    app_id = raw.get("app_id")
    if isinstance(app_id, str) and app_id and app_id != APP_ID:
        logger.warning(f"更新包属于 {app_id!r}，不是 {APP_ID!r}，忽略")
        return None
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        logger.warning("更新包没有 version，忽略")
        return None

    plan = UpdatePlan(
        version=version.strip(),
        base_version=str(raw.get("base_version") or "").strip(),
        exe_name=exe_name,
    )
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        logger.warning("更新包没列出任何文件，忽略")
        return None
    for item in files:
        if not isinstance(item, dict):
            continue
        rel = _safe_rel(item.get("path"))
        if rel is None:
            logger.warning(f"更新包里有不安全的路径，整份忽略: {item.get('path')!r}")
            return None
        if not _is_replaceable(rel, exe_name):
            logger.warning(f"更新包里出现不该被替换的路径，整份忽略: {rel}")
            return None
        digest = item.get("sha256")
        if not isinstance(digest, str) or not digest:
            logger.warning(f"更新包里的 {rel} 没有 sha256，整份忽略")
            return None
        try:                                   # size 只用来估体积，坏值当 0
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        plan.files.append({
            "path": rel,
            "sha256": digest.lower(),
            "size": size,
        })
    removes = raw.get("remove")
    if isinstance(removes, list):
        for rel_raw in removes:
            rel = _safe_rel(rel_raw)
            # 只允许删 _internal/ 里的东西；删别的（尤其是用户数据）一律无视
            if rel is not None and rel.startswith(INTERNAL_PREFIX):
                plan.remove.append(rel)
    return plan


# ---- ① 检查 -------------------------------------------------------------

def _pick_update_asset(release: dict) -> dict | None:
    """在 release 的资产里挑出精简更新包。"""
    for asset in release.get("assets", []):
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        if name.lower().endswith(UPDATE_ASSET_SUFFIX):
            return asset
    return None


def fetch_latest_update(timeout: int = 6) -> dict | None:
    """查一次最新 Release。返回 None = 没查到 / 网络不通 / 没有更新包。

    只查 ``/releases/latest``（GitHub 会跳过草稿和预发布），比拉整个
    releases 列表再挑要省流量也省时间。
    """
    try:
        req = urllib.request.Request(
            SELF_UPDATE_API,
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        )
        # 超时给短一点：这个请求在后台跑，卡住的每一秒都是白等
        with urllib.request.urlopen(
            req, timeout=timeout, context=UNVERIFIED_SSL_CTX
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        logger.info(f"查询启动器更新失败（不影响使用）: {e}")
        return None
    if not isinstance(data, dict):
        logger.info("启动器更新接口返回的不是对象（可能被限流）")
        return None
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None
    asset = _pick_update_asset(data)
    if asset is None:
        # 有新版但没带更新包 —— 只能让用户手动下。这不是错误，
        # 是「发布时漏传了资产」，记一条 info 就够了。
        logger.info(f"发现 {tag}，但里面没有 {UPDATE_ASSET_SUFFIX} 资产")
        return {"version": tag.lstrip("vV").strip(), "url": "", "size": 0,
                "sha256": "", "notes": str(data.get("body") or ""),
                "html_url": str(data.get("html_url") or "")}
    raw_digest = str(asset.get("digest") or "")
    return {
        "version": tag.lstrip("vV").strip(),
        "url": str(asset.get("browser_download_url") or ""),
        "size": int(asset.get("size") or 0),
        # GitHub 从 2025 起给资产带 digest（"sha256:xxxx"）；没有就空着，
        # 那种情况下靠大小 + 解压时的逐文件校验兜底。
        "sha256": raw_digest[7:].lower() if raw_digest.startswith("sha256:") else "",
        "notes": str(data.get("body") or ""),
        "html_url": str(data.get("html_url") or ""),
    }


# ---- ② 下载 + 暂存 ------------------------------------------------------

def download_update(
    info: dict,
    dest: Path,
    mirror: str = "",
    stop_event: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> bool:
    """下更新包。镜像前缀试不过就退回直连（跟游戏版本下载一个套路）。"""
    urls = []
    if mirror and info.get("url"):
        urls.append(mirror + info["url"])
    if info.get("url"):
        urls.append(info["url"])
    if not urls:
        return False
    for url in urls:
        if stop_event is not None and stop_event.is_set():
            return False
        ok = download_file(
            url, dest, stop_event,
            on_progress=on_progress,
            expected_sha256=info.get("sha256") or None,
        )
        if ok:
            expect = int(info.get("size") or 0)
            actual = dest.stat().st_size
            if expect > 0 and actual != expect:
                logger.warning(f"更新包大小不符（预期 {expect}，实际 {actual}）")
                dest.unlink(missing_ok=True)
                continue
            if not zipfile.is_zipfile(dest):
                logger.warning("更新包不是有效的 zip")
                dest.unlink(missing_ok=True)
                continue
            return True
        logger.warning(f"从 {url} 下更新包失败，换下一个地址")
    return False


def stage_update(
    zip_path: Path, staging_dir: Path, exe_name: str
) -> UpdatePlan | None:
    """把更新包解到暂存区，返回计划。**不做任何替换动作。**

    分两步（先解压、后替换）是有意的：解压和校验都在主程序还活着的时候
    做完，真正的「换文件」留给执行体 —— 那一步必须快、且只在旧进程退出
    之后发生，不能一边解压一边占着目标文件。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            if UPDATE_MANIFEST not in names:
                logger.warning("更新包里没有 update.json")
                return None
            raw = json.loads(zf.read(UPDATE_MANIFEST).decode("utf-8"))
            plan = parse_manifest(raw, exe_name)
            if plan is None:
                return None
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)
            for item in plan.files:
                entry = f"{UPDATE_FILES_PREFIX}{item['path']}"
                if entry not in names:
                    logger.warning(f"更新包里缺文件: {entry}")
                    return None
                target = staging_dir / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                # ★ 逐文件校验 sha256：包整体校验过是一回事，解出来的
                #   每个文件对不对是另一回事（能抓住解压路径/编码的坑）
                if _sha256_file(target) != item["sha256"]:
                    logger.warning(f"更新包里的 {item['path']} 校验失败")
                    return None
    except (zipfile.BadZipFile, json.JSONDecodeError, OSError, KeyError) as e:
        logger.warning(f"解更新包失败: {e}")
        return None
    logger.info(
        f"更新包已就绪：v{plan.version}，{len(plan.files)} 个文件，"
        f"解出 {plan.total_size()} 字节"
    )
    return plan


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- ③ 应用（执行体）---------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """那个进程还活着吗。拿不到就说「不在了」（宁可直接往下走）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _wait_for_exit(pid: int, timeout: float = 90.0) -> bool:
    """等旧启动器退出（释放 exe 的文件锁）。超时就放弃。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            # 进程没了不等于文件锁立刻放开（句柄回收、杀软还在扫），
            # 所以再多给一小会儿，让「改名旧 exe」这一步别撞上 WinError 32
            time.sleep(0.4)
            return True
        time.sleep(0.25)
    logger.warning(f"等了 {timeout:.0f} 秒，进程 {pid} 还没退出，放弃本次更新")
    return False


def launch_updater(plan_path: Path, app_dir: Path) -> bool:
    """把当前 exe 复制到 %TEMP% 再以 ``--apply-update`` 启动它。

    ★ 为什么不用 .cmd 批处理：这个程序的路径里**带中文**（用户目录、
      项目目录都可能），批处理在 GBK/UTF-8 上非常容易翻车（表现是
      「双击没反应」或「路径变成乱码」）。用自己这个 exe 当执行体，
      路径全程走 Python 的宽字符 API，没有编码问题，而且替换逻辑
      跟主程序共用一份代码、能被回归测到。
    """
    try:
        updater_dir = Path(tempfile.gettempdir()) / UPDATER_DIR_NAME
        updater_dir.mkdir(parents=True, exist_ok=True)
        updater_exe = updater_dir / Path(sys.executable).name
        shutil.copy2(sys.executable, updater_exe)
        flags = 0
        if os.name == "nt":
            # 脱离父进程：父进程随后就退出了，不能让它连带把执行体带走
            flags = (subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
        subprocess.Popen(
            [str(updater_exe), "--apply-update", str(plan_path)],
            cwd=str(updater_dir),
            creationflags=flags,
            close_fds=True,
        )
        logger.info(f"更新执行体已启动：{updater_exe}")
        return True
    except OSError as e:
        logger.error(f"启动更新执行体失败: {e}")
        return False


def apply_update_main(plan_path: Path) -> int:
    """执行体主流程（由 ``--apply-update`` 调用，**不返回 GUI**）。

    顺序上有一条讲究：**先替换 ``_internal/``，最后替换 exe**。
    反过来的话，exe 已经是新版、而它要用的运行时还是旧的 —— 那中间态
    一旦被打断（断电、被杀软拦），用户就得到一个起不来的程序；
    按这个顺序，最坏情况也只是「旧 exe + 新运行时」，而 PyInstaller 的
    运行时就向后兼容得多。
    """
    try:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"更新执行体读不到计划: {e}")
        return 2

    app_dir = Path(plan.get("app_dir") or "")
    staging = Path(plan.get("staging_dir") or "")
    parent_pid = int(plan.get("parent_pid") or 0)
    version = str(plan.get("version") or "")
    files = plan.get("files") or []
    removes = plan.get("remove") or []
    if not app_dir or not staging:
        logger.error("更新计划里缺少 app_dir / staging_dir")
        return 2

    logger.info(f"开始应用更新 v{version} -> {app_dir}")
    if not _wait_for_exit(parent_pid):
        _write_state(app_dir, "failed", version,
                     "等待旧进程退出超时，未做任何改动")
        return 3

    # 先内部文件、后 exe（见 docstring）
    ordered = sorted(files, key=lambda f: str(f.get("path", "")).lower()
                     .endswith(".exe"))
    done: list[Path] = []          # 已经换过的目标（回滚用）
    backups: list[tuple[Path, Path]] = []

    try:
        for item in ordered:
            rel = str(item.get("path") or "")
            src = staging / rel
            dst = app_dir / rel
            if not src.is_file():
                raise OSError(f"暂存区里没有 {rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            backup = dst.with_name(dst.name + BACKUP_SUFFIX)
            if backup.exists():
                backup.unlink()
            if dst.exists():
                os.replace(dst, backup)      # 同盘，原子
                backups.append((dst, backup))
            os.replace(src, dst)             # 同盘，原子（staging 就在 app_dir 下）
            done.append(dst)
            logger.info(f"已替换 {rel}")

        # 该删的旧文件（只在 _internal/ 里，先改名再统一删，留回滚余地）
        for rel in removes:
            dst = app_dir / rel
            if not dst.exists():
                continue
            backup = dst.with_name(dst.name + BACKUP_SUFFIX)
            if backup.exists():
                backup.unlink()
            os.replace(dst, backup)
            backups.append((dst, backup))
            logger.info(f"已移除 {rel}")
    except OSError as e:
        logger.error(f"替换失败，开始回滚: {e}")
        for dst, backup in reversed(backups):
            try:
                if dst.exists():
                    dst.unlink()
                if backup.exists():
                    os.replace(backup, dst)
            except OSError as rollback_err:                          # noqa: BLE001
                logger.error(f"回滚 {dst} 也失败了: {rollback_err}")
        _write_state(app_dir, "failed", version, f"替换失败：{e}")
        return 4

    # 全部成功 → 清备份
    for _, backup in backups:
        try:
            backup.unlink(missing_ok=True)
        except OSError as e:
            logger.info(f"备份没删掉（无害，下次更新会覆盖）: {e}")

    # 整个暂存区（含 files/ 与 plan.json）一起清掉；删不掉也无所谓，
    # 下次更新会先 rmtree 再重建。
    shutil.rmtree(staged_root(app_dir), ignore_errors=True)
    _write_state(app_dir, "done", version, f"已更新 {len(done)} 个文件")
    logger.info(f"更新完成：v{version}")
    return 0


# ---- 更新状态（下次启动时读）------------------------------------------

def state_path(app_dir: Path) -> Path:
    return Path(app_dir) / STATE_NAME


def read_state(app_dir: Path) -> dict | None:
    p = state_path(app_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("update-state.json 读不了，忽略")
        return None
    return data if isinstance(data, dict) else None


def clear_state(app_dir: Path) -> None:
    try:
        state_path(app_dir).unlink(missing_ok=True)
    except OSError as e:
        logger.info(f"清 update-state.json 失败（无害）: {e}")


def _write_state(app_dir: Path, status: str, version: str, message: str) -> None:
    try:
        atomic_write_json(state_path(app_dir), {
            "status": status,
            "version": version,
            "message": message,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    except OSError as e:
        logger.error(f"写 update-state.json 失败: {e}")


# ---- 总开关 -------------------------------------------------------------

def mode_of(config: Any) -> str:
    """当前档位（读配置 + 各种「一律停用」的硬条件）。"""
    if os.environ.get(DISABLE_ENV):
        return MODE_OFF
    if not getattr(sys, "frozen", False):
        # 源码运行 = 开发/测试机：它跑的代码比 Release 新，自动更新只会
        # 帮倒忙（把工作副本覆盖成发布版）。一律不检查。
        return MODE_OFF
    try:
        raw = config.get("launcher_update")
    except Exception as e:                                          # noqa: BLE001
        logger.info(f"读 launcher_update 失败，按默认处理: {e}")
        raw = LAUNCHER_UPDATE_MODES[0]
    return normalize_launcher_update(raw)


def staged_root(app_dir: Path) -> Path:
    """暂存区的根（程序目录下，同盘 —— 替换才能用原子的 os.replace）。"""
    return Path(app_dir) / STAGING_DIR_NAME


def staged_files_dir(app_dir: Path) -> Path:
    """解出来的新文件放这儿（执行体按 ``这里/相对路径`` 取文件）。"""
    return staged_root(app_dir) / "files"


def staged_plan_path(app_dir: Path) -> Path:
    return staged_root(app_dir) / "plan.json"


def write_plan(app_dir: Path, plan: UpdatePlan, parent_pid: int) -> Path:
    """把计划落盘（执行体读它，所以必须是文件而不是内存里的一份）。

    ★ ``staging_dir`` 要指向 ``.update/files``（真正放解出来的文件的目录），
      不是 ``.update`` 本身 —— 执行体是按 ``staging_dir / rel`` 取文件的，
      指错一层就会「找不到暂存文件」而整个更新失败。
    """
    staging = staged_files_dir(app_dir)
    plan_path = staged_plan_path(app_dir)
    atomic_write_json(plan_path, {
        "format": UPDATE_FORMAT,
        "version": plan.version,
        "base_version": plan.base_version,
        "app_dir": str(app_dir),
        "staging_dir": str(staging),
        "exe_name": plan.exe_name,
        "parent_pid": int(parent_pid),
        "files": plan.files,
        "remove": plan.remove,
    })
    return plan_path


def check_and_stage(
    config: Any,
    app_dir: Path,
    stop_event: threading.Event | None = None,
    on_status: StatusCallback | None = None,
) -> dict:
    """跑一遍「检查 → 下载 → 暂存」。

    返回值是给界面用的结果字典（``status`` 是 ``off`` / ``latest`` /
    ``available`` / ``staged`` / ``failed`` 之一，``version`` 是新版本号，
    ``plan_path`` 在 ``staged`` 时有效）。

    ★ 这是个**会阻塞的网络函数**，调用方必须丢进后台线程。
    ★ 任何一步失败都只是「这次没更新成」，绝不影响启动器本身。
    """
    def status(msg: str, log_level: int | None = None) -> None:
        """转给界面。日志级别那一档只有真的回调支持时才传。"""
        if not on_status:
            return
        try:
            if log_level is None:
                on_status(msg)
            else:
                on_status(msg, log_level=log_level)
        except TypeError:
            # 回调不认 log_level（测试里塞的假回调）—— 退回只传文本
            on_status(msg)

    mode = mode_of(config)
    if mode == MODE_OFF:
        return {"status": "off"}

    try:
        mirror = normalize_mirror(config.get("github_mirror"))
    except Exception as e:                                          # noqa: BLE001
        logger.info(f"读镜像配置失败，按直连处理: {e}")
        mirror = ""

    status("🔍 正在检查启动器更新...")
    info = fetch_latest_update()
    if info is None:
        return {"status": "latest"}
    version = info.get("version") or ""
    if not is_newer(version):
        return {"status": "latest", "version": version}
    logger.info(f"发现启动器新版本 v{version}（当前 v{__version__}）")

    if mode == MODE_CHECK or not info.get("url"):
        # check 档只提示；auto 档但发布时没带更新包 —— 也只能提示
        return {"status": "available", "version": version,
                "html_url": info.get("html_url") or ""}

    staging_root = staged_root(app_dir)
    staging_root.mkdir(parents=True, exist_ok=True)
    zip_path = staging_root / "update.zip"
    size_txt = f"{info['size'] / 1048576:.1f} MB" if info.get("size") else "?"
    status(f"📥 正在下载启动器更新 v{version}（{size_txt}）...")
    ok = download_update(
        info, zip_path, mirror=mirror, stop_event=stop_event,
        on_progress=lambda pct: status(
            f"📥 下载启动器更新 v{version}: {pct:.1f}%",
            log_level=logging.DEBUG,
        ),
    )
    if not ok:
        zip_path.unlink(missing_ok=True)
        return {"status": "failed", "version": version,
                "message": "更新包下载失败"}

    status(f"📦 正在校验并解包 v{version}...")
    exe_name = Path(sys.executable).name
    plan = stage_update(zip_path, staged_files_dir(app_dir), exe_name)
    zip_path.unlink(missing_ok=True)
    if plan is None:
        return {"status": "failed", "version": version,
                "message": "更新包校验失败"}
    plan_version = plan.version
    plan_path = write_plan(app_dir, plan, os.getpid())
    logger.info(f"启动器更新已就绪：v{plan_version}")
    return {"status": "staged", "version": plan_version,
            "plan_path": str(plan_path)}


def cleanup_updater_dir(hours: float = 24.0) -> None:
    """清掉 %TEMP% 里过期的执行体副本（它自己删不掉自己）。"""
    root = Path(tempfile.gettempdir()) / UPDATER_DIR_NAME
    if not root.is_dir():
        return
    cutoff = time.time() - hours * 3600
    for f in root.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
