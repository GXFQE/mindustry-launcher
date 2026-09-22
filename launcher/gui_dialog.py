# -*- coding: utf-8 -*-
"""对话框工具：模态 grab 管理、自适应尺寸、分类选择与数据复制。"""
import contextlib
import logging
import os
import shutil
import stat
import tkinter as tk
from collections.abc import Callable, Iterator
from pathlib import Path
from tkinter import ttk

logger = logging.getLogger(__name__)


def _is_link_like(path: Path) -> bool:
    """是不是链接 / junction。

    copytree 默认跟随符号链接；而 junction 连 ``is_symlink()`` 都返回
    False，会被当成普通目录整棵递归下去 —— 源目录里有个指回上层的
    junction 就是无限递归（把磁盘写满）。这里两种都认出来。
    """
    try:
        if path.is_symlink():
            return True
        st = os.lstat(path)
    except OSError:
        return False
    tag = getattr(st, "st_reparse_tag", 0)
    return tag in (
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1),
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", -2),
    )


def _ignore_links(dir_path: str, names: list[str]) -> list[str]:
    """给 shutil.copytree 用的 ignore：每一层都跳过链接类条目。"""
    return [n for n in names if _is_link_like(Path(dir_path) / n)]


class DialogMixin:
    @contextlib.contextmanager
    def _release_grab(self, win: tk.Misc) -> Iterator[None]:
        """临时交还窗口的模态 grab。

        Windows 的 Tk 里，一个窗口持有 grab 时，事件只会送到它的子树；
        而 simpledialog / filedialog / messagebox 建出来的都是同级的
        Toplevel（不在子树里），于是「按钮点不到」。这些标准弹窗自己会
        抢 grab，所以这里先让出来，用完再抢回去。
        """
        had_grab = False
        try:
            had_grab = win.grab_current() is win
            if had_grab:
                win.grab_release()
        except tk.TclError:
            had_grab = False
        try:
            yield
        finally:
            if had_grab:
                try:
                    win.grab_set()
                except tk.TclError as e:
                    logger.debug(f"恢复窗口 grab 失败: {e}")

    def _begin_modal(
        self, win: tk.Toplevel, parent: tk.Misc
    ) -> Callable[[], None]:
        """把一个子窗口设为模态，返回收尾函数。

        父窗口通常持有 grab，会抢走子窗口的点击事件（表现为「按钮点不到」），
        所以先临时释放，等子窗口关掉再恢复。
        """
        win.transient(parent)
        had_grab = False
        try:
            had_grab = parent.grab_current() is parent
            if had_grab:
                parent.grab_release()
        except tk.TclError:
            had_grab = False
        win.update_idletasks()
        try:
            win.grab_set()
            win.focus_force()
        except tk.TclError as e:
            logger.debug(f"模态 grab 失败（不影响使用）: {e}")

        def restore() -> None:
            if had_grab:
                try:
                    parent.grab_set()
                except tk.TclError as e:
                    logger.debug(f"恢复父窗口 grab 失败: {e}")

        return restore

    def _fit_dialog(
        self,
        win: tk.Toplevel,
        base_width: int = 460,
        min_height: int = 220,
    ) -> None:
        """按内容实测尺寸，别让提示语把按钮挤出可视区。

        Tk 的坑：一旦调了 geometry()，窗口就不再自动适应内容，
        硬写的尺寸装不下时（提示语长、路径长），底部按钮会被直接裁掉
        —— 表现就是「按钮根本看不见 / 点不到」。所以这里先量一次
        实际需要的宽高再定尺寸，长路径也能撑开窗口。
        """
        win.update_idletasks()
        widest = 0
        for child in win.winfo_children():
            try:
                widest = max(widest, child.winfo_reqwidth())
            except tk.TclError:
                pass
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        width = min(max(base_width, widest + 28), int(screen_w * 0.9))
        height = min(
            max(min_height, win.winfo_reqheight()), int(screen_h * 0.85)
        )
        win.geometry(f"{width}x{height}")

    def _choose_profile_dialog(
        self,
        parent: tk.Misc,
        title: str,
        prompt: str,
        items: list[str],
        skip_label: str = "跳过",
        detail: str = "",
    ) -> str | None:
        """让用户从存档分类里挑一个，返回名字；跳过/取消返回 None。"""
        picked: dict[str, str | None] = {"value": None}
        win = tk.Toplevel(parent)
        win.title(title)

        # 先把按钮排在底部占好位置，再排上面的内容。
        # 这样即使内容超高，按钮也不会被裁掉。
        foot = ttk.Frame(win)
        foot.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=10)

        ttk.Label(
            win, text=prompt, wraplength=520, justify=tk.LEFT, anchor=tk.W
        ).pack(fill=tk.X, padx=12, pady=(12, 4))
        if detail:
            ttk.Label(
                win,
                text=detail,
                font=("Microsoft YaHei", 8),
                foreground="#555555",
                justify=tk.LEFT,
                anchor=tk.W,
            ).pack(fill=tk.X, padx=12, pady=(0, 8))

        list_frame = ttk.Frame(win)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12)
        box = tk.Listbox(
            list_frame, font=self.font, activestyle="dotbox", height=6
        )
        box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_frame, command=box.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        box.config(yscrollcommand=sb.set)
        for item in items:
            box.insert(tk.END, item)
        if items:
            box.selection_set(0)
            box.focus_set()

        def confirm(event: object = None) -> None:
            sel = box.curselection()
            if sel:
                picked["value"] = items[sel[0]]
                win.destroy()

        box.bind("<Double-Button-1>", confirm)
        win.bind("<Return>", confirm)
        win.bind("<Escape>", lambda e: win.destroy())
        ttk.Button(foot, text="确定", command=confirm).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(foot, text=skip_label, command=win.destroy).pack(
            side=tk.LEFT, padx=4
        )
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._fit_dialog(win, base_width=480, min_height=280)
        restore = self._begin_modal(win, parent)
        win.wait_window()
        restore()
        return picked["value"]

    def _copy_profile_data(self, src: Path, dst: Path) -> tuple[int, str]:
        """把 src 数据目录的全部内容复制进 dst，返回 (成功项数, 错误说明)。"""
        src = Path(src)
        dst = Path(dst)
        if not src.is_dir():
            return 0, f"源目录不存在: {src}"
        src_r = src.resolve()
        dst_r = dst.resolve()
        if src_r == dst_r:
            return 0, "源目录和目标目录是同一个"
        if src_r.is_relative_to(dst_r):
            return 0, "源目录在目标目录内部，会造成无限递归"
        if dst_r.is_relative_to(src_r):
            return 0, "目标目录在源目录内部，会造成无限递归"
        try:
            dst.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return 0, f"无法创建目标目录: {e}"
        copied = 0
        errors: list[str] = []
        for item in sorted(src.iterdir()):
            target = dst / item.name
            if _is_link_like(item):
                # 不跟随链接/junction：既是防无限递归，也是语义问题 ——
                # "复制"会变成复制链接指向的那份内容，体积和结果都不对
                errors.append(f"{item.name}: 是链接/junction，已跳过")
                logger.warning(f"跳过链接类条目: {item}")
                continue
            try:
                if item.is_dir():
                    shutil.copytree(
                        item,
                        target,
                        dirs_exist_ok=True,
                        ignore=_ignore_links,
                    )
                else:
                    shutil.copy2(item, target)
                copied += 1
            except (OSError, shutil.Error) as e:
                errors.append(f"{item.name}: {e}")
                logger.warning(f"复制 {item} 失败: {e}")
        return copied, "；".join(errors)
