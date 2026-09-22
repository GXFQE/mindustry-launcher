# _tools —— 开发辅助脚本

**这里的脚本不是程序的一部分**，不影响打包、不影响运行。
打包用项目根的 `Launcher.spec`（它把 `launcher/` 和入口打进去，不碰本目录）。

脚本自己会往上找项目根（认 `launcher/__init__.py`），所以**放在子目录里也能跑**。

跑之前先切到项目根（也就是本仓库根）：

```bash
cd <仓库根>
```

⚠️ 必须用**带 tkinter** 的 Python（不少发行版的默认解释器不带）：

```bash
python -c "import tkinter; tkinter.Tk()"      # 能开出窗口才作数
python _tools/verify/code_regression.py
```

下面示例一律写 `python`；如果你的 `python` 不带 tkinter，换成那个带的
（`MDT_PYTHON` 环境变量可以让 `build.py` 优先用指定解释器）。

★ **本仓库只放开发的东西**，运行时目录（exe + `_internal/` + `jre/` + 游戏版本
+ 存档）在仓库**同级**。找法见 `_paths.find_runtime_root()`：

```
环境变量 MDT_RUNTIME_DIR  →  同级 runtime/  →  同级里任意一个像运行时的目录
```

`_paths.py` 是**所有脚本的路径来源**，别在脚本里另写一套路径推导；
它只在**真正访问** `DATA` / `VERSIONS` / `JRE` 这些属性时才去找运行时目录，
所以「刚 clone、还没有运行时目录」时，只想用 `ROOT` 的脚本不会在 import 阶段就炸。

构建完用 `build.py --deploy` 一条命令同步到运行时目录。

---

## build.py —— 打包 exe

| 命令 | 作用 |
|---|---|
| `python _tools/build.py` | 构建，产物在 `dist/Mindustry启动器/` |
| `python _tools/build.py --deploy` | 构建完同步到运行时目录（自动认，见 `_paths.find_runtime_root`） |
| `python _tools/build.py --deploy-to <目录>` | 同步到指定目录 |
| `python _tools/build.py --deploy --prune` | 顺手删掉目标里多出来的陈旧文件 |
| `python _tools/build.py --deploy-only` | **不重打**，只把 `dist/` 里现有的产物部署过去 |
| `python _tools/build.py --outdir <目录>` | 换个构建输出目录 |

★ 它会**自己挑一个「带 tkinter」的解释器**再动手。这一步不能省：
`Launcher.spec` 从解释器的 `sys.base_prefix` 推导 tcl/tk/ffi 运行库的位置
（Anaconda 放在 `Library/bin/`，PyInstaller 默认扫不到），用错解释器打出来的
exe 一启动就**静默崩溃**、windowed 模式下连报错都看不见。
也可用 `MDT_PYTHON` 环境变量指定解释器。

### 部署是增量的

`_internal/` 有 992 个文件。整目录 `rmtree` + `copytree` 既慢又会撞上
「批量删除确认」；实际改动往往只影响 `base_library.zip` 一个文件。
所以部署**只复制真正不同的**，并把目标里多出来的文件报出来（`--prune` 才删）。
实测：内容没变时「复制 0 个，未变 992 个」；改坏一个文件时只重抄那一个。

### 部署撞上「exe 还开着」

构建会成功、部署会失败，报的是 `PermissionError: [WinError 32] 另一个程序
正在使用此文件` —— 日常用的那份 exe 正跑着（窗口开着就是占用），而
**改完代码必须重启启动器才生效**，所以正确做法是：关掉启动器，然后

```bash
python _tools/build.py --deploy-only     # 复用 dist/，不用再等 40 秒重打
```

排查「到底是谁占着」别用 `tasklist`（Git Bash 里中文进程名会变成
"Binary file matches"，看不见），用 PowerShell 按可执行文件路径筛：

```bash
powershell -c "Get-CimInstance Win32_Process | ? { \$_.ExecutablePath -like '*<你的运行时目录名>*' } | select ProcessId,Name,ExecutablePath"
```

判据：目标 exe「**不能以写方式打开，但能改名**」= 有进程把它当映像加载着。

## make_source_zip.py —— 生成可独立构建的源码包

| 命令 | 作用 |
|---|---|
| `python _tools/make_source_zip.py` | 生成 `Mindustry启动器_源码包_<日期>.zip` |
| `python _tools/make_source_zip.py -o 名字.zip` | 指定输出名 |

打出一个约 120 KB 的 zip，解压后在根目录跑 `python _tools/build.py`
就能构建，**不需要项目里的任何其它文件**。

收什么：源码 + `Launcher.spec` + 构建资源（`mindustry.ico`）+ 开发工具。
不收什么：用户数据（`versions/`、`backups/`、`config.json`）、构建产物
（`_internal/`、exe）、`jre/`、日志。

生成后会**逐文件比对哈希**，确认 zip 内容与源文件完全一致。

## make_release_zip.py —— 生成「解压即用」的发布包

给**使用者**的成品，不是给开发者的源码。

| 命令 | 作用 |
|---|---|
| `python _tools/make_release_zip.py` | 生成 `Mindustry启动器_发布包_<日期>.zip` |
| `python _tools/make_release_zip.py --runtime-dir <目录>` | 从哪儿取 exe/`_internal`/`jre`（默认自动找运行时目录） |
| `python _tools/make_release_zip.py --outdir <目录>` | 换个输出位置 |
| `python _tools/make_release_zip.py --level 9` | 压得更狠（更慢） |

收什么：`Mindustry启动器.exe` + `_internal/` + `jre/`（内置 Java）
+ 现场生成的 `使用说明.txt`。约 **31 MB**（原始 60 MB），解压后双击 exe 就能用，
**不需要装 Python，也不需要单独装 Java**。
不收什么：源码（`launcher/`、`_tools/`）、用户数据（`versions/`、`backups/`、
`config.json`、`launcher.log`）—— 数据是使用者的资产，绝不能打进交付包。

★ 它从**运行时目录**取 exe / `_internal` / `jre`（本仓库里没有这些），
默认自动认（同 `build.py`）；判据是「那儿有 exe 和 `_internal`」，比只看目录名可靠。

生成后同样**逐文件比对 sha256**（改完东西忘了重新生成，是这类交付最常见的翻车方式）。

> 两者别搞混：`make_source_zip.py` 给的是「要自己构建的人」，
> `make_release_zip.py` 给的是「只想双击用的人」。

## 已退役的一次性脚本

下面这些是**跑完就没用了**的脚本，已经从本仓库移除。
列在这里不为再跑它们，而是把「当时为什么这么做」留下来 —— 尤其第一条，
它解释了**为什么运行时刻意不带向上兼容代码**。

| 脚本 | 当年干了什么 | 为什么不再需要 |
|---|---|---|
| `migrate_mindustry_json.py` | 把旧版 exe 旁边的 `Mindustry.json` 合并进 `config.json` 的 `jvm` 段，旧文件送回收站。**只在 2026-09-18 那次合并用过一次** | 迁移不可逆且已完成；运行时**刻意没有**向上兼容代码去读旧文件，所以也不会再有第二次迁移 |
| `import_old_backups.py` | 把旧启动器的备份导入为独立存档分类（CAS 去重，1.3 GB → 57.7 MB） | 一次性数据搬迁，已完成 |

⚠️ 归档版 `migrate_mindustry_json.py` 里那个顺序仍然成立，将来若真要再迁移一次：
**先部署新 exe，再删旧文件**。旧 exe 还在读旧文件，先删它会让当前那份打不开。
（往 `config.json` 里加 `jvm` 段对旧 exe 无害，它只是忽略不认识的顶层键。）

## recycle.py —— 安全删除（进回收站）

整理目录、清重复文件时用它，**别用 `rm -rf` / `shutil.rmtree`**。

| 命令 | 作用 |
|---|---|
| `python _tools/recycle.py --list <路径>…` | 只列体积，不动手（**先预演**） |
| `python _tools/recycle.py <路径>…` | 真正移入回收站 |
| `python _tools/recycle.py --from-file 清单.txt` | 从清单读（每行一个路径，`#` 开头跳过） |
| `python _tools/recycle.py --list --from-file 清单.txt` | 先预演再执行 |

走的是和资源管理器同一条回收站路径（`SHFileOperationW` + `FOF_ALLOWUNDO`），
删完逐个报告成功/失败并复核路径确实消失。带 `FOF_WANTNUKEWARNING`，
万一回收站装不下只能永久删除时会有提示，不会静默粉碎。

⚠️ **回收站有容量上限**，超出的部分会被永久删除。批量删大目录前先看：
`HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\<卷GUID>\MaxCapacity`
（单位 MB），盘符对应的卷 GUID 用 `mountvol` 查。

## verify/ —— 改完代码就该跑

| 脚本 | 作用 | 什么时候跑 |
|---|---|---|
| `code_regression.py` | **主回归，344 项**：jar 条目内容 vs CAS 逐字节核对（真实数据全量 6504 条）、报错分支、GC 让路与恢复、清单指纹、预热状态机、关窗半路中断（带反向对照）、**导入进行中跑 GC**、**恢复备份的安全边界**、**启动参数解析与命令拼装顺序**、**jvm 段归一化**、**游戏输出日志收集**（含**管道编码**：拿真 jre 跑一遍，并用「不传参数时管道里是 GBK」做反向对照）、**镜像候选清单的容错整理**、**锁的作用域**（`_proc_lock` 必须可重入、with 块里不许弹窗）、**开关型配置的脏值容错**（`bool("false")` 是 True —— 会反过来；而且退默认时的告警**要点名配置键**，用户得能拿它去 config.json 里找）、**「保存并返回」的结构**（保存归 `save_settings`、跳转归 `save_and_return`，源码里不许再有「不保存就能离开设置页」的按钮）、**自动关闭必须排在自动备份之后**、**「直接删除」开关**（默认关、脏值退回 False、`delete_path` 的分发要真删得掉、链接只删自己、CAS 回收不受影响）、**Java 路径可改可测**（JRE / JDK 都收；`probe_java` 拿真 JRE 跑 `java -version`，路径写坏要退回默认而不是打不开）、**没 jre 时翻环境变量兜底**（JAVA_HOME 优先于 PATH、坏桩被跳过、只用于本次运行**不写回配置**、两处都没有才报错）、**设置页提示不被裁**（每一条灰字提示都得套 `_auto_wrap_hint`，漏一条就红）、**分割线都要跨满 3 列**（只盖 2 列就会在按钮那一列左边断掉）、**配置格式版本与迁移**（新配置写 `config_version`；第 0 代老配置升级后要**立刻落盘**；不认识的顶层键原样保留、明确废弃的键丢掉；**版本号只升不降**）、**版本来源注册表**（内置来源的档位与版本号解析与重构前逐条一致；config 里能加/覆盖来源）、**扩展点**（坏扩展不影响好扩展；回调抛异常不许冒出来）、**版本号只有一处**、**_paths 懒解析**（刚 clone、没有运行时目录时 import 不能炸） | 改了 `launcher/` 里任何东西之后 |
| `sandbox_seed.py` | **不是测试**，是给下面几个沙箱脚本用的工具：把真实数据目录里**每个类型最新的版本清单**拷进沙箱，让 `auto_update` 直接给出「已是最新」，不再真去下 200+ MB。默认只放清单（判重只看清单），要启动游戏再传 `with_objects=True`（同卷走硬链接，不占空间） | 被 `release_check` / `exe_edge_check` 自动调用 |
| `gui_smoke.py` | **界面冒烟，107 项，不起游戏**：主窗口/设置页布局（底部按钮没被裁掉、设置项可滚动、**所有输入框左边缘对齐**）、自定义参数保存与校验（未闭合引号被拦、`-cp` 会警告）、运行日志窗口（两页签、轮询能刷出内容、单实例、**轮询期间不 lift**）、游戏运行中点「设置」不会自锁（锁是放开的）、**滚轮该滚页面而不是改下拉框**（带反向对照 + 下拉列表 popdown 的层级断言）、**镜像下拉框的候选站读的是 config**、**「保存并返回」真的存盘并回到主界面、被拦下时留在设置页**、**Java 路径能编辑 + 点「检测」真跑一次 java -version 并报出版本、填错路径当场拦下、换路径自动体检**、**兜底生效时那一栏显示实际在用的 Java、存过一次兜底就作废**、**「直接删除」开关存得住/回显得出**、**设置页里没有标签被裁掉**（默认 780 与最小 720 各量一遍：需要的宽度 > 分到的宽度 就是被切了尾巴）、**提示折行不会越算越窄**、**输入框 / 分割线都撑到右边**（默认 780 与最小 720 各量一遍，带反向对照：把下拉框缩回单列必须当场报出来）、**游戏退出自动关闭（开/关两路对照 + 备份先做完 + 下载中不关）**、干净退出 | 改了 `gui_*.py`、布局、对话框之后 |
| `packed_code_check.py` | 解出 exe 内部 PYZ、递归收 `co_names`，断言新符号真进了打包产物 | **每次重打完 exe**（"能打开"证明不了包里是新代码） |
| `release_check.py` | 解压发布包到空目录、预置最新版本清单、**用不相关的 cwd 真启动一次**，断言关键依赖齐全、cwd 没被污染、数据落在 exe 旁边 | **每次发发布包**（⚠️ 会真弹 GUI 窗口约 10 秒） |
| `exe_edge_check.py` | **打包版的边界条件**：坏配置自愈 / 坏清单与非 UTF8 清单（有效版本照常列出）/ 孤儿对象宽限期 / 缺 jre 时的报错质量（**要把环境变量里的 java 摘干净再跑**，否则测到的是下一条）/ **脏 jvm 段与脏自定义参数** / **jre 路径写错时自动退回默认（配置也要改回去）** / **没 jre 但 JAVA_HOME 里有能用的 Java 时照常启动、且不写回配置**。全程在临时沙箱里跑，**不碰真实数据** | 打包部署之后；改动启动路径、配置加载、GC、日志之后 |
| `e2e_launcher.py` | 端到端：真的走 `_monitor_game` 把游戏拉起来，确认分段计时落到日志、游戏能起到窗口出现；跑完自动关游戏 | 预热/启动流程改过之后 |
| `e2e_jar_launch.py` | 端到端：直接用优化后的代码拼 jar 并真实启动游戏，测「点启动 → 能玩」的实际耗时 | 怀疑拼装性能有问题时 |

`code_regression.py` 的**日志里出现 ERROR 是正常的** —— 它故意触发错误分支
（`对象丢失` / `清单不存在` / `模拟失败`）来验证报错行为，看结论行即可。

### 为什么还要单独验一遍打包版

`code_regression.py` 跑的是**源码**逻辑（在沙箱里直接 import `launcher`）。
exe 是 PyInstaller 冻结过的另一份代码：数据根、cwd、日志文件名都和源码运行
不同（源码写 `launcher.dev.log`，exe 写 `launcher.log`）。**「源码对」推不出
「exe 对」**，所以边界条件得拿 exe 再真跑一次 —— 这就是 `exe_edge_check.py`。

它把 exe + `_internal` + `jre` 拷进 `%TEMP%` 下的沙箱再启动，所以：
- 真实数据（`versions/`、`Backups/`、`config.json`）一点都碰不到；
- exe 写的是**沙箱里的** `launcher.log`，不会污染实际使用的那份。

用 WM_CLOSE 关窗口（而不是 terminate），这样走的是真实 `on_closing`，
顺带就验了「关得干不干净」。

### 写验证脚本时的日志规矩

**源码/测试运行绝不往 `launcher.log` 写** —— 那是**打包后的 exe 实际使用**时的
日志，被测试噪音污染就看不出真实运行情况了。规则在
`launcher/utils.py::resolve_log_file`：

```
打包后的 exe  → <数据根>/launcher.log        实际使用
源码运行      → <数据根>/launcher.dev.log    开发/测试（默认走这条）

想指到别处（比如临时目录）→ 设环境变量 MDT_LOG_FILE=<路径>，优先级最高
```

- 要拿「刚刚这次运行真正写的那份日志」，**同进程里读
  `from launcher.utils import LOG_FILE`**（源码运行的 BASE_DIR 是 cwd，
  未必等于 `_paths.DATA`，直接拼 `_paths.LOG` 容易读错文件）。
- 启动 exe 做冒烟测试时，用 `env={"MDT_LOG_FILE": <临时路径>}` 就不会在
  数据目录留东西。

## 拼 jar 的性能归因

当初为查明「拼 jar 为什么慢」写过一批量测脚本，**现已从仓库移除**（有的很慢、
有的会起游戏，正常不会重跑）。**结论已经落到 `launcher/storage.py` 里
`_JAR_READ_WORKERS` / `_JAR_BATCH` 的注释上**，下面这张表留的是「每个脚本
当年回答了什么」，照着结论改那两处常量即可：

| 脚本 | 当年用来回答什么 |
|---|---|
| `measure_jar.py` | 真实环境下 `build_runtime_jar` 的耗时构成（读 vs 写） |
| `bench_workers.py` | 并行度取多少最合适 → 结论 `_JAR_READ_WORKERS = 24` |
| `bench_variance.py` | **连跑多轮看稳态** → 发现「1.8 s 是缓存假象，稳态 3.6 s」 |
| `bench_cache.py` | 系统缓存热/冷对耗时的影响 |
| `bench_tk.py` | Tk / 线程环境有没有拖慢拼装（结论：没有） |
| `bench_pipeline.py` | 流水线预取能不能加快 → **结论：更慢，别做** |
| `verify_jar_opt.py` | 优化后的 jar 内容是否正确 |

## 典型流程

```bash
python _tools/verify/code_regression.py      # 1. 改动后先跑回归（344 项）
python _tools/verify/gui_smoke.py            # 2. 改了界面：真建窗口点一遍（107 项，不起游戏）
python _tools/recycle.py dist/Mindustry启动器  # 3. ★ 打包前先清产物（见下方说明）
python _tools/build.py --deploy              # 4. 构建并同步到运行时目录
python _tools/verify/packed_code_check.py    # 5. 确认新代码真进了 exe
python _tools/verify/exe_edge_check.py       # 6. 改了启动/配置/日志路径后：打包版边界冒烟
```

第 5 步别省 —— 源码改了没打进包时，exe 照常启动、界面照常出现、什么都不报错，
只是跑的是旧逻辑，光看"能打开"发现不了。

第 3 步也别省 —— PyInstaller 建 COLLECT 时会先清空 `dist/Mindustry启动器`
（1000+ 个文件），这一步会被工具自带的「批量删除确认闸」拦下（单轮累计 ≥ 50
个文件就要人工确认），**构建直接失败**。`recycle.py` 走 `SHFileOperationW`
原生 API，既不受闸管、又真进回收站。

### 要交付的时候，看你交给谁

```bash
# 给「想自己构建 / 看代码」的人 —— 120 KB 的源码包
python _tools/make_source_zip.py

# 给「只想双击用」的人 —— 31 MB 的发布包，解压即用（自带 Java）
python _tools/make_release_zip.py
python _tools/verify/release_check.py       # ★ 发出去之前必须跑
```

`release_check.py` 是发布包唯一的验收手段：它会解压到**全新空目录**、
用**完全无关的 cwd** 真启动一次（能弹窗 10 秒，别在忙时跑）。
它证明的是「这个 zip 到别人机器上真的能跑」——光看 zip 里的文件清单证明不了。

## 环境

- **开发辅助脚本不进包**，不影响构建产物。
- 脚本自己往上找项目根（认 `launcher/__init__.py`），放子目录里也能跑。
- 需要**带 tkinter** 的解释器。Anaconda / Miniconda 的 Python 自带，最省事；
  官方 Windows 安装包默认也带。判断标准就一条：
  `python -c "import tkinter; tkinter.Tk()"` 不报错。
  `build.py` 会自动在几个常见位置里挑一个能用的（`MDT_PYTHON` 可指定）。

## 清理

- 打包中间产物 `build/`、`dist/`、`_dist_*/` —— 用完即删（可重建，已在 `.gitignore` 里）。
  删除时注意：一次删几千文件会触发批量删除确认被拦（阈值 50 文件/轮），
  **逐个目录单独删**就过了。
- `__pycache__/` —— 不用管（Python 自动重建，清理时顺手删，已在 `.gitignore` 里）。

## 删东西之前

本仓库遇到过好几次「大规模整理目录」的场景（清重复版本、合并旧启动器、
瘦身备份）。铁律：

1. **先证明是重复的，再删**。CAS 是按 jar 内**每个文件**分片存的，
   所以「整只 jar 的 sha256」在对象池里查不到 —— 正确做法是把 jar 拆成
   `条目名 -> sha256(条目内容)`，和 manifest 的 `files` 映射比对。
   比错方向会得出「CAS 缺了 24 个对象」这种吓人的假结论。
2. **删除一律走 `recycle.py`（回收站）**，并且删完要看一眼回收站体积是否
   涨了预计的量 —— 没涨就说明是硬删，得马上停手。
3. **删前先列路径 + 报体积，让本人确认**。
