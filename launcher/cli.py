# -*- coding: utf-8 -*-
"""启动器**自己**的命令行参数 —— 不是游戏参数（游戏那个在 gamecmd.py）。

为什么单独一个模块
------------------
入口 ``MindustryLauncher.py`` 只是个壳（它还得兼「更新执行体」那条分支，
见那里 docstring），而「怎么从命令行把它用起来」是独立的一件事，值得能单独
测：本模块**只 import 标准库**，几个函数都是纯函数（不读文件、不碰 tkinter、
不 import 包内模块），``_tools/verify/code_regression.py`` 直接喂列表进去
断言，不用建窗口、不用真实数据。

目前只有一个开关：``--launch <版本>``
-------------------------------------
让启动器打开后**直接**启动指定版本，省掉「选中 → 点启动 → 点确认」。
版本名就是主界面列表里显示的那一行（如 ``Mindustry v146`` / ``BE v146``）；
也可以只写版本号本身（如 ``v146``），前提是它只属于一个类型。

★ 匹配刻意**只做精确匹配**（忽略大小写、忽略两端空白），不做前缀/模糊匹配：
  版本决定用哪一套存档目录，猜一个「差不多的」比找不到更坏 —— 找不到只是
  没启动，猜错是往别的存档目录里写了一局。撞名（同一个版本号出现在多个
  类型里）同样不猜，原样把候选交给调用方去提示。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 直接启动指定版本的开关 —— 对外承诺的参数名，文档（README / 使用说明.txt）
# 和它必须一致。
LAUNCH_FLAG = "--launch"

# 「可用版本」列出来时最多显示几个：多了就是把整个版本列表糊进弹窗。
AVAILABLE_LIMIT = 12

# ------------------------------------------------------------------ 退出码
#
# 命令行用法下 ``%ERRORLEVEL%`` / ``$?`` 拿到的东西。脚本里靠它判断要不要
# 报警、要不要重试 —— 而**不能**靠控制台里那几行字（那是给人看的，会被
# 翻译、会换行）。正常收工（含游戏玩完退出）一律 0：把游戏拉起来就算成功，
# 玩多久、玩不玩是用户的事。
EXIT_OK = 0
#: 启动器自己崩了（连窗口都没起来）—— 只有入口的兜底 except 会给这个
EXIT_CRASH = 1
#: 参数写坏：``--launch`` 后面没跟版本名
EXIT_BAD_USAGE = 2
#: 没找到这个版本（含「版本号撞名，不知道要哪个」）
EXIT_NO_VERSION = 3
#: 版本找到了，但游戏没起来（拼 JAR / 拉 Java 进程那一步失败）
EXIT_LAUNCH_FAILED = 4


@dataclass(frozen=True)
class CliRequest:
    """命令行解析结果。不可变 —— 解析一次，之后谁都不该改它。"""

    #: 要直接启动的版本名（用户原样输入、已去空白，尚未匹配）；空串 = 没要求
    launch: str = ""
    #: 写法不完整（``--launch`` 后面没跟值）。只在**一个有效值都没有**时为真
    bad_usage: bool = False

    @property
    def used(self) -> bool:
        """用户用了命令行开关吗（写坏了也算）。

        它决定两件事：**不建可见窗口**（他要的是「直接进游戏」，不是看着
        启动器）和 **把话说给控制台**（他是在控制台敲的命令）——
        见 ``CoreMixin.__init__`` 与 ``MindustryLauncher.main``。
        """
        return bool(self.launch or self.bad_usage)


def parse_cli(argv: list[str]) -> CliRequest:
    """从 ``argv`` 里挑出我们自己认识的参数。不认识的**一律忽略**。

    ★ 不认识的参数不许报错：这个 exe 会被快捷方式、脚本、以及它自己的兄弟
      分支（``--debug``、``--apply-update <计划文件>``）拉起来，为一个多余
      参数弹窗说「参数错误」只会让人莫名其妙。

    认的写法两种：``--launch <值>`` 和 ``--launch=<值>``（很多工具的直觉写法）。
    重复给时取**最后一个有效值**；中间写坏过一次、但另一次给对了，按对的来。
    """
    launch = ""
    incomplete = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == LAUNCH_FLAG:
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            # 后面没跟值、跟的是另一个开关（``--launch --debug``）、或者是个
            # 空白串，都算「写了一半」—— 不能把 ``--debug`` 当成版本名。
            if nxt and not nxt.startswith("--") and nxt.strip():
                launch = nxt.strip()
                i += 2
            else:
                incomplete = True
                i += 1
            continue
        if arg.startswith(LAUNCH_FLAG + "="):
            value = arg[len(LAUNCH_FLAG) + 1:].strip()
            if value:
                launch = value
            else:
                incomplete = True
            i += 1
            continue
        i += 1
    return CliRequest(
        launch=launch,
        bad_usage=incomplete and not launch,
    )


def match_version(
    versions: list[dict[str, Any]], query: str
) -> tuple[dict[str, Any] | None, list[str]]:
    """按名字找版本，返回 ``(命中的版本 | None, 候选名清单)``。

    规则按顺序，取第一个成立的：

    1. **显示名**精确相同（不分大小写、忽略两端空白）—— 就是列表里那一行；
    2. 否则**原始版本号**精确相同（``v146`` 这种，即不带类型前缀的写法）。

    返回的两个值合起来有三种含义：

    * ``(版本, [])`` —— 找到了，唯一；
    * ``(None, [名字…])`` —— 撞名：这个版本号在多个类型里都有，**不猜**，
      交给调用方提示用户写全名；
    * ``(None, [])`` —— 没有这个版本。

    两种都返回 None 时，调用方拿 ``available_names()`` 列可用的给用户看。
    """
    q = (query or "").strip().casefold()
    if not q:
        return None, []
    same_name = [
        v for v in versions if str(v.get("name", "")).strip().casefold() == q
    ]
    if len(same_name) == 1:
        return same_name[0], []
    if len(same_name) > 1:
        return None, [str(v.get("name", "")) for v in same_name]
    same_raw = [
        v for v in versions
        if str(v.get("raw_version", "")).strip().casefold() == q
    ]
    if len(same_raw) == 1:
        return same_raw[0], []
    return None, [str(v.get("name", "")) for v in same_raw]


def available_names(
    versions: list[dict[str, Any]], limit: int = AVAILABLE_LIMIT
) -> tuple[list[str], int]:
    """可用的版本名（最多 ``limit`` 个）＋ 没列出来的个数。"""
    names = [str(v.get("name", "")) for v in versions]
    return names[:limit], max(0, len(names) - limit)
