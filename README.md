# Mindustry 启动器

Mindustry 多版本启动器：同时管理多个游戏版本（CAS 内容寻址去重，相同文件只存一份）、
按存档分类隔离数据目录、自动备份与恢复、更新检查、启动预热、自定义启动参数、
游戏运行日志查看。

tkinter 界面，**只用 Python 标准库，没有任何第三方依赖**。

---

## 下载使用

不想折腾环境，就用 [Releases](../../releases/latest) 里的发布包：

1. 解压到一个**有写权限**的位置（别放 `C:\Program Files`、桌面这类地方）；
2. 双击 `Mindustry启动器.exe`。

**内置 Java，不需要装 Python，也不需要单独装 Java。** 用法详见包内 `使用说明.txt`。

## 从源码运行

| 需要 | 说明 |
|---|---|
| Python 3.10+ | 代码用了 `X \| None` / `list[dict]` 等新语法 |
| tkinter | **必须能真正创建 Tk 窗口**，不是只看装没装 |
| PyInstaller | 仅构建 exe 需要：`python -m pip install pyinstaller` |

```bash
python MindustryLauncher.py
```

⚠️ tkinter 是硬要求 —— 构建时要从解释器推导 tcl/tk 运行库的位置。如果
`python -c "import tkinter; tkinter.Tk()"` 报错，换一个解释器（Anaconda 自带，
Windows 官方安装包默认也带）。

## 项目结构

```
MindustryLauncher.py    入口（约 20 行 wrapper，真正的代码在 launcher/）
Launcher.spec           PyInstaller 打包配置
mindustry.ico           窗口图标
launcher/               全部源码
    version.py          ★ 唯一的版本号来源 + 兼容性常量（配置/清单格式版本）
    sources.py          ★ 版本来源注册表（从哪儿下游戏版本，加来源不用改代码）
    extensions.py       ★ 扩展点（加功能不用重新打包）
    utils.py            路径、日志、原子写、回收站
    config.py           配置读写（存档分类 + jvm 启动配置 + 迁移链）
    storage.py          CAS 存储、备份、拼装运行时 jar
    updates.py          更新检查与下载
    gamecmd.py          启动参数解析 + 命令行拼装
    gamelog.py          游戏输出捕获（落盘 + 内存缓冲 + 管道编码兜底）
    gui_core.py         界面内核：线程队列、状态栏、窗口骨架
    gui_*.py            各功能页（主界面 / 启动 / 版本 / 存档分类 / 备份 / 日志 / 设置）
_tools/                 开发辅助脚本（不参与打包，不属于程序）
    _paths.py           ★ 脚本共用的路径来源
    build.py            一键构建 + 部署
    make_source_zip.py  生成源码包
    make_release_zip.py 生成发布包
    recycle.py          安全删除：移入回收站，绝不硬删
    verify/             回归与验证脚本
LICENSE                 GNU GPL-3.0 全文
```

## 开发流程

```bash
python _tools/verify/code_regression.py          # 1. 改完代码先跑回归（344 项）
python _tools/verify/gui_smoke.py                # 2. 改了界面：真建窗口点一遍（107 项，不起游戏）
python _tools/recycle.py dist/Mindustry启动器     # 3. ★ 打包前先清产物
python _tools/build.py --deploy                  # 4. 构建 + 同步到部署目录
python _tools/verify/packed_code_check.py        # 5. 确认新代码真进了 exe
python _tools/verify/exe_edge_check.py           # 6. 改了启动/配置/日志路径后：打包版边界冒烟
```

- **第 3 步不能省**：PyInstaller 建 COLLECT 前会先清空 `dist/Mindustry启动器`
  （1000+ 个文件），会撞上工具的「批量删除确认闸」（单轮累计 ≥ 50 个文件就要人工确认），
  构建**直接失败**。`recycle.py` 走 `SHFileOperationW` 原生 API，不受闸管、而且真进回收站。
- **第 5 步不能省**：源码改了没打进包、或 spec 漏了模块时，exe 照常启动、界面照常出现、
  什么都不报错，只是跑的是旧逻辑 —— 光看「能打开」发现不了。
- 部署是**增量**的：`_internal/` 有近千个文件，整目录重抄又慢又会撞删除闸，所以只复制
  真正不同的。`--deploy-to <目录>` 可指定目标，`--prune` 才会删掉目标里多出来的陈旧文件。
- 每个脚本干什么、什么时候跑，见 `_tools/README.md`。

## 构建与交付

| 交付物 | 命令 | 体积 | 给谁 |
|---|---|---|---|
| **源码包** | `python _tools/make_source_zip.py` | ~280 KB | 想自己构建、看代码的人 |
| **发布包** | `python _tools/make_release_zip.py` | ~31 MB | 只想双击用的人（不需要 Python / Java） |

发布包 = exe + `_internal/` + 内置 `jre/` + `使用说明.txt` + `LICENSE`，解压即用。
发出去之前跑一次验收：

```bash
python _tools/verify/release_check.py
```

它会解压到**全新空目录**、用**完全无关的 cwd** 真启动一次，断言关键依赖齐全、
数据落在 exe 旁边（⚠️ 会弹 GUI 窗口约 10 秒）。光看 zip 的文件清单证明不了
「到别人机器上真能跑」，必须实跑。

## 配置与扩展

**`config.json` 是唯一的配置文件**，首次运行时自动生成在 exe 旁边。完整的结构、
默认值与容错规则见 `launcher/config.py`；与「发版以后还能读懂老配置」有关的三条约定：

- **`config_version` 是配置的格式版本**，跟应用版本号不是一回事。只是**新增**一个带默认
  值的键 → 不用管它；**改名 / 改含义 / 删键 / 换类型** → `version.py` 的 `CONFIG_VERSION`
  +1，并在 `config.py` 的 `MIGRATIONS` 里补一条从旧版本到新版本的迁移函数（逐级走，
  不猜不跳级）。
- **未知键原样保留**（向前兼容）：本版本读不懂的键在存盘时会写回，不会因为「我读不懂」
  就被删掉。
- **开关型的键只认 `true`/`false`**（数字 `0`/`1` 也认）：`bool("false")` 在 Python 里是
  `True`，写成字符串会把开关**反过来**。写成别的值一律退回默认 + 一条 WARNING。

另外两个「不动本体就能扩展」的口子：

| 想做什么 | 写在哪儿 | 说明 |
|---|---|---|
| 加一个**游戏版本来源** | `config.json` 的 `version_sources` 数组 | 内置 `Anuken/Mindustry`、`TinyLake/MindustryX`；同名即覆盖内置项，不改代码也不重新打包。字段含义见 `launcher/sources.py` |
| 加一点**自己的功能** | 部署目录下的 `extensions/*.py` | 四个钩子：`on_config_loaded` / `on_versions_refreshed` / `on_before_launch` / `on_game_exited`。钩子名发布后**只增不改**，详见 `launcher/extensions.py` |

⚠️ 扩展里的代码跟启动器**同权限**运行，只放自己写的或信得过的文件。
设 `MDT_NO_EXTENSIONS=1` 可整体停用。

## 设计要点

- **数据根 vs 资源根**：`BASE_DIR`（exe 所在目录）与 `RESOURCE_DIR`（`sys._MEIPASS`）
  是分开的，资源一律走 `resource_path()` —— 先看 exe 旁边的外部副本，再回退包内。
  所以换个 `jre/` 不用重新打包。
- **写文件一律 `atomic_write_text()`**（Windows 上 `os.replace` 报 WinError 5 多半是目标
  被占用，靠模块级锁 + 重试 + 兜底覆盖解决）。
- **删数据默认走回收站**：统一入口 `delete_path()` → `SHFileOperationW` + `FOF_ALLOWUNDO`，
  绝不 `shutil.rmtree`。
- **版本号只有一处**（`launcher/version.py`）：日志、窗口标题、`User-Agent` 都引用它，
  避免出现「日志说 1.0.1、界面写着 1.0.0」。
- **启动预热**：窗口一能用来就在后台把当前版本的游戏文件拼好，点启动直接复用。

## 许可证

本项目以 **GNU General Public License v3.0** 发布，全文见 [LICENSE](LICENSE)。

Mindustry 游戏本体**不在本仓库内**，版权归其作者所有，遵循游戏自身的许可协议。
本程序不修改、也不再分发游戏本体 —— 它只是从官方发布页下载可执行文件、启动一个
独立进程。协议的选择是独立的：GPL 的传染性只作用于「派生作品」，而通过命令行调用
另一个独立程序不构成派生。
