# -*- coding: utf-8 -*-
"""配置写盘用的**进程间**互斥锁（不是线程锁）。

为什么需要它
------------
设置是「启动时读进内存、退出时整个写回」的（见 ``gui_updates.on_closing`` 里
那句 ``config.save()``）。于是有两条会互相打架的路径：

* 启动器开着的时候在**命令行**改了一项 → 用户一关启动器，那份旧内存把改动
  **静默覆盖**掉，而且没有任何提示（2026-10-17 实测复现）；
* 反方向同理：命令行刚改完，界面上点一次「保存并返回」也会盖掉它。

所以 ``--set`` 必须在动手之前先确认「没有启动器正开着」。判断方式不能靠
进程名（源码运行、改名、多个安装目录都会误判），而是**让启动器自己占一把锁**：
它在启动时尝试拿到这把锁、一直持有到进程结束；``--set`` 拿不到就是有实例在跑。

锁文件放在**系统临时目录**，不放进数据根
----------------------------------------
理由有两条：项目根约定只放「程序 + 文档 + LICENSE」，让一个运行期文件出现在
那儿很碍眼；而且数据根可能在只读位置（U 盘、共享盘），临时目录总是可写。

文件名带上**数据根的哈希**：同一台机器上装了两份启动器（比如开发版和日常版）
各有各的锁，互不干扰 —— 它们改的本来就是两份 config.json。

Windows 用 ``msvcrt.locking``、其它平台用 ``fcntl.flock``，都是**非阻塞**的
一次性尝试：拿不到就是拿不到，绝不等待（等待会让命令行卡在一个看不见的窗口
后面，比报错难查得多）。进程不管怎么死（崩了、被 taskkill），系统都会释放，
所以**不存在需要手动清理的残留锁文件** —— 这一点是「用文件存在与否当锁」的
做法做不到的。
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: 锁文件名前缀（后面接数据根哈希）。想手工排查时按这个前缀去 %TEMP% 找。
LOCK_PREFIX = "mdt-launcher-"
#: 锁住几个字节。1 个就够 —— 这类锁是「有没有人占着」的旗子，不是数据。
_LOCK_BYTES = 1


def lock_file(base_dir: Path) -> Path:
    """某份安装（数据根）对应的锁文件路径。

    ★ 哈希前先 ``resolve()`` + 小写：Windows 路径不分大小写，``D:\\A`` 和
      ``d:\\a`` 必须是同一把锁，否则两份安装各自锁各自的、等于没锁。
    """
    try:
        key = str(Path(base_dir).resolve()).lower()
    except OSError:                                   # pragma: no cover
        key = str(base_dir).lower()
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"{LOCK_PREFIX}{digest}.lock"


class AppLock:
    """一把「谁在运行」的锁。``acquire()`` 拿不到时**不抛异常**，只是 ``held`` 为假。

    典型用法（启动器）::

        self.app_lock = applock.acquire(base_dir)   # 拿不到就只记一条日志
        ...
        self.app_lock.release()                     # 退出时（可选，进程结束也会放）

    命令行那边是 ``AppLock(lock_file(...)).acquire()``，然后看 ``held``。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        #: 拿不到锁的原因：``"busy"`` = 别人占着；其它 = 这一串错误说明。
        #: 命令行靠它区分「先关掉启动器」和「临时目录写不了」—— 两种情况的
        #: 处理办法完全不同，说错了用户只会白折腾。
        self.reason: str = ""

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "AppLock":
        """非阻塞地尝试占锁。失败（别人占着 / 磁盘写不了）都只返回自己。"""
        if self._fd is not None:
            return self
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            logger.warning(f"锁文件打不开（本次不占锁）：{self.path}：{e}")
            self.reason = str(e)
            return self
        try:
            # 顺手把自己的 pid 写进去：锁文件本身也能当「谁在跑」的线索
            #（排查时看一眼就行，读它不能代替拿锁）。
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.lseek(fd, 0, os.SEEK_SET)
            _lock(fd)
        except OSError as e:
            # 被别的实例占着 —— 这是**正常情况**，不是错误（--set 会据此答复用户）
            logger.debug(f"配置锁被占用：{self.path}：{e}")
            self.reason = "busy"
            os.close(fd)
            return self
        self._fd = fd
        self.reason = ""
        logger.debug(f"已占住配置锁：{self.path}")
        return self

    def release(self) -> None:
        """放锁。可以重复调用（``on_closing`` 有可能被走两次）。"""
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        except OSError as e:                          # pragma: no cover
            logger.debug(f"解锁失败（进程退出时系统也会放）：{e}")
        try:
            os.close(self._fd)
        except OSError:                               # pragma: no cover
            pass
        self._fd = None


def _lock(fd: int) -> None:
    """占住 ``fd`` 的第一个字节；占用时抛 ``OSError``。"""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_BYTES)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_BYTES)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def acquire(base_dir: Path) -> AppLock:
    """``AppLock(lock_file(base_dir)).acquire()`` 的短写法。"""
    return AppLock(lock_file(base_dir)).acquire()
