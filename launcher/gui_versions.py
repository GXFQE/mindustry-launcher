# -*- coding: utf-8 -*-
"""版本界面：版本管理窗口与 JAR 导入任务。"""
import json
import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from collections.abc import Callable

from .config import sanitize_version_name
from .i18n import t
from .utils import atomic_write_json

logger = logging.getLogger(__name__)


class VersionsMixin:
    def manage_versions(self) -> None:
        if self._game_is_running():
            messagebox.showinfo(t("common.tip"), t("versions.running"))
            return
        win = tk.Toplevel(self.root)
        win.title(t("versions.title"))
        win.transient(self.root)
        win.grab_set()
        list_frame = ttk.Frame(win)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        listbox = tk.Listbox(list_frame, font=self.font)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(list_frame, command=listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        listbox.config(yscrollcommand=scrollbar.set)
        versions = self.version_manager.get_versions()

        def refresh() -> None:
            nonlocal versions
            versions = self.version_manager.get_versions()
            listbox.delete(0, tk.END)
            for v in versions:
                listbox.insert(tk.END, v["name"])

        refresh()
        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        def import_version() -> None:
            with self._release_grab(win):
                jar_path = filedialog.askopenfilename(
                    title=t("versions.pick_jar"),
                    filetypes=[(t("versions.jar_filter"), "*.jar")],
                    parent=win,
                )
            if not jar_path:
                return
            dlg = tk.Toplevel(win)
            dlg.title(t("versions.import_title"))
            tk.Label(dlg, text=t("versions.type_label")).grid(
                row=0, column=0, padx=5, pady=5, sticky=tk.W
            )
            type_var = tk.StringVar(value="Mindustry")
            ttk.Combobox(
                dlg, textvariable=type_var, values=["Mindustry", "MindustryX"]
            ).grid(row=0, column=1)
            tk.Label(dlg, text=t("versions.number_label")).grid(
                row=1, column=0, padx=5, pady=5, sticky=tk.W
            )
            ver_var = tk.StringVar()
            tk.Entry(dlg, textvariable=ver_var).grid(row=1, column=1)

            def do_import() -> None:
                vtype = type_var.get()
                try:
                    # 版本号会直接拼进清单文件名，先挡住 ／ \ 之类
                    ver = sanitize_version_name(ver_var.get())
                except ValueError as e:
                    messagebox.showerror(t("common.error"), str(e))
                    return
                dlg.destroy()
                self.set_status(t("versions.importing", type=vtype, ver=ver))
                self.executor.submit(
                    self._import_task, Path(jar_path), vtype, ver, refresh
                )

            tk.Button(dlg, text=t("versions.import"), command=do_import).grid(
                row=2, columnspan=2, pady=10
            )
            dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
            self._fit_dialog(dlg, base_width=320, min_height=170)
            restore = self._begin_modal(dlg, win)
            dlg.wait_window()
            restore()

        def delete_version() -> None:
            sel = listbox.curselection()
            if not sel:
                return
            v = versions[sel[0]]
            if messagebox.askyesno(
                t("versions.delete_title"),
                t("versions.delete_confirm", name=v["name"]),
            ):
                self.version_manager.delete_version(
                    v["type"], v["raw_version"]
                )
                refresh()
                # ★ 主界面的列表和缓存也必须跟着更新。以前只刷新了这个
                #   窗口里的局部列表，主列表还显示着已删的版本，点「启动
                #   游戏」会直接报「清单不存在」。
                self.refresh_versions()
                self.set_status(t("versions.deleted", name=v["name"]))
                # 回收要扫 3 万+ 个对象（十几秒），扔 GUI 线程上界面会假死
                self.executor.submit(
                    self._gc_task, t("versions.deleted_gc", name=v["name"])
                )

        def rename_version() -> None:
            sel = listbox.curselection()
            if not sel:
                return
            v = versions[sel[0]]
            with self._release_grab(win):
                new_name = simpledialog.askstring(
                    t("versions.rename_title"),
                    t("versions.rename_prompt"),
                    initialvalue=v["raw_version"],
                    parent=win,
                )
            if not (new_name and new_name.strip()):
                return
            new_name = new_name.strip()
            if new_name == v["raw_version"]:
                return
            if any(
                ver["type"] == v["type"] and ver["raw_version"] == new_name
                for ver in versions
            ):
                messagebox.showerror(
                    t("common.error"),
                    t("versions.exists", type=v["type"], ver=new_name),
                )
                return
            try:
                old_path = self.version_manager.manifest_path(
                    v["type"], v["raw_version"]
                )
                new_path = self.version_manager.manifest_path(
                    v["type"], new_name
                )
                data = json.loads(old_path.read_text(encoding="utf-8"))
                data["version"] = new_name
                atomic_write_json(new_path, data)
                old_path.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"重命名版本失败: {e}")
                messagebox.showerror(
                    t("common.error"), t("versions.rename_failed", err=e)
                )
                return
            refresh()
            # 同上：主列表/缓存不同步的话，重命名后会点不动游戏
            self.refresh_versions()
            self.set_status(
                t("versions.renamed", type=v["type"], ver=new_name)
            )

        ttk.Button(
            btn_frame, text=t("versions.import"), command=import_version
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame, text=t("versions.delete"), command=delete_version
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame, text=t("versions.rename"), command=rename_version
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame, text=t("common.close"), command=win.destroy
        ).pack(side=tk.RIGHT, padx=5)
        self._fit_dialog(win, base_width=540, min_height=420)

    def _gc_task(self, done_msg: str) -> None:
        """后台回收孤儿对象。

        扫一遍对象池要十几秒（本项目 3 万+ 对象），**绝不能**放在 GUI 线程上：
        以前删一个版本会让整个界面假死到扫完。
        """
        try:
            self.version_manager.garbage_collect(self.stop_event)
            self.set_status(t("versions.gc_done", msg=done_msg))
        except Exception as e:
            logger.error(f"垃圾回收失败: {e}")
            self.set_status(t("versions.gc_failed", msg=done_msg, err=e))

    def _import_task(
        self,
        jar_path: Path,
        vtype: str,
        ver: str,
        refresh_cb: Callable[[], None],
    ) -> None:
        try:
            self.version_manager.add_version_from_jar(jar_path, ver, vtype)
            # 读清单（约 120 ms）可以留在工作线程；碰控件必须回 GUI 线程。
            # 原来这里直接调 refresh_versions()，那条路最后会走到
            # root.after —— 从工作线程碰 tk 是未定义行为。
            versions_now = self.version_manager.get_versions()
            self.run_on_gui(
                lambda: self._apply_versions(versions_now)
            )
            self.run_on_gui(refresh_cb)
            self.version_manager.garbage_collect(self.stop_event)
            self.set_status(
                t("versions.imported", type=vtype, ver=ver)
            )
        except Exception as e:
            logger.error(f"导入版本失败: {e}")
            self.set_status(t("versions.import_failed", err=e))
            self.run_on_gui(
                lambda e=e: messagebox.showerror(
                    t("versions.import_failed_title"), str(e)
                )
            )
