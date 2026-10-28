# -*- coding: utf-8 -*-
"""确认「新写的代码」真的进了打包好的 exe。

背景：PyInstaller 的 onedir 产物里，Python 模块被塞进 _internal/base_library.zip
旁边的 PYZ 归档（存在 exe 里）。源码改了但忘了重打、或者 spec 漏了模块时，
exe 能正常启动、看起来一切正常，只是跑的是旧逻辑 —— 光看「能打开」是发现不了的。

用法：
    python _tools/verify/packed_code_check.py [exe路径]

不带参数时按「你真正在跑的那份」优先找：项目根 → **运行时目录** → `dist/`
（顺序与理由见 `find_exe`）。
想专门验刚打出来的产物，就把 `dist/Mindustry启动器/Mindustry启动器.exe`
当参数传进来（注意：新 exe 和已部署的那份可能是同一个文件——打完记得先覆盖再验）。

做法：CArchiveReader 取出 PYZ 归档 → 逐模块解出 code object →
递归遍历所有嵌套函数的 co_names，断言所需符号都在；
**再把 `launcher.version` 真执行一遍**，断言里面的版本值跟源码一致
（符号在不在是一回事，值对不对是另一回事，见 `check_version_values`）。
"""
import sys
import tempfile
from pathlib import Path

# 要核对「值」而不是「名字」的模块：包内 code object 执行结果 vs 源码 import 结果。
# 键 = 模块名，值 = 要比对的属性名。
VALUE_MODULES = {
    "launcher.version": (
        "APP_NAME", "APP_ID", "__version__", "USER_AGENT",
        "CONFIG_VERSION", "MANIFEST_VERSION", "MIN_PYTHON",
    ),
}

def _find_project_root(start: Path) -> Path:
    """往上找项目根（含 launcher/ 包的那一层）。脚本放在 _tools/ 下也照样对。"""
    for p in (start, *start.parents):
        if (p / "launcher" / "__init__.py").is_file():
            return p
    raise RuntimeError("找不到项目根：往上没找到 launcher/__init__.py")


ROOT = _find_project_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # _tools/ 共用路径模块
from _paths import DATA, JRE, LOG, MANIFESTS, VERSIONS, find_runtime_root  # noqa: E402
sys.path.insert(0, str(ROOT))

# 模块 -> 必须在里面出现的符号
REQUIRED = {
    "launcher.gui_game": [
        "start_preheat", "_preheat", "_preheat_task", "_preheat_selected",
        "_take_preheated_jar", "preheat_busy", "cleanup_preheat",
        "_cleanup_stale_preheat", "_preheat_key_of", "_on_version_selected",
        "_PREHEAT_DEBOUNCE_MS", "_PREHEAT_STALE_HOURS",
        # 2026-09-16：等游戏退出改成 daemon 线程（原来占着 executor，
        # 关窗口会被它 join 住），以及版本列表跨线程刷新
        "_game_session", "_apply_versions", "_PREHEAT_WAIT_S",
        # 2026-09-18：启动命令改由 gamecmd 拼（自定义参数进得去）、
        # jvm 配置改从 config.json 读（原 Mindustry.json）、
        # 游戏输出不再丢进 DEVNULL 而是接出来
        "build_java_command", "split_args", "get_jvm_config", "_java_exe",
        "GameLog", "game_log", "list2cmdline", "PIPE",
        # 2026-09-18 追加：游戏退出后按设置关掉启动器（备份做完才关）
        "_close_after_game", "downloading",
        # 2026-10-16 追加：命令行直接启动指定版本。启动实现只此一份
        # （_launch_version，confirm 决定要不要先问一句），命令行这条路
        # 只是把选中挪过去、然后调它。少了这些名字 = exe 还是
        # 「命令行参数被当没看见」的旧版本 —— 而双击照常能用，发现不了。
        "_launch_version", "_select_version_row", "_handle_cli_launch",
        "_cli_report_missing", "cli",
        # 2026-10-16 追加（当天第二版）：那条路改成**零界面**了 —— 窗口全程
        # 收起、进度与结果写回控制台、失败才退化成弹框、退出码有意义。
        # ⚠️ 这一版**删掉了** _cli_auto_launch 与 _CLI_LAUNCH_DELAY_MS
        #    （不再有「开窗 → 等 1.2 秒 → 自动启动」那段），所以那两个名字
        #    从这里撤了 —— 留在表里会让检查永远红。
        "_cli_say", "_cli_note", "_headless", "_cli_console", "console",
    ],
    "launcher.gui_core": [
        "preheat_busy",            # GC 让路条件里用到了它
        "_preheat_idle", "_preheat_root", "_cleanup_stale_preheat",
        # 2026-09-18
        "GameLog", "game_log", "_java_exe", "_log_win", "_fit_root_window",
        # 2026-09-18 追加：持锁弹窗会把 GUI 线程锁死（弹窗「未响应」）
        "_game_is_running", "_proc_lock",
        # 2026-09-18 追加：滚轮只能滚设置页，不许顺手改掉下拉框的值
        # （Tk 的 TCombobox 类绑定会「滚一格换一个选项」）
        "_disable_combo_wheel", "_is_in_settings", "_wheel_targets_settings",
        # 2026-09-19 追加：jre 路径写坏时退回默认 jre（设置页里能改之后，
        # 「改错一个路径就再也打不开启动器」必须堵死）
        "_java_exe_of", "_default_java_exe", "JVM_DEFAULTS",
        # 2026-09-19 追加：连 jre/ 都没有时去 JAVA_HOME / PATH 里兜一个
        # （兜底只影响本次运行，句柄和撤销入口都得在）
        "_java_exe_override", "_reset_java_override", "_jre_path_for_form",
        "_override_dir", "find_system_java",
        # 2026-09-22：扩展载入挂在「窗口已可用」的后台步骤上，**不塞进
        # _start_background_gc**（那个方法的行为被回归逐条断言着）
        "ExtensionRegistry", "extensions", "_load_extensions_once",
        "_start_post_window_tasks",
        # 2026-10-05：界面语言。两页的文案在建控件那一刻就取好了，换语言只能
        # 整体重建 —— 所以 _build_pages 必须和 rebuild_for_language 一起在包里。
        "t", "apply_setting", "_build_pages", "rebuild_for_language",
        # 2026-10-16 追加：命令行请求从 __init__ 传进来，并且**必须入队**
        # （run_on_gui → _handle_cli_launch，排在 _apply_versions 之后 ——
        # 直接调用时列表还是空的，选中会被静默丢掉，见 REF §十八）。
        "CliRequest", "cli_request", "_handle_cli_launch",
        # 2026-10-16 追加（当天第二版）：零界面形态的两个标志（_headless 由
        # cli_request.used 推导、console 由入口的 attach() 结果传进来）、
        # 退出码（入口 return app.exit_code）、以及**必须早于建控件**的那句
        # withdraw —— 它要是掉到 _setup_gui 之后，用户会看见窗口闪一下。
        "_headless", "_cli_console", "exit_code", "withdraw",
    ],
    # 2026-09-18 新增两个模块。不在包里 = exe 还是旧逻辑（读 Mindustry.json、
    # 把游戏输出扔掉），而这种退化只看「能打开」是发现不了的。
    "launcher.gamecmd": [
        "split_args", "build_java_command", "check_extra_args",
        "_CONFLICTING_VM_FLAGS", "_DATA_DIR_PREFIX",
        # 2026-09-18：管道里的中文 mod 名整片乱码 —— 启动命令固定带这两个
        # 参数，让 JVM 按 UTF-8 写输出（见 gamelog 的兜底解码）
        "_OUTPUT_ENCODING_ARGS",
        # 2026-09-19：「检测」按钮真跑一次 java -version（只查文件存不存在
        # 是查不出「缺 VC 运行库 / 被拦」这类问题的）
        "probe_java", "_JAVA_PROBE_TIMEOUT",
        # 2026-09-19 追加：没 jre 时翻 JAVA_HOME / PATH（候选必须真跑过才认）
        "find_system_java", "_JAVA_FIND_TIMEOUT", "_JAVA_FIND_MAX_CANDIDATES",
    ],
    "launcher.gamelog": [
        "GameLog", "LOG_DIR_NAME", "LOG_PREFIX", "KEEP_FILES", "keep_files",
        "start", "close", "snapshot", "tail", "_open_file", "_prune",
        "_pump", "_add", "active", "path",
        # 按字节读管道、按行解码（UTF-8 优先，失败退系统代码页）
        "decode_output_line", "fallback_encoding",
        # 2026-09-19：清旧日志默认走回收站，设置里开了才直接删
        "delete_path", "permanent_delete",
    ],
    "launcher.gui_log": [
        "LogMixin", "open_log_window", "tail_text", "POLL_MS",
        "LAUNCHER_LOG_TAIL", "_build_game_log_tab", "_build_launcher_log_tab",
        "_refresh_game_log", "_poll_log", "_close_log_window",
        # 轮询只能查「窗口还在吗」，不能 lift（否则任务栏一直高亮）
        "_log_window_alive", "_focus_existing_log_window",
    ],
    "launcher.storage": [
        "manifest_digest", "build_runtime_jar", "_JAR_READ_WORKERS",
        # 2026-09-16：批量写入门闩 + 新对象宽限期（防 GC 误删正在导入的对象）、
        # 恢复备份前必须先拿到安全快照
        "_BULK_WRITE_LOCK", "_GC_GRACE_SECONDS",
        "manifest_path", "_make_rollback_snapshot", "_clear_save_dir",
        "_extract_zip_into",
        # 2026-09-22：版本来源改成注册表（不再把仓库地址写在 updates 里）、
        # 清单格式版本独立于程序版本
        "SourceRegistry", "MANIFEST_VERSION", "sources",
        # 2026-10-17：版本库的**唯一**打开方式。界面和命令行（--list）共用它 ——
        # 少了它，`--list` 在包里就退化成「自己另拼一份路径」或者直接 ImportError。
        "open_version_manager",
    ],
    "launcher.gui_updates": ["cleanup_preheat", "_close_log_window"],
    # 日志分流（2026-09-16）：源码/测试写 launcher.dev.log，exe 才写 launcher.log。
    # 这几个名字不在包里 = exe 仍是旧逻辑，会跟源码抢同一个 launcher.log。
    "launcher.utils": [
        "resolve_log_file", "LOG_NAME", "DEV_LOG_NAME", "LOG_FILE_ENV", "LOG_FILE",
        # 不再改 ssl._create_default_https_context（进程级副作用），
        # 改成只给自己的请求传这个 context
        "UNVERIFIED_SSL_CTX",
        # 2026-09-22：开关值解析只有这一处实现（config.normalize_bool 是它的
        # 薄委托）—— sources.py 也要用，而它不能 import config（会绕成环）
        "parse_bool", "TRUE_WORDS", "FALSE_WORDS",
        # 2026-10-05：语言 code 的识别同样放在这里（i18n 要用，但 i18n 不能
        # 反向 import config，否则又是一个环）
        "normalize_language", "LANGUAGE_AUTO", "LANGUAGE_CODES",
        "LANGUAGE_DEFAULT", "LANGUAGES",
        # 2026-10-07：文件下载也会带上 gh 认证（仅 GitHub 官方域名；镜像站
        # 这类第三方地址不带 —— 白名单在 ghauth 里）
        "ghauth",
        # 2026-10-11：下载支持断点续传（Range 续写；失败保留断点、跨源/跨次
        # 续传；「下满但校验不过」才删）。辅助函数与读块常量都在包里才算
        # 真带上这个能力。
        "download_file", "download_tmp_path", "file_sha256",
        "_parse_content_range", "_try_unlink", "_DOWNLOAD_CHUNK",
    ],
    "launcher.config": [
        # 版本号会拼进清单文件名，必须和分类名一样过校验
        "sanitize_version_name", "VERSION_NAME_FORBIDDEN",
        # 坏配置留档 .bad 再重建，别静默抹掉用户的分类
        "_quarantine_bad_config",
        # 2026-09-18：游戏启动配置并进 config.json 的 jvm 段
        "DEFAULT_VM_ARGS", "JVM_DEFAULTS", "_normalize_jvm",
        "get_jvm_config", "get_jvm", "set_jvm",
        # 2026-09-18 追加：GitHub 镜像改成可填地址 + 日志保留份数
        "normalize_mirror", "DEFAULT_MIRROR", "DEFAULT_MIRROR_PRESETS",
        # 下拉框的候选清单也进 config.json 了（github_mirror_presets）
        "normalize_mirror_presets", "MAX_MIRROR_PRESETS",
        "normalize_log_keep", "MAX_LOG_KEEP",
        # 2026-10-11：测速轮数做成设置项（1~3，默认 3）—— 同样是「带默认
        # 值的键，不涨 config_version」（同下面 language 那一条）
        "normalize_mirror_rounds", "DEFAULT_MIRROR_ROUNDS",
        "MAX_MIRROR_ROUNDS",
        # ★ 开关型不能走 type(bool)(值)：bool("false") 是 True
        "normalize_bool",
        # 2026-09-19：删除方式开关 + 统一删除入口（默认回收站，开了才硬删）
        "permanent_delete", "delete_path",
        # 2026-09-22 兼容三条：① 强转只有一处（规格表）；
        # ② 未知键原样保留、已废弃键丢掉（这两件事必须分开）；
        # ③ config_version 缺失=第 0 代，MIGRATIONS 逐级升级
        "KNOWN_TOP_KEYS", "OBSOLETE_TOP_KEYS", "_coerce_global",
        "_coerce_profile", "_extras", "CONFIG_VERSION_KEY",
        "MIGRATIONS", "_migrate", "_migrate_0_to_1",
        # 2026-10-05：新增带默认值的键（language）**不涨 config_version** ——
        # 老配置读进来就是 auto，靠的是「规格表里多一行」而不是一次迁移。
        "normalize_language", "LANGUAGE_AUTO",
    ],
    "launcher.gui_versions": ["_gc_task", "sanitize_version_name"],
    "launcher.gui_dialog": ["_is_link_like", "_ignore_links"],
    "launcher.gui_main": [
        # 自定义启动参数：校验（引号闭合）与危险项告警
        "split_args", "check_extra_args",
        "extra_vm_var", "extra_prog_var", "save_game_log_var",
        "open_log_window",
        # 镜像地址 + 日志保留份数；两帧共用一个标签列宽（输入框才对得齐）
        "github_mirror_var", "max_log_files_var", "normalize_mirror",
        # 2026-10-11：测速轮数（采样总数 = 候选数 × 轮数，直接决定等待）。
        # 2026-10-12：保存与测速共用 gui_mirror 的读取器（超范围弹错拦下、
        # 不静默夹取）—— 这里钉上限常量，读取器本身在 gui_mirror 名单里。
        "mirror_test_rounds_var", "MAX_MIRROR_ROUNDS",
        "_label_col_width",
        # 2026-10-05：界面文案接进语言包。原来写死的两个模块级常量
        # （SETTINGS_LABEL_TEXTS / LAUNCHER_UPDATE_LABELS）已删，改成函数 ——
        # 常量是 import 时求值的，换语言它不会变。
        "_settings_label_texts", "launcher_update_labels",
        "launcher_update_values", "launcher_update_label",
        "language_labels", "language_values", "_language_label_in_form",
        "_selected_language", "language_var",
        "apply_setting", "current_language", "t", "rebuild_for_language",
        # 2026-09-18 追加：保存和返回合成一个按钮（离开设置页＝保存）
        "save_and_return", "close_on_game_exit_var",
        # 2026-09-19 追加：JRE 路径在设置页里可改 + 「检测」跑 java -version
        # （后台线程），以及「删除文件时直接彻底删除」开关
        "jre_path_var", "jre_check_var", "check_jre", "_start_jre_probe",
        "_jre_probe_task", "_apply_jre_probe", "probe_java", "_java_exe_of",
        "permanent_delete_var",
        # 2026-09-19 追加：存过一次就把环境变量兜底撤掉（不然界面写 A、
        # 实际跑 B）；开设置页时显示的是实际在用的那个 Java
        "_reset_java_override", "_jre_path_for_form",
        # 2026-09-19 追加：设置页的灰字提示放不下就折行（以前长提示会被
        # grid 直接裁掉一截，用户截过图）
        "_auto_wrap_hint", "_HINT_PAD",
        # 2026-10-07 追加：gh 认证的勾选框 + 后台探测（子进程）回填状态行
        "use_gh_auth_var", "gh_auth_status_var", "ghauth",
        "_on_gh_auth_toggled", "_start_gh_auth_probe", "_gh_auth_probe_task",
        "_apply_gh_auth_status",
    ],
    # 2026-10-05 新增：i18n 地基。不在包里 = exe 还是「文案写死在代码里」的
    # 旧逻辑（界面上根本换不了语言，而且这种退化只看窗口能不能打开发现不了）。
    "launcher.i18n": [
        "LANG_SUBDIR", "LANG_ENV",
        "set_language", "current_language", "language_is_forced", "t",
        "apply_setting", "_load_pack", "resource_path",
        # 语言解析：系统界面语言优先，认不出退默认
        "LANGUAGE_DEFAULT", "LANGUAGE_CODES", "normalize_language",
        "_system_language", "_system_primary_language", "_bootstrap",
    ],
    # gui.py 只做组装；这里钉「新 Mixin 确实进了 MRO」——从组合元组里漏掉
    # 一个 Mixin，功能会整个消失，而窗口照样能打开。
    "launcher.gui": ["LogMixin", "MirrorTestMixin"],
    "launcher.updates": [
        "StatusCallback", "downloading", "normalize_mirror",
        # 2026-09-22：改成一个来源一个来源地拉（来源表在 sources.py），
        # 不再在 updates 里写死仓库地址
        "_fetch_release", "sources", "parse_bool",
        # 2026-10-07：查版本也会带 gh 认证（GitHub 官方来源；自定义来源不带）
        "apply_auth",
        # 2026-10-11：断点文件用确定性路径（download_tmp_path）；换源/重试
        # 从已下部分接着下，而不是从零开始
        "download_tmp_path", "download_file",
        # 2026-10-28：`--check-updates` 靠它把「所有来源都失败」和「没有新
        # 版本」分开（默认那条路把异常吞成 []，断网会被读成「已是最新」）
        "UpdateCheckError", "get_available_updates",
    ],
    # 2026-09-22 新增的三个模块（面向发布的可扩展性接口）。跟 09-18 那两个
    # 一样：不在包里 = exe 还是旧逻辑（来源写死、没有迁移链、没有扩展点），
    # 而单看「exe 能打开」是发现不了的。
    "launcher.version": [
        "APP_NAME", "APP_ID", "__version__", "USER_AGENT",
        "CONFIG_VERSION", "MANIFEST_VERSION", "MIN_PYTHON",
        "version_string", "full_version",
    ],
    "launcher.sources": [
        "VersionSource", "DEFAULT_SOURCES", "SourceRegistry",
        "load_sources", "build_source", "normalize_version_sources",
        "_SOURCE_KEYS", "DEFAULT_SORT_RANK", "matches_asset",
    ],
    "launcher.extensions": [
        "ExtensionRegistry", "ExtensionAPI", "HOOKS", "EXT_DIR_NAME",
        "DISABLE_ENV", "_HOOK_SET", "_import", "_note_failure",
        # ⚠️ 这里**不查** "register"：它只出现在模块 docstring 的示例里（给扩展
        # 作者看的契约），不是模块级符号 —— loader 是用 getattr(module, ...)
        # 取的，那个字符串落在 co_consts，而本检查只扫 co_names（会误报缺失）。
        # 「入口必须叫 register」由 code_regression 那条「扩展能载入」的用例
        # 功能性地钉住：改了名字那条就红。
    ],
    # 2026-09-22 新增：启动器自身更新。不在包里 = exe 还是个「不会更新自己」的
    # 版本，而且这种缺失光看窗口能不能打开是发现不了的。
    "launcher.selfupdate": [
        "UpdatePlan", "parse_version", "is_newer", "parse_manifest",
        "fetch_latest_update", "download_update", "stage_update",
        "launch_updater", "apply_update_main", "check_and_stage",
        "state_path", "read_state", "clear_state", "mode_of",
        "staged_root", "staged_files_dir", "staged_plan_path", "write_plan",
        "cleanup_updater_dir",
        # 「查新版」的地址可被环境变量盖掉（端到端验证 / 走代理）
        "update_api", "API_ENV",
        # 只放行 exe 自己与 _internal/ —— 「绝不碰用户数据」就落在这两个名字上
        "_is_replaceable", "_safe_rel", "PROTECTED_NAMES",
        "UPDATE_ASSET_SUFFIX", "DISABLE_ENV", "MODE_AUTO",
        # 2026-10-07：查「自己的新版」也带 gh 认证（接口被环境变量换成
        # 自建代理时白名单会挡住令牌 —— 代理拿到它等于泄露）
        "apply_auth",
        # 2026-10-11：更新包也走断点续传（失败保留已下部分，换源/重试接着下）。
        # 这里只钉 launcher.utils.download_file 这个名字 —— 断点续传的参数
        # （resume= / expected_size=）是**关键字实参**，不出现在 co_names 里，
        # 由 code_regression 的 AST 接线断言负责。
        "download_file",
    ],
    # 2026-10-07 新增：GitHub 镜像测速（mirrortest 纯逻辑 + gui_mirror 界面）。
    # 跟前面那些一样：不在包里 = exe 还是个「设置页没有测速按钮」的旧版本，
    # 只看「窗口能打开」发现不了。
    "launcher.mirrortest": [
        "MirrorResult", "test_mirror", "test_mirrors", "probe_url",
        "any_stop", "_AnyStop", "sorted_results",
        "format_size", "format_speed", "format_latency",
        # 六个状态常量 + 排序档位表：界面文案与结果窗的次序都按它来
        "STATUS_OK", "STATUS_TIMEOUT", "STATUS_SHORT", "STATUS_HTTP",
        "STATUS_NET", "STATUS_CANCELED", "_STATUS_RANK",
        # 测速参数：目标写死老版本 release 资源（各候选同款才可比）、
        # 样本 1.5 MB、低于 MIN_OK_BYTES 当错误页（几 KB 的 502 页面
        # 不能算「快」）、单候选总预算、候选上限（清单来自 config.json）
        "TEST_URL", "SAMPLE_BYTES", "MIN_OK_BYTES", "MIRROR_BUDGET",
        "SOCKET_TIMEOUT", "MAX_TARGETS", "UNVERIFIED_SSL_CTX",
        # ★ 2026-10-11 测速方法学改造：交错多轮（ROUNDS）+ 固定时长采样
        # （SAMPLE_SECONDS）+ 取中位（_aggregate_samples）+ 死候选退出
        # （_DEAD_STATUSES）+ 波动提示阈值（SPREAD_WARN）。少了这些 =
        # exe 还是「顺序测一遍、谁前谁后看运气」的旧版本（见 REF §十七）。
        "SAMPLE_SECONDS", "ROUNDS", "SPREAD_WARN", "_DEAD_STATUSES",
        "_aggregate_samples",
    ],
    "launcher.gui_mirror": [
        "MirrorTestMixin", "mirror_test_click",
        # 候选 = 直连 + 下拉清单 + 当前手填（去重保序）；测速走后台线程
        "_mirror_test_running", "_mirror_test_targets",
        # ★ 轮数即测即用：界面当前值优先、读不到才退配置（2026-10-12）
        "_mirror_test_rounds_from_ui",
        "_mirror_test_task", "_mirror_test_done",
        # 结果窗（表格 + 用最快的 / 应用选中）；填回只改内存、不落盘
        "_mirror_test_show", "_mirror_test_apply",
        "_mirror_display_name", "_mirror_status_text",
        # 换语言的整页重建会把按钮换掉 —— 改按钮文字要吞 TclError
        "_set_mirror_test_button", "mirrortest", "normalize_mirror",
        # 2026-10-12：超范围弹错要报「1~MAX」——上限常量进包才算新版本
        "MAX_MIRROR_ROUNDS", "t",
    ],
    # 2026-10-07 新增：GitHub CLI 认证（复用本机 gh 登录态，API 额度 60→5000）。
    # 不在包里 = exe 还是「匿名请求、被共享出口 IP 的 60 次/时卡住」的旧版本。
    "launcher.ghauth": [
        "set_enabled", "get_github_token", "refresh_status", "apply_auth",
        # ★ 域名白名单与超时是安全底线：只给 GitHub 官方域名带令牌、
        #   关掉开关连探测都不做、子进程卡住最多等 6 秒
        "_host_allowed", "_GITHUB_HOSTS", "_GH_TIMEOUT",
    ],
    # ★ 2026-10-07 补漏：这两个**老**模块一直不在上面的清单里。这次加测速
    # 时才发现的 —— 「逐模块断言」的覆盖面**自己也会漏**，而漏掉一个模块
    # ＝ 对它完全没有守门（改了没重打，检查照样全绿）。
    "launcher.gui_backup": [
        "BackupMixin", "BackupManager", "manage_backups",
        "_safe_refresh", "_backup_task", "_restore_backup_task",
        "_fit_dialog", "run_on_gui", "t",
    ],
    "launcher.gui_profiles": [
        "ProfilesMixin", "manage_profiles", "switch_profile",
        "open_current_save_dir", "_refresh_profile_widgets",
        "_on_profile_selected",
        "default_data_dir", "delete_path", "dir_summary", "roaming_dir",
        "sanitize_profile_name",
    ],
    # 2026-10-16 新增：启动器**自己**的命令行参数（`--launch <版本>`）。
    # ★ 又是上面那条「覆盖面自己也会漏」：新模块不主动加进来，它就完全没有
    #   守门。而且漏了格外隐蔽 —— 入口那句接线（parse_cli + cli_request=）
    #   落在入口脚本里、不在这份检查的可视范围（由 code_regression 的 AST
    #   断言钉），于是「解析/匹配规则本身在不在包里」只剩这里能查。
    "launcher.cli": [
        "CliRequest", "LAUNCH_FLAG",
        # 2026-10-17：--list / --help —— 纯输出、不建窗口的两个开关。
        # ★ 它们和 --launch 一条链上的东西不共享：少了这两个常量，
        #   `--list` 会退化成「不认识的参数」→ 反手打开界面（最坏的那种失败：
        #   用户以为敲错了，其实是被当成双击）。
        "LIST_FLAG", "HELP_FLAG", "info_only",
        # 2026-10-17 下午：--export / --export-backup / --list-backups / --out
        # ★ 少一个常量的症状和上面一样 —— 那个开关变成「不认识的参数」，
        #   于是 `--export` 反手打开界面（而它本该连 Tk 都不建）。
        "EXPORT_FLAG", "EXPORT_BACKUP_FLAG", "LIST_BACKUPS_FLAG", "OUT_FLAG",
        "export_only", "EXIT_EXPORT_FAILED",
        # 2026-10-17 傍晚：--get / --set（看和改设置，同样在 import tkinter 之前）
        "GET_FLAG", "SET_FLAG", "cfg_only", "EXIT_CONFIG_FAILED",
        # 2026-10-28：--import / --restore-backup / --delete-* / --check-updates
        # ★ 少一个常量的症状同上：那个开关变成「不认识的参数」→ 反手打开界面，
        #   而它本该连 Tk 都不建（`--restore-backup` 还会把存档目录留在游戏手里）。
        "IMPORT_FLAG", "AS_FLAG", "TYPE_FLAG", "import_only",
        "RESTORE_BACKUP_FLAG", "restore_only", "EXIT_RESTORE_FAILED",
        "DELETE_VERSION_FLAG", "DELETE_BACKUP_FLAG", "EXIT_DELETE_FAILED",
        "CHECK_UPDATES_FLAG", "EXIT_UPDATE_AVAILABLE", "EXIT_UPDATE_FAILED",
        "YES_FLAG", "EXIT_IMPORT_FAILED",
        # 「可用版本」列出来时的上限（弹窗不把整个版本列表糊上去）
        "AVAILABLE_LIMIT",
        # 解析：认 `--launch X` 与 `--launch=X`，不认识的参数一律忽略
        "parse_cli",
        # 匹配：显示名精确 → 原始版本号精确；**撞名不猜**、不做前缀匹配
        "match_version", "available_names",
    ],
    # 2026-10-17 新增：`--list` / `--help` 的**全部实现**。★ 它们在 exe 里的
    # 位置格外要紧：入口必须在 `import tkinter` 之前分派（见 code_regression
    # 的 AST 断言），所以这两个开关的代码路径和 gui 一点关系都没有 ——
    # 打包时漏掉这个模块，症状是「--help 没反应，反而弹出启动器界面」。
    "launcher.cli_info": [
        # 帮助里「本程序怎么敲」：打包后是 exe 名，源码跑是 python + 脚本名
        "command_name",
        # 读版本列表（走 storage.open_version_manager，和界面同一个库）
        "collect_versions",
        # 三种输出
        "render_list", "render_help", "render_backups",
        # 入口 + 两条出口（控制台 / 没控制台时的弹框退化路）
        # ★ 2026-10-17：`_say` / `_show_in_box` 改成了公开的 `say` /
        #   `show_in_box`（cli_export 也要用同一个出口）；符号表跟着改，
        #   否则打包后才会在「导出」那条路上炸。
        "run", "say", "show_in_box", "report_bad_usage",
        # 失败时的退出码（读配置/清单都可能出意外）
        "EXIT_CRASH", "EXIT_BAD_USAGE",
    ],
    # 2026-10-17 新增：`--export` / `--export-backup` 的全部实现。
    # ★ 这条路上一个 Tk 对象都不建（入口在 import tkinter 之前就分派走了）。
    "launcher.cli_export": [
        "run",
        # 两条导出路：版本 → jar、备份 → zip
        "_export_version", "_export_backup",
        # 输出路径：同名加序号（绝不覆盖）、分类名防越界、文件名清洗
        "resolve_output", "safe_filename", "_resolve_profile",
        "version_filename",
        "EXIT_EXPORT_FAILED", "EXIT_NO_VERSION",
    ],
    # 2026-10-17 新增：`--get` / `--set` 的全部实现（严格校验 + 回读 + 锁）。
    # ★ 严格表（GLOBAL_SPECS / JVM_SPECS / PROFILE_SPECS）一旦缺了，
    #   `--set` 会把所有键都当成「没有可改的项」—— 用户看到的是「没有这一项」，
    #   但 `--get` 又列得出来，最难查的那类矛盾。
    "launcher.cli_cfg": [
        "run",
        "render_get", "do_set",
        # 严格表与解析（坏值必须报错，绝不退默认）
        "GLOBAL_SPECS", "JVM_SPECS", "PROFILE_SPECS",
        "resolve", "parse_value",
        # 读 / 写 / 回读（写盘后重新读文件对账）
        "read_value", "write_value", "apply_pairs", "_read_disk", "_dig",
        "EXIT_BAD_USAGE", "EXIT_CONFIG_FAILED",
    ],
    # 2026-10-28 新增：`--delete-version` / `--delete-backup` 的全部实现。
    # ★ 这条路上一个 Tk 对象都不建；漏掉这个模块的症状是「删除命令没反应，
    #   反而弹出启动器界面」—— 和 --help 那次一模一样，而且**会删错东西**
    #   之前先建个窗口，用户还以为程序没听懂。
    "launcher.cli_delete": [
        "run",
        # 两条删除路：版本 → 清单；备份 → 本分类清单（查表，绝不拼路径）
        "_delete_version", "_delete_backup",
        # 「没有这个版本」时的可用清单
        "_not_found_text",
        "EXIT_DELETE_FAILED", "EXIT_NO_VERSION",
    ],
    # 2026-10-28 新增：`--import` 的全部实现。
    # ★ 和 cli_delete 同一条路：一个 Tk 对象都不建。漏掉它的症状是「导入命令
    #   没反应，反而弹出启动器界面」，用户以为程序没听懂。
    "launcher.cli_import": [
        "run",
        "_import_version",
        "EXIT_IMPORT_FAILED", "EXIT_BAD_USAGE",
    ],
    # 2026-10-28 新增：`--restore-backup` 的全部实现。
    # ★ 和 cli_delete 同一条路（零界面、同一把进程锁），但方向相反：它**写**
    #   存档目录。漏掉它的症状是「恢复命令没反应，反而弹出启动器界面」。
    "launcher.cli_restore": [
        "run",
        "_restore_backup",
        # 恢复后回读：清单里几个文件，目录里就该有几个
        "_expected_files", "_count_files",
        "EXIT_RESTORE_FAILED", "EXIT_NO_VERSION",
    ],
    # 2026-10-17 新增：配置写盘的进程间锁。★ 少了它，`--set` 会在启动器
    # 开着时照改，然后被对方退出时那次 save() 静默覆盖 —— 不报错、不留痕。
    "launcher.applock": [
        "LOCK_PREFIX",
        "lock_file", "acquire", "AppLock",
    ],
    # 2026-10-16 新增：把话说给「敲命令的那个控制台」。★ 打包成**窗口子系统**
    # 之后这个进程天生没有控制台（sys.stdout 是 None，print 被 Python 直接
    # 丢掉、还不报错），所以「接上父进程控制台」这件事只能在 exe 里验 ——
    # 源码里跑（有控制台）永远是好的，看不出这一层没了。
    "launcher.console": [
        "ATTACH_PARENT_PROCESS", "CONOUT",
        # 接上/判断/说话。say 在没接上时是空操作（调用方据此退化成弹框）
        "attach", "attached", "say", "_console_encoding",
        # 接上以后那个流与它的锁（多线程都会说话，不加锁会绞在一起）
        "_stream",
    ],
}


def collect_names(code, seen=None):
    """递归收一个 code object 里出现过的所有名字。"""
    if seen is None:
        seen = set()
    seen.update(code.co_names)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            collect_names(const, seen)
    return seen


def find_exe(argv):
    """不带参数时按常见位置找，也可以把路径当参数传进来。

    顺序＝「你真正在跑的那份」优先：
      ① 项目根（老形态，exe 直接放仓库根）
      ② 运行时目录（**现在部署到这儿** —— 日常双击的就是它）
      ③ `dist/`（刚构建、还没部署的形态）
    ①②③ 都能直接跑，不用手敲路径。

    ★ 2026-10-11 补上 ②：以前只找 ① 和 ③，于是**部署之后不带参数跑，
      验的其实是 `dist/` 里那份**（内容通常一样，但这个检查的全部意义就是
      「验实际要用的那份」—— 部署那一步真出问题（覆盖失败、目标被占用而
      没换掉）时，验 dist 会给出**假的安心**）。要专门验刚打的产物，把
      路径当参数传进来。
    """
    if len(argv) > 1:
        return Path(argv[1])
    runtime = find_runtime_root(required=False)
    cands = [ROOT / "Mindustry启动器.exe"]
    if runtime is not None:
        cands.append(runtime / "Mindustry启动器.exe")
    cands.append(ROOT / "dist" / "Mindustry启动器" / "Mindustry启动器.exe")
    for cand in cands:
        if cand.exists():
            return cand
    raise SystemExit(
        "找不到 exe。先构建（_tools/build.py）或把 exe 路径当参数传进来。"
    )


def check_version_values(zarc) -> list[str]:
    """把包里的 `launcher.version` **真执行一遍**，断言值 == 源码里的值。

    ★ 为什么光查符号不够：符号检查只能证明「包里有个叫 `__version__` 的东西」，
    证明不了「它等于几」。「改了版本号但忘了重打」或者「部署时覆盖失败」时，
    exe 会**带着上一版的版本号**照常启动 —— 表面上一切正常，但
    `USER_AGENT` 会拿旧版本号去请求 GitHub、`full_version()` 会记错的版本、
    发版时清单文件名也跟着错。这类退化只有当有人去比对值的时候才看得见。
    所以这里不查名字，直接比。

    做法：从 PYZ 解出模块级 code object → 在一个干净命名空间里 exec →
    读值；再 `import launcher.version` 拿源码里的值；两边逐个比对。
    这两个模块**不 import 任何东西**（`launcher/version.py` 的 docstring 里有
    这条约定），所以 exec 不会因为缺依赖而失败。
    """
    import importlib

    bad: list[str] = []
    for module, attrs in VALUE_MODULES.items():
        if module not in zarc.toc:
            bad.append(f"{module} 整个模块不在包里（值无从核对）")
            print(f"  [FAIL] {module} 不在包里，无法核对版本值")
            continue
        try:
            code = zarc.extract(module)
            ns: dict = {"__name__": module}
            exec(code, ns)                       # noqa: S102 —— 核对的就是它
        except Exception as e:                   # noqa: BLE001
            bad.append(f"{module} 解出来的 code 执行失败: {e!r}")
            print(f"  [FAIL] {module} 执行失败: {e!r}")
            continue

        src = importlib.import_module(module)
        mismatched = []
        for attr in attrs:
            want = getattr(src, attr, "<源码里没有这个属性>")
            got = ns.get(attr, "<包里没有这个属性>")
            if got != want:
                mismatched.append(f"{attr}: 包里={got!r} 源码={want!r}")
        if mismatched:
            bad.extend(mismatched)
            for line in mismatched:
                print(f"  [FAIL] {module} 值不一致 -> {line}")
        else:
            print(f"  [OK  ] {module}  版本值 {len(attrs)} 项与源码一致"
                  f"（__version__={ns.get('__version__')!r}）")
    return bad


def main():
    exe = find_exe(sys.argv)
    print(f"检查 {exe}  ({exe.stat().st_size / 1024 / 1024:.2f} MB)")

    from PyInstaller.archive.readers import (
        CArchiveReader, ZlibArchiveReader,
    )

    car = CArchiveReader(str(exe))
    toc = car.toc
    pyz_entry = next((n for n in toc if n.endswith(".pyz")), None)
    if pyz_entry is None:
        raise SystemExit("exe 里没找到 PYZ 归档，形态是不是变了？")
    print(f"  PYZ 条目: {pyz_entry}")

    tmp = Path(tempfile.mktemp(suffix=".pyz"))
    try:
        # 注意 API：CArchiveReader.extract(name) 直接返回 bytes，
        # 不接文件句柄（PyInstaller 6.x 起是这样）。
        tmp.write_bytes(car.extract(pyz_entry))
        zarc = ZlibArchiveReader(str(tmp))

        fail = []
        for module, symbols in REQUIRED.items():
            if module not in zarc.toc:
                fail.append(f"{module} 整个模块不在包里")
                print(f"  [FAIL] {module} 不在包里")
                continue
            code = zarc.extract(module)
            names = collect_names(code)
            missing = [s for s in symbols if s not in names]
            if missing:
                fail.append(f"{module} 缺符号: {missing}")
                print(f"  [FAIL] {module} 缺: {missing}")
            else:
                print(f"  [OK  ] {module}  {len(symbols)} 个符号全部命中")

        print()
        fail.extend(check_version_values(zarc))
    finally:
        tmp.unlink(missing_ok=True)

    print()
    if fail:
        print(f"不通过：{len(fail)} 个模块有问题")
        for f in fail:
            print("  -", f)
        return 1
    print("通过：新代码确实在 exe 里，版本值也对得上")
    return 0


if __name__ == "__main__":
    sys.exit(main())
