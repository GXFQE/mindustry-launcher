# -*- coding: utf-8 -*-
"""把话说给「敲命令的那个控制台」。

为什么需要这个模块
------------------
打包出来的是**窗口子系统**程序（`Launcher.spec` 里 ``console=False``）——
这是对的：双击时不该先弹一个黑框。代价是它**天生没有控制台**：
``sys.stdout`` 是 None，``print()`` 被 Python 直接丢弃（不报错，所以这件事
很难被发现 —— 源码里跑得好好的，打包后一句话都看不见）。

而命令行用法（``--launch``）恰恰需要输出：用户是在控制台里敲的命令，结果
就该回到那个控制台。做法是 Windows 的标准套路：

  ① ``AttachConsole(ATTACH_PARENT_PROCESS)`` —— 附到**父进程**的控制台，
     而不是 ``AllocConsole()`` 新建一个黑框（那又会弹一个窗口出来）；
  ② 打开 ``CONOUT$`` 当输出流，顺手把 ``sys.stdout`` / ``sys.stderr`` 也
     指过去：这样 ``print``、以及最后没接住的那个 traceback，才有地方去；
  ③ 编码按 ``GetConsoleOutputCP()`` 选 —— 中文控制台是 936，直接按 UTF-8
     写会变成一片乱码。

★ 拿不到控制台（双击快捷方式、父进程自己也是个 GUI 程序）是**正常情况**，
  不是错误：``attach()`` 安静地返回 False，``say()`` 之后全是空操作，
  由调用方决定退化成什么（见 ``CoreMixin._cli_note``）。

★ 只 import 标准库。非 Windows 上 ``attach()`` 直接返回 False。
"""
import codecs
import logging
import sys
import threading

logger = logging.getLogger(__name__)

#: ``AttachConsole`` 的约定值：附到父进程的控制台（不是新建一个）
ATTACH_PARENT_PROCESS = -1
#: 控制台设备名 —— 打开它写就是写到当前控制台
CONOUT = "CONOUT$"

#: 接上以后的那个文本流；None = 没接上（``say()`` 空操作）
_stream = None
#: 会说话的不止一个线程（启动在 GUI 线程、启动结果与收尾在工作线程），
#: 不加锁的话两句话会在控制台里绞在一起
_lock = threading.Lock()


def _console_encoding() -> str:
    """控制台现在用哪个代码页，就用哪个写；问不出来退 UTF-8。

    ★ 中文控制台默认是 936(GBK)，此时按 UTF-8 写中文 = 一片乱码；反过来
      在 ``chcp 65001`` 的控制台上按 GBK 写也一样。所以每次都问一次系统。
      问不出来、或者 Python 不认那个代码页，就退 UTF-8 —— 宁可个别字变
      成问号，也不能因为一个字符把整条输出吞掉（``errors="replace"``）。
    """
    name = "utf-8"
    try:
        import ctypes

        cp = int(ctypes.windll.kernel32.GetConsoleOutputCP() or 0)
        if cp > 0:
            name = f"cp{cp}"
    except Exception as e:                                          # noqa: BLE001
        logger.debug(f"[控制台] 问不到代码页（按 utf-8 处理）: {e}")
        return "utf-8"
    try:
        codecs.lookup(name)
    except LookupError:
        logger.debug(f"[控制台] Python 不认代码页 {name}（按 utf-8 处理）")
        return "utf-8"
    return name


def attach() -> bool:
    """把自己接到**父进程**的控制台上。接上了返回 True（之后 ``say()`` 有效）。

    ★ 只在命令行用法里调（见 ``MindustryLauncher.main``）：双击启动那条路
      不需要控制台，也不该去动进程的 std 流（那会让本来安静的进程开始往
      某个看不见的地方写东西）。
    """
    global _stream
    if sys.platform != "win32":
        return False
    with _lock:
        if _stream is not None:
            return True
        try:
            import ctypes

            k32 = ctypes.windll.kernel32
            # 没有父控制台是**正常失败**：安静地返回 False，别弹窗、别新建。
            if not k32.AttachConsole(ATTACH_PARENT_PROCESS):
                logger.debug("[控制台] 父进程没有控制台，输出只进日志")
                return False
            stream = open(
                CONOUT, "w",
                encoding=_console_encoding(), errors="replace", buffering=1,
            )
        except Exception as e:                                      # noqa: BLE001
            logger.debug(f"[控制台] 接入失败（忽略）: {e}")
            return False
        _stream = stream
        # 顺手把 std 流也接过去：打包成窗口子系统时 sys.stdout 是 None，
        # 接上以后 print 与未接住的 traceback 才有地方去。
        sys.stdout = stream
        sys.stderr = stream
        logger.debug("[控制台] 已接入父进程控制台")
        return True


def attached() -> bool:
    """接上控制台了吗（测试/分支用）。"""
    return _stream is not None


def say(text: str = "") -> None:
    """往控制台写一行。没接上、或者控制台已经被关掉，就什么都不做。

    ★ ``print`` 是故意的：流是我们自己开的那个，不经过 ``sys.stdout``
      （那个可能是 None，也可能被谁换过）。
    """
    with _lock:
        if _stream is None:
            return
        try:
            print(text, file=_stream, flush=True)
        except (OSError, ValueError) as e:
            # 用户把那个控制台窗口关了 —— 后面再也写不进去，只留个记录。
            # 不在这里清 _stream：那等于顺手把 sys.stdout 变成悬空引用。
            logger.debug(f"[控制台] 写入失败（控制台可能已关闭）: {e}")
