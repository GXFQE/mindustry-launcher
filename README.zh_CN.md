# Mindustry 启动器

**简体中文** · [English](README.md)

同时管理多个 Mindustry 版本、每个版本各用一套互不干扰的存档、自动备份。
tkinter 界面，**内置 Java** —— 不需要装 Python，也不需要单独装 Java。

---

## 下载使用

从 [Releases](../../releases/latest) 拿发布包，解压到一个**有写权限**的位置
（别放 `C:\Program Files`、桌面这类地方），双击 `Mindustry启动器.exe`。

首次打开时本地还没有游戏版本：点「检查更新」下载一个，然后在列表里选中它，
点「启动游戏」。

包内另有一份 `使用说明.txt`，内容与这份 README 一致，并且是**中英对照**的
（上半中文、下半英文）—— 界面切到 English 的用户看后半份就行。

## 主要功能

- **多版本并存** —— 相同文件只存一份，所以多装一个版本不会多占一整套的体积。
- **版本管理** —— 别人给的本地 jar 包可以直接导入（选 `desktop.jar`）；已有版本可以
  重命名、删除。都在「管理版本」里。
- **存档分类** —— 每个分类使用**完全不同**的游戏数据目录。切换分类再启动，
  游戏读到的就是另一套存档。每个分类可以单独设置数据目录、最小备份时间、最大备份数量。
- **自动备份** —— 在游戏退出后按你设的条件执行（默认：一局玩满 20 分钟才备份，
  最多保留 20 份）。
- **更新检查** —— 帮你把游戏版本下下来。走 GitHub 慢的话，去设置里填一个镜像。
- **启动预热** —— 窗口一打开就在后台把选中版本的游戏文件拼好，点启动时更快。
- **日志查看** —— 把游戏输出捕获下来，退出后还能看。

## 数据放在哪

全部在 exe 旁边，整个文件夹拷走就能一起带走：

```
versions\       下载的游戏版本（自动去重）
Backups\        存档备份
logs\           游戏输出日志（每次启动一份；可在设置里关掉）
extensions\     自己写的扩展（可选，见下节；没有这个目录也不影响）
lang\           自己覆盖的界面文案（可选，见下节）
config.json     你的设置
launcher.log    启动器自己的运行日志 —— 出问题时先看这个
```

## 值得知道的几个设置

- **界面语言** —— `跟随系统`（默认）、`简体中文`、`English`。改完整个界面立刻换过来，
  不用重启。
- **启动器更新** —— `自动`（默认）会在后台悄悄下好，等你关掉启动器时换上；
  `只提示` 只告诉你有新版；`停用` 完全关掉。
- **Java 路径** —— 填 JRE 或 JDK 的根目录即可，也可以直接放一个 `jre\` 文件夹到 exe 旁边。
  填错不用怕：会退回内置那份；连内置的都没有时，还会去 `JAVA_HOME` / `PATH` 里找。
- **GitHub 镜像** —— 留空 = 直连 GitHub。旁边有「测速」：对每个候选
  （含直连）真下一小段、按速度排个序（测的时候有进度条，看得出正在测哪个、
  不用干等），「用最快的」一键填回设置 —— 网速因运营商/地区而异，
  下拉框里的顺序不代表你这里的速度。
- **使用 GitHub CLI 认证** —— 本机装了 GitHub CLI（`gh`）并登录过的话，
  勾上它，GitHub 请求就带上认证：API 额度从 60 次/时（按出口 IP 共享，
  内网里大家一起分）提高到 5000 次/时。令牌只放在内存里、不写入文件；
  镜像站和自定义来源收不到它。
- **删除文件时直接彻底删除** —— 默认关闭，所以删除走**回收站**、反悔了能还原；
  打开之后就真找不回来了。

⚠️ 改完要点右下角的「保存并返回」才会生效。

## 不用重新打包就能改的东西

下面几项都是往 exe 旁边加点东西、或者手改 `config.json`，改完重启启动器即可 ——
不用重新打包，也不用会编程。

- **界面文字 / 加一门语言** —— 在 exe 旁边新建 `lang\` 文件夹，放一份与程序内置
  **同名**的 JSON（`zh_CN.json` / `en_US.json`），**外面那份优先**。想改词、想加一门
  新语言都走这条路。改坏了最坏情况是某几句显示成设置项的名字（比如
  `settings.save_return`），不会让启动器打不开；删掉外面那份就恢复原样。
- **多加几个「从哪儿下游戏版本」的来源** —— 来源是一张表（`config.json` 的
  `version_sources` 数组），程序内置两个，你可以往里加；`type` 跟内置的重名就是
  **覆盖**它（比如把内置 Mindustry 换成你自己的镜像仓库）。不改代码、不用重新打包。
  字段含义见 `launcher/sources.py`。内置那两个长这样，照这个格式写就行：

  ```json
  "version_sources": [
    { "type": "MindustryX",  "api_url": "https://api.github.com/repos/TinyLake/MindustryX/releases",
      "asset_pattern": "Desktop\\.jar$", "prerelease": false, "sort_rank": 0 },
    { "type": "Mindustry",   "api_url": "https://api.github.com/repos/Anuken/Mindustry/releases",
      "asset_pattern": "Mindustry.jar", "prerelease": null, "sort_rank": 1 }
  ]
  ```

  只有 `type` 和 `api_url` 是必填的；`asset_pattern`（正则，挑哪个文件）、
  `prerelease`（`true`/`false`/`null`＝正式版和预发布都要）、`use_mirror`
  （`false`＝这个来源不走镜像）、`sort_rank`（小号排在前面）都可不写。
  写坏了不会让配置失效 —— 那一项会被丢掉并记一条日志。

- **镜像下拉框里那几个候选** —— `config.json` 的 `github_mirror_presets` 数组，
  写进去就会出现在设置页「GitHub 镜像」的下拉框里。
- **自己加一点功能** —— 在数据根下建 `extensions\` 目录，往里放 `.py`（每个文件里要有
  `register(api)`），启动时自动载入。四个钩子：`on_config_loaded` /
  `on_versions_refreshed` / `on_before_launch` / `on_game_exited`。

  ```python
  # extensions\我的统计.py
  def register(api):
      api.on("on_game_exited", lambda **kw: print(kw["version_name"], kw["playtime_minutes"]))
  ```

  ⚠️ 扩展里的代码跟启动器**同权限**运行，只放自己写的或信得过的文件。
  怀疑是扩展搞的鬼时，设环境变量 `MDT_NO_EXTENSIONS=1` 就全部停用。

⚠️ 手改 `config.json` 之前先**关掉启动器** —— 它退出时会把整个文件重新写一遍，
你开着它改的内容会被冲掉。改坏了不用怕：认不出的键原样保留、写坏的值退回默认
并在 `launcher.log` 里记一条，不会让配置整个作废。

## 注意

- **杀毒软件可能误报**（PyInstaller 打包的 exe 挺常见），信任一下即可。
- 升级启动器**只替换程序文件**（exe 与 `_internal\`），不会动你的游戏版本、存档备份、
  设置、扩展，也不碰 `jre\`。
- 「检查更新」一直失败，多半是连不上 GitHub。可以设一个环境变量 `MDT_SELFUPDATE_API`，
  把它指向你能用起来的代理地址。
- 出问题时把 `launcher.log` 一起发出来，启动器每一步做了什么它都记着。

## Mindustry 本体

本程序不修改、也不重新分发游戏本体 —— 它只是从官方发布页下载可执行文件，
然后启动一个独立进程。

## 给开发者

从源码运行、项目结构、验证流程与发版流程，见 **[DEVELOPING.zh_CN.md](DEVELOPING.zh_CN.md)**。
