# -*- coding: utf-8 -*-
"""启动器**自己**的命令行参数 —— 不是游戏参数（游戏那个在 gamecmd.py）。

为什么单独一个模块
------------------
入口 ``MindustryLauncher.py`` 只是个壳（它还得兼「更新执行体」那条分支，
见那里 docstring），而「怎么从命令行把它用起来」是独立的一件事，值得能单独
测：本模块**只 import 标准库**，几个函数都是纯函数（不读文件、不碰 tkinter、
不 import 包内模块），``_tools/verify/code_regression.py`` 直接喂列表进去
断言，不用建窗口、不用真实数据。

目前有九个开关：``--launch`` / ``--list`` / ``--help`` / ``--export`` /
``--export-backup`` / ``--list-backups`` / ``--out`` / ``--get`` / ``--set``
---------------------------------------------------------------------
``--launch <版本>``
    让启动器**直接**启动指定版本，省掉「选中 → 点启动 → 点确认」。
    版本名就是主界面列表里显示的那一行（如 ``Mindustry v146`` / ``BE v146``）；
    也可以只写版本号本身（如 ``v146``），前提是它只属于一个类型。

``--list``
    列出本机已有的版本（就是列表里那些行）。想知道 ``--launch`` 该写什么
    的时候用它 —— 不用先故意敲错一次去看报错。

``--list-backups``
    列出各存档分类下的备份（时间戳 + 备注），``--export-backup`` 要写的时间戳
    从这儿看。

``--get [<键>]``
    看当前设置（不带键就全列出来）。输出的写法可以**直接**拿去 ``--set``：
    ``hide_on_launch``、``jvm.jre_path``、``profile.<分类>.max_backups``。

``--set <键>=<值> [<键>=<值> …]``
    改设置，一次可以给多项（一次读、一次写、任何一项不合格就整批不落盘）。
    只改 ``config.json``，**下次启动启动器才生效**；启动器正开着时会拒绝改
    —— 它退出时会用内存里的旧配置整个写回，这次改动会被静默吃掉。

``--export <版本>``
    把一个游戏版本**拼成一个 jar**（就是启动器自己启动时拼的那个，带
    ``Main-Class``，拿到就能 ``java -jar``）。不启动游戏、不碰存档。

``--export-backup <分类> [<时间戳>]``
    把一个存档备份**拼成一个 zip**。省略时间戳 = 该分类最新那个。
    ★ 只是把备份里的文件拼出来，**不碰存档目录**（恢复才碰）。

``--out <路径>``
    ``--export`` / ``--export-backup`` 的输出位置（文件或已存在的目录）。
    省略 = 写到当前目录、用默认文件名。

``--help``
    打印用法说明。它**永远最先回答**：混着别的开关给，也只打印帮助。

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
# 列出本机已有的版本（只输出，不启动任何东西）。
LIST_FLAG = "--list"
# 打印用法说明。★ 七个开关里它**优先**：问「怎么用」永远先回答。
HELP_FLAG = "--help"
# 把游戏版本拼成 jar（拿到就能 java -jar），不启动游戏。
EXPORT_FLAG = "--export"
# 把存档备份拼成 zip（只读备份，不碰存档目录）。
EXPORT_BACKUP_FLAG = "--export-backup"
# 列出备份（只输出）。--export-backup 要写的时间戳从这儿看。
LIST_BACKUPS_FLAG = "--list-backups"
# 导出到哪儿（文件，或已存在的目录）。
OUT_FLAG = "--out"
# 看当前设置（可带一个键）。
GET_FLAG = "--get"
# 改设置（一个或多个 键=值；只写 config.json，下次启动生效）。
SET_FLAG = "--set"

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
#: 导出失败：版本 / 备份找到了，但拼文件时出错（CAS 对象丢失、磁盘写不了）
EXIT_EXPORT_FAILED = 5
#: 改配置失败：启动器正开着（改动会被它退出时覆盖）、锁文件不可用、
#: 或者写下去以后回读对不上（磁盘满 / 没权限 / 被别的程序占着）
EXIT_CONFIG_FAILED = 6


@dataclass(frozen=True)
class CliRequest:
    """命令行解析结果。不可变 —— 解析一次，之后谁都不该改它。"""

    #: 要直接启动的版本名（用户原样输入、已去空白，尚未匹配）；空串 = 没要求
    launch: str = ""
    #: 写法不完整（``--launch`` 后面没跟值）。只在**一个有效值都没有**时为真
    bad_usage: bool = False
    #: 打印用法说明（``--help``）
    show_help: bool = False
    #: 列出本机已有的版本（``--list``）
    list_versions: bool = False
    #: 列出各分类下的备份（``--list-backups``）
    list_backups: bool = False
    #: 要导出成 jar 的版本名（``--export``）；空串 = 不导出
    export: str = ""
    #: 要导出成 zip 的备份所属分类（``--export-backup``）；空串 = 不导出备份
    export_profile: str = ""
    #: 导出哪个备份（时间戳，如 ``2026-09-20_16-35-32``）；空 = 该分类最新那个
    export_backup: str = ""
    #: 输出到哪儿（``--out``）；空 = 当前目录 + 默认文件名
    out: str = ""
    #: 看设置（``--get``）
    get_config: bool = False
    #: ``--get`` 后面跟的那个键；空 = 全列出来
    get_key: str = ""
    #: ``--set`` 的 ``键=值``（原样保留，解析与校验在 ``cli_cfg`` 里）
    set_pairs: tuple[str, ...] = ()
    #: ``--set`` 里那个「不是 键=值」的参数（原样），用于说清楚哪里错了
    bad_token: str = ""
    #: 写坏了的那个开关名（如 ``--export``）。只在**一个有效值都没有**时才算错
    bad_flag: str = ""

    @property
    def used(self) -> bool:
        """用户用了命令行开关吗（写坏了也算）。

        它决定两件事：**不建可见窗口**（他要的是「直接进游戏」或者「在控制台
        里问一句话」，不是看着启动器）和 **把话说给控制台**（他是在控制台敲的
        命令）—— 见 ``CoreMixin.__init__`` 与 ``MindustryLauncher.main``。
        """
        return bool(
            self.launch
            or self.bad_usage
            or self.show_help
            or self.list_versions
            or self.list_backups
            or self.export
            or self.export_profile
            or self.get_config
            or self.set_pairs
        )

    @property
    def info_only(self) -> bool:
        """只输出信息、不启动游戏（``--help`` / ``--list`` / ``--list-backups``）。

        入口靠它决定「**连 tkinter 都不 import**」—— 见
        ``cli_info.py`` 与 ``MindustryLauncher.main`` 里的那条分支。
        """
        return bool(self.show_help or self.list_versions or self.list_backups)

    @property
    def export_only(self) -> bool:
        """只往磁盘写文件、不启动游戏（``--export`` / ``--export-backup``）。

        和 ``info_only`` 一样不需要界面（入口同样在 import tkinter 之前就分派
        走了），区别只是这两个**会产出文件**、耗时长得多（拼 jar 要读几千个
        对象、115 MB 量级）。
        """
        return bool(self.export or self.export_profile)

    @property
    def cfg_only(self) -> bool:
        """只读 / 只改配置、不启动游戏（``--get`` / ``--set``）。

        和 ``info_only`` 一样在入口、``import tkinter`` 之前就分派走 ——
        ``--set`` 要动 config.json，**绝不能**顺带建个窗口再收掉。
        """
        return bool(self.get_config or self.set_pairs)


def _value_of(argv: list[str], index: int, flag: str) -> tuple[str, int]:
    """取 ``--flag <值>`` / ``--flag=<值>`` 的值，返回 ``(值, 吃掉几个参数)``。

    值取不出来时返回空串 —— 调用方据此判断「写了一半」。三种取不出来的情况
    要分清楚：后面没东西了、后面跟的还是个开关（``--launch --debug``，绝不能
    把它当版本名）、后面是个纯空白；``--flag=`` 后面空着也算。

    ``--flag=`` 前缀**只匹配带等号的写法**，所以 ``--export`` 不会吞掉
    ``--export-backup``（后者是独立开关，不是前者的值）。
    """
    arg = argv[index]
    if arg == flag:
        nxt = argv[index + 1] if index + 1 < len(argv) else ""
        if nxt and not nxt.startswith("--") and nxt.strip():
            return nxt.strip(), 2
        return "", 1
    if arg.startswith(flag + "="):
        return arg[len(flag) + 1:].strip(), 1
    return "", 0


def parse_cli(argv: list[str]) -> CliRequest:
    """从 ``argv`` 里挑出我们自己认识的参数。不认识的**一律忽略**。

    ★ 不认识的参数不许报错：这个 exe 会被快捷方式、脚本、以及它自己的兄弟
      分支（``--debug``、``--apply-update <计划文件>``）拉起来，为一个多余
      参数弹窗说「参数错误」只会让人莫名其妙。

    认的写法两种：``--launch <值>`` 和 ``--launch=<值>``（很多工具的直觉写法）。
    重复给时取**最后一个有效值**；中间写坏过一次、但另一次给对了，按对的来。

    ★ 九个开关**一次只做一件事**，优先级：

        ``--help`` → ``--list`` → ``--list-backups`` → ``--get`` → ``--set``
        → ``--export`` → ``--export-backup`` → ``--launch``

      ``--help`` 是「问怎么用」，永远先回答；只读的排前面（看一眼有什么，
      不该先动起手来）；``--set`` 排在导出前面 —— 它只改一个 json，最快、
      也不占任何目录；导出不改任何状态，而启动会开着游戏占住存档目录。
      这条规则写进了 ``--help`` 的输出，不是隐藏行为。
    """
    # --help 永远先回答：连「别的参数写坏了」都先不管 —— 用户就是想问怎么用。
    if HELP_FLAG in argv:
        return CliRequest(show_help=True)

    out = ""
    launch = ""
    export = ""
    profile = ""
    stamp = ""
    # --get / --set
    get_seen = False
    get_key = ""
    set_pairs: list[str] = []
    bad_token = ""
    # 写坏了的开关名（记住第一个就够，提示一次即可）
    broken = ""

    i = 0
    while i < len(argv):
        arg = argv[i]

        # --get 可以带一个键（--get / --get language / --get=language）。
        # ★ 不带键时**不能**把后面的开关当成键（--get --list 里的 --list 不是键）。
        if arg == GET_FLAG or arg.startswith(GET_FLAG + "="):
            get_seen = True
            if arg.startswith(GET_FLAG + "="):
                value = arg[len(GET_FLAG) + 1:].strip()
                if value:
                    get_key = value
                i += 1
                continue
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if nxt and not nxt.startswith("-") and nxt.strip():
                get_key = nxt.strip()
                i += 2
            else:
                i += 1
            continue

        # --set 吃掉后面所有「键=值」，直到遇到下一个开关。
        # ★ 遇到不含等号的参数**记下来**、并且**不当成别的东西**：那多半是
        #   带空格的参数没加引号（--set "extra_vm_args=-Xmx4G -XX:+UseZGC"
        #   忘了引号 → 第二个词到这里），悄悄忽略掉一半比报错危险得多。
        if arg == SET_FLAG or arg.startswith(SET_FLAG + "="):
            if arg.startswith(SET_FLAG + "="):
                single = arg[len(SET_FLAG) + 1:].strip()
                if single and "=" in single and single.split("=", 1)[0].strip():
                    set_pairs.append(single)
                elif single:
                    bad_token = bad_token or single
                else:
                    broken = broken or SET_FLAG
                i += 1
                continue
            eaten = 0
            j = i + 1
            while j < len(argv):
                token = argv[j]
                if token.startswith("--"):
                    break
                head = token.split("=", 1)[0].strip()
                if "=" in token and head:
                    set_pairs.append(token)
                elif token.strip():
                    bad_token = bad_token or token
                    break
                j += 1
                eaten += 1
            if not eaten:
                broken = broken or SET_FLAG
            i += 1 + eaten
            continue

        # ⚠️ --export 与 --export-backup 是两个独立开关。用 _value_of 按
        #    「整串相等」或「--flag= 前缀」匹配，--export-backup 不会被
        #    --export 当成「值后面还有东西」捡走。
        if arg == OUT_FLAG or arg.startswith(OUT_FLAG + "="):
            value, n = _value_of(argv, i, OUT_FLAG)
            if value:
                out = value
            i += n or 1
            continue

        if arg == LAUNCH_FLAG or arg.startswith(LAUNCH_FLAG + "="):
            value, n = _value_of(argv, i, LAUNCH_FLAG)
            if value:
                launch = value
            else:
                broken = broken or LAUNCH_FLAG
            i += n or 1
            continue

        if arg == EXPORT_FLAG or arg.startswith(EXPORT_FLAG + "="):
            value, n = _value_of(argv, i, EXPORT_FLAG)
            if value:
                export = value
            else:
                broken = broken or EXPORT_FLAG
            i += n or 1
            continue

        if arg == EXPORT_BACKUP_FLAG or arg.startswith(EXPORT_BACKUP_FLAG + "="):
            if arg.startswith(EXPORT_BACKUP_FLAG + "="):
                value = arg[len(EXPORT_BACKUP_FLAG) + 1:].strip()
                if value:
                    profile = value
                else:
                    broken = broken or EXPORT_BACKUP_FLAG
                i += 1
                continue
            # 位置写法：--export-backup <分类> [<时间戳>]
            value, n = _value_of(argv, i, EXPORT_BACKUP_FLAG)
            if not value:
                broken = broken or EXPORT_BACKUP_FLAG
                i += n or 1
                continue
            profile = value
            # 第二个位置参数才是时间戳；跟个开关（--out）时不许吞掉它。
            nxt = argv[i + n] if i + n < len(argv) else ""
            if nxt and not nxt.startswith("--") and nxt.strip():
                stamp = nxt.strip()
                n += 1
            i += n
            continue

        i += 1

    # ------------------------------------------------------------ 一次只做一件事
    # 顺序＝优先级。只在「该类参数真的给了值」时才命中，所以
    # `--launch v146 --list` 会去列清单、`--export` 写坏了不会拦住别的。
    if LIST_FLAG in argv:
        return CliRequest(list_versions=True)
    if LIST_BACKUPS_FLAG in argv:
        return CliRequest(list_backups=True)
    # ★ 写坏的 --set 参数**先答复**（哪怕同时给了 --get）：只做一半、剩下的
    #   悄悄丢掉，比报错危险得多 —— 用户会以为那几项也改成了。
    if bad_token:
        return CliRequest(
            bad_usage=True, bad_flag=SET_FLAG, bad_token=bad_token
        )
    if get_seen:
        return CliRequest(get_config=True, get_key=get_key)
    if set_pairs:
        return CliRequest(set_pairs=tuple(set_pairs))
    if export:
        return CliRequest(export=export, out=out)
    if profile:
        return CliRequest(export_profile=profile, export_backup=stamp, out=out)
    return CliRequest(
        launch=launch,
        # 只有**一个有效值都没有**时才算参数写坏 —— 用户敲错一次又补对了，
        # 不该继续罢工（跟「重复给取最后一个」是同一套宽容度）。
        bad_usage=bool(broken) and not launch,
        bad_flag=broken if not launch else "",
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
