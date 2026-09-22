# -*- coding: utf-8 -*-
"""通用工具：SSL 兼容、日志、原子写入、目录压缩、文件下载。"""
import hashlib
import json
import logging
import os
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "BASE_DIR",
    "DEV_LOG_NAME",
    "LOG_FILE",
    "LOG_NAME",
    "RESOURCE_DIR",
    "UNVERIFIED_SSL_CTX",
    "atomic_write_json",
    "atomic_write_text",
    "download_file",
    "logger",
    "parse_bool",
    "resource_path",
    "resolve_log_file",
    "setup_logging",
    "zip_directory",
]

# GitHub 直连在国内常撞上证书吊销检查失败（CRYPT_E_NO_REVOCATION_CHECK），
# 所以对我们自己发起的那几个 HTTPS 请求不校验证书。
#
# ⚠️ 以前这行是 ``ssl._create_default_https_context = _create_unverified_context``
# —— 那是**进程级**副作用：会把整个进程（包括别处 import 进来的库）的证书
# 校验一起关掉。现在只把下面这个 context 显式传给自己的请求（download_file、
# updates._fetch_release），影响面就只剩这两处。
UNVERIFIED_SSL_CTX = ssl._create_unverified_context()

# 实际使用（打包后的 exe）写的日志
LOG_NAME = "launcher.log"
# 源码运行（开发/调试/测试/跑验证脚本）写的日志 —— 单独一个文件，
# 免得测试的噪音把真正用起来时的日志冲掉、或者反过来被占住删不掉。
DEV_LOG_NAME = "launcher.dev.log"

# 显式指定日志文件（测试脚本用它把日志引到临时目录，绝不碰真实日志）。
# 值为路径；相对路径按进程 cwd 解析。
LOG_FILE_ENV = "MDT_LOG_FILE"


# ---- 开关值解析（全项目唯一实现）----------------------------------------
# ★ 放在 utils 而不是 config：config 和 sources 都要用它，而 sources 不能
#   import config（config 要 import sources，会绕成环）。这里谁都 import 得到。
#
# 为什么必须专门解析：``bool("false")`` 在 Python 里是 **True** —— 配置里
# 写成字符串 "false"，开关反而被打开。对 ``close_on_game_exit`` 来说那是
# 「启动器自己关了」，对 ``permanent_delete`` 来说是「删了不能还原」。
TRUE_WORDS = frozenset({"true", "1", "yes", "y", "on", "t"})
FALSE_WORDS = frozenset({"false", "0", "no", "n", "off", "f"})


def parse_bool(raw: Any, default: bool, *, what: str = "开关") -> bool:
    """把配置里的开关值清成真正的 bool。**不抛异常。**

    认：真 bool、0/1、以及 "true/false/yes/no/on/off/y/n/t/f"（不分大小写）。
    其它一律退回默认值 + WARNING（不猜用户的意思）。
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):                     # 0 / 1（JSON 里可能写成数字）
        return bool(raw)
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
    logger.warning(f"配置项 {what}={raw!r} 不是布尔值，按默认 {default} 处理")
    return default


# ---- 启动器自更新的档位（同样放这里，理由见上面的 parse_bool）------------
# 三档：auto = 自动检查+下载，退出时应用；check = 只提示；off = 完全停用。
# ★ 定义在 utils 是因为 **config 和 selfupdate 都要用它**，而 selfupdate
#   要 import config（读镜像、读档位）—— 解析逻辑留在 selfupdate 里就会
#   绕成 config <-> selfupdate 的循环 import。
LAUNCHER_UPDATE_MODES = ("auto", "check", "off")
LAUNCHER_UPDATE_DEFAULT = "auto"


def normalize_launcher_update(
    raw: Any, *, default: str = LAUNCHER_UPDATE_DEFAULT,
    what: str = "launcher_update",
) -> str:
    """把「启动器自更新」的档位清成三档之一。**不抛异常。**

    认不出的一律退回默认 + WARNING（并**点名配置键**，用户得知道去
    config.json 里改哪一行）。按兼容约定：以后要加档位只能往
    ``LAUNCHER_UPDATE_MODES`` 里加，别改现有三个词的含义。
    """
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in LAUNCHER_UPDATE_MODES:
            return word
    logger.warning(
        f"配置项 {what}={raw!r} 不是 {'/'.join(LAUNCHER_UPDATE_MODES)} 之一，"
        f"按默认 {default} 处理"
    )
    return default


def resolve_log_file(base_dir: Path) -> Path:
    """决定日志写到哪个文件。

    优先级：
      1. 环境变量 ``MDT_LOG_FILE``（测试/冒烟用，可指到临时目录）
      2. 打包后的 exe（真正在用）→ ``<数据根>/launcher.log``
      3. 源码运行（开发/测试）    → ``<数据根>/launcher.dev.log``

    ★ 铁律：**源码/测试运行绝不往 launcher.log 写**。实际使用的日志要能
    干净地反映「用户双击 exe 跑出来的这一次」，不能被开发时的跑动污染。
    """
    override = os.environ.get(LOG_FILE_ENV)
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        return base_dir / LOG_NAME
    return base_dir / DEV_LOG_NAME


def setup_logging(log_file: Path | None = None) -> None:
    """配置根 logger。不给 log_file 就用 ``LOG_FILE``（见 `resolve_log_file`）。

    注意：会清掉已有的 handler —— 重复调用是安全的，但后一次会顶掉前一次。
    """
    log_file = Path(log_file) if log_file else LOG_FILE
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    level = logging.DEBUG if "--debug" in sys.argv else logging.INFO
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    # 万一 MDT_LOG_FILE 指到还没建的目录，别让 import 阶段直接炸
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    fh.setLevel(level)
    root_logger.addHandler(fh)
    # 打包成 windowed exe 后没有控制台，sys.stderr 为 None。此时
    # StreamHandler.emit() 每写一条日志都会抛 AttributeError（被 logging
    # 静默吞掉，但白白付出异常开销）——所以只在有 stderr 时才加。
    if sys.stderr is not None:
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        ch.setLevel(level)
        root_logger.addHandler(ch)

def _app_dir() -> Path:
    """数据根：放 config.json / launcher.log / versions / backups 的地方。

    frozen（打包成 exe）时是 exe 所在目录——用 cwd 不可靠，从快捷方式或
    别的目录启动时 cwd 会变，配置和备份就会散落到意外位置。
    源码运行时保持原来的 Path.cwd() 语义不变。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path.cwd()


BASE_DIR = _app_dir()

# 资源根：jre / Mindustry.json 这类「跟着程序走、不由用户改」的文件。
# onefile 打包后它们被解压到 sys._MEIPASS，不在 BASE_DIR 里。
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", "") or BASE_DIR)

# 本次进程实际写日志的文件（见 resolve_log_file 的规则）。
LOG_FILE = resolve_log_file(BASE_DIR)

setup_logging(LOG_FILE)
logger = logging.getLogger(__name__)


def resource_path(rel: str | Path) -> Path:
    """定位打包资源：优先 exe 旁边的外部副本，回退到包内解压目录。

    外部优先让用户能覆盖——想换 jre 版本，在 exe 旁边放个 jre/ 即可，
    不必重新打包。源码运行时两个根相同，行为与原来一致。
    """
    outer = BASE_DIR / rel
    if outer.exists():
        return outer
    return RESOURCE_DIR / rel


# 所有原子写入串行化。进程内多线程同时 os.replace 同一个目标，
# Windows 会直接甩 WinError 5（拒绝访问）——先在同进程里排好队，
# 外部占用（杀毒扫描等）再交给下面的重试处理。
_WRITE_LOCK = threading.Lock()

def atomic_write_text(
    path: Path, content: str, encoding: str = "utf-8"
) -> None:
    """原子写入文本。

    Windows 上 os.replace 报 WinError 5（拒绝访问）通常是两种情况：
      1. 本进程多个线程同时替换同一个目标；
      2. 目标文件被外部占用（杀毒软件正在扫、编辑器开着等）。
    第 1 种靠 _WRITE_LOCK 排掉，第 2 种靠重试；实在不行退化成
    直接覆盖写——宁可丢掉「原子性」也不能丢掉用户的配置。
    """
    path = Path(path)
    with _WRITE_LOCK:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        tmp_path: Path | None = Path(tmp)
        try:
            with os.fdopen(fd, "w", encoding=encoding) as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            # 临时文件已经落盘，之后只重试「替换」这一步，
            # 不再反复建/删临时文件（那本身又会惊动杀毒软件）。
            last_err: OSError | None = None
            for attempt in range(6):
                try:
                    os.replace(tmp_path, path)
                    if attempt:
                        logger.debug(
                            f"原子写入第 {attempt + 1} 次尝试成功: {path}"
                        )
                    return
                except OSError as e:
                    last_err = e
                    time.sleep(0.08 * (attempt + 1))  # 累计约 1.7 秒
            logger.error(f"原子写入失败 {path}: {last_err}")

            # 兜底：直接覆盖写。有些程序（编辑器、同步盘）会以
            # 「允许写、不允许删除」的方式占用文件，此时 replace 必失败，
            # 但普通写入是通的。
            try:
                with path.open("w", encoding=encoding) as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                logger.warning(f"原子替换长时间失败，已退化为直接写入: {path}")
                return
            except OSError as e2:
                logger.error(f"直接写入也失败 {path}: {e2}")
                raise last_err  # type: ignore[misc]
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(
        path, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def zip_directory(source: Path, dest: Path) -> int:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in source.walk():
            for file in files:
                f = root / file
                zf.write(f, f.relative_to(source))
    size = dest.stat().st_size
    logger.debug(f"压缩目录 {source} -> {dest}，大小 {size} 字节")
    return size

def download_file(
    url: str,
    dest: Path,
    stop_event: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
    expected_sha256: str | None = None,
) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(
            req, timeout=30, context=UNVERIFIED_SSL_CTX
        ) as resp:
            if resp.status != 200:
                logger.warning(f"下载失败 HTTP {resp.status}: {url}")
                return False
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            last_update = time.time()
            sha256 = hashlib.sha256()
            with open(dest, "wb") as f:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        logger.info("下载被用户取消")
                        return False
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    sha256.update(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if on_progress and total > 0 and now - last_update >= 1.0:
                        on_progress(downloaded / total * 100)
                        last_update = now
            if expected_sha256:
                actual = sha256.hexdigest().lower()
                if actual != expected_sha256.lower():
                    logger.error(
                        f"SHA256 校验失败: 期望 {expected_sha256}, 实际 {actual}"
                    )
                    return False
            logger.info(f"下载成功: {url} -> {dest}")
            return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.error(f"下载异常 {url}: {e}")
        return False
