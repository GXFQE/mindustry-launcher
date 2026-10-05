# -*- coding: utf-8 -*-
"""存档分类界面：切换、分类管理窗口、删除与复制数据。"""
import logging
import os
import shutil
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .config import (
    default_data_dir,
    delete_path,
    dir_summary,
    roaming_dir,
    sanitize_profile_name,
)
from .i18n import t

logger = logging.getLogger(__name__)


class ProfilesMixin:
    def _refresh_profile_widgets(self) -> None:
        names = self.config.get_profile_names()
        current = self.config.get_current_profile()
        self.profile_combo.config(values=names)
        self.profile_var.set(current)
        self.profile_path_var.set(
            f"📁 {self.config.get_current_save_path()}"
        )
        # ★ 标题也得走 t()：换语言时 rebuild_for_language() 会调到这里，标题
        #   顺手就跟着变了。硬写中文的话，界面切成英文之后标题栏会一直是中文
        #   （标题不在那两页里，重建时没人管它）。
        self.root.title(t("app.title_profile", name=current))

    def _on_profile_selected(self, event: object = None) -> None:
        name = self.profile_var.get()
        if name == self.config.get_current_profile():
            return
        if self._game_is_running():
            messagebox.showinfo(t("common.tip"), t("profiles.busy_switch"))
            self._refresh_profile_widgets()
            return
        self.switch_profile(name)

    def switch_profile(self, name: str) -> None:
        try:
            self.config.set_current_profile(name)
        except KeyError:
            messagebox.showerror(
                t("common.error"), t("profiles.not_exist", name=name)
            )
            self._refresh_profile_widgets()
            return
        self.backup_manager = self._make_backup_manager()
        self._refresh_profile_widgets()
        self.set_status(
            t(
                "profiles.switched",
                name=name,
                path=self.config.get_current_save_path(),
            )
        )

    def open_current_save_dir(self) -> None:
        path = Path(self.config.get_current_save_path())
        if not path.exists():
            if not messagebox.askyesno(
                t("profiles.dir_missing_title"),
                t("profiles.data_dir_missing", path=path),
            ):
                return
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror(
                    t("common.error"), t("profiles.mkdir_failed", err=e)
                )
                return
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror(
                t("common.error"), t("profiles.open_dir_failed", err=e)
            )

    def manage_profiles(self) -> None:
        if self._game_is_running():
            messagebox.showinfo(t("common.tip"), t("profiles.busy_manage"))
            return
        win = tk.Toplevel(self.root)
        win.title(t("profiles.title"))
        win.transient(self.root)
        win.grab_set()
        ttk.Label(
            win,
            text=t("profiles.intro"),
            font=("Microsoft YaHei", 8),
            foreground="#555555",
            anchor=tk.W,
        ).pack(fill=tk.X, padx=10, pady=(8, 0))
        list_frame = ttk.Frame(win)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 0))
        listbox = tk.Listbox(list_frame, font=self.font, activestyle="dotbox")
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(list_frame, command=listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        listbox.config(yscrollcommand=scrollbar.set)
        detail_var = tk.StringVar()
        ttk.Label(
            win,
            textvariable=detail_var,
            font=("Microsoft YaHei", 8),
            foreground="#333333",
            anchor=tk.W,
            wraplength=620,
            justify=tk.LEFT,
        ).pack(fill=tk.X, padx=10, pady=(6, 0))
        names: list[str] = []

        def backup_count(name: str) -> int:
            d = self.backup_base / name / "manifests"
            return len(list(d.glob("backup_*.json"))) if d.is_dir() else 0

        def refresh(select: str | None = None) -> None:
            nonlocal names
            names = self.config.get_profile_names()
            current = self.config.get_current_profile()
            listbox.delete(0, tk.END)
            for n in names:
                star = "★ " if n == current else "   "
                listbox.insert(
                    tk.END,
                    t(
                        "profiles.item_line",
                        star=star,
                        name=n,
                        count=backup_count(n),
                    ),
                )
            if select in names:
                idx = names.index(select)
                listbox.selection_clear(0, tk.END)
                listbox.selection_set(idx)
                listbox.see(idx)
            elif names:
                listbox.selection_set(0)
            show_detail()

        def selected_name() -> str | None:
            sel = listbox.curselection()
            return names[sel[0]] if sel else None

        def show_detail(event: object = None) -> None:
            name = selected_name()
            if not name:
                detail_var.set("")
                return
            tag = (
                t("profiles.tag_current")
                if name == self.config.get_current_profile()
                else t("profiles.tag_inactive")
            )
            data_dir = Path(self.config.get_save_path(name))
            exists = (
                t("profiles.dir_exists")
                if data_dir.is_dir()
                else t("profiles.dir_absent")
            )
            auto = (
                t("profiles.auto_on")
                if self.config.get_profile_setting(name, "auto_backup")
                else t("profiles.auto_off")
            )
            detail_var.set(
                t(
                    "profiles.detail",
                    name=name,
                    tag=tag,
                    exists=exists,
                    data_dir=data_dir,
                    backup_dir=self.backup_base / name,
                    count=backup_count(name),
                    min_playtime=self.config.get_profile_setting(
                        name, "min_playtime"
                    ),
                    max_backups=self.config.get_profile_setting(
                        name, "max_backups"
                    ),
                    auto=auto,
                )
            )

        listbox.bind("<<ListboxSelect>>", show_detail)
        refresh()

        def do_use() -> None:
            name = selected_name()
            if not name:
                messagebox.showwarning(
                    t("common.warning"), t("profiles.need_select"), parent=win
                )
                return
            self.switch_profile(name)
            refresh(name)
            detail_var.set(detail_var.get() + t("profiles.set_current"))

        def do_new() -> None:
            with self._release_grab(win):
                raw = simpledialog.askstring(
                    t("profiles.new_title"),
                    t("profiles.new_prompt"),
                    parent=win,
                )
            if raw is None:
                return
            try:
                name = sanitize_profile_name(raw)
            except ValueError as e:
                messagebox.showerror(t("common.error"), str(e), parent=win)
                return
            if name in self.config.get_profiles():
                messagebox.showerror(
                    t("common.error"), t("profiles.exists", name=name), parent=win
                )
                return

            # 默认数据目录放在 C 盘的 %APPDATA%\Mindustry_Profiles\<分类名>
            suggested = default_data_dir(name)
            init_dir = suggested.parent
            if not init_dir.is_dir():
                init_dir = roaming_dir()
            with self._release_grab(win):
                picked = filedialog.askdirectory(
                    title=t("profiles.pick_data_dir", name=name),
                    initialdir=str(init_dir),
                    parent=win,
                )
            target = Path(picked) if picked else suggested
            try:
                target.mkdir(parents=True, exist_ok=True)
                self.config.add_profile(name, str(target))
            except Exception as e:
                logger.error(f"新建存档分类失败: {e}")
                messagebox.showerror(
                    t("common.error"), t("profiles.create_failed", err=e), parent=win
                )
                return
            refresh(name)
            self.switch_profile(name)
            refresh(name)

            # 可选：从其它分类完整复制一份数据过去
            sources = [
                n
                for n in self.config.get_profile_names()
                if n != name and Path(self.config.get_save_path(n)).is_dir()
            ]
            if sources:
                src_name = self._choose_profile_dialog(
                    win,
                    t("profiles.copy_pick_title"),
                    t("profiles.copy_pick_prompt", name=name),
                    sources,
                    detail=t("profiles.copy_target", target=target),
                )
                if src_name:
                    src = Path(self.config.get_save_path(src_name))
                    count, err = self._copy_profile_data(src, target)
                    logger.info(
                        f"新分类「{name}」完全复制自「{src_name}」，"
                        f"共 {count} 项"
                    )
                    if err:
                        messagebox.showwarning(
                            t("profiles.copy_partial_title"),
                            t(
                                "profiles.copy_partial_body",
                                count=count,
                                err=err,
                            ),
                            parent=win,
                        )
                    else:
                        messagebox.showinfo(
                            t("profiles.copy_done_title"),
                            t(
                                "profiles.copy_done_body",
                                src_name=src_name,
                                count=count,
                                name=name,
                            ),
                            parent=win,
                        )
                    refresh(name)
            self.set_status(t("profiles.created", name=name))

        def do_rename() -> None:
            old = selected_name()
            if not old:
                messagebox.showwarning(
                    t("common.warning"), t("profiles.need_select"), parent=win
                )
                return
            with self._release_grab(win):
                raw = simpledialog.askstring(
                    t("profiles.rename_title"),
                    t("profiles.rename_prompt"),
                    initialvalue=old,
                    parent=win,
                )
            if raw is None:
                return
            try:
                new = sanitize_profile_name(raw)
            except ValueError as e:
                messagebox.showerror(t("common.error"), str(e), parent=win)
                return
            if new == old:
                return
            if new in self.config.get_profiles():
                messagebox.showerror(
                    t("common.error"), t("profiles.exists", name=new), parent=win
                )
                return
            old_bk = self.backup_base / old
            new_bk = self.backup_base / new
            msg = t("profiles.rename_msg_head", old=old, new=new)
            if old_bk.exists():
                if new_bk.exists():
                    messagebox.showerror(
                        t("common.error"),
                        t("profiles.rename_bk_exists", new_bk=new_bk),
                        parent=win,
                    )
                    return
                msg += t(
                    "profiles.rename_bk_move", old_bk=old_bk, new_bk=new_bk
                )
            msg += t(
                "profiles.rename_msg_tail",
                old_path=self.config.get_save_path(old),
            )
            if not messagebox.askyesno(
                t("profiles.rename_confirm_title"), msg, parent=win
            ):
                return
            try:
                if old_bk.exists():
                    shutil.move(str(old_bk), str(new_bk))
                self.config.rename_profile(old, new)
            except Exception as e:
                logger.error(f"重命名分类失败: {e}")
                messagebox.showerror(
                    t("common.error"), t("profiles.rename_failed", err=e), parent=win
                )
                return
            self.backup_manager = self._make_backup_manager()
            self._refresh_profile_widgets()
            refresh(new)

        def do_change_path() -> None:
            name = selected_name()
            if not name:
                messagebox.showwarning(
                    t("common.warning"), t("profiles.need_select"), parent=win
                )
                return
            old_path = self.config.get_save_path(name)
            with self._release_grab(win):
                picked = filedialog.askdirectory(
                    title=t("profiles.change_path_pick", name=name),
                    initialdir=(
                        old_path
                        if Path(old_path).is_dir()
                        else str(self.base_dir)
                    ),
                    parent=win,
                )
            if not picked:
                return
            if Path(picked).resolve() == Path(old_path).resolve():
                return
            if not messagebox.askyesno(
                t("profiles.change_path_title"),
                t(
                    "profiles.change_path_body",
                    name=name,
                    picked=picked,
                    old_path=old_path,
                ),
                parent=win,
            ):
                return
            try:
                self.config.set_profile_path(name, picked)
            except Exception as e:
                messagebox.showerror(
                    t("common.error"),
                    t("profiles.change_path_failed", err=e),
                    parent=win,
                )
                return
            self._refresh_profile_widgets()
            refresh(name)

        def do_open() -> None:
            name = selected_name()
            if not name:
                messagebox.showwarning(
                    t("common.warning"), t("profiles.need_select"), parent=win
                )
                return
            path = Path(self.config.get_save_path(name))
            if not path.exists():
                if not messagebox.askyesno(
                    t("profiles.dir_missing_title"),
                    t("profiles.dir_missing_open", path=path),
                    parent=win,
                ):
                    return
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    messagebox.showerror(
                        t("common.error"),
                        t("profiles.mkdir_failed", err=e),
                        parent=win,
                    )
                    return
            try:
                os.startfile(path)
            except OSError as e:
                messagebox.showerror(
                    t("common.error"),
                    t("profiles.open_dir_failed", err=e),
                    parent=win,
                )

        def do_delete() -> None:
            """统一的删除入口：勾选要处理的内容，一次看清删什么。"""
            name = selected_name()
            if not name:
                messagebox.showwarning(
                    t("profiles.delete_no_select_title"),
                    t("profiles.delete_no_select_body"),
                    parent=win,
                )
                return
            if len(self.config.get_profile_names()) <= 1:
                messagebox.showinfo(
                    t("profiles.delete_last_title"),
                    t("profiles.delete_last_body", name=name),
                    parent=win,
                )
                return
            if self._game_is_running():
                messagebox.showinfo(
                    t("profiles.delete_running_title"),
                    t("profiles.delete_running_body"),
                    parent=win,
                )
                return

            data_dir = Path(self.config.get_save_path(name))
            bk_dir = self.backup_base / name
            n_backups = backup_count(name)
            data_exists = data_dir.is_dir()
            native = (roaming_dir() / "Mindustry").resolve()
            is_native = data_exists and data_dir.resolve() == native
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            trash_dst = self.backup_base / "_trash" / f"{name}_{stamp}"
            # 删除方式由设置里的 permanent_delete 决定（默认 False = 回收站）。
            # 这一页的**所有文案都得跟着它走** —— 「勾了直接删除、却看到
            # 『会移入回收站』的说明」比没有说明更糟。
            permanent = bool(self.config.get("permanent_delete"))
            data_dst = (
                t("profiles.dst_permanent")
                if permanent
                else t("profiles.dst_recycle")
            )
            # 确认框里要接在「即将把下面这个目录……」后面，得自带动词
            data_confirm = (
                t("profiles.dst_permanent")
                if permanent
                else t("profiles.confirm_recycle")
            )

            dlg = tk.Toplevel(win)
            dlg.title(t("profiles.delete_title"))
            ttk.Label(
                dlg,
                text=t("profiles.delete_heading", name=name),
                font=("Microsoft YaHei", 12, "bold"),
            ).pack(anchor=tk.W, padx=14, pady=(14, 4))
            ttk.Label(
                dlg,
                text=t("profiles.delete_hint"),
                font=("Microsoft YaHei", 8),
                foreground="#555555",
            ).pack(anchor=tk.W, padx=14, pady=(0, 6))

            # 按钮和说明先按 BOTTOM 排好，保证内容再长也不会把按钮挤没了
            btns = ttk.Frame(dlg)
            btns.pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=(10, 14))
            note = (
                t("profiles.delete_note_permanent")
                if permanent
                else t("profiles.delete_note_recycle")
            )
            ttk.Label(
                dlg,
                text=note,
                font=("Microsoft YaHei", 8),
                foreground="#555555",
                wraplength=600,
                justify=tk.LEFT,
            ).pack(side=tk.BOTTOM, anchor=tk.W, padx=14, pady=(12, 0))

            body = ttk.Frame(dlg)
            body.pack(fill=tk.BOTH, expand=True, padx=14)
            body.columnconfigure(0, weight=1)

            v_cfg = tk.BooleanVar(value=True)
            v_bk = tk.BooleanVar(value=n_backups > 0)
            v_data = tk.BooleanVar(value=data_exists and not is_native)
            row_no = 0

            def add_item(
                var: tk.BooleanVar,
                title: str,
                detail: str,
                enabled: bool = True,
            ) -> None:
                nonlocal row_no
                cb = ttk.Checkbutton(body, text=title, variable=var)
                cb.grid(row=row_no, column=0, sticky=tk.W, pady=(10, 0))
                if not enabled:
                    cb.state(["disabled"])
                ttk.Label(
                    body,
                    text=detail,
                    font=("Microsoft YaHei", 8),
                    foreground="#555555",
                    justify=tk.LEFT,
                    wraplength=580,
                ).grid(row=row_no + 1, column=0, sticky=tk.W, padx=24)
                row_no += 2

            add_item(
                v_cfg,
                t("profiles.item_cfg_title"),
                t("profiles.item_cfg_detail", name=name),
            )
            add_item(
                v_bk,
                t("profiles.item_bk_title", count=n_backups),
                t(
                    "profiles.item_bk_detail",
                    bk_dir=bk_dir,
                    trash_dst=trash_dst,
                ),
                enabled=n_backups > 0,
            )
            if data_exists:
                extra = (
                    t("profiles.item_data_native_warn") if is_native else ""
                )
                add_item(
                    v_data,
                    t("profiles.item_data_title"),
                    t(
                        "profiles.item_data_detail",
                        data_dir=data_dir,
                        summary=dir_summary(data_dir),
                        data_dst=data_dst,
                        extra=extra,
                    ),
                )
            else:
                add_item(
                    v_data,
                    t("profiles.item_data_missing_title"),
                    t("profiles.item_data_missing_detail", data_dir=data_dir),
                    enabled=False,
                )

            def do_execute() -> None:
                use_cfg = v_cfg.get()
                use_bk = v_bk.get()
                use_data = v_data.get()
                if not (use_cfg or use_bk or use_data):
                    messagebox.showinfo(
                        t("profiles.nothing_checked_title"),
                        t("profiles.nothing_checked_body"),
                        parent=dlg,
                    )
                    return
                if use_cfg and not use_data and data_exists:
                    if not messagebox.askyesno(
                        t("profiles.orphan_title"),
                        t("profiles.orphan_body", data_dir=data_dir),
                        parent=dlg,
                        icon="warning",
                    ):
                        return
                if use_data:
                    if is_native and not messagebox.askyesno(
                        t("profiles.danger_title"),
                        t(
                            "profiles.danger_body",
                            name=name,
                            data_dir=data_dir,
                        ),
                        parent=dlg,
                        icon="warning",
                    ):
                        return
                    if not messagebox.askyesno(
                        t("profiles.final_title"),
                        t(
                            "profiles.final_body",
                            data_confirm=data_confirm,
                            data_dir=data_dir,
                            summary=dir_summary(data_dir),
                        ),
                        parent=dlg,
                        icon="warning",
                    ):
                        return

                dlg.destroy()
                done: list[str] = []
                failed: list[str] = []
                if use_bk and bk_dir.exists():
                    try:
                        trash_dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(bk_dir), str(trash_dst))
                        done.append(t("profiles.done_bk", trash_dst=trash_dst))
                    except Exception as e:
                        logger.error(f"移动备份目录失败: {e}")
                        failed.append(t("profiles.failed_bk", err=e))
                if use_data and data_exists:
                    try:
                        delete_path(data_dir, permanent=permanent)
                        done.append(t("profiles.done_data", data_dst=data_dst))
                    except Exception as e:
                        logger.error(f"删除数据目录失败: {e}")
                        failed.append(t("profiles.failed_data", err=e))
                if use_cfg:
                    try:
                        self.config.remove_profile(name)
                        done.append(t("profiles.done_cfg"))
                    except Exception as e:
                        logger.error(f"移除分类配置失败: {e}")
                        failed.append(t("profiles.failed_cfg", err=e))

                self.backup_manager = self._make_backup_manager()
                self._refresh_profile_widgets()
                refresh()
                show_detail()

                if failed:
                    logger.error(f"删除分类「{name}」存在失败项: {failed}")
                    messagebox.showwarning(
                        t("profiles.partial_title"),
                        t("profiles.partial_done_head")
                        + ("\n  ".join(done) if done else t("profiles.partial_none"))
                        + t("profiles.partial_fail_head")
                        + "\n  ".join(failed),
                        parent=win,
                    )
                    self.set_status(
                        t("profiles.delete_failed_status", name=name)
                    )
                else:
                    logger.info(f"删除分类「{name}」完成: {done}")
                    messagebox.showinfo(
                        t("profiles.done_title"),
                        t("profiles.done_body", name=name)
                        + "\n  ".join(done),
                        parent=win,
                    )
                    self.set_status(t("profiles.deleted_status", name=name))

            ttk.Button(
                btns, text=t("profiles.btn_execute"), command=do_execute
            ).pack(side=tk.LEFT, padx=4)
            ttk.Button(btns, text=t("common.cancel"), command=dlg.destroy).pack(
                side=tk.LEFT, padx=4
            )
            dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
            self._fit_dialog(dlg, base_width=660, min_height=380)
            restore = self._begin_modal(dlg, win)
            dlg.wait_window()
            restore()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=10, pady=(4, 0))
        ttk.Button(
            btn_frame, text=t("profiles.btn_use"), command=do_use
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            btn_frame, text=t("profiles.btn_new"), command=do_new
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            btn_frame, text=t("profiles.btn_rename"), command=do_rename
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            btn_frame, text=t("profiles.btn_change_path"), command=do_change_path
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            btn_frame, text=t("profiles.btn_open"), command=do_open
        ).pack(side=tk.LEFT, padx=4)
        btn_frame2 = ttk.Frame(win)
        btn_frame2.pack(fill=tk.X, padx=10, pady=(6, 10))
        ttk.Button(
            btn_frame2, text=t("profiles.btn_delete"), command=do_delete
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame2, text=t("common.close"), command=win.destroy).pack(
            side=tk.RIGHT, padx=4
        )
        self._fit_dialog(win, base_width=700, min_height=520)
