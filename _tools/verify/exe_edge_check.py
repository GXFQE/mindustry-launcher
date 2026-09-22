# -*- coding: utf-8 -*-
"""对**已打包的 exe** 做边界条件冒烟。

为什么不能只靠 code_regression.py：那份跑的是源码逻辑（沙箱里 import launcher）。
exe 是 PyInstaller 冻结过的另一份代码，数据根、cwd、日志文件名都跟源码运行不同，
「源码对」推不出「exe 对」。所以边界条件必须拿 exe 再真跑一遍。

七个场景（都在临时沙箱里跑，不碰真实数据）：

  A 坏配置自愈   config.json 被截断 → 留档成 .bad、重建默认配置、进程不崩
  B 脏数据不炸   坏清单 / 非 UTF8 清单 → 跳过并继续；剩下的有效版本照常列出
  C 孤儿对象     刚写进去的孤儿对象不能被开机后台 GC 删掉（宽限期）；旧的照删
  D 缺 jre       哪儿都没有 Java 时必须给出**可读的中文提示**再退出，不能静默失败
  E 脏 jvm 配置  jvm 段字段类型全错 + 额外参数引号没闭合 → 降级到默认照常启动
  F jre 路径错   jvm.jre_path 指向不存在的地方 → 退回默认 jre、写回配置才继续
                 （设置页里能改 jre 路径之后，「改错就再也打不开」必须堵死）
  G 走环境变量   没 jre/ 但 JAVA_HOME 里有能用的 Java → 照常启动，只用这一次、
                 **不写回配置**（D 和 G 是一对：有就兜住，没有才报错）

关窗口一律用 WM_CLOSE 而不是 terminate —— 走的是真实 on_closing，
才能顺带验证「关得干不干净」这件事。

★ 沙箱必须带上 jre/：启动器在 __init__ 里就会校验 jre 存在，缺了会直接退出，
  根本走不到窗口那一步（这正是场景 D / G 要验的）。所以 A/B/C/E/F 用的沙箱要带
  jre，D/G 用的沙箱故意不带 —— 两个沙箱分开建（D 和 G 共用同一个）。
  ⚠️ D 还要用 _env_without_java() 把 PATH / JAVA_HOME 里的 java 摘掉：开发环境
  里通常装着 JDK，不摘的话启动器会（正确地）兜住，D 就永远复现不出来。

★ 沙箱还要**预置最新的版本清单**（见 sandbox_seed.py）：不预置的话，启动器
  默认开着 auto_update，一开机就判定「有新版本」→ 静默下载 109 MB 的
  Mindustry 160.4 + 115 MB 的 MindustryX，跑一轮冒烟要好几分钟。
  预置之后更新检查直接给出「已是最新」，一个字节都不下。

用法：
    python _tools/verify/exe_edge_check.py
    python _tools/verify/exe_edge_check.py --exe <path> --seconds 12 --keep
    python _tools/verify/exe_edge_check.py --only D      # 只验缺 jre 那一条
    python _tools/verify/exe_edge_check.py --only G      # 只验环境变量兜底那一条
    python _tools/verify/exe_edge_check.py --only D,G    # 两条一起（共用沙箱）
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sandbox_seed import seed_latest_versions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # _tools/ 共用路径模块
from _paths import ROOT, find_runtime_root   # noqa: E402

USER32 = ctypes.windll.user32
WM_CLOSE = 0x0010

EXE_NAME = "Mindustry启动器.exe"
WINDOW_KEYWORD = "启动器"

# 开机后多久会排那次后台 GC（gui_core.py 里是 after(3000, ...)）
GC_DELAY_S = 3.0


# --------------------------------------------------------------------------
# 窗口操作
# --------------------------------------------------------------------------
def _windows_of_pid(pid: int) -> list[tuple[int, str]]:
    """按进程号找窗口。

    比按标题找可靠：Tk 的根窗口在 ``root.title()`` 被调用**之前**就已经可见，
    标题还是默认的 "tk"；而且全局按标题找会认错别人的窗口（自己开着的启动器、
    别的同名程序）。所以一律先锁定「这个进程自己的窗口」。
    """
    found: list[tuple[int, str]] = []
    PROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
    )

    def cb(hwnd, lp):
        if not USER32.IsWindowVisible(hwnd):
            return True
        owner = ctypes.c_ulong()
        USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        n = USER32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        USER32.GetWindowTextW(hwnd, buf, n + 1)
        found.append((hwnd, buf.value or "(无标题)"))
        return True

    USER32.EnumWindows(PROC(cb), 0)
    return found


def _wait_window(timeout: float, proc: subprocess.Popen) -> tuple[int, str] | None:
    """等本进程的主窗口出现；进程要是先死了就别干等。

    先要求标题里带「启动器」（证明标题真设上了），超时后再退一步认「本进程的
    任意窗口」，这样能区分「真没窗口」和「窗口在但标题不是预期那样」。
    """
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        mine = _windows_of_pid(proc.pid)
        for hwnd, title in mine:
            if WINDOW_KEYWORD in title:
                return (hwnd, title)
        if proc.poll() is not None:
            return None
        time.sleep(0.1)
    mine = _windows_of_pid(proc.pid)
    return mine[0] if mine else None


def _env_without_java() -> dict[str, str]:
    """把环境里所有能找到 java.exe 的地方摘掉（JAVA_HOME + PATH 那几项）。

    场景 D 要测的是「哪儿都没有 Java」时的报错质量。开发环境里通常装着好几个
    JDK，不摘干净的话启动器会（正确地）用环境变量兜住 —— 于是「缺 jre」这
    个场景根本复现不出来，测出来的会是别的东西。定向摘掉那几项就够了，
    其它环境照旧（PyInstaller 的 exe 还得靠系统环境正常起来）。
    """
    env = dict(os.environ)
    env.pop("JAVA_HOME", None)
    entries = env.get("PATH", "").split(os.pathsep)

    def _key(text: str) -> str:
        text = text.strip().strip('"')
        if not text:
            return ""
        return os.path.normcase(os.path.normpath(text))

    java_dirs = {_key(e) for e in entries if _key(e)
                 and (Path(_key(e)) / "java.exe").is_file()}
    env["PATH"] = os.pathsep.join(
        e for e in entries if _key(e) not in java_dirs
    )
    return env


def _run_once(root: Path, seconds: float,
              env: dict[str, str] | None = None) -> dict:
    """起一次 exe：等窗口 → 观察 → WM_CLOSE → 等退出。

    ``env`` 给了就用它（场景 D/G 要控制「环境里有没有 Java」）；不给就继承。
    """
    t0 = time.perf_counter()
    proc = subprocess.Popen([str(root / EXE_NAME)], cwd=str(root), env=env)
    win = _wait_window(40.0, proc)
    info: dict = {
        "window": win[1] if win else None,
        "window_at": (time.perf_counter() - t0) if win else None,
        "alive_before_close": proc.poll() is None,
        "alive_after_wait": None,
        "clean_exit": None,
        "exit_code": None,
    }

    time.sleep(max(0.0, seconds) if proc.poll() is None else 0.0)

    if win is not None:
        USER32.PostMessageW(win[0], WM_CLOSE, 0, 0)
        deadline = time.perf_counter() + 25
        while time.perf_counter() < deadline and proc.poll() is None:
            time.sleep(0.2)
        info["clean_exit"] = proc.poll() is not None
    else:
        info["clean_exit"] = False

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        info["alive_after_wait"] = True
    else:
        info["alive_after_wait"] = False
    info["exit_code"] = proc.returncode
    info["elapsed"] = time.perf_counter() - t0
    return info


def _read_log(root: Path) -> str:
    """打包版写 launcher.log（源码版写 launcher.dev.log）。"""
    log = root / "launcher.log"
    if not log.is_file():
        return ""
    return log.read_text(encoding="utf-8", errors="replace")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seed_object(root: Path, data: bytes, age_seconds: float = 0.0) -> Path:
    sha = _sha(data)
    path = root / "versions" / "objects" / sha[:2] / sha[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if age_seconds:
        past = time.time() - age_seconds
        os.utime(path, (past, past))
    return path


def _reset_version_manifests(root: Path) -> Path:
    mf = root / "versions" / "manifests"
    mf.mkdir(parents=True, exist_ok=True)
    for f in mf.glob("*"):
        if f.is_file():
            f.unlink()
    return mf


# --------------------------------------------------------------------------
# 沙箱
# --------------------------------------------------------------------------
def _make_sandbox(exe_src: Path, with_jre: bool = True) -> Path:
    root = Path(tempfile.mkdtemp(prefix="mdt_exe_edge_"))
    shutil.copy2(exe_src, root / EXE_NAME)

    internal = exe_src.parent / "_internal"
    if not internal.is_dir():
        raise RuntimeError(f"exe 旁边找不到 _internal：{internal}")
    shutil.copytree(internal, root / "_internal")

    # ★ 故意**不**拷 Mindustry.json：那几项已并进 config.json 的 jvm 段，
    #   缺了用内置默认值。下面还会断言它确实不在，免得以后有人顺手加回来。
    assert not (root / "Mindustry.json").exists()

    # ★ jre 必须带上，否则启动器连窗口都建不起来（见模块说明）
    if with_jre:
        jre_src = exe_src.parent / "jre"
        if not jre_src.is_dir():
            raise RuntimeError(
                f"exe 旁边找不到 jre：{jre_src}\n"
                "A/B/C 三个场景需要一个带 jre 的环境。"
            )
        shutil.copytree(jre_src, root / "jre")

    (root / "versions" / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "versions" / "objects").mkdir(parents=True, exist_ok=True)
    (root / "backups" / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "backups" / "objects").mkdir(parents=True, exist_ok=True)

    # ★ 预置最新版本清单：不预置的话 auto_update 一开机就真去下 200+ MB
    seed_latest_versions(root)
    return root


class Report:
    def __init__(self, title: str) -> None:
        self.title = title
        self.items: list[tuple[str, bool, str]] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.items.append((label, bool(ok), detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.items)

    def dump(self) -> None:
        print(f"\n--- {self.title} ---")
        for label, ok, detail in self.items:
            mark = "OK  " if ok else "失败"
            tail = f"   {detail}" if detail else ""
            print(f"  [{mark}] {label}{tail}")


# --------------------------------------------------------------------------
# 场景 A：坏配置自愈
# --------------------------------------------------------------------------
def scenario_bad_config(root: Path, seconds: float) -> Report:
    rep = Report("A 坏配置自愈")
    bad_bytes = b'{"hide_on_launch": true, "prof'
    cfg = root / "config.json"
    cfg.write_bytes(bad_bytes)
    (root / "config.json.bad").unlink(missing_ok=True)

    info = _run_once(root, seconds)
    rep.check("窗口正常出现", info["window"] is not None,
              f"{info['window_at']:.2f}s" if info["window_at"] else "没等到")
    rep.check("进程没崩", info["alive_before_close"])
    rep.check("关窗口能干净退出", info["clean_exit"])
    rep.check("退出码是 0", info["exit_code"] == 0, f"exit={info['exit_code']}")

    bad = root / "config.json.bad"
    rep.check("坏配置被留档为 config.json.bad",
              bad.is_file() and bad.read_bytes() == bad_bytes,
              f"{bad.stat().st_size}B" if bad.is_file() else "文件不存在")

    rebuilt = False
    detail = ""
    data: dict = {}
    try:
        loaded = json.loads(cfg.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
        rebuilt = isinstance(loaded, dict) and bool(loaded.get("profiles"))
        detail = f"分类 {list(loaded.get('profiles', {}))}"
    except Exception as e:  # noqa: BLE001
        detail = f"重建的配置读不出来: {e}"
    rep.check("重建出可用的 config.json", rebuilt, detail)

    # 原 Mindustry.json 的内容现在由 config.json 的 jvm 段承担，
    # 所以「坏配置重建」必须连这一段一起建出来，否则下次启动会缺主类。
    jvm = data.get("jvm") if isinstance(data, dict) else None
    rep.check(
        "重建的配置自带 jvm 段（原 Mindustry.json）",
        isinstance(jvm, dict)
        and jvm.get("main_class") == "mindustry.desktop.DesktopLauncher",
        f"main_class={jvm.get('main_class') if isinstance(jvm, dict) else None}",
    )
    rep.check(
        "jvm 段带上了默认 vmArgs",
        isinstance(jvm, dict)
        and isinstance(jvm.get("vm_args"), list)
        and len(jvm["vm_args"]) >= 4,
        f"{len(jvm.get('vm_args') or [])} 条" if isinstance(jvm, dict) else "",
    )
    rep.check(
        "没有 Mindustry.json 也能起（它已不是必需文件）",
        not (root / "Mindustry.json").exists(),
    )

    log = _read_log(root)
    rep.check("日志记录了留档动作", "config.json.bad" in log)
    rep.check("日志没有 Traceback", "Traceback" not in log)
    return rep


# --------------------------------------------------------------------------
# 场景 B+C：脏数据不炸 与 孤儿对象宽限期
# --------------------------------------------------------------------------
def scenario_dirty_and_gc(root: Path, seconds: float) -> Report:
    rep = Report("B/C 脏数据不炸 + 孤儿对象宽限期")

    cfg_data = {
        "hide_on_launch": False,
        # 旧的镜像开关：已废弃，留着看它会不会被当成脏值时炸掉（应该被无害忽略）
        "use_mirror": True,
        "github_mirror": "这不是地址，也不是镜像前缀",
        # 候选清单也写坏（既不是数组也不是字符串）：该退回内置默认，不能崩
        "github_mirror_presets": {"本该": "是个数组"},
        "max_log_files": "一大堆",
        # 开关型写成不认识的东西：老写法 bool("随便写") 是 True —— 而这一项
        # 写错就是「启动器自己关掉」，所以必须退回默认（不关）并记一条 WARNING。
        # （注意别写 "false"：那在 normalize_bool 里是**认得**的写法，正确地
        #   判成 False 且不告警 —— 这里要验的是「认不出来时退默认且留话」。）
        "close_on_game_exit": "随便写个值",
        "auto_update": True,          # 顺带走一遍更新检查（版本已是最新，不会真下）
        "current_profile": "根本没有这个分类",
        "profiles": {
            "默认": {
                "data_dir": str(root / "no_such_data_dir"),
                "min_playtime": 20,
                "max_backups": 20,
                "auto_backup": True,
            }
        },
    }
    (root / "config.json").write_text(
        json.dumps(cfg_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "config.json.bad").unlink(missing_ok=True)

    # 坏清单：缺 version 字段
    mf = _reset_version_manifests(root)
    (mf / "坏清单.json").write_bytes(b'{"type": "Mindustry"}')
    # 非 UTF-8 清单
    (mf / "非UTF8.json").write_bytes(b"\xff\xfe\x00\x01bad")
    # ★ 清完坏清单再预置：上面那句「版本已是最新，不会真下」得靠它才算数
    seed_latest_versions(root)

    # 孤儿对象：新的（应被宽限期保住）+ 旧的（应被删掉，做对照）
    fresh = _seed_object(root, b"fresh-orphan-object")
    old = _seed_object(root, b"old-orphan-object", age_seconds=7200)

    info = _run_once(root, seconds)
    rep.check("窗口正常出现", info["window"] is not None,
              f"{info['window_at']:.2f}s" if info["window_at"] else "没等到")
    rep.check("坏了 2 份清单也不崩（有效版本照常列出）", info["alive_before_close"])
    rep.check("关窗口能干净退出", info["clean_exit"])

    rep.check("刚写入的孤儿对象被宽限期保住", fresh.is_file())
    rep.check("宽限期外的旧孤儿对象被回收", not old.exists())

    log = _read_log(root)
    rep.check("坏清单被跳过而不是崩掉", "跳过无效清单" in log)
    rep.check("日志记录了 GC 跳过新对象", "垃圾回收跳过" in log)
    rep.check("后台 GC 正常跑完", "垃圾回收完成" in log)
    rep.check("日志点名了脏的「游戏退出后自动关闭启动器」开关",
              "close_on_game_exit" in log)
    rep.check("日志没有 Traceback", "Traceback" not in log)

    # 只作观察，不算失败：更新检查在离线/限流时会记 ERROR，那不是崩溃
    for line in log.splitlines():
        if "[更新]" in line or "检查更新" in line or "更新检查" in line:
            print(f"    · {line.strip()}")
    return rep


# --------------------------------------------------------------------------
# 场景 D：缺 jre
# --------------------------------------------------------------------------
def scenario_missing_jre(root: Path) -> Report:
    """哪儿都没有 Java 时必须「说人话」：弹一个可读的错误框 + 留日志，而不是静默。

    用户手动把 jre 挪走／杀软吞掉文件时，无控制台的 exe 双击一下什么都没发生，
    是最糟糕的体验。当前实现是顶层兜底弹 ``messagebox.showerror("错误", ...)``，
    所以正确行为是「有名有姓的错误框在等你点确定」，而不是「干脆不起来」。

    ⚠️ 这个框是**模态**的：不点掉它进程就一直活着。以前把预期写成「不建窗口」，
    结果 40 s 超时后才发现窗口一直在那儿 —— 是预期错了，不是程序错了。

    ★ 2026-09-19 起「没 jre」多了一层兜底（JAVA_HOME / PATH 里找 Java，见场景
    G），所以**必须**把环境里那几个 java 摘干净才算真的复现「哪儿都没有」，
    否则测到的会是场景 G 的行为。沙箱由 main() 建好传进来（跟 G 共用，
    省一次 60 MB 拷贝），这里只管跑。
    """
    rep = Report("D 缺 jre 时的报错质量")
    # 注意 env：开发环境的 PATH 里通常有 JDK，不摘掉的话启动器会（正确地）用环境
    # 变量兜住，于是「缺 jre」根本复现不出来 —— 见 _env_without_java。
    proc = subprocess.Popen([str(root / EXE_NAME)], cwd=str(root),
                            env=_env_without_java())
    dlg: tuple[int, str] | None = None
    deadline = time.perf_counter() + 40
    while time.perf_counter() < deadline:
        wins = _windows_of_pid(proc.pid)
        if wins:
            dlg = wins[0]
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)

    rep.check("没有静默什么都不发生", dlg is not None or proc.poll() is not None)
    if dlg is not None:
        rep.check("弹出的是可读的错误提示框", dlg[1] == "错误", f"标题「{dlg[1]}」")
        USER32.PostMessageW(dlg[0], WM_CLOSE, 0, 0)

    deadline = time.perf_counter() + 20
    while time.perf_counter() < deadline and proc.poll() is None:
        time.sleep(0.1)
    exited = proc.poll() is not None
    rep.check("点掉提示后进程能正常退出", exited)
    if not exited:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    log = _read_log(root)
    rep.check("日志指出了缺 jre", "没有找到 jre 目录" in log)
    rep.check("日志写明了期望路径", "jre" in log and "java.exe" in log)
    rep.check("给出的是可读中文而不是裸异常", "请确认 jre 文件夹" in log)
    rep.check("★ 日志里说明了「环境变量里也没找到」（不是悄悄放弃）",
              "JAVA_HOME" in log)
    return rep


# --------------------------------------------------------------------------
# 场景 G：没 jre，但环境变量里有能用的 Java（就这一次，不写回配置）
# --------------------------------------------------------------------------
def scenario_env_java(root: Path, seconds: float, jre_dir: Path) -> Report:
    """内置 jre/ 不在，JAVA_HOME 指着一套能用的 Java —— 不能再报错了。

    用户原话：「话说如果没有 jre 是不是可以先试试找环境变量看看有没有 jdk」。
    三条要点：

      1. 照常起来（很多人机器上本来就装着 JDK，没必要为了启动器再下一份 jre）；
      2. 日志里 WARNING 说清楚改用的是哪一个（不然用户不知道跑的是谁的 java）；
      3. ★ **配置不许被写脏** —— ``jre_path`` 还是原来的值。jre/ 只是暂时不在，
         拿机器上的绝对路径把用户填的值盖掉才是真搞坏配置。

    和场景 D 共用同一个没有 jre 的沙箱（省一次 60 MB 拷贝），所以进来先把
    上一轮留下的 launcher.log 删掉，免得两轮的日志混在一起看不清。
    """
    rep = Report("G 没 jre 时用环境变量里的 Java 顶上")
    (root / "config.json").write_text(
        json.dumps(
            {
                "hide_on_launch": False,
                "auto_update": False,
                "github_mirror": "",
                "current_profile": "默认",
                "profiles": {
                    "默认": {
                        "data_dir": str(root / "g_data"),
                        "min_playtime": 20,
                        "max_backups": 20,
                        "auto_backup": True,
                    }
                },
                "jvm": {"jre_path": "jre"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "launcher.log").unlink(missing_ok=True)

    if not (jre_dir / "bin" / "java.exe").is_file():
        rep.check("测试机上有可用的 Java 供兜底（不然这条测不了）", False,
                  str(jre_dir))
        return rep

    env = _env_without_java()
    env["JAVA_HOME"] = str(jre_dir)     # 只留这一条路：JAVA_HOME
    info = _run_once(root, seconds, env=env)
    rep.check("窗口照常出现（没 jre 也进得去，没被拦在门外）",
              info["window"] is not None,
              f"{info['window_at']:.2f}s" if info["window_at"] else "没等到")
    rep.check("关窗口能干净退出", info["clean_exit"])
    rep.check("退出码是 0", info["exit_code"] == 0, f"exit={info['exit_code']}")

    log = _read_log(root)
    rep.check("★ 日志说清楚了改用哪个 Java（用户得知道跑的是谁的）",
              "改用" in log and str(jre_dir) in log,
              next((ln for ln in log.splitlines() if "改用" in ln), "没找到那行"))
    rep.check("日志没有 Traceback", "Traceback" not in log)
    rep.check("没有弹「找不到 jre」的错误框", info["window"] is None
              or "错误" not in (info["window"] or ""))
    on_disk = json.loads((root / "config.json").read_text(encoding="utf-8"))
    rep.check("★ 配置里的 jre_path 还是 jre（兜底只管这一次，不写回配置）",
              on_disk.get("jvm", {}).get("jre_path") == "jre",
              repr(on_disk.get("jvm", {}).get("jre_path")))
    rep.check("没被误判成「配置损坏」而留档重建",
              not (root / "config.json.bad").exists())
    return rep


# --------------------------------------------------------------------------
# 场景 F：jre 路径写错（现在设置页里能改，所以这条路必须自愈）
# --------------------------------------------------------------------------
def scenario_wrong_jre_path(root: Path, seconds: float) -> Report:
    """config.json 里的 jre 路径指向一个不存在的地方 —— 必须退回默认的 jre。

    2026-09-19 起 JRE 路径在**设置页里能改**，于是「改错一个路径就再也打不开
    启动器」成了一条真实的路。老做法（找不到就抛 FileNotFoundError）在这个功能
    面前站不住，正确行为是：

      1. 退回默认的 `jre/`（exe 旁边那个）照常启动 —— 用户至少还能进设置页改回来；
      2. 日志里 WARNING 说清楚是哪条路径失效了；
      3. **把配置一起改回默认** —— 不然每次启动都要再警一次，设置页里还显示着
         那个错的路径，用户根本不知道「其实没生效」。
    """
    rep = Report("F jre 路径写错时自动退回默认")
    bogus = r"D:\这个路径不存在\jre"
    (root / "config.json").write_text(
        json.dumps(
            {
                "hide_on_launch": False,
                "auto_update": False,          # 这条里不跑网络
                "github_mirror": "",
                "current_profile": "默认",
                "profiles": {
                    "默认": {
                        "data_dir": str(root / "f_data"),
                        "min_playtime": 20,
                        "max_backups": 20,
                        "auto_backup": True,
                    }
                },
                "jvm": {"jre_path": bogus},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "config.json.bad").unlink(missing_ok=True)

    info = _run_once(root, seconds)
    rep.check("窗口照常出现（没被一个写错的路径拦在门外）",
              info["window"] is not None,
              f"{info['window_at']:.2f}s" if info["window_at"] else "没等到")
    rep.check("关窗口能干净退出", info["clean_exit"])
    rep.check("退出码是 0", info["exit_code"] == 0, f"exit={info['exit_code']}")

    log = _read_log(root)
    rep.check("日志点明了失效的那条路径", bogus in log or "退回默认" in log)
    rep.check("日志没有 Traceback", "Traceback" not in log)
    on_disk = json.loads((root / "config.json").read_text(encoding="utf-8"))
    rep.check("★ 配置被改回默认的 jre（不会每次启动都再警一次）",
              on_disk.get("jvm", {}).get("jre_path") == "jre",
              repr(on_disk.get("jvm", {}).get("jre_path")))
    rep.check("没被误判成「配置损坏」而留档重建（分类还在）",
              not (root / "config.json.bad").exists()
              and "默认" in on_disk.get("profiles", {}))
    return rep


# --------------------------------------------------------------------------
# 场景 E：脏启动配置 / 自定义启动参数
# --------------------------------------------------------------------------
def scenario_dirty_jvm(root: Path, seconds: float) -> Report:
    """jvm 段被写坏时，必须逐项退回默认值照常启动。

    jvm 段和「额外启动参数」现在都是用户能直接手改的东西（config.json、
    设置页）。设置页会拦住填错，但手改文件拦不住。原则跟坏配置自愈一样：
    能退化成默认值就继续跑，绝不能因为一行配置就打不开启动器。

    （「jvm 整个不是对象」这种更极端的输入由 code_regression 覆盖 ——
      那是个纯配置归一化路径，没必要为它多花十几秒起一次 exe。）
    """
    rep = Report("E 脏 jvm 配置 / 脏自定义参数")
    cfg_data = {
        "hide_on_launch": False,
        "use_mirror": True,            # 废弃键，留着验证被忽略
        "github_mirror": "ghfast.top",  # 只写主机名也该被补成完整前缀
        "max_log_files": 5,
        "auto_update": False,          # 这条里不跑网络，省得被限流干扰
        "current_profile": "默认",
        # 自定义参数：引号没闭合（设置页会拦住，手改文件拦不住）
        "extra_vm_args": '-Xmx4G -Dfoo="未闭合',
        "extra_program_args": "",
        "save_game_log": False,        # 顺带走上「不落盘」那条分支
        # 破坏性开关的脏值：写成字符串"开关"以外的字眼 → 必须退回默认 False
        # （bool("false") 是 True 那个坑的反面：认不出来时绝不能变成 True）
        "permanent_delete": "开",
        "profiles": {
            "默认": {
                "data_dir": str(root / "dirty_jvm_data"),
                "min_playtime": 20,
                "max_backups": 20,
                "auto_backup": True,
            }
        },
        # jvm 段字段类型全不对
        "jvm": {
            "main_class": "-这是选项不是类名",
            "vm_args": 12345,
            "program_args": {"不是": "数组"},
            "jre_path": "   ",
        },
    }
    (root / "config.json").write_text(
        json.dumps(cfg_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "config.json.bad").unlink(missing_ok=True)

    info = _run_once(root, seconds)
    rep.check("窗口正常出现", info["window"] is not None,
              f"{info['window_at']:.2f}s" if info["window_at"] else "没等到")
    rep.check("脏 jvm 段没把启动器搞崩", info["alive_before_close"])
    rep.check("关窗口能干净退出", info["clean_exit"])
    rep.check("退出码是 0", info["exit_code"] == 0, f"exit={info['exit_code']}")
    rep.check("脏配置没被误判为损坏而留档",
              not (root / "config.json.bad").exists())

    log = _read_log(root)
    rep.check("日志点名了非法的主类名", "jvm.main_class" in log, )
    rep.check("日志点名了非数组的 vm_args", "jvm.vm_args 必须是字符串数组" in log)
    rep.check("日志点名了非数组的 program_args",
              "jvm.program_args 必须是字符串数组" in log)
    rep.check("日志点名了空的 jre_path", "jvm.jre_path" in log)
    rep.check("日志点名了脏的镜像地址（并说明退回直连）",
              "镜像地址" in log and "直连" in log)
    rep.check("日志点名了脏的镜像候选清单（退回内置默认）",
              "镜像可选项" in log)
    rep.check("日志点名了脏的日志保留份数", "max_log_files" in log)
    rep.check("日志点名了脏的「直接删除」开关（并说明按默认 False 处理）",
              "permanent_delete" in log and "False" in log)
    # ★ 反面：这是个破坏性开关，认不出来时绝不能变成 True。exe 退出时会
    #   把配置重写一遍（on_closing → config.save），所以磁盘上的那份
    #   就是「exe 认为的值」。
    on_disk = json.loads(
        (root / "config.json").read_text(encoding="utf-8")
    )
    rep.check("脏开关落盘成了 False（默认走回收站，没被字符串顶成 True）",
              on_disk.get("permanent_delete") is False,
              repr(on_disk.get("permanent_delete")))
    rep.check("日志没有 Traceback", "Traceback" not in log)
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description="打包版 exe 边界条件冒烟")
    ap.add_argument("--exe", default=None,
                    help="要测的 exe（默认取运行时目录里的 Mindustry启动器.exe）")
    ap.add_argument("--seconds", type=float, default=12.0,
                    help="窗口起来后再观察多少秒（默认 12，够后台 GC 跑完）")
    ap.add_argument("--keep", action="store_true", help="保留沙箱目录，方便排查")
    ap.add_argument("--only", default=None,
                    help="只跑指定场景，逗号分隔：A / B / D / E / F / G（B 含孤儿对象对照）")
    args = ap.parse_args()

    want = None
    if args.only:
        want = {s.strip().upper() for s in args.only.split(",") if s.strip()}
        bad = want - {"A", "B", "D", "E", "F", "G"}
        if bad:
            print(f"--only 只认 A/B/D/E/F/G，收到：{sorted(bad)}")
            return 2

    if args.exe:
        exe_src = Path(args.exe).resolve()
    else:
        runtime = find_runtime_root(required=False)
        exe_src = (runtime or ROOT) / EXE_NAME
    if not exe_src.is_file():
        print(f"找不到 exe：{exe_src}")
        print("先跑 python _tools/build.py --deploy，或用 --exe 指一个路径")
        return 2

    print("=" * 64)
    print(f"目标 exe：{exe_src}")
    print(f"体积 {exe_src.stat().st_size / 1048576:.1f} MB，"
          f"改动时间 {time.strftime('%H:%M:%S', time.localtime(exe_src.stat().st_mtime))}")
    print("=" * 64)

    reps: list[Report] = []
    root: Path | None = None
    no_jre_root: Path | None = None
    try:
        if want is None or (want & {"A", "B", "E", "F"}):
            print("准备沙箱（要连 jre 一起拷，约 60 MB）…")
            root = _make_sandbox(exe_src)
            print(f"沙箱：{root}")
            if want is None or "A" in want:
                reps.append(scenario_bad_config(root, args.seconds))
            if want is None or "B" in want:
                reps.append(scenario_dirty_and_gc(root, args.seconds))
            if want is None or "E" in want:
                reps.append(scenario_dirty_jvm(root, args.seconds))
            if want is None or "F" in want:
                reps.append(scenario_wrong_jre_path(root, args.seconds))
        if want is None or (want & {"D", "G"}):
            # D 和 G 都要一个「没有 jre/」的沙箱，共用一份（各拷 60 MB 太浪费）。
            # 顺序：先 G 再 D —— G 会把 launcher.log 清掉重来，D 读的必须是
            # 它自己那一轮的日志（里面得有那条「找不到」的 critical）。
            print("准备「没有 jre」的沙箱（约 60 MB）…")
            no_jre_root = _make_sandbox(exe_src, with_jre=False)
            print(f"沙箱：{no_jre_root}")
            if want is None or "G" in want:
                reps.append(
                    scenario_env_java(no_jre_root, args.seconds,
                                      exe_src.parent / "jre")
                )
            if want is None or "D" in want:
                reps.append(scenario_missing_jre(no_jre_root))
    finally:
        for r in reps:
            r.dump()

        ok = bool(reps) and all(r.ok for r in reps)
        print("\n" + "=" * 64)
        if ok:
            print("结论：通过 —— 打包版在坏配置 / 脏数据 / 孤儿对象 / 缺 jre / "
                  "脏启动配置 / jre 路径写错 / 靠环境变量兜 Java 下都表现正常")
        else:
            failed = [lbl for r in reps for lbl, o, _ in r.items if not o]
            print(f"结论：有问题 —— {len(failed)} 项没过：{failed}")
        print("=" * 64)

        for path in (root, no_jre_root):
            if path is None:
                continue
            if args.keep:
                print(f"保留现场：{path}")
            else:
                shutil.rmtree(path, ignore_errors=True)
        if root is not None or no_jre_root is not None:
            print("已清理沙箱")

    return 0 if (reps and all(r.ok for r in reps)) else 1


if __name__ == "__main__":
    sys.exit(main())
