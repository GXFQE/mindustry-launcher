# -*- coding: utf-8 -*-
"""拼装游戏启动命令行 + 解析用户自定义的额外参数 + 试跑一次 java 做体检。

单独一个模块（而不是塞进 gui_game）是为了**能脱离 tkinter 直接测**：
参数顺序、引号处理、反斜杠这些正是最容易错、又最难靠点界面发现的地方。
"""
import logging
import os
import re
import subprocess
from pathlib import Path

from .gamelog import decode_output_line

logger = logging.getLogger(__name__)

# 「额外 JVM 参数」里出现这些就说明用户在跟自己较劲：它们会跟启动器
# 自己安排的东西打架。不拦，但要在保存时明确告诉他后果。
_CONFLICTING_VM_FLAGS = {
    "-cp": "会顶掉启动器组装好的运行时 jar（-cp 在下面会被整个覆盖）",
    "-classpath": "会顶掉启动器组装好的运行时 jar",
    "-jar": "会顶掉主类，游戏起不来",
}
_DATA_DIR_PREFIX = "-Dmindustry.data.dir"

# 让 JVM 把标准输出/错误按 UTF-8 写。**不是可选的美化**：
# Java 在没有控制台时（我们用管道接它的输出）取 native.encoding —— 简中
# Windows 上就是 GBK，而管道这头按 UTF-8 解，中文 mod 名会整片变成替换字符
# （实测一份日志 44 个 U+FFFD，mod 列表基本没法看）。
# Java 19+ 认这两个键（实测 -XshowSettings 里 stdout.encoding 变成 UTF-8）；
# 更老的 JRE 会忽略它们，那种情况由 gamelog.decode_output_line 兜底解码。
_OUTPUT_ENCODING_ARGS = ["-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8"]


def split_args(text: str) -> list[str]:
    """把一行命令行文本切成 argv。

    为什么不用 shlex：
      * ``shlex.split(text)``（posix 模式）把反斜杠当转义符，Windows 路径
        ``C:\\Users\\me`` 会被啃成 ``C:Usersme``；
      * ``shlex.split(text, posix=False)`` 又原样保留引号，得到 ``'"a b"'``。

    所以只实现「双引号可以包住一段带空格的内容」这一条规则，其余字符
    （包括反斜杠）一律按字面量处理。

    引号没闭合时抛 ``ValueError`` —— 这种输入一定不是用户想要的，
    与其猜，不如让调用方明确报错。
    """
    if "\x00" in text:
        raise ValueError("参数里不能有 NUL 字符")
    args: list[str] = []
    cur: list[str] = []
    in_quote = False
    started = False     # 用来区分 `""`（一个空参数）和「这里没参数」
    for ch in text:
        if ch == '"':
            in_quote = not in_quote
            started = True
            continue
        if ch.isspace() and not in_quote:
            if started:
                args.append("".join(cur))
                cur = []
                started = False
            continue
        cur.append(ch)
        started = True
    if in_quote:
        raise ValueError("双引号没有闭合")
    if started:
        args.append("".join(cur))
    return args


def check_extra_args(extra_vm_args: list[str]) -> list[str]:
    """检查额外 JVM 参数里的危险项，返回给用户看的告警（空 = 没问题）。"""
    warnings: list[str] = []
    for arg in extra_vm_args:
        base = arg.split("=", 1)[0]
        if base in _CONFLICTING_VM_FLAGS:
            warnings.append(f"「{arg}」{_CONFLICTING_VM_FLAGS[base]}")
        elif arg.startswith(_DATA_DIR_PREFIX):
            warnings.append(
                f"「{arg}」会覆盖启动器的存档隔离 —— 换了存档分类也会读写"
                "同一个目录，请确认这是你要的"
            )
    return warnings


def build_java_command(
    java_exe: str | Path,
    jar_path: str | Path,
    main_class: str,
    data_dir: str | Path,
    vm_args: list[str] | None = None,
    extra_vm_args: list[str] | None = None,
    program_args: list[str] | None = None,
    extra_program_args: list[str] | None = None,
) -> list[str]:
    """拼出完整的 java 启动命令。顺序是有讲究的：

        java <内置 vmArgs> <输出编码> -Dmindustry.data.dir=<目录>
             <额外 JVM 参数> -cp <jar> <主类> <内置游戏参数> <额外游戏参数>

    * ``-Dmindustry.data.dir`` 必须排在 ``-cp`` **前面**（铁律）。JVM 把
      ``-cp`` 之后的内容当成类路径和程序参数，放后面等于没设。
    * ``-D`` 排在**内置 vmArgs 之后**：万一官方那份 vmArgs 里哪天也带了
      这个属性，存档隔离仍然以启动器的为准。
    * 输出编码（``_OUTPUT_ENCODING_ARGS``）紧跟内置参数：它是启动器自己
      的要求，但用户真要在「额外 JVM 参数」里写自己的值，JVM 取最后一个，
      仍然是用户赢。
    * 用户自己的参数一律排**最后**：JVM 对重复选项取最后一个，这样
      ``-Xmx4G`` 能压过内置值 —— 「我写的应该赢」才符合直觉。
    """
    cmd = [str(java_exe)]
    cmd += list(vm_args or [])
    cmd += _OUTPUT_ENCODING_ARGS
    cmd.append(f"{_DATA_DIR_PREFIX}={data_dir}")
    cmd += list(extra_vm_args or [])
    cmd += ["-cp", str(jar_path), main_class]
    cmd += list(program_args or [])
    cmd += list(extra_program_args or [])
    return cmd


# 「检测 JRE」时跑 java -version 的超时。正常 JVM 起个 -version 只要几百毫秒，
# 给足冷启动（机械盘 + 杀软首次扫描）的余量，但别让界面上的「检测中…」转到
# 天荒地老 —— 卡住本身就是个该报告的结果。
_JAVA_PROBE_TIMEOUT = 20.0
# 打包成无控制台的 exe 后再 subprocess 起 java，会闪一个黑框。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# java -version 第一行里的版本号：Java 9+ 是 ``version "25"``，
# Java 8 是 ``version "1.8.0_402"``，用同一个正则都能捞到。
_JAVA_VERSION_RE = re.compile(r'version "([^"]+)"')
# 想显示发行版的话从这几家里认一个（认不出就不写，不猜）
_JAVA_VENDORS = ("Temurin", "Zulu", "GraalVM", "Corretto", "Microsoft", "OpenJ9")


def probe_java(
    java_exe: str | Path, timeout: float = _JAVA_PROBE_TIMEOUT
) -> tuple[bool, str]:
    """真跑一次 ``java -version``，确认这个 JRE **能用**（不只是文件存在）。

    为什么非要真跑：``jre/bin/java.exe`` 存在只能说明「有这么个文件」——
    它可能是个 0 字节的残留、可能是别的东西改了名、也可能因为缺 VC 运行库
    或被安全软件拦着而起不来。那些情况原本要等到点「启动游戏」才报错
    （那时用户已经等过拼 jar 的几秒、还以为在加载），现在点一下「检测」
    几百毫秒就能问清楚。

    ★ **绝不抛异常**：路径不存在、不是可执行文件、超时、被系统拒绝，一律
    折成 ``(False, 说明)`` —— 调用方（GUI 按钮 / 设置页保存）只需要看第一个
    返回值，不用再包一层 try。

    返回 ``(是否可用, 一行说明)``，说明直接拿去显示给用户看。
    """
    java_exe = Path(java_exe)
    if not java_exe.is_file():
        return False, f"找不到文件：{java_exe}"
    try:
        proc = subprocess.run(
            [str(java_exe), "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # java -version 是往 stderr 写的
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, f"执行超过 {timeout:.0f} 秒没结束（可能卡在杀软扫描）"
    except OSError as e:
        return False, f"无法运行：{e}"
    # 输出按行过同一个解码器：JVM 报的系统错（缺 DLL 之类）可能是 GBK 的中文
    lines = [decode_output_line(raw) for raw in (proc.stdout or b"").splitlines()]
    head = next((line.strip() for line in lines if line.strip()), "")
    joined = " ".join(lines)
    if proc.returncode != 0:
        return False, f"退出码 {proc.returncode}：{head or '（没有输出）'}"
    found = _JAVA_VERSION_RE.search(joined)
    if not found:
        return False, f"输出看不懂（不像 java -version）：{head or '（没有输出）'}"
    vendor = next(
        (v for v in _JAVA_VENDORS if v.lower() in joined.lower()), ""
    )
    logger.info(
        f"JRE 检测通过：{java_exe} -> {head}"
        + (f"（{vendor}）" if vendor else "")
    )
    return True, f"Java {found.group(1)}" + (f" · {vendor}" if vendor else "")


# 环境变量兜底时**每个**候选最多等多久。这里比 _JAVA_PROBE_TIMEOUT 短得多：
# 正常 java -version 是几百毫秒的事，会卡住的只有坏掉的残留桩，而这条路
# 跑在启动路径上（窗口还没出来），不能让用户对着空屏幕等 20 秒。
_JAVA_FIND_TIMEOUT = 8.0
# 最多试几个候选。PATH 里塞了七八个 java 是可能的（IDE 自带、旧版残留…），
# 但只有一个能用；试三个还没中就不再往下翻了。
_JAVA_FIND_MAX_CANDIDATES = 3


def find_system_java(
    timeout: float = _JAVA_FIND_TIMEOUT,
) -> tuple[Path | None, str]:
    """去环境变量里捞一个**真能用**的 Java（内置 jre/ 找不到时的兜底）。

    只看两处，按优先级：

    1. ``JAVA_HOME`` —— 指向 JDK/JRE 的根目录（Windows 安装器都会设它）；
    2. ``PATH`` 里的各个目录 —— 按顺序取 ``<目录>/java.exe``。

    ★ 找到候选**必须真跑一次** ``java -version`` 才算数：``PATH`` 里那个
    ``java.exe`` 常常是 Oracle 安装器留下的 ``javapath`` 转发桩，JRE 卸载
    之后它还在、还要报错 —— 光看文件在不在会捞到一个「活着但没用」的东西。

    返回 ``(java.exe 路径 或 None, 一行说明)`` —— **返回的是 java.exe
    本身**，不是目录：``PATH`` 里捞到的不一定是 ``<Java根>/bin`` 这种能反推
    出根目录的布局（Oracle 的 javapath 目录就是个反例），只有「这个二进制
    我们已经验过能用」是永远成立的。想显示成目录由调用方按 bin 规则推。
    **绝不抛异常**；说明直接写进日志/状态栏给用户看。
    """
    # (来源, java.exe)
    candidates: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add(source: str, exe: Path) -> None:
        try:
            key = os.path.normcase(os.path.normpath(str(exe)))
        except (OSError, ValueError):       # pragma: no cover - 病态路径
            key = str(exe)
        if key in seen or not exe.is_file():
            return
        seen.add(key)
        candidates.append((source, exe))

    home = os.environ.get("JAVA_HOME", "").strip().strip('"')
    if home:
        add("JAVA_HOME", Path(home) / "bin" / "java.exe")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if entry:
            add("PATH", Path(entry) / "java.exe")

    if not candidates:
        logger.info("内置 jre 不可用，JAVA_HOME / PATH 里也没有 java.exe")
        return None, "JAVA_HOME 和 PATH 里都没有找到 java.exe"

    for source, exe in candidates[:_JAVA_FIND_MAX_CANDIDATES]:
        ok, text = probe_java(exe, timeout=timeout)
        if ok:
            logger.warning(
                f"内置 jre 不可用，改用 {source} 里的 Java：{exe}（{text}）"
            )
            return exe, f"{source} 里的 {text}"
        logger.warning(f"{source} 里的 java 用不了（{exe}）：{text}")
    return None, "环境变量里找到的 java 都起不来（可能已被卸载）"
