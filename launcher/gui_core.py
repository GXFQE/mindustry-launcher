# -*- coding: utf-8 -*-
"""GUI 主类内核：初始化、线程队列、状态栏、窗口骨架。"""
import logging
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import ttk
from typing import Any

from . import gamecmd
from .config import ConfigManager
from .extensions import ExtensionRegistry
from .gamelog import GameLog
from .sources import load_sources
from .storage import CASStore, BackupManager, VersionManager
from .updates import UpdateManager
from .utils import BASE_DIR, resource_path
from .version import APP_NAME, __version__

logger = logging.getLogger(__name__)


class CoreMixin:
    def __init__(self) -> None:
        self.base_dir = BASE_DIR
        self.config_file = self.base_dir / "config.json"
        logger.info(
            f"{APP_NAME} {__version__} 启动，数据根 {self.base_dir}"
        )
        self.config = ConfigManager(self.config_file)
        # 运行时数据根下的扩展目录（可选，默认不存在）。★ 只在这里建对象，
        # 真正载入放到「窗口已经能用之后」的后台步骤里 —— 启动路径上不许加 I/O。
        self.extensions = ExtensionRegistry(self.base_dir)
        # jre 路径写坏时自动退回默认（见 _validate_common_files）留下的说明，
        # 等窗口建出来再显示 —— 这一步跑在 tk.Tk() 之前，那时候还没有状态栏。
        self._jre_fix_note = ""
        # 内置 jre/ 和配置里的路径都不可用时，去 JAVA_HOME / PATH 里捞到的
        # java.exe（见 _validate_common_files）。**只影响本次运行，不写回
        # 配置** —— 配置里那个值是用户自己填的意图，「jre/ 暂时不在」是个
        # 临时状况，拿机器上的绝对路径把它盖掉才是真的搞坏配置。
        self._java_exe_override: Path | None = None
        self._validate_common_files()
        # 「从哪儿下游戏版本」的来源表：内置那两个 + config.json 里
        # ``version_sources`` 自定义的（加来源不用改代码，见 sources.py）。
        self.sources = load_sources(self.config)
        version_store = CASStore(self.base_dir / "versions")
        self.version_manager = VersionManager(
            version_store,
            self.base_dir / "versions" / "manifests",
            sources=self.sources,
        )
        backup_store = CASStore(self.base_dir / "backups")
        self.backup_base = self.base_dir / "backups"
        self.backup_store = backup_store
        self.backup_manager = self._make_backup_manager()
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.stop_event = threading.Event()
        self.update_manager = UpdateManager(
            self.version_manager, self.config, self.executor, self.stop_event
        )
        # ⚠️ 必须是 RLock，不能是 Lock。这个锁保护的是 current_process，
        # 而 GUI 线程自己会「持锁 → 弹 messagebox」。messagebox 会跑一个嵌套
        # 事件循环，嵌套期间同一个线程里的 after 回调（比如每秒一次的
        # 「游戏运行中 mm:ss」计时器）会再次抢这把锁 —— 普通 Lock 是
        # 不可重入的，于是 GUI 线程把自己锁死，弹窗标题变成「(未响应)」。
        self._proc_lock = threading.RLock()
        self.current_process = None
        self.game_start_time = None
        # 游戏输出收集。这里先建一个空壳，「运行日志」窗口任何时候打开都能看；
        # 每次启动会用当时的「是否落盘」开关重建一个（见 _monitor_game）。
        self.game_log = GameLog(
            self.base_dir,
            save_to_file=self.config.get("save_game_log"),
            keep_files=self.config.get("max_log_files"),
            permanent_delete=self.config.get("permanent_delete"),
        )
        self._log_win = None            # 运行日志窗口（只允许开一个）
        self._log_poll_id: str | None = None
        self._log_shown_seq = 0
        # 「正在启动游戏」的牌子。启动时要拼一个 100MB+ 的 jar，
        # 那是整套流程里最吃磁盘的一步；后台 GC 要扫 3 万多个对象，
        # 跟它抢盘只会两头都慢（实测并发时读对象 1.3s → 1.9s）。
        self._launching = threading.Event()
        # 预热：启动器能用了以后，后台把「当前选中版本」的 jar 先拼到临时
        # 目录。用户点启动时直接复用 —— 拼一次 3.7 秒，复用一次就省 3.7 秒。
        # 零持久化成本：放 %TEMP%/mdt_preheat，退出时删。
        self._preheat_root = Path(tempfile.gettempdir()) / "mdt_preheat"
        self._preheat_lock = threading.Lock()
        self._preheat_jar: Path | None = None      # 已拼好、可复用的 jar
        self._preheat_key: tuple | None = None     # 它的身份证（类型/版本/清单哈希）
        self._preheat_target: tuple | None = None  # 正在拼的那个身份证
        self._preheat_idle = threading.Event()     # set = 当前没有预热在跑
        self._preheat_idle.set()
        self._preheat_after_id: str | None = None  # 去抖定时器
        self._versions_cache: list[dict] = []      # 版本列表缓存（见 refresh_versions）
        # 诊断模式（见文件末尾 _bench_jar）。只影响量测那一轮，
        # 顺手把会在量测途中弹窗的「自动检查更新」关掉。
        self._bench = bool(os.environ.get("MDT_BENCH_JAR"))
        # 「已经下载好、等着退出时替换」的启动器自更新计划（见 gui_updates
        # 与 selfupdate）。None = 这次没有要应用的更新。
        self.self_update_plan: str | None = None
        self.root = tk.Tk()
        self.font = ("Microsoft YaHei", 10)
        self.gui_queue = queue.Queue()
        self._setup_gui()
        self._process_gui_queue()
        self.refresh_versions()
        if self.config.get("auto_update") and not self._bench:
            self.auto_update_check()
        logger.info(
            f"启动器初始化完成，当前存档分类「"
            f"{self.config.get_current_profile()}」"
        )
        # 垃圾回收要遍历整个 CAS 对象池（本项目版本池 3 万+ 对象），
        # 放在启动路径上会让窗口干等十几秒。窗口已经能用了再让它后台跑。
        # ★ 走 `_start_post_window_tasks` 而不是直接排 GC：扩展（可选目录）
        #   也在这里载入 —— 两者都是「窗口已经能用之后才做的可选 I/O」，
        #   合成一个入口，启动路径上不加东西。见 extensions.py。
        gc_after_id = self.root.after(3000, self._start_post_window_tasks)
        if not self._bench:
            # 清掉上次异常退出（或被强杀）留下的临时 jar。
            # 只动我们自己那个目录，且只清 24 小时前的 —— 更近的可能是
            # "启动器关了但游戏还在跑"，那个 jar 正被 JVM 占着。
            # （预热本身由 refresh_versions() 里的 start_preheat() 安排，
            #  排在 GC 前面：用户点「启动」是立刻要结果的，GC 晚几秒无所谓。）
            self._cleanup_stale_preheat()
        # 诊断模式：藏起窗口，在真实（打包后）进程里量一次拼 jar 的耗时
        # 然后退出。必须能在这里量——打包后的 I/O 环境跟源码不一样，
        # 源码里量出来的 1.8s 代表不了 exe 里的实际数字。
        if self._bench:
            self.root.after_cancel(gc_after_id)
            self.root.withdraw()
            self.root.after(200, self._bench_jar)

    def _make_backup_manager(self, profile_name: str | None = None) -> BackupManager:
        return BackupManager(
            self.backup_store,
            self.config,
            self.backup_base,
            profile_name or self.config.get_current_profile(),
        )

    def _game_is_running(self) -> bool:
        """游戏进程还活着吗。

        ★ 所有「游戏运行中就不许做 X」的判断都走这里，**不要**自己写
        ``with self._proc_lock:`` 再在 with 里面弹 messagebox ——
        messagebox 的嵌套事件循环会让 GUI 线程自己把锁抢第二遍
        （详见 ``_proc_lock`` 那段注释）。正确姿势：

            if self._game_is_running():
                messagebox.showinfo(...)   # 锁已经放掉了
                return
        """
        with self._proc_lock:
            proc = self.current_process
        return proc is not None and proc.poll() is None

    @staticmethod
    def _java_exe_of(jre_path: str | Path) -> Path:
        """某个 jre 路径下的 ``bin/java.exe``（相对路径按「exe 旁优先」解析）。

        默认值是相对的 ``jre``，交给 resource_path 按「先找 exe 旁边的
        外部副本、再回退包内」解析；写成绝对路径时直接用那个路径 ——
        想换成自己的 JRE，在设置页改一下就行，不用重新打包。
        """
        return resource_path(Path(jre_path) / "bin" / "java.exe")

    def _java_exe(self) -> Path:
        """按配置里的 jvm.jre_path 定位 java.exe（游戏启动用）。

        如果启动时发现哪儿都没有 jre、最后靠环境变量兜住了（见
        ``_validate_common_files``），这里返回的就是**当时验过的那个
        java.exe 本身** —— 不按 bin 规则反推目录再拼一遍，保证「跑的就是
        检测通过的那一个」。
        """
        if self._java_exe_override is not None:
            return self._java_exe_override
        return self._java_exe_of(self.config.get_jvm("jre_path"))

    def _default_java_exe(self) -> Path:
        """内置默认 jre（``jre/``）下的 java.exe —— 配置写坏时的退路。"""
        return self._java_exe_of(ConfigManager.JVM_DEFAULTS["jre_path"])

    def _jre_path_for_form(self) -> str:
        """设置页「Java 路径」那一栏该显示什么。

        环境变量兜底生效时显示**实际在用的那套 Java 的目录**：否则框里写着
        `jre`、实际跑的却是系统里的 JDK，而且一按保存就被「这个路径下没有
        java.exe」拦下 —— 那才叫让人看不懂。显示兜底路径之后，用户顺手存
        一次就等于把这个 Java 固定下来，不存就只是本次生效。
        """
        if self._java_exe_override is not None:
            return str(self._override_dir())
        return str(self.config.get_jvm("jre_path"))

    def _override_dir(self) -> Path:
        """兜底那个 java.exe 对应的目录（给设置页显示 / 保存用）。

        Windows 上 Java 的布局固定是 ``<根>/bin/java.exe``，所以父目录叫
        ``bin`` 就再往上走一层 —— 那正好是能填进设置页那个框、也能写进
        ``jvm.jre_path`` 的值。``PATH`` 里那些不叫 bin 的目录（Oracle 的
        javapath 就是个转发桩目录）反推不出根目录，只能原样给出去；那种
        情况用户按「浏览」重选一个就是了，不影响本次启动实际用哪个 java。
        """
        parent = self._java_exe_override.parent
        if parent.name.lower() == "bin":
            return parent.parent
        return parent

    def _reset_java_override(self) -> None:
        """撤掉环境变量兜底，重新按配置里的路径走。

        用户在设置页保存了一个能用的 Java 路径之后必须调这个：否则这一轮
        启动仍然拿着环境变量里那个 java 去跑游戏，他刚填的路径要等下次
        重启才生效 —— 界面上写着新路径、实际跑的是另一个，最难查的那种。
        """
        if self._java_exe_override is not None:
            logger.info(
                "Java 路径已由用户指定，不再用环境变量兜底"
                f"（原兜底：{self._java_exe_override}）"
            )
        self._java_exe_override = None

    def _validate_common_files(self) -> None:
        # jre 不打进 exe（见 Launcher.spec：小 exe + 旁置 jre 目录），
        # 所以正常情况下它就在 exe 旁边的 jre/ 里。resource_path() 会先看
        # 外部副本、再回退包内，两条路都走不通才报错。
        #
        # 注：原来这里还要检查 Mindustry.json，现在那几项已经并进
        # config.json 的 jvm 段，缺了就用内置默认值，不再是必需文件。
        jre_exe = self._java_exe()
        if jre_exe.exists():
            return
        jre_path = str(self.config.get_jvm("jre_path"))
        default_path = ConfigManager.JVM_DEFAULTS["jre_path"]
        fallback_exe = self._default_java_exe()
        if jre_path.strip() != default_path and fallback_exe.exists():
            # ★ jre 路径现在是**设置页里能改的**东西了，所以「改错一个路径
            #   就再也打不开启动器」这条路必须堵死：配置里那个找不到就退回
            #   默认的 jre/，并把配置一起改回去（不然每次启动都要再警一次、
            #   设置页还显示着那个错的路径）。降级 + WARNING，跟 jvm 段其它
            #   字段的处理方式保持一致。
            logger.warning(
                f"配置里的 jre 路径「{jre_path}」下没有 java.exe，"
                f"已退回默认的「{default_path}」: {fallback_exe}"
            )
            self.config.set_jvm("jre_path", default_path)
            self.config.save()
            self._jre_fix_note = (
                f"jre 路径「{jre_path}」里找不到 java.exe，已退回默认的 jre/"
            )
            return
        # ★ 兜底第二层：内置 jre/ 也没有（没下 jre 包，或者自己填的路径写坏了
        #   而默认的又不在）。这时候去 JAVA_HOME / PATH 里捞一个能用的 java ——
        #   很多人机器上本来就装了 JDK，没必要为了这个启动器再下一份 jre。
        #   找到就**只用于本次运行**，不写回配置：配置里那是用户自己填的意图，
        #   「jre/ 暂时不在」是临时状况，拿机器绝对路径把配置盖掉才是真搞坏它。
        system_java, why = gamecmd.find_system_java()
        if system_java is not None:
            self._java_exe_override = system_java
            self._jre_fix_note = (
                f"没有找到 jre 目录，这次先用系统里的 Java：{system_java}"
            )
            logger.warning(
                f"内置 jre 不可用（{jre_exe}），本次改用 {system_java}（{why}）"
            )
            return
        logger.critical(f"JRE 未找到: {jre_exe}；环境变量兜底也没找到（{why}）")
        raise FileNotFoundError(
            "没有找到 jre 目录。它需要和启动器放在同一个文件夹里：\n\n"
            f"    {jre_exe}\n\n"
            "请确认 jre 文件夹没有被删除或挪到别处。\n"
            f"（也找过 JAVA_HOME 和 PATH 里的 Java，没找到能用的：{why}）\n"
            "（如果你在设置页或 config.json 里改过 Java 路径，也检查一下那个"
            f"路径对不对，当前是「{jre_path}」）"
        )

    def _process_gui_queue(self) -> None:
        try:
            while True:
                callback = self.gui_queue.get_nowait()
                # ★ 单个回调炸掉**不能**把这条轮询带走。queue 里塞的是
                #   工作线程委托给 GUI 线程的活（刷状态栏、弹窗、刷新列表），
                #   一旦有异常冒出去，这个 after 回调就中断、不再排下一次，
                #   于是「后台发生的一切都再也到不了界面上」——而且不报错，
                #   界面看起来只是「卡在最后那个状态不动了」。
                try:
                    callback()
                except tk.TclError:
                    # 窗口正在销毁（退出流程里最常见的那个），不算错
                    pass
                except Exception as e:                          # noqa: BLE001
                    logger.error(
                        f"GUI 队列里的回调出错（已跳过，队列继续跑）: {e}",
                        exc_info=True,
                    )
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(50, self._process_gui_queue)

    def _start_post_window_tasks(self) -> None:
        """窗口已经能用之后才做的可选工作（扩展载入 → 后台 GC → 自更新检查）。

        ★ 单独一个入口，而不是把这些塞进 ``_start_background_gc``：
          那个方法只该管「GC 要不要让路」，它的行为被回归测试逐条断言着
          （让路延时、恢复提交、二次确认）。扩展和自更新属于另外的事，
          混进去会让两边都不好单独测。

        顺序也是有意排的：扩展载入和 GC 都是**本地**动作，先跑；自更新要
        发网络请求（丢后台线程），放最后，免得网络慢的时候挡住前面两件。
        """
        if self.stop_event.is_set():
            return
        self._load_extensions_once()
        self._start_background_gc()
        # 自更新最后跑：查一次远端、有必要就下到暂存区。真正的替换要等
        # 退出时（见 gui_updates._apply_staged_update）。
        self._self_update_startup()

    def _load_extensions_once(self) -> None:
        """载入 ``extensions/*.py``（只跑一次；绝不抛异常）。

        放在这里而不是启动路径上：它要扫目录、import 本地 .py 文件，
        属于「可做可不做的 I/O」，不该拖慢窗口出现。见 extensions.py。
        """
        try:
            if self.extensions.is_loaded():
                return
            self.extensions.load()
            if self.extensions.count():
                self.extensions.call("on_config_loaded", config=self.config)
        except Exception as e:                                  # noqa: BLE001
            # load() 自己已经吞了异常，这里是最后一道保险：
            # 扩展再怎么写坏也不能挡住 GC 和后面的启动流程
            logger.error(f"扩展载入过程异常（已忽略）: {e}", exc_info=True)

    def _start_background_gc(self) -> None:
        """窗口已经能用了，再慢慢回收孤儿对象。"""
        if self.stop_event.is_set():
            return
        # 刚点了「启动游戏」、或者正在预热拼 jar 时，磁盘正要被占满。
        # 回收孤儿对象一点都不急，让开，几秒后再来。
        if self._launching.is_set() or self.preheat_busy():
            self.root.after(5000, self._start_background_gc)
            return
        self.executor.submit(self._run_gc_task)

    def _run_gc_task(self) -> None:
        """后台线程里跑垃圾回收。传 stop_event 进去，
        关窗口时能提前收手，不把退出拖住。"""
        if self.stop_event.is_set():
            return
        # 排队期间用户可能已经点了启动、或预热已经开跑，这里再确认一次。
        # 本方法跑在工作线程里，不能直接碰 tk，排定时器要回到 GUI 线程。
        if self._launching.is_set() or self.preheat_busy():
            self.run_on_gui(
                lambda: self.root.after(5000, self._start_background_gc)
            )
            return
        logger.debug("开始后台垃圾回收")
        try:
            self.version_manager.garbage_collect(self.stop_event)
        except Exception as e:
            logger.error(f"后台垃圾回收失败: {e}")

    def _bench_jar(self) -> None:
        """诊断用：在真实进程里量拼 JAR 的耗时（设 MDT_BENCH_JAR=1 触发）。

        量两种情形——单跑、以及跟 GC 并发——因为两者的差别正好说明
        「后台 GC 有没有拖慢启动」。量完自己退出。
        """
        versions = self.version_manager.get_versions()
        if not versions:
            logger.error("[BENCH] 没有可用版本")
            self.on_closing()
            return
        v = versions[0]
        for label, run_gc in (("单跑", False), ("GC并发", True)):
            gc_thread = None
            if run_gc:
                gc_thread = threading.Thread(
                    target=self.version_manager.garbage_collect, daemon=True
                )
                gc_thread.start()
                time.sleep(0.15)
            out = Path(tempfile.mktemp(suffix=".jar"))
            t0 = time.perf_counter()
            self.version_manager.build_runtime_jar(
                v["type"], v["raw_version"], out
            )
            t1 = time.perf_counter()
            out.unlink(missing_ok=True)
            logger.info(
                f"[BENCH] {v['name']} {label}: {(t1 - t0) * 1000:.0f} ms"
            )
            if gc_thread is not None:
                gc_thread.join()
        self.on_closing()

    def run_on_gui(self, func: Callable[[], Any]) -> None:
        if not self.stop_event.is_set():
            self.gui_queue.put(func)

    def set_status(self, text: str, log_level: int = logging.INFO) -> None:
        logger.log(log_level, f"状态更新: {text}")
        self.run_on_gui(lambda: self.status_var.set(text))

    def _setup_gui(self) -> None:
        self.root.title("Mindustry 启动器")
        self.root.minsize(720, 540)
        ico = resource_path("mindustry.ico")
        if ico.exists():
            try:
                self.root.iconbitmap(ico)
            except tk.TclError:
                pass
        # 必须在建下拉框之前/之后都一样生效（摘的是类绑定，跟建控件无关），
        # 但要赶在设置页挂上全局滚轮之前，免得第一下滚轮就被下拉框吃掉。
        self._disable_combo_wheel()
        self.main_frame = ttk.Frame(self.root, padding=(15, 15))
        self._make_settings_scrollable()
        self._build_main_ui()
        self._build_settings_ui()
        self._fit_root_window()
        self._load_profile_settings_into_form()
        self._refresh_profile_widgets()
        self.show_main()
        if self._jre_fix_note:
            # 走到这儿说明配置里的 jre 路径被退回默认了（见
            # _validate_common_files）：日志里已有 WARNING，这里再在状态栏
            # 说一句 —— 否则用户会对着「怎么不是我配的那个 JRE」发愣。
            self.set_status(f"⚠️ {self._jre_fix_note}")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _disable_combo_wheel(self) -> None:
        """摘掉 ttk 下拉框「滚轮换选项」的类绑定。

        Tk 8.6 给 ``TCombobox`` 挂了一条类绑定（``ttk::combobox::Scroll``）：
        指针压在下拉框上滚一格，就切到上/下一项。Tk 觉得这是贴心，在这里是
        事故 —— 类绑定排在 ``all`` 标签**之前**，滚轮事件先被下拉框用掉、
        再传给设置页的滚动处理，于是一下滚轮干了两件事：

        * 设置页：页面滚了、**镜像也被换了**（实测一次就把
          ``gh.tinylake.top`` 换成 ``ghfast.top``，正好是用户报的「冲突」）；
        * 主面板：「当前存档」同样是下拉框，滚轮扫过＝**悄悄切换存档分类**
          （数据隔离，整套数据目录都换掉），而且这个换值会真的触发
          ``<<ComboboxSelected>>``。

        摘掉之后滚轮不再被下拉框吃掉，继续往上传给设置页的滚动处理。
        要改选项就用鼠标点开列表或用方向键 —— 下拉框不该被「随手滚一下」改掉。
        """
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.root.unbind_class("TCombobox", seq)
            except tk.TclError:      # pragma: no cover - 老 Tk 没有这个标签
                pass

    def _is_in_settings(self, widget: tk.Misc | None) -> bool:
        """widget 是不是设置页里的控件（含底部按钮那一行）。

        往上走到别的窗口就停 —— 下拉框展开时那个列表是挂在 ``<下拉框>.popdown``
        这个 Toplevel 下的，不拦住的话「滚下拉列表」会顺带把背后的设置页也滚了。
        """
        while widget is not None:
            if widget is self.settings_frame:
                return True
            if isinstance(widget, tk.Toplevel):
                return False
            widget = getattr(widget, "master", None)
        return False

    def _wheel_targets_settings(self, event: tk.Event) -> bool:
        """这次滚轮该不该滚设置页。

        两道判断：

        1. 事件落点自己在设置页里 —— 指针压在设置页上时的常见情形。
           ★ 这里**不能**用 ``<Enter>``/``<Leave>`` 开关绑定：Tk 连「指针从
           父控件移到子控件」都会发一串 Enter/Leave（含 ``NotifyInferior``），
           指针扫过输入框就可能把绑定撤掉，「滚轮在设置页里没反应」就是这么来的。
        2. 落点不在设置页里（Windows 上滚轮是发给**焦点控件**的，焦点可能
           还留在别处）→ 改看**指针位置**，跟眼睛看到的保持一致。
        """
        if self._is_in_settings(getattr(event, "widget", None)):
            return True
        try:
            under = self.root.winfo_containing(
                self.root.winfo_pointerx(), self.root.winfo_pointery()
            )
        except tk.TclError:              # 窗口正在销毁
            return False
        return self._is_in_settings(under)

    def _make_settings_scrollable(self) -> None:
        """设置页内容区套一个滚动容器，底部按钮固定在窗口最下方。

        设置页比主面板高不少（存档分类 + 启动器设置 + 启动参数）。而 Tk 一旦
        调了 ``geometry()`` 就不再「跟着内容长个儿」——内容超出时底部按钮会被
        直接裁掉，界面上看不出任何报错，就是「按钮不见了」（这个坑踩过一次）。
        换成「内容可滚 + 按钮钉底」以后，小屏（1280×800 这类）也点得到每一项，
        以后再加设置项也不用重新算高度。
        """
        outer = ttk.Frame(self.root)
        self.settings_frame = outer         # 对外仍是同一个名字（show_* 用它）

        # 先占底部，剩下的都给滚动区 —— 这样按钮永远不会被内容挤走
        self.settings_footer = ttk.Frame(outer, padding=(15, 8))
        self.settings_footer.pack(side=tk.BOTTOM, fill=tk.X)

        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        vbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        body = ttk.Frame(canvas, padding=(15, 15))
        item = canvas.create_window((0, 0), window=body, anchor=tk.NW)
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.settings_body = body
        self.settings_canvas = canvas

        def sync(event: tk.Event | None = None) -> None:
            # 内容宽度跟着窗口走，否则宽窗口里控件会挤在左边一小条
            if event is not None and event.widget is canvas:
                canvas.itemconfigure(item, width=event.width)
            canvas.configure(scrollregion=canvas.bbox("all"))

        body.bind("<Configure>", sync)
        canvas.bind("<Configure>", sync)

        def on_wheel(event: tk.Event) -> None:
            # 设置页没显示、或这次滚轮根本不是冲它来的，就什么都别做
            # （bind_all 是全局的：主界面的版本列表、日志窗口都会经过这里）
            if not canvas.winfo_ismapped():
                return
            if not self._wheel_targets_settings(event):
                return
            try:
                canvas.yview_scroll(
                    -1 if getattr(event, "delta", 0) > 0 else 1, "units"
                )
            except tk.TclError:
                # 设置页已经销毁（bind_all 是全局的，可能晚一步才收到事件）
                pass

        # Windows 上 <MouseWheel> 是发给焦点控件的，所以要挂全局；
        # 按「这次是不是冲设置页来的」在 on_wheel 里收手（见 _wheel_targets_settings）。
        # ⚠️ 别用 <Enter>/<Leave> 开关这个绑定：Tk 的 Enter/Leave 连「指针从
        # 父控件移到子控件」都会发（含 NotifyInferior），指针扫过输入框就会把
        # 绑定撤掉 ——「滚轮在设置页里没反应」正是这么来的。
        self.root.bind_all("<MouseWheel>", on_wheel)

    def _fit_root_window(self, base_width: int = 780) -> None:
        """主窗口高度按**主面板**的自然高度定。

        设置页不参与 —— 它在滚动容器里（见 _make_settings_scrollable），
        高度不够就滚，不会把按钮挤出可视区。另外还要给标题栏和任务栏留位置：
        屏幕只有 800 px 高时，窗口开到 780 就会有一截掉到屏幕外面。
        """
        self.root.update_idletasks()
        need = 580
        try:
            need = max(need, self.main_frame.winfo_reqheight() + 8)
        except tk.TclError:
            pass
        height = min(need, self.root.winfo_screenheight() - 140)
        self.root.geometry(f"{base_width}x{height}")
        logger.debug(f"主窗口尺寸 {base_width}x{height}（主面板需要 {need}）")
