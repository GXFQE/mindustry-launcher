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
"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import mirrortest
from .config import normalize_mirror
from .i18n import t

logger = logging.getLogger(__name__)


class MirrorTestMixin:
    # ---------- 按钮：开始 / 取消 ----------

    def mirror_test_click(self) -> None:
        """「测速」按钮：没在测就开测；正在测时这一次点击＝取消。"""
        if self._mirror_test_running():
            logger.info("用户点了「取消」，正在收手中")
            self._mirror_test_cancel.set()
            self.mirror_test_var.set(t("mirror_test.canceling"))
            return
        targets = self._mirror_test_targets()
        total = min(len(targets), mirrortest.MAX_TARGETS)
        # 每一轮都是新的取消信号：上一轮 set 过的 Event 不能带过来
        #（否则新线程一进循环就判「已取消」，秒退）。
        self._mirror_test_cancel = threading.Event()
        self._mirror_test_thread = threading.Thread(
            target=self._mirror_test_task,
            args=(targets, total),
            daemon=True,
        )
        self._set_mirror_test_button(t("common.cancel"))
        self.mirror_test_var.set(
            t("mirror_test.running", done=0, total=total)
        )
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
            self.run_on_gui(lambda: self.mirror_test_var.set(
                t("mirror_test.running", done=n, total=total)
            ))

        # 两个停止信号合二为一：用户点了「取消」，或者启动器要关了。
        stop = mirrortest.any_stop(self.stop_event, self._mirror_test_cancel)
        error = ""
        try:
            results = mirrortest.test_mirrors(
                targets, on_result=on_result, stop_event=stop
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
        """回到 GUI 线程收尾：恢复按钮、写提示行、弹结果窗。"""
        if thread is not self._mirror_test_thread:
            # 上一轮的结果姗姗来迟（用户取消后立刻又测了一轮）——
            # 别拿旧数据盖掉新一轮的进度显示。
            logger.info("收到过期的测速结果，已丢弃")
            return
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
