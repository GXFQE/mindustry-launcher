# -*- coding: utf-8 -*-
"""游戏输出日志：把子进程的 stdout/stderr 接住，落盘 + 内存回看。

原来 Popen 用的是 ``stdout=DEVNULL, stderr=DEVNULL`` —— 游戏崩了、卡住了，
在启动器这边**一点线索都没有**，只能靠猜。接上以后有两个用处：
  1) 窗口里能看着它实时滚（查 mod 冲突、显存不足最有效）；
  2) 每次启动落一份文件，事后还能翻。

⚠️ 两条必须守住的实现约束：

  * **管道必须一直有人读**。子进程往没人读的管道里写，写满 64 KB 缓冲区
    就会永久阻塞 —— 表现是「游戏卡在一片黑里不进主菜单」，而且极难复现。
    所以读线程是 daemon + ``for line in stream`` 死磕到底，异常也要吞掉
    继续读（读线程一死就等于把游戏顶死了）。
  * **解码不能抛异常、也不能丢字节**。管道是按字节读进来的，解码一律走
    ``decode_output_line``：先按 UTF-8（启动命令里钉了这个，见 gamecmd），
    解不开再退系统 ANSI 代码页，**每一步都带 errors="replace"**。严格解码
    撞上一个怪字节就抛 UnicodeDecodeError，等于上面那条挂掉。
"""
import collections
import locale
import logging
import os
import threading
import time
from pathlib import Path
from typing import IO, Any

from .config import delete_path

logger = logging.getLogger(__name__)

LOG_DIR_NAME = "logs"
LOG_PREFIX = "game-"


def fallback_encoding() -> str:
    """管道里出现非 UTF-8 字节时用的兜底编码。

    Windows 上「没有控制台」的 Java（我们就是用管道接它的输出）把
    System.out 按 ``native.encoding`` 写 —— 简中系统上是 GBK，所以兜底
    必须是系统 ANSI 代码页。``mbcs`` 在 Windows 上永远等于那个代码页。

    ⚠️ **别用 ``locale.getpreferredencoding()``**：Python 一旦开了 UTF-8
    模式（设了 ``PYTHONUTF8=1``、或 3.15 起的默认行为）它会返回
    ``'utf-8'``，兜底等于没兜。
    """
    if os.name == "nt":
        return "mbcs"
    return locale.getpreferredencoding(False)


def decode_output_line(raw: bytes | str) -> str:
    """把管道里出来的一行解成文本，顺手去掉行尾换行。

    正常路径是 UTF-8：启动命令里固定带了 ``-Dstdout.encoding=UTF-8``
    （见 gamecmd），游戏就是按 UTF-8 往管道里写的。但**旧 JRE 不认那两个
    属性**（Java 19 才引入），它会继续按 GBK 写 —— 那时硬按 UTF-8 解会把
    中文整片撕成 U+FFFD，所以这里退一步用系统代码页重解。

    ★ 只能「UTF-8 优先、失败才退」，**不能反过来**：GBK 编码的中文几乎
      总能被当成某个字符解出来，先试 GBK 会把本来正常的 UTF-8 日志毁掉。

    也接受 str：万一以后有人把管道换回 ``text=True``，这里不能因为
    ``str.rstrip(b"...")`` 这种类型错抛异常 —— 读线程一死管道就没人排空，
    游戏写满 64 KB 缓冲会卡住（那是最坏的结果）。
    """
    if isinstance(raw, bytes):
        if not raw:
            return ""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode(fallback_encoding(), errors="replace")
    else:
        text = raw                      # 已经是文本，别再解第二次
    return text.rstrip("\r\n")

class GameLog:
    """一次游戏会话的输出收集。

    生命周期：``start(proc)`` 挂上去 → 读线程一直读到管道 EOF →
    ``close()`` 收尾。落盘与否由 ``save_to_file`` 决定（设置里可关），
    关掉时窗口里照样能实时看。
    """

    # 内存里给窗口留最近多少行。游戏一局下来通常几百行，
    # 刷 mod 列表那种会到几千行，5000 足够回看又不至于吃内存。
    MAX_LINES = 5000
    # 落盘保留多少份的**兜底值**；实际用设置项 max_log_files（见 _prune）。
    # 留这个常量是为了让不传参数的调用（测试/脚本）也有个确定行为。
    KEEP_FILES = 20

    def __init__(
        self,
        base_dir: Path,
        save_to_file: bool = True,
        keep_files: int = KEEP_FILES,
        permanent_delete: bool = False,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.save_to_file = bool(save_to_file)
        # 调用方给什么用什么都可能：0 会让刚写的这份被自己删掉，
        # 所以这里至少留 1 份。
        self.keep_files = max(1, int(keep_files))
        # 超出保留份数的旧日志怎么删：默认进回收站（可还原），
        # 设置里把 permanent_delete 打开才是直接删（见 config.delete_path）。
        self.permanent_delete = bool(permanent_delete)
        self._lock = threading.Lock()
        self._lines: collections.deque[tuple[int, str]] = collections.deque(
            maxlen=self.MAX_LINES
        )
        self._seq = 0                      # 收到过的总行数（单调递增）
        self._path: Path | None = None
        self._file: IO[str] | None = None
        self._reader: threading.Thread | None = None
        self._finished = threading.Event()
        self._finished.set()               # set = 没有正在收的会话
        self._dropped = 0                  # 被 maxlen 挤掉的行数

    # ---------- 给界面看的 ----------

    @property
    def path(self) -> Path | None:
        """本次会话的日志文件；没落盘或还没启动时为 None。"""
        return self._path

    @property
    def active(self) -> bool:
        """还有会话在收输出吗。"""
        return not self._finished.is_set()

    def snapshot(self) -> tuple[int, int, int, list[str]]:
        """返回 ``(最新序号, 最早序号, 被挤掉的行数, 当前缓存的行)``。

        界面对照着上一次渲染到的序号取增量；一旦发现最早序号 > 上次序号 + 1，
        说明中间有行被 maxlen 挤掉了，界面得整体重画（见 gui_log）。
        """
        with self._lock:
            if not self._lines:
                return self._seq, self._seq + 1, self._dropped, []
            first = self._lines[0][0]
            return self._seq, first, self._dropped, [t for _, t in self._lines]

    def tail(self, count: int = 200) -> list[str]:
        with self._lock:
            return [t for _, t in list(self._lines)[-count:]]

    # ---------- 会话管理 ----------

    def start(self, proc: Any) -> None:
        """挂到一个刚拉起的进程上开始收输出。"""
        stream = getattr(proc, "stdout", None)
        if stream is None:
            # 没接管道（理论上不会走到），别把状态搞成「一直在收」
            logger.debug("进程没有可读的输出管道，跳过日志收集")
            return
        if self.active:
            logger.debug("上一次会话还没收完，先收尾再开新的")
            self.close()
        self._seq = 0
        self._dropped = 0
        with self._lock:
            self._lines.clear()
        self._finished.clear()
        if self.save_to_file:
            self._open_file()
        self._reader = threading.Thread(
            target=self._pump, args=(stream,), daemon=True,
            name="mdt-gamelog",
        )
        self._reader.start()
        logger.info(
            "游戏输出已开始收集"
            + (f" -> {self._path}" if self._path else "（未落盘，仅在窗口显示）")
        )

    def close(self) -> None:
        """收尾：关文件。不碰管道 —— 管道归 subprocess 的进程对象管。

        可以被重复调用（游戏正常退出、启动器退出都各调一次）。
        """
        self._close_file()

    # ---------- 内部 ----------

    def _open_file(self) -> None:
        try:
            d = self.base_dir / LOG_DIR_NAME
            d.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = d / f"{LOG_PREFIX}{stamp}.log"
            n = 1
            # 同一秒内起两次（连点、测试）不要互相覆盖
            while path.exists():
                n += 1
                path = d / f"{LOG_PREFIX}{stamp}-{n}.log"
            # buffering=1 = 行缓冲：游戏被硬杀时也能保住最后几行
            self._file = path.open(
                "w", encoding="utf-8", errors="replace", buffering=1
            )
            self._path = path
        except OSError as e:
            logger.warning(f"游戏输出无法落盘，改为只在窗口里显示: {e}")
            self._file = None
            self._path = None
            return
        self._prune(d, path)

    def _prune(self, d: Path, keep: Path) -> None:
        """只保留最近 ``self.keep_files`` 份，更旧的删掉。

        默认走回收站（``config.delete_path``）：项目默认铁律是「删任何东西走
        回收站」。代价是每次启动最多多一次 SHFileOperation（一份几百 KB 的
        小文件，几十毫秒），而且这一步发生在**游戏进程已经拉起来之后**，
        不占启动的关键路径。设置里把 ``permanent_delete`` 打开则直接删
        （旧日志本来就不值钱，磁盘紧张时不想让回收站再占一份）。
        """
        try:
            files = sorted(
                (p for p in d.glob(f"{LOG_PREFIX}*.log") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError as e:
            logger.debug(f"列旧游戏日志失败，跳过清理: {e}")
            return
        for old in files[self.keep_files:]:
            if old == keep:
                continue
            try:
                delete_path(old, permanent=self.permanent_delete)
                logger.debug(
                    f"旧游戏日志已"
                    f"{'直接删除' if self.permanent_delete else '移入回收站'}: "
                    f"{old.name}"
                )
            except OSError as e:
                # 清不动就算了，绝不能因为清日志把这次启动搞失败
                logger.debug(f"清理旧游戏日志失败 {old.name}: {e}")
                break

    def _pump(self, stream: IO[bytes]) -> None:
        try:
            for raw in stream:
                # 管道是二进制的（见 gui_game 的 Popen）：在这里按行切开再解码，
                # 而不是把编码钉在 Popen 上 —— 游戏用哪种编码写，得看它
                # JRE 的脸色（见 decode_output_line）。
                self._add(decode_output_line(raw))
        except Exception as e:                             # noqa: BLE001
            # ★ 这里绝不能只接 OSError —— 读线程一旦退出，管道就没人排空，
            #   游戏写满缓冲区后会永久卡死。出任何问题都当成一行日志记下来
            #   继续读。
            self._add(f"[启动器] 读取游戏输出中断: {type(e).__name__}: {e}")
        finally:
            self._close_file()
            self._finished.set()
            with self._lock:
                total, kept = self._seq, len(self._lines)
            logger.info(f"游戏输出收集结束（{total} 行，缓存 {kept} 行）")

    def _add(self, line: str) -> None:
        with self._lock:
            self._seq += 1
            if len(self._lines) == self._lines.maxlen:
                self._dropped += 1
            self._lines.append((self._seq, line))
            if self._file is not None:
                try:
                    self._file.write(line + "\n")
                except (OSError, ValueError) as e:
                    # 磁盘满 / 文件被占：放弃落盘，但窗口里还得继续显示
                    logger.warning(f"写游戏日志失败，改为只在窗口显示: {e}")
                    self._close_file_locked()

    def _close_file(self) -> None:
        with self._lock:
            self._close_file_locked()

    def _close_file_locked(self) -> None:
        f, self._file = self._file, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
