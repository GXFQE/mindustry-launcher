# -*- coding: utf-8 -*-
"""GitHub 镜像测速 —— 实测每个候选镜像的下载速度，挑最快的那个。

为什么是「实测下载」而不是「ping 一下」
--------------------------------------
镜像站（ghfast.top / gh-proxy.com 之类）快不快，取决于**它自己到 GitHub 的
链路**和当时的 CDN 缓存命中情况，跟「你到它的 RTT」基本没关系 —— 一个
10 ms 就能握手的镜像，回源慢的话下载照样是几十 KB/s。所以唯一算数的指标
就是**真下这个文件时的速度**，这里就真下。

测法（★ **交错多轮**：每轮把所有候选各测一次，而不是把一个测完再测下一个）：
  1. 拼出下载地址（前缀 + 目标文件），发一个普通 GET；
  2. 记下**首字节延迟**（含 DNS + TCP + TLS + 服务器响应，比 ping 更接近
     点开下载时的体感）；
  3. 每次采样读**最多 ``SAMPLE_BYTES`` 字节、最多 ``SAMPLE_SECONDS`` 秒**，
     哪个先到就停 —— 提前关连接，不把整个 jar 拖下来；
     速度 = 读到的字节数 / 实际耗时；
  4. 每个候选重复 ``ROUNDS`` 轮，最后取**中位数**当它的成绩。

★★ 为什么必须「交错 + 多轮 + 取中位」（2026-10-11 实测后改的，别改回去）
---------------------------------------------------------------------
原来是一个候选测完再测下一个、每个只测一次。实测发现**这条路本身就不稳定**：
同一台主机、同一字节区间、背靠背连着测，10 秒内速度就能在 0.26 ~ 3.7 MB/s
之间跳（14 倍），且**没有单调趋势**；同一个镜像间隔几分钟再测，能差 100 倍。
在这个抖动水平下，「顺序 + 单次抽样」有两个致命毛病：

  * **顺序** ⇒ 各候选在**不同时刻**被测，量到的是「当时网络好不好」，
    不是「谁更快」。实测：同一批候选、同样方法跑两轮，**赢家直接翻转**。
  * **单次** ⇒ 报出来的就是**一次抽签**，没有任何平均。

交错让所有候选跨过**同一段**时间窗（网络抖动对大家一视同仁），
多轮取中位再把单次尖峰削掉。三个参数是配套的，别只调一个。

★ 目标文件写死在 ``TEST_URL``：一个**老版本、不会被删**的官方 release 资源。
  所有候选测的是同一个文件，结果才能横向比。
★ 目标文件是 30 MB 级的 jar，而这里每个候选只读 ~1.5 MB × 轮数 ——
  GET 发出后主动断开，镜像那边最多再推一个 TCP 窗口的数据，浪费可以忽略。

对外契约：**这一层绝不抛异常**。连不上、超时、返回错误页…… 全都变成一条
``MirrorResult``（带 status），让界面自己决定怎么显示 —— 测速是辅助功能，
任何一个候选出问题都不该把启动器带崩。回调（``on_result`` / ``on_progress``）
同此底线：跑在调用线程里，抛出的异常被吞并记日志。
"""
from __future__ import annotations

import logging
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import partial
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

# 单次采样**最多**读多少字节。注意它是「上限」不是「目标」：正常情况下
# 候选在 SAMPLE_SECONDS 到点时就停了，只有链路很快时才碰得到它 ——
# 不设上限的话，4 秒能读几十 MB，白花流量。
SAMPLE_BYTES = 1_500_000
# ★ 单次采样最多读多久（秒）。**这才是采样窗口的主控**：
#   * 固定时长 ⇒ 每个候选的平均窗口一样长，才谈得上横向比；
#   * 总耗时 = ROUNDS × SAMPLE_SECONDS × 候选数，**可预测** ——
#     按字节数采样时，慢候选能把每一轮都拖满预算。
#   ⚠️ 别退回「只按字节数」（2026-10-11 之前的写法）：那样慢候选读不满样本，
#   会被判成 STATUS_TIMEOUT 掉进更差的排序档（**跟它实际多快无关**），
#   而快候选只测 0.4 秒就下结论 —— 窗口太短，噪声占满。
SAMPLE_SECONDS = 4.0
# ★ 交错轮数：每轮把所有候选各测一次，最后取中位。见模块 docstring ——
#   这是「排名可信」的关键，别退回「一个测完再测下一个、只测一次」。
ROUNDS = 3
# 读到的量低于这个数就不算「下到了那个文件」：镜像返回的**错误页**（HTML）
# 通常只有几 KB —— 不拦住的话，一个「秒回 502 页面」的镜像会比谁都"快"。
# ⚠️ 它同时是「窗口到时算不算可用」的门槛：256 KB / 4 秒 = 64 KB/s，
#   比这还慢的候选标 TIMEOUT（太慢），不参与正常排序。
MIN_OK_BYTES = 256_000
# 单个候选**所有轮次加一起**的预算（秒）。半死不活的候选别把整轮测速拖成
# 分钟级；额度不够再采一个像样的样本了就不再给它下一轮（已有的样本照样出结果）。
# 18 ≈ ROUNDS(3) × SAMPLE_SECONDS(4) + 握手/收尾的余量 —— 正常候选跑满
# 三轮够用，而三样一起调的时候记得回来看这个数。
MIRROR_BUDGET = 18.0
# socket 级的单次连接/读超时。它管的是「某一次 read 卡住」，总时长由上面的
# 预算兜底（所以一边读一边还要查预算）。
SOCKET_TIMEOUT = 5.0
# 一次最多实测多少个候选：候选来自设置页的下拉清单（config.json 里可手改），
# 写进去 20 个就会一个一个测 —— 太久。截断时要记日志，别让人对着少测的名单
# 猜为什么。
MAX_TARGETS = 12
# 观测到的速度极差超过这个倍数时，界面要提示「波动大，排名仅供参考」。
# 依据：实测同 host 背靠背 10 秒内就能差 14 倍 —— 两个候选的中位速度差不到
# 这个量级时，谁前谁后本来就是抽签，不该让用户以为「最快的」是个定论。
SPREAD_WARN = 3.0
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

    多轮采样时，这里装的是**聚合后**的成绩（见 ``_aggregate_samples``）：
    ``speed_bps`` 是中位数，``samples`` 是参与聚合的样本数，
    ``speed_low_bps`` / ``speed_high_bps`` 是观测到的下限/上限 ——
    界面拿后两者判断「这次排名可信吗」。
    """

    prefix: str
    status: str
    latency_ms: float | None = None    # 首字节延迟（多轮取中位）
    speed_bps: float | None = None     # 读到的字节 / 实际耗时（多轮取中位）
    bytes_read: int = 0
    http_code: int = 0
    message: str = ""
    samples: int = 1                   # 参与聚合的采样次数
    speed_low_bps: float | None = None
    speed_high_bps: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def spread(self) -> float:
        """观测到的速度极差倍数（``max/min``）；样本不足或没波动时给 1.0。

        界面用它提示「波动大，排名仅供参考」—— 在抖动链路上，
        两个候选的中位速度差不到这个倍数时，谁前谁后本来就是抽签。
        """
        lo, hi = self.speed_low_bps, self.speed_high_bps
        if not lo or not hi or lo <= 0 or hi <= lo:
            return 1.0
        return hi / lo


def probe_url(prefix: str, test_url: str = TEST_URL) -> str:
    """拼出真正要请求的地址。空前缀 = 直连（原样返回目标地址）。"""
    return (prefix + test_url) if prefix else test_url


def _stopped(stop_event: Any | None) -> bool:
    """停止信号来了吗。``stop_event`` 只要是「有 is_set()」的东西都收。"""
    return stop_event is not None and stop_event.is_set()


def _safe_call(callback: Callable[..., None], *args: Any) -> None:
    """调一下回调；出错只记日志，不往外抛。

    回调是调用方（界面）的代码：它的 bug 不该把测速本身毁掉 ——
    ``test_mirror`` / ``test_mirrors`` 对外都得守住「不抛」的底线。
    """
    try:
        callback(*args)
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"测速回调出错（已忽略）: {e}")


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
    sample_seconds: float = SAMPLE_SECONDS,
    budget: float = MIRROR_BUDGET,
    socket_timeout: float = SOCKET_TIMEOUT,
    stop_event: Any | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> MirrorResult:
    """实测一个候选（前缀）**一次**。**不抛异常** —— 一切意外都变成一条失败结果。

    采样窗口 = ``min(sample_seconds, budget)`` 秒、字节上限 ``sample_bytes``，
    哪个先到就停（见模块 docstring 讲为什么是固定时长）。

    ``on_progress``（可选）报**字节进度**：先来一条 ``0``（＝这个候选
    开测了，界面靠它把「正在测 X」切过来），之后每读一块报一次累计
    字节数。跟 ``test_mirrors`` 的回调一样跑在**调用线程**、出错被吞。

    参数给的是默认值，测试会用更小的样本/预算 + 本地 HTTP 服务来跑，
    所以别再写第二套实现（见 ``_tools/verify/code_regression.py``）。
    """
    if _stopped(stop_event):
        return MirrorResult(prefix, STATUS_CANCELED)
    url = probe_url(prefix, test_url)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    # 「开测了」要发在请求之前：连接阶段（DNS/TCP/TLS）界面也得有东西
    # 可显示 —— 不然慢镜像握手那几秒里进度条和文字全是定格。
    if on_progress is not None:
        _safe_call(on_progress, 0)
    t0 = time.perf_counter()
    received = 0
    window = min(sample_seconds, budget)
    first_byte_at: float | None = None
    truncated = False       # 窗口用尽 —— 没读满样本
    canceled = False
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
                if time.perf_counter() - t0 > window:
                    truncated = True
                    break
                chunk = resp.read(_READ_CHUNK)
                if not chunk:
                    break           # 服务器提前关了（文件比样本小 / 被截断）
                if first_byte_at is None:
                    first_byte_at = time.perf_counter()
                received += len(chunk)
                if on_progress is not None:
                    _safe_call(on_progress, received)
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

    # ★ 判据是「这次采样能不能得出一个可用速率」，而不是「有没有读满那块
    #   固定字节数」。后者会把慢候选一律打成 TIMEOUT，于是排序先按状态分档、
    #   档内才比速度 —— 慢候选掉进更差的档，**跟它实际多快无关**。
    #   （2026-10-11 实测：镜像普遍 0.07~0.2 MB/s，而读满 1.5 MB 的门槛是
    #   0.125 MB/s ⇒ 那时很多候选根本没读满，排名主要看「凑没凑够字节」。）
    if canceled:
        status = STATUS_CANCELED
    elif received >= sample_bytes or received >= MIN_OK_BYTES:
        # 读满了本次上限，或者窗口到时但已读到够量 —— 两种都算「速率可用」。
        status = STATUS_OK
    elif truncated:
        status = STATUS_TIMEOUT
    else:
        status = STATUS_SHORT
    return MirrorResult(
        prefix, status,
        latency_ms=latency_ms, speed_bps=speed,
        bytes_read=received, message=message,
    )


# 一条候选「已经死透」的状态：再给它一轮也是白花时间，直接退出后续轮次。
# （TIMEOUT 不在里面 —— 那只是「慢」，多采几次正是我们要的。）
_DEAD_STATUSES = frozenset({STATUS_HTTP, STATUS_NET, STATUS_SHORT, STATUS_CANCELED})


def _aggregate_samples(prefix: str, samples: Sequence[MirrorResult]) -> MirrorResult:
    """把同一候选的多轮采样合成一条结果（**取中位**）。

    ★ 为什么是中位，不是平均、更不是最大：
      * 取最大 = 挑「运气最好的那一次」—— 而我们恰恰是要把运气去掉；
      * 取平均会被单次离群值拉扯（实测出现过 10 秒内 14 倍的跳变）；
      * 中位对离群值不敏感，代表**典型情况** —— 那才是用户真去下那个
        87 MB 的 jar 时会遇到的。
    没采到可用样本时返回最后一次采样（大概率是一条失败状态）。

    ★ 上下限（``speed_low/high_bps``）取**全部有速度的样本**，连 timeout
      一起算 —— timeout 的「读不完」也是用实际读到的部分算的速度，正是
      「这个候选能慢到什么程度」的直接观测。
      ⚠️ 只用 ok 样本会把**最典型的不稳**过滤掉：实测本机链路常在
      「一轮快（ok）、一轮慢得读不够（timeout）」之间交替，慢的那轮根本
      进不了 ok 样本 —— spread 反而显示 1.0，「波动大」的提示永远不出现。
      中位数仍然只由 ok 样本定（那是「典型速度」），两者别混。
    """
    usable = [r for r in samples if r.ok and r.speed_bps]
    if not usable:
        return samples[-1] if samples else MirrorResult(prefix, STATUS_CANCELED)
    rates = sorted(r.speed_bps for r in usable)
    lats = sorted(r.latency_ms for r in usable if r.latency_ms is not None)
    rated = sorted(r.speed_bps for r in samples if r.speed_bps)
    return MirrorResult(
        prefix,
        STATUS_OK,
        latency_ms=(lats[len(lats) // 2] if lats else None),
        # ★ 偶数个样本时取**下中位**（第 (n-1)//2 个）—— 宁可保守：报的是
        #   「用户可能遇到的典型速度」，低估比高估好（高估会让用户照着
        #   选一个其实更慢的镜像）。3 样本是常态（ROUNDS=3），不受影响。
        speed_bps=rates[(len(rates) - 1) // 2],
        bytes_read=sum(r.bytes_read for r in samples),
        samples=len(usable),
        speed_low_bps=rated[0] if rated else None,
        speed_high_bps=rated[-1] if rated else None,
    )


def test_mirrors(
    prefixes: Sequence[str],
    *,
    on_result: Callable[[MirrorResult], None] | None = None,
    stop_event: Any | None = None,
    test_url: str = TEST_URL,
    sample_bytes: int = SAMPLE_BYTES,
    sample_seconds: float = SAMPLE_SECONDS,
    budget: float = MIRROR_BUDGET,
    socket_timeout: float = SOCKET_TIMEOUT,
    on_progress: Callable[[str, int], None] | None = None,
    rounds: int = ROUNDS,
) -> list[MirrorResult]:
    """实测一串候选，**每个候选返回一条聚合结果**（被取消时可能不满）。

    ★ 采样顺序是**交错**的：第 1 轮把所有候选挨个测一遍，再回来测第 2 轮……
    而不是「把 A 测完三次再去测 B」。理由见模块 docstring —— 顺序测量在
    抖动链路上量到的是「当时网络好不好」，实测过**赢家会翻转**。
    ⇒ 因此 ``on_result`` 是在**所有轮次跑完之后**每个候选各报一次，
    中途不会逐条冒出来；实时反馈看 ``on_progress``。

    ``on_progress(prefix, nbytes)``（可选）是**字节级**进度：每次采样先来
    一条 ``0``，之后按块报 —— 界面拿它画「第几轮 + 测到谁 + 读到哪」。
    ★ 两个回调都跑在**调用线程**里（不是 Tk 主线程）—— 界面那边要自己
    ``run_on_gui`` 转回去再碰控件；回调抛异常会被吞并记日志。
    """
    targets = list(prefixes)
    if len(targets) > MAX_TARGETS:
        logger.warning(
            f"测速候选有 {len(targets)} 个，只测前 {MAX_TARGETS} 个"
            "（清单来自 config.json 的 github_mirror_presets）"
        )
        targets = targets[:MAX_TARGETS]

    # ★ 按**下标**记账，不按前缀 —— 候选清单允许重复（测试就传了
    #   ``[base] * 3``），用前缀当键会把它们塌成一个。
    count = len(targets)
    buckets: list[list[MirrorResult]] = [[] for _ in range(count)]
    alive = [True] * count
    spent = [0.0] * count
    canceled = False

    for rnd in range(1, max(1, rounds) + 1):
        for i, prefix in enumerate(targets):
            if canceled or _stopped(stop_event):
                canceled = True
                break
            if not alive[i]:
                continue
            left = budget - spent[i]
            if left < sample_seconds * 0.5:
                # 额度不够再采一个像样的样本了：收手，已有的样本照样出结果。
                # （不拿剩下的零点几秒硬采 —— 那种样本窗口太短，只会添噪声。）
                alive[i] = False
                continue
            # 把候选前缀缝进单候选的进度回调里：界面只认 (前缀, 字节) 一种
            # 事件（用 partial 而不是闭包 lambda —— 循环变量不会晚绑定）。
            cb = (None if on_progress is None
                  else partial(_safe_call, on_progress, prefix))
            t0 = time.perf_counter()
            r = test_mirror(
                prefix,
                test_url=test_url,
                sample_bytes=sample_bytes,
                sample_seconds=min(sample_seconds, left),
                budget=left,
                socket_timeout=socket_timeout,
                stop_event=stop_event,
                on_progress=cb,
            )
            spent[i] += time.perf_counter() - t0
            buckets[i].append(r)
            logger.debug(
                f"第 {rnd}/{rounds} 轮 · {prefix or '（直连）'}: {r.status} "
                f"bytes={r.bytes_read}"
                + (f" speed={r.speed_bps / 1024:.0f}KB/s" if r.speed_bps else "")
            )
            if r.status in _DEAD_STATUSES:
                # 明确死掉了（404 / 连不上 / 错误页）：重试纯属浪费额度
                alive[i] = False
            if r.status == STATUS_CANCELED:
                canceled = True
                break

    results: list[MirrorResult] = []
    for i, prefix in enumerate(targets):
        if not buckets[i]:
            continue
        agg = _aggregate_samples(prefix, buckets[i])
        results.append(agg)
        # 每个候选留一条日志：测速结果直接影响用户改镜像的选择，
        # 事后要能对着日志复盘（界面上的结果窗关掉就没了）。
        detail = f"bytes={agg.bytes_read} samples={agg.samples}"
        if agg.speed_bps:
            detail += f" speed={agg.speed_bps / 1024:.0f}KB/s"
            if agg.spread > 1.0:
                detail += f" spread={agg.spread:.1f}x"
        if agg.latency_ms is not None:
            detail += f" latency={agg.latency_ms:.0f}ms"
        if agg.http_code:
            detail += f" http={agg.http_code}"
        if agg.message:
            detail += f" msg={agg.message}"
        logger.info(f"测速候选 {prefix or '（直连）'}: {agg.status} {detail}")
        # 回调出错（界面那边的 bug）不该毁掉整轮测速 —— 见 _safe_call
        if on_result is not None:
            _safe_call(on_result, agg)

    logger.info(
        f"镜像测速结束：{len(results)}/{len(targets)} 个候选，"
        f"每个最多 {max(1, rounds)} 轮 × {sample_seconds:g}s（交错采样）"
    )
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
