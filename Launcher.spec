# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

用法（在项目根目录执行，`python` 必须是**带 tkinter** 的那个解释器）：
    python -m PyInstaller --clean --noconfirm Launcher.spec

  或者直接用现成的构建脚本（它会先挑一个能 `import tkinter` 的解释器）：
    python _tools/build.py

产物（由下面的 _ONEDIR 开关决定）：
    _ONEDIR = True   → dist/Mindustry启动器/  文件夹模式（推荐，启动快）
    _ONEDIR = False  → dist/Mindustry启动器.exe  单文件（慢，每次启动要解压）

★ 打包形态参照官方 Mindustry 的分发方式：**小 exe + 旁边放 jre/ 目录**。
官方那个包就是 `Mindustry.exe`(584KB) + `Mindustry.json` + `jre/`(110MB)。
jre 不放进包里的原因见下方 datas 注释。

两种形态下 exe 都必须和 jre/ 在同一目录：

    <任意目录>/
        Mindustry启动器      ← 这个包（单文件时是 .exe）
        _internal/          ← 文件夹模式才有，PyInstaller 的运行时
        jre/                ← 必须有（放在 exe 旁边）
        Mindustry.json      ← 有内置副本，外面放一份可覆盖
        config.json / versions/ / Backups/   ← 运行时按需生成

程序把「exe 所在目录」当作数据根，所以把整个目录挪到哪儿都行。

⚠️ 为什么默认用文件夹模式而不是单文件：
onefile 每次启动都要把约 1000 个文件解压到 %TEMP%\\_MEI*，退出时再删。
实测解压本身就要 2.4 秒，加上从解压目录加载模块和杀毒软件对新解的
上千个文件做实时扫描，整体要多花 5~6 秒。文件夹模式直接在原地读，
不需要解压，启动只要几百毫秒。
"""

import os
import sys

# ── 打包形态开关 ────────────────────────────────────────────────
# True  = 文件夹模式（启动快，推荐）
# False = 单文件模式（只有一个 exe，但每次启动要解压，慢 5~6 秒）
_ONEDIR = True

_NAME = "Mindustry启动器"

# ⚠️ Anaconda 把 tcl/tk 的 DLL 放在 Library/bin/ 而不是 DLLs/，
# PyInstaller 的依赖解析器找不到它们，于是 _tkinter.pyd 打进去了、
# 它依赖的 tcl86t.dll / tk86t.dll 却没打进去 —— 结果 exe 一启动就在
# `import tkinter` 处崩溃（windowed 模式还没有任何报错可见）。
# 这里显式补上依赖闭包：tcl86t → zlib.dll，tk86t → tcl86t。
# 用 sys.base_prefix 推导，换 conda 环境也能对上。
_CONDA_LIB = os.path.join(sys.base_prefix, "Library", "bin")

# anaconda 把一批运行库放在 Library/bin/，而 PyInstaller 的依赖解析器只看
# DLLs/ 和工作目录，于是 .pyd 打进去了、它们依赖的 .dll 却漏掉。症状有两种：
#   1. 启动即崩 —— _tkinter.pyd 缺 tcl86t/tk86t.dll，`import tkinter` 直接失败，
#      windowed 模式下连报错都看不见；
#   2. 用到才崩 —— _ctypes.pyd 缺 ffi.dll，而 ctypes 是函数内局部 import，
#      要等用户点「删除此分类」那一刻才会炸。
# 所以必须按依赖闭包显式补全，不能只补 tk 那两个。
_REQUIRED_DLLS = (
    "tcl86t.dll", "tk86t.dll",           # _tkinter 的直接依赖
    "zlib.dll",                          # 上面两个的依赖
    "ffi.dll",                           # _ctypes → 回收站删除
    "libmpdec-4.dll",                    # _decimal
    "liblzma.dll",                       # _lzma
    "libexpat.dll",                      # pyexpat
)

for _dll in _REQUIRED_DLLS:
    _p = os.path.join(_CONDA_LIB, _dll)
    if not os.path.exists(_p):
        raise SystemExit(
            f"打包中止：找不到 {_p}\n"
            f"anaconda 的运行库位置与预期不符，请检查 _CONDA_LIB。"
        )

datas = [
    # ⚠️ jre/ 故意不打包。它占 32MB，放进包里只会拖慢启动：
    # 单文件模式下每次启动都要解压，文件夹模式下白白多占体积。
    # 运行时 resource_path() 会优先找 exe 旁边的 jre/，所以只要它跟 exe
    # 放在一起就能被找到；想换 jre 版本直接替换那个目录，不用重新打包。
    #
    # ⚠️ Mindustry.json 也不再打包了：那几项（jrePath / mainClass / vmArgs）
    # 已经并进 config.json 的 jvm 段，缺了就用内置默认值，不再是必需文件。
    # 窗口图标还是打进包内：运行时 _setup_gui 用 resource_path() 找它，
    # 把 exe 挪到别处时窗口图标才不会丢。
    ("mindustry.ico", "."),
]

binaries = [(_p, ".") for _p in
            (os.path.join(_CONDA_LIB, d) for d in _REQUIRED_DLLS)]

a = Analysis(
    ["Launcher_Test_123.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy", "pandas", "matplotlib", "scipy", "PIL",
        "pytest", "IPython", "notebook", "sqlalchemy",
        "sklearn", "numba", "cython", "sympy",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

_EXE_KW = dict(
    name=_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="mindustry.ico",
)

if _ONEDIR:
    # 文件夹模式：exe 只带引导程序，依赖全放在旁边的 _internal/ 里。
    # 启动时原地读，不解压 —— 这是快的关键。
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_EXE_KW)
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=_NAME,
    )
else:
    # 单文件模式：所有东西塞进一个 exe，运行时解压到 %TEMP%\\_MEI*。
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **_EXE_KW)
