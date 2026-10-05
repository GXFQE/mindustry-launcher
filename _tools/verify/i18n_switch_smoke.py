# -*- coding: utf-8 -*-
"""换语言：真的换过来了没 —— 真建窗口、真点一次「保存并返回」。

为什么**不**放进 gui_smoke.py
------------------------------
那个脚本**故意把界面语言钉死成中文**（``MDT_LANG=zh_CN``），因为它有十来处
断言直接比中文文案。而这里要验的恰恰是「语言能被用户改」——
一旦被钉死，``i18n.apply_setting()`` 会直接 return，测到的就不是真东西了。
所以单独一个脚本，并且在最开头**清掉 MDT_LANG**。

起点为什么不依赖系统语言
------------------------
沙箱的 config.json 里写死 ``"language": "zh_CN"``。若写 ``auto``，结果会随
跑脚本那个人的系统语言变 —— 那样这个脚本就不是确定性的了（同一条纪律：
自检不许跟"用户设置"纠缠，见 launcher/i18n.py 的 docstring 第 3 条）。

用法：
    python _tools/verify/i18n_switch_smoke.py
    python _tools/verify/i18n_switch_smoke.py --keep
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

# ★ 必须在 import launcher 之前清掉：本脚本测的就是「不钉死」时的行为。
os.environ.pop("MDT_LANG", None)


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
    """按文本找控件（和 gui_smoke 同一个套路：不去生产代码里存引用）。"""
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


def find_combo(root_widget, var):
    """按 ``textvariable`` 定位下拉框（不靠控件顺序 —— 加一个下拉框就全错位）。"""
    target = str(var)
    for child in root_widget.winfo_children():
        if isinstance(child, ttk.Combobox):
            try:
                if str(child.cget("textvariable")) == target:
                    return child
            except tk.TclError:
                pass
        found = find_combo(child, var)
        if found is not None:
            return found
    return None


def pump(app, seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.root.update()
        time.sleep(0.02)


def make_sandbox() -> Path:
    """最小沙箱：config.json（language 钉成 zh_CN）+ 真实 jre 的绝对路径。"""
    root = Path(tempfile.mkdtemp(prefix="mdt_i18n_smoke_"))
    jre_abs = (DATA / "jre").resolve()
    if not (jre_abs / "bin" / "java.exe").is_file():
        raise SystemExit(f"找不到真实 jre：{jre_abs}")
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "language": "zh_CN",            # ★ 起点固定，不看系统语言
        "hide_on_launch": False,
        "auto_update": False,           # 别在这里发网络请求
        "github_mirror": "",
        "current_profile": "默认",
        "profiles": {
            "默认": {
                "data_dir": str(data_dir),
                "min_playtime": 20,
                "max_backups": 20,
                "auto_backup": False,
            },
        },
        "jvm": {
            "jre_path": str(jre_abs),
            "main_class": "mindustry.desktop.Main",
            "vm_args": [],
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
    # 语言包要跟着程序目录走（源码模式下 BASE_DIR 就是 cwd，而这里 cwd 切到了
    # 沙箱）—— 见 gui_smoke.make_sandbox 里同一段说明。
    shutil.copytree(ROOT / "lang", root / "lang")
    return root


def main() -> int:
    ap = argparse.ArgumentParser(description="换语言冒烟（真建窗口）")
    ap.add_argument("--keep", action="store_true", help="保留沙箱")
    args = ap.parse_args()

    root = make_sandbox()
    cwd_backup = os.getcwd()
    print("=" * 64)
    print(f"沙箱：{root}")
    print(f"MDT_LANG 已清除：{'MDT_LANG' not in os.environ}")
    print("=" * 64)

    app = None
    try:
        os.chdir(root)
        for mod in [m for m in list(sys.modules) if m.startswith("launcher")]:
            del sys.modules[mod]
        import launcher.i18n as i18n
        from launcher.gui import MindustryLauncher

        check("本脚本没把语言钉死（钉死了就测不到切换）",
              i18n.language_is_forced() is False)

        app = MindustryLauncher()
        app.root.update()

        print("\n[1] 起点：配置里写着 zh_CN，界面就该是中文")
        title_cn = app.root.title()
        check("窗口标题是中文", title_cn.startswith("Mindustry 启动器"), title_cn)
        check("生效语言就是配置里那个", i18n.current_language() == "zh_CN",
              i18n.current_language())
        launch_cn = find_widget(app.main_frame, "启动游戏")
        check("主面板按钮是中文（「启动游戏」）", launch_cn is not None)
        app.show_settings()
        app.root.update()
        combo = find_combo(app.settings_frame, app.language_var)
        check("设置页里有「界面语言」下拉框", combo is not None)
        check("下拉框当前显示「简体中文」",
              app.language_var.get() == "简体中文", app.language_var.get())
        check("候选里有英文（自己的名字，不是翻译名）",
              "English" in list(combo["values"]) if combo else False,
              str(list(combo["values"])) if combo else "")
        check("设置页底部按钮是中文",
              find_widget(app.settings_frame, "保存并返回") is not None
              and find_widget(app.settings_frame, "重置默认") is not None)

        print("\n[2] 切成英文 → 保存并返回")
        assert combo is not None
        app.language_var.set("English")
        before_settings_frame = app.settings_frame
        app.save_and_return()
        pump(app, 0.3)

        saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("★ 存进 config.json 的是契约值 en_US（不是界面文案 English）",
              saved.get("language") == "en_US", repr(saved.get("language")))
        check("语言真的换过来了", i18n.current_language() == "en_US",
              i18n.current_language())
        title_en = app.root.title()
        check("窗口标题跟着变英文",
              title_en.startswith("Mindustry Launcher"), title_en)

        print("\n[3] 两页都重建了（不是只改了标题）")
        check("★ 中文的「启动游戏」已经不存在了",
              find_widget(app.main_frame, "启动游戏") is None)
        launch_en = find_widget(app.main_frame, "Launch game")
        check("主面板按钮换成英文（Launch game）", launch_en is not None)
        check("★ 换的是新控件（老的那个已经销毁，不是叠了一层）",
              launch_cn is not None and launch_cn.winfo_exists() == 0
              and launch_en is not launch_cn)
        check("旧的设置页对象也被销毁了",
              before_settings_frame.winfo_exists() == 0)
        app.show_settings()
        app.root.update()
        check("重开设置页：底部按钮是英文",
              find_widget(app.settings_frame, "Save and go back") is not None
              and find_widget(app.settings_frame, "Reset to defaults") is not None)
        check("★ 中文的「保存并返回」不存在了",
              find_widget(app.settings_frame, "保存并返回") is None)
        check("下拉框回显的是 English",
              app.language_var.get() == "English", app.language_var.get())

        print("\n[4] 重建时数据要灌回去（不能变成空界面）")
        check("主面板「当前存档」还是那个分类",
              app.profile_var.get() == "默认", app.profile_var.get())
        check("存档路径那行也还在",
              bool(app.profile_path_var.get()), app.profile_path_var.get())
        app.show_main()
        app.root.update()
        check("回到主界面了（设置页收起来）",
              app.main_frame.winfo_ismapped() == 1
              and app.settings_frame.winfo_ismapped() == 0)
        status = app.status_var.get()
        check("★ 状态栏那句提示用的是新语言（English）",
              "Language switched" in status, status)

        print("\n[5] 再切回中文（回程也要能走通）")
        app.show_settings()
        app.root.update()
        combo = find_combo(app.settings_frame, app.language_var)
        assert combo is not None
        app.language_var.set("简体中文")
        app.save_and_return()
        pump(app, 0.3)
        saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("配置里回到 zh_CN", saved.get("language") == "zh_CN",
              repr(saved.get("language")))
        check("窗口标题变回中文",
              app.root.title().startswith("Mindustry 启动器"), app.root.title())
        check("英文那句已经不在了",
              find_widget(app.main_frame, "Launch game") is None
              and find_widget(app.main_frame, "启动游戏") is not None)

        print("\n[6] 回退链：缺文案不许抛异常、不许给空串")
        check("★ 不存在的 key 原样返回（界面显示 key，便于定位）",
              i18n.t("这里根本没有这条.key") == "这里根本没有这条.key")
        check("带占位符的正常取词",
              i18n.t("err.logs_body", max=20).startswith("要填 1 到 20"),
              i18n.t("err.logs_body", max=20))
        # 认不出的语言退回默认（放在最后：它会改全局状态）
        check("认不出的语言 code 退回默认并点名",
              i18n.set_language("klingon") == "zh_CN")
        check("退回后界面语言仍是可用的那个",
              i18n.current_language() == "zh_CN", i18n.current_language())

        app.on_closing()
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
