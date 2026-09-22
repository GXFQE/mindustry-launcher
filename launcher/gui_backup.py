# -*- coding: utf-8 -*-
"""备份界面：备份管理窗口、备份与恢复任务。"""
import json
import logging
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from .storage import BackupManager

logger = logging.getLogger(__name__)


class BackupMixin:
    def manage_backups(self) -> None:
        profile = self.config.get_current_profile()
        manager = self.backup_manager
        win = tk.Toplevel(self.root)
        win.title(f"管理备份 - 存档「{profile}」")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(
            win,
            text=f"存档分类：{profile}    数据目录："
            f"{self.config.get_current_save_path()}",
            font=("Microsoft YaHei", 8),
            anchor=tk.W,
        ).pack(fill=tk.X, padx=10, pady=(8, 0))
        ttk.Label(
            win,
            text="（各存档分类的备份互相隔离，这里只显示当前分类）",
            font=("Microsoft YaHei", 8),
            foreground="#555555",
            anchor=tk.W,
        ).pack(fill=tk.X, padx=10, pady=(2, 0))
        list_frame = ttk.Frame(win)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        listbox = tk.Listbox(list_frame, font=self.font)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(list_frame, command=listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        listbox.config(yscrollcommand=scrollbar.set)
        backup_paths = []

        def refresh() -> None:
            nonlocal backup_paths
            backup_paths = manager.list_backups()
            listbox.delete(0, tk.END)
            for bp in backup_paths:
                listbox.insert(tk.END, manager.get_backup_display(bp))

        refresh()
        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        def do_immediate_backup() -> None:
            if self._game_is_running() and not messagebox.askyesno(
                "游戏运行中", "游戏运行中备份可能损坏存档，继续吗？"
            ):
                return
            # 备份 = 读完整个存档目录 + 回收对象池（几秒到几十秒）。
            # 放在 GUI 线程上就是整窗假死，丢后台跑；注意用窗口打开时
            # 抓住的这个 manager，跟列表显示的分类保持一致。
            self.set_status(
                f"💾 正在备份 (存档「{manager.profile_name}」)..."
            )
            self.executor.submit(self._backup_task, manager, refresh)

        def do_restore() -> None:
            sel = listbox.curselection()
            if not sel:
                messagebox.showwarning("警告", "请选择备份")
                return
            if self._game_is_running():
                messagebox.showinfo("提示", "游戏运行中无法恢复备份")
                return
            bp = backup_paths[sel[0]]
            if not messagebox.askyesno(
                "确认恢复",
                f"确定要恢复这个备份吗？\n\n"
                f"存档分类：{profile}\n"
                f"目标目录：{manager.config.get_save_path(profile)}\n\n"
                f"该分类的当前存档将被覆盖。",
            ):
                return
            self.executor.submit(self._restore_backup_task, bp, manager)

        def do_rename() -> None:
            sel = listbox.curselection()
            if not sel:
                messagebox.showwarning("警告", "请选择备份")
                return
            bp = backup_paths[sel[0]]
            old_desc = ""
            try:
                manifest = json.loads(bp.read_text(encoding="utf-8"))
                old_desc = manifest.get("description", "")
            except Exception:
                pass
            with self._release_grab(win):
                new_desc = simpledialog.askstring(
                    "备注备份",
                    "请输入备注（留空则显示时间）:",
                    initialvalue=old_desc,
                    parent=win,
                )
            if new_desc is not None:
                try:
                    manager.rename_backup(bp, new_desc.strip())
                    refresh()
                    self.set_status("✅ 备份备注已更新")
                except Exception as e:
                    logger.error(f"重命名备份失败: {e}")
                    messagebox.showerror("错误", f"重命名失败: {e}")

        ttk.Button(
            btn_frame, text="立即备份", command=do_immediate_backup
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="恢复备份", command=do_restore).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(btn_frame, text="备注", command=do_rename).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(btn_frame, text="关闭", command=win.destroy).pack(
            side=tk.RIGHT, padx=5
        )
        self._fit_dialog(win, base_width=580, min_height=420)

    @staticmethod
    def _safe_refresh(refresh_cb) -> None:
        """窗口可能已经被用户关掉了，刷新时别再抛 TclError 出来。"""
        try:
            refresh_cb()
        except tk.TclError as e:
            logger.debug(f"刷新已关闭的窗口失败: {e}")

    def _backup_task(self, manager: BackupManager, refresh_cb) -> None:
        try:
            manager.create_backup()
            self.set_status(
                f"💾 手动备份完成 (存档「{manager.profile_name}」)"
            )
            self.run_on_gui(lambda: self._safe_refresh(refresh_cb))
        except Exception as e:
            logger.error(f"手动备份失败: {e}")
            self.run_on_gui(
                lambda e=e: messagebox.showerror("错误", f"备份失败: {e}")
            )

    def _restore_backup_task(
        self, backup_path: Path, manager: BackupManager
    ) -> None:
        try:
            manager.restore_backup(backup_path)
            self.set_status(f"✅ 备份恢复完成 (存档「{manager.profile_name}」)")
            self.run_on_gui(
                lambda: messagebox.showinfo("成功", "存档恢复完成！")
            )
        except Exception as e:
            logger.error(f"恢复备份任务失败: {e}")
            self.run_on_gui(
                lambda e=e: messagebox.showerror("错误", f"恢复失败: {e}")
            )
