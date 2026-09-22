# -*- coding: utf-8 -*-
"""运行日志窗口：游戏实时输出 + 启动器日志。

两个页签：
  * 游戏输出 —— 实时滚动本次（或上次）游戏进程的 stdout/stderr。
    这解决的是「游戏崩了/卡住了，启动器这边一点线索都没有」的问题。
  * 启动器日志 —— 直接看 launcher.log，排查启动失败时更相关。

⚠️ 刷新用的是 ``after`` 轮询而不是线程回调：Tk 控件只能在 GUI 线程碰，
让工作线程反过来调 UI 是未定义行为（项目里已经栽过一次）。
"""
import logging
import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .gamelog import GameLog
from .utils import LOG_FILE

logger = logging.getLogger(__name__)

# 刷新间隔。400 ms 在「看着它滚」和「别白烧 CPU」之间比较平衡。
POLL_MS = 400
# 启动器日志只显示最后这么多行：跑久了 launcher.log 能到几 MB
LAUNCHER_LOG_TAIL = 3000
LOG_FONT = ("Consolas", 9)
# 读尾部时的块大小
_TAIL_BLOCK = 64 * 1024


def tail_text(path: Path, max_lines: int) -> str:
    """读文件最后 max_lines 行（从尾部按块往回读）。

    不 ``read_text()`` 整个读：launcher.log 跑久了是 MB 级的，
    而这个窗口只要看结尾。
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            chunks: list[bytes] = []
            pos = size
            newlines = 0
            while pos > 0 and newlines <= max_lines:
                step = min(_TAIL_BLOCK, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
    except OSError as e:
        return f"（读取失败：{e}）"
    data = b"".join(reversed(chunks))
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:]) + "\n"


class LogMixin:
    def open_log_window(self) -> None:
        """打开运行日志窗口（只允许一个实例）。"""
        if self._focus_existing_log_window():
            return
        win = tk.Toplevel(self.root)
        self._log_win = win
        win.title("运行日志")
        win.transient(self.root)
        win.geometry("840x540")
        win.minsize(560, 320)

        nb = ttk.Notebook(win)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))
        self._build_game_log_tab(nb)
        self._build_launcher_log_tab(nb)

        foot = ttk.Frame(win)
        foot.pack(fill=tk.X, padx=10, pady=8)
        ttk.Button(
            foot, text="打开日志文件夹", command=self._open_log_dir
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            foot, text="复制游戏输出", command=self._copy_game_log
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(foot, text="关闭", command=self._close_log_window).pack(
            side=tk.RIGHT, padx=4
        )

        win.protocol("WM_DELETE_WINDOW", self._close_log_window)
        self._log_shown_seq = 0
        self._log_poll_id = None
        self._launcher_log_stamp = None
        # 打开时先铺一次已有内容（比如上一次跑游戏留下的），再开始轮询
        self._refresh_game_log()
        self._refresh_launcher_log(force=True)
        self._schedule_log_poll()

    def _log_window_alive(self) -> bool:
        """日志窗口还在吗 —— **只查，不动窗口**。

        ⚠️ 轮询里必须用这个，不能调 ``_focus_existing_log_window()``：
        那个会 ``deiconify() + lift()``，而轮询是每 400 ms 一次。反复对
        已经正常显示的窗口发 deiconify/lift，Windows 会当成「程序在反复
        请求前台焦点」，任务栏图标就一直闪/高亮（用户报的现象）。
        """
        win = self._log_win
        if win is None:
            return False
        try:
            exists = bool(win.winfo_exists())
        except tk.TclError:
            exists = False
        if not exists:
            self._log_win = None
        return exists

    def _focus_existing_log_window(self) -> bool:
        """把已经开着的日志窗口提到前面（**只在用户点按钮时**调用）。"""
        if not self._log_window_alive():
            return False
        win = self._log_win
        try:
            win.deiconify()
            win.lift()
        except tk.TclError:
            self._log_win = None
            return False
        return True

    # ---------- 界面搭建 ----------

    def _make_log_text(self, parent: tk.Misc) -> tk.Text:
        """只读 + 等宽 + 带双向滚动条的文本区。"""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        box = tk.Text(
            frame,
            wrap=tk.NONE,          # 日志一行很长，硬换行反而看不清栈
            font=LOG_FONT,
            state=tk.DISABLED,     # 只读；写入时临时解禁（_text_*）
            height=10,
        )
        ysb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=box.yview)
        xsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=box.xview)
        box.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        box.config(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        return box

    def _build_game_log_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="游戏输出")
        head = ttk.Frame(tab)
        head.pack(fill=tk.X, padx=8, pady=(8, 0))
        self._log_follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            head, text="自动滚动到最新", variable=self._log_follow
        ).pack(side=tk.LEFT)
        self._log_state_var = tk.StringVar(value="")
        ttk.Label(
            head,
            textvariable=self._log_state_var,
            font=("Microsoft YaHei", 8),
            foreground="#555555",
        ).pack(side=tk.LEFT, padx=10)
        self._game_log_text = self._make_log_text(tab)

    def _build_launcher_log_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="启动器日志")
        head = ttk.Frame(tab)
        head.pack(fill=tk.X, padx=8, pady=(8, 0))
        ttk.Button(head, text="刷新", command=self._refresh_launcher_log).pack(
            side=tk.LEFT
        )
        ttk.Button(
            head, text="用默认程序打开", command=self._open_launcher_log
        ).pack(side=tk.LEFT, padx=6)
        self._launcher_log_info_var = tk.StringVar(value="")
        ttk.Label(
            head,
            textvariable=self._launcher_log_info_var,
            font=("Microsoft YaHei", 8),
            foreground="#555555",
        ).pack(side=tk.LEFT, padx=10)
        ttk.Label(
            tab,
            text=f"{LOG_FILE}",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
            anchor=tk.W,
        ).pack(fill=tk.X, padx=10, pady=(6, 0))
        self._launcher_log_text = self._make_log_text(tab)

    @staticmethod
    def _text_replace(box: tk.Text, text: str) -> None:
        box.config(state=tk.NORMAL)
        box.delete("1.0", tk.END)
        box.insert("1.0", text)
        box.config(state=tk.DISABLED)

    @staticmethod
    def _text_append(box: tk.Text, text: str) -> None:
        if not text:
            return
        box.config(state=tk.NORMAL)
        box.insert(tk.END, text)
        box.config(state=tk.DISABLED)

    # ---------- 轮询 ----------

    def _schedule_log_poll(self) -> None:
        win = self._log_win
        if win is None:
            return
        try:
            self._log_poll_id = win.after(POLL_MS, self._poll_log)
        except tk.TclError:
            self._log_poll_id = None

    def _poll_log(self) -> None:
        self._log_poll_id = None
        # 注意用 _log_window_alive：这里**不能** lift，否则任务栏一直闪
        if not self._log_window_alive():
            return
        try:
            self._refresh_game_log()
        except tk.TclError:
            # 窗口在刷新途中被关掉了：别再排下一次
            return
        self._schedule_log_poll()

    def _refresh_game_log(self) -> None:
        log = self.game_log
        last, first, dropped, lines = log.snapshot()
        box = self._game_log_text
        shown = self._log_shown_seq
        redraw = False
        if last < shown:
            # 序号倒退 = 又开了一局（GameLog.start 会把序号清零重来）
            redraw = True
        elif lines and first > shown + 1:
            # 内存缓冲没跟上，中间的行已经看不到了。整体重画 —— 否则窗口上
            # 会缺一段，而且看不出来缺了。
            redraw = True
        if redraw:
            self._text_replace(box, "".join(ln + "\n" for ln in lines))
        elif last > shown:
            start = max(0, shown + 1 - first)
            self._text_append(box, "".join(ln + "\n" for ln in lines[start:]))
        if (redraw or last > shown) and self._log_follow.get():
            box.see(tk.END)
        self._log_shown_seq = last
        self._log_state_var.set(self._game_log_state(log, last, dropped))

    @staticmethod
    def _game_log_state(log: GameLog, last: int, dropped: int) -> str:
        path = log.path
        where = f" → {path}" if path else "（未落盘，仅本次显示）"
        if log.active:
            state = f"● 正在收集{where}"
        elif last:
            state = f"○ 本次已结束，共 {last} 行{where}"
        else:
            state = "○ 还没有启动过游戏"
        if dropped:
            state += f"；内存只留最近 {GameLog.MAX_LINES} 行，已滚过 {dropped} 行"
        return state

    def _refresh_launcher_log(self, force: bool = False) -> None:
        """只在用户点刷新（或首次打开）时读盘。

        不做自动刷新：这个 Text 每次整体重写都会丢掉选中和滚动位置，
        而排查问题时恰恰经常是「一边看着一边选中几行复制」。
        """
        try:
            stat = LOG_FILE.stat()
        except OSError:
            self._text_replace(
                self._launcher_log_text, f"（读不到日志文件：{LOG_FILE}）"
            )
            self._launcher_log_info_var.set("文件不存在")
            return
        text = tail_text(LOG_FILE, LAUNCHER_LOG_TAIL)
        shown = text.count("\n")
        self._text_replace(self._launcher_log_text, text)
        self._launcher_log_text.see(tk.END)
        stamp = f"{stat.st_size / 1024:.0f} KB"
        if shown >= LAUNCHER_LOG_TAIL:
            stamp += f"，只显示最后 {LAUNCHER_LOG_TAIL} 行"
        self._launcher_log_info_var.set(stamp)

    # ---------- 按钮 ----------

    def _open_log_dir(self) -> None:
        path = self.game_log.path
        target = path.parent if path is not None else self.base_dir / "logs"
        if not target.is_dir():
            target = self.base_dir
        try:
            os.startfile(target)
        except OSError as e:
            messagebox.showerror("错误", f"打开目录失败: {e}")

    def _open_launcher_log(self) -> None:
        try:
            os.startfile(LOG_FILE)
        except OSError as e:
            messagebox.showerror("错误", f"打开日志文件失败: {e}")

    def _copy_game_log(self) -> None:
        text = self._game_log_text.get("1.0", tk.END)
        if not text.strip():
            self.set_status("游戏输出还是空的")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status(f"✅ 已复制游戏输出（{text.count(chr(10))} 行）")

    def _close_log_window(self) -> None:
        if self._log_poll_id is not None:
            win = self._log_win
            if win is not None:
                try:
                    win.after_cancel(self._log_poll_id)
                except tk.TclError:
                    pass
            self._log_poll_id = None
        win, self._log_win = self._log_win, None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
