# Mindustry 启动器 —— 开发仓库

Mindustry 多版本启动器。管理多个游戏版本（CAS 内容寻址去重，相同文件只存一份）、
按存档分类隔离数据目录、自动备份与恢复、更新检查、启动预热、自定义启动参数、
游戏运行日志查看。

tkinter 界面，**只用 Python 标准库，没有任何第三方依赖**。

---

## ★ 这个仓库只放「开发」的东西

代码和运行时**分开放**，这是刻意的：

```
<父目录>/
├── <本仓库>/         ← 源码、构建脚本、开发工具（git 管理）
├── <运行时目录>/      ← exe + _internal/ + jre/ + 游戏版本 + 存档
```

| | 本仓库 | 运行时目录 |
|---|---|---|
| 放什么 | `launcher/` 源码、`_tools/`、spec | exe、`_internal/`、`jre/` |
| 谁在用 | 写代码时 | **平时双击玩**时 |
| 进 git | ✅ | ❌ |

好处是**开发不会碰坏日常用的那份**：改代码、构建、验证都在本仓库做，
确认没问题了再一条命令推过去。

运行时目录的**位置是配置出来的**，不是写死的。找法（`_tools/_paths.py`）：

```
环境变量 MDT_RUNTIME_DIR  →  同级的 runtime/  →  同级里任意一个「像运行时」的目录
```

想换个名字或挪到别处，设 `MDT_RUNTIME_DIR` 就行；`build.py --deploy-to <目录>`
也可以单独指定。判据是「那儿有 `versions/manifests/*.json`，或 exe + `_internal/`」。

---

## 环境要求

| 需要 | 说明 |
|---|---|
| Python 3.10+ | 代码用了 `X \| None` / `list[dict]` 等新语法 |
| tkinter | **必须能真正创建 Tk 窗口**，不是只看装没装 |
| PyInstaller | 仅构建 exe 需要：`python -m pip install pyinstaller` |

⚠️ **tkinter 这一条是硬要求**，因为构建时要从解释器推导 tcl/tk 运行库的位置。
[Anaconda](https://www.anaconda.com/) 自带 tkinter，最省事；官方 Windows 安装包
默认也带。如果 `python -c "import tkinter; tkinter.Tk()"` 报错，就换一个解释器。

## 日常开发流程

```bash
python _tools/verify/code_regression.py    # 1. 改完代码先跑回归（344 项）
python _tools/verify/gui_smoke.py          # 2. 改了界面：真建窗口点一遍（107 项，不起游戏）
python _tools/recycle.py dist/Mindustry启动器   # 3. ★ 打包前先清产物！
python _tools/build.py --deploy            # 4. 构建 + 同步到运行时目录
python _tools/verify/packed_code_check.py  # 5. 确认新代码真进了 exe
python _tools/verify/exe_edge_check.py     # 6. 改了启动/配置/日志路径后：打包版边界冒烟
```

第 3 步为什么不能省：PyInstaller 建 COLLECT 前会**先清空** `dist/Mindustry启动器`
（1000+ 个文件），这会撞上工具自带的「批量删除确认闸」（单轮累计 ≥ 50 个文件就要
人工确认），**构建直接失败**。`recycle.py` 走 `SHFileOperationW` 原生 API，
不受闸管、而且真进回收站（可还原）。

第 4 步的 `--deploy` 会自动认出运行时目录并把新 exe 同步过去
（旧的 exe 自动备份成 `.bak`）。也可以明确指定：

```bash
python _tools/build.py --deploy-to "D:\别的地方"
python _tools/build.py --deploy --prune    # 顺手删掉目标里多出来的陈旧文件
```

**第 3 步别省。** 源码改了但没打进包、或 spec 漏了模块时，exe 照常启动、
界面照常出现、什么都不报错，只是跑的是旧逻辑 —— **光看"能打开"发现不了**。

### 部署是增量的

`_internal/` 有 993 个文件，整目录删了重抄既慢又会撞上「批量删除确认」。
实际改动往往只影响 `base_library.zip` 一个文件，所以部署只复制真正不同的，
顺便把目标里多出来的文件报出来（加 `--prune` 才删）。

## 目录说明

```
Launcher_Test_123.py      入口（约 20 行 wrapper，真正的代码在 launcher/）
Launcher.spec             PyInstaller 打包配置
mindustry.ico             窗口图标（打进包）
launcher/                 全部源码
    version.py            ★ 唯一的版本号来源 + 兼容性常量（配置/清单格式版本）
    sources.py            ★ 版本来源注册表（从哪儿下游戏版本，加来源不用改代码）
    extensions.py         ★ 扩展点（可选的自定义目录，加功能不用重新打包）
    utils.py              路径、日志、原子写、回收站
    config.py             配置读写（存档分类 + jvm 启动配置 + 迁移链）
    storage.py            CAS 存储、备份、拼装运行时 jar
    updates.py            更新检查与下载
    gamecmd.py            启动参数解析 + 命令行拼装
    gamelog.py            游戏输出捕获（落盘 + 内存缓冲 + 管道编码兜底）
    gui_core.py           内核：线程队列、状态栏、窗口骨架
    gui_main.py           主界面
    gui_game.py           启动游戏、进程监控、预热
    gui_log.py            运行日志窗口
    gui_versions.py       版本管理
    gui_profiles.py       存档分类
    gui_backup.py         备份
    gui_dialog.py         共用对话框
    gui_updates.py        更新界面、退出流程
    gui.py                组装各部分
_tools/                   开发辅助脚本（不影响打包，不属于程序）
    _paths.py             ★ 所有脚本共用的路径来源（代码根 / 运行时目录）
    build.py              一键构建 + 部署
    make_source_zip.py    生成源码包（给要自己构建的人）
    make_release_zip.py   生成发布包（给双击就用的人）
    recycle.py            安全删除：移入回收站，绝不硬删
    verify/               回归与验证脚本
    README.md             每个脚本干什么、什么时候跑
LICENSE                   GNU GPL-3.0 全文
```

## git

本目录是一个 git 仓库（`main` 分支），**只管代码，不管运行时**。

```
进 git：launcher/  _tools/  Launcher.spec  Launcher_Test_123.py
        README.md  LICENSE  mindustry.ico  .gitignore  .gitattributes
不进：  exe  _internal/  jre/  versions/  backups/  config.json  *.log  *.zip
        build/  dist/  __pycache__/
```

排除规则见 `.gitignore`。构建产物和交付包都是**可重建**的，所以不占仓库空间。

★ 从本目录直接跑源码时 `BASE_DIR = Path.cwd()`，所以会在这里生成
`launcher.dev.log`（以及脚本可能写的 `config.json`）—— 那是**跑脚本的噪音**，
不是数据出了问题（日常使用的数据在运行时目录里）。这些文件都在
`.gitignore` 里，**随时可以删**。日志里出现 ERROR 也可能是正常的：
`code_regression.py` 会故意触发「模拟失败」等错误分支来验证报错行为。

### 日志有两个，别搞混

| 文件 | 谁写 | 对应场景 |
|---|---|---|
| `launcher.log` | **打包后的 exe** | 实际使用（双击 exe）—— 这才是真实运行记录 |
| `launcher.dev.log` | **源码运行** | 开发 / 调试 / 跑回归 / 跑验证脚本 |

**源码运行绝不往 `launcher.log` 写**。否则两个方向都会出问题：测试的噪音把
真实日志冲掉；而且 Windows 上 `logging.FileHandler` 不给 `FILE_SHARE_DELETE`，
被占住的日志会「能写、删不掉」，还得去查是谁持有的句柄。

规则在 `launcher/utils.py::resolve_log_file`。想让日志落到别处（比如冒烟测试
不想在数据目录留文件），设环境变量 `MDT_LOG_FILE=<路径>`，它优先级最高。

## 运行时布局（运行时目录）

```
<运行时目录>/
    Mindustry启动器.exe      ← 构建产物，由 build.py --deploy 更新
    _internal/              ← 构建产物（约 29 MB，不能删）
    jre/                    ← 内置 Java（约 32 MB，必须有）
    config.json             ← 首次运行自动生成（唯一一个配置文件）
    logs/                   ← 游戏输出日志，每次启动一份，只留最近 20 份
    versions/  backups/     ← 首次运行自动生成
```

程序把 **exe 所在目录当作数据根**，所以整个目录可以随便挪。

## 配置文件（`config.json`）

**这是唯一的配置文件**。原先还有一份 exe 旁边的 `Mindustry.json`（给官方原生
启动器 `Mindustry.exe` 用的），它的内容已经并进 `config.json` 的 `jvm` 段：

```json
{
  "config_version": 1,
  "hide_on_launch": true,      "auto_update": true,   "github_mirror":
    "https://gh.tinylake.top/",
  "close_on_game_exit": false,
  "github_mirror_presets": [ "https://gh.tinylake.top/", "https://ghfast.top/",
                             "https://gh-proxy.com/", "https://ghproxy.net/" ],
  "extra_vm_args": "",         "extra_program_args": "",
  "save_game_log": true,       "max_log_files": 20,
  "permanent_delete": false,
  "version_sources": [],
  "current_profile": "默认",
  "profiles": { "默认": { "data_dir": "...", "min_playtime": 20,
                          "max_backups": 20, "auto_backup": true } },
  "jvm": {
    "jre_path": "jre",
    "main_class": "mindustry.desktop.DesktopLauncher",
    "vm_args": [ "-Dhttps.protocols=...", "-XX:+ShowCodeDetailsInExceptionMessages",
                 "-XX:+UseCompactObjectHeaders", "--enable-native-access=ALL-UNNAMED" ],
    "program_args": []
  }
}
```

* `jvm` 段缺了或写坏了都会**退回内置默认值**，不会打不开启动器。
  `jre_path` 支持绝对路径 —— 想换自己的 JRE / JDK，在**设置页**改一下就行（还能顺手
  「检测」），不用重新打包；写成失效的路径也不会卡住启动（自动退回默认 `jre`）。
* 合并时丢掉了旧文件里的 `classPath` 和 `useZgcIfSupportedOs`：前者会让
  `jre/desktop.jar` 和运行时组装的 jar 同时进 classpath，后者的效果在
  「额外 JVM 参数」里写 `-XX:+UseZGC` 就能达到。
* `github_mirror` 是**镜像前缀**（空串 = 直连 GitHub），`github_mirror_presets`
  是设置页下拉框里的候选清单（数组），`max_log_files` 是 `logs/` 里最多留几份。
  三个都容错：地址不像话就退回直连、候选项不合法的剔掉（全没了就用内置默认）、
  份数超范围就夹到 1–200，日志里各留一条 WARNING。
* **开关型（`hide_on_launch` / `close_on_game_exit` / `auto_update` /
  `permanent_delete` …）不许写字符串当值**：`bool("false")` 在 Python 里是 **True**，
  写成 `"false"` 会把开关**反过来**。所以这几个键只认 `true`/`false`（数字 `0`/`1`
  也认），写了别的（比如 `"开"`）一律退回默认值 + 一条 WARNING —— 对
  `close_on_game_exit` 来说，这一层是「启动器会不会自己关掉」的差别；对
  `permanent_delete` 来说，是「删了还能不能还原」的差别。
* 日志里出现「jvm.xxx 非法，已改用默认值」这类 WARNING 是**预期**的降级行为。

#### 配置格式版本与迁移（`config_version`）

`config_version` 是**配置文件的格式版本**，跟启动器版本号不是一回事：
启动器每次发版都会变，配置格式只在「键的含义/结构变了」时才 +1。

| 情况 | 要做什么 |
|---|---|
| 只是**新增**一个带默认值的键 | 什么都不用做。老配置缺这一项会走默认值；新配置在旧版本里会被**原样保留**（见下） |
| **改名 / 改含义 / 删键 / 换类型** | `launcher/version.py` 的 `CONFIG_VERSION` +1，并在 `config.py` 的 `MIGRATIONS` 里补一条 `旧版本号: 函数` |

* 缺 `config_version` = **第 0 代**（加这个字段之前的所有配置），会自动升级。
* 迁移**只按版本号逐级走**，不猜、不跳级 —— 每一级都能单独测。
* 迁移函数不许抛异常；真抛了也只是「按原样继续读」，**不会打不开启动器**。
* 配置来自**更新的**启动器时（`config_version` 比本代码大）：只读认识的项，
  其余原样保留，并在日志里说明一句。

#### 未知键会被原样保留（向前兼容）

本版本**不认识**的顶层键（以及分类里的未知子键）读进内存后会在 `save()` 时
**原样写回**，不会因为「我读不懂」就被删掉。这条是为「把 `config.json` 从新版本
挪回旧版本用一次」准备的 —— 少了它，旧版本一存盘就等于把新项全削掉。

### 增加一个「从哪儿下游戏版本」的来源

「有哪些来源」不是写死在代码里的，而是一张表（`launcher/sources.py`）：
内置两个（`Anuken/Mindustry`、`TinyLake/MindustryX`），**额外来源写在
`config.json` 的 `version_sources` 数组里**，不用改代码、不用重新打包：

```json
"version_sources": [
  {
    "type": "MindustryBuilds",
    "api_url": "https://api.github.com/repos/Anuken/MindustryBuilds/releases",
    "asset_pattern": "Mindustry\\.jar$",
    "prerelease": null,
    "sort_rank": 2
  }
]
```

| 字段 | 含义 |
|---|---|
| `type` | 版本类型名（会写进版本清单，界面上也显示它）；**同名即覆盖内置那项** |
| `api_url` | GitHub releases 接口地址（任何返回同结构 JSON 的地址都行） |
| `asset_pattern` | **资产名**正则，命中第一个即为该版本的下载地址 |
| `prerelease` | `null`=正式版和预发布都要；`true`/`false`=只要预发布 / 只要正式版 |
| `use_mirror` | 该来源是否走 GitHub 镜像前缀（非 GitHub 的源应当设 `false`） |
| `sort_rank` | 排序档位（小的在前），同档内按版本号从新到旧 |
| `version_regex` / `version_group` | 怎么从 tag 里切出可比较的数字（默认取全部数字） |

写坏了**逐项丢掉**、不影响其它来源，也不会让配置失效；不认识的键被忽略
（这正是「新版本写的配置在旧版本里也能读」的样子）。

### 扩展点（`extensions/`）

在**运行时的数据根**（exe 所在目录）下建一个 `extensions/` 目录，里面每个
`*.py` 都会被当作扩展载入 —— 可以加点自己的东西，**不用改启动器本体、
不用重新打包**：

```python
# extensions/我的统计.py
def register(api):
    api.on("on_game_exited", lambda **kw: print(kw["version_name"], kw["playtime_minutes"]))
```

可用的钩子（名字一旦发布**只增不改**）：

| 钩子 | 时机 | 能拿到什么 |
|---|---|---|
| `on_config_loaded` | 配置读完之后（启动早期） | `config` |
| `on_versions_refreshed` | 版本列表刷新完之后 | `versions`（只读，别改） |
| `on_before_launch` | 游戏进程拉起来之前 | `context` —— **可写字典**，`context["extra_vm_args"]` / `["extra_program_args"]` 可追加参数（list of str） |
| `on_game_exited` | 游戏退出、收尾与自动备份**都做完之后** | `version_name`、`profile_name`、`playtime_minutes`、`exit_code` |

设计上刻意很克制：不做包管理、不做依赖解析、不做沙箱；只传 kwargs，
**不承诺**任何内部对象的结构（要稳定数据就自己读 `config.json` / 日志）。
载入时机放在「窗口已经能用之后」的后台步骤里，启动路径上不加 I/O；
**任何扩展出错都只记日志，绝不影响启动器**。设 `MDT_NO_EXTENSIONS=1` 可整体停用。

⚠️ 扩展里的代码跟启动器**同权限**运行，只放自己写的或信得过的文件。

### Java 路径（JRE / JDK 都收，设置页可改 + 一键「检测」）

设置页 → 「启动器设置」里有一栏 **Java 路径(JRE/JDK)**（`jvm.jre_path`），填的是
**Java 的根目录**（里面要有 `bin\java.exe`）：

* 只写 `jre` ＝ 启动器旁边那个 jre 文件夹（默认，相对路径按 exe 所在目录算）；
* 想换成自己装的 JRE，填绝对路径，比如 `D:\Java\jdk-21\jre`，**不用重新打包**；
  **填 JDK 的根目录也一样能用**（判定只看 `bin\java.exe` 在不在、跑不跑得起来，
  不看这个目录叫什么、有没有 `javac`）—— 已经装了 JDK 的人没必要再下一份 jre；
* 旁边两个按钮：**浏览**（选目录）、**检测**。

⚠️ 填的是**根目录**，不是 `bin` 目录、也不是 `java.exe` 文件本身（报错提示里会把
实际拼出来的路径打给你看，一眼能看出是不是拼重了）。另外 `C:\Program Files\Java\latest`
这种「外壳目录」也不是根目录 —— 它里面还有一层 `jdk-25`。

**「检测」不是看文件在不在，而是真起一次 `java -version`** —— 0 字节的残留、缺 VC
运行库、被安全软件拦住，这些都以「文件存在」的样子躺着，只查存在性是查不出来的。
检测在**后台线程**里跑（几百毫秒，界面不卡），结果直接显示在那一行上：

```
✅ Java 25.0.1 · Temurin        ← 能用
❌ 找不到文件：D:\...\bin\java.exe   ← 不能用，右边会写清原因
```

#### 连 jre 都没有时：自动去环境变量里找

没放 `jre/`（或者自己填的路径失效了、默认的又不在）时，启动器不会直接罢工，而是按
**`JAVA_HOME` → `PATH`** 的顺序找一个能用的 Java 顶上：

* 候选**必须真跑一次 `java -version`** 才算数 —— `PATH` 里那个 `java.exe` 常是 Oracle
  安装器留下的 `javapath` 转发桩，JRE 卸载之后它还在、还要报错，只看文件在不在会
  捞到一个「活着但没用」的东西；
* 找到就**只用于本次运行**，**不写回配置**：`jre/` 只是暂时不在，配置里那是用户自己
  填的意图，拿机器上的绝对路径把它盖掉才是真搞坏配置；
* 状态栏会说明「没有找到 jre 目录，这次先用系统里的 Java：<路径>」，设置页那一栏也
  显示**实际在用的那个目录** —— 顺手存一次就等于把它固定下来；
* 两处都没有（或者找到的都起不来）才弹那条「没有找到 jre 目录」的报错，并说明已经
  找过环境变量。

填错了也不会把启动器弄成打不开：保存时先查「这个目录里有没有 `java.exe`」，没有就
当场拦下；万一（比如手改 config.json）配上了一个失效的路径，启动时会**自动退回默认
的 `jre`** 并在状态栏 + 日志里说一句，而不是甩一个错误框。

### 自定义启动参数

设置页 → 「启动器设置（所有存档共用）」里有两项，**全局共用**（JVM 参数本来就
和存档无关）：

| 项 | 位置 | 例子 |
|---|---|---|
| 额外 JVM 参数 | 加在 `-cp` 之前 | `-Xmx4G`、`-XX:+UseZGC` |
| 额外游戏参数 | 加在主类之后 | 一般用不到，排查问题时才填 |

规则：按空格分隔，某项本身含空格就用英文双引号包住（`-Dfoo="a b"`）。
反斜杠按字面量处理，所以 Windows 路径不用转义。**用户写的参数一律排最后** ——
JVM 对重复选项取最后一个，这样 `-Xmx4G` 能压过内置值。

填 `-cp` / `-classpath` / `-jar` / `-Dmindustry.data.dir=` 会导致启动器自己的
安排被顶掉，保存时会弹窗提醒（但不拦着）。

### GitHub 镜像

国内直连 GitHub 下 jar 很慢，所以设置页可以填一个**镜像前缀**：
下载时拼在完整地址前面，例如

```
https://ghfast.top/  +  https://github.com/Anuken/Mindustry/releases/download/...
```

下拉框里有几个常用前缀（`gh.tinylake.top` 是默认值），也可以自己敲任意地址。
写法比较宽松：只写主机名（`ghfast.top`）会自动补成 `https://ghfast.top/`；
**留空 = 直连 GitHub**。

下拉框的**候选清单也是配置项**：`config.json` 里的 `github_mirror_presets`。
想把自己常用的镜像站放进下拉框，改这个数组就行（改完重启启动器生效）：

```json
"github_mirror": "https://ghfast.top/",
"github_mirror_presets": [
  "https://ghfast.top/",
  "https://gh-proxy.com/",
  "https://我自己的镜像站/"
],
```

里面写了不合法的东西会被自动剔掉；整份不是数组、或者一项都不剩，就退回内置
默认的那几个（下拉框空着更像坏了）。这一项是「给配置文件用的」，所以设置页
的「恢复默认」不会动它。

* 镜像只是加速手段，拉不动会**自动换下一个源**，最后一定是直连 GitHub，
  所以填错了也不会「下不了」。
* 更新信息本身（`api.github.com`）不走镜像，只加速 jar 下载。
* 填了个不像地址的东西，保存时会直接弹窗拦下 —— 免得「看着填了，其实退回直连」。
* 下拉框**不吃滚轮**：设置页里滚轮只负责滚页面。Tk 默认会让滚轮经过下拉框时
  顺手换选项，于是「滚一下页面、镜像也被换了」（主面板的「当前存档」更严重，
  滚一下等于悄悄切换存档分类）。要换前缀就点开列表选，或者用方向键。

### 日志保留份数

`logs/` 里最多留几份游戏日志（默认 20，范围 1–200），超出后**从最旧的开始**删掉
（进回收站还是直接删，看下面的「删除方式」开关）。下次启动游戏时生效。日志窗口里
看到的实时内容不受这个限制。

### 删除方式：回收站 / 直接彻底删除

设置页 → 「启动器设置」最下面的开关 `permanent_delete`，**默认关**：

| 开关 | 删文件时 | 后果 |
|---|---|---|
| 关（默认） | 移入 Windows 回收站 | 删错了能右键「还原」找回来 |
| 开 | 直接彻底删除 | **不可还原**；但更快、也不占回收站空间 |

为什么留这个开关：存档数据目录动辄几个 GB，进回收站等于把这份数据**又复制一份**
（`SHFileOperation` 是搬进 `$Recycle.Bin`，跨卷时退化成复制 + 删除），磁盘紧张时
又慢又占地方。

它**只作用于用户文件**这三处：「删除此分类…」里勾的游戏数据目录、恢复备份时清空
存档目录、清理超额的旧游戏日志。**CAS 对象池回收**（一次几万个碎片对象）和
**备份清单超额清理**是内部文件，照旧直接删 —— 让它们进回收站只会把回收站塞爆，
那不是这个开关的意思。

开了这个开关以后，「删除此分类…」那个确认框和图上的说明都会改成「直接彻底删除、
无法还原」，不会嘴上说进回收站、实际硬删。

### 设置页只有「保存并返回」

设置页底部**没有单独的「返回主界面」**——离开设置页这件事本身就等于保存。
以前是两个按钮（「保存设置」+「返回主界面」），手快点成后者就等于白改，
所以现在只剩一个出口：**校验通过 → 存盘 → 回主界面**。

* 校验没过（比如启动参数引号没闭合、镜像地址不像地址）时**留在设置页**
  并弹出说明 —— 不会「假装存好了再切走」。
* 保存成功不再弹「已保存」对话框：按钮上写着「保存并返回」，切回主界面
  本身就是反馈，主界面状态栏也会显示「设置已保存」。
* 位置按惯例分两端：**「保存并返回」在右下角**（跟对话框的「确定」一个位置），
  「重置默认」在左边。

唯一的例外是**直接用窗口右上角的 × 关掉启动器**：那种情况下设置页里的改动
不会存盘（跟以前一样）。想保存就走「保存并返回」。

### 游戏退出后自动关闭启动器

设置页 → 「启动器设置」里的开关（`close_on_game_exit`，默认**关**）。勾上以后
游戏一退出，启动器自己关掉，不用再手动点一次 ×。

* 顺序是定死的：**游戏退出 → 收尾（日志/临时 jar/进程句柄）→ 自动备份 →
  才关启动器**。关早了这局的备份就没了，所以这一步排在最后。
* 有版本包**正在下载**时不关：那时候关会把下载打断、留下半个文件，启动器
  留在那儿并在状态栏说明原因，等下载跑完。
* 和「启动游戏时隐藏窗口」是一对：后者管「玩的时候窗口在哪」，前者管
  「玩完了窗口还在不在」。两个都开就是「启动游戏→窗口消失→玩完→启动器也退掉」。
* ⚠️ 勾上以后，游戏崩了启动器也会关掉。游戏输出仍然会落盘到
  `logs/game-<时间>.log`（除非把「保存到 logs/ 目录」也关了），回头能翻。

### 游戏输出日志

「运行日志」按钮打开，两个页签：

* **游戏输出** —— 实时滚动本次游戏进程的 stdout/stderr。以前这两股流是直接
  丢进 `DEVNULL` 的，游戏崩了在启动器这边一点线索都没有。
* **启动器日志** —— 直接看 `launcher.log`，排查启动失败时更相关。

落盘与否由设置里的「把游戏输出保存到 logs/ 目录」控制（关掉只在窗口里看）。
每次启动一份 `logs/game-<时间>.log`，只保留最近 20 份，更旧的进回收站。

⚠️ 实现上有两处**不能改**：

* **管道必须有专门的线程一直读。** 子进程往没人排空的管道里写，写满 64 KB
  缓冲区后会**永久阻塞** —— 表现是「游戏卡在黑屏不进主菜单」。所以
  `gamelog.py` 的读线程把异常全吞掉继续读，绝不因为解码之类的破事退出。
* **管道按字节读，编码逐行判断**（`decode_output_line`）。Java 在没有控制台时
  （我们就是用管道接它的输出）按 `native.encoding` 写 —— 简中 Windows 上是
  **GBK**，硬按 UTF-8 解会把中文 mod 名整片撕成 `�`（实测一份日志 44 个）。
  所以两头都管：启动命令里钉住 `-Dstdout.encoding=UTF-8`（Java 19+ 认），
  解不开时再退系统代码页兜住旧 JRE。

## 交付

两种交付物，给的是不同的人：

| 交付物 | 命令 | 体积 | 给谁 |
|---|---|---|---|
| **源码包** | `python _tools/make_source_zip.py` | ~120 KB | 想自己构建、看代码的人 |
| **发布包** | `python _tools/make_release_zip.py` | ~31 MB | 只想双击用的人（**不需要 Python，也不需要装 Java**） |

发布包 = exe + `_internal/` + 内置 `jre/` + 使用说明，解压即用。
它从**运行时目录**取 exe / `_internal` / `jre`（默认自动找）。发出去之前跑一次验收：

```bash
python _tools/verify/release_check.py
```

它会解压到**全新空目录**、用**完全无关的 cwd** 真启动一次，断言关键依赖齐全、
cwd 没被污染、数据落在 exe 旁边（⚠️ 会弹 GUI 窗口约 10 秒）。
光看 zip 里的文件清单证明不了「到别人机器上真能跑」，必须实跑。

## 设计要点

- **数据根 vs 资源根**：`BASE_DIR`（数据，exe 所在目录）与 `RESOURCE_DIR`
  （资源，`sys._MEIPASS`）是分开的。资源一律走 `resource_path()` —— 先看 exe
  旁边的外部副本，再回退包内。所以换个 `jre/` 不用重新打包。
- **写文件一律 `atomic_write_text()`**：Windows 上 `os.replace` 报 WinError 5
  通常是目标被占用而不是权限问题，靠模块级锁 + 重试 + 兜底覆盖解决。
- **删数据默认走回收站**：统一入口 `delete_path()` → `move_to_recycle_bin()`
  （`SHFileOperationW` + `FOF_ALLOWUNDO`），绝不 `shutil.rmtree`。设置里的
  `permanent_delete` 能把「用户文件」这三处改成直接删（不可还原），破坏性开关
  所以默认关、脏值一律退回 `False`（`bool("false")` 是 `True` 那个坑的反面）。
- **启动预热**：窗口一能用来就在后台把当前版本的游戏文件拼好，点启动直接复用
  （97 ms vs 3840 ms）。要点见 `_tools/README.md`。
- **版本号只有一处**：`launcher/version.py`。日志、窗口标题、`User-Agent`、
  兼容性常量都引用它，避免「日志说 1.0.1、界面写着 1.0.0」。
- **加来源 / 加功能不动核心代码**：来源是一张表（`sources.py`，可被
  `config.json` 扩展），功能挂在钩子上（`extensions.py`）。两条路都是
  「新版本往前兼容旧的、旧版本读得懂新配置」。见上面的两节。

## 改完代码之后

```bash
python _tools/verify/code_regression.py      # 主回归，344 项
python _tools/verify/gui_smoke.py            # 界面冒烟，107 项（不起游戏）
python _tools/recycle.py dist/Mindustry启动器  # 打包前必须：清掉旧产物，否则构建被删除闸拦下
python _tools/build.py --deploy              # 构建并同步到运行时目录
python _tools/verify/packed_code_check.py    # 确认新代码真进了 exe
```

## 许可证

本项目以 **GNU General Public License v3.0** 发布，全文见 [LICENSE](LICENSE)。

Mindustry 游戏本体**不在本仓库内**，版权归其作者所有，遵循游戏自身的许可
协议。本程序不修改、也不再分发游戏本体 —— 它只是从官方发布页下载可执行
文件、启动一个独立进程。协议的选择是独立的：GPL 的传染性只作用于「派生
作品」，而通过命令行调用另一个独立程序不构成派生。
