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

进度显示（设置页那一条 + 进度条）：★ 采样是**交错多轮**的（见 mirrortest），
所以刻度按**采样次数**算（候选数 × 轮数），读数 = 已跑完的采样 + 当前这一次
已用的时间比例；文字带「已用 n 秒」的秒表（ticker 每 250 ms 一跳），
单个候选卡在「连接 / 等首字节」时也不会看起来像死掉。数据源是 mirrortest
的 ``on_progress`` 回调（每次采样都以一条 0 字节的「开测」事件开头，本层靠
它数采样次数），本层只负责显示与收放。

⚠️ ``on_result`` 改成等**所有轮次跑完**才逐条到达（交错采样的必然结果，
见 mirrortest 模块 docstring）—— 别拿它当进度源，进度一律看 ``on_progress``。
"""
from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import mirrortest
from .config import MAX_MIRROR_ROUNDS, normalize_mirror
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
        # ★ 轮数先读、先校验：界面上显示几轮就必须跑几轮 —— 不合法
        #   （空/非整数/超范围）会弹错并返回 None，这里直接收手，后面的
        #   状态一律不动（不给「填错值」留半初始化痕迹）。
        rounds = self._mirror_test_rounds_from_ui()
        if rounds is None:
            return
        targets = self._mirror_test_targets()
        total = min(len(targets), mirrortest.MAX_TARGETS)
        # 每一轮都是新的取消信号：上一轮 set 过的 Event 不能带过来
        #（否则新线程一进循环就判「已取消」，秒退）。
        self._mirror_test_cancel = threading.Event()
        # 进度显示的初态：采样 0 / (候选数 × 轮数)、还没有「当前采样」。
        # ★ 轮数用设置页**当前填的**值（即测即用 —— 不必先保存，跟旁边
        #   Java「检测」按钮、镜像本身都是同一口径，见上面 rounds 的读取）
        #   —— 采样总数 = 候选数 × 轮数，直接决定等待时长；后台线程拿的
        #   是同一份值（随线程参数传下去，见 _mirror_test_task）。
        self._mirror_test_total = total
        self._mirror_test_rounds = rounds
        self._mirror_test_samples = 0
        self._mirror_test_cur_prefix = None
        self._mirror_test_canceling = False
        self._set_mirror_test_button(t("common.cancel"))
        self._mirror_test_refresh_progress()     # 提示行 =「正在测速…（0/采样总数）」
        self._mirror_test_set_progress_visible(True)
        self._mirror_test_thread = threading.Thread(
            target=self._mirror_test_task,
            args=(targets, total, self._mirror_test_rounds),
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

    def _mirror_test_rounds_from_ui(self) -> int | None:
        """读设置页当前填的轮数：**合法就用、不合法弹错并中止**。

        跟 Java「检测」按钮一个哲学 —— 动作用眼前的值，不必先保存。
        ★ 界面上显示什么，就必须用什么：空框 / 非整数 / 超范围一律
        **弹错拦下**（返回 None），绝不悄悄夹成别的值 —— 否则会出现
        「界面显示 5、实际跑 3」这种最伤人的不一致（2026-10-12 用户
        实测反馈）。合法时只影响**这一次测速**：绝不写回配置（想固定
        下来照旧要「保存并返回」；保存时走的也是本函数，两处口径完全
        一致，不会一个严一个松）。
        """
        try:
            raw = self.mirror_test_rounds_var.get()
        except tk.TclError:
            raw = None
        if raw is not None and 1 <= raw <= MAX_MIRROR_ROUNDS:
            return raw
        logger.warning(f"测速轮数不合法（界面填了 {raw!r}），已拦下并提示")
        messagebox.showerror(
            t("err.rounds_title"),
            t("err.rounds_body", max=MAX_MIRROR_ROUNDS),
        )
        return None

    # ---------- 后台线程 ----------

    def _mirror_test_task(
        self, targets: list[str], total: int, rounds: int
    ) -> None:
        """后台线程：交错多轮测完所有候选（这里唯一碰网络的地方）。

        ``rounds`` 由调用方（GUI 线程）读出后传进来（界面当前值优先，
        不合法会被拦下，见 ``_mirror_test_rounds_from_ui``）—— 后台
        线程不自己去读配置，避免「读的时候设置页正在被重建」这类
        跨线程意外。

        ⚠️ 不接 ``on_result``：交错采样下它要**所有轮次跑完**才逐条到达，
        拿它画进度会「全程 0、末尾连跳 N 格」。进度只看 ``on_progress``
        —— 每次采样都以一条 0 字节的「开测」事件开头（mirrortest 的契约）。
        """
        thread = threading.current_thread()

        def on_progress(prefix: str, nbytes: int) -> None:
            # 0 字节 = 一次采样的第一枪（连接都还没开始）：界面靠它数采样
            # 次数、把秒表切到这一次采样上。非零事件本层不消费（刻度按
            # 时间比例画）。
            self.run_on_gui(
                lambda: self._mirror_test_progress_bytes(thread, prefix, nbytes)
            )

        # 两个停止信号合二为一：用户点了「取消」，或者启动器要关了。
        stop = mirrortest.any_stop(self.stop_event, self._mirror_test_cancel)
        error = ""
        try:
            results = mirrortest.test_mirrors(
                targets,
                rounds=rounds,
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
            # 抖动链路上第一名的观测极差可能很大 —— 那时「最快」只在本次
            # 排序里成立，提示行里就别说成定论（见 mirrortest.SPREAD_WARN）。
            key = ("mirror_test.summary_unstable"
                   if best.spread >= mirrortest.SPREAD_WARN
                   else "mirror_test.summary")
            self.mirror_test_var.set(t(key,
                                       name=self._mirror_display_name(best.prefix),
                                       speed=mirrortest.format_speed(best.speed_bps)))
        else:
            self.mirror_test_var.set(t("mirror_test.none_ok"))
        self._mirror_test_show(ordered, total, canceled)

    # ---------- 进度显示（进度条 + 「正在测哪个」）----------

    def _mirror_test_progress_bytes(
        self, thread: threading.Thread, prefix: str, nbytes: int
    ) -> None:
        """后台报来的「某次采样开跑 / 读到 n 字节」（已经转回 GUI 线程）。

        刻度按「采样次数 + 当前这次的时间比例」画（见 refresh_progress），
        所以只有 ``nbytes == 0`` 那一枪（每次采样的开头都是它，见
        mirrortest 的契约）需要处理；非零的块级事件现在不消费 —— 采样
        窗口由 SAMPLE_SECONDS 主导，字节数对刻度没有贡献。
        """
        if thread is not self._mirror_test_thread:
            return          # 上一轮姗姗来迟的事件：别拿旧事件盖新一轮的条
        if nbytes > 0:
            return
        # ⚠️ 判据是「0 字节」，不是「前缀变了」—— 交错多轮下同一个候选
        #    会跨轮连着被测，光看前缀认不出「又开了一枪」。
        self._mirror_test_samples += 1
        self._mirror_test_cur_prefix = prefix
        self._mirror_test_cur_started = time.monotonic()
        self._mirror_test_refresh_progress()

    def _mirror_test_refresh_progress(self) -> None:
        """把当前进度画到提示行 + 进度条上（只能在 GUI 线程调）。

        ★ 刻度按**采样次数**算（候选数 × 轮数）—— 交错采样把「一个候选
        测完再测下一个」的节奏打散了，按候选数画会和真实进度对不上。
        读数 = 已开跑的采样数 - 1（当前这个还没完）+ 当前这次已用的
        时间比例（采样窗口由 SAMPLE_SECONDS 主导，见 mirrortest）；
        文字里带「已用 n 秒」的秒表（ticker 每 250 ms 刷一次）—— 单个
        候选卡在连接 / 等首字节时条不动，秒表还在跳，看得出没死。
        """
        samples_total = max(
            1, self._mirror_test_total * max(1, self._mirror_test_rounds)
        )
        live = self._mirror_test_cur_prefix is not None
        # 死候选会中途退出后续轮次（见 mirrortest），实际采样数可能到不了
        # 刻度上限 —— 到不了就到不了，收尾时进度条反正会被收回。
        cur_no = min(self._mirror_test_samples, samples_total)
        elapsed = 0.0
        if live:
            elapsed = max(0.0, time.monotonic() - self._mirror_test_cur_started)
        if self._mirror_test_canceling:
            text = t("mirror_test.canceling")
        elif not live:
            # 还没有采样开跑（刚点下 / 被取消之后）或上一枪已收尾：
            # 报「已跑完的采样数」。
            text = t("mirror_test.running",
                     done=max(0, cur_no - 1), total=samples_total)
        else:
            text = t("mirror_test.testing",
                     current=cur_no, total=samples_total,
                     name=self._mirror_display_name(self._mirror_test_cur_prefix),
                     elapsed=f"{elapsed:.1f}")
        self.mirror_test_var.set(text)
        frac = 0.0
        if live:
            # 0.95 封顶：这一次采样读满了也别提前显示成「整格完」——
            # 那一格要等下一次「开测」事件（采样数 +1）才落地。
            frac = min(elapsed / max(0.1, mirrortest.SAMPLE_SECONDS), 0.95)
        try:
            self.mirror_progress.configure(
                maximum=samples_total,
                value=max(0, cur_no - 1) + frac,
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
            seconds=f"{mirrortest.SAMPLE_SECONDS:g}",
            rounds=max(1, self._mirror_test_rounds),
        )
        if canceled:
            intro += t("mirror_test.partial", n=max(0, total - len(ordered)))
        ttk.Label(
            body, text=intro, wraplength=600, justify=tk.LEFT, anchor=tk.W
        ).pack(fill=tk.X)
        if any(r.ok and r.spread >= mirrortest.SPREAD_WARN for r in ordered):
            # 抖动大时「排名」不是定论 —— 在表上面就说清楚，别让用户拿
            # 「第一名」当承诺（判据见 mirrortest.SPREAD_WARN）。
            ttk.Label(
                body,
                text=t("mirror_test.unstable_note",
                       x=f"{mirrortest.SPREAD_WARN:g}"),
                wraplength=600, justify=tk.LEFT, anchor=tk.W,
            ).pack(fill=tk.X, pady=(4, 0))

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
            if r.spread >= mirrortest.SPREAD_WARN:
                # 多轮之间极差很大：结果是有效的，但名次只在本次成立
                return t("mirror_test.status.ok_unstable",
                         spread=f"{r.spread:.0f}")
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
