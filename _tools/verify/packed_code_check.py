# -*- coding: utf-8 -*-
"""确认「新写的代码」真的进了打包好的 exe。

背景：PyInstaller 的 onedir 产物里，Python 模块被塞进 _internal/base_library.zip
旁边的 PYZ 归档（存在 exe 里）。源码改了但忘了重打、或者 spec 漏了模块时，
exe 能正常启动、看起来一切正常，只是跑的是旧逻辑 —— 光看「能打开」是发现不了的。

用法：
    python _tools/verify/packed_code_check.py [exe路径]

不带参数时查项目根那个已部署的 `Mindustry启动器.exe`；
打完之后想验产物，就把新 exe 的路径当参数传进来。
（注意：新 exe 和已部署的那份可能是同一个文件——打完记得先覆盖再验。）

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
from _paths import DATA, JRE, LOG, MANIFESTS, VERSIONS  # noqa: E402
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
    "launcher.gui": ["LogMixin"],
    "launcher.updates": [
        "StatusCallback", "downloading", "normalize_mirror",
        # 2026-09-22：改成一个来源一个来源地拉（来源表在 sources.py），
        # 不再在 updates 里写死仓库地址
        "_fetch_release", "sources", "parse_bool",
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

    先找项目根（已部署的形态），再找 dist/（刚构建、还没部署的形态）——
    两种状态下都能直接跑，不用手敲路径。
    """
    if len(argv) > 1:
        return Path(argv[1])
    for cand in (
        ROOT / "Mindustry启动器.exe",
        ROOT / "dist" / "Mindustry启动器" / "Mindustry启动器.exe",
    ):
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
