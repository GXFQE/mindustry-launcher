# -*- coding: utf-8 -*-
"""主界面：主面板、设置面板、界面刷新与目录选择。"""
import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from .config import (
    MAX_LOG_KEEP,
    ConfigManager,
    normalize_log_keep,
    normalize_mirror,
)
from .gamecmd import check_extra_args, probe_java, split_args

logger = logging.getLogger(__name__)

# 设置页两组设置各在自己的 LabelFrame 里 grid，列宽默认按**各自**最长的标签
# 算 —— 于是「最小备份时间(分钟):」把存档那组的输入框推得比启动器那组更靠右，
# 看着就是「输入框没对齐」。把两组所有左侧标签放一起量一次，给两帧的
# column 0 设同一个 minsize，输入框左边缘才齐（见 _label_col_width）。
SETTINGS_LABEL_TEXTS = (
    "分类名称:",
    "数据目录:",
    "最小备份时间(分钟):",
    "最大备份数量:",
    "GitHub 镜像:",
    "额外 JVM 参数:",
    "额外游戏参数:",
    "日志保留份数:",
    "Java 路径(JRE/JDK):",
    "启动器更新:",
)

# 「启动器自更新」档位 → 界面文案。键是 config.json 里存的英文值（那是稳定
# 契约），值是给人看的说法。★ 这套映射**只加不改**：以后要加档位，就在
# utils.LAUNCHER_UPDATE_MODES 和这里各加一项，别动 auto/check/off 的含义。
LAUNCHER_UPDATE_LABELS = {
    "auto": "自动（有新版就下载，退出时替换）",
    "check": "只提示有新版本",
    "off": "关闭",
}
LAUNCHER_UPDATE_VALUES = {v: k for k, v in LAUNCHER_UPDATE_LABELS.items()}


def launcher_update_label(value: str) -> str:
    """配置值 → 界面文案。认不出就按默认档显示，别让下拉框出现空行。"""
    return LAUNCHER_UPDATE_LABELS.get(
        value, LAUNCHER_UPDATE_LABELS["auto"]
    )

# ttk.Label 的自然宽度 = 字体实测宽度 + 这个内边距（Tk 8.6 实测正好 4）。
# 算「这句提示需要多宽」时得把它加回去，不然每句都会「差 4 像素」而被误折行。
_HINT_PAD = 4


def _auto_wrap_hint(label: ttk.Label) -> ttk.Label:
    """让灰色提示按「手里这点宽度」折行，而不是被直接裁掉（原样返回好链式调）。

    设置页的提示都放在输入框那一列：**列宽由窗口分配，标签的自然宽度由文字
    长度决定**。文字比列宽长的时候，Tk 既不报错也不换行 —— 右边就是少一截
    （用户报过：「填 JRE 或 JDK 的根目录都行…」被切在「启动器旁」）。

    wraplength 只能在「分到多宽」出来之后才算，所以绑 ``<Configure>``：布局完
    比一下「文字真实需要多宽」（ttk.Label 的自然宽度正好是字体实测 + 4）和
    「分到多宽」，真放不下才折。反过来（无条件折）会把那些没被裁的提示也折一
    行 —— 末尾几个字无缘无故掉到下一行，同样是事故。

    ★ **用它的提示必须占满整行**（grid 传 ``sticky=tk.EW``、pack 传
    ``fill=tk.X``）：不然它拿到的宽度是「**折行之后**的自然宽度」，而
    「需要多宽」是按没折的整句算的 —— 每来一次 Configure 就多折 2 像素，
    一轮一轮往下缩，最后缩成一列一个字。
    """
    try:
        font = tkfont.Font(font=label.cget("font"))
    except tk.TclError:             # 没显式给字体：拿默认的凑合量
        font = tkfont.nametofont("TkDefaultFont")

    def on_configure(event: tk.Event) -> None:
        try:
            name = label.cget("textvariable")
            text = str(label.getvar(name)) if name else str(label.cget("text"))
        except (tk.TclError, ValueError):       # pragma: no cover - 控件已销毁
            return
        need = font.measure(text) + _HINT_PAD
        wrap = max(event.width - 2, 1) if need > event.width else 0
        if int(label.cget("wraplength") or 0) != wrap:
            label.configure(wraplength=wrap)

    label.bind("<Configure>", on_configure, add="+")
    return label


class MainMixin:
    def _build_main_ui(self) -> None:
        ttk.Label(
            self.main_frame,
            text="🎮 Mindustry 启动器",
            font=("Microsoft YaHei", 14, "bold"),
        ).pack(pady=(0, 12))
        profile_frame = ttk.LabelFrame(
            self.main_frame, text="存档分类（切换后启动的游戏数据完全隔离）",
            padding="10",
        )
        profile_frame.pack(fill=tk.X, pady=(0, 10))
        profile_row = ttk.Frame(profile_frame)
        profile_row.pack(fill=tk.X)
        ttk.Label(profile_row, text="当前存档:").pack(side=tk.LEFT)
        self.profile_var = tk.StringVar(
            value=self.config.get_current_profile()
        )
        self.profile_combo = ttk.Combobox(
            profile_row,
            textvariable=self.profile_var,
            values=self.config.get_profile_names(),
            state="readonly",
            font=self.font,
            width=24,
        )
        self.profile_combo.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        self.profile_combo.bind(
            "<<ComboboxSelected>>", self._on_profile_selected
        )
        ttk.Button(
            profile_row, text="管理存档", command=self.manage_profiles
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            profile_row, text="打开目录", command=self.open_current_save_dir
        ).pack(side=tk.LEFT, padx=(0, 5))
        self.profile_path_var = tk.StringVar()
        ttk.Label(
            profile_frame,
            textvariable=self.profile_path_var,
            font=("Microsoft YaHei", 8),
            foreground="#555555",
            anchor=tk.W,
        ).pack(fill=tk.X, pady=(6, 0))
        version_frame = ttk.LabelFrame(
            self.main_frame, text="选择游戏版本", padding="10"
        )
        version_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        list_cont = ttk.Frame(version_frame)
        list_cont.pack(fill=tk.BOTH, expand=True)
        self.version_listbox = tk.Listbox(list_cont, font=self.font, height=12)
        self.version_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(
            list_cont, command=self.version_listbox.yview
        )
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.version_listbox.config(yscrollcommand=scrollbar.set)
        btn_frame = ttk.Frame(self.main_frame)
        btn_frame.pack(fill=tk.X, pady=10)
        self.launch_btn = ttk.Button(
            btn_frame,
            text="启动游戏",
            command=self.launch_game,
            state=tk.DISABLED,
        )
        self.launch_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame, text="刷新版本", command=self.refresh_versions
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame, text="管理版本", command=self.manage_versions
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame, text="管理备份", command=self.manage_backups
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame, text="检查更新", command=self.check_updates
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame, text="运行日志", command=self.open_log_window
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="设置", command=self.show_settings).pack(
            side=tk.RIGHT, padx=5
        )
        status_frame = ttk.Frame(self.main_frame)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM, pady=(10, 0))
        self.status_var = tk.StringVar(value="✅ 就绪 - 选择游戏版本")
        status_label = ttk.Label(
            status_frame,
            textvariable=self.status_var,
            font=("Microsoft YaHei", 9),
            relief=tk.SUNKEN,
            anchor=tk.W,
            padding=(5, 2),
        )
        status_label.pack(fill=tk.X)
        self.version_listbox.bind(
            "<<ListboxSelect>>",
            self._on_version_selected,
        )
        self.version_listbox.bind(
            "<Double-Button-1>", lambda e: self.launch_game()
        )

    @staticmethod
    def _label_col_width(texts: tuple[str, ...]) -> int:
        """按当前字体实测最宽的那个标签 —— 两帧的标签列用同一个宽度。

        不用写死的像素值：系统字体或 DPI 缩放一变，写死值又会错开。
        量不出来时给个保守宽度，总比两组各算各的、彻底不对齐强。
        """
        try:
            font = tkfont.nametofont("TkDefaultFont")
            return max(font.measure(t) for t in texts) + 12
        except (tk.TclError, ValueError):
            return 140

    def _build_settings_ui(self) -> None:
        # 内容一律放进滚动区（settings_body），按钮钉在窗口底部
        #（容器见 gui_core._make_settings_scrollable）
        ttk.Label(
            self.settings_body,
            text="⚙️ 启动器设置",
            font=("Microsoft YaHei", 14, "bold"),
        ).pack(pady=(0, 4))
        ttk.Label(
            self.settings_body,
            text="「存档分类设置」只作用于当前这个存档；"
            "「启动器设置」对所有存档都生效。\n"
            "想换存档，回主界面用顶部的分类下拉框切换。",
            font=("Microsoft YaHei", 8),
            foreground="#555555",
            justify=tk.LEFT,
            anchor=tk.W,
        ).pack(fill=tk.X, pady=(0, 12))

        # 两帧的标签列定同一个宽度，输入框才会左右对齐
        label_w = self._label_col_width(SETTINGS_LABEL_TEXTS)

        # ---- 存档分类级：每个存档各自一套 ----
        content = ttk.LabelFrame(
            self.settings_body,
            text="存档分类设置（每个存档独立）",
            padding="12",
        )
        content.pack(fill=tk.X, pady=(0, 10))
        content.columnconfigure(0, minsize=label_w)
        row = 0
        ttk.Label(content, text="分类名称:").grid(
            row=row, column=0, sticky=tk.W, pady=5
        )
        self.settings_profile_var = tk.StringVar()
        ttk.Label(
            content,
            textvariable=self.settings_profile_var,
            font=("Microsoft YaHei", 9, "bold"),
        ).grid(row=row, column=1, columnspan=2, sticky=tk.W, padx=5)
        row += 1
        ttk.Label(content, text="数据目录:").grid(
            row=row, column=0, sticky=tk.W, pady=5
        )
        self.save_path_var = tk.StringVar()
        ttk.Entry(content, textvariable=self.save_path_var, width=46).grid(
            row=row, column=1, padx=5, pady=5, sticky=tk.EW
        )
        ttk.Button(
            content,
            text="浏览",
            command=lambda: self._browse_dir(self.save_path_var),
        ).grid(row=row, column=2)
        row += 1
        _auto_wrap_hint(ttk.Label(
            content,
            text="改这个路径只改「以后用哪个目录」，原有存档不会被搬走，需要自己搬。",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        )).grid(row=row, column=1, columnspan=2, sticky=tk.EW, padx=5)
        row += 1
        ttk.Label(content, text="最小备份时间(分钟):").grid(
            row=row, column=0, sticky=tk.W, pady=5
        )
        self.min_playtime_var = tk.IntVar()
        # 数字框 + 它的说明收进一个**跨满两列**的容器（跟「日志保留份数」一个
        # 路子）：说明紧挨着框子，「←」才真的指着它。以前说明单独占第 2 列，
        # 被那一列的宽度顶到最右边 —— 箭头离框子四百多像素，指着空气；顺带
        # 把第 2 列撑得很宽，上面「数据目录」那个输入框被压短一截。
        spin_row = ttk.Frame(content)
        spin_row.grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=3
        )
        ttk.Spinbox(
            spin_row,
            from_=1,
            to=60,
            textvariable=self.min_playtime_var,
            width=10,
        ).pack(side=tk.LEFT)
        _auto_wrap_hint(ttk.Label(
            spin_row,
            text="← 一局玩不够这么久就跳过不备份",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        )).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))
        row += 1
        ttk.Label(content, text="最大备份数量:").grid(
            row=row, column=0, sticky=tk.W, pady=5
        )
        self.max_backups_var = tk.IntVar()
        max_row = ttk.Frame(content)        # 同上：数字框 + 说明挤一格、跨两列
        max_row.grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=3
        )
        ttk.Spinbox(
            max_row,
            from_=1,
            to=50,
            textvariable=self.max_backups_var,
            width=10,
        ).pack(side=tk.LEFT)
        _auto_wrap_hint(ttk.Label(
            max_row,
            text="← 超出后自动删最旧的那一份",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        )).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))
        row += 1
        self.auto_backup_var = tk.BooleanVar()
        ttk.Checkbutton(
            content, text="启用自动备份", variable=self.auto_backup_var
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 3))
        content.columnconfigure(1, weight=1)

        # ---- 全局级：所有存档共用 ----
        glob = ttk.LabelFrame(
            self.settings_body,
            text="启动器设置（所有存档共用）",
            padding="12",
        )
        glob.pack(fill=tk.X, pady=(0, 10))
        glob.columnconfigure(0, minsize=label_w)
        self.hide_on_launch_var = tk.BooleanVar(
            value=self.config.get("hide_on_launch")
        )
        # 复选框一律 columnspan=2：它们的文字比标签长得多，只占第 0 列的话
        # 会把标签列撑宽 —— 那样下面几行的输入框又跟存档那组错开了。
        ttk.Checkbutton(
            glob,
            text="启动游戏时隐藏窗口",
            variable=self.hide_on_launch_var,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=3)
        # 和上面那个是一对：一个管「玩的时候窗口在哪」，一个管「玩完窗口还在不在」
        self.close_on_game_exit_var = tk.BooleanVar(
            value=self.config.get("close_on_game_exit")
        )
        ttk.Checkbutton(
            glob,
            text="游戏退出后自动关闭启动器（自动备份做完才关）",
            variable=self.close_on_game_exit_var,
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=3)
        self.auto_update_var = tk.BooleanVar(
            value=self.config.get("auto_update")
        )
        ttk.Checkbutton(
            glob, text="启动时自动检查更新", variable=self.auto_update_var
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=3)
        row = 3
        # ---- 启动器自身的更新 ----
        # 注意跟上面那个「启动时自动检查更新」不是一回事：那个管**游戏**版本
        # （Anuken 的 jar），这个管**启动器自己**。默认 auto：后台下载，退出
        # 启动器时由外部执行体替换（Windows 不允许覆盖运行中的 exe）。
        # 只替换程序文件，**不碰** config.json / 备份 / 存档。
        ttk.Label(glob, text="启动器更新:").grid(
            row=row, column=0, sticky=tk.W, pady=3
        )
        self.launcher_update_var = tk.StringVar(
            value=launcher_update_label(self.config.get("launcher_update"))
        )
        ttk.Combobox(
            glob,
            textvariable=self.launcher_update_var,
            values=list(LAUNCHER_UPDATE_LABELS.values()),
            state="readonly",
            font=self.font,
            width=28,
        ).grid(row=row, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=3)
        row += 1
        _auto_wrap_hint(ttk.Label(
            glob,
            text="只换启动器程序本身，不动游戏版本、配置、备份和存档；"
                 "源码运行时自动停用",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        )).grid(row=row, column=1, columnspan=2, sticky=tk.EW, padx=5)
        row += 1
        # ---- Java（java.exe）路径 ----
        # 以前这一项只能改 config.json；现在放在界面上，改完能当场「检测」。
        # **JRE 和 JDK 都收** —— 判定只看目录里有没有 bin\java.exe，不看它
        # 叫什么；装了 JDK 的人没必要为了这个启动器再下一份 jre。
        # 路径填错不会再让启动器打不开：启动时找不到就退回默认的 jre/
        # （见 gui_core._validate_common_files），再不行才去环境变量里兜。
        # ★ columnspan=3：这一帧有 3 列（标签 / 输入框 / 按钮），只盖 2 列的话
        #   分割线会在「按钮那一列的左边」断掉 —— 右边凭空少一截（量过：差 178 px）。
        ttk.Separator(glob, orient=tk.HORIZONTAL).grid(
            row=row, column=0, columnspan=3, sticky=tk.EW, pady=6
        )
        row += 1
        ttk.Label(glob, text="Java 路径(JRE/JDK):").grid(
            row=row, column=0, sticky=tk.W, pady=3
        )
        self.jre_path_var = tk.StringVar(value=self._jre_path_for_form())
        ttk.Entry(glob, textvariable=self.jre_path_var).grid(
            row=row, column=1, sticky=tk.EW, padx=5, pady=3
        )
        jre_btns = ttk.Frame(glob)
        jre_btns.grid(row=row, column=2, sticky=tk.W)
        ttk.Button(
            jre_btns,
            text="浏览",
            command=lambda: self._browse_dir(self.jre_path_var),
        ).pack(side=tk.LEFT)
        # 「检测」＝真跑一次 java -version（后台线程，不卡界面）
        ttk.Button(
            jre_btns, text="检测", command=self.check_jre
        ).pack(side=tk.LEFT, padx=(4, 0))
        row += 1
        self._jre_check: tuple[str, bool, str] | None = None
        self.jre_check_var = tk.StringVar()
        self.jre_check_label = _auto_wrap_hint(ttk.Label(
            glob,
            textvariable=self.jre_check_var,
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        ))
        # columnspan=2：检测结果可能是「❌ 找不到文件：D:\…\bin\java.exe」
        # 这种带长路径的一句话，占满整行才不至于被右边的按钮列挤掉尾巴。
        self.jre_check_label.grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, padx=5
        )
        row += 1
        _auto_wrap_hint(ttk.Label(
            glob,
            text="填 JRE 或 JDK 的根目录都行（里面要有 bin\\java.exe）；"
                 "只写 jre 表示启动器旁边那个",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        )).grid(row=row, column=1, columnspan=2, sticky=tk.EW, padx=5)
        row += 1
        # ---- GitHub 镜像 ----
        # ★ columnspan=3：这一帧有 3 列（标签 / 输入框 / 按钮），只盖 2 列的话
        #   分割线会在「按钮那一列的左边」断掉 —— 右边凭空少一截（量过：差 178 px）。
        ttk.Separator(glob, orient=tk.HORIZONTAL).grid(
            row=row, column=0, columnspan=3, sticky=tk.EW, pady=6
        )
        row += 1
        ttk.Label(glob, text="GitHub 镜像:").grid(
            row=row, column=0, sticky=tk.W, pady=3
        )
        self.github_mirror_var = tk.StringVar(
            value=self.config.get("github_mirror")
        )
        # 可编辑的下拉框：预设不够用就直接把地址敲进去。候选清单取自
        # config.json 的 github_mirror_presets（想加自己的镜像站改那份文件，
        # 改完重启启动器生效）—— 界面只读它，不往回写。
        # ★ columnspan=2：这一行右边没有按钮（列宽是全表共享的，第 2 列被
        #   Java 那两个按钮占着），跨满两列才不留空档 —— 否则输入框右边平白
        #   空出一整列按钮的宽度（量过：差 183 px）。
        ttk.Combobox(
            glob,
            textvariable=self.github_mirror_var,
            values=list(self.config.get("github_mirror_presets")),
        ).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=3
        )
        row += 1
        _auto_wrap_hint(ttk.Label(
            glob,
            text="下载 jar 时拼在地址前做加速；留空＝直连 GitHub，"
            "这个源拉不动会自动换下一个",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        )).grid(row=row, column=1, columnspan=2, sticky=tk.EW, padx=5)
        row += 1
        # ---- 自定义启动参数 ----
        # ★ columnspan=3：这一帧有 3 列（标签 / 输入框 / 按钮），只盖 2 列的话
        #   分割线会在「按钮那一列的左边」断掉 —— 右边凭空少一截（量过：差 178 px）。
        ttk.Separator(glob, orient=tk.HORIZONTAL).grid(
            row=row, column=0, columnspan=3, sticky=tk.EW, pady=6
        )
        row += 1
        ttk.Label(glob, text="额外 JVM 参数:").grid(
            row=row, column=0, sticky=tk.W, pady=3
        )
        self.extra_vm_var = tk.StringVar(
            value=self.config.get("extra_vm_args")
        )
        # columnspan=2：右边没有按钮，跟镜像那行一样跨满两列（见上面那段说明）
        ttk.Entry(glob, textvariable=self.extra_vm_var).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=3
        )
        row += 1
        _auto_wrap_hint(ttk.Label(
            glob,
            text="加在 -cp 之前，如 -Xmx4G；空格分隔，含空格的项用英文双引号包住",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        )).grid(row=row, column=1, columnspan=2, sticky=tk.EW, padx=5)
        row += 1
        ttk.Label(glob, text="额外游戏参数:").grid(
            row=row, column=0, sticky=tk.W, pady=3
        )
        self.extra_prog_var = tk.StringVar(
            value=self.config.get("extra_program_args")
        )
        # columnspan=2：同上一行，跨满两列才对得齐
        ttk.Entry(glob, textvariable=self.extra_prog_var).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=3
        )
        row += 1
        _auto_wrap_hint(ttk.Label(
            glob,
            text="传给游戏本体，加在主类之后（一般用不到，排查问题时才填）",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        )).grid(row=row, column=1, columnspan=2, sticky=tk.EW, padx=5)
        row += 1
        # ★ columnspan=3：这一帧有 3 列（标签 / 输入框 / 按钮），只盖 2 列的话
        #   分割线会在「按钮那一列的左边」断掉 —— 右边凭空少一截（量过：差 178 px）。
        ttk.Separator(glob, orient=tk.HORIZONTAL).grid(
            row=row, column=0, columnspan=3, sticky=tk.EW, pady=6
        )
        row += 1
        self.save_game_log_var = tk.BooleanVar(
            value=self.config.get("save_game_log")
        )
        ttk.Checkbutton(
            glob,
            text="把游戏输出保存到 logs/ 目录（关掉则只在日志窗口里实时显示）",
            variable=self.save_game_log_var,
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=3)
        row += 1
        ttk.Label(glob, text="日志保留份数:").grid(
            row=row, column=0, sticky=tk.W, pady=(3, 5)
        )
        self.max_log_files_var = tk.IntVar(
            value=self.config.get("max_log_files")
        )
        # 数字框和它的说明挤在同一个格子里：单独占一列会把上面两个输入框
        # 的右边缘顶短（列宽是全表共享的，犯不着为一行提示改整体版式）。
        # ★ columnspan=2：右边没有按钮，跟镜像/参数那几行一样跨满两列，
        #   右边缘才对得齐（不然这一行比它们短一整列按钮的宽度）。
        # sticky=EW + 提示 fill=X：让说明拿到「这一格真实的剩余宽度」，
        # 窗口拉窄时它才会折行，而不是被框子裁掉几个字（见 _auto_wrap_hint）。
        keep_row = ttk.Frame(glob)
        keep_row.grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=(3, 5)
        )
        ttk.Spinbox(
            keep_row,
            from_=1,
            to=MAX_LOG_KEEP,
            textvariable=self.max_log_files_var,
            width=10,
        ).pack(side=tk.LEFT)
        _auto_wrap_hint(ttk.Label(
            keep_row,
            text="← 超出后从最旧的开始丢（下次启动游戏生效）",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        )).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))
        row += 1
        # ---- 删除方式 ----
        # ★ columnspan=3：这一帧有 3 列（标签 / 输入框 / 按钮），只盖 2 列的话
        #   分割线会在「按钮那一列的左边」断掉 —— 右边凭空少一截（量过：差 178 px）。
        ttk.Separator(glob, orient=tk.HORIZONTAL).grid(
            row=row, column=0, columnspan=3, sticky=tk.EW, pady=6
        )
        row += 1
        self.permanent_delete_var = tk.BooleanVar(
            value=self.config.get("permanent_delete")
        )
        ttk.Checkbutton(
            glob,
            text="删除文件时直接彻底删除（不进回收站，不可还原）",
            variable=self.permanent_delete_var,
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=3)
        row += 1
        _auto_wrap_hint(ttk.Label(
            glob,
            text="← 只作用于存档目录 / 旧日志这类用户文件",
            font=("Microsoft YaHei", 8),
            foreground="#777777",
        )).grid(row=row, column=1, columnspan=2, sticky=tk.EW, padx=5)
        glob.columnconfigure(1, weight=1)

        btn_frame = self.settings_footer
        # ★「保存」和「返回」合成一个按钮。以前是两个（「保存设置」+「返回
        #   主界面」），手快点成后者就等于白改 —— 用户报过好几次「改完发现
        #   设置没生效」。现在**离开设置页这件事本身就等于保存**，不存在
        #   「改完没保存就走了」这条路。
        # ★ 位置按惯例分两端：「重置默认」是次要（而且带破坏性）的操作，留在
        #   左边；「保存并返回」是**主操作**，钉在右下角 —— 对话框/设置页的
        #   确认键都在那儿，眼睛扫到右下角就知道点哪儿。两个都用同一个
        #   settings_footer（它 fill=X），所以 side=RIGHT 会一直贴到窗口右边。
        ttk.Button(
            btn_frame, text="重置默认", command=self.reset_settings
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame, text="保存并返回", command=self.save_and_return
        ).pack(side=tk.RIGHT, padx=5)

    def show_main(self) -> None:
        self.settings_frame.pack_forget()
        self.main_frame.pack(fill=tk.BOTH, expand=True)

    def _load_profile_settings_into_form(self) -> None:
        """把当前存档分类的设置灌进设置页表单。"""
        name = self.config.get_current_profile()
        self.settings_profile_var.set(name)
        self.save_path_var.set(self.config.get_current_save_path())
        self.min_playtime_var.set(
            self.config.get_profile_setting(name, "min_playtime")
        )
        self.max_backups_var.set(
            self.config.get_profile_setting(name, "max_backups")
        )
        self.auto_backup_var.set(
            self.config.get_profile_setting(name, "auto_backup")
        )
        # 全局项也重读一遍：这次打开设置页之前可能已经改过了
        self.hide_on_launch_var.set(self.config.get("hide_on_launch"))
        self.close_on_game_exit_var.set(
            self.config.get("close_on_game_exit")
        )
        self.github_mirror_var.set(self.config.get("github_mirror"))
        self.auto_update_var.set(self.config.get("auto_update"))
        self.launcher_update_var.set(
            launcher_update_label(self.config.get("launcher_update"))
        )
        self.extra_vm_var.set(self.config.get("extra_vm_args"))
        self.extra_prog_var.set(self.config.get("extra_program_args"))
        self.save_game_log_var.set(self.config.get("save_game_log"))
        self.max_log_files_var.set(self.config.get("max_log_files"))
        self.permanent_delete_var.set(self.config.get("permanent_delete"))
        self.jre_path_var.set(self._jre_path_for_form())
        # 上次「检测」的结果留着：关掉设置页再打开还得看得见，不然用户会以为
        # 又要重测一遍（重测要真起一次 JVM，没必要）。
        self._render_jre_check()

    def check_jre(self) -> None:
        """「检测」按钮：真跑一次 ``java -version``，确认这套 Java 能用。

        JRE、JDK 一样对待 —— 只认 ``bin\\java.exe`` 跑不跑得起来。
        只查文件在不在是不够的（0 字节的残留、缺 VC 运行库、被安全软件拦住
        都以「文件存在」的样子躺着），所以这里拉起来问一句。★ 起 JVM 要几百
        毫秒，一律丢后台线程 —— 界面上的「检测中…」是活的，不卡窗口。
        """
        text = self.jre_path_var.get().strip()
        if not text:
            text = ConfigManager.JVM_DEFAULTS["jre_path"]
        self.jre_check_var.set("⏳ 正在检测…")
        self._start_jre_probe(self._java_exe_of(text))

    def _start_jre_probe(self, java_exe: Path) -> None:
        """后台跑一次 java -version（结果由 _apply_jre_probe 回 GUI 线程显示）。"""
        self.executor.submit(self._jre_probe_task, java_exe)

    def _jre_probe_task(self, java_exe: Path) -> None:
        # 工作线程：只做「起进程 + 读输出」，绝不碰 tk 控件
        try:
            ok, text = probe_java(java_exe)
        except Exception as e:                                # noqa: BLE001
            # probe_java 自己不抛；真出了意料之外的事也不能让这条线程静默死掉
            logger.error(f"Java 检测异常: {e}")
            ok, text = False, f"检测出错：{e}"
        self.run_on_gui(lambda: self._apply_jre_probe(java_exe, ok, text))

    def _apply_jre_probe(self, java_exe: Path, ok: bool, text: str) -> None:
        self._jre_check = (str(java_exe), ok, text)
        self._render_jre_check()
        # 状态栏也报一句：保存后自动检测（人已经回到主界面了）就靠它
        self.set_status(
            f"{'✅' if ok else '❌'} Java{'可用' if ok else '不可用'}：{text}"
        )

    def _render_jre_check(self) -> None:
        """把最近一次检测结果画到设置页那一行上（换颜色 + 文案）。"""
        if self._jre_check is None:
            self.jre_check_var.set(
                "点右边「检测」跑一次 java -version，确认它真能跑"
            )
            color = "#777777"
        else:
            _, ok, text = self._jre_check
            self.jre_check_var.set(("✅ " if ok else "❌ ") + text)
            color = "#1a7f37" if ok else "#b3261e"
        try:
            self.jre_check_label.configure(foreground=color)
        except tk.TclError:                  # pragma: no cover - 控件已销毁
            pass

    def show_settings(self) -> None:
        if self._game_is_running():
            messagebox.showinfo("提示", "游戏运行中无法打开设置")
            return
        self._load_profile_settings_into_form()
        self.main_frame.pack_forget()
        self.settings_frame.pack(fill=tk.BOTH, expand=True)

    def save_and_return(self) -> None:
        """「保存并返回」按钮：保存成功才回主界面。

        ★ 校验没过要**留在设置页** —— 那时候切回去，用户看到的是「点了按钮
        什么都没发生」，跟以前那个「没生效」的坑一模一样。
        """
        if self.save_settings():
            self.show_main()

    def save_settings(self) -> bool:
        """把设置页的改动写进 config.json，返回是否真的存下来了。

        ``False`` = 被校验拦下（不合法的启动参数 / 镜像地址 / 保留份数），
        或者用户在「参数有风险」的确认框里选了「否」—— 两种情况都**什么都没改**。

        ★ 只管保存，不管跳转（那是 ``save_and_return`` 的事）：以前这里末尾
        直接 ``show_main()``，按钮也只能跟着叫「保存设置」，于是「保存」和
        「返回」分成了两个按钮，用户还老是点错那个不保存的。
        """
        name = self.config.get_current_profile()
        # 自定义启动参数先校验，而且要在**动任何配置之前** ——
        # 引号没闭合这种输入一定不是用户想要的，与其猜，不如当场说清。
        # 放最前面还能保证「报错了就什么都没改」，不会存下一半。
        try:
            extra_vm_text = self.extra_vm_var.get()
            extra_prog_text = self.extra_prog_var.get()
            extra_vm_args = split_args(extra_vm_text)
            split_args(extra_prog_text)
        except ValueError as e:
            logger.warning(f"启动参数校验失败: {e}")
            messagebox.showerror(
                "启动参数有问题",
                f"{e}\n\n"
                "参数按空格分隔；如果某一项本身含空格，用英文双引号把它包起来，"
                '例如 -Dfoo="a b"。',
            )
            return False
        # 镜像地址同样先校验：填了个不像地址的东西，多半是想留空或填错，
        # 与其默默退成「直连」（看起来像没生效），不如当场说清。
        mirror_text = self.github_mirror_var.get()
        mirror = normalize_mirror(mirror_text)
        if mirror_text.strip() and not mirror:
            logger.warning(f"镜像地址不合法: {mirror_text!r}")
            messagebox.showerror(
                "镜像地址有问题",
                f"「{mirror_text.strip()}」不像有效的镜像地址。\n\n"
                "填法是把加速前缀写全，例如 https://ghfast.top/\n"
                "留空表示直连 GitHub（不加速）。",
            )
            return False
        # Java 路径同样先看：这个目录里到底有没有 bin\java.exe。这里**只查
        # 文件在不在**（毫秒级）—— 「它能不能真跑起来」是「检测」按钮的事，
        # 保存时去起 JVM 会让按钮卡住几百毫秒。
        jre_text = self.jre_path_var.get().strip()
        if not jre_text:
            logger.warning("Java 路径为空")
            messagebox.showerror(
                "Java 路径有问题",
                "Java 路径不能为空。\n\n"
                "只写「jre」＝启动器旁边那个 jre 文件夹；"
                "也可以填 JRE / JDK 的绝对路径。",
            )
            return False
        jre_exe = self._java_exe_of(jre_text)
        if not jre_exe.exists():
            logger.warning(f"Java 路径下没有 java.exe: {jre_exe}")
            messagebox.showerror(
                "Java 路径有问题",
                f"这个路径下没有找到 java.exe：\n\n{jre_exe}\n\n"
                "要填的是 Java 的根目录（JRE 或 JDK 都行，里面有个 bin "
                "子目录），不是 bin 目录本身，也不是 java.exe 文件。",
            )
            return False
        try:
            max_log_files = normalize_log_keep(
                self.max_log_files_var.get(), what="日志保留份数"
            )
        except tk.TclError as e:
            logger.warning(f"日志保留份数不合法: {e}")
            messagebox.showerror(
                "日志保留份数有问题",
                f"要填 1 到 {MAX_LOG_KEEP} 之间的整数。",
            )
            return False
        warnings = check_extra_args(extra_vm_args)
        if warnings and not messagebox.askyesno(
            "启动参数可能有问题",
            "\n".join("· " + w for w in warnings) + "\n\n仍然保存吗？",
        ):
            return False
        new_path = self.save_path_var.get().strip()
        if new_path:
            try:
                self.config.set_profile_path(name, new_path, persist=False)
            except (KeyError, OSError) as e:
                logger.error(f"更新存档目录失败: {e}")
                messagebox.showerror("错误", f"更新存档目录失败: {e}")
                return False
        # 分类级
        try:
            self.config.set_profile_setting(
                name, "min_playtime", self.min_playtime_var.get()
            )
            self.config.set_profile_setting(
                name, "max_backups", self.max_backups_var.get()
            )
            self.config.set_profile_setting(
                name, "auto_backup", self.auto_backup_var.get()
            )
        except (KeyError, tk.TclError, ValueError) as e:
            logger.error(f"保存分类设置失败: {e}")
            messagebox.showerror("错误", f"保存失败: {e}")
            return False
        # 全局级
        old_mirror = self.config.get("github_mirror")
        old_jre = str(self.config.get_jvm("jre_path"))
        self.config.set("hide_on_launch", self.hide_on_launch_var.get())
        self.config.set(
            "close_on_game_exit", self.close_on_game_exit_var.get()
        )
        self.config.set("github_mirror", mirror)
        self.config.set("auto_update", self.auto_update_var.get())
        # 下拉框里是给人看的文案，存回配置要换回英文档位值；万一认不出
        # （理论上不会）就保持原值 —— 绝不写进去一个界面文案当档位。
        self.config.set(
            "launcher_update",
            LAUNCHER_UPDATE_VALUES.get(
                self.launcher_update_var.get(),
                self.config.get("launcher_update"),
            ),
        )
        self.config.set("extra_vm_args", extra_vm_text.strip())
        self.config.set("extra_program_args", extra_prog_text.strip())
        self.config.set("save_game_log", self.save_game_log_var.get())
        self.config.set("max_log_files", max_log_files)
        self.config.set(
            "permanent_delete", self.permanent_delete_var.get()
        )
        self.config.set_jvm("jre_path", jre_text)
        self.config.save()
        # ★ 用户刚在界面上指定了 Java 路径（而且上面校验过它真有 java.exe），
        #   所以本次启动的环境变量兜底作废 —— 不然界面写着 A、跑起来是 B。
        self._reset_java_override()
        self._refresh_profile_widgets()
        if extra_vm_args or extra_prog_text.strip():
            logger.info(
                "自定义启动参数已更新: JVM="
                f"{extra_vm_text.strip() or '（空）'}"
            )
        if mirror != old_mirror:
            logger.info(f"GitHub 镜像已更新: {mirror or '（直连）'}")
        if jre_text != old_jre:
            # 换了 Java 路径就顺手体检一次：几百毫秒的后台进程，结果落在状态栏
            # （这时人可能已经回到主界面了）。填了个「有 java.exe 但跑不起来」
            # 的目录，就是靠这一步当场发现的。
            logger.info(f"Java 路径已更新: {old_jre} -> {jre_text}")
            self._start_jre_probe(jre_exe)
        # ★ 成功时不弹窗了：按钮叫「保存并返回」，切回主界面本身就是反馈，
        #   状态栏那句「设置已保存」在回到主界面后看得见。
        self.set_status(f"✅ 存档「{name}」的设置已保存")
        return True

    def reset_settings(self) -> None:
        name = self.config.get_current_profile()
        if messagebox.askyesno(
            "确认重置",
            f"把「{name}」的备份策略、全局开关、Java 路径恢复默认值？\n"
            "（数据目录、存档分类列表和已有备份都不会被动）",
        ):
            self.min_playtime_var.set(
                ConfigManager.PROFILE_DEFAULTS["min_playtime"]
            )
            self.max_backups_var.set(
                ConfigManager.PROFILE_DEFAULTS["max_backups"]
            )
            self.auto_backup_var.set(
                ConfigManager.PROFILE_DEFAULTS["auto_backup"]
            )
            self.hide_on_launch_var.set(
                ConfigManager.GLOBAL_DEFAULTS["hide_on_launch"]
            )
            self.close_on_game_exit_var.set(
                ConfigManager.GLOBAL_DEFAULTS["close_on_game_exit"]
            )
            self.github_mirror_var.set(
                ConfigManager.GLOBAL_DEFAULTS["github_mirror"]
            )
            self.auto_update_var.set(
                ConfigManager.GLOBAL_DEFAULTS["auto_update"]
            )
            self.launcher_update_var.set(
                launcher_update_label(
                    ConfigManager.GLOBAL_DEFAULTS["launcher_update"]
                )
            )
            self.extra_vm_var.set(
                ConfigManager.GLOBAL_DEFAULTS["extra_vm_args"]
            )
            self.extra_prog_var.set(
                ConfigManager.GLOBAL_DEFAULTS["extra_program_args"]
            )
            self.save_game_log_var.set(
                ConfigManager.GLOBAL_DEFAULTS["save_game_log"]
            )
            self.max_log_files_var.set(
                ConfigManager.GLOBAL_DEFAULTS["max_log_files"]
            )
            self.permanent_delete_var.set(
                ConfigManager.GLOBAL_DEFAULTS["permanent_delete"]
            )
            self.jre_path_var.set(ConfigManager.JVM_DEFAULTS["jre_path"])
            self.set_status("已恢复默认值，点「保存并返回」以应用")

    def _browse_dir(self, var: tk.StringVar) -> None:
        current = var.get().strip()
        initial = current if current and Path(current).is_dir() else None
        path = filedialog.askdirectory(
            initialdir=initial or str(self.base_dir)
        )
        if path:
            var.set(path)
