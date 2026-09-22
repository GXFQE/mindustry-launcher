# -*- coding: utf-8 -*-
"""GUI 冒烟：不开游戏、不碰真实数据，快速验证界面这一层。

和 e2e_launcher.py 的分工：
  * `e2e_launcher.py` 真把游戏拉起来，验「点启动 → 能玩」这条链（几十秒）；
  * 本脚本只建窗口 + 点按钮 + 读控件状态，**不起游戏**（几秒），
    专门盯那些「一改界面就可能坏、但只有真开窗口才发现」的东西：

      - 主窗口建得起来，设置页比主面板高时**底部按钮不会被裁掉**
      - 设置页里**没有标签被裁掉**（默认宽度和最小窗口尺寸各量一遍）：
        灰字提示放不下要折行，且折行宽度不会被越算越窄
      - 新加的校验逻辑（引号没闭合的启动参数要被拦下）
      - 运行日志窗口能开、两个页签在、轮询真的在跑、收到输出会长出来
      - 自定义启动参数能存进 config.json 并重新读出来
      - 关窗口干净

★ 沙箱里**不拷 jre**（32 MB），而是把 config.json 的 `jvm.jre_path` 写成真实
  jre 的**绝对路径** —— 既省时间，又顺带验证了「jre_path 支持绝对路径」。

用法：
    python _tools/verify/gui_smoke.py
    python _tools/verify/gui_smoke.py --keep
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk


def _find_project_root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / "launcher" / "__init__.py").is_file():
            return p
    raise RuntimeError(f"找不到项目根（从 {start} 往上没看到 launcher/__init__.py）")


ROOT = _find_project_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import DATA  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(label)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}"
          + (f"   {detail}" if detail else ""))


def find_widget(root_widget, text: str,
                kinds=(ttk.Button, ttk.Label, ttk.Checkbutton)):
    """按文本找控件（不用改生产代码去存引用）。"""
    for child in root_widget.winfo_children():
        try:
            if isinstance(child, kinds) and child.cget("text") == text:
                return child
        except Exception:                                       # noqa: BLE001
            pass
        found = find_widget(child, text, kinds)
        if found is not None:
            return found
    return None


def find_by_kind(root_widget, kind):
    for child in root_widget.winfo_children():
        if isinstance(child, kind):
            return child
        found = find_by_kind(child, kind)
        if found is not None:
            return found
    return None


def walk_by_kind(root_widget, kinds):
    """递归收集**所有**匹配的控件（find_by_kind 只给第一个）。

    测「左边缘是否对齐」要拿到整组输入框，一个不够。
    """
    for child in root_widget.winfo_children():
        if isinstance(child, kinds):
            yield child
        yield from walk_by_kind(child, kinds)


class FakeMessagebox:
    """把弹窗换成记录，别让冒烟测试卡在「等用户点确定」上。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def _rec(self, kind):
        def inner(title, message, **kw):
            self.calls.append((kind, title, str(message)))
            return True
        return inner

    def __getattr__(self, name):
        return self._rec(name)

    def errors(self) -> list[str]:
        return [m for k, _, m in self.calls if k == "showerror"]

    def messages(self) -> str:
        return "\n".join(f"{k}:{t}:{m}" for k, t, m in self.calls)


# 镜像下拉框的候选清单来自 config.json 的 github_mirror_presets。沙箱里
# 故意换成**跟内置默认不同**的一份：既验证「选项真的从配置读进来」，
# 又给下面 [2c] 的滚轮断言提供值。
SMOKE_MIRROR_PRESETS = [
    "https://smoke-a.example.com/",
    "https://smoke-b.example.com/",
]


def make_sandbox() -> Path:
    root = Path(tempfile.mkdtemp(prefix="mdt_gui_smoke_"))
    jre_abs = (DATA / "jre").resolve()
    if not (jre_abs / "bin" / "java.exe").is_file():
        raise SystemExit(f"找不到真实 jre：{jre_abs}")
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "备用").mkdir(parents=True, exist_ok=True)
    cfg = {
        "hide_on_launch": False,
        # 旧的镜像开关：已废弃，留着验证「保存后不会再被写回配置」
        "use_mirror": True,
        "github_mirror": "",            # 沙箱不下载，直连即可
        # 候选镜像站也是配置项（设置页下拉框读它）
        "github_mirror_presets": list(SMOKE_MIRROR_PRESETS),
        "auto_update": False,           # 别在这里发网络请求
        "extra_vm_args": "",
        "extra_program_args": "",
        "save_game_log": True,
        "max_log_files": 20,
        "current_profile": "默认",
        "profiles": {
            "默认": {
                "data_dir": str(data_dir),
                "min_playtime": 20,
                "max_backups": 20,
                "auto_backup": True,
            },
            # 第二个分类：滚轮不能悄悄把「当前存档」切到它上面（见 [2c]）
            "备用": {
                "data_dir": str(data_dir / "备用"),
                "min_playtime": 20,
                "max_backups": 20,
                "auto_backup": True,
            },
        },
        # ★ 绝对路径：省掉拷 32 MB 的 jre
        "jvm": {
            "jre_path": str(jre_abs),
            "main_class": "mindustry.desktop.Main",
            "vm_args": ["-Dsmoke=1"],
            "program_args": [],
        },
    }
    (root / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "versions" / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "versions" / "objects").mkdir(parents=True, exist_ok=True)
    (root / "backups" / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "backups" / "objects").mkdir(parents=True, exist_ok=True)
    return root


def pump(app, seconds: float) -> None:
    """让 Tk 真的跑一会儿（否则 after 回调永远不触发）。"""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.root.update()
        time.sleep(0.02)


def main() -> int:
    ap = argparse.ArgumentParser(description="GUI 冒烟（不起游戏）")
    ap.add_argument("--keep", action="store_true", help="保留沙箱")
    args = ap.parse_args()

    root = make_sandbox()
    cwd_backup = os.getcwd()
    print("=" * 64)
    print(f"沙箱：{root}")
    print("=" * 64)

    app = None
    try:
        # BASE_DIR 在 import 时就定下了（源码运行 = cwd），所以必须先切目录
        os.chdir(root)
        for mod in [m for m in list(sys.modules) if m.startswith("launcher")]:
            del sys.modules[mod]
        from launcher.gui import MindustryLauncher
        import launcher.gui_main as GM

        fake_mb = FakeMessagebox()
        GM.messagebox = fake_mb

        print("\n[1] 主窗口与设置页布局")
        app = MindustryLauncher()
        app.root.update()
        check("主窗口标题正确",
              app.root.title().startswith("Mindustry 启动器"),
              app.root.title())
        win_h = app.root.winfo_height()
        screen_h = app.root.winfo_screenheight()
        check("窗口高度按主面板算，没有被设置页撑高",
              win_h <= app.main_frame.winfo_reqheight() + 40,
              f"窗口高 {win_h}，主面板需要 {app.main_frame.winfo_reqheight()}")
        # 1280x800 这类小屏上，窗口底不能掉到屏幕外
        bottom = app.root.winfo_rooty() + win_h
        check("窗口底边在屏幕内（不会有一截点不到）", bottom <= screen_h,
              f"窗口底 {bottom} vs 屏幕高 {screen_h}")

        app.show_settings()
        app.root.update()
        # 设置页内容比窗口高，底部按钮钉在窗口底，不能被内容挤走
        bottom_btn = find_widget(app.settings_frame, "保存并返回")
        check("设置页底部按钮找得到", bottom_btn is not None)
        if bottom_btn is not None:
            btn_bottom = bottom_btn.winfo_rooty() + bottom_btn.winfo_height()
            win_bottom = app.root.winfo_rooty() + app.root.winfo_height()
            check("设置页底部按钮完整可见（没被裁掉）",
                  btn_bottom <= win_bottom,
                  f"按钮底 {btn_bottom} vs 窗口底 {win_bottom}")
        # 主操作在右下角、次要操作在左 —— 光看源码 `side=tk.RIGHT` 不算数，
        # 得真建窗口量一下：万一那个 frame 没撑满宽度，RIGHT 也「右」不到哪去。
        reset_btn = find_widget(app.settings_frame, "重置默认")
        check("底部两个按钮都在", reset_btn is not None and bottom_btn is not None)
        if reset_btn is not None and bottom_btn is not None:
            footer = app.settings_footer
            footer_right = footer.winfo_rootx() + footer.winfo_width()
            gap = footer_right - (bottom_btn.winfo_rootx()
                                  + bottom_btn.winfo_width())
            check("「保存并返回」在「重置默认」右边",
                  bottom_btn.winfo_rootx() > reset_btn.winfo_rootx(),
                  f"保存 x={bottom_btn.winfo_rootx()} vs "
                  f"重置 x={reset_btn.winfo_rootx()}")
            check("「保存并返回」贴住底栏右边缘（不是缩在中间）",
                  0 <= gap <= 40,
                  f"距右边缘 {gap}px（底栏宽 {footer.winfo_width()}）")
        # 内容区确实是可滚动的：内容比可视区高时要有滚动范围
        canvas = app.settings_canvas
        bbox = canvas.bbox("all")
        check("设置页内容区是可滚动的容器", bbox is not None,
              str(bbox))
        check("内容高于可视区时滚动范围大于可视高度（真的能滚）",
              bbox is not None and bbox[3] > canvas.winfo_height(),
              f"内容高 {bbox[3] if bbox else '?'} vs 可视 {canvas.winfo_height()}")
        # 新加的控件都在
        check("额外 JVM 参数一栏在",
              find_widget(app.settings_frame, "额外 JVM 参数:") is not None)
        check("额外游戏参数一栏在",
              find_widget(app.settings_frame, "额外游戏参数:") is not None)
        check("游戏输出落盘开关在",
              find_widget(
                  app.settings_frame,
                  "把游戏输出保存到 logs/ 目录（关掉则只在日志窗口里实时显示）",
              ) is not None)
        check("GitHub 镜像一栏在",
              find_widget(app.settings_frame, "GitHub 镜像:") is not None)
        check("日志保留份数一栏在",
              find_widget(app.settings_frame, "日志保留份数:") is not None)
        check("「游戏退出后自动关闭启动器」开关在",
              find_widget(
                  app.settings_frame,
                  "游戏退出后自动关闭启动器（自动备份做完才关）",
              ) is not None)
        check("Java 路径一栏在（JRE / JDK 都收）",
              find_widget(app.settings_frame, "Java 路径(JRE/JDK):") is not None)
        check("「检测」按钮在",
              find_widget(app.settings_frame, "检测") is not None)
        check("「直接删除」开关在",
              find_widget(
                  app.settings_frame,
                  "删除文件时直接彻底删除（不进回收站，不可还原）",
              ) is not None)
        # 沙箱那份配置里没写 permanent_delete（模拟老配置）：默认必须是「关」，
        # 这是个破坏性开关，绝不能自己打开。
        check("老配置里没有 permanent_delete 时默认不勾（默认走回收站）",
              app.permanent_delete_var.get() is False,
              repr(app.permanent_delete_var.get()))
        check("Java 路径回显的是配置里那个（沙箱写的是绝对路径）",
              app.jre_path_var.get() == str((DATA / "jre").resolve()),
              app.jre_path_var.get())
        # 「路径 ui 页面可改动」：这一项必须是**能编辑**的输入框，
        # 不是只读展示（不能改的话，这一栏等于没加）。
        jre_entry = None
        for w in walk_by_kind(app.settings_frame, (ttk.Entry,)):
            if str(w.cget("textvariable")) == str(app.jre_path_var):
                jre_entry = w
        check("Java 路径是能编辑的输入框（不是只读展示）",
              jre_entry is not None
              and str(jre_entry.cget("state")) not in ("readonly", "disabled"),
              str(jre_entry.cget("state")) if jre_entry else "没找到输入框")
        check("参数输入框回显了配置里的值", app.extra_vm_var.get() == "",
              repr(app.extra_vm_var.get()))
        # 沙箱那份配置里**故意没有** close_on_game_exit 这一项（模拟老配置）：
        # 缺了必须默认「不关」，绝不能自己把启动器关掉。
        check("老配置里没有这一项时默认不勾（不会自己把启动器关了）",
              app.close_on_game_exit_var.get() is False,
              repr(app.close_on_game_exit_var.get()))

        # 两组设置的输入框必须左边缘对齐。
        # 以前两帧各自 grid，列宽按各自最长的标签算 —— 存档那组的
        # 「最小备份时间(分钟):」更长，就把它的输入框推得比启动器那组更靠右，
        # 看着就是「没对齐」。现在两帧共用同一个标签列宽（_label_col_width）。
        boxes = list(walk_by_kind(app.settings_frame, (ttk.Entry, ttk.Combobox)))
        xs = [w.winfo_rootx() for w in boxes]
        check("设置页里能收集到这一组输入框", len(boxes) >= 5,
              f"{len(boxes)} 个")
        check("两组设置的输入框左边缘对齐（同一个 x）",
              len(set(xs)) == 1 and min(xs, default=0) > 0,
              f"x = {sorted(set(xs))}")

        def short_rows() -> list[str]:
            """「右边空了一截」的输入框 / 分割线 —— 一眼能看出来的没撑满。

            列宽是全帧共享的：第 2 列被按钮占着，只写 ``column=1`` 的输入框
            右边会平白空出整整一列（量过：183 px）；分割线只盖 ``columnspan=2``
            时同理（178 px）。**唯一该让位的情形是同一行右边真站着按钮**
            （「Java 路径」那一行），所以那种行跳过。
            """
            bad = []
            for fr in walk_by_kind(app.settings_frame, (ttk.LabelFrame,)):
                bx, _, bw, _ = fr.grid_bbox(0, 0, 10, fr.grid_size()[0] + 1)
                right = bx + bw
                cells: dict[int, list[tuple[int, object]]] = {}
                for ch in fr.winfo_children():
                    try:
                        info = ch.grid_info()
                    except tk.TclError:         # 控件正在销毁
                        continue
                    if not info:
                        continue
                    cells.setdefault(int(info["row"]), []).append(
                        (int(info["column"]), ch)
                    )
                for r, items in cells.items():
                    for col, ch in items:
                        if ch.winfo_class() not in (
                            "TSeparator", "TEntry", "TCombobox",
                        ):
                            continue
                        if any(c > col for c, _ in items):
                            continue            # 右边还有按钮，让位是应该的
                        gap = right - (ch.winfo_x() + ch.winfo_width())
                        if gap > 8:
                            bad.append(f"r{r} {ch.winfo_class()} 差 {gap}px")
            return bad

        bad = short_rows()
        check("★ 设置页里的输入框 / 分割线都撑到了右边（不然空一整列）",
              not bad, f"{bad[:4]}")

        # 反向对照：把镜像下拉框缩回单列，守门必须当场报出来 —— 不然这条
        # 断言可能只是「一个控件都没扫到」的空转。
        mirror = next(
            iter(walk_by_kind(app.settings_frame, (ttk.Combobox,))), None
        )
        if mirror is not None:
            mirror.grid_configure(columnspan=1)
            app.root.update()
            caught = short_rows()
            check("[对照] 缩回单列后守门确实会报（断言打到了现场）",
                  bool(caught), f"{caught[:2]}")
            mirror.grid_configure(columnspan=2)
            app.root.update()
            check("还原成跨两列后又不报了", not short_rows())

        def clipped_texts() -> list[str]:
            """设置页里「需要的宽度 > 分到的宽度」的标签 —— 也就是被裁掉尾巴的。

            灰字提示都 grid 在输入框那一列：列宽由窗口分配，标签的自然宽度由
            文字长度决定。文字比列宽长的时候 Tk 既不报错也不换行，**右边就是
            少一截**（用户报过：「填 JRE 或 JDK 的根目录都行…」被切在「启动器
            旁」）。修法是 _auto_wrap_hint —— 放不下就折行。
            """
            bad = []
            for w in walk_by_kind(app.settings_body,
                                  (ttk.Label, ttk.Checkbutton)):
                try:
                    if w.winfo_reqwidth() > w.winfo_width() + 1:
                        bad.append(str(w.cget("text"))[:22])
                except tk.TclError:         # 控件正在销毁，跳过
                    pass
            return bad

        bad = clipped_texts()
        check("设置页里没有被裁掉的标签（宽度不够要折行，不能少一截）",
              not bad, f"被裁 {len(bad)} 个：{bad[:3]}")

        # 窗口拉到**最小尺寸**（root.minsize(720, 540)）也得成立：这是小屏上
        # 的真实宽度，只测默认 780 等于没测窄的那一头。
        app.root.geometry("720x620")
        pump(app, 0.4)
        bad = clipped_texts()
        check("★ 拉到最小窗口尺寸时标签也不被裁（提示会自动折行）",
              not bad, f"被裁 {len(bad)} 个：{bad[:3]}")
        bad = short_rows()
        check("最小窗口下输入框 / 分割线也照样撑到右边", not bad, f"{bad[:4]}")
        # 折行宽度不能「一轮一轮往下缩」：wraplength 按「分到的宽度」算，万一
        # 某条提示没被拉伸到整行，它拿到的就是**折行之后**的宽度，每布局一次
        # 多折 2 像素，最后缩成一列一个字。真要那样，这里的值会小得离谱。
        tiny = [int(w.cget("wraplength") or 0)
                for w in walk_by_kind(app.settings_body, (ttk.Label,))
                if str(w.cget("foreground")).lower() == "#777777"
                and 0 < int(w.cget("wraplength") or 0) < 100]
        check("折行宽度没被越算越窄（缩到几十像素就成一列一个字了）",
              not tiny, f"{tiny}")
        app._fit_root_window()          # 还原成启动时的尺寸，后面的检查照旧
        pump(app, 0.2)

        print("\n[1b] 「保存并返回」：离开设置页就等于保存")
        # 用户原话：「总是点成返回主页面然后发现设置没生效」。
        # 根子在于设置页有两个出口，其中一个不保存 —— 现在只剩一个。
        check("设置页里没有「返回主界面」按钮（否则又能改完不保存就走）",
              find_widget(app.settings_frame, "返回主界面") is None)
        check("设置页里也没有单独的「保存设置」（保存/返回不再分家）",
              find_widget(app.settings_frame, "保存设置") is None)

        app.show_settings()
        app.root.update()
        app.extra_vm_var.set("-Xmx3G")
        fake_mb.calls.clear()
        app.save_and_return()
        app.root.update()
        check("保存并返回：设置真的进了 config.json",
              json.loads((root / "config.json").read_text(encoding="utf-8"))
              .get("extra_vm_args") == "-Xmx3G")
        check("保存并返回：人回到了主界面（设置页收起来了）",
              app.main_frame.winfo_manager() == "pack"
              and app.settings_frame.winfo_manager() != "pack",
              f"main={app.main_frame.winfo_manager()!r} "
              f"settings={app.settings_frame.winfo_manager()!r}")
        check("保存并返回：成功时不弹对话框（少点一次）",
              fake_mb.calls == [], fake_mb.messages())

        # ★ 反向：校验没过时必须**留在设置页**。切回主界面就等于
        #   「点了按钮但什么都没变」—— 跟原来那个坑一模一样。
        app.show_settings()
        app.root.update()
        app.extra_vm_var.set('-Dfoo="没闭合')
        fake_mb.calls.clear()
        app.save_and_return()
        app.root.update()
        check("★ 被校验拦下时留在设置页（不会假装存好了再切走）",
              app.settings_frame.winfo_manager() == "pack"
              and app.main_frame.winfo_manager() != "pack",
              f"main={app.main_frame.winfo_manager()!r} "
              f"settings={app.settings_frame.winfo_manager()!r}")
        check("被拦下时仍然弹错误说明",
              any("闭合" in m for m in fake_mb.errors()), fake_mb.messages())
        app.extra_vm_var.set("")
        app.save_and_return()               # 收拾干净，后面从主界面开始
        app.root.update()

        print("\n[1c] Java 路径：能改、能「跑个 version」验一遍")
        # 用户原话：「要不跑个 version？而且可以让路径 ui 页面可改动」。
        # 这里真的点一下「检测」—— 它会起一次真 JVM 跑 java -version，
        # 所以既是界面测试，也是「这套 Java 真能用」的端到端确认。
        # 收的是 JRE 或 JDK（判定只看 bin\java.exe）。

        def wait_jre_check(seconds: float = 20.0) -> str:
            end = time.perf_counter() + seconds
            while time.perf_counter() < end:
                app.root.update()
                if "正在检测" not in app.jre_check_var.get():
                    return app.jre_check_var.get()
                time.sleep(0.05)
            return app.jre_check_var.get()

        app.show_settings()
        app.root.update()
        real_jre = str((DATA / "jre").resolve())
        app.jre_path_var.set(real_jre)
        fake_mb.calls.clear()
        app.check_jre()
        check("点了「检测」立刻有反馈（正在检测…）",
              "正在检测" in app.jre_check_var.get(), app.jre_check_var.get())
        result = wait_jre_check()
        check("★ 检测真跑了一次 java -version 并报出版本",
              result.startswith("✅") and "Java" in result, result)
        check("检测过程不弹框（后台跑，不打断用户）",
              fake_mb.calls == [], fake_mb.messages())

        # 这一栏下面那句提示以前是**被裁掉**的：文字比列宽长，Tk 不报错也不
        # 换行，右边直接少一截（用户截了图）。修法是 _auto_wrap_hint。这里钉
        # 两头：默认宽度下是完整的一行（别矫枉过正、把能放下的也折了），
        # 窄窗下折行但一个字都不少。
        hint = None
        for w in walk_by_kind(app.settings_body, (ttk.Label,)):
            if str(w.cget("text")).startswith("填 JRE 或 JDK 的根目录"):
                hint = w
        check("Java 路径下面那句提示在（按文本找得到）", hint is not None)
        if hint is not None:
            check("默认宽度下它是完整的一行（没有被多余的折行）",
                  not int(hint.cget("wraplength") or 0)
                  and hint.winfo_height() <= 24,
                  f"wraplength={hint.cget('wraplength')}"
                  f" 高={hint.winfo_height()}")
            app.root.geometry("720x620")
            pump(app, 0.4)
            check("★ 窄到最小窗口时也没被裁（放不下就折行，一个字都不少）",
                  hint.winfo_reqwidth() <= hint.winfo_width() + 1,
                  f"需要 {hint.winfo_reqwidth()} 分到 {hint.winfo_width()}"
                  f" wrap={hint.cget('wraplength')}")
            app._fit_root_window()
            pump(app, 0.2)

        # 填了个不存在的目录：保存必须当场拦下，而且什么都不许改
        app.jre_path_var.set(str(root / "没有这个目录"))
        fake_mb.calls.clear()
        before_jre = json.loads((root / "config.json").read_text(encoding="utf-8"))
        ok = app.save_settings()
        check("Java 路径下没有 java.exe 时保存被拦下", ok is False)
        check("并说明「要填 JRE / JDK 的根目录」",
              any("java.exe" in m for m in fake_mb.errors()), fake_mb.messages())
        check("被拦下时配置里的 Java 路径没被改坏",
              json.loads((root / "config.json").read_text(encoding="utf-8"))
              ["jvm"]["jre_path"] == before_jre["jvm"]["jre_path"],
              json.loads((root / "config.json").read_text(encoding="utf-8"))
              ["jvm"]["jre_path"])

        # 换成另一个能用的路径（同一个 jre，只是末尾多个分隔符）：
        # 存进去之后要**自动体检一次**，结果落在状态栏上。
        # ⚠️ 状态栏那句话是**瞬时**的：后面任何一条状态更新（版本列表刷新之类）
        #   都会把它顶掉，所以不能写完「等 0.3 秒再看一眼」—— 那条断言会随机器
        #   快慢时红时绿（实测就偶发红过一次）。改成**记下状态栏的每一次变化**，
        #   只要出现过一次就算过，被谁顶掉都不影响判定。
        seen_status: list[str] = []
        app.status_var.trace_add(
            "write", lambda *_: seen_status.append(app.status_var.get())
        )

        app.jre_path_var.set(real_jre + "/")
        fake_mb.calls.clear()
        seen_status.clear()
        ok = app.save_settings()
        app.root.update()
        check("路径合法时能存进 config.json",
              ok is True
              and json.loads((root / "config.json").read_text(encoding="utf-8"))
              ["jvm"]["jre_path"] == real_jre + "/")
        body = wait_jre_check()
        check("★ 换了 Java 路径会自动体检一次（结果显示出来）",
              "Java" in body, body)
        deadline = time.perf_counter() + 3.0     # 那句话走消息队列，晚一拍
        while (time.perf_counter() < deadline
               and not any("Java" in s for s in seen_status)):
            pump(app, 0.1)
        check("状态栏也报过「Java 可用」（记录每一次变化，不怕被顶掉）",
              any("Java" in s for s in seen_status), f"{seen_status[-3:]}")
        app.jre_path_var.set(real_jre)
        app.save_settings()
        app.root.update()

        print("\n[1c-2] 没 jre 时靠环境变量兜底：界面别骗人、存一次就撤掉")
        # 用户原话：「话说如果没有 jre 是不是可以先试试找环境变量看看有没有
        # jdk」。兜底本身在 code_regression 里真跑过；这里只管界面这一层：
        # 显示的是**实际在用的**那套 Java，而且用户存过一次之后兜底要作废
        # （不然框里写 A、跑的是 B，最难查的那种）。
        app._java_exe_override = Path(real_jre) / "bin" / "java.exe"
        try:
            check("★ 兜底生效时那一栏显示实际在用的 Java（不是配置里那个）",
                  app._jre_path_for_form() == real_jre,
                  f"{app._jre_path_for_form()} vs {real_jre}")
            app.jre_path_var.set(real_jre)      # 用户顺手存一次
            fake_mb.calls.clear()
            ok = app.save_settings()
            app.root.update()
            check("兜底路径能直接存进 config.json",
                  ok is True
                  and json.loads((root / "config.json").read_text(encoding="utf-8"))
                  ["jvm"]["jre_path"] == real_jre)
            check("★ 存过之后兜底作废（界面和实际跑的是同一个）",
                  app._java_exe_override is None,
                  str(app._java_exe_override))
        finally:
            app._java_exe_override = None
            app.jre_path_var.set(real_jre)
            app.save_settings()
            app.root.update()

        print("\n[1d] 「直接删除（不进回收站）」开关")
        app.show_settings()
        app.root.update()
        check("默认不勾（默认走回收站）",
              app.permanent_delete_var.get() is False,
              repr(app.permanent_delete_var.get()))
        app.permanent_delete_var.set(True)
        fake_mb.calls.clear()
        ok = app.save_settings()
        app.root.update()
        saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("勾上后存进 config.json 的是真布尔 True",
              ok is True and saved.get("permanent_delete") is True,
              repr(saved.get("permanent_delete")))
        app.show_settings()
        app.root.update()
        check("重开设置页回显这个开关（勾还在）",
              app.permanent_delete_var.get() is True)
        app.permanent_delete_var.set(False)     # 收拾干净：后面还要用沙箱
        app.save_settings()
        app.root.update()
        check("关掉也能存回 False",
              json.loads((root / "config.json").read_text(encoding="utf-8"))
              .get("permanent_delete") is False)

        print("\n[2] 自定义启动参数的保存与校验")
        # [1b] 那次「故意填错」留了个 showerror 在记录里，这里要的是干净的一页
        fake_mb.calls.clear()
        app.extra_vm_var.set('-Xmx2G -Dname="带 空格"')
        app.save_settings()
        app.root.update()
        saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("合法参数被存进 config.json",
              saved.get("extra_vm_args") == '-Xmx2G -Dname="带 空格"',
              repr(saved.get("extra_vm_args")))
        check("非法参数没触发错误弹窗", not fake_mb.errors(),
              fake_mb.messages())

        # 引号没闭合：必须当场拦下，而且不许改配置
        fake_mb.calls.clear()
        app.show_settings()
        app.extra_vm_var.set('-Dfoo="没闭合')
        app.save_settings()
        app.root.update()
        after = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("引号没闭合时弹错误框拦住",
              any("闭合" in m for m in fake_mb.errors()), fake_mb.messages())
        check("被拦下时配置没被改动",
              after.get("extra_vm_args") == '-Xmx2G -Dname="带 空格"',
              repr(after.get("extra_vm_args")))

        # 危险参数：askyesno 返回 True（FakeMessagebox 一律 True）→ 允许保存
        fake_mb.calls.clear()
        app.show_settings()
        app.extra_vm_var.set("-cp 别的.jar")
        app.save_settings()
        app.root.update()
        check("危险参数（-cp）会先弹确认",
              any(k == "askyesno" for k, _, _ in fake_mb.calls),
              str(fake_mb.calls))

        print("\n[2b] GitHub 镜像与日志保留份数")
        app.show_settings()
        app.github_mirror_var.set("ghfast.top")     # 故意只写主机名
        app.max_log_files_var.set(7)
        fake_mb.calls.clear()
        app.save_settings()
        app.root.update()
        saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("镜像地址补全协议后存进配置",
              saved.get("github_mirror") == "https://ghfast.top/",
              repr(saved.get("github_mirror")))
        check("日志保留份数存进配置",
              saved.get("max_log_files") == 7, str(saved.get("max_log_files")))
        check("废弃的 use_mirror 键不再写回配置",
              "use_mirror" not in saved, str(sorted(saved))[:200])

        app.show_settings()
        check("重开设置页回显镜像地址",
              app.github_mirror_var.get() == "https://ghfast.top/",
              repr(app.github_mirror_var.get()))
        check("重开设置页回显保留份数",
              app.max_log_files_var.get() == 7,
              str(app.max_log_files_var.get()))

        # 不像地址的输入：当场拦下，而且不许改配置
        fake_mb.calls.clear()
        app.github_mirror_var.set("这不是地址")
        app.save_settings()
        app.root.update()
        after_mirror = json.loads(
            (root / "config.json").read_text(encoding="utf-8")
        )
        check("不像地址的镜像当场弹错误框",
              any("镜像" in t for _, t, _ in fake_mb.calls),
              fake_mb.messages())
        check("镜像被拦下时配置没被改坏",
              after_mirror.get("github_mirror") == "https://ghfast.top/",
              repr(after_mirror.get("github_mirror")))

        # 留空 = 直连 GitHub（这也是「不想用镜像」的唯一操作方式）
        app.show_settings()
        app.github_mirror_var.set("")
        app.max_log_files_var.set(20)
        app.save_settings()
        app.root.update()
        cleared = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("镜像留空＝直连（存成空串）",
              cleared.get("github_mirror") == "",
              repr(cleared.get("github_mirror")))

        print("\n[2c] 滚轮：该滚设置页，不该改下拉框")
        # 事故：Tk 给 TCombobox 挂了一条类绑定（ttk::combobox::Scroll，滚轮换
        # 选项），而类绑定排在 `all` 标签之前 —— 滚轮扫过「GitHub 镜像」时
        # **页面滚了、镜像也被换掉**（用户报的「冲突」）。主面板的「当前存档」
        # 同样是下拉框，滚一下就等于悄悄切换存档分类（数据隔离）。
        wheel_binding = app.root.bind_class("TCombobox", "<MouseWheel>")
        check("TCombobox 的滚轮类绑定已摘掉", wheel_binding == "",
              repr(wheel_binding))

        app.show_settings()
        app.root.update()
        combos = list(walk_by_kind(app.settings_frame, (ttk.Combobox,)))
        check("设置页里找得到镜像下拉框", len(combos) == 1, f"{len(combos)} 个")
        if combos:
            mirror_combo = combos[0]
            # 候选清单是配置项（github_mirror_presets）：沙箱里写的是自己那
            # 一份，下拉框就该显示那一份 —— 而不是代码里写死的内置列表。
            check("下拉框的候选站来自配置（不是代码里写死的）",
                  list(mirror_combo["values"]) == list(SMOKE_MIRROR_PRESETS),
                  str(list(mirror_combo["values"])))
            app.github_mirror_var.set(SMOKE_MIRROR_PRESETS[0])
            app.root.update()
            canvas = app.settings_canvas
            canvas.yview_moveto(0)
            app.root.update()
            before_view = canvas.yview()
            # 模拟「滚轮事件落在下拉框上」（Windows 上滚轮发给焦点控件）
            mirror_combo.event_generate("<MouseWheel>", delta=-120)
            app.root.update()
            check("滚轮扫过镜像下拉框不会改镜像",
                  app.github_mirror_var.get() == SMOKE_MIRROR_PRESETS[0],
                  repr(app.github_mirror_var.get()))
            check("同一滚轮落在设置页上，页面确实滚了（滚动归属没跑偏）",
                  canvas.yview() != before_view,
                  f"{before_view} → {canvas.yview()}")

            # 反向对照：把 Tk 那条类绑定装回去，这一下就该换镜像了 ——
            # 否则「没换」可能只是滚轮压根没送到控件上，测试就是白测
            app.root.bind_class(
                "TCombobox", "<MouseWheel>",
                "ttk::combobox::Scroll %W [expr {-%D / 120}]",
            )
            app.github_mirror_var.set(SMOKE_MIRROR_PRESETS[0])
            app.root.update()
            mirror_combo.event_generate("<MouseWheel>", delta=-120)
            app.root.update()
            check("[对照] 装回类绑定后滚轮确实会改镜像（测试打到了现场）",
                  app.github_mirror_var.get() == SMOKE_MIRROR_PRESETS[1],
                  repr(app.github_mirror_var.get()))
            app.root.unbind_class("TCombobox", "<MouseWheel>")
            app.github_mirror_var.set("")
            app.root.update()

            # 下拉列表展开时挂在 <下拉框>.popdown 这个 Toplevel 下（实测它的
            # master 就是那个下拉框）。滚它不能顺带把背后的设置页也滚了 ——
            # _is_in_settings 往上走 master 时碰到 Toplevel 就停，正是为这个。
            mirror_combo.event_generate("<Button-1>")      # 展开列表
            app.root.update()
            popdown = f"{mirror_combo}.popdown"
            chain, cur = [], popdown
            while cur:
                chain.append(cur)
                cur = app.root.tk.call("winfo", "parent", cur)
            check("下拉列表确实挂在 <下拉框>.popdown 这个 Toplevel 下",
                  f"{mirror_combo}.popdown" in chain, " ← ".join(chain))
            app.root.tk.call("ttk::combobox::Unpost", mirror_combo)
            app.root.update()
            # 同构的层级（设置页里的控件 > Toplevel）必须判成「不在设置页里」，
            # 否则滚下拉列表会把背后的设置页一起滚了
            probe = tk.Toplevel(mirror_combo)
            check("_is_in_settings 往上碰到别的窗口就停（下拉列表/弹窗都算）",
                  not app._is_in_settings(probe))
            probe.destroy()

        # 主面板的「当前存档」：滚一下也不许换分类
        app.show_main()
        app.root.update()
        before_profile = app.config.get_current_profile()
        app.profile_combo.event_generate("<MouseWheel>", delta=-120)
        app.root.update()
        check("滚轮扫过主面板「当前存档」不会悄悄切换分类",
              app.config.get_current_profile() == before_profile
              and app.profile_var.get() == before_profile,
              f"{before_profile} → {app.profile_var.get()}")

        print("\n[3] 运行日志窗口")
        app.extra_vm_var.set("")
        app.save_settings()
        app.root.update()
        app.open_log_window()
        app.root.update()
        win = app._log_win
        check("日志窗口开出来了", win is not None and win.winfo_exists())
        nb = find_by_kind(win, ttk.Notebook) if win is not None else None
        check("日志窗口有两个页签（游戏输出 / 启动器日志）",
              nb is not None and len(nb.tabs()) == 2,
              str(nb.tabs() if nb is not None else None))
        check("切到启动器日志页能读出内容",
              "启动" in app._launcher_log_text.get("1.0", "end")
              or "初始" in app._launcher_log_text.get("1.0", "end"),
              repr(app._launcher_log_text.get("1.0", "end")[:60]))

        # 再点一次「运行日志」不应该开出第二个窗口
        first = app._log_win
        app.open_log_window()
        app.root.update()
        check("重复打开只聚焦同一个窗口（不会开一堆）",
              app._log_win is first)

        # 塞一段假输出，验证轮询真的会把内容显示出来
        check("还没启动过游戏时状态是「还没有启动过」",
              "还没有启动过" in app._log_state_var.get(),
              app._log_state_var.get())

        class _Stream:
            def __init__(self, lines):
                self._it = iter(list(lines))

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._it)

            def close(self):
                pass

        class _Proc:
            # 管道是二进制的（Popen 也是二进制读，见 gamelog._pump）
            stdout = _Stream([b"smoke line 1\n", b"smoke line 2\n"])

        app.game_log.start(_Proc())
        pump(app, 1.2)
        shown = app._game_log_text.get("1.0", "end")
        check("假输出被显示到日志窗口里",
              "smoke line 1" in shown and "smoke line 2" in shown,
              repr(shown[:80]))
        check("状态切到「本次已结束」", "已结束" in app._log_state_var.get(),
              app._log_state_var.get())
        check("落盘文件真的建了", app.game_log.path is not None
              and app.game_log.path.is_file(),
              str(app.game_log.path))

        # ★ 轮询期间绝不能碰窗口状态。每 400 ms 一次 deiconify()+lift()，
        #   Windows 会当成「程序在反复请求前台焦点」→ 任务栏图标一直高亮
        #   （用户报过：「开着日志页面任务栏会一直高亮启动器」）。
        win = app._log_win
        calls = {"deiconify": 0, "lift": 0}
        real_deiconify, real_lift = win.deiconify, win.lift
        win.deiconify = lambda *a, **k: calls.__setitem__(
            "deiconify", calls["deiconify"] + 1)
        win.lift = lambda *a, **k: calls.__setitem__("lift", calls["lift"] + 1)

        pump(app, 1.2)                      # 至少跑过 2 个轮询周期
        check("轮询期间不 deiconify（任务栏不会一直闪）",
              calls["deiconify"] == 0, str(calls))
        check("轮询期间不 lift（任务栏不会一直高亮）",
              calls["lift"] == 0, str(calls))
        check("轮询本身还活着（不是靠停掉刷新换来的安静）",
              app._log_poll_id is not None)

        # 对照：用户**主动**再点一次「运行日志」时，应该把窗口提到前面
        app.open_log_window()
        check("用户主动打开时才提窗口（这次该 lift）",
              calls["lift"] >= 1, str(calls))
        win.deiconify, win.lift = real_deiconify, real_lift

        print("\n[4] 游戏运行中点「设置」不能把 GUI 线程锁死")
        # 事故复现路径：show_settings 持着 _proc_lock 弹 messagebox，
        # 弹窗的嵌套事件循环里计时器回调又来抢同一把锁 → 普通 Lock 直接自锁。
        # 这里断言「弹提示的那一刻锁是放开的」，别的回调进得来。
        class _RunningProc:
            def poll(self):                 # None = 还在跑
                return None

        saved_proc = app.current_process
        app.current_process = _RunningProc()
        picked: list[bool] = []

        def probing_showinfo(*a, **k):
            got = app._proc_lock.acquire(timeout=0.5)   # 模拟别的回调来抢锁
            picked.append(got)
            if got:
                app._proc_lock.release()
            return True

        fake_mb.showinfo = probing_showinfo
        try:
            app.show_settings()
        finally:
            del fake_mb.showinfo
            app.current_process = saved_proc
        check("游戏运行中走的是提示而不是切到设置页",
              not app.settings_frame.winfo_ismapped())
        check("弹提示期间锁是放开的（别的回调抢得到，不会自锁）",
              picked == [True], str(picked))
        app.root.update()

        print("\n[5] 游戏退出后自动关闭启动器")
        # 这一路真会把窗口关掉，所以拿假进程演一遍「游戏退出」，并把
        # on_closing 换成只记一笔 —— 不然测试自己就被关掉了。
        order: list[str] = []
        closed: list[bool] = []
        deiconified: list[int] = []
        real_closing = app.on_closing
        real_backup_mgr = app._make_backup_manager
        real_deiconify = app.root.deiconify
        app.on_closing = lambda: closed.append(True)
        app.root.deiconify = lambda *a, **k: deiconified.append(1)

        class _FakeBackup:
            def create_backup(self):
                order.append("backup")

        app._make_backup_manager = lambda name=None: _FakeBackup()

        class _ExitedProc:
            stdout = None

            def wait(self):         # 立刻返回 = 游戏已经退出了
                return 0

            def poll(self):
                return 0

        def run_session():
            with app._proc_lock:
                app.current_process = _ExitedProc()
                app.game_start_time = time.time()
            app._game_session({"name": "smoke"}, "默认", None, False)

        # min_playtime 设 0：这一局「玩了 0 分钟」也要走到自动备份那一段
        app.config.set_profile_setting("默认", "min_playtime", 0)
        app.config.set_profile_setting("默认", "auto_backup", True)

        # [对照] 选项关着：不许关启动器，而且窗口要回来、按钮要能用
        app.config.set("close_on_game_exit", False)
        closed.clear(), deiconified.clear(), order.clear()
        run_session()
        pump(app, 0.4)
        check("[对照] 选项关着时不会自动关闭启动器", closed == [], str(closed))
        check("[对照] 选项关着时窗口会回来（deiconify 走了）",
              deiconified == [1], str(deiconified))
        check("自动备份照旧先做完", order == ["backup"], str(order))
        check("退出后「启动游戏」按钮重新可用",
              str(app.launch_btn.cget("state")) == "normal",
              str(app.launch_btn.cget("state")))

        # 选项开着：备份 → 关启动器
        app.config.set("close_on_game_exit", True)
        closed.clear(), deiconified.clear(), order.clear()
        run_session()
        pump(app, 0.4)
        check("选项开着：游戏退出后请求关闭启动器",
              closed == [True], str(closed))
        check("★ 关闭排在自动备份之后（顺序反了这局备份就没了）",
              order == ["backup"], str(order))
        check("要关了就不再 deiconify（免得先闪一下再关）",
              deiconified == [], str(deiconified))

        # 有下载在跑：留着窗口，别把下载打断（半截文件会留在 versions/）
        app.update_manager.downloading.set()
        closed.clear()
        run_session()
        pump(app, 0.4)
        check("下载进行中不关启动器（免得留下半个版本包）",
              closed == [], str(closed))
        check("并说明为什么没关", "下载" in app.status_var.get(),
              app.status_var.get())
        app.update_manager.downloading.clear()
        app.config.set("close_on_game_exit", False)
        app.on_closing = real_closing
        app._make_backup_manager = real_backup_mgr
        app.root.deiconify = real_deiconify

        print("\n[6] 关闭")
        app._close_log_window()
        app.root.update()
        check("日志窗口关掉后引用被清空", app._log_win is None)
        check("关窗口后没留下定时器", app._log_poll_id is None)
        app.on_closing()
        check("启动器能干净退出", True)
        app = None
    finally:
        os.chdir(cwd_backup)
        if app is not None:
            try:
                app.on_closing()
            except Exception:                                   # noqa: BLE001
                pass
        if args.keep:
            print(f"\n保留沙箱：{root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
            print("\n已清理沙箱")

    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  失败:", f)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
