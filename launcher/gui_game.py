# -*- coding: utf-8 -*-
"""启动游戏：版本刷新、进程启动、运行监控、运行时更新。"""
import logging
import os
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from .gamecmd import build_java_command, split_args
from .gamelog import GameLog

logger = logging.getLogger(__name__)


class GameMixin:
    # 换版本时用 after 去抖，连续按方向键不会每次都真拼一遍
    _PREHEAT_DEBOUNCE_MS = 800
    # 预热残留的安全期：比这更老的才敢删。更近的可能是
    # 「启动器关了但游戏还在跑」，那个 jar 正被 JVM 占着。
    _PREHEAT_STALE_HOURS = 24
    # 点启动时最多等「正在拼同一个版本」的预热多久。正常拼一次约 4 秒；
    # 等太久等于点了没反应，不如自己重新拼。
    _PREHEAT_WAIT_S = 45

    def refresh_versions(self) -> None:
        """读盘 + 更新界面。可以在任意线程调用（界面部分会回到 GUI 线程）。"""
        versions = self.version_manager.get_versions()
        # 缓存起来：get_versions() 要读 26 个清单（约 120 ms），
        # 放在换版本/点启动的热路径上会卡界面。refresh_versions 本来
        # 就在导入、删除之后被调用，缓存不会过期。
        self._versions_cache = versions
        self.run_on_gui(lambda: self._apply_versions(versions))
        self.set_status(f"✅ 已刷新版本列表 ({len(versions)} 个)")

    def _apply_versions(self, versions: list[dict]) -> None:
        """把一份已经读好的版本列表应用到界面。

        ⚠️ 必须在 GUI 线程里调用 —— 里面的 start_preheat() 会碰
        ``root.after``。导入是在工作线程跑完的，所以那边走
        ``run_on_gui(_apply_versions)``，而不是直接调 refresh_versions()。
        """
        self._versions_cache = versions
        self._update_version_listbox(versions)
        # 列表换了（导入/删除版本）可能影响选中项要预热什么
        self.start_preheat()
        # 扩展点（见 extensions.py）：这里一定是 GUI 线程，回调里摸 tk 是安全的。
        # 传的列表是**只读**约定，扩展别原地改它。
        self.extensions.call("on_versions_refreshed", versions=versions)

    def _update_version_listbox(self, versions: list[dict]) -> None:
        self.version_listbox.delete(0, tk.END)
        if not versions:
            self.version_listbox.insert(tk.END, "未找到游戏版本，请导入")
            self.launch_btn.config(state=tk.DISABLED)
        else:
            for v in versions:
                self.version_listbox.insert(tk.END, v["name"])
            self.version_listbox.selection_set(0)
            self.launch_btn.config(state=tk.NORMAL)

    def _current_versions(self) -> list[dict]:
        """优先用缓存，缓存空（异常路径）才现读一次。"""
        if not self._versions_cache:
            self._versions_cache = self.version_manager.get_versions()
        return self._versions_cache

    def _on_version_selected(self, _event=None) -> None:
        """换了版本：把启动按钮放开，并让预热跟着换成新选的这个。"""
        self.launch_btn.config(state=tk.NORMAL)
        self.start_preheat()

    # ------------------------------------------------------------ 预热
    #
    # 拼一个运行时 jar 要 3.7 秒（6504 个 CAS 小对象，115 MB），
    # 而这一步是「点启动 → 能玩」里唯一属于启动器自己的开销。
    # 做法：窗口能用了以后后台先拼好当前选中版本，用户点启动时直接复用。
    # 结果放 %TEMP%/mdt_preheat，退出即删 —— 不占持久磁盘。

    def _preheat_key_of(self, version_info: dict) -> tuple:
        """预热结果的身份证。清单内容变了就作废（版本被重新导入/更新）。"""
        vtype = version_info["type"]
        raw = version_info["raw_version"]
        return (vtype, raw, self.version_manager.manifest_digest(vtype, raw))

    def preheat_busy(self) -> bool:
        """有预热在跑吗？后台 GC 靠它让路。"""
        return not self._preheat_idle.is_set()

    def start_preheat(self, delay_ms: int = 0) -> None:
        """去抖地安排一次预热。启动完成、列表选择变化、版本列表刷新都会调它。"""
        if self.stop_event.is_set() or self._bench:
            return
        if self._preheat_after_id is not None:
            self.root.after_cancel(self._preheat_after_id)
        self._preheat_after_id = self.root.after(
            delay_ms or self._PREHEAT_DEBOUNCE_MS, self._preheat_selected
        )

    def _preheat_selected(self) -> None:
        self._preheat_after_id = None
        if self.stop_event.is_set():
            return
        # 游戏正在跑的时候别拼：那是纯抢盘的活，等它结束再说。
        with self._proc_lock:
            if self.current_process is not None:
                return
        sel = self.version_listbox.curselection()
        if not sel:
            return
        versions = self._current_versions()
        if not versions or sel[0] >= len(versions):
            return
        self._preheat(versions[sel[0]])

    def _preheat(self, version_info: dict) -> None:
        key = self._preheat_key_of(version_info)
        with self._preheat_lock:
            if self._preheat_key == key and self._preheat_jar is not None:
                return                      # 已经是这个了
            if self._preheat_target == key and self.preheat_busy():
                return                      # 已经在拼这个了
            self._preheat_target = key
            self._preheat_idle.clear()
        logger.debug(f"[预热] 开始: {version_info['name']}")
        self.executor.submit(self._preheat_task, version_info, key)

    def _preheat_task(self, version_info: dict, key: tuple) -> None:
        out: Path | None = None
        try:
            if self.stop_event.is_set():
                return
            self._preheat_root.mkdir(parents=True, exist_ok=True)
            out = Path(tempfile.mktemp(suffix=".jar", dir=self._preheat_root))
            t0 = time.perf_counter()
            self.version_manager.build_runtime_jar(
                version_info["type"], version_info["raw_version"], out
            )
            logger.info(
                f"[预热] {version_info['name']} 已就绪，"
                f"点启动可直接复用（{(time.perf_counter() - t0) * 1000:.0f} ms）"
            )
            # 拼的过程中用户可能已经把窗口关了。这时绝不能把结果挂上去 ——
            # cleanup_preheat() 早就跑完了，这个 jar 会没人删，一直躺到下次
            # 启动的残留清理（而那只清 24 小时前的），白占 100+MB。
            # 走到 finally 里 out 还不是 None，会被删掉。
            if self.stop_event.is_set():
                logger.debug("[预热] 拼完时已开始退出，丢弃这次结果")
                return
            with self._preheat_lock:
                old = self._preheat_jar
                self._preheat_jar = out
                self._preheat_key = key
                out = None              # 交出去了，别在 finally 里删掉
            if old is not None:
                try:
                    old.unlink(missing_ok=True)
                except OSError as e:
                    # 上一局游戏可能还占着它，删不掉就留着，退出时再试
                    logger.debug(f"[预热] 旧 jar 删不掉（可能仍被占用）: {e}")
        except Exception as e:
            logger.error(f"[预热] 失败: {e}")
        finally:
            if out is not None:
                out.unlink(missing_ok=True)
            with self._preheat_lock:
                if self._preheat_target == key:
                    self._preheat_target = None
            self._preheat_idle.set()

    def _take_preheated_jar(self, key: tuple, wait: bool = False):
        """有能复用的预热 jar 就返回它。
        wait=True 时，若同一个版本正在预热，就等它拼完（比重新拼一遍划算）。"""
        if wait and self.preheat_busy():
            with self._preheat_lock:
                target = self._preheat_target
            if target == key:
                logger.debug("[预热] 正在拼同一版本，等它就绪")
                if not self._preheat_idle.wait(timeout=self._PREHEAT_WAIT_S):
                    # 等超了就自己拼：总比让用户在"点启动"上干等强
                    logger.warning(
                        f"[预热] 等待超过 {self._PREHEAT_WAIT_S}s，改为自己拼"
                    )
        with self._preheat_lock:
            jar = self._preheat_jar
            if jar is not None and self._preheat_key == key and jar.exists():
                return jar
        return None

    def cleanup_preheat(self) -> None:
        """退出时把自己预热出来的临时 jar 删掉。"""
        if self._preheat_after_id is not None:
            try:
                self.root.after_cancel(self._preheat_after_id)
            except tk.TclError:
                pass
            self._preheat_after_id = None
        with self._preheat_lock:
            jar, self._preheat_jar = self._preheat_jar, None
            self._preheat_key = None
        if jar is not None:
            try:
                jar.unlink(missing_ok=True)
            except OSError as e:
                # 游戏还在跑，jar 被 JVM 占着 —— 下次启动时按残留清理
                logger.debug(f"[预热] 退出时删不掉（游戏仍占用）: {e}")
        try:
            self._preheat_root.rmdir()
        except OSError:
            pass

    def _cleanup_stale_preheat(self) -> None:
        """清掉上次异常退出（或被强杀）留下的预热 jar。

        只动我们自己那个目录；且只清够老的 —— 太新的可能是「启动器关了
        但游戏还在跑」，那个 jar 正被 JVM 占着，删了会让游戏加载缺类。
        """
        root = self._preheat_root
        if not root.is_dir():
            return
        cutoff = time.time() - self._PREHEAT_STALE_HOURS * 3600
        removed = 0
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    if entry.stat().st_mtime > cutoff:
                        continue
                    os.unlink(entry.path)
                    removed += 1
                except OSError:
                    pass
        if removed:
            logger.debug(f"[预热] 清掉 {removed} 个残留的临时 jar")

    def launch_game(self) -> None:
        if self._game_is_running():
            messagebox.showwarning("警告", "已有游戏正在运行")
            return
        sel = self.version_listbox.curselection()
        if not sel:
            messagebox.showwarning("警告", "请选择版本")
            return
        versions = self._current_versions()
        version = versions[sel[0]]
        profile = self.config.get_current_profile()
        save_path = self.config.get_current_save_path()
        if not messagebox.askyesno(
            "确认启动",
            f'启动 {version["name"]}？\n\n'
            f"存档分类：{profile}\n"
            f"数据目录：{save_path}",
        ):
            return
        if self.config.get("hide_on_launch"):
            self.root.withdraw()
        self.set_status(f'🚀 启动 {version["name"]} (存档「{profile}」)...')
        self.launch_btn.config(state=tk.DISABLED)
        # 先亮牌再排队：后台 GC 要扫 3 万多个对象，跟拼 jar 抢盘只会
        # 两头都慢。在提交任务之前就置位，堵住「GC 抢在 _monitor_game
        # 开头置位之前启动」这个窗口。
        self._launching.set()
        try:
            self.executor.submit(self._monitor_game, version)
        except RuntimeError as e:
            # 线程池已经在关（用户正好在关窗口）—— 这时必须把牌子摘掉，
            # 否则「正在启动」永远亮着，后台 GC 会被永久挡在门外。
            logger.warning(f"无法开始启动游戏（线程池已关闭）: {e}")
            self._launching.clear()
            self.set_status(f"❌ 无法启动游戏: {e}")
            self.run_on_gui(lambda: self.launch_btn.config(state=tk.NORMAL))
            if self.config.get("hide_on_launch"):
                self.run_on_gui(self.root.deiconify)

    def _monitor_game(self, version_info: dict) -> None:
        """拼好运行时 jar 并把游戏拉起来；拉起来之后交给 `_game_session`。

        ⚠️ 这个方法跑在 executor 线程里，所以**不能**在这里等游戏退出：
        `on_closing` 会在 GUI 线程上 `executor.shutdown(wait=True)`，
        一旦这个线程阻塞在 `process.wait()`，关窗口就会把主线程 join 住，
        表现是窗口「未响应」直到游戏退出（几十分钟）。等待必须是 daemon 线程。
        """
        temp_jar = None
        own_jar = False      # True = 这个 jar 是本函数拼的，退出后要删掉
        launched = False
        # 分段计时。启动慢的时候，日志必须能直接指出是哪一段慢，
        # 不然只能靠猜（打包前后 I/O 环境不一样，源码里量出来的数字不算数）。
        timings: list[tuple[str, float]] = []
        t_prev = time.perf_counter()

        def mark(label: str) -> None:
            nonlocal t_prev
            now = time.perf_counter()
            timings.append((label, (now - t_prev) * 1000))
            t_prev = now

        # 记录启动时所属的存档分类，退出后的自动备份必须写回同一分类
        profile_name = self.config.get_current_profile()
        try:
            temp_jar = self._take_preheated_jar(
                self._preheat_key_of(version_info), wait=True
            )
            if temp_jar is not None:
                mark("复用预热JAR")
            else:
                # 没有预热结果（刚启动就点了、或版本刚换过）：自己拼。
                # 这个是临时文件，游戏退出后要删掉。
                temp_jar = Path(tempfile.mktemp(suffix=".jar"))
                own_jar = True
                self.version_manager.build_runtime_jar(
                    version_info["type"], version_info["raw_version"], temp_jar
                )
                mark("拼装JAR")
            java_exe = self._java_exe()
            jvm = self.config.get_jvm_config()
            main_class = jvm["main_class"]
            vm_args = jvm["vm_args"]
            program_args = jvm["program_args"]
            # 自定义参数是「一行命令行文本」，解析失败也不能拦着不让玩：
            # 设置页保存时已经校验过（引号没闭合会当场报错），走到这里还出错
            # 只可能是有人直接改了 config.json，那就退化成不用这组参数。
            try:
                extra_vm_args = split_args(self.config.get("extra_vm_args"))
                extra_program_args = split_args(
                    self.config.get("extra_program_args")
                )
            except ValueError as e:
                logger.warning(f"自定义启动参数无法解析（{e}），本次忽略")
                extra_vm_args, extra_program_args = [], []
            # 让游戏把该分类的目录当作 %APPDATA%/Mindustry 使用
            data_dir = Path(self.config.get_save_path(profile_name))
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning(f"创建存档数据目录失败 {data_dir}: {e}")
            mark("准备命令")
            env = os.environ.copy()
            env["MINDUSTRY_DATA_DIR"] = str(data_dir)
            # 扩展点：拉起进程之前的最后一次改写机会（见 extensions.py）。
            # 允许扩展往两个额外参数列表里追加，但**必须**是字符串 ——
            # 这里是 exec 的入参，混进非字符串会导致 Popen 直接报错。
            context = {
                "version_type": version_info["type"],
                "version": version_info.get("raw_version", ""),
                "version_name": version_info["name"],
                "profile": profile_name,
                "data_dir": str(data_dir),
                "java_exe": str(java_exe),
                "extra_vm_args": list(extra_vm_args),
                "extra_program_args": list(extra_program_args),
            }
            try:
                self.extensions.call("on_before_launch", context=context)
            except Exception as e:                              # noqa: BLE001
                logger.error(f"on_before_launch 钩子出错（忽略）: {e}")
            extra_vm_args = [str(a) for a in context.get(
                "extra_vm_args", extra_vm_args)]
            extra_program_args = [str(a) for a in context.get(
                "extra_program_args", extra_program_args)]
            cmd = build_java_command(
                java_exe, temp_jar, main_class, data_dir,
                vm_args, extra_vm_args, program_args, extra_program_args,
            )
            logger.info(
                f"启动命令[分类:{profile_name}] 数据目录={data_dir}"
            )
            # 完整命令落到日志里：出问题时第一件想做的事就是「把它复制到
            # 终端里手动跑一遍」，没有这行就只能靠拼
            logger.info("命令行: " + subprocess.list2cmdline(cmd))
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            # 游戏输出这次接住（原来是 DEVNULL，崩了完全没线索）。
            # stderr 并进 stdout：反正都写同一份日志，分两个流只会乱序。
            #
            # ★ 这个 GameLog 实例要**一路传到 _game_session**，会话收尾时
            #   关的必须是它自己那一个：self.game_log 会被下一局启动顶掉，
            #   上一局的收尾线程要是去关 self.game_log，就会把下一局刚开的
            #   日志文件提前关掉（那一局的文件从此只写进内存，落盘静默失效）。
            game_log = GameLog(
                self.base_dir,
                save_to_file=self.config.get("save_game_log"),
                # 每次启动都重读一次：改了保留份数，下一次启动就生效
                keep_files=self.config.get("max_log_files"),
                # 同上：清旧日志是进回收站还是直接删，改了也下次启动生效
                permanent_delete=self.config.get("permanent_delete"),
            )
            self.game_log = game_log
            with self._proc_lock:
                self.current_process = subprocess.Popen(
                    cmd,
                    # 固定成系统临时目录：预热 jar 放在它下面的子目录里，
                    # 直接用 parent 会顺手把子目录当工作目录传出去。
                    cwd=str(Path(tempfile.gettempdir())),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    startupinfo=startupinfo,
                    env=env,
                    # 二进制读、由 GameLog 自己解码（gamelog.decode_output_line）。
                    # ★ 别换回 text=True + encoding="utf-8"：游戏输出的编码由
                    #   它的 JRE 决定 —— Java 没有控制台时按 native.encoding
                    #   （简中系统 = GBK）写，钉死 UTF-8 会把中文 mod 名整片
                    #   撕成 U+FFFD；反过来钉死 GBK 又会在新版 JRE（命令行里
                    #   带了 -Dstdout.encoding=UTF-8，见 gamecmd）下全乱。
                    #   拿不准编码的东西就得按字节读、按行猜。
                    # bufsize 用默认：二进制模式下 bufsize=1 是「1 字节缓冲」，
                    #   会把每一行变成几百次系统调用；行边界照样由 \n 决定，
                    #   实时性不受影响。
                )
                self.game_start_time = time.time()
            launched = True
            # 读线程必须马上起来：管道没人排空，游戏写满 64 KB 缓冲区就会
            # 永久阻塞（表现是卡在黑屏不进主菜单）。
            try:
                game_log.start(self.current_process)
            except Exception as e:                             # noqa: BLE001
                # 收集起不来也不能把这次启动废掉 —— 游戏已经在跑了
                logger.error(
                    f"游戏输出收集启动失败（游戏本身不受影响）: {e}",
                    exc_info=True,
                )
            mark("拉起进程")
            logger.info(
                "[启动耗时] "
                + " | ".join(f"{k} {v:.0f}ms" for k, v in timings)
                + f" | 合计 {sum(v for _, v in timings):.0f}ms"
            )
            # jar 拼好了、JVM 也起来了，磁盘空出来了，GC 可以干活了。
            self._launching.clear()
            self.set_status(
                f'▶ {version_info["name"]} 运行中... (存档「{profile_name}」)'
            )
            self._start_runtime_updater()
        except Exception as e:
            logger.error(f"游戏启动异常: {e}", exc_info=True)
            self.set_status(f"❌ 错误: {e}")
            self.run_on_gui(
                lambda e=e: messagebox.showerror("错误", f"启动游戏失败: {e}")
            )
            # 没拉起来：这次启动留下的东西由这里收干净（拉起来了的话
            # 全部交给 _game_session，它才是那个知道游戏什么时候结束的人）
            if own_jar and temp_jar is not None and temp_jar.exists():
                temp_jar.unlink(missing_ok=True)
            with self._proc_lock:
                self.current_process = None
                self.game_start_time = None
            self.run_on_gui(lambda: self.launch_btn.config(state=tk.NORMAL))
            self.run_on_gui(self.root.deiconify)
        finally:
            # 无论成功还是异常，都要把「正在启动」的牌子摘掉，
            # 否则 GC 会被永久挡在门外。
            self._launching.clear()

        if not launched:
            return
        threading.Thread(
            target=self._game_session,
            args=(version_info, profile_name, temp_jar, own_jar, game_log),
            daemon=True,
        ).start()

    def _game_session(
        self,
        version_info: dict,
        profile_name: str,
        temp_jar: Path | None,
        own_jar: bool,
        game_log: GameLog | None = None,
    ) -> None:
        """游戏已经在跑了：等它退出、算时长、按策略自动备份。

        ★ 必须是 daemon 线程，不能占 executor —— 否则关窗口时
        `executor.shutdown(wait=True)` 会被 `proc.wait()` 一直 join 住。

        退出后的顺序是定死的：**先收尾、再备份、最后才**（按设置）关启动器
        —— 见 _close_after_game。
        """
        proc = None
        try:
            with self._proc_lock:
                proc = self.current_process
                start = self.game_start_time
            if proc is not None:
                proc.wait()
            playtime = (time.time() - start) / 60 if start else 0.0
            self.set_status(
                f'⏹ {version_info["name"]} 已关闭 (运行 {playtime:.1f} 分钟)'
            )
            logger.info(
                f"游戏 {version_info['name']} 退出，运行 {playtime:.1f} 分钟"
            )
            if self.stop_event.is_set():
                # 启动器已经在关了：这时候再起一次备份只会跟退出流程抢磁盘
                logger.debug("启动器正在退出，跳过自动备份")
                return
            # 备份策略取自启动时所属的分类
            auto_backup = self.config.get_profile_setting(
                profile_name, "auto_backup"
            )
            min_playtime = self.config.get_profile_setting(
                profile_name, "min_playtime"
            )
            if auto_backup and playtime >= min_playtime:
                try:
                    self._make_backup_manager(profile_name).create_backup()
                    self.set_status(
                        f"💾 自动备份完成 (存档「{profile_name}」)"
                    )
                except Exception as e:
                    logger.error(f"自动备份失败: {e}")
                    self.set_status(f"❌ 自动备份失败: {e}")
        except Exception as e:
            logger.error(f"游戏会话异常: {e}", exc_info=True)
        finally:
            # 只删自己拼的那个。预热出来的 jar 要留着给下一次启动复用，
            # 而且它可能还被刚退出的 JVM 占着（删了也没关系，是 tmp）。
            if own_jar and temp_jar is not None and temp_jar.exists():
                temp_jar.unlink(missing_ok=True)
            # 输出管道收尾。读线程读到 EOF 自己会退，但进程对象上的那个
            # 句柄得主动关，否则每启动一局就漏一个。
            if proc is not None and proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            # ★ 关的必须是**这一局自己那一个**（上面一路传下来的）：期间用户
            #   可能已经又点了一次启动，self.game_log 早被换成下一局的了，
            #   关 self.game_log 等于把下一局的日志文件提前关掉。
            target_log = game_log if game_log is not None else self.game_log
            if target_log is not None:
                target_log.close()
            with self._proc_lock:
                self.current_process = None
                self.game_start_time = None
            # ★ 关启动器这件事排在**所有收尾之后**：自动备份上面已经做完了，
            #   日志/临时 jar/进程句柄也都收干净了才关 —— 反过来的话
            #   「勾了这个选项 = 备份丢了」。
            if not self._close_after_game():
                self.run_on_gui(lambda: self.launch_btn.config(state=tk.NORMAL))
                self.run_on_gui(self.root.deiconify)

    def _close_after_game(self) -> bool:
        """游戏退出后按设置关掉启动器；真的要去关了才返回 True。

        关之前看两件事：

        * 启动器本来就在退出流程里（用户已经点了关闭）—— 那就别再关一次；
        * 有版本包正在下载 —— 这会儿关会把下载打断（半截文件留在 versions/），
          留着窗口让下载跑完，顺便在状态栏说一句「为什么没关」。
        """
        if self.stop_event.is_set():
            return False
        if not self.config.get("close_on_game_exit"):
            return False
        if self.update_manager.downloading.is_set():
            logger.info("游戏已退出，但正在下载版本包，暂不自动关闭启动器")
            self.set_status("⏬ 还有下载在进行，暂不自动关闭启动器")
            return False
        logger.info("游戏已退出，按设置自动关闭启动器")
        self.set_status("游戏已退出，正在关闭启动器...")
        # 走用户点「关闭」时同一条路（on_closing）：收预热、存配置、关窗口，
        # 一样都不少。必须丢回 GUI 线程执行 —— tk 的控件只能在主线程销毁。
        self.run_on_gui(self.on_closing)
        return True

    def _start_runtime_updater(self) -> None:
        def update() -> None:
            with self._proc_lock:
                proc = self.current_process
                start = self.game_start_time
            if (
                self.stop_event.is_set()
                or proc is None
                or proc.poll() is not None
            ):
                return
            if start:
                elapsed = int(time.time() - start)
                mins, secs = divmod(elapsed, 60)
                self.set_status(
                    f"⏱ 游戏运行中 {mins:02d}:{secs:02d}",
                    log_level=logging.DEBUG,
                )
            self.root.after(1000, update)

        # 本方法由工作线程调用，必须回到 GUI 线程再排定时器，
        # 否则 tk 会抛 "main thread is not in main loop"。
        self.run_on_gui(lambda: self.root.after(1000, update))
