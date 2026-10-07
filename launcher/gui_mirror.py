# -*- coding: utf-8 -*-
"""GitHub 镜像测速的界面部分：设置页那个「测速」按钮 + 结果窗口。

分工：``mirrortest.py`` 只管「怎么测」（纯逻辑、不碰界面），这里只管
「怎么显示、怎么把结果填回设置」。

三条纪律：

  * 测速要发网络请求，一律走**后台线程**；GUI 线程只做轻活（改按钮/提示
    文字、建结果窗、把选中的前缀填进下拉框），跨线程一律 ``run_on_gui``。
  * 结果只**填进设置页的下拉框**，真正的落盘还是「保存并返回」——
    设置页只有一个出口（见 gui_main 的 save_settings / save_and_return）。
  * 结果窗同时只能开一个；测速期间按钮变成「取消」，点它就收手
    （每个候选最多多等一次 socket 超时，见 mirrortest.SOCKET_TIMEOUT）。

进度显示（设置页那一条 + 进度条）：条的最大刻度 = 候选数，读数 = 已测完
+ 当前候选的字节比例；文字带「已用 n 秒」的秒表（ticker 每 250 ms 一跳），
单个候选卡在「连接 / 等首字节」时也不会看起来像死掉。数据源是 mirrortest
的 ``on_progress`` 回调，本层只负责显示与收放。
"""
from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import mirrortest
from .config import normalize_mirror
from .i18n import t

logger = logging.getLogger(__name__)

# 进度文字里「已用 n 秒」的刷新间隔：250 ms —— 够看出在跳，又不至于刷屏。
_PROGRESS_TICK_MS = 250


class MirrorTestMixin:
    # ---------- 按钮：开始 / 取消 ----------

    def mirror_test_click(self) -> None:
        """「测速」按钮：没在测就开测；正在测时这一次点击＝取消。"""
        if self._mirror_test_running():
            logger.info("用户点了「取消」，正在收手中")
            self._mirror_test_cancel.set()
            # 提示行切成「正在取消…」。旗标先立住：ticker 每 250 ms 刷新
            # 时先看它，不会把文案抢回「正在测速」。
            self._mirror_test_canceling = True
            self._mirror_test_refresh_progress()
            return
        targets = self._mirror_test_targets()
        total = min(len(targets), mirrortest.MAX_TARGETS)
        # 每一轮都是新的取消信号：上一轮 set 过的 Event 不能带过来
        #（否则新线程一进循环就判「已取消」，秒退）。
        self._mirror_test_cancel = threading.Event()
        # 进度显示的初态：0/总数、还没有「当前候选」
        self._mirror_test_total = total
        self._mirror_test_finished = 0
        self._mirror_test_cur_prefix = None
        self._mirror_test_cur_bytes = 0
        self._mirror_test_canceling = False
        self._set_mirror_test_button(t("common.cancel"))
        self._mirror_test_refresh_progress()     # 提示行 =「正在测速…（0/N）」
        self._mirror_test_set_progress_visible(True)
        self._mirror_test_thread = threading.Thread(
            target=self._mirror_test_task,
            args=(targets, total),
            daemon=True,
        )
        self._mirror_test_start_ticker()
        self._mirror_test_thread.start()

    def _mirror_test_running(self) -> bool:
        th = self._mirror_test_thread
        return th is not None and th.is_alive()

    def _mirror_test_targets(self) -> list[str]:
        """要测的候选：直连 + 下拉清单 + 当前手填的那个（去重，保序）。

        **直连（空前缀）也是一名正式候选** —— 挂着系统代理/VPN 的时候
        它可能比任何镜像都快，不放进来的话用户就看不到这个结论。
        """
        raw: list[str] = [""]
        raw += list(self.config.get("github_mirror_presets"))
        raw.append(self.github_mirror_var.get())
        out: list[str] = []
        for item in raw:
            text = str(item or "").strip()
            # 手填的也先过一遍整理（只写主机名、缺斜杠都能救）；整理完
            # 认不出来的（比如打错的地址）= 空串 = 直连，跟直连项去重。
            prefix = normalize_mirror(text) if text else ""
            if prefix not in out:
                out.append(prefix)
        return out

    # ---------- 后台线程 ----------

    def _mirror_test_task(self, targets: list[str], total: int) -> None:
        """后台线程：串行测完所有候选（这里唯一碰网络的地方）。"""
        thread = threading.current_thread()
        done = 0

        def on_result(_result: mirrortest.MirrorResult) -> None:
            nonlocal done
            done += 1
            n = done
            self.run_on_gui(lambda: self._mirror_test_progress_result(thread, n))

        def on_progress(prefix: str, nbytes: int) -> None:
            # 每个候选先来一条 0（开测），之后按块报 —— 界面靠它把进度条
            # 画细、把「已用时间」的秒表切到这个候选上。
            self.run_on_gui(
                lambda: self._mirror_test_progress_bytes(thread, prefix, nbytes)
            )

        # 两个停止信号合二为一：用户点了「取消」，或者启动器要关了。
        stop = mirrortest.any_stop(self.stop_event, self._mirror_test_cancel)
        error = ""
        try:
            results = mirrortest.test_mirrors(
                targets,
                on_result=on_result,
                on_progress=on_progress,
                stop_event=stop,
            )
        except Exception as e:                                  # noqa: BLE001
            # mirrortest 对外承诺不抛；真抛了也只当「这一轮没结果」，
            # 不许把启动器带崩（测速是辅助功能）。
            logger.error(f"镜像测速异常: {e}", exc_info=True)
            results, error = [], str(e)
        canceled = stop.is_set()
        self.run_on_gui(lambda: self._mirror_test_done(
            thread, results, total, canceled, error
        ))

    def _mirror_test_done(
        self,
        thread: threading.Thread,
        results: list[mirrortest.MirrorResult],
        total: int,
        canceled: bool,
        error: str,
    ) -> None:
        """回到 GUI 线程收尾：收进度、恢复按钮、写提示行、弹结果窗。"""
        if thread is not self._mirror_test_thread:
            # 上一轮的结果姗姗来迟（用户取消后立刻又测了一轮）——
            # 别拿旧数据盖掉新一轮的进度显示。
            logger.info("收到过期的测速结果，已丢弃")
            return
        # 先把进度显示收干净（ticker 停、条收回、旗标复位）再写结论 ——
        # 「正在测速」文案和进度条都不该在结果窗弹出后还挂着。
        self._mirror_test_stop_ticker()
        self._mirror_test_canceling = False
        self._mirror_test_cur_prefix = None
        self._mirror_test_cur_bytes = 0
        self._mirror_test_set_progress_visible(False)
        self._set_mirror_test_button(t("settings.mirror_test"))
        if not results:
            if error:
                self.mirror_test_var.set(t("mirror_test.failed", err=error))
            elif canceled:
                self.mirror_test_var.set(t("mirror_test.canceled"))
            else:
                self.mirror_test_var.set(t("mirror_test.none_ok"))
            return
        ordered = mirrortest.sorted_results(results)
        best = next((r for r in ordered if r.ok), None)
        if best is not None:
            self.mirror_test_var.set(t("mirror_test.summary",
                                       name=self._mirror_display_name(best.prefix),
                                       speed=mirrortest.format_speed(best.speed_bps)))
        else:
            self.mirror_test_var.set(t("mirror_test.none_ok"))
        self._mirror_test_show(ordered, total, canceled)

    # ---------- 进度显示（进度条 + 「正在测哪个」）----------

    def _mirror_test_progress_bytes(
        self, thread: threading.Thread, prefix: str, nbytes: int
    ) -> None:
        """后台报来的「某候选读到 n 字节」（已经转回 GUI 线程）。"""
        if thread is not self._mirror_test_thread:
            return          # 上一轮姗姗来迟的事件：别拿旧字节盖新一轮的条
        if prefix != self._mirror_test_cur_prefix:
            # 换候选了：秒表的起点跟着挪。连不上的候选也走这儿 ——
            # mirrortest 对每个候选都会先发一条 0 字节的「开测」事件。
            self._mirror_test_cur_prefix = prefix
            self._mirror_test_cur_started = time.monotonic()
        self._mirror_test_cur_bytes = nbytes
        self._mirror_test_refresh_progress()

    def _mirror_test_progress_result(
        self, thread: threading.Thread, done: int
    ) -> None:
        """后台报来的「一个候选测完了」（已经转回 GUI 线程）。"""
        if thread is not self._mirror_test_thread:
            return
        self._mirror_test_finished = done
        self._mirror_test_cur_prefix = None
        self._mirror_test_cur_bytes = 0
        self._mirror_test_refresh_progress()

    def _mirror_test_refresh_progress(self) -> None:
        """把当前进度画到提示行 + 进度条上（只能在 GUI 线程调）。

        进度条的最大刻度 = 候选数，读数 = 已测完 + 当前候选的字节比例；
        文字里带「已用 n 秒」的秒表（ticker 每 250 ms 刷一次）—— 单个
        候选卡在连接 / 等首字节时条不动，秒表还在跳，看得出没死。
        """
        total = self._mirror_test_total
        if self._mirror_test_canceling:
            text = t("mirror_test.canceling")
        elif self._mirror_test_cur_prefix is None:
            text = t("mirror_test.running",
                     done=self._mirror_test_finished, total=total)
        else:
            elapsed = max(0.0, time.monotonic() - self._mirror_test_cur_started)
            text = t("mirror_test.testing",
                     current=self._mirror_test_finished + 1, total=total,
                     name=self._mirror_display_name(self._mirror_test_cur_prefix),
                     elapsed=f"{elapsed:.1f}")
        self.mirror_test_var.set(text)
        frac = 0.0
        if self._mirror_test_cur_prefix is not None:
            # 0.99 封顶：样本读满了也别提前显示成「整格完」——那一格要等
            # 结果事件（finished+1）才落地。
            frac = min(
                self._mirror_test_cur_bytes / max(1, mirrortest.SAMPLE_BYTES),
                0.99,
            )
        try:
            self.mirror_progress.configure(
                maximum=max(1, total), value=self._mirror_test_finished + frac
            )
        except tk.TclError:
            logger.debug("测速进度条不存在（设置页被重建过），跳过读数")

    def _mirror_test_set_progress_visible(self, show: bool) -> None:
        """进度条只在测速期间占地方（grid / grid_remove 成对用，行列参数不丢）。"""
        try:
            if show:
                self.mirror_progress.grid()
            else:
                self.mirror_progress.grid_remove()
        except tk.TclError:
            logger.debug("测速进度条不存在（设置页被重建过），跳过显隐")

    # ---------- 秒表（ticker）----------

    def _mirror_test_start_ticker(self) -> None:
        self._mirror_test_stop_ticker()
        self._mirror_test_tick_job = self.root.after(
            _PROGRESS_TICK_MS, self._mirror_test_tick
        )

    def _mirror_test_stop_ticker(self) -> None:
        job, self._mirror_test_tick_job = self._mirror_test_tick_job, None
        if job is not None:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass

    def _mirror_test_tick(self) -> None:
        """每 250 ms 刷一次文字：「已用 n 秒」跳着走，看着就不像卡死。"""
        self._mirror_test_tick_job = None
        if self.stop_event.is_set() or not self._mirror_test_running():
            return              # 测完了 / 启动器正在关：不再接力
        try:
            self._mirror_test_refresh_progress()
            self._mirror_test_tick_job = self.root.after(
                _PROGRESS_TICK_MS, self._mirror_test_tick
            )
        except tk.TclError:
            pass                # 窗口正在销毁

    # ---------- 结果窗口 ----------

    def _mirror_test_show(
        self,
        ordered: list[mirrortest.MirrorResult],
        total: int,
        canceled: bool,
    ) -> None:
        """列出每个候选的速度/延迟/状态，让用户挑一个填回设置页。

        模态（跟 ``_choose_profile_dialog`` 一个路子）：这是个「选一个」的
        窗口，选完就关，不需要跟主窗口并排看。
        """
        win = tk.Toplevel(self.root)
        win.title(t("mirror_test.title"))
        self._mirror_test_win = win

        # 按钮先钉到底部占好位置，再排上面的内容 —— 这是本项目对话框的
        # 通例（内容再高也不会把按钮挤出可视区，见 _choose_profile_dialog）。
        foot = ttk.Frame(win)
        foot.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=10)

        body = ttk.Frame(win, padding=(12, 12, 12, 0))
        body.pack(fill=tk.BOTH, expand=True)
        intro = t(
            "mirror_test.intro",
            size=mirrortest.format_size(mirrortest.SAMPLE_BYTES),
        )
        if canceled:
            intro += t("mirror_test.partial", n=max(0, total - len(ordered)))
        ttk.Label(
            body, text=intro, wraplength=600, justify=tk.LEFT, anchor=tk.W
        ).pack(fill=tk.X)

        table = ttk.Frame(body)
        table.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        tree = ttk.Treeview(
            table,
            columns=("mirror", "speed", "latency", "status"),
            show="headings",
            selectmode="browse",
            height=min(max(len(ordered), 3), 10),
        )
        for col, key, width, anchor in (
            ("mirror", "mirror_test.col.mirror", 260, tk.W),
            ("speed", "mirror_test.col.speed", 90, tk.E),
            ("latency", "mirror_test.col.latency", 80, tk.E),
            ("status", "mirror_test.col.status", 200, tk.W),
        ):
            tree.heading(col, text=t(key))
            tree.column(col, width=width, anchor=anchor, stretch=True)
        # iid → 前缀：显示名会被翻译（直连那行），不能拿它反查地址。
        row_prefix: dict[str, str] = {}
        for i, r in enumerate(ordered):
            iid = str(i)
            row_prefix[iid] = r.prefix
            tree.insert("", tk.END, iid=iid, values=(
                self._mirror_display_name(r.prefix),
                mirrortest.format_speed(r.speed_bps),
                mirrortest.format_latency(r.latency_ms),
                self._mirror_status_text(r),
            ))
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(table, command=tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.config(yscrollcommand=sb.set)
        first = tree.get_children()
        if first:
            # 列表是排过序的：第一行就是「最快且可用」那个，默认选中它
            tree.selection_set(first[0])
            tree.focus_set()

        def apply_selected() -> None:
            selection = tree.selection()
            if not selection:
                messagebox.showinfo(
                    t("mirror_test.title"), t("mirror_test.need_select")
                )
                return
            self._mirror_test_apply(row_prefix.get(selection[0]))

        fastest = next((r for r in ordered if r.ok), None)
        btn_fast = ttk.Button(
            foot,
            text=t("mirror_test.apply_fastest"),
            command=lambda: self._mirror_test_apply(
                fastest.prefix if fastest is not None else None
            ),
        )
        btn_fast.pack(side=tk.LEFT, padx=4)
        if fastest is None:
            # 一个都没测通时「用最快的」无从谈起，置灰比点了弹提示好
            btn_fast.state(["disabled"])
        ttk.Button(
            foot, text=t("mirror_test.apply"), command=apply_selected
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            foot, text=t("common.close"), command=win.destroy
        ).pack(side=tk.RIGHT, padx=4)

        tree.bind("<Double-1>", lambda e: apply_selected())
        win.bind("<Return>", lambda e: apply_selected())
        win.bind("<Escape>", lambda e: win.destroy())
        win.protocol("WM_DELETE_WINDOW", win.destroy)

        self._fit_dialog(win, base_width=680, min_height=360)
        restore = self._begin_modal(win, self.root)
        win.wait_window()
        restore()
        self._mirror_test_win = None

    def _mirror_test_apply(self, prefix: str | None) -> None:
        """把选中的前缀填进设置页的下拉框（**不落盘**，等「保存并返回」）。"""
        if prefix is None:
            messagebox.showinfo(
                t("mirror_test.title"), t("mirror_test.none_ok")
            )
            return
        self.github_mirror_var.set(prefix)
        self.mirror_test_var.set(t(
            "mirror_test.applied", name=self._mirror_display_name(prefix)
        ))
        logger.info(f"测速结果已填入设置页: {prefix or '（直连）'}")
        win, self._mirror_test_win = self._mirror_test_win, None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    # ---------- 小工具 ----------

    def _mirror_display_name(self, prefix: str) -> str:
        return prefix or t("mirror_test.direct")

    def _mirror_status_text(self, r: mirrortest.MirrorResult) -> str:
        if r.status == mirrortest.STATUS_OK:
            return t("mirror_test.status.ok")
        if r.status == mirrortest.STATUS_TIMEOUT:
            return t("mirror_test.status.timeout")
        if r.status == mirrortest.STATUS_SHORT:
            return t("mirror_test.status.short",
                     size=mirrortest.format_size(r.bytes_read))
        if r.status == mirrortest.STATUS_HTTP:
            return t("mirror_test.status.http", code=r.http_code or "?")
        if r.status == mirrortest.STATUS_CANCELED:
            return t("mirror_test.status.canceled")
        return t("mirror_test.status.net")

    def _set_mirror_test_button(self, text: str) -> None:
        """改「测速」按钮上的字（测速中转成「取消」）。

        控件可能已经不在（换语言的整页重建把它换掉了）——那就不动，
        设置页重建后的按钮反正显示的是默认文案。
        """
        try:
            self.mirror_test_btn.configure(text=text)
        except tk.TclError:
            logger.debug("测速按钮已不存在（设置页被重建过），跳过改字")
