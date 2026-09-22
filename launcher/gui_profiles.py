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
        self.root.title(f"Mindustry 启动器 - 存档「{current}」")

    def _on_profile_selected(self, event: object = None) -> None:
        name = self.profile_var.get()
        if name == self.config.get_current_profile():
            return
        if self._game_is_running():
            messagebox.showinfo("提示", "游戏运行中无法切换存档分类")
            self._refresh_profile_widgets()
            return
        self.switch_profile(name)

    def switch_profile(self, name: str) -> None:
        try:
            self.config.set_current_profile(name)
        except KeyError:
            messagebox.showerror("错误", f"存档分类「{name}」不存在")
            self._refresh_profile_widgets()
            return
        self.backup_manager = self._make_backup_manager()
        self._refresh_profile_widgets()
        self.set_status(
            f"✅ 已切换到存档分类「{name}」 "
            f"({self.config.get_current_save_path()})"
        )

    def open_current_save_dir(self) -> None:
        path = Path(self.config.get_current_save_path())
        if not path.exists():
            if not messagebox.askyesno(
                "目录不存在",
                f"数据目录还不存在：\n{path}\n\n是否创建并打开？",
            ):
                return
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror("错误", f"创建目录失败: {e}")
                return
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror("错误", f"打开目录失败: {e}")

    def manage_profiles(self) -> None:
        if self._game_is_running():
            messagebox.showinfo("提示", "游戏运行中无法管理存档分类")
            return
        win = tk.Toplevel(self.root)
        win.title("管理存档分类")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(
            win,
            text="每个分类有各自独立的游戏数据目录、备份和备份策略；"
            "启动前切换即可。★ 表示当前正在使用的分类。",
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
                    tk.END, f"{star}{n}    （{backup_count(n)} 个备份）"
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
                "当前分类"
                if name == self.config.get_current_profile()
                else "未启用"
            )
            data_dir = Path(self.config.get_save_path(name))
            exists = "[存在]" if data_dir.is_dir() else "[目录不存在]"
            detail_var.set(
                f"【{name}】{tag}   {exists}\n"
                f"数据目录：{data_dir}\n"
                f"备份目录：{self.backup_base / name}\n"
                f"备份数量：{backup_count(name)}    "
                f"策略：{self.config.get_profile_setting(name, 'min_playtime')}"
                f" 分钟 / 上限 "
                f"{self.config.get_profile_setting(name, 'max_backups')} 个"
                f" / 自动备份 "
                f"{'开' if self.config.get_profile_setting(name, 'auto_backup') else '关'}"
            )

        listbox.bind("<<ListboxSelect>>", show_detail)
        refresh()

        def do_use() -> None:
            name = selected_name()
            if not name:
                messagebox.showwarning("警告", "请先选择一个存档分类", parent=win)
                return
            self.switch_profile(name)
            refresh(name)
            detail_var.set(detail_var.get() + "\n\n✅ 已设为当前分类")

        def do_new() -> None:
            with self._release_grab(win):
                raw = simpledialog.askstring(
                    "新建存档分类",
                    "分类名称（例如：A 服、生存存档）:",
                    parent=win,
                )
            if raw is None:
                return
            try:
                name = sanitize_profile_name(raw)
            except ValueError as e:
                messagebox.showerror("错误", str(e), parent=win)
                return
            if name in self.config.get_profiles():
                messagebox.showerror(
                    "错误", f"存档分类「{name}」已存在", parent=win
                )
                return

            # 默认数据目录放在 C 盘的 %APPDATA%\Mindustry_Profiles\<分类名>
            suggested = default_data_dir(name)
            init_dir = suggested.parent
            if not init_dir.is_dir():
                init_dir = roaming_dir()
            with self._release_grab(win):
                picked = filedialog.askdirectory(
                    title=f"选择「{name}」的游戏数据目录（取消则用默认）",
                    initialdir=str(init_dir),
                    parent=win,
                )
            target = Path(picked) if picked else suggested
            try:
                target.mkdir(parents=True, exist_ok=True)
                self.config.add_profile(name, str(target))
            except Exception as e:
                logger.error(f"新建存档分类失败: {e}")
                messagebox.showerror("错误", f"新建失败: {e}", parent=win)
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
                    "完全复制某个存档的数据",
                    f"要把哪个存档的数据完整复制到「{name}」？\n"
                    f"会复制该分类数据目录下的全部内容"
                    f"（saves / mods / schematics / 各项设置等），"
                    f"同名文件会被覆盖。\n"
                    f"不复制就直接点「跳过」。",
                    sources,
                    detail=f"目标目录：\n{target}",
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
                            "部分内容复制失败",
                            f"已复制 {count} 项，但有失败：\n\n{err}",
                            parent=win,
                        )
                    else:
                        messagebox.showinfo(
                            "复制完成",
                            f"已从「{src_name}」完整复制 {count} 项到"
                            f"「{name}」。",
                            parent=win,
                        )
                    refresh(name)
            self.set_status(f"✅ 已新建并切换到存档分类「{name}」")

        def do_rename() -> None:
            old = selected_name()
            if not old:
                messagebox.showwarning("警告", "请先选择一个存档分类", parent=win)
                return
            with self._release_grab(win):
                raw = simpledialog.askstring(
                    "重命名存档分类",
                    "新的分类名称:",
                    initialvalue=old,
                    parent=win,
                )
            if raw is None:
                return
            try:
                new = sanitize_profile_name(raw)
            except ValueError as e:
                messagebox.showerror("错误", str(e), parent=win)
                return
            if new == old:
                return
            if new in self.config.get_profiles():
                messagebox.showerror(
                    "错误", f"存档分类「{new}」已存在", parent=win
                )
                return
            old_bk = self.backup_base / old
            new_bk = self.backup_base / new
            msg = f"将分类「{old}」重命名为「{new}」。\n\n"
            if old_bk.exists():
                if new_bk.exists():
                    messagebox.showerror(
                        "错误",
                        f"目标备份目录已存在：\n{new_bk}\n请换一个名字。",
                        parent=win,
                    )
                    return
                msg += f"备份目录一并改名：\n  {old_bk}\n  → {new_bk}\n\n"
            msg += (
                f"游戏数据目录不会被移动：\n"
                f"  {self.config.get_save_path(old)}\n\n继续吗？"
            )
            if not messagebox.askyesno("确认重命名", msg, parent=win):
                return
            try:
                if old_bk.exists():
                    shutil.move(str(old_bk), str(new_bk))
                self.config.rename_profile(old, new)
            except Exception as e:
                logger.error(f"重命名分类失败: {e}")
                messagebox.showerror("错误", f"重命名失败: {e}", parent=win)
                return
            self.backup_manager = self._make_backup_manager()
            self._refresh_profile_widgets()
            refresh(new)

        def do_change_path() -> None:
            name = selected_name()
            if not name:
                messagebox.showwarning("警告", "请先选择一个存档分类", parent=win)
                return
            old_path = self.config.get_save_path(name)
            with self._release_grab(win):
                picked = filedialog.askdirectory(
                    title=f"为「{name}」选择新的游戏数据目录",
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
                "确认修改",
                f"把「{name}」的数据目录改为：\n  {picked}\n\n"
                f"原目录：\n  {old_path}\n\n"
                f"原目录的内容不会被移动或删除，需要你自行迁移。\n继续吗？",
                parent=win,
            ):
                return
            try:
                self.config.set_profile_path(name, picked)
            except Exception as e:
                messagebox.showerror("错误", f"修改失败: {e}", parent=win)
                return
            self._refresh_profile_widgets()
            refresh(name)

        def do_open() -> None:
            name = selected_name()
            if not name:
                messagebox.showwarning("警告", "请先选择一个存档分类", parent=win)
                return
            path = Path(self.config.get_save_path(name))
            if not path.exists():
                if not messagebox.askyesno(
                    "目录不存在",
                    f"{path}\n\n该目录尚不存在，是否创建后打开？",
                    parent=win,
                ):
                    return
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    messagebox.showerror("错误", f"创建目录失败: {e}", parent=win)
                    return
            try:
                os.startfile(path)
            except OSError as e:
                messagebox.showerror("错误", f"打开目录失败: {e}", parent=win)

        def do_delete() -> None:
            """统一的删除入口：勾选要处理的内容，一次看清删什么。"""
            name = selected_name()
            if not name:
                messagebox.showwarning(
                    "还没选中",
                    "请先在列表里点一下要删除的存档分类。",
                    parent=win,
                )
                return
            if len(self.config.get_profile_names()) <= 1:
                messagebox.showinfo(
                    "不能删除",
                    f"「{name}」是目前唯一的存档分类，删掉启动器就没得用了。\n\n"
                    "如果只是想清空它的游戏数据，可以在下面的对话框里"
                    "只勾「游戏数据目录」。",
                    parent=win,
                )
                return
            if self._game_is_running():
                messagebox.showinfo(
                    "游戏正在运行",
                    "请先关闭游戏，再删除存档分类。",
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
                "直接彻底删除（不进回收站，删了找不回来）"
                if permanent
                else "Windows 回收站（通常可右键「还原」找回）"
            )
            # 确认框里要接在「即将把下面这个目录……」后面，得自带动词
            data_confirm = (
                "直接彻底删除（不进回收站，删了找不回来）"
                if permanent
                else "移入 Windows 回收站"
            )

            dlg = tk.Toplevel(win)
            dlg.title("删除存档分类")
            ttk.Label(
                dlg,
                text=f"即将处理存档分类「{name}」",
                font=("Microsoft YaHei", 12, "bold"),
            ).pack(anchor=tk.W, padx=14, pady=(14, 4))
            ttk.Label(
                dlg,
                text="勾选下面要一并处理的内容。全部勾选 = 把这个分类彻底删干净。",
                font=("Microsoft YaHei", 8),
                foreground="#555555",
            ).pack(anchor=tk.W, padx=14, pady=(0, 6))

            # 按钮和说明先按 BOTTOM 排好，保证内容再长也不会把按钮挤没了
            btns = ttk.Frame(dlg)
            btns.pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=(10, 14))
            note = (
                "说明：数据目录会被直接彻底删除，不进回收站、无法还原"
                "（设置里开着「删除文件时直接彻底删除」）。\n"
                "备份记录移到 _trash 目录后不会被自动清理。"
                if permanent
                else "说明：数据目录会移入 Windows 回收站，通常可以右键「还原」"
                "找回；\n但网络盘 / 非 NTFS 卷上没有回收站，系统这时会提示"
                "「将永久删除」。\n备份记录移到 _trash 目录后不会被自动清理。"
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
                "分类配置",
                f"从 config.json 里移除「{name}」，"
                f"启动器的存档列表里不再出现它。",
            )
            add_item(
                v_bk,
                f"备份记录（{n_backups} 条）",
                f"{bk_dir}\n"
                f"      →  {trash_dst}\n"
                f"      移到回收目录，不会自动清理，需要时手动搬回来。",
                enabled=n_backups > 0,
            )
            if data_exists:
                extra = (
                    "   ⚠️ 这是游戏原生数据目录，删了主力存档就没了！"
                    if is_native
                    else ""
                )
                add_item(
                    v_data,
                    "游戏数据目录",
                    f"{data_dir}\n"
                    f"      [{dir_summary(data_dir)}]\n"
                    f"      →  {data_dst}{extra}",
                )
            else:
                add_item(
                    v_data,
                    "游戏数据目录（当前不存在）",
                    f"{data_dir}\n      这个目录不存在，无需处理。",
                    enabled=False,
                )

            def do_execute() -> None:
                use_cfg = v_cfg.get()
                use_bk = v_bk.get()
                use_data = v_data.get()
                if not (use_cfg or use_bk or use_data):
                    messagebox.showinfo(
                        "什么都没勾",
                        "没有勾选任何内容，未做任何操作。",
                        parent=dlg,
                    )
                    return
                if use_cfg and not use_data and data_exists:
                    if not messagebox.askyesno(
                        "会留下孤立目录",
                        f"你勾了「分类配置」，但没勾「游戏数据目录」。\n\n"
                        f"删掉分类后，下面这个目录会留在磁盘上没人管：\n"
                        f"{data_dir}\n\n确定继续吗？",
                        parent=dlg,
                        icon="warning",
                    ):
                        return
                if use_data:
                    if is_native and not messagebox.askyesno(
                        "⚠️ 高危操作",
                        f"「{name}」的数据目录是 Mindustry 原生目录：\n"
                        f"{data_dir}\n\n"
                        f"把它删掉（即使进回收站）游戏就读不到存档了。\n\n"
                        f"确定要一起删除吗？",
                        parent=dlg,
                        icon="warning",
                    ):
                        return
                    if not messagebox.askyesno(
                        "最后确认",
                        f"即将把下面这个目录{data_confirm}：\n\n"
                        f"{data_dir}\n[{dir_summary(data_dir)}]\n\n"
                        f"确认执行吗？",
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
                        done.append(f"备份记录 → {trash_dst}")
                    except Exception as e:
                        logger.error(f"移动备份目录失败: {e}")
                        failed.append(f"备份记录：{e}")
                if use_data and data_exists:
                    try:
                        delete_path(data_dir, permanent=permanent)
                        done.append(f"游戏数据目录 → {data_dst}")
                    except Exception as e:
                        logger.error(f"删除数据目录失败: {e}")
                        failed.append(f"游戏数据目录：{e}")
                if use_cfg:
                    try:
                        self.config.remove_profile(name)
                        done.append("分类配置 → 已从 config.json 移除")
                    except Exception as e:
                        logger.error(f"移除分类配置失败: {e}")
                        failed.append(f"分类配置：{e}")

                self.backup_manager = self._make_backup_manager()
                self._refresh_profile_widgets()
                refresh()
                show_detail()

                if failed:
                    logger.error(f"删除分类「{name}」存在失败项: {failed}")
                    messagebox.showwarning(
                        "部分操作失败",
                        "已完成：\n  "
                        + ("\n  ".join(done) if done else "无")
                        + "\n\n失败：\n  "
                        + "\n  ".join(failed),
                        parent=win,
                    )
                    self.set_status(f"⚠️ 删除「{name}」时有失败项")
                else:
                    logger.info(f"删除分类「{name}」完成: {done}")
                    messagebox.showinfo(
                        "处理完成",
                        f"存档分类「{name}」：\n\n  "
                        + "\n  ".join(done),
                        parent=win,
                    )
                    self.set_status(f"✅ 已删除存档分类「{name}」")

            ttk.Button(
                btns, text="执行删除", command=do_execute
            ).pack(side=tk.LEFT, padx=4)
            ttk.Button(btns, text="取消", command=dlg.destroy).pack(
                side=tk.LEFT, padx=4
            )
            dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
            self._fit_dialog(dlg, base_width=660, min_height=380)
            restore = self._begin_modal(dlg, win)
            dlg.wait_window()
            restore()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=10, pady=(4, 0))
        ttk.Button(btn_frame, text="设为当前存档", command=do_use).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(btn_frame, text="新建分类", command=do_new).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(btn_frame, text="重命名", command=do_rename).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(btn_frame, text="修改路径", command=do_change_path).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(btn_frame, text="打开数据目录", command=do_open).pack(
            side=tk.LEFT, padx=4
        )
        btn_frame2 = ttk.Frame(win)
        btn_frame2.pack(fill=tk.X, padx=10, pady=(6, 10))
        ttk.Button(
            btn_frame2, text="删除此分类…", command=do_delete
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame2, text="关闭", command=win.destroy).pack(
            side=tk.RIGHT, padx=4
        )
        self._fit_dialog(win, base_width=700, min_height=520)
