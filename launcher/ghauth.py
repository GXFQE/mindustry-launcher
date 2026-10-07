# -*- coding: utf-8 -*-
"""GitHub CLI（gh）登录态复用：给发往 GitHub 官方域名的请求带上认证。

**为什么做这个**：未认证的 GitHub API 额度是 60 次/时，而且这个额度是
**按出口 IP 共享**的 —— 单位、校园网的内网 NAT 后面所有机器一起分；本机
装了 gh 并且 ``gh auth login`` 过的话，同样的请求带上认证就是 5000 次/时。

**令牌怎么拿**：跑一次 ``gh auth token`` —— 这是 gh 自己的公开接口（给
CI、集成工具用的就是它）。令牌只进**内存**（本模块的缓存），不落盘、
不进日志、随进程消失。不去读 gh 的配置文件，也不碰系统凭据管理器。

**★ 两条安全底线**（改这个模块之前先把它们读完）：
1. 只有 GitHub **官方域名**（见 ``_GITHUB_HOSTS``，精确匹配）才配带上
   令牌。用户能自定义版本来源、镜像前缀、自更新接口 —— 那些请求绝不能
   看到令牌（一个恶意的「镜像站」拿到带 repo 权限的令牌就全完了）。
2. 开关关闭时（``config.json`` 的 ``use_gh_auth``，**默认关** = opt-in），
   请求路径**连探测都不做** —— 不是「取回来不用」，而是根本不碰用户凭据。

**本模块只依赖标准库**：它会被 ``utils.download_file`` 使用，而 utils
位于依赖链最底层（utils → config → …），所以这里不许 import 任何
launcher 模块（否则立刻绕成循环 import）。

**线程约束**：首次探测会在调用线程里起一个 ``gh`` 子进程（正常一两百
毫秒，上限 6 秒）。调用点必须在**后台线程** —— 现在三处（游戏版本检查、
启动器自更新检查、文件下载）都满足；将来加调用点时照此办理。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# gh 子进程的超时上限。正常一两百毫秒就回；卡住说明它自己出了问题。
# 到点就放弃 —— 认证是增强项，绝不能把请求路径拖死。
_GH_TIMEOUT = 6.0

# ★ 允许携带认证头的域名（**精确匹配**，不做后缀匹配 —— 「api.github.com.
#   evil.com」这种必须以「不在册」处理）。要加新域名先想清楚：它是不是
#   GitHub 自己控制的？第三方一律不加 —— 那是泄露，不是优化。
_GITHUB_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})

_lock = threading.RLock()

# 开关：由配置层注入（见 set_enabled）。这里的初值 = **出厂默认（关）** ——
# 配置加载前万一被某个请求先用到，也只会「什么都不做」，不会去碰用户凭据。
# ConfigManager 一构造就会调 set_enabled 覆盖它。
_enabled = False

# 探测只做一次（成功、失败都算）：失败（没装 gh / 没登录）也要记住，
# 否则每个请求都起一次子进程白等几百毫秒。
_probed = False
_token: str | None = None
_status = "unknown"        # unknown / ok / not-logged-in / missing


def set_enabled(enabled: bool) -> None:
    """由配置层注入开关（``config.load`` / ``config.set`` 都会调）。

    关掉时把缓存一并清干净：用户说不用，进程里就不该留着令牌。
    """
    global _enabled, _probed, _token, _status
    with _lock:
        _enabled = bool(enabled)
        if not _enabled:
            _probed = False
            _token = None
            _status = "unknown"


def _probe() -> None:
    """真跑一次 ``gh auth token``，更新 ``_token`` / ``_status``。

    调用方持锁。**不抛异常**（任何失败都落到 missing / not-logged-in）。
    """
    global _token, _status
    exe = shutil.which("gh")
    if not exe:
        _token = None
        _status = "missing"
        logger.info("未检测到 GitHub CLI（gh），GitHub 请求走匿名")
        return
    # Windows：打包成窗口程序后起子进程会闪一个控制台黑框，压掉它。
    # （其它平台该值为 0 = 无标志，等于不传。）
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    try:
        proc = subprocess.run(
            [exe, "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_GH_TIMEOUT,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as e:
        _token = None
        _status = "missing"
        logger.info(f"调用 gh 取令牌失败: {e}")
        return
    token = (proc.stdout or "").strip()
    if proc.returncode != 0 or not token:
        _token = None
        _status = "not-logged-in"
        logger.info("检测到 gh 但未登录（gh auth login 后可提高 GitHub 额度）")
        return
    _token = token
    _status = "ok"
    logger.info("已启用 GitHub CLI 认证：GitHub API 额度 5000 次/时")


def get_github_token() -> str | None:
    """取令牌（进程内只探测一次，之后走缓存）。**不抛异常**。

    开关关闭时返回 None，且**不做任何探测** —— 这是安全底线之一，
    别为了「提前预热」把它改成先探测再判断。
    """
    global _probed
    with _lock:
        if not _enabled:
            return None
        if not _probed:
            _probed = True
            _probe()
        return _token


def refresh_status() -> str:
    """重新探测一次并返回状态（``ok`` / ``not-logged-in`` / ``missing``）。

    设置页显示专用：用户可能刚装好 gh、刚登录，不能拿进程启动时的旧结果
    给他看。探测结果与请求路径**共用同一份缓存** —— 设置页显示什么，
    请求就会怎么做，两边不会打架。**不抛异常**。
    """
    global _probed
    with _lock:
        _probed = True
        _probe()
        return _status


def _host_allowed(url: str) -> bool:
    """该 URL 配不配带认证：https + 默认端口 + GitHub 官方域名。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    if parts.port not in (None, 443):
        return False
    return (parts.hostname or "").lower() in _GITHUB_HOSTS


def apply_auth(req: urllib.request.Request) -> None:
    """给一个待发请求加认证头（该加才加）。**不抛异常**。

    认证是增强项：就算这里机关算尽出了问题，也不许把请求本身挡下来。
    """
    try:
        if not _host_allowed(req.full_url):
            return      # 镜像站 / 自定义源 / 自建代理：绝不带令牌
        token = get_github_token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"附加 GitHub 认证失败（不影响本次请求）: {e}")
