# -*- coding: utf-8 -*-
"""GitHub 镜像测速 —— 实测每个候选镜像的下载速度，挑最快的那个。

为什么是「实测下载」而不是「ping 一下」
--------------------------------------
镜像站（ghfast.top / gh-proxy.com 之类）快不快，取决于**它自己到 GitHub 的
链路**和当时的 CDN 缓存命中情况，跟「你到它的 RTT」基本没关系 —— 一个
10 ms 就能握手的镜像，回源慢的话下载照样是几十 KB/s。所以唯一算数的指标
就是**真下这个文件时的速度**，这里就真下。

测法（对每个候选各做一遍，**串行** —— 并行会互相抢带宽，量出来的是「每人
分到的份额」，不是单个镜像的真实能力）：

  1. 拼出下载地址（前缀 + 目标文件），发一个普通 GET；
  2. 记下**首字节延迟**（含 DNS + TCP + TLS + 服务器响应，比 ping 更接近
     点开下载时的体感）；
  3. 读满 ``SAMPLE_BYTES`` 就停 —— 提前关连接，不把整个 jar 拖下来；
     速度 = 读到的字节数 / 实际耗时；
  4. 单个候选有总预算 ``MIRROR_BUDGET`` 秒：超了收手。已经读到的数据照样
     用来算速度（那是它真实的吞吐，只是样本没读满），结果标成「超时」。

★ 目标文件写死在 ``TEST_URL``：一个**老版本、不会被删**的官方 release 资源。
  所有候选测的是同一个文件，结果才能横向比。
★ 目标文件是 30 MB 级的 jar，而这里只读 ~1.5 MB —— GET 发出后主动断开，
  镜像那边最多再推一个 TCP 窗口的数据，浪费可以忽略。

对外契约：**这一层绝不抛异常**。连不上、超时、返回错误页…… 全都变成一条
``MirrorResult``（带 status），让界面自己决定怎么显示 —— 测速是辅助功能，
任何一个候选出问题都不该把启动器带崩。
"""
from __future__ import annotations

import logging
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .utils import UNVERIFIED_SSL_CTX

logger = logging.getLogger(__name__)

# 测速目标：Mindustry v160.4 的官方桌面 jar。挑它的理由 ——
#   * 老版本的 release 资源不会被删（要挑就得挑「过去时」的 tag）；
#   * 它正是本启动器最常下载的那个文件，镜像对它的处理（缓存/回源）最接近
#     用户真实下载时遇到的情况。
TEST_URL = (
    "https://github.com/Anuken/Mindustry/releases/download/v160.4/Mindustry.jar"
)

# 每个候选最多读多少字节当样本。太小测不出速度（TCP 慢启动还没结束），
# 太大又纯属浪费时间 —— 1.5 MB 在「读数够稳」和「别等太久」之间。
SAMPLE_BYTES = 1_500_000
# 读到的量低于这个数就不算「下到了那个文件」：镜像返回的**错误页**（HTML）
# 通常只有几 KB —— 不拦住的话，一个「秒回 502 页面」的镜像会比谁都"快"。
MIN_OK_BYTES = 256_000
# 单个候选的总预算（秒）。连不上 / 半死不活的镜像，等它别超过这个数。
MIRROR_BUDGET = 12.0
# socket 级的单次连接/读超时。它管的是「某一次 read 卡住」，总时长由上面的
# 预算兜底（所以一边读一边还要查预算）。
SOCKET_TIMEOUT = 5.0
# 一次最多实测多少个候选：候选来自设置页的下拉清单（config.json 里可手改），
# 写进去 20 个就会一个一个测 —— 太久。截断时要记日志，别让人对着少测的名单
# 猜为什么。
MAX_TARGETS = 12
# 下载用 UA —— 和 utils.download_file 保持一致（有镜像站按 UA 分流）。
_USER_AGENT = "Mozilla/5.0"
# 每次 read 的大小。64 KB 是「syscall 次数」和「单次阻塞粒度」的折中。
_READ_CHUNK = 64 * 1024

# 「卡住」类异常。现代 Python 里 socket.timeout 就是 TimeoutError 的别名
# （3.10 起弃用别名写法），但为了在两边都认，探测式地拼一下元组 ——
# 直接写 socket.timeout 的话，将来它被删掉会在 except 求值时抛 AttributeError。
_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError,)
_ALIAS_TIMEOUT = getattr(socket, "timeout", None)
if _ALIAS_TIMEOUT is not None and _ALIAS_TIMEOUT is not TimeoutError:
    _TIMEOUT_ERRORS += (_ALIAS_TIMEOUT,)

# ---- 结果状态（界面按它选文案，别在界面里比裸字符串）----
STATUS_OK = "ok"                # 读满样本，速度可信
STATUS_TIMEOUT = "timeout"      # 预算内没读完（太慢/半死；可能有部分数据）
STATUS_SHORT = "short"          # 提前结束且量太少（多半是错误页，不是那个文件）
STATUS_HTTP = "http"            # 明确的 HTTP 错误码（404/502/…）
STATUS_NET = "net"              # 连不上：DNS / 拒绝连接 / TLS / 被重置
STATUS_CANCELED = "canceled"    # 用户取消（或启动器关闭）

# 排序档位：小的在前。同档内再按速度快慢排。
_STATUS_RANK = {
    STATUS_OK: 0,
    STATUS_TIMEOUT: 1,
    STATUS_SHORT: 2,
    STATUS_HTTP: 3,
    STATUS_NET: 4,
    STATUS_CANCELED: 5,
}


@dataclass(frozen=True)
class MirrorResult:
    """一个候选的测速结果。

    ``prefix`` 是镜像前缀，**空串 = 直连 GitHub**（它也是一名正式候选：
    有时候直连反而最快，比如挂着系统代理的时候）。
    """

    prefix: str
    status: str
    latency_ms: float | None = None    # 首字节延迟
    speed_bps: float | None = None     # 读到的字节 / 实际耗时
    bytes_read: int = 0
    http_code: int = 0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def probe_url(prefix: str, test_url: str = TEST_URL) -> str:
    """拼出真正要请求的地址。空前缀 = 直连（原样返回目标地址）。"""
    return (prefix + test_url) if prefix else test_url


def _stopped(stop_event: Any | None) -> bool:
    """停止信号来了吗。``stop_event`` 只要是「有 is_set()」的东西都收。"""
    return stop_event is not None and stop_event.is_set()


class _AnyStop:
    """把多个停止信号合成一个：任一 ``is_set()`` 为真即为真。

    用途：测速既要能被「取消」按钮停掉，也要在启动器关闭时立刻收手 ——
    于是把两个 Event 合成一份传进 ``test_mirrors``。只实现 ``is_set()``，
    跟 ``threading.Event`` 在这件事上长得一样。
    """

    __slots__ = ("_events",)

    def __init__(self, events: Iterable[Any]) -> None:
        self._events = tuple(e for e in events if e is not None)

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)


def any_stop(*events: Any) -> _AnyStop:
    """合成停止信号（任一生效即停）。见 ``_AnyStop``。"""
    return _AnyStop(events)


def test_mirror(
    prefix: str,
    *,
    test_url: str = TEST_URL,
    sample_bytes: int = SAMPLE_BYTES,
    budget: float = MIRROR_BUDGET,
    socket_timeout: float = SOCKET_TIMEOUT,
    stop_event: Any | None = None,
) -> MirrorResult:
    """实测一个候选（前缀）。**不抛异常** —— 一切意外都变成一条失败结果。

    参数给的是默认值，测试会用更小的样本/预算 + 本地 HTTP 服务来跑，
    所以别再写第二套实现（见 ``_tools/verify/code_regression.py``）。
    """
    if _stopped(stop_event):
        return MirrorResult(prefix, STATUS_CANCELED)
    url = probe_url(prefix, test_url)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    t0 = time.perf_counter()
    received = 0
    first_byte_at: float | None = None
    truncated = False       # 预算超了 / socket 读超时 —— 没读满样本
    canceled = False
    eof = False
    message = ""
    try:
        with urllib.request.urlopen(
            req, timeout=socket_timeout, context=UNVERIFIED_SSL_CTX
        ) as resp:
            code = int(getattr(resp, "status", 0) or 0)
            if code != 200:
                return MirrorResult(
                    prefix, STATUS_HTTP, http_code=code, message=f"HTTP {code}"
                )
            while received < sample_bytes:
                if _stopped(stop_event):
                    canceled = True
                    break
                if time.perf_counter() - t0 > budget:
                    truncated = True
                    break
                chunk = resp.read(_READ_CHUNK)
                if not chunk:
                    eof = True
                    break
                if first_byte_at is None:
                    first_byte_at = time.perf_counter()
                received += len(chunk)
    except urllib.error.HTTPError as e:
        # urlopen 对 4xx/5xx 直接抛异常，拿不到 resp —— 错误码在异常里。
        code = int(getattr(e, "code", 0) or 0)
        return MirrorResult(
            prefix, STATUS_HTTP, http_code=code, message=f"HTTP {code}"
        )
    except _TIMEOUT_ERRORS as e:
        # 连接或读到一半卡住：有数据就留着算速度，没有就是干等超时。
        truncated = True
        message = str(e)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, _TIMEOUT_ERRORS):
            truncated = True
            message = str(reason)
        else:
            return MirrorResult(prefix, STATUS_NET, message=str(reason or e))
    except OSError as e:
        # ConnectionResetError / SSLError / ssl.SSLCertVerificationError 等
        return MirrorResult(prefix, STATUS_NET, message=str(e))
    except ValueError as e:
        # 地址本身不合法（http:// 都没写对之类）—— 也是「这个候选不可用」
        return MirrorResult(prefix, STATUS_NET, message=str(e))
    except Exception as e:                                      # noqa: BLE001
        # 兜底：本方法对外承诺不抛。宁可报一条「未知错误」也不炸界面。
        logger.error(f"测速 {url} 出现意外错误: {e}", exc_info=True)
        return MirrorResult(prefix, STATUS_NET, message=str(e))

    elapsed = time.perf_counter() - t0
    speed = (received / elapsed) if (received > 0 and elapsed > 0) else None
    latency_ms = (first_byte_at - t0) * 1000 if first_byte_at else None

    if canceled:
        status = STATUS_CANCELED
    elif received >= sample_bytes:
        status = STATUS_OK
    elif truncated:
        status = STATUS_TIMEOUT
    elif eof and received >= MIN_OK_BYTES:
        # 文件比样本还小（理论上不会，但镜像可能截断过）—— 够量就算可用
        status = STATUS_OK
    else:
        status = STATUS_SHORT
    return MirrorResult(
        prefix, status,
        latency_ms=latency_ms, speed_bps=speed,
        bytes_read=received, message=message,
    )


def test_mirrors(
    prefixes: Sequence[str],
    *,
    on_result: Callable[[MirrorResult], None] | None = None,
    stop_event: Any | None = None,
    test_url: str = TEST_URL,
    sample_bytes: int = SAMPLE_BYTES,
    budget: float = MIRROR_BUDGET,
    socket_timeout: float = SOCKET_TIMEOUT,
) -> list[MirrorResult]:
    """依次实测一串候选，返回**已测完**的结果（被取消时可能不满）。

    ``on_result`` 每测完一个就回调一次，用来报进度。★ 它跑在**调用线程**里
    （不是 Tk 主线程）—— 界面那边要自己 ``run_on_gui`` 转回去再碰控件。
    """
    targets = list(prefixes)
    if len(targets) > MAX_TARGETS:
        logger.warning(
            f"测速候选有 {len(targets)} 个，只测前 {MAX_TARGETS} 个"
            "（清单来自 config.json 的 github_mirror_presets）"
        )
        targets = targets[:MAX_TARGETS]
    results: list[MirrorResult] = []
    for prefix in targets:
        if _stopped(stop_event):
            break
        r = test_mirror(
            prefix,
            test_url=test_url,
            sample_bytes=sample_bytes,
            budget=budget,
            socket_timeout=socket_timeout,
            stop_event=stop_event,
        )
        results.append(r)
        # 每个候选留一条日志：测速结果直接影响用户改镜像的选择，
        # 事后要能对着日志复盘（界面上的结果窗关掉就没了）。
        detail = f"bytes={r.bytes_read}"
        if r.speed_bps:
            detail += f" speed={r.speed_bps / 1024:.0f}KB/s"
        if r.latency_ms is not None:
            detail += f" latency={r.latency_ms:.0f}ms"
        if r.http_code:
            detail += f" http={r.http_code}"
        if r.message:
            detail += f" msg={r.message}"
        logger.info(f"测速候选 {r.prefix or '（直连）'}: {r.status} {detail}")
        # 进度回调出错（界面那边的 bug）不该毁掉整轮测速
        if on_result is not None:
            try:
                on_result(r)
            except Exception as e:                              # noqa: BLE001
                logger.warning(f"测速进度回调出错（已忽略）: {e}")
        if r.status == STATUS_CANCELED:
            break
    logger.info(f"镜像测速结束：测了 {len(results)}/{len(targets)} 个候选")
    return results


def sorted_results(results: Iterable[MirrorResult]) -> list[MirrorResult]:
    """按「先可用、同状态再按快慢」排序 —— 结果窗和「用最快的」都看这个序。"""
    return sorted(
        results,
        key=lambda r: (
            _STATUS_RANK.get(r.status, 9),
            -(r.speed_bps or 0.0),
            r.prefix,
        ),
    )


def format_size(num_bytes: float) -> str:
    """字节数 → 人看的字符串（1024 进制）。速度/样本量都用它。"""
    value = float(num_bytes)
    unit = "B"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} B"
    return f"{value:.1f} {unit}"


def format_speed(bps: float | None) -> str:
    """速度 → 人看的字符串；没有数据给 ``—``（界面里显示成「没测到」）。"""
    if not bps or bps <= 0:
        return "—"
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


def format_latency(ms: float | None) -> str:
    """延迟 → 人看的字符串（超过 1 秒换成秒，免得四个数字挤在一起）。"""
    if ms is None or ms < 0:
        return "—"
    if ms >= 1000:
        return f"{ms / 1000:.1f} s"
    return f"{ms:.0f} ms"
