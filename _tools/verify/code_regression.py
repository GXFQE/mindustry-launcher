# -*- coding: utf-8 -*-
"""验证本轮改动：
1) 拼出的 jar 每个条目的内容与 CAS 对象逐字节一致（用真实数据全量核对）
2) 出错行为没退步：缺对象报错带 sha、失败不留半个 jar
3) 后台 GC 会给「正在启动」让路，并且不会永久卡住
4) 导入进行中跑 GC：新对象不能被当孤儿删掉（带反向对照）
5) 恢复备份的安全边界：快照建不成功必须中止、旧内容走回收站
6) 自定义启动参数：解析（反斜杠/引号）、危险项告警、命令拼装顺序
7) 游戏启动配置并进 config.json 的 jvm 段（原 Mindustry.json）
8) 游戏输出日志：落盘、内存环形缓冲、保留份数、读流出错不静默死掉
8b) 输出编码：管道钉 UTF-8（Java 没控制台时默认写 GBK）+ 解不开时退系统
    代码页；拿真 jre 走一遍管道，并用「不传参数读不出中文」做反向对照
9) 锁的作用域：_proc_lock 可重入、with 块里不弹窗（持锁弹窗会自锁）
10) GitHub 镜像地址的整理规则 + 日志保留份数的范围钳制
10b) 镜像候选清单（config.json 的 github_mirror_presets）的容错整理
11) 滚轮的归属：TCombobox 的滚轮换选项被摘掉、设置页滚动不再靠 Enter/Leave
12) 设置页「保存并返回」：保存归 save_settings、跳转归 save_and_return，
    源码里不许再有「不保存就离开设置页」的按钮；主操作钉右下角、次要在左
13) 游戏退出自动关闭启动器：开关的脏值容错（bool("false") 是 True！）、
    自动关闭必须排在自动备份之后
14) 「直接删除（不进回收站）」开关：默认关、脏值不许打开它、只有用户文件
    那几处删除受它影响；delete_path 的分发要真的删得掉（不是空实现）
15) Java 路径（JRE / JDK 都收）可在设置页改 + 「检测」真跑一次 java -version
    （后台线程）；路径写坏时退回默认 jre 而不是让启动器打不开；连 jre 都
    没有时去 JAVA_HOME / PATH 里兜一个（候选必须真跑过才认，且不写回配置）
16) 设置页的灰字提示不被裁：每条提示都得套 _auto_wrap_hint（放不下就折行 ——
    grid 对超宽标签既不报错也不换行，右边直接少一截）
17) **配置格式版本与迁移**：新配置写上 config_version；缺这个键的老配置
    按第 0 代升级并**立刻落盘**；迁移不弄丢老值；不认识的顶层键**原样保留**
    （新配置在旧版本里过一趟不掉东西）；明确废弃的键（use_mirror）丢掉；
    更新的配置只读认识的项、**版本号只升不降**；顶层不是对象时按损坏处理
18) **版本来源注册表**：内置两个来源的档位与版本号解析与重构前**逐条一致**；
    没登记的类型按老规则处理；配置里能加/覆盖来源（同名覆盖只改用户写的字段）；
    资产匹配用正则或可调用对象都可以
19) **扩展点**：钩子表是兼容性契约；坏扩展不影响好扩展（载入炸 / 没 register /
    回调抛异常都只记日志）；下划线开头的文件跳过；call() 收集返回值并传 kwargs；
    挂到不认识的钩子上要明确报错；环境变量能整体停用
20) 版本号只有一处（launcher/version.py，包外暴露的与它一致）
21) **_paths 的懒解析**：import 时不去找运行时目录（刚 clone 不会 import 就炸）；
    MDT_RUNTIME_DIR 可覆盖；找不到时 required=False 给 None、True 给可读报错

项数见末尾统计（改完 launcher/ 就该跑一遍）。
"""
import ast
import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

def _find_project_root(start: Path) -> Path:
    """往上找项目根（含 launcher/ 包的那一层）。脚本放在 _tools/ 下也照样对。"""
    for p in (start, *start.parents):
        if (p / "launcher" / "__init__.py").is_file():
            return p
    raise RuntimeError("找不到项目根：往上没找到 launcher/__init__.py")


ROOT = _find_project_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(ROOT))
# 共用路径模块在 _tools/ 里
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import DATA, MANIFESTS, VERSIONS  # noqa: E402

import launcher.storage as S  # noqa: E402
from launcher.gui_core import CoreMixin  # noqa: E402
from launcher.gui_game import GameMixin  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


# ---------------------------------------------------------------- 1. 正确性
def test_jar_content_matches_cas():
    print("\n[1] jar 条目内容 vs CAS 对象（真实数据全量核对）")
    # ⚠️ 真实数据在**运行时目录**里（拆分后不在代码仓库中），由 _paths 定位
    V = VERSIONS
    vm = S.VersionManager(S.CASStore(V), V / "manifests")
    manifest = json.loads(
        (V / "manifests" / "MindustryX_2026.09.X37.json").read_text()
    )
    files = manifest["files"]
    out = Path(tempfile.mktemp(suffix=".jar"))
    vm.build_runtime_jar("MindustryX", "2026.09.X37", out)

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        check("条目数与清单一致", len(names) == len(files),
              f"{len(names)} vs {len(files)}")
        check("条目名集合与清单一致", set(names) == set(files))

        store = S.CASStore(V)
        bad = 0
        checked = 0
        for rel, sha in files.items():
            data = store.get_file(sha)
            got = zf.read(rel)
            if got != data:
                bad += 1
                if bad <= 3:
                    print(f"       内容不一致: {rel}")
            checked += 1
        check("全部条目内容逐字节一致", bad == 0,
              f"核对了 {checked} 个，不一致 {bad} 个")
        # 顺带确认 zip 没做压缩（ZIP_STORED），否则游戏读起来会白白解压
        check("全部为 STORED（未压缩）",
              all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist()))
    out.unlink(missing_ok=True)


# ---------------------------------------------------------------- 2. 错误处理
def test_error_handling():
    print("\n[2] 出错行为")
    sb = Path(tempfile.mkdtemp()) / "sb"
    store = S.CASStore(sb / "versions")
    vm = S.VersionManager(store, sb / "versions" / "manifests")

    # 缺对象
    vm.manifests_dir.mkdir(parents=True, exist_ok=True)
    mf = vm.manifests_dir / "T_1.json"
    mf.write_text(json.dumps({
        "type": "T", "version": "1",
        "files": {"a.txt": "a" * 64, "b.txt": "b" * 64},
    }))
    out = sb / "out.jar"
    try:
        vm.build_runtime_jar("T", "1", out)
        check("缺对象时抛异常", False, "居然没抛")
    except FileNotFoundError as e:
        check("缺对象抛 FileNotFoundError", True)
        check("报错里带上了缺失的 sha", "a" * 64 in str(e), str(e)[:60])
    check("失败后没有留下半个 jar", not out.exists())

    # 清单不存在
    try:
        vm.build_runtime_jar("Nope", "999", out)
        check("清单不存在时抛异常", False, "居然没抛")
    except FileNotFoundError:
        check("清单不存在抛 FileNotFoundError", True)

    # 超大清单走并行分支，中途失败也要清干净
    mf2 = vm.manifests_dir / "T_2.json"
    files = {f"f{i}.txt": f"{i:064d}" for i in range(600)}
    store.store_file(b"x")  # 只放一个无关对象，其余全缺
    files["f0.txt"] = store.sha256(b"x")
    mf2.write_text(json.dumps({"type": "T", "version": "2", "files": files}))
    out2 = sb / "out2.jar"
    try:
        vm.build_runtime_jar("T", "2", out2)
        check("并行分支缺对象时抛异常", False, "居然没抛")
    except FileNotFoundError:
        check("并行分支缺对象抛 FileNotFoundError", True, "(600 条目走并行分支)")
    check("并行分支失败后也没留半个 jar", not out2.exists())


# ---------------------------------------------------------------- 3. GC 让路
class FakeRoot:
    def __init__(self):
        self.calls = []

    def after(self, delay, func=None):
        self.calls.append(delay)
        return "id"


class FakeExecutor:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *a, **k):
        self.submitted.append(fn)
        return None


class FakeVM:
    """替掉真的 VersionManager，只记录 GC 有没有被真正调起来。"""

    def __init__(self):
        self.called = []

    def garbage_collect(self, stop_event=None):
        self.called.append(1)
        return 0


class StubGC(GameMixin, CoreMixin):
    def __init__(self):
        self.stop_event = threading.Event()
        self._launching = threading.Event()
        # GC 现在要给「启动中」和「预热中」两种状态让路，得有个空闲标志
        self._preheat_idle = threading.Event()
        self._preheat_idle.set()
        self.executor = FakeExecutor()
        self.root = FakeRoot()
        self.gui_queue = queue.Queue()
        self.version_manager = FakeVM()

    @property
    def ran(self):
        return self.version_manager.called


def test_gc_yields():
    print("\n[3] 后台 GC 给启动让路")
    s = StubGC()

    # 没在启动 -> 正常提交
    s._start_background_gc()
    check("未启动时 GC 正常提交", len(s.executor.submitted) == 1)

    # 正在启动 -> 推迟，不提交
    s2 = StubGC()
    s2._launching.set()
    s2._start_background_gc()
    check("启动中 GC 不提交", len(s2.executor.submitted) == 0)
    check("启动中 GC 改为延后重试", s2.root.calls == [5000],
          f"after 延时={s2.root.calls}")

    # 让路之后必须能恢复（否则 GC 就永久罢工了）
    s3 = StubGC()
    s3._launching.set()
    s3._start_background_gc()
    check("让路后没有真的跑 GC", s3.ran == [])
    s3._launching.clear()
    s3._start_background_gc()
    check("启动结束后 GC 恢复提交", len(s3.executor.submitted) == 1)
    s3.executor.submitted[0]()          # 真跑一次，确认能跑通
    check("恢复后 GC 确实执行了", s3.ran == [1])

    # 排队期间才点启动 -> 真 _run_gc_task 里的二次确认要拦住
    s4 = StubGC()
    s4._launching.set()
    s4._run_gc_task()
    check("排队期间改成启动则 GC 让路", s4.ran == [])
    check("排队让路时排了延后重试（而不是直接丢掉）",
          s4.gui_queue.qsize() == 1)

    # 没在启动时 _run_gc_task 走正常路径
    s4b = StubGC()
    s4b._run_gc_task()
    check("未启动时 _run_gc_task 正常执行 GC", s4b.ran == [1])

    # stop_event 仍然生效
    s5 = StubGC()
    s5.stop_event.set()
    s5._start_background_gc()
    check("关窗后 GC 不再提交", len(s5.executor.submitted) == 0)


def test_manifest_digest():
    print("\n[4] 清单指纹（预热结果的有效性判据）")
    V = VERSIONS
    vm = S.VersionManager(S.CASStore(V), V / "manifests")
    d1 = vm.manifest_digest("MindustryX", "2026.09.X37")
    d2 = vm.manifest_digest("MindustryX", "2026.09.X37")
    check("同一清单两次结果相同", d1 == d2 and len(d1) == 64, d1[:16])
    d3 = vm.manifest_digest("Mindustry", "160.3")
    check("不同版本结果不同", d1 != d3, d3[:16])
    check("清单不存在返回空串（不抛异常）",
          vm.manifest_digest("Nope", "999") == "")


# ---------------------------------------------------------------- 5. 预热
class FakeListbox:
    def __init__(self):
        self._sel = (0,)

    def curselection(self):
        return self._sel


class FakeVM2:
    """只记录调用，不真拼 jar。"""

    def __init__(self, root):
        self.root = root
        self.built = []
        self.gc_calls = []

    def manifest_digest(self, vtype, version):
        return f"digest-{vtype}-{version}"

    def build_runtime_jar(self, vtype, version, out):
        self.built.append((vtype, version, str(out)))
        Path(out).write_bytes(b"fake jar " + vtype.encode())
        return out

    def garbage_collect(self, stop_event=None):
        self.gc_calls.append(1)
        return 0


class StubApp(GameMixin, CoreMixin):
    """把 Tk 换成假的，专门测预热状态机和它与 GC 的互斥。"""

    def __init__(self, tmp):
        self.stop_event = threading.Event()
        self._bench = False
        self._launching = threading.Event()
        self._preheat_root = Path(tmp) / "mdt_preheat"
        self._preheat_lock = threading.Lock()
        self._preheat_jar = None
        self._preheat_key = None
        self._preheat_target = None
        self._preheat_idle = threading.Event()
        self._preheat_idle.set()
        self._preheat_after_id = None
        self._versions_cache = [
            {"name": "V1", "type": "T", "raw_version": "1"},
            {"name": "V2", "type": "T", "raw_version": "2"},
        ]
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.root = FakeRoot()
        self.gui_queue = queue.Queue()
        self._proc_lock = threading.Lock()
        self.current_process = None
        self.version_manager = FakeVM2(tmp)
        self.launch_btn = FakeListbox()
        self.status_text = None

    def _update_version_listbox(self, versions):  # 缩掉的 GUI 部分
        pass

    def set_status(self, text, log_level=None):
        self.status_text = text

    def on_closing(self):  # 预热清理要用
        pass


def wait_idle(app, timeout=15):
    return app._preheat_idle.wait(timeout=timeout)


def test_preheat():
    print("\n[5] 预热：拼一次、复用到会话结束")
    tmp = Path(tempfile.mkdtemp())
    app = StubApp(tmp)
    v1, v2 = app._versions_cache
    k1 = app._preheat_key_of(v1)
    k2 = app._preheat_key_of(v2)

    check("初始没有预热结果", app._take_preheated_jar(k1) is None)
    check("初始不算忙", app.preheat_busy() is False)

    # 预热 V1
    app._preheat(v1)
    check("预热开始后标记为忙", app.preheat_busy() is True)
    wait_idle(app)
    check("预热结束后不再忙", app.preheat_busy() is False)
    check("真的拼了一次", len(app.version_manager.built) == 1)
    jar1 = app._take_preheated_jar(k1)
    check("V1 能取到预热结果", jar1 is not None and jar1.exists())

    # 取另一个版本 -> 不命中
    check("换版本不命中（不会拿错 jar）",
          app._take_preheated_jar(k2) is None)

    # 同一个版本重复预热 -> 不重复拼
    app._preheat(v1)
    wait_idle(app)
    check("已就绪的版本不重复拼",
          len(app.version_manager.built) == 1,
          f"拼了 {len(app.version_manager.built)} 次")

    # 预热 V2 -> 覆盖，旧的要删掉
    app._preheat(v2)
    wait_idle(app)
    jar2 = app._take_preheated_jar(k2)
    check("换版本后新结果就绪", jar2 is not None and jar2.exists())
    check("V2 就绪后 V1 的结果已清掉", not jar1.exists())
    check("同时只留一个预热 jar",
          len(list(app._preheat_root.glob("*.jar"))) == 1)

    # 拼失败 -> 不能留下半个文件，也不能卡在"忙"
    def boom(vtype, version, out):
        Path(out).write_bytes(b"half")
        raise OSError("模拟失败")

    app.version_manager.build_runtime_jar = boom
    app._preheat({"name": "V3", "type": "T", "raw_version": "3"})
    wait_idle(app)
    check("拼失败后不卡在忙", app.preheat_busy() is False)
    check("拼失败不留半个 jar",
          len(list(app._preheat_root.glob("*.jar"))) == 1,
          "应该只剩 V2 那一个")

    # 退出清理
    app.cleanup_preheat()
    check("退出时清掉预热 jar", not jar2.exists())
    check("退出时清掉状态",
          app._preheat_jar is None and app._preheat_key is None)

    # 残留清理：只清够老的
    app._preheat_root.mkdir(parents=True, exist_ok=True)
    fresh = app._preheat_root / "fresh.jar"
    stale = app._preheat_root / "stale.jar"
    fresh.write_bytes(b"x")
    stale.write_bytes(b"x")
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))
    app._cleanup_stale_preheat()
    check("残留清理不动新的（游戏可能还在用）", fresh.exists())
    check("残留清理删掉旧的", not stale.exists())

    # GC 见到预热在跑要让路
    app2 = StubApp(Path(tempfile.mkdtemp()))
    app2._preheat_idle.clear()          # 假装预热正在跑
    app2._start_background_gc()
    check("预热期间 GC 让路", app2.version_manager.gc_calls == []
          and app2.root.calls == [5000])
    app2._preheat_idle.set()
    app2._start_background_gc()
    app2.executor.submit(lambda: None)
    time.sleep(0.5)
    check("预热结束后 GC 恢复", app2.version_manager.gc_calls == [1])
    app2.executor.shutdown(wait=False)
    app.executor.shutdown(wait=False)


def test_preheat_on_close():
    """关窗那一刻预热正拼到一半 —— 最容易漏掉那个 jar 的场景。

    顺序很关键：on_closing 是「先 cleanup_preheat()，再等 executorshutdown」，
    所以那次清理跑的时候拼装还没结束。如果拼装线程收尾时照样把结果挂上去，
    就没人负责删了（下次启动的残留清理只清 24 小时前的），白占 100+MB。
    """
    print("\n[6] 关窗时预热正拼到一半")
    tmp = Path(tempfile.mkdtemp())
    app = StubApp(tmp)
    started = threading.Event()
    gate = threading.Event()

    def slow_build(vtype, version, out):
        Path(out).write_bytes(b"half-built")   # 先落个文件，模拟拼到一半
        started.set()
        gate.wait(10)                          # 卡住，等测试放行
        return out

    app.version_manager.build_runtime_jar = slow_build
    app._preheat({"name": "V9", "type": "T", "raw_version": "9"})
    check("预热确实开始拼了", started.wait(5))

    # 复刻 on_closing 的真实顺序
    app.stop_event.set()
    app.cleanup_preheat()          # 此时拼装还在跑
    gate.set()                     # 放行，让它拼完
    app.executor.shutdown(wait=True)
    app.cleanup_preheat()          # 第二次清理（收手后再清一次）

    left = list(app._preheat_root.glob("*.jar"))
    check("半路关窗不留孤儿 jar", left == [], f"残留 {[p.name for p in left]}")
    check("半路关窗不留状态", app._preheat_jar is None)
    check("半路关窗后临时目录也一并清掉", not app._preheat_root.exists())

    # 反向对照：同样卡在半路的拼装，只要没在退出，结果就该留住。
    # 否则上面那条断言可能只是「glob 永远找不到东西」的恒真式。
    tmp2 = Path(tempfile.mkdtemp())
    app2 = StubApp(tmp2)
    gate2 = threading.Event()

    def slow_build2(vtype, version, out):
        Path(out).write_bytes(b"half-built")
        gate2.wait(10)
        return out

    app2.version_manager.build_runtime_jar = slow_build2
    app2._preheat({"name": "V9", "type": "T", "raw_version": "9"})
    gate2.set()
    app2.executor.shutdown(wait=True)
    kept = list(app2._preheat_root.glob("*.jar"))
    check("（对照）没在退出时结果会留住", len(kept) == 1,
          f"找到 {len(kept)} 个")
    app2.cleanup_preheat()


def main():
    # 故意制造的「对象丢失」是测试用例本身，别让它刷屏
    logging.getLogger("launcher.storage").setLevel(logging.CRITICAL)
    test_jar_content_matches_cas()
    test_error_handling()
    test_gc_yields()
    test_manifest_digest()
    test_preheat()
    test_preheat_on_close()
    test_import_vs_gc_race()
    test_restore_safety()
    test_launch_args()
    test_jvm_config()
    test_game_log()
    test_output_encoding_end_to_end()
    test_mirror_and_log_keep()
    test_lock_scope()
    test_wheel_routing()
    test_settings_flow_and_autoclose()
    test_jre_probe_and_permanent_delete()
    test_release_compat_layer()
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  失败:", f)
        return 1
    return 0


# ------------------------------------------------- 7. 导入进行中跑 GC（真实踩过）
def test_import_vs_gc_race():
    """导入期间后台 GC 撞进来，绝不能把刚写进去的对象当孤儿删掉。

    `add_version_from_jar` 是「先写几千个对象、最后才写清单」，中间那段时间
    新对象在 GC 眼里全是无人引用的。旧代码在这里 100% 丢失对象：清单照样
    写成功、版本列表也正常，一点「启动游戏」才报「对象丢失: <sha>」。
    """
    print("\n[7] 导入进行中跑 GC")
    sb = Path(tempfile.mkdtemp()) / "sb"
    sb.mkdir(parents=True, exist_ok=True)
    store = S.CASStore(sb / "versions")
    vm = S.VersionManager(store, sb / "versions" / "manifests")

    jar = sb / "in.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        for i in range(40):
            # 内容各不相同 -> 40 个互不相同的对象
            zf.writestr(f"a/{i}.class", b"#" * 200 + str(i).encode())

    real_store_file = store.store_file
    stored = []

    def slow_store_file(data):
        sha = real_store_file(data)
        stored.append(sha)
        time.sleep(0.01)        # 把导入拉长，给 GC 留出撞进来的机会
        return sha

    store.store_file = slow_store_file
    gc_calls = []

    def run_gc():
        gc_calls.append(vm.garbage_collect())

    t_import = threading.Thread(target=vm.add_version_from_jar,
                                args=(jar, "146", "Mindustry"))
    t_import.start()
    time.sleep(0.2)             # 让导入先跑起来（此时它正持着批量写锁）
    t_gc = threading.Thread(target=run_gc)
    t_gc.start()
    t_import.join(60)
    t_gc.join(60)
    store.store_file = real_store_file

    check("导入 40 个对象都写完了", len(stored) == 40, f"{len(stored)}/40")
    missing = [s for s in stored if not store._object_path(s).exists()]
    check("导入期间 GC 没删掉新对象", not missing, f"丢了 {len(missing)} 个")
    out = sb / "out.jar"
    try:
        vm.build_runtime_jar("Mindustry", "146", out)
        check("导入后能正常拼出 jar", True)
    except Exception as e:
        check("导入后能正常拼出 jar", False, str(e)[:70])

    # 反向对照：宽限期一过，孤儿对象该回收还得回收。
    # 否则上面那条可能只是「GC 什么都不删」的恒真式。
    vm.delete_version("Mindustry", "146")
    removed = store.garbage_collect(set(), grace_seconds=0)
    check("（对照）宽限期过后孤儿对象照删", removed >= 40,
          f"回收了 {removed} 个")
    # 再确认一次「新对象受保护」不是幻觉：同一次调用加回宽限期就删不动
    store.store_file(b"brand-new-object")
    removed2 = store.garbage_collect(set())
    check("（对照）宽限期内的新对象删不动", removed2 == 0,
          f"删了 {removed2} 个")


# ------------------------------------------------- 8. 恢复备份的安全边界
class FakeConfig:
    """只给 BackupManager 要用的那几口。"""

    def __init__(self, data_dir, permanent_delete=False):
        self._data_dir = Path(data_dir)
        self._permanent_delete = permanent_delete

    def get_save_path(self, name):
        return str(self._data_dir)

    def get_profile_setting(self, name, key):
        return {"max_backups": 20, "auto_backup": True, "min_playtime": 0}[key]

    def get(self, key):
        return self._permanent_delete if key == "permanent_delete" else None


def test_restore_safety():
    """恢复备份：没有退路就不许动用户的存档。"""
    print("\n[8] 恢复备份的安全边界")
    sb = Path(tempfile.mkdtemp())
    data = sb / "data"
    (data / "saves").mkdir(parents=True)
    save_file = data / "saves" / "world.msav"

    store = S.CASStore(sb / "backups")
    bm = S.BackupManager(store, FakeConfig(data), sb / "backups", "测试")

    save_file.write_bytes(b"v1")
    manifest = bm.create_backup("第一版")
    check("备份能建成", manifest.exists())
    save_file.write_bytes(b"v2")          # 之后又玩了一局

    # (1) 快照建不起来 -> 必须中止，一个文件都不许动
    real_zip_directory = S.zip_directory

    def boom(*a, **k):
        raise OSError("模拟磁盘写满")

    S.zip_directory = boom
    try:
        bm.restore_backup(manifest)
        check("快照失败时恢复被中止", False, "居然没抛")
    except RuntimeError as e:
        check("快照失败时恢复被中止", True, str(e)[:40])
    except Exception as e:  # noqa: BLE001
        check("快照失败时恢复被中止", False, f"抛的是 {type(e).__name__}")
    finally:
        S.zip_directory = real_zip_directory
    check("中止后存档内容没被改动", save_file.read_bytes() == b"v2",
          save_file.read_bytes()[:20])

    # (2) 正常恢复：旧内容走回收站（不是硬删），内容回到备份时的样子
    recycled = []
    real_delete = S.delete_path
    S.delete_path = lambda p, *, permanent=False: recycled.append(Path(p))
    try:
        bm.restore_backup(manifest)
        ok = True
        err = ""
    except Exception as e:  # noqa: BLE001
        ok = False
        err = str(e)[:70]
    finally:
        S.delete_path = real_delete

    check("正常恢复不报错", ok, err)
    check("恢复后内容是备份里的那一版", save_file.read_bytes() == b"v1",
          save_file.read_bytes()[:20])
    check("旧内容走的是回收站而不是硬删", bool(recycled),
          f"回收了 {[p.name for p in recycled]}")
    left = list((sb / "backups" / "测试").glob("rollback_*.zip"))
    check("成功恢复后回滚快照已清掉", left == [], f"残留 {[p.name for p in left]}")

    # (3) 清单坏掉：也要拒绝执行，而不是先清空再失败
    bad = sb / "backups" / "测试" / "manifests" / "backup_broken.json"
    bad.write_text('{"description": "坏"}', encoding="utf-8")
    save_file.write_bytes(b"v3")
    try:
        bm.restore_backup(bad)
        check("清单缺 files 时拒绝恢复", False, "居然没抛")
    except ValueError:
        check("清单缺 files 时拒绝恢复", True)
    check("拒绝后存档内容也没动", save_file.read_bytes() == b"v3",
          save_file.read_bytes()[:20])


# --------------------------------------- 9. 自定义启动参数与命令拼装
def test_launch_args():
    """自定义启动参数：解析规则 + 危险项告警 + 命令里的顺序。"""
    print("\n[9] 自定义启动参数解析与命令拼装")
    from launcher.gamecmd import (
        build_java_command, check_extra_args, split_args,
    )

    # ---- split_args：反斜杠必须按字面量走，引号能包住空格 ----
    check("普通空格分隔",
          split_args("-Xmx4G -XX:+UseZGC") == ["-Xmx4G", "-XX:+UseZGC"])
    # shlex.split 在这里会把 \U 当转义吃掉（C:\Users -> C:Users），
    # 所以是自写的切分规则；这条防止以后有人「顺手换回 shlex」
    win_path = r"-Dfoo=C:\Users\me\data"
    check("Windows 路径里的反斜杠不被吃掉",
          split_args(win_path) == [win_path], repr(split_args(win_path)))
    check("双引号能包住带空格的一项",
          split_args('-Dname="a b c"') == ["-Dname=a b c"])
    check("多余空白被折叠", split_args("   -Xmx4G    -Xms1G  ")
          == ["-Xmx4G", "-Xms1G"])
    check("空字符串 -> 没有参数", split_args("") == [])
    check("只有空白 -> 没有参数", split_args("   ") == [])
    check('两个引号 -> 一个空参数', split_args('""') == [""])
    for bad, label in (('-Dfoo="未闭合', "引号没闭合"), ("a\x00b", "带 NUL 字符")):
        try:
            split_args(bad)
            check(f"{label}要报错", False, "居然没抛")
        except ValueError:
            check(f"{label}要报错", True)

    # ---- 危险参数要能认出来（保存时弹给用户看）----
    warns = check_extra_args(["-cp", "evil.jar",
                              "-Dmindustry.data.dir=D:/tmp"])
    check("认出 -cp 会顶掉组装好的 jar",
          any("-cp" in w for w in warns), str(warns))
    check("认出 data.dir 会破坏存档隔离",
          any("data.dir" in w for w in warns))
    check("正常参数不误报", check_extra_args(["-Xmx4G", "-XX:+UseZGC"]) == [])

    # ---- 命令拼装：顺序是这套东西唯一容易错的地方 ----
    data_dir = r"D:\data"
    cmd = build_java_command(
        r"C:\jre\bin\java.exe", r"C:\tmp\runtime.jar",
        "mindustry.desktop.DesktopLauncher", data_dir,
        ["-Dbase=1"], ["-Xmx4G"], ["--prog"], ["--extra"],
    )
    dd = "-Dmindustry.data.dir=" + data_dir
    check("-D 排在 -cp 前面（铁律：否则等于没设）",
          cmd.index(dd) < cmd.index("-cp"), str(cmd))
    check("-D 排在内置 vmArgs 之后（存档隔离优先于内置）",
          cmd.index("-Dbase=1") < cmd.index(dd), str(cmd))
    check("额外 JVM 参数在内置之后、-cp 之前",
          cmd.index(dd) < cmd.index("-Xmx4G") < cmd.index("-cp"), str(cmd))
    check("主类紧跟在 jar 后面",
          cmd[cmd.index("-cp") + 2] == "mindustry.desktop.DesktopLauncher")
    check("额外游戏参数排在主类之后", cmd[-1] == "--extra", str(cmd[-3:]))
    check("内置游戏参数排在额外游戏参数之前",
          cmd.index("--prog") < cmd.index("--extra"))
    check("java 可执行文件排在第一位",
          cmd[0] == r"C:\jre\bin\java.exe")

    # ---- 输出编码：中文 mod 名整片乱码就是少了这两个参数 ----
    # （Java 没有控制台时按 native.encoding 写，简中系统 = GBK）
    check("命令行钉住 JVM 的 stdout/stderr 编码为 UTF-8",
          "-Dstdout.encoding=UTF-8" in cmd and "-Dstderr.encoding=UTF-8" in cmd,
          str(cmd))
    check("输出编码排在内置 vmArgs 之后、用户额外参数之前（用户可覆盖）",
          cmd.index("-Dbase=1")
          < cmd.index("-Dstdout.encoding=UTF-8") < cmd.index("-Xmx4G"),
          str(cmd))


# ---------------------------------- 10. jvm 段（原 Mindustry.json）
def test_jvm_config():
    """游戏启动配置并进 config.json 的 jvm 段，且坏值能逐项退回。"""
    print("\n[10] 游戏启动配置（config.json 的 jvm 段）")
    from launcher.config import DEFAULT_VM_ARGS, ConfigManager

    sb = Path(tempfile.mkdtemp()) / "cfg"
    sb.mkdir(parents=True, exist_ok=True)
    cfg_file = sb / "config.json"

    # (1) 连 config.json 都没有时，也要能拿到一套完整可用的启动配置。
    #     以前 Mindustry.json 是必需文件，缺了整个启动器都打不开。
    cm = ConfigManager(cfg_file)
    jvm = cm.get_jvm_config()
    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    check("首次运行就把 jvm 段写进了 config.json", "jvm" in on_disk,
          str(list(on_disk)))
    check("默认主类正确",
          jvm["main_class"] == "mindustry.desktop.DesktopLauncher")
    check("默认 vmArgs 就是官方那一组（4 条）",
          jvm["vm_args"] == DEFAULT_VM_ARGS, f"{len(jvm['vm_args'])} 条")
    check("默认 jre 路径是相对的 jre", jvm["jre_path"] == "jre")

    # (2) 字段类型写错：逐项退回默认值，不能连累启动
    good_then_bad = {
        "hide_on_launch": True, "auto_update": False,
        "github_mirror": "这不是地址",
        "max_log_files": "很多份",
        "current_profile": "默认",
        "profiles": {"默认": {"data_dir": str(sb / "d")}},
        "jvm": {
            "main_class": "-Xmx4G",     # 以 - 开头会被 JVM 当选项解析
            "vm_args": "-Xmx4G",        # 应该是数组
            "program_args": 7,
            "jre_path": "   ",
            "没见过的键": 1,
        },
    }
    cfg_file.write_text(json.dumps(good_then_bad, ensure_ascii=False),
                        encoding="utf-8")
    cm2 = ConfigManager(cfg_file)
    jvm2 = cm2.get_jvm_config()
    check("主类非法时退回默认",
          jvm2["main_class"] == "mindustry.desktop.DesktopLauncher",
          jvm2["main_class"])
    check("vm_args 不是数组时退回默认", jvm2["vm_args"] == DEFAULT_VM_ARGS)
    check("program_args 不是数组时退回空", jvm2["program_args"] == [])
    check("jre_path 是空白时退回默认", jvm2["jre_path"] == "jre")
    check("用户的存档分类没被连累", cm2.get_profile_names() == ["默认"],
          str(cm2.get_profile_names()))
    check("脏镜像地址退回「直连」（空串而不是垃圾值）",
          cm2.get("github_mirror") == "", repr(cm2.get("github_mirror")))
    check("脏日志保留份数退回默认",
          cm2.get("max_log_files") == 20, str(cm2.get("max_log_files")))

    # (3) jvm 整个不是对象（手改文件时最常见的错法）
    broken = dict(good_then_bad)
    broken["jvm"] = "应该是对象"
    cfg_file.write_text(json.dumps(broken, ensure_ascii=False),
                        encoding="utf-8")
    cm3 = ConfigManager(cfg_file)
    check("jvm 不是对象时改用默认值而不是崩",
          cm3.get_jvm_config()["main_class"]
          == "mindustry.desktop.DesktopLauncher")

    # (4) 改完能存回来、重新读得到；不认识的键要拒绝
    cm3.set_jvm("main_class", "mindustry.desktop.DesktopLauncherX")
    cm3.save()
    check("改过的 jvm 能存回来并重新读到",
          ConfigManager(cfg_file).get_jvm("main_class")
          == "mindustry.desktop.DesktopLauncherX")
    try:
        cm3.set_jvm("没这个键", 1)
        check("设置不存在的 jvm 键要拒绝", False, "居然没抛")
    except KeyError:
        check("设置不存在的 jvm 键要拒绝", True)


# --------------------------------------- 11. 游戏输出日志收集
class _FakeStream:
    """冒充 subprocess 的管道：可迭代、可关闭。

    管道是**二进制**的（见 gui_game 的 Popen 注释）：这里同样按字节给行，
    才真的测到「解码」那一段。``encoding`` 用来模拟不同 JRE 往管道里写
    什么编码 —— 命令行带了 -Dstdout.encoding=UTF-8 的新版写 UTF-8，
    旧 JRE 只认 native.encoding，写 GBK；传 ``None`` 则原样给 str，
    用来模拟「有人把管道换回 text=True」。
    """

    def __init__(self, lines, encoding="utf-8"):
        self._it = iter([
            ln if (isinstance(ln, bytes) or not encoding) else ln.encode(encoding)
            for ln in lines
        ])
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        return next(self._it)

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, lines, encoding="utf-8"):
        self.stdout = _FakeStream(lines, encoding)


class _BoomStream(_FakeStream):
    """读两行之后炸掉 —— 模拟管道出问题。"""

    def __init__(self, lines):
        super().__init__(lines)
        self._n = 0

    def __next__(self):
        self._n += 1
        if self._n > 2:
            raise OSError("模拟管道炸了")
        return super().__next__()


def _wait_done(log, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and log.active:
        time.sleep(0.02)


def test_game_log():
    """游戏输出收集：落盘、内存缓冲、保留份数、出错不静默死掉。"""
    print("\n[11] 游戏输出日志收集")
    from launcher import gamelog as G

    sb = Path(tempfile.mkdtemp())
    recycled: list[Path] = []
    # 删除入口打桩，但要**真的把文件挪走**（只记进列表的话，文件还在原地，
    # 「只保留 N 份」那条断言就成了恒假）。顺手记下每次调用选的删除方式 ——
    # 老日志到底走回收站还是直接删，是设置项 permanent_delete 说了算，
    # 这一层的桩必须能看出来。
    fake_bin = sb / "_fake_recycle"
    fake_bin.mkdir(parents=True, exist_ok=True)
    real_delete = G.delete_path
    modes: list[bool] = []

    def fake_delete(p, *, permanent=False):
        p = Path(p)
        modes.append(bool(permanent))
        recycled.append(p)
        if permanent:
            p.unlink()
        else:
            p.rename(fake_bin / p.name)

    G.delete_path = fake_delete
    try:
        # ---- 基本：三行同时进文件和内存 ----
        log = G.GameLog(sb, save_to_file=True)
        log.start(_FakeProc(["hello\n", "第二行\n", "err\n"]))
        _wait_done(log)
        check("读线程读完就收工（不会卡住）", not log.active)
        check("落盘文件建出来了", log.path is not None and log.path.is_file())
        content = log.path.read_text(encoding="utf-8") if log.path else ""
        check("三行都写进了文件（含中文）",
              content.splitlines() == ["hello", "第二行", "err"], repr(content))
        last, first, dropped, lines = log.snapshot()
        check("内存里也有这三行（窗口靠它显示）",
              lines == ["hello", "第二行", "err"], str(lines))
        check("没有丢行，序号从 1 开始",
              dropped == 0 and first == 1 and last == 3,
              f"first={first} last={last} dropped={dropped}")
        check("tail 取最后两行", log.tail(2) == ["第二行", "err"])
        check("文件名带 game- 前缀", log.path.name.startswith(G.LOG_PREFIX))

        # ---- 关掉落盘：窗口里照样能看，但不产文件 ----
        log2 = G.GameLog(sb, save_to_file=False)
        log2.start(_FakeProc(["only-memory\n"]))
        _wait_done(log2)
        check("关掉落盘时不建文件", log2.path is None)
        check("关掉落盘时窗口仍能拿到内容",
              log2.snapshot()[3] == ["only-memory"],
              str(log2.snapshot()[3]))

        # ---- 编码：管道是字节流，中文 mod 名不能变成一排 U+FFFD ----
        # 现场事故：Java 在没有控制台时按 native.encoding（简中系统 = GBK）
        # 往管道里写，而这边按 UTF-8 解 —— 一份日志里 44 个 U+FFFD，
        # mod 列表整片看不出是哪个包。两头都管：命令行钉 UTF-8（gamecmd 的
        # _OUTPUT_ENCODING_ARGS），这边解不开时再退系统代码页兜住旧 JRE。
        log7 = G.GameLog(sb, save_to_file=False)
        log7.start(_FakeProc(["UTF-8 写的中文\n", "第二条\n"]))
        _wait_done(log7)
        check("UTF-8 管道：中文原样出来",
              log7.snapshot()[3] == ["UTF-8 写的中文", "第二条"],
              str(log7.snapshot()[3]))

        log8 = G.GameLog(sb, save_to_file=False)
        log8.start(_FakeProc(["Loading mod 红警崛起3.3.0.zip\n"], encoding="gbk"))
        _wait_done(log8)
        got8 = log8.snapshot()[3]
        check("GBK 管道（旧 JRE）：兜底解码认得中文",
              got8 == ["Loading mod 红警崛起3.3.0.zip"], str(got8))
        check("没有留下替换字符", not any("\ufffd" in g for g in got8), str(got8))

        # 解码规则本身：UTF-8 优先、失败才退
        check("解码：空行 -> 空串", G.decode_output_line(b"") == "")
        check("解码：UTF-8 优先（不会被 GBK 抢先解出怪字符）",
              G.decode_output_line("中文 mod 名".encode("utf-8")) == "中文 mod 名")
        check("解码：GBK 字节救得回来",
              G.decode_output_line("红警崛起3.3.0".encode("gbk"))
              == "红警崛起3.3.0")
        check("解码：两种都不合法的字节也不抛异常",
              isinstance(G.decode_output_line(b"\xff\xfe\x81\x40"), str),
              repr(G.decode_output_line(b"\xff\xfe\x81\x40")))
        check("解码：已经是文本就原样返回（别再解一次）",
              G.decode_output_line("已是文本") == "已是文本")
        check("兜底编码不是 utf-8（否则兜底等于没兜）",
              G.fallback_encoding().lower().replace("-", "")
              not in ("utf8", "utf"), G.fallback_encoding())

        # 万一以后有人把管道换回 text=True：类型不对也绝不能抛出来 ——
        # 读线程一死，管道没人排空，游戏写满 64 KB 缓冲会卡住。
        log9 = G.GameLog(sb, save_to_file=False)
        log9.start(_FakeProc(["文本行一\n", "文本行二\n"], encoding=None))
        _wait_done(log9)
        check("文本流也收得下（类型不对不把读线程弄死）",
              log9.snapshot()[3] == ["文本行一", "文本行二"],
              str(log9.snapshot()[3]))

        # ---- 环形缓冲：超上限丢最旧的，并把「丢了多少」记下来 ----
        real_max = G.GameLog.MAX_LINES
        G.GameLog.MAX_LINES = 5
        try:
            log3 = G.GameLog(sb, save_to_file=False)
            log3.start(_FakeProc([f"line{i}\n" for i in range(20)]))
            _wait_done(log3)
            _, first3, dropped3, lines3 = log3.snapshot()
            check("环形缓冲只留最近 5 行", len(lines3) == 5, f"{len(lines3)} 行")
            check("留下的确实是最新的 5 行",
                  lines3 == [f"line{i}" for i in range(15, 20)], str(lines3))
            check("被挤掉的行数被记下来（窗口要据此整体重画）",
                  dropped3 == 15, f"dropped={dropped3}")
            check("最早序号指出窗口该从哪儿接着接",
                  first3 == 16, f"first={first3}")
        finally:
            G.GameLog.MAX_LINES = real_max

        # ---- 保留份数：多余的走回收站，且只回收最旧的那批 ----
        d = sb / G.LOG_DIR_NAME
        for f in d.iterdir():
            f.unlink()
        base = time.time() - 10000
        for i in range(G.GameLog.KEEP_FILES + 3):
            p = d / f"{G.LOG_PREFIX}20260101-0000{i:02d}.log"
            p.write_text("x\n", encoding="utf-8")
            os.utime(p, (base + i, base + i))
        recycled.clear()
        modes.clear()
        log4 = G.GameLog(sb, save_to_file=True)
        log4.start(_FakeProc(["a\n"]))
        _wait_done(log4)
        left = sorted(d.glob(f"{G.LOG_PREFIX}*.log"))
        check(f"旧日志只保留最近 {G.GameLog.KEEP_FILES} 份",
              len(left) == G.GameLog.KEEP_FILES, f"剩 {len(left)} 份")
        check("多出来的走了回收站而不是硬删", len(recycled) == 4,
              f"回收 {len(recycled)} 个")
        check("（对照）默认模式下走的是「进回收站」那条路（permanent=False）",
              modes and not any(modes), str(modes))
        check("被回收的文件确实进了回收站（不在原目录）",
              all(not p.exists() for p in recycled))
        names = sorted(p.name for p in recycled)
        check("回收的正是最旧的那 4 份",
              names == [f"{G.LOG_PREFIX}20260101-0000{i:02d}.log"
                        for i in range(4)], str(names))

        # ---- 直接删除（设置里的 permanent_delete）：不走回收站 ----
        for f in d.iterdir():
            f.unlink()
        for i in range(6):
            p = d / f"{G.LOG_PREFIX}20260101-0003{i:02d}.log"
            p.write_text("x\n", encoding="utf-8")
            os.utime(p, (base + i, base + i))
        recycled.clear()
        modes.clear()
        log7 = G.GameLog(
            sb, save_to_file=True, keep_files=2, permanent_delete=True
        )
        log7.start(_FakeProc(["a\n"]))
        _wait_done(log7)
        check("开了直接删除时照样只留 keep_files 份",
              len(list(d.glob(f"{G.LOG_PREFIX}*.log"))) == 2,
              f"剩 {len(list(d.glob(f'{G.LOG_PREFIX}*.log')))} 份")
        check("★ 直接删除时走的是「彻底删」那条路（permanent=True）",
              bool(modes) and all(modes), str(modes))
        check("直接删除的文件真的没了（不是只记了一笔）",
              bool(recycled) and all(not p.exists() for p in recycled))
        check("（对照）没开这个开关时不会走彻底删",
              G.GameLog(sb, save_to_file=True).permanent_delete is False)

        # ---- 保留份数现在是设置项（原来写死 20）----
        for f in d.iterdir():
            f.unlink()
        for i in range(8):
            p = d / f"{G.LOG_PREFIX}20260101-0001{i:02d}.log"
            p.write_text("x\n", encoding="utf-8")
            os.utime(p, (base + i, base + i))
        recycled.clear()
        log6 = G.GameLog(sb, save_to_file=True, keep_files=3)
        log6.start(_FakeProc(["a\n"]))
        _wait_done(log6)
        left6 = sorted(d.glob(f"{G.LOG_PREFIX}*.log"))
        check("保留份数跟着设置走（keep_files=3 → 只留 3 份）",
              len(left6) == 3, f"剩 {len(left6)} 份")
        check("keep_files 传 0 兜到 1（否则刚写的那份会被自己删掉）",
              G.GameLog(sb, keep_files=0).keep_files == 1,
              str(G.GameLog(sb, keep_files=0).keep_files))

        # ---- 读流炸掉：必须留一行痕迹，不能静默把读线程弄死。
        #      读线程一死管道就没人排空，游戏写满 64KB 缓冲会永久卡死。
        log5 = G.GameLog(sb, save_to_file=False)
        boom = _FakeProc([])
        boom.stdout = _BoomStream(["l1\n", "l2\n", "l3\n", "l4\n"])
        log5.start(boom)
        _wait_done(log5)
        _wait_done(log5)
        got = log5.snapshot()[3]
        check("读流出错时被记成一行日志",
              any("读取游戏输出中断" in g for g in got), str(got))
        check("出错后读线程也正常收尾", not log5.active)
    finally:
        G.delete_path = real_delete


# --------------------- 11b. 输出编码（拿真实 jre 走一遍管道）
def test_output_encoding_end_to_end():
    """真起一次 java，让输出管道整个走一遍：中文必须原样出来。

    源码级断言只能证明「命令行里有那两个参数」，证明不了「加了以后管道里
    真的是 UTF-8」。这里用**真 jre** 跑一次 GameLog 的采集管线，并拿
    「不传参数 = GBK」当反向对照 —— 免得哪天 JRE 改了默认行为，
    这个测试还绿着。
    """
    print("\n[11b] 输出编码（真 jre 走一遍管道）")
    import subprocess
    from launcher import gamelog as G
    from launcher.gamecmd import _OUTPUT_ENCODING_ARGS

    java = DATA / "jre" / "bin" / "java.exe"
    if not java.is_file():
        print(f"  [跳过] 没有真实 jre：{java}")
        return
    probe = "红警崛起3.3.0 拓展包"

    def cmd_of(extra_args):
        # 借 -XshowSettings 让它把（含中文的）系统属性打出来，走的正是
        # System.out —— 也就是我们要盯的那条流。
        return [str(java), *extra_args, "-Dmdt.probe=" + probe,
                "-XshowSettings:properties", "-version"]

    def read_raw(extra_args):
        """裸读管道字节：看清 JRE 究竟写的是哪种编码。"""
        proc = subprocess.Popen(
            cmd_of(extra_args), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(Path(tempfile.gettempdir())),
        )
        out, _ = proc.communicate(timeout=60)
        return out

    def via_gamelog(extra_args):
        """走启动器真正的采集管线（GameLog）收一遍。"""
        log = G.GameLog(Path(tempfile.mkdtemp()), save_to_file=False)
        proc = subprocess.Popen(
            cmd_of(extra_args), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(Path(tempfile.gettempdir())),
        )
        log.start(proc)
        _wait_done(log, timeout=60)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return [ln for ln in log.snapshot()[3] if "mdt.probe" in ln]

    def probe_line(raw):
        return repr([ln for ln in raw.split(b"\n") if b"mdt.probe" in ln])

    utf8_bytes = read_raw(_OUTPUT_ENCODING_ARGS)
    check("加了编码参数：管道里真的是 UTF-8 字节",
          probe.encode("utf-8") in utf8_bytes, probe_line(utf8_bytes))
    # 反向对照：证明「不传参数时 JRE 写的是 GBK」—— 所以这事必须两头都管
    # （命令行钉 UTF-8 + 解不开时退系统代码页），只做一头都会漏。
    # JRE 哪天改了默认行为，这条会立刻红。
    gbk_bytes = read_raw([])
    check("[对照] 不传参数：管道里是 GBK 字节而不是 UTF-8",
          probe.encode("gbk") in gbk_bytes
          and probe.encode("utf-8") not in gbk_bytes, probe_line(gbk_bytes))

    got = via_gamelog(_OUTPUT_ENCODING_ARGS)
    check("采集管线读 UTF-8 管道：中文原样出来",
          bool(got) and probe in got[0], str(got))
    got2 = via_gamelog([])
    check("采集管线读 GBK 管道（旧 JRE / 参数没生效）：兜底解码也认得",
          bool(got2) and probe in got2[0], str(got2))
    check("两条路上都没留下替换字符",
          not any("\ufffd" in ln for ln in got + got2), str(got + got2))


# ------------------- 12. 镜像地址与日志保留份数（两项都是手填的配置）
def test_mirror_and_log_keep():
    """手填的东西一定要能容错：留着脏值也不能让启动器起不来。"""
    print("\n[12] GitHub 镜像地址与日志保留份数")
    from launcher.config import (
        DEFAULT_MIRROR,
        DEFAULT_MIRROR_PRESETS,
        MAX_LOG_KEEP,
        MAX_MIRROR_PRESETS,
        ConfigManager,
        normalize_log_keep,
        normalize_mirror,
        normalize_mirror_presets,
    )

    check("默认镜像就是原来写死的那个地址（行为不变）",
          normalize_mirror(DEFAULT_MIRROR) == "https://gh.tinylake.top/",
          DEFAULT_MIRROR)
    check("默认候选清单里都是整理过、能直接拼的地址",
          all(normalize_mirror(p) == p for p in DEFAULT_MIRROR_PRESETS),
          str(DEFAULT_MIRROR_PRESETS))

    cases = [
        ("", "", "留空＝直连 GitHub"),
        ("   ", "", "只有空白也当直连"),
        ("https://ghfast.top/", "https://ghfast.top/", "规范地址原样保留"),
        ("https://ghfast.top", "https://ghfast.top/", "缺结尾斜杠自动补"),
        ("ghfast.top", "https://ghfast.top/", "只写主机名自动补协议"),
        ("  https://gh-proxy.com/  ", "https://gh-proxy.com/", "去掉两端空白"),
        ("ftp://mirror.example.com/", "", "协议不支持→退回直连"),
        ("随便写的中文", "", "不像主机名→退回直连"),
        ("https://a b.com/", "", "带空格→退回直连"),
    ]
    for raw, want, why in cases:
        got = normalize_mirror(raw)
        check(f"镜像「{raw}」：{why}", got == want, f"得到 {got!r}")

    merged = normalize_mirror("ghfast.top") + "https://github.com/x/y.jar"
    check("整理后的前缀能直接拼出下载地址",
          merged == "https://ghfast.top/https://github.com/x/y.jar", merged)

    for raw, want in [("abc", 20), (None, 20), (0, 1), (-5, 1), ("7", 7),
                      (999, MAX_LOG_KEEP)]:
        got = normalize_log_keep(raw)
        check(f"保留份数 {raw!r} → {want}", got == want, f"得到 {got}")

    # ★ 退默认时**必须点名配置键名**：这一项就是「想改就手改 config.json」
    #   的那一类，告警里没有 `max_log_files` 这个字符串，用户就没法拿它去
    #   文件里找是哪一行（2026-09-22 重写强转层时丢过一次，是 exe 边界
    #   冒烟抓回来的 —— 那条要重打包才跑，所以在这里放个几毫秒的快断言）。
    _msgs: list[str] = []
    _h = logging.Handler()
    _h.emit = lambda rec: _msgs.append(rec.getMessage())
    _cfg_log = logging.getLogger("launcher.config")
    _cfg_log.addHandler(_h)
    try:
        normalize_log_keep("一大堆")
    finally:
        _cfg_log.removeHandler(_h)
    check("脏保留份数的告警点名了配置键 max_log_files",
          any("max_log_files" in m for m in _msgs), str(_msgs))

    # ---- 下拉框的候选镜像站也进了 config.json（github_mirror_presets）----
    # 这一项存在的理由就是「让人手改配置文件」，所以脏成什么样都不许崩。
    presets_cases = [
        (["https://a.example.com/", "https://b.example.com/"],
         ["https://a.example.com/", "https://b.example.com/"], "正常列表原样保留"),
        (["a.example.com"], ["https://a.example.com/"], "只写主机名也认"),
        (["https://a.example.com/", "https://a.example.com/"],
         ["https://a.example.com/"], "重复项去掉"),
        (["https://a.example.com/", "不是地址", 123],
         ["https://a.example.com/"], "不合法的项丢掉"),
        ("https://a.example.com/", ["https://a.example.com/"],
         "手滑写成一个字符串：当一项，不拆成一堆单字符"),
        ([], list(DEFAULT_MIRROR_PRESETS),
         "空列表 -> 退回内置默认（下拉框空着更像坏了）"),
        ("乱七八糟", list(DEFAULT_MIRROR_PRESETS),
         "整份不是列表 -> 退回内置默认"),
        (None, list(DEFAULT_MIRROR_PRESETS), "没这一项 -> 用内置默认"),
    ]
    for raw, want, why in presets_cases:
        got = normalize_mirror_presets(raw)
        check(f"镜像可选项：{why}", got == want, f"得到 {got!r}")
    check(f"候选清单最多留 {MAX_MIRROR_PRESETS} 项",
          len(normalize_mirror_presets(
              [f"https://m{i}.example.com/" for i in range(50)]
          )) == MAX_MIRROR_PRESETS)

    # 走一遍真实的 config.json（脏值不能把启动器带崩）
    sb = Path(tempfile.mkdtemp()) / "cfg"
    sb.mkdir(parents=True, exist_ok=True)
    cfg_file = sb / "config.json"
    cfg_file.write_text(json.dumps(
        {"github_mirror_presets": "https://only.example.com/"},
        ensure_ascii=False), encoding="utf-8")
    cm = ConfigManager(cfg_file)
    check("配置文件里手滑写成字符串：读出来是单项列表",
          cm.get("github_mirror_presets") == ["https://only.example.com/"],
          str(cm.get("github_mirror_presets")))
    cfg_file2 = sb / "config2.json"
    cfg_file2.write_text(json.dumps({
        "github_mirror_presets": ["https://mine.example.com/", "崩"],
        "github_mirror": "mine.example.com",
    }, ensure_ascii=False), encoding="utf-8")
    cm2 = ConfigManager(cfg_file2)
    check("自定义的镜像站能读进来（只有合法项留下）",
          cm2.get("github_mirror_presets") == ["https://mine.example.com/"],
          str(cm2.get("github_mirror_presets")))
    check("界面上的当前镜像不受候选清单影响",
          cm2.get("github_mirror") == "https://mine.example.com/",
          str(cm2.get("github_mirror")))
    cm2.save()
    saved2 = json.loads(cfg_file2.read_text(encoding="utf-8"))
    check("存回的配置里带着候选清单（下次打开还在）",
          saved2.get("github_mirror_presets") == ["https://mine.example.com/"],
          str(saved2.get("github_mirror_presets")))

    # 老配置里没有这一项：要顺手补写进文件，否则用户打开 config.json 根本
    # 看不到它，也就没法「改配置文件加自己的镜像站」。
    cfg_file3 = sb / "config3.json"
    cfg_file3.write_text(json.dumps({"auto_update": False}), encoding="utf-8")
    ConfigManager(cfg_file3)
    saved3 = json.loads(cfg_file3.read_text(encoding="utf-8"))
    check("老配置会自动补上新增项（用户才看得见）",
          saved3.get("github_mirror_presets") == list(DEFAULT_MIRROR_PRESETS),
          str(saved3.get("github_mirror_presets")))
    check("补写不会动用户已有的值", saved3.get("auto_update") is False,
          str(saved3.get("auto_update")))


# ------------------------------------- 13. 锁的作用域（持锁弹窗 = 自锁）
def _dotted(node):
    """把 ``messagebox.showinfo`` / ``foo`` 这类表达式还原成点号字符串。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _callee(node):
    """``X(...)`` 里的 ``X``（点号字符串）；不是调用就返回空串。"""
    return _dotted(node.func) if isinstance(node, ast.Call) else ""


def test_lock_scope():
    """★ 两条结构性不变量，都来自真实事故。

    **事故**：游戏跑着的时候点「设置」→ ``show_settings`` 持着 ``_proc_lock``
    弹 messagebox → 弹窗的嵌套事件循环里，每秒一次的「⏱ 游戏运行中 mm:ss」
    计时器（``_start_runtime_updater.update``）回调又去抢同一把锁 →
    普通 ``threading.Lock`` 不可重入 → **GUI 线程把自己锁死**，
    弹窗标题变成「提示 (未响应)」，整个界面再也点不动。

    所以：
      1. ``_proc_lock`` 必须是 ``RLock``（同线程重入不能死锁）；
      2. 更根本的：**with 块里不许弹窗** —— 锁的作用域只圈住「读/写状态」，
         弹窗、askyesno、startfile 一律挪到锁外面。

    这两条没法靠跑一遍程序来验（要真起游戏 + 手动点击），
    但它们是纯粹的代码结构问题，直接扫 AST 最准。
    """
    launcher_dir = ROOT / "launcher"
    assert launcher_dir.is_dir(), launcher_dir

    rlock_ok = False
    offenders: list[str] = []

    for path in sorted(launcher_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:                      # 语法错另有测试会炸出来
            check(f"{path.name} 能解析", False, str(e))
            continue

        for node in ast.walk(tree):
            # ---- 不变量 1：_proc_lock 用 RLock 建
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if _dotted(tgt) == "self._proc_lock":
                        rlock_ok = _callee(node.value) == "threading.RLock"

            # ---- 不变量 2：with self._proc_lock 里不许出现交互调用
            if not isinstance(node, ast.With):
                continue
            holds_proc_lock = any(
                _dotted(item.context_expr) == "self._proc_lock"
                for item in node.items
            )
            if not holds_proc_lock:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                name = _dotted(sub.func)
                if name.startswith("messagebox.") or name in (
                    "messagebox.askyesno",
                    "os.startfile",
                    "wait_window",
                ):
                    offenders.append(
                        f"{path.name}:{sub.lineno} 持 {name.rsplit('.', 1)[-1]} 在锁内"
                    )

    check("_proc_lock 是 RLock（同线程重入不会自锁）", rlock_ok,
          "" if rlock_ok else "在 launcher/gui_core.py 里找 self._proc_lock 的赋值")
    check("没有「持 _proc_lock 弹窗」的地方", not offenders,
          "；".join(offenders[:5]))
    check("有 _game_is_running() 这个安全的判断入口",
          hasattr(CoreMixin, "_game_is_running"))


# --------------------------- 14. 滚轮的归属（滚页面 vs 改下拉框）
def _func_def(tree, name):
    """按名字取第一个函数定义（找不到返回 None）。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def _calls_named(node, dotted):
    """node 子树里调用 ``dotted`` 的 Call 节点。"""
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and _callee(n) == dotted
    ]


def _has_str(node, text):
    """node 子树里出现过字符串常量 text 吗。"""
    return any(
        isinstance(n, ast.Constant) and n.value == text
        for n in ast.walk(node)
    )


def _refers(node, name):
    """node 子树里提到过这个名字吗（属性 ``self.x`` 和变量 ``x`` 都算）。

    ★ 查「这段代码有没有用某个控件/属性」必须用这个，**别用 _has_str**：
    ``self.permanent_delete_var`` 是 AST 的 Attribute、``data_confirm`` 是
    Name，两者都不是字符串常量 —— 用 _has_str 查永远返回 False，
    于是断言看着很严格、其实恒真或恒假。
    """
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == name:
            return True
        if isinstance(n, ast.Name) and n.id == name:
            return True
    return False


def _mentions(node, name):
    """node 子树里提到这个名字吗 —— 属性 / 变量 / 字符串常量三种写法都算。

    同一个配置项在代码里会出现三种样子：``self.permanent_delete``（属性）、
    ``data_confirm``（局部变量）、``config.get("permanent_delete")``（常量），
    三种都是「这段代码用到了它」的有效证据。
    """
    return _refers(node, name) or _has_str(node, name)


def _hints_not_wrapped(func):
    """设置页里「没套 _auto_wrap_hint」的灰字提示，返回行号列表。

    判据是 ``foreground="#777777"`` —— 设置页顶部那段说明用的是 #555555，
    不在这一列。颜色本身就是「这是条提示」的标记。

    要求的写法一律是 ``_auto_wrap_hint(ttk.Label(...))``：helper 原样返回
    标签，所以后面接 ``.grid()`` / ``.pack()`` 都不受影响。
    """
    bad = []

    def walk(node, parent):
        for child in ast.iter_child_nodes(node):
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "Label"
                    and _has_str(child, "#777777")):
                wrapped = (isinstance(parent, ast.Call)
                           and isinstance(parent.func, ast.Name)
                           and parent.func.id == "_auto_wrap_hint")
                if not wrapped:
                    bad.append(child.lineno)
            walk(child, child)

    if func is not None:
        walk(func, None)
    return bad


def _separator_spans(func):
    """gui_main 里每个 ``ttk.Separator(...).grid(...)`` 的 columnspan 值。

    分割线是「这一段到头了」的视觉标记，只盖一半会显得右边凭空缺一块。
    启动器那帧有 3 列（标签 / 输入框 / 按钮），所以要盖满 3 列 —— 写 2 的话
    分割线正好在按钮那一列的左边断掉（实测差 178 px）。取不到值的返回 None，
    也算一条要看的记录。
    """
    out = []
    if func is None:
        return out
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        inner = node.func
        if not (isinstance(inner, ast.Attribute) and inner.attr == "grid"):
            continue
        target = inner.value
        if not (isinstance(target, ast.Call)
                and isinstance(target.func, ast.Attribute)
                and target.func.attr == "Separator"):
            continue
        span = next(
            (k.value for k in node.keywords if k.arg == "columnspan"), None
        )
        out.append(getattr(span, "value", None))
    return out


def _button_pack_side(node, text):
    """找 ``text=「text」`` 的 Button，返回它 ``.pack(side=…)`` 的选项名。

    取不到返回 None（按钮不存在，或者 pack 没写 side）。只看
    ``ttk.Button(...).pack(side=tk.X)`` 这一种写法 —— 界面代码里就这一种。
    """
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not (isinstance(f, ast.Attribute) and f.attr == "pack"):
            continue
        btn = f.value
        if not (isinstance(btn, ast.Call)
                and isinstance(btn.func, ast.Attribute)
                and btn.func.attr == "Button"):
            continue
        if not any(
            k.arg == "text" and isinstance(k.value, ast.Constant)
            and k.value.value == text
            for k in btn.keywords
        ):
            continue
        for k in n.keywords:
            if k.arg == "side" and isinstance(k.value, ast.Attribute):
                return k.value.attr
    return None


def test_wheel_routing():
    """★ 滚轮只能滚设置页，绝不能顺带改掉下拉框的值。

    **事故**：Tk 8.6 给 ``TCombobox`` 挂了一条类绑定
    （``ttk::combobox::Scroll``：滚一格换一个选项）。类绑定排在 ``all`` 标签
    **之前**，于是滚轮扫过设置页里的「GitHub 镜像」时干了两件事 —— 页面滚了、
    镜像也被换掉（用户原话：「滚轮切换镜像和设置页面的滚轮下滑是冲突的」，
    实测一下就把 ``gh.tinylake.top`` 换成 ``ghfast.top``）。主面板的「当前
    存档」同样是下拉框，滚一下就等于**悄悄切换存档分类**（整套数据目录都换），
    而且这个换值真的会触发 ``<<ComboboxSelected>>``。

    这一层是纯结构问题（谁吃掉了事件、绑定什么时候装/拆），扫 AST 最准：

      1. TCombobox 的滚轮类绑定必须被摘掉；
      2. 设置页的滚动**不许**再用 ``<Enter>``/``<Leave>`` 开关 —— Tk 连
         「指针从父控件移到子控件」都会发 Enter/Leave（含 ``NotifyInferior``），
         指针扫过输入框就可能把绑定撤掉，「滚轮在设置页里没反应」就是这么来的；
      3. 落点判断必须有「看指针位置」这一手：Windows 上滚轮是发给**焦点
         控件**的，焦点不一定跟着指针走。
    """
    core = ROOT / "launcher" / "gui_core.py"
    tree = ast.parse(core.read_text(encoding="utf-8"))

    fn = _func_def(tree, "_disable_combo_wheel")
    check("有 _disable_combo_wheel()（把 Tk 的滚轮换选项摘掉）", fn is not None)
    if fn is not None:
        unbind = _calls_named(fn, "self.root.unbind_class")
        check("摘的是 TCombobox 的滚轮类绑定",
              len(unbind) == 1
              and any(isinstance(a, ast.Constant) and a.value == "TCombobox"
                      for a in unbind[0].args)
              and _has_str(fn, "<MouseWheel>"),
              f"{len(unbind)} 处 unbind_class")

    # 老的写法是「<Enter> 装上 / <Leave> 拆掉」，拆绑定那一手会误伤子控件
    torn_down = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and _dotted(n.func).split(".")[-1] == "unbind_all"
        and _has_str(n, "<MouseWheel>")
    ]
    check("滚轮绑定不再靠 <Leave> 拆掉（Enter/Leave 会误伤子控件）",
          not torn_down, f"{len(torn_down)} 处 unbind_all(\"<MouseWheel>\")")

    installs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _callee(n) == "self.root.bind_all"
        and _has_str(n, "<MouseWheel>")
    ]
    check("滚轮挂在 root.bind_all 上（焦点在输入框里也收得到）",
          len(installs) == 1, f"{len(installs)} 处")

    gate = _func_def(tree, "_wheel_targets_settings")
    check("滚轮处理先判断「这次是不是冲设置页来的」"
          "（事件落点 + 指针位置两道）",
          gate is not None
          and len(_calls_named(gate, "self._is_in_settings")) == 2
          and len(_calls_named(gate, "self.root.winfo_containing")) == 1,
          "缺 _wheel_targets_settings / _is_in_settings / winfo_containing")
    check("有 _is_in_settings()（往上走到别的窗口就停）",
          hasattr(CoreMixin, "_is_in_settings"))


# --------------- 15. 设置页「保存并返回」+ 游戏退出自动关闭启动器 ---------------
def test_settings_flow_and_autoclose():
    """两块都是用户的一句话，但都得有结构性的守门。

    **用户原话 1**：「能不能把保存设置和返回主页面合并成保存并返回（总是点成
    返回主页面然后发现设置没生效）」。所以要保证：``save_settings`` **只负责
    保存**、跳转归 ``save_and_return``，而且 ``launcher/`` 里**再也不存在**一个
    「不保存就离开设置页」的按钮 —— 这才是那个坑的根。

    **用户原话 2**：「加个游戏退出自动关闭启动器的选项」。这个开关会真的把窗口
    关掉，所以有两处不能含糊：

      1. 脏配置不许把它打开（``bool("false")`` 是 **True** —— 老写法
         ``type(default)(value)`` 正好会踩这个坑，所以有了 ``normalize_bool``）；
      2. 自动关闭必须排在**自动备份之后** —— 关早了，这一局的备份就没了。
    """
    print("\n[15] 设置页「保存并返回」/ 游戏退出自动关闭启动器")
    from launcher.config import ConfigManager, normalize_bool

    # ---- normalize_bool：手改 config.json 写成字符串也得认对 ----
    for raw, want in [
        (True, True), (False, False), (1, True), (0, False),
        ("true", True), ("False", False), ("  on  ", True), ("no", False),
        ("y", True), ("N", False), ("OFF", False),
    ]:
        got = normalize_bool(raw, default=not want, what="t")
        check(f"开关 {raw!r} → {want}",
              got is want and isinstance(got, bool), f"得到 {got!r}")

    for raw in ("一大堆", "", "maybe", None, [], {}, 1.5):
        check(f"脏开关 {raw!r} 两边默认值都跟得住",
              normalize_bool(raw, True, what="t") is True
              and normalize_bool(raw, False, what="t") is False,
              f"{normalize_bool(raw, True, what='t')!r}"
              f"/{normalize_bool(raw, False, what='t')!r}")

    check("★ 旧写法 bool(\"false\") 是 True —— 这一层不能省"
          "（所以字符串开关必须走 normalize_bool）",
          bool("false") is True)

    # ---- 脏配置真的走一遍 load() ----
    tmp = Path(tempfile.mkdtemp(prefix="mdt_autoclose_"))
    cfg_file = tmp / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "close_on_game_exit": "不是布尔值",
                "hide_on_launch": "false",
                "current_profile": "默认",
                "profiles": {
                    "默认": {
                        "data_dir": str(tmp / "data"),
                        "auto_backup": "off",
                        "min_playtime": 20,
                        "max_backups": 20,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cm = ConfigManager(cfg_file)
    check("默认值是「不自动关闭启动器」（这个开关会真的关窗口，不该默认开）",
          ConfigManager.GLOBAL_DEFAULTS["close_on_game_exit"] is False,
          repr(ConfigManager.GLOBAL_DEFAULTS["close_on_game_exit"]))
    check("脏的 close_on_game_exit 退回默认，不会把启动器自己关了",
          cm.get("close_on_game_exit") is False,
          repr(cm.get("close_on_game_exit")))
    check("字符串 \"false\" 的 hide_on_launch 现在真的存成 False",
          cm.get("hide_on_launch") is False, repr(cm.get("hide_on_launch")))
    check("分类级的字符串 \"off\" 也认（auto_backup）",
          cm.get_profile_setting("默认", "auto_backup") is False,
          repr(cm.get_profile_setting("默认", "auto_backup")))
    new_keys = set(json.loads(cfg_file.read_text(encoding="utf-8")))
    check("新键会补写进老配置（用户在 config.json 里看得到它）",
          "close_on_game_exit" in new_keys, str(sorted(new_keys))[:200])

    # ---- 设置页那条路：保存归保存、跳转归跳转 ----
    gm = ast.parse((ROOT / "launcher" / "gui_main.py").read_text(encoding="utf-8"))
    save = _func_def(gm, "save_settings")
    check("有 save_settings()", save is not None)
    if save is not None:
        check("save_settings() 不再自己跳回主界面（跳转归 save_and_return）",
              not _calls_named(save, "self.show_main"))
        check("保存成功不再弹「成功」对话框（点按钮→回主界面本身就是反馈）",
              not _calls_named(save, "messagebox.showinfo"))
        bare = [n for n in ast.walk(save)
                if isinstance(n, ast.Return) and n.value is None]
        check("save_settings() 没有裸 return（返回 None 会被当成保存失败）",
              not bare, f"{len(bare)} 处")
        check("save_settings() 写进了 close_on_game_exit",
              any(_has_str(c, "close_on_game_exit")
                  for c in _calls_named(save, "self.config.set")))

    sar = _func_def(gm, "save_and_return")
    check("有 save_and_return()：保存成功才 show_main",
          sar is not None
          and len(_calls_named(sar, "self.save_settings")) == 1
          and len(_calls_named(sar, "self.show_main")) == 1)

    build = _func_def(gm, "_build_settings_ui")
    check("设置页底部挂的是「保存并返回」",
          build is not None and _has_str(build, "保存并返回"))
    # 主操作钉右下角（跟对话框的「确定」一个位置），次要的「重置默认」在左。
    check("「保存并返回」在右边（主操作）",
          _button_pack_side(build, "保存并返回") == "RIGHT",
          str(_button_pack_side(build, "保存并返回")))
    check("「重置默认」在左边（次要操作）",
          _button_pack_side(build, "重置默认") == "LEFT",
          str(_button_pack_side(build, "重置默认")))
    check("设置页里有「游戏退出后自动关闭启动器」这一项",
          build is not None and _has_str(build, "close_on_game_exit")
          and any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and "自动关闭启动器" in n.value
                  for n in ast.walk(build)))

    # 只要还有一个「不保存就能离开设置页」的按钮，用户就还能踩回那个坑
    leftovers = [
        p.name for p in sorted((ROOT / "launcher").glob("*.py"))
        if "返回主界面" in p.read_text(encoding="utf-8")
    ]
    check("launcher/ 里不再有「返回主界面」（离开设置页＝保存）",
          not leftovers, "、".join(leftovers))

    # ---- 游戏退出后：备份先做完，再关启动器 ----
    gg = ast.parse((ROOT / "launcher" / "gui_game.py").read_text(encoding="utf-8"))
    session = _func_def(gg, "_game_session")
    close_fn = _func_def(gg, "_close_after_game")
    check("有 _close_after_game()", close_fn is not None)
    if session is not None:
        closes = _calls_named(session, "self._close_after_game")
        backups = [
            n for n in ast.walk(session)
            if isinstance(n, ast.Call) and _dotted(n.func) == "create_backup"
        ]
        check("_game_session 收尾时问一次「要不要关启动器」",
              len(closes) == 1, f"{len(closes)} 处")
        check("★ 自动关闭排在自动备份之后（关早了这局备份就没了）",
              bool(backups) and bool(closes)
              and min(c.lineno for c in closes)
              > max(b.lineno for b in backups),
              f"备份在 {[b.lineno for b in backups]}，"
              f"关闭在 {[c.lineno for c in closes]}")
    if close_fn is not None:
        check("自动关闭读的是 close_on_game_exit",
              _has_str(close_fn, "close_on_game_exit"))
        calls = _calls_named(close_fn, "self.run_on_gui")
        check("自动关闭走 on_closing（跟用户点关闭同一条退出路径）",
              len(calls) == 1 and calls[0].args
              and _dotted(calls[0].args[0]) == "self.on_closing",
              str([_dotted(c.func) for c in calls]))
        check("启动器本来就在退出时不再关第二次",
              len(_calls_named(close_fn, "self.stop_event.is_set")) == 1)
        check("下载进行中不关（免得留下半个版本包）",
              bool(_calls_named(
                  close_fn, "self.update_manager.downloading.is_set")))


# -------------- 16. Java 路径体检 + 「直接删除（不进回收站）」开关 --------------
def test_jre_probe_and_permanent_delete():
    """几件事都是用户一句话要来的，但都得有结构性的守门。

    **用户原话 1**：「要不跑个 version？而且可以让路径 ui 页面可改动」——
    Java 路径以前只能去改 config.json，而且「文件存在」就当它能用。现在
    设置页能改、顺手「检测」＝真起一次 JVM 跑 ``java -version`` 看它答不
    答应；路径写坏也不能再把启动器弄成打不开（退回默认 jre + WARNING）。
    ★ 收的是 **JRE 或 JDK**，判定只看 ``bin\\java.exe`` 在不在、跑不跑得起来。

    **用户原话 2**：「顺便加一个直接删除文件（不经过回收站）的设置，默认值
    为 false」—— 这是个**破坏性**开关，所以三条都要钉住：默认必须是关的、
    脏配置不许把它打开、只有用户文件那几处删除受它影响（CAS 对象池回收
    内部文件照旧硬删，几万个碎片对象进回收站是灾难）。

    **用户原话 3**：「话说如果没有 jre 是不是可以先试试找环境变量看看有没有
    jdk」—— 于是 _validate_common_files 多了一层兜底：JAVA_HOME → PATH，
    ★ 而且找到的候选必须**真跑一次**才算数（PATH 里那个 java.exe 常是 Oracle
    安装器留下的转发桩，JRE 卸了它还在），找到只用于**本次运行、不写回配置**。

    **用户报的 bug**：「被截断了」（配一张设置页截图）—— 灰字提示 grid 在
    输入框那一列，列宽由窗口分配、标签的自然宽度由文字长度决定，文字更长时
    Tk 既不报错也不换行，右边直接少一截。现在每条提示都必须套
    ``_auto_wrap_hint``（放不下就折行），漏一条这里就红。
    """
    print("\n[16] Java 路径可改可测 + 环境变量兜底 + 「直接删除」开关"
          " + 提示不被裁")
    import launcher.config as C
    from launcher import gamecmd as GC
    from launcher.config import ConfigManager

    # ---- 「直接删除」默认关，脏值不许把它打开 ----
    check("permanent_delete 默认是 False（默认仍然走回收站）",
          ConfigManager.GLOBAL_DEFAULTS["permanent_delete"] is False,
          repr(ConfigManager.GLOBAL_DEFAULTS["permanent_delete"]))

    tmp = Path(tempfile.mkdtemp(prefix="mdt_pdel_"))
    cfg_file = tmp / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "permanent_delete": "true",      # 手改写成字符串也得认对
                "hide_on_launch": "不是布尔值",   # 这个退回默认
                "current_profile": "默认",
                "profiles": {"默认": {"data_dir": str(tmp / "data")}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cm = ConfigManager(cfg_file)
    check("手改的字符串 \"true\" 认成 True（不是按类型硬转）",
          cm.get("permanent_delete") is True,
          repr(cm.get("permanent_delete")))
    dirty_file = tmp / "c2.json"
    dirty_file.write_text(
        json.dumps({"permanent_delete": "随便写个值"}, ensure_ascii=False),
        encoding="utf-8",
    )
    check("认不出来的脏值退回默认 False（而不是当 True）",
          ConfigManager(dirty_file).get("permanent_delete") is False)
    cm.set("permanent_delete", "false")      # set() 也不能按类型硬转
    check("★ set() 传字符串 \"false\" 也得是 False（bool(\"false\") 是 True）",
          cm.get("permanent_delete") is False, repr(cm.get("permanent_delete")))
    cm.set("permanent_delete", True)
    cm.save()
    check("开关能存回文件",
          json.loads(cfg_file.read_text(encoding="utf-8"))
          .get("permanent_delete") is True)

    # ---- delete_path 的分发：默认回收站，开了才硬删 ----
    work = tmp / "work"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "a.txt").write_text("x", encoding="utf-8")
    (work / "b.txt").write_text("y", encoding="utf-8")
    check("先确认待删的目录里真有东西（不然「删干净了」是句空话）",
          (work / "sub" / "a.txt").is_file())
    binned: list[Path] = []
    real_bin = C.move_to_recycle_bin
    C.move_to_recycle_bin = lambda p: binned.append(Path(p))
    try:
        C.delete_path(work / "b.txt")                 # 默认（permanent=False）
        check("默认走的是回收站那条路", binned == [work / "b.txt"], str(binned))
        check("默认模式下文件还在原地（桩没搬走，说明真的没走到硬删）",
              (work / "b.txt").exists())
    finally:
        C.move_to_recycle_bin = real_bin
    binned.clear()
    C.move_to_recycle_bin = lambda p: binned.append(Path(p))
    try:
        C.delete_path(work / "b.txt", permanent=True)
    finally:
        C.move_to_recycle_bin = real_bin
    check("★ permanent=True 时**不**走回收站", binned == [], str(binned))
    check("直接删除真的把文件删掉了（不是空实现）", not (work / "b.txt").exists())
    C.delete_path(work / "sub", permanent=True)
    check("直接删目录是整棵删掉（连里面的文件一起）",
          not (work / "sub").exists() and not (work / "sub" / "a.txt").exists())
    try:
        C.delete_path(tmp / "本来就不存在", permanent=True)
        check("删一个不存在的东西不报错", True)
    except OSError as e:
        check("删一个不存在的东西不报错", False, str(e))

    # 链接只删自己，绝不跟进去删目标（回收站版和直接删版都得守）
    link_target = tmp / "link_target"
    link_target.mkdir()
    (link_target / "keep.txt").write_text("keep", encoding="utf-8")
    link = tmp / "link_dir"
    try:
        os.symlink(link_target, link, target_is_directory=True)
        made_link = True
    except (OSError, NotImplementedError, AttributeError):
        made_link = False
    if made_link:
        C.delete_path(link, permanent=True)
        check("直接删链接只删链接本身（目标目录和里面的文件都还在）",
              not link.is_symlink() and (link_target / "keep.txt").is_file())
    else:
        print("  [SKIP] 当前环境建不了符号链接，跳过「只删链接」那条")

    # ---- AST：三处「用户文件」删除都走 delete_path，且读同一个开关 ----
    st = ast.parse((ROOT / "launcher" / "storage.py").read_text(encoding="utf-8"))
    clear = _func_def(st, "_clear_save_dir")
    check("恢复备份时清空存档目录走 delete_path（默认回收站）",
          clear is not None and bool(_calls_named(clear, "delete_path")))
    check("清空存档目录读的是 permanent_delete",
          clear is not None and _mentions(clear, "permanent_delete"))
    check("清空存档目录仍然不用 shutil.rmtree（链接也照样只删自己）",
          clear is not None
          and not _calls_named(clear, "shutil.rmtree")
          and _refers(clear, "is_symlink"))
    gl = ast.parse((ROOT / "launcher" / "gamelog.py").read_text(encoding="utf-8"))
    prune = _func_def(gl, "_prune")
    check("旧日志清理走 delete_path 且读 permanent_delete",
          prune is not None and bool(_calls_named(prune, "delete_path"))
          and _refers(prune, "permanent_delete"))
    gp = ast.parse(
        (ROOT / "launcher" / "gui_profiles.py").read_text(encoding="utf-8")
    )
    check("「删除此分类」里删数据目录走 delete_path 且读 permanent_delete",
          bool(_calls_named(gp, "delete_path")) and _has_str(gp, "permanent_delete"))
    check("删数据目录的确认框和图上的文案会跟着删除方式变",
          _refers(gp, "data_confirm") and _refers(gp, "data_dst"))
    # CAS 对象池回收（几万个碎片对象）与备份清单清理是内部文件，
    # 必须**照旧硬删** —— 走回收站会变成灾难，也根本不是用户要的
    st_text = (ROOT / "launcher" / "storage.py").read_text(encoding="utf-8")
    gc_body = st_text.split("def garbage_collect", 1)[-1].split("\n    def ", 1)[0]
    check("CAS 对象池回收不受这个开关影响（内部文件照旧硬删）",
          "permanent_delete" not in gc_body and "os.unlink" in gc_body)

    # ---- probe_java：真跑一次 java -version ----
    real_java = DATA / "jre" / "bin" / "java.exe"
    if real_java.is_file():
        ok, text = GC.probe_java(real_java)
        check("拿真 JRE 跑 java -version 能通过", ok is True, text)
        check("检测结果里带版本号（给用户看的那行）", "Java" in text, text)
    else:
        print("  [SKIP] 没有真 jre，跳过 probe_java 的正向用例")

    ok, text = GC.probe_java(tmp / "这个目录不存在" / "java.exe")
    check("路径不存在时返回「失败」而不是抛异常", ok is False, text)
    fake_bin = tmp / "假java" / "bin"
    fake_bin.mkdir(parents=True)
    fake_exe = fake_bin / "java.exe"
    fake_exe.write_text("这不是可执行文件", encoding="utf-8")
    ok, text = GC.probe_java(fake_exe)
    check("不是可执行文件时也返回「失败」而不是崩", ok is False, text)

    # 超时分支：打桩成 TimeoutExpired，比真等 20 秒确定性好得多
    real_run = GC.subprocess.run

    def _timeout(*a, **k):
        raise GC.subprocess.TimeoutExpired("java", 1)

    GC.subprocess.run = _timeout
    try:
        ok, text = GC.probe_java(fake_exe, timeout=1)
    finally:
        GC.subprocess.run = real_run
    check("超时算「不可用」并说明原因", ok is False and "秒" in text, text)

    # ---- AST：设置页那一行 + 检测别塞进 GUI 线程/启动路径 ----
    core = ast.parse(
        (ROOT / "launcher" / "gui_core.py").read_text(encoding="utf-8")
    )
    val = _func_def(core, "_validate_common_files")
    check("★ jre 路径写坏时退回默认（不是直接打不开启动器）",
          val is not None
          and bool(_calls_named(val, "self._default_java_exe"))
          and any(_has_str(c, "jre_path")
                  for c in _calls_named(val, "self.config.set_jvm")),
          "缺 _default_java_exe 或 set_jvm('jre_path', ...)")
    check("有 _java_exe_of()（相对/绝对路径共用一套解析）",
          hasattr(CoreMixin, "_java_exe_of")
          and hasattr(CoreMixin, "_default_java_exe"))
    check("★ jre 也没有时调 find_system_java 去环境变量里兜",
          val is not None and bool(_calls_named(val, "gamecmd.find_system_java")))
    check("兜底是「只影响本次运行」——有撤销它的入口",
          hasattr(CoreMixin, "_reset_java_override")
          and hasattr(CoreMixin, "_jre_path_for_form"))

    gm = ast.parse(
        (ROOT / "launcher" / "gui_main.py").read_text(encoding="utf-8")
    )
    build = _func_def(gm, "_build_settings_ui")
    check("设置页里有 Java 路径输入框 + 「检测」按钮（JRE / JDK 都收）",
          build is not None and _has_str(build, "Java 路径(JRE/JDK):")
          and _refers(build, "jre_path_var") and _has_str(build, "检测"))
    check("设置页里有「直接删除」开关",
          build is not None and _refers(build, "permanent_delete_var"))
    probe = _func_def(gm, "_jre_probe_task")
    check("检测真跑 java -version，结果回 GUI 线程再显示",
          probe is not None and bool(_calls_named(probe, "probe_java"))
          and bool(_calls_named(probe, "self.run_on_gui")))
    start = _func_def(gm, "_start_jre_probe")
    check("★ 检测丢后台线程跑（起 JVM 几百毫秒，别卡界面）",
          start is not None
          and bool(_calls_named(start, "self.executor.submit")))
    ck = _func_def(gm, "check_jre")
    check("「检测」按钮自己不直接起进程（统一走 _start_jre_probe）",
          ck is not None and not _calls_named(ck, "probe_java")
          and bool(_calls_named(ck, "self._start_jre_probe")))
    save = _func_def(gm, "save_settings")
    check("保存时校验 Java 路径（要填 JRE / JDK 根目录本身）",
          save is not None and bool(_calls_named(save, "self._java_exe_of")))
    check("保存时把 Java 路径写回 config.json",
          save is not None
          and any(_has_str(c, "jre_path")
                  for c in _calls_named(save, "self.config.set_jvm")))
    check("保存时写 permanent_delete",
          save is not None
          and any(_has_str(c, "permanent_delete")
                  for c in _calls_named(save, "self.config.set")))
    check("保存时校验失败要 return False（不能假装存好了）",
          save is not None
          and any(isinstance(n, ast.Return)
                  and isinstance(n.value, ast.Constant)
                  and n.value.value is False
                  for n in ast.walk(save)))
    load = _func_def(gm, "_load_profile_settings_into_form")
    check("重开设置页会回填 Java 路径和删除方式",
          load is not None and _refers(load, "jre_path_var")
          and _refers(load, "permanent_delete_var"))
    check("Java 路径进了设置页标签列宽的统一清单（输入框才对得齐）",
          _has_str(gm, "Java 路径(JRE/JDK):"))
    check("★ 保存成功后撤掉环境变量兜底（不然界面写 A、实际跑 B）",
          save is not None
          and bool(_calls_named(save, "self._reset_java_override")))

    # ---- 设置页的灰字提示不许被裁（宽度不够就折行）----
    #
    # 用户报的：「填 JRE 或 JDK 的根目录都行…」在设置页里被切在「启动器旁」。
    # 根子是这些提示 grid 在输入框那一列：**列宽由窗口分配，标签的自然宽度由
    # 文字长度决定**，文字比列宽长时 Tk 既不报错也不换行，右边直接少一截。
    # 以后谁再往设置页加一条长提示，这两条会拦住。
    wrap = _func_def(gm, "_auto_wrap_hint")
    check("有 _auto_wrap_hint()（提示放不下就折行，不是被裁掉）",
          wrap is not None and _has_str(wrap, "<Configure>"))
    loose = _hints_not_wrapped(build)
    check("★ 设置页每条灰字提示都套了 _auto_wrap_hint（新加的长提示也别漏）",
          build is not None and not loose,
          f"漏了 {len(loose)} 条（行号）：{loose[:3]}")

    # ---- 分割线要跨满整帧（只盖一半 = 右边缺一块）----
    #
    # 同一件事的另一面：提示折行解决了「文字被裁」，但「一行没撑满」还有
    # 别的形态 —— 分割线 columnspan 少写一列，右边就空出按钮那一列的宽度。
    spans = _separator_spans(build)
    check("设置页里找得到分隔线", len(spans) >= 4, f"{len(spans)} 条")
    check("★ 每条分隔线都跨满 3 列（只盖 2 列右边少一截，量过 178 px）",
          bool(spans) and all(s == 3 for s in spans), f"{spans}")

    # ---- 没 jre 时去环境变量里兜：JAVA_HOME 优先，其次 PATH ----
    #
    # 用户原话：「如果没有 jre 是不是可以先试试找环境变量看看有没有 jdk」。
    # 这里两头都要钉：既能真捞到（很多人机器上本来就装着 JDK），又不能
    # 捞到个「文件在、其实跑不起来」的桩（Oracle 的 javapath 就长这样）。
    import launcher.gui_core as GCORE

    check("gamecmd 里有 find_system_java（环境变量兜底的入口）",
          hasattr(GC, "find_system_java"))
    real_jre_dir = DATA / "jre"
    sys_java = real_jre_dir / "bin" / "java.exe"
    saved_env = (os.environ.get("JAVA_HOME"), os.environ.get("PATH", ""))
    saved_res = GCORE.resource_path

    def _restore_env() -> None:
        home, path = saved_env
        if home is None:
            os.environ.pop("JAVA_HOME", None)
        else:
            os.environ["JAVA_HOME"] = home
        os.environ["PATH"] = path

    if sys_java.is_file():
        try:
            os.environ["JAVA_HOME"] = str(real_jre_dir)
            os.environ["PATH"] = str(tmp / "这条PATH里没有java")
            got, why = GC.find_system_java()
            check("★ JAVA_HOME 里的 Java 被认出来",
                  got is not None and Path(got) == sys_java, f"{got} / {why}")
            check("兜底给的是验过的那个 java.exe 本身（不是反推出来的目录）",
                  got is not None and Path(got).name.lower() == "java.exe")

            os.environ.pop("JAVA_HOME", None)
            os.environ["PATH"] = str(real_jre_dir / "bin")
            got, why = GC.find_system_java()
            check("JAVA_HOME 没有时退到 PATH 里找",
                  got is not None and Path(got) == sys_java, f"{got} / {why}")

            os.environ["PATH"] = str(tmp / "这条PATH里没有java")
            got, why = GC.find_system_java()
            check("★ 两处都没有时如实返回 None（不许硬塞一个假的）",
                  got is None and "没有找到" in why, f"{got} / {why}")

            # 「文件在、跑不起来」的桩必须跳过 —— 只看 is_file() 是查不出来的
            stub = tmp / "javapath"
            stub.mkdir(exist_ok=True)
            (stub / "java.exe").write_text("这是个桩", encoding="utf-8")
            os.environ["PATH"] = os.pathsep.join(
                [str(stub), str(real_jre_dir / "bin")]
            )
            got, why = GC.find_system_java()
            check("★ PATH 里那个坏桩被跳过，用后面那个能跑的",
                  got is not None and Path(got) == sys_java, f"{got} / {why}")
            os.environ["PATH"] = str(stub)
            got, why = GC.find_system_java()
            check("只剩一个坏桩时返回 None（不是把桩当救命稻草）",
                  got is None, f"{got} / {why}")
        finally:
            _restore_env()
    else:
        print("  [SKIP] 没有真 jre，跳过 find_system_java 的正向用例")

    # ---- 真跑一遍 _validate_common_files 的兜底分支 ----
    # 用配置 + 一个空目录台（没有 jre/）真调一次：这条路的要点不是「能找到」，
    # 而是「找到了也**不写回配置**」—— jre/ 只是暂时不在，用户填的值别动。
    bare = tmp / "没jre的启动器"
    bare.mkdir(exist_ok=True)
    (bare / "config.json").write_text(
        json.dumps(
            {
                "current_profile": "默认",
                "profiles": {"默认": {"data_dir": str(bare / "data")}},
                "jvm": {"jre_path": "jre"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class _FakeCore(CoreMixin):
        """只装 _validate_common_files 用得着的那点状态。

        真跑真逻辑（含 resource_path / probe_java），但不碰 tkinter 和 CAS
        —— 那些跟「去哪找 java」这条路无关。
        """

        def __init__(self, cfg_file: Path, base: Path) -> None:
            self.config = ConfigManager(cfg_file)
            self.base_dir = base
            self._jre_fix_note = ""
            self._java_exe_override = None

    try:
        GCORE.resource_path = lambda rel: Path(bare) / rel
        if sys_java.is_file():
            os.environ["JAVA_HOME"] = str(real_jre_dir)
            os.environ["PATH"] = str(tmp / "这条PATH里没有java")
            obj = _FakeCore(bare / "config.json", bare)
            try:
                obj._validate_common_files()
                crashed = ""
            except Exception as e:                                 # noqa: BLE001
                crashed = f"{type(e).__name__}: {e}"
            check("★ 没 jre 时靠环境变量顶上，不抛异常",
                  not crashed and obj._java_exe_override is not None,
                  crashed or str(obj._java_exe_override))
            check("启动真的会用兜底那个 java.exe",
                  obj._java_exe() == Path(str(obj._java_exe_override)))
            check("状态栏有话说（用户得知道现在跑的是系统 Java）",
                  "系统里的 Java" in obj._jre_fix_note, obj._jre_fix_note)
            on_disk = json.loads(
                (bare / "config.json").read_text(encoding="utf-8")
            )
            check("★ 兜底不写回配置（jre/ 只是暂时不在，别盖掉用户填的值）",
                  on_disk["jvm"]["jre_path"] == "jre",
                  repr(on_disk["jvm"]["jre_path"]))
            check("设置页那栏显示实际在用的目录（不是配置里那个死的 jre）",
                  obj._jre_path_for_form() == str(real_jre_dir),
                  obj._jre_path_for_form())
        else:
            print("  [SKIP] 没有真 jre，跳过「兜底不写配置」那组")

        os.environ.pop("JAVA_HOME", None)
        os.environ["PATH"] = str(tmp / "这条PATH里没有java")
        raised = ""
        try:
            _FakeCore(bare / "config.json", bare)._validate_common_files()
        except FileNotFoundError as e:
            raised = str(e)
        check("★ 哪儿都没有时仍然是那条可读报错（保留原话 + 说明找过环境变量）",
              "没有找到 jre 目录" in raised and "JAVA_HOME" in raised,
              raised.splitlines()[0] if raised else "居然没抛")
    finally:
        GCORE.resource_path = saved_res
        _restore_env()


def test_release_compat_layer():
    """面向发布的兼容性护栏：配置版本/迁移/未知键、版本来源表、扩展点。

    这些不是「现在的功能」，而是**以后升级别把用户数据搞坏**的那几层，
    所以每条规则都要有一条断言钉着 —— 不然下次重构顺手就把护栏拆了。
    """
    # 这一段会**故意**喂一堆脏值（非法来源、炸掉的扩展……），
    # 它们的 WARNING/ERROR 正是被验的对象，别让它刷满整屏。
    _saved_levels = {
        name: logging.getLogger(name).level
        for name in ("launcher.sources", "launcher.extensions")
    }
    for name in _saved_levels:
        logging.getLogger(name).setLevel(logging.CRITICAL)

    print("\n[17] 配置格式版本、迁移与未知键保留")
    from launcher.config import CONFIG_VERSION_KEY, ConfigManager
    from launcher.version import CONFIG_VERSION

    # --- 新配置（文件还不存在）---
    d1 = Path(tempfile.mkdtemp())
    ConfigManager(d1 / "config.json")
    disk1 = json.loads((d1 / "config.json").read_text(encoding="utf-8"))
    check("新配置写盘时带上了 config_version",
          disk1.get(CONFIG_VERSION_KEY) == CONFIG_VERSION,
          repr(disk1.get(CONFIG_VERSION_KEY)))
    check("新配置里 version_sources 默认是空数组（只用内置来源）",
          disk1.get("version_sources") == [],
          repr(disk1.get("version_sources")))

    # --- 第 0 代（没有 config_version）应该自动升级并立刻落盘 ---
    d2 = Path(tempfile.mkdtemp())
    (d2 / "config.json").write_text(
        json.dumps({"hide_on_launch": False, "max_log_files": 3}),
        encoding="utf-8",
    )
    ConfigManager(d2 / "config.json")
    disk2 = json.loads((d2 / "config.json").read_text(encoding="utf-8"))
    check("★ 缺 config_version 的老配置被当成第 0 代，升完级立刻落盘"
          "（不然每次启动都要再迁一遍）",
          disk2.get(CONFIG_VERSION_KEY) == CONFIG_VERSION,
          repr(disk2.get(CONFIG_VERSION_KEY)))
    check("迁移不会弄丢老配置里已有的值",
          disk2.get("hide_on_launch") is False
          and disk2.get("max_log_files") == 3,
          f"{disk2.get('hide_on_launch')!r} / {disk2.get('max_log_files')!r}")
    check("第 0 代升到第 1 代补出了 version_sources",
          disk2.get("version_sources") == [],
          repr(disk2.get("version_sources")))

    # --- 不认识的顶层键：读一趟 + 存一趟，不能掉 ---
    d3 = Path(tempfile.mkdtemp())
    future = {"未来的键": {"nested": [1, 2, 3]}, "另一个未来键": "值"}
    (d3 / "config.json").write_text(
        json.dumps({"config_version": CONFIG_VERSION, **future}),
        encoding="utf-8",
    )
    cm3 = ConfigManager(d3 / "config.json")
    cm3.set("max_log_files", 5)
    cm3.save()
    disk3 = json.loads((d3 / "config.json").read_text(encoding="utf-8"))
    for key, want in future.items():
        check(f"★ 不认识的顶层键「{key}」被原样保留"
              "（新版本的配置在旧版本里过一趟不掉东西）",
              disk3.get(key) == want, repr(disk3.get(key)))
    check("（对照）认识的项照常被改动", disk3.get("max_log_files") == 5,
          repr(disk3.get("max_log_files")))

    # --- 明确废弃的键：丢掉，别跟「未知键」混为一谈 ---
    d4 = Path(tempfile.mkdtemp())
    (d4 / "config.json").write_text(
        json.dumps({"config_version": CONFIG_VERSION, "use_mirror": True}),
        encoding="utf-8",
    )
    ConfigManager(d4 / "config.json").save()
    disk4 = json.loads((d4 / "config.json").read_text(encoding="utf-8"))
    check("已废弃的 use_mirror 会被丢掉（它不属于「未知键」）",
          "use_mirror" not in disk4, str(sorted(disk4))[:200])

    # --- 配置来自更新的启动器 ---
    d5 = Path(tempfile.mkdtemp())
    newer = CONFIG_VERSION + 90
    (d5 / "config.json").write_text(
        json.dumps({"config_version": newer, "hide_on_launch": False,
                    "未来的键": 1}),
        encoding="utf-8",
    )
    cm5 = ConfigManager(d5 / "config.json")
    check("更新的配置：认识的项照常读出来，不崩",
          cm5.get("hide_on_launch") is False
          and cm5.get("max_log_files") == 20,
          f"{cm5.get('hide_on_launch')!r} / {cm5.get('max_log_files')!r}")
    cm5.save()
    disk5 = json.loads((d5 / "config.json").read_text(encoding="utf-8"))
    check("★ 版本号只升不降（旧启动器存一次盘不能把新版号写小 —— "
          "那会让新启动器以为要重跑迁移）",
          disk5.get(CONFIG_VERSION_KEY) == newer,
          repr(disk5.get(CONFIG_VERSION_KEY)))
    check("更新的配置里的未知键也照样留着", disk5.get("未来的键") == 1)

    # --- 顶层不是对象（被改成数组/字符串）：按损坏处理，不能崩 ---
    d6 = Path(tempfile.mkdtemp())
    (d6 / "config.json").write_text('["这不是对象"]', encoding="utf-8")
    cm6 = ConfigManager(d6 / "config.json")
    check("配置顶层是数组时按损坏处理并重建（不是抛 AttributeError）",
          cm6.get("max_log_files") == 20, repr(cm6.get("max_log_files")))

    # --- version_sources 的脏值容错（这是给人手改的一项）---
    from launcher.sources import normalize_version_sources

    for why, raw in [
        ("整份写成字符串", "乱七八糟"),
        ("整份写成数字", 123),
        ("项不是对象", ["不是对象"]),
        ("缺 type", [{"api_url": "https://a.example.com/"}]),
        ("type 是空白", [{"type": "   ", "api_url": "https://a/"}]), 
        ("api_url 不像地址", [{"type": "A", "api_url": "ftp://a/"}]),
        ("api_url 是空串", [{"type": "A", "api_url": ""}]),
        ("asset_pattern 正则写坏",
         [{"type": "A", "api_url": "https://a/", "asset_pattern": "(["}]), 
    ]:
        got = normalize_version_sources(raw)
        check(f"version_sources 脏值：{why} → 丢掉", got == [], str(got))

    ok = normalize_version_sources([
        {"type": "A", "api_url": "https://a.example.com/api",
         "asset_pattern": r"\.jar$", "sort_rank": 2, "prerelease": False,
         "use_mirror": False, "version_regex": r"\d+", "version_group": 1},
        {"type": "B", "api_url": "https://b.example.com/api",
         "sort_rank": "2"},
    ])
    check("正常项留下且字段都带上了",
          len(ok) == 2 and ok[0]["sort_rank"] == 2
          and ok[0]["use_mirror"] is False and ok[0]["prerelease"] is False,
          str(ok))
    check("非整数的 sort_rank 只忽略那一项字段，不作废整条来源",
          "sort_rank" not in ok[1], str(ok[1]))
    check("手滑写成单个对象也认（当成一项）",
          len(normalize_version_sources(
              {"type": "A", "api_url": "https://a/"})) == 1)
    check("★ use_mirror 写成字符串 \"false\" 不会被 bool() 变成 True",
          normalize_version_sources(
              [{"type": "A", "api_url": "https://a/", "use_mirror": "false"}]
          )[0]["use_mirror"] is False)

    print("\n[18] 版本来源注册表（加来源不用改核心代码）")
    from launcher.sources import DEFAULT_SOURCES, SourceRegistry, load_sources

    reg = SourceRegistry()
    check("内置来源仍是两个，顺序也没变（MindustryX 在前）",
          [s.type for s in reg.all()] == ["MindustryX", "Mindustry"],
          str([s.type for s in reg.all()]))
    check("★ 排序档位与重构前一致",
          (reg.rank_of("MindustryX"), reg.rank_of("Mindustry")) == (0, 1))
    check("没登记的类型按老规则排（档位 1）", reg.rank_of("自制版") == 1)

    parse_cases = [
        ("MindustryX", "2026.09.X37", (37,), "X 后面的数字才是版本"),
        ("Mindustry", "160.4", (160, 4), "官方版取全部数字"),
        ("Mindustry", "v160.4", (160, 4), "前缀 v 不影响"),
        ("自制版", "a1b2c3", (1, 2, 3), "没登记的类型：取全部数字"),
        ("MindustryX", "没有数字", (0,), "切不出数字 → (0,) 排最后"),
    ]
    for vtype, raw, want, why in parse_cases:
        got = reg.parse_version(vtype, raw)
        check(f"版本号解析「{vtype} {raw}」：{why}", got == want,
              f"得到 {got}")

    check("资产匹配：Mindustry 只认 Mindustry.jar",
          reg.get("Mindustry").matches_asset("Mindustry.jar") is True
          and reg.get("Mindustry").matches_asset("MindustryX.jar") is False)
    check("资产匹配：MindustryX 认 Desktop.jar",
          reg.get("MindustryX").matches_asset("MindustryX-Desktop.jar")
          is True)

    # 从 config.json 加来源：同名覆盖内置、新名字追加
    cfg = {"version_sources": [
        {"type": "Mindustry", "api_url": "https://mirror.example.com/rel"},
        {"type": "每日构建",
         "api_url": "https://api.github.com/repos/x/y/releases",
         "asset_pattern": r"\.jar$", "sort_rank": 2, "use_mirror": False},
    ]}
    reg2 = load_sources(cfg)
    check("自定义来源：同名覆盖内置（只改用户写的那个字段）",
          reg2.get("Mindustry").api_url == "https://mirror.example.com/rel",
          reg2.get("Mindustry").api_url)
    check("自定义来源：覆盖时没写的字段保留内置默认（资产匹配器还在）",
          reg2.get("Mindustry").matches_asset("Mindustry.jar") is True)
    check("自定义来源：新名字被追加，档位生效",
          [s.type for s in reg2.all()]
          == ["MindustryX", "Mindustry", "每日构建"],
          str([s.type for s in reg2.all()]))
    check("自定义来源：use_mirror=False 传下去了（非 GitHub 的源别拼镜像）",
          reg2.get("每日构建").use_mirror is False)
    check("没给配置时只用内置那两个（测试/脚本可以不带 config 用）",
          sorted(s.type for s in load_sources(None).all())
          == sorted(s.type for s in DEFAULT_SOURCES),
          str([s.type for s in load_sources(None).all()]))

    # VersionManager 真的用上了这张表
    d7 = Path(tempfile.mkdtemp())
    mf = d7 / "versions" / "manifests"
    mf.mkdir(parents=True)
    for fname, vtype, ver in (
        ("Mindustry_160.4.json", "Mindustry", "160.4"),
        ("MindustryX_2026.09.X37.json", "MindustryX", "2026.09.X37"),
        ("自制版_3.10.json", "自制版", "3.10"),
    ):
        (mf / fname).write_text(
            json.dumps({"type": vtype, "version": ver, "files": {}}),
            encoding="utf-8",
        )
    vm = S.VersionManager(S.CASStore(d7 / "versions"), mf)
    got = vm.get_versions()
    check("VersionManager 按来源档位排序：MindustryX 在最前",
          got[0]["type"] == "MindustryX", str([v["type"] for v in got]))
    check("没登记的版本类型也能列出来（不是被丢掉）",
          {v["type"] for v in got} == {"Mindustry", "MindustryX", "自制版"},
          str([v["type"] for v in got]))
    check("解析出的版本号元组进了返回结构（排序用）",
          got[0]["parsed_version"] == (37,), str(got[0]["parsed_version"]))

    print("\n[19] 扩展点（加功能不用改启动器本体、不用重打包）")
    from launcher.extensions import (
        DISABLE_ENV, HOOKS, ExtensionAPI, ExtensionRegistry,
    )
    from launcher.version import APP_ID, __version__ as VER

    check("钩子表就是兼容性契约，四个都还在",
          set(HOOKS) == {"on_config_loaded", "on_versions_refreshed",
                         "on_before_launch", "on_game_exited"},
          str(sorted(HOOKS)))

    d8 = Path(tempfile.mkdtemp())
    ext_dir = d8 / "extensions"
    ext_dir.mkdir()
    (ext_dir / "a_good.py").write_text(
        "def register(api):\n"
        "    api.on('on_config_loaded', lambda **kw: 'cfg')\n"
        "    api.on('on_game_exited', lambda **kw: kw['playtime_minutes'])\n",
        encoding="utf-8",
    )
    (ext_dir / "b_import_boom.py").write_text(
        "raise RuntimeError('载入就炸')", encoding="utf-8"
    )
    (ext_dir / "c_no_register.py").write_text("X = 1\n", encoding="utf-8")
    (ext_dir / "d_raises.py").write_text(
        "def register(api):\n"
        "    api.on('on_before_launch', boom)\n"
        "def boom(**kw):\n"
        "    raise RuntimeError('回调炸了')\n",
        encoding="utf-8",
    )
    # 下划线开头的不该被载入（载入了必然报错，能从这里看出来）
    (ext_dir / "_private.py").write_text(
        "raise RuntimeError('下划线开头的文件不该被载入')", encoding="utf-8"
    )

    reg3 = ExtensionRegistry(d8)
    n = reg3.load()
    check("★ 坏扩展不影响好扩展：载入成功 2 个（a_good + d_raises）",
          n == 2, f"载入 {n} 个，失败 {reg3.failures()}")
    check("载入失败的扩展被记进 failures（不是静默吞掉）",
          any("b_import_boom" in f for f in reg3.failures())
          and any("c_no_register" in f for f in reg3.failures()),
          str(reg3.failures()))
    check("下划线开头的文件被跳过", 
          not any("_private" in f for f in reg3.failures()),
          str(reg3.failures()))
    check("钩子登记到注册表里了", reg3.has("on_config_loaded"))
    check("call() 把回调返回值收集起来", 
          reg3.call("on_config_loaded", config=None) == ["cfg"],
          str(reg3.call("on_config_loaded", config=None)))
    check("call() 会把 kwargs 传下去",
          reg3.call("on_game_exited", playtime_minutes=12.5,
                    version_name="V", profile_name="P", exit_code=0)
          == [12.5])

    raised = ""
    try:
        reg3.call("on_before_launch", context={})
    except Exception as e:                                      # noqa: BLE001
        raised = repr(e)
    check("★ 回调抛异常不会冒出来（一个扩展崩了不能带崩启动器）",
          raised == "", raised)
    check("抛异常的那个回调返回 None（其余钩子照常）",
          reg3.call("on_before_launch", context={}) == [None])

    api = ExtensionAPI(reg3, "试一下")
    raised = ""
    try:
        api.on("不存在的钩子", lambda **kw: None)
    except ValueError as e:
        raised = str(e)
    check("挂到不认识的钩子上会明确报错（不是静默失效）",
          "不存在的钩子" in raised, raised)
    raised = ""
    try:
        api.on("on_config_loaded", "不是函数")
    except TypeError as e:
        raised = str(e)
    check("回调不是可调用对象时报错", raised != "", raised)

    reg4 = ExtensionRegistry(d8)
    os.environ[DISABLE_ENV] = "1"
    try:
        check("环境变量能整体停用扩展（排查「是不是扩展搞的鬼」）",
              reg4.load() == 0 and reg4.count() == 0)
    finally:
        os.environ.pop(DISABLE_ENV, None)

    reg5 = ExtensionRegistry(d8)
    check("没有 extensions/ 目录时安静地什么都不做",
          ExtensionRegistry(Path(tempfile.mkdtemp())).load() == 0
          and reg5.count() == 0)

    print("\n[20] 版本号只有一处")
    import launcher
    from launcher import version as V
    check("包外暴露的版本号来自 version.py",
          launcher.__version__ == V.__version__)
    check("配置格式版本是 ≥1 的整数",
          isinstance(V.CONFIG_VERSION, int) and V.CONFIG_VERSION >= 1,
          repr(V.CONFIG_VERSION))
    check("User-Agent 里带上了应用名与版本",
          APP_ID in V.USER_AGENT and VER in V.USER_AGENT, V.USER_AGENT)

    print("\n[21] _paths 的懒解析（刚 clone、没有运行时目录时不能 import 就炸）")
    import _paths as P
    check("import _paths 不会立刻去解析 DATA", "DATA" not in vars(P))
    check("ROOT 在 import 时就可用（只做目录判断，不碰运行时目录）",
          (P.ROOT / "launcher" / "__init__.py").is_file(), str(P.ROOT))
    saved_env = os.environ.get(P.RUNTIME_DIR_ENV)
    os.environ[P.RUNTIME_DIR_ENV] = str(d8 / "这儿没有运行时")
    try:
        check("MDT_RUNTIME_DIR 指到一个不存在的目录时，required=False 返回 None",
              P.find_runtime_root(required=False) is None)
        raised = ""
        try:
            P.find_runtime_root()
        except RuntimeError as e:
            raised = str(e)
        check("required=True 时给的是可读报错（而不是不知所云的 None 崩）",
              "不存在" in raised and P.RUNTIME_DIR_ENV in raised, raised)
    finally:
        if saved_env is None:
            os.environ.pop(P.RUNTIME_DIR_ENV, None)
        else:
            os.environ[P.RUNTIME_DIR_ENV] = saved_env
    check("环境变量可覆盖：指到真目录就认它",
          P.find_runtime_root(required=False) is not None)

    for name, level in _saved_levels.items():
        logging.getLogger(name).setLevel(level)


if __name__ == "__main__":
    sys.exit(main())
