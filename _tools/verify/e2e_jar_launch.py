"""端到端：用优化后的代码组装 jar，真实启动游戏，确认能正常进主菜单。

同时测出「点启动 → 能玩」的实际耗时变化。
"""
import ctypes
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

def _find_project_root(start: Path) -> Path:
    """往上找项目根（含 launcher/ 包的那一层）。脚本放在 _tools/ 下也照样对。"""
    for p in (start, *start.parents):
        if (p / "launcher" / "__init__.py").is_file():
            return p
    raise RuntimeError("找不到项目根：往上没找到 launcher/__init__.py")


ROOT = _find_project_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # _tools/ 共用路径模块
from _paths import DATA, JRE, LOG, MANIFESTS, VERSIONS  # noqa: E402
sys.path.insert(0, str(ROOT))
from launcher.config import DEFAULT_VM_ARGS, ConfigManager  # noqa: E402
from launcher.gamecmd import build_java_command  # noqa: E402
from launcher.storage import CASStore, VersionManager  # noqa: E402

user32 = ctypes.windll.user32


def game_windows():
    found = []
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_int))

    def cb(hwnd, lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            t = buf.value
            if "Mindustry" in t and "启动器" not in t:
                found.append(t)
        return True

    user32.EnumWindows(PROC(cb), 0)
    return found


store = CASStore(VERSIONS)
vm = VersionManager(store, MANIFESTS)
jre = JRE / "bin" / "java.exe"


def _load_launch_config() -> tuple[str, list[str], Path]:
    """从 config.json 的 jvm 段取启动配置（原 Mindustry.json 已并进去）。

    这里故意直接读文件而不实例化 ConfigManager：验证脚本不该有
    「顺手把配置写回去」的可能。缺文件/字段就退回内置默认值。
    """
    cfg: dict = {}
    cfg_file = DATA / "config.json"
    if cfg_file.is_file():
        try:
            raw = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg = raw
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            print(f"  ⚠️ 读 config.json 失败，用默认值: {e}")
    jvm = cfg.get("jvm") if isinstance(cfg.get("jvm"), dict) else {}
    main_class = jvm.get("main_class") or \
        ConfigManager.JVM_DEFAULTS["main_class"]
    vm_args = jvm.get("vm_args")
    if not isinstance(vm_args, list):
        vm_args = list(DEFAULT_VM_ARGS)
    profiles = cfg.get("profiles") if isinstance(cfg.get("profiles"), dict) else {}
    prof = profiles.get(cfg.get("current_profile"))
    data_dir = prof.get("data_dir") if isinstance(prof, dict) else None
    return main_class, vm_args, Path(data_dir) if data_dir else TMP / "data"


T, V = "Mindustry", "160.3"
TMP = Path(tempfile.mkdtemp(prefix="mdt_e2e_"))
jar = TMP / f"{T}_{V}.jar"

print("=" * 62)
print("1. 组装 jar（优化后代码）")
print("=" * 62)
t_assemble0 = time.perf_counter()
vm.build_runtime_jar(T, V, jar)
t_assemble = time.perf_counter() - t_assemble0
print(f"  {T} {V}: {t_assemble*1000:.0f} ms  "
      f"({jar.stat().st_size/1048576:.1f} MB)")

print()
print("=" * 62)
print("2. 启动游戏并跟踪输出")
print("=" * 62)
main_class, vm_args, data_dir = _load_launch_config()
print(f"  主类 {main_class}，数据目录 {data_dir}")
cmd = build_java_command(jre, jar, main_class, data_dir, vm_args)
si = subprocess.STARTUPINFO()
si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
si.wShowWindow = subprocess.SW_HIDE

t0 = time.perf_counter()
p = subprocess.Popen(cmd, cwd=str(TMP), stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, startupinfo=si, text=True,
                     bufsize=1, encoding="utf-8", errors="replace")
lines = []
errors = []


def reader():
    try:
        for ln in p.stdout:
            ts = time.perf_counter() - t0
            lines.append((ts, ln.rstrip()))
            low = ln.lower()
            if "error" in low or "exception" in low or "failed" in low:
                errors.append((ts, ln.rstrip()))
    except Exception:
        pass


threading.Thread(target=reader, daemon=True).start()

t_window = None
t_load = None
deadline = t0 + 45
while time.perf_counter() < deadline:
    el = time.perf_counter() - t0
    if t_window is None and game_windows():
        t_window = el
    for ts, ln in lines:
        if t_load is None and "Total time to load" in ln:
            m = re.search(r"Total time to load:\s*(\d+)\s*ms", ln)
            if m:
                t_load = ts
                load_ms = int(m.group(1))
    if t_load is not None and el > t_load + 2:
        break
    if p.poll() is not None:
        break
    time.sleep(0.05)

if p.poll() is None:
    p.terminate()
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()

print(f"  游戏窗口出现      : {t_window:.2f} s" if t_window else "  窗口未出现")
print(f"  资源加载完成      : {t_load:.2f} s" if t_load else "  未收到加载日志")
m = None
for _, ln in lines:
    mm = re.search(r"Total time to load:\s*(\d+)\s*ms", ln)
    if mm:
        m = int(mm.group(1))
if m:
    print(f"  游戏自报加载耗时  : {m} ms")

print()
print("  关键日志:")
for ts, ln in lines[:12]:
    if any(k in ln for k in ("[Mindustry]", "Total time", "SDL", "GL]",
                             "RAM]", "JAVA]")):
        print(f"    {ts:6.2f}s  {ln[:100]}")

print()
print("=" * 62)
print("3. 结果")
print("=" * 62)
ok = t_load is not None and not errors
print(f"  游戏正常启动到加载完成: {'是' if t_load else '否'}")
print(f"  输出中的报错条数      : {len(errors)}")
if errors:
    for ts, ln in errors[:5]:
        print(f"    {ts:6.2f}s  {ln[:110]}")

if t_load:
    total = t_assemble + t_load
    print()
    print(f"  组装 jar            : {t_assemble:.2f} s")
    print(f"  游戏自身            : {t_load:.2f} s")
    print(f"  点启动 → 能玩        : {total:.2f} s")

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 62)
print("端到端:", "通过 ✓" if ok else "有问题 ✗")
print("=" * 62)
