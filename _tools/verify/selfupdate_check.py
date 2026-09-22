# -*- coding: utf-8 -*-
"""启动器自更新（``launcher/selfupdate.py``）的离线验证。

不联网、不建窗口、**不碰真实数据** —— 全程在一个临时目录里跑。

盯的是三件最容易出问题的事：

  1. **版本比较**必须「严格大于」才动作：相等不重复下载，更小绝不降级
     （开发机上自编的版本可能比 Release 新，这是最后一道闸）。
  2. **更新包的安全解析**：路径穿越 / 越权替换（去动 config.json、jre）/
     格式不符 / 缺 sha，一律**整份丢掉** —— 半份计划比没有计划危险得多。
  3. **替换与回滚**：真跑一遍 ``apply_update_main``（父进程 pid 给 0，
     等于「旧进程已退出」），确认文件真的换了、失败真的回滚了、状态写对了。

跑法：``<python> _tools/verify/selfupdate_check.py``
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# _paths 是脚本的唯一路径来源（本文件在 _tools/verify/ 下，往上两层才是 _tools）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import ROOT  # noqa: E402

# 隔离：日志指到临时文件；自更新明确停用（万一以后有人在别处调它，
# 也不会因为跑这个脚本而真去联网）
_TMP = Path(tempfile.mkdtemp(prefix="mdt_selfupdate_"))
os.environ["MDT_LOG_FILE"] = str(_TMP / "selfupdate_check.log")
os.environ["MDT_NO_SELFUPDATE"] = "1"
sys.path.insert(0, str(ROOT))

from launcher import selfupdate as su  # noqa: E402
from launcher.utils import LAUNCHER_UPDATE_MODES, normalize_launcher_update  # noqa: E402

EXE = "Mindustry启动器.exe"
_results: list[tuple[bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    ok = bool(cond)
    _results.append((ok, name, detail))
    line = f"  {'[OK  ]' if ok else '[FAIL]'} {name}"
    if detail and not ok:
        line += f"    {detail}"
    print(line)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_package(
    path: Path,
    files: dict[str, bytes],
    *,
    version: str = "1.0.1",
    app_id: str = "mindustry-launcher",
    fmt: int = 1,
    extra_entries: list[dict] | None = None,
    omit: set[str] | None = None,
    wrong_sha: set[str] | None = None,
) -> Path:
    """造一个更新包（模拟发布脚本 ``make_release_zip.py`` 的产出）。"""
    omit = omit or set()
    wrong_sha = wrong_sha or set()
    entries = list(extra_entries or [])
    for rel, data in files.items():
        entries.append({
            "path": rel,
            "sha256": sha(b"WRONG" if rel in wrong_sha else data),
            "size": len(data),
        })
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("update.json", json.dumps({
            "format": fmt, "app_id": app_id, "version": version,
            "base_version": "1.0.0", "files": entries, "remove": [],
        }, ensure_ascii=False))
        for rel, data in files.items():
            if rel not in omit:
                z.writestr(f"files/{rel}", data)
    return path


# ---- ① 版本比较 ---------------------------------------------------------

def test_versions() -> None:
    print("--- 版本比较（必须严格大于才动作）---")
    check("parse_version 认 v 前缀与多位段",
          su.parse_version("v1.0.10") == (1, 0, 10))
    check("parse_version 认两段（1.1 → (1,1)）",
          su.parse_version("1.1") == (1, 1))
    check("parse_version 认不出给 (0,)",
          su.parse_version("abc") == (0,))
    check("远端 1.0.1 > 本地 1.0.0", su.is_newer("1.0.1", "1.0.0"))
    check("相等不算新（否则每次启动都重下）",
          not su.is_newer("1.0.0", "1.0.0"))
    check("远端更旧绝不降级", not su.is_newer("0.9.9", "1.0.0"))
    check("多位段按数值比（1.0.10 > 1.0.9）",
          su.is_newer("1.0.10", "1.0.9"))
    check("不给本地版本时跟当前 __version__ 比",
          su.is_newer("99.0.0"))


# ---- ② 更新包解析（安全边界）-------------------------------------------

def test_manifest_safety() -> None:
    print("--- 更新包解析：不安全的一律整份丢掉 ---")

    def one(path: str, **kw):
        raw = {"format": 1, "app_id": "mindustry-launcher", "version": "1.0.1",
               "files": [{"path": path, "sha256": "a" * 64, "size": 1}]}
        raw.update(kw)
        return su.parse_manifest(raw, EXE)

    check("正常包能解析（exe）", one(EXE) is not None)
    check("正常包能解析（_internal/）",
          one("_internal/a.dll") is not None)
    check("路径穿越 ../ 丢掉", one("../../evil.dll") is None)
    check("绝对路径丢掉", one("C:/Windows/evil.dll") is None)
    check("UNC 路径丢掉", one("//server/share/x.dll") is None)
    check("反斜杠穿越也丢掉", one("..\\..\\evil.dll") is None)
    check("不许动 config.json（用户数据）", one("config.json") is None)
    check("不许动 jre/（用户可能自己换过）", one("jre/bin/java.exe") is None)
    check("不许往程序目录塞别的 exe", one("evil.exe") is None)
    check("格式版本对不上就丢掉",
          su.parse_manifest(
              {"format": 99, "version": "1.0.1",
               "files": [{"path": EXE, "sha256": "a"}]}, EXE) is None)
    check("app_id 不是本项目就丢掉",
          su.parse_manifest(
              {"format": 1, "app_id": "someone-else", "version": "1.0.1",
               "files": [{"path": EXE, "sha256": "a"}]}, EXE) is None)
    check("没有 version 丢掉",
          su.parse_manifest({"format": 1, "files": []}, EXE) is None)
    check("files 不是数组丢掉",
          su.parse_manifest({"format": 1, "version": "1.0.1", "files": "x"},
                            EXE) is None)
    check("files 为空丢掉",
          su.parse_manifest({"format": 1, "version": "1.0.1", "files": []},
                            EXE) is None)
    check("缺 sha256 丢掉",
          su.parse_manifest(
              {"format": 1, "version": "1.0.1",
               "files": [{"path": EXE}]}, EXE) is None)
    check("不是对象丢掉", su.parse_manifest("nope", EXE) is None)

    # ★ parse_manifest 的契约是「**不抛异常**」。更新包是网络来的，
    #   字段类型什么样都可能，所以畸形 size 必须被吞掉而不是炸出去。
    weird = su.parse_manifest(
        {"format": 1, "version": "1.0.1",
         "files": [{"path": EXE, "sha256": "a" * 64, "size": "不是数字"}]}, EXE)
    check("畸形 size 不抛异常、按 0 处理",
          weird is not None and weird.files[0]["size"] == 0,
          f"实际 {weird.files if weird else None}")
    check("size 是 None 也按 0",
          one(EXE, files=[{"path": EXE, "sha256": "a" * 64, "size": None}])
          is not None)
    # 显式保护名单：即使前缀判断放宽了，这几个也不许被覆盖
    check("config.json / launcher.log 在保护名单里且不可替换",
          all(not su._is_replaceable(n, EXE) for n in su.PROTECTED_NAMES))
    check("exe 本身在没给名字时也认（顶层 .exe）",
          su._is_replaceable("别的名.exe", "") is True)
    check("给了名字就不再认别的 exe",
          su._is_replaceable("别的名.exe", EXE) is False)

    # remove 列表只认 _internal/ 下的
    plan = su.parse_manifest({
        "format": 1, "version": "1.0.1",
        "files": [{"path": EXE, "sha256": "a"}],
        "remove": ["_internal/gone.dll", "config.json", "../x", "jre/a"],
    }, EXE)
    check("remove 只保留 _internal/ 下的",
          plan is not None and plan.remove == ["_internal/gone.dll"],
          f"实际 {plan.remove if plan else None}")

    # 资产挑选取
    rel = {"assets": [{"name": "MindustryLauncher-v1.0.1-win64.zip"},
                      {"name": "MindustryLauncher-v1.0.1-update.zip"}]}
    picked = su._pick_update_asset(rel)
    check("资产只认 *-update.zip（不误取完整包）",
          picked is not None and picked["name"].endswith("-update.zip"))
    check("没有更新包时返回 None",
          su._pick_update_asset({"assets": [{"name": "full.zip"}]}) is None)


# ---- ③ 解包 + 逐文件校验 -----------------------------------------------

def test_staging() -> None:
    print("--- 解包到暂存区（带逐文件 sha256 校验）---")
    work = _TMP / "staging"
    work.mkdir(parents=True, exist_ok=True)
    files = {EXE: b"NEW-EXE", "_internal/a.dll": b"AA"}

    good = make_package(work / "good.zip", files)
    plan = su.stage_update(good, work / "out_good", EXE)
    check("正常包能解开", plan is not None)
    check("解出的 exe 内容正确",
          (work / "out_good" / EXE).read_bytes() == b"NEW-EXE")
    check("解出的 _internal 文件内容正确",
          (work / "out_good" / "_internal" / "a.dll").read_bytes() == b"AA")

    bad = make_package(work / "bad.zip", files, wrong_sha={EXE})
    check("sha256 不匹配就整份失败",
          su.stage_update(bad, work / "out_bad", EXE) is None)

    miss = make_package(work / "miss.zip", files, omit={EXE})
    check("包里缺 files/ 条目就整份失败",
          su.stage_update(miss, work / "out_miss", EXE) is None)

    evil = make_package(work / "evil.zip",
                        {"../../evil.dll": b"X"})
    check("路径穿越包在解包阶段就被拦下",
          su.stage_update(evil, work / "out_evil", EXE) is None)

    nojson = work / "nojson.zip"
    with zipfile.ZipFile(nojson, "w") as z:
        z.writestr("files/x", b"y")
    check("没有 update.json 就失败",
          su.stage_update(nojson, work / "out_nojson", EXE) is None)

    broken = work / "broken.zip"
    broken.write_bytes(b"this is not a zip")
    check("坏 zip 不抛异常（返回 None）",
          su.stage_update(broken, work / "out_broken", EXE) is None)


# ---- ④ 替换 / 回滚 -----------------------------------------------------

def _fresh_app(name: str) -> Path:
    app = _TMP / name
    if app.exists():
        shutil.rmtree(app)
    (app / "_internal").mkdir(parents=True, exist_ok=True)
    (app / EXE).write_bytes(b"OLD-EXE")
    (app / "_internal" / "a.dll").write_bytes(b"OLD-DLL")
    # 用户数据：更新器**不许**碰它，这里放一份用来做反向对照
    (app / "config.json").write_text('{"keep": true}', encoding="utf-8")
    (app / "versions").mkdir(exist_ok=True)
    (app / "versions" / "x").write_bytes(b"USER-DATA")
    return app


def test_apply_and_rollback() -> None:
    print("--- 替换成功 ---")
    app = _fresh_app("app_ok")
    files = {EXE: b"NEW-EXE", "_internal/a.dll": b"NEW-DLL"}
    pkg = make_package(_TMP / "ok.zip", files)
    staging = su.staged_files_dir(app)
    plan = su.stage_update(pkg, staging, EXE)
    check("暂存成功", plan is not None)
    plan_path = su.write_plan(app, plan, 0)   # pid=0 → 视为旧进程已退出
    rc = su.apply_update_main(plan_path)
    check("apply 返回 0（成功）", rc == 0, f"rc={rc}")
    check("exe 已换成新的", (app / EXE).read_bytes() == b"NEW-EXE")
    check("_internal 里的文件也换了",
          (app / "_internal" / "a.dll").read_bytes() == b"NEW-DLL")
    check("用户数据没被动（config.json）",
          (app / "config.json").read_text(encoding="utf-8") == '{"keep": true}')
    check("用户数据没被动（versions/）",
          (app / "versions" / "x").read_bytes() == b"USER-DATA")
    check("没留下 .mdt-old 备份",
          not list(app.rglob(f"*{su.BACKUP_SUFFIX}")))
    check("暂存区已清掉", not su.staged_root(app).exists())
    state = su.read_state(app)
    check("状态写成 done + 版本号",
          state is not None and state.get("status") == "done"
          and state.get("version") == "1.0.1", f"实际 {state}")

    print("--- 替换失败必须回滚 ---")
    app2 = _fresh_app("app_rollback")
    # 手工构造计划（下面不用 make_package）：_internal 有文件、exe 的源文件
    # 故意不放进暂存区 —— 模拟「包坏了/少了一半」，必须整份判失败并回滚。
    staging2 = su.staged_files_dir(app2)
    plan2 = su.UpdatePlan(version="1.0.1", exe_name=EXE, files=[
        {"path": "_internal/a.dll", "sha256": sha(b"NEW-DLL"), "size": 7},
        {"path": EXE, "sha256": sha(b"NEW-EXE"), "size": 7},
    ])
    staging2.mkdir(parents=True, exist_ok=True)
    (staging2 / "_internal").mkdir(parents=True, exist_ok=True)
    (staging2 / "_internal" / "a.dll").write_bytes(b"NEW-DLL")
    plan_path2 = su.write_plan(app2, plan2, 0)
    rc2 = su.apply_update_main(plan_path2)
    check("apply 返回非 0（失败）", rc2 != 0, f"rc={rc2}")
    check("失败后 _internal 的文件回滚成旧的",
          (app2 / "_internal" / "a.dll").read_bytes() == b"OLD-DLL")
    check("失败后 exe 仍是旧的", (app2 / EXE).read_bytes() == b"OLD-EXE")
    check("失败后没留下 .mdt-old",
          not list(app2.rglob(f"*{su.BACKUP_SUFFIX}")))
    state2 = su.read_state(app2)
    check("状态写成 failed 且带原因",
          state2 is not None and state2.get("status") == "failed"
          and bool(state2.get("message")), f"实际 {state2}")

    print("--- 计划文件不可读时不炸 ---")
    bogus = _TMP / "bogus.json"
    bogus.write_text("{not json", encoding="utf-8")
    check("读不了计划返回非 0", su.apply_update_main(bogus) != 0)

    print("--- 状态文件的读/清 ---")
    check("read_state 认得出刚写的内容",
          su.read_state(app) is not None)
    su.clear_state(app)
    check("clear_state 之后读到 None", su.read_state(app) is None)
    check("没有状态文件时 read_state 给 None",
          su.read_state(_TMP / "app_ok_none") is None)


# ---- ⑤ 档位总开关 ------------------------------------------------------

def test_mode() -> None:
    print("--- 档位与停用条件 ---")
    check("三档就是 auto/check/off",
          LAUNCHER_UPDATE_MODES == ("auto", "check", "off"))
    check("auto 认得", normalize_launcher_update("auto") == "auto")
    check("CHECK 大小写不敏感",
          normalize_launcher_update("CHECK") == "check")
    check("off 认得", normalize_launcher_update("off") == "off")
    check("脏值退默认 auto 而不是乱猜",
          normalize_launcher_update("maybe") == "auto")
    check("非字符串退默认", normalize_launcher_update({"a": 1}) == "auto")

    class Cfg:
        def __init__(self, value):
            self.value = value

        def get(self, key):
            return self.value

    check("环境变量禁用优先于任何配置",
          su.mode_of(Cfg("auto")) == su.MODE_OFF)
    os.environ.pop("MDT_NO_SELFUPDATE")
    check("源码运行一律 off（不会把自己编的版本顶掉）",
          su.mode_of(Cfg("auto")) == su.MODE_OFF)
    os.environ["MDT_NO_SELFUPDATE"] = "1"

    # 配置层：新增的键要有默认值，且坏值退默认时要点名键名
    from launcher.config import ConfigManager  # noqa: E402
    check("config 里有 launcher_update 且默认 auto",
          ConfigManager.GLOBAL_DEFAULTS.get("launcher_update") == "auto")
    check("强转表里登记了它（界面与手改文件走同一张表）",
          "launcher_update" in ConfigManager.GLOBAL_COERCERS)


def test_release_baseline() -> None:
    """发布侧：从「上一版发布包 zip」反推基线。

    v1.0.0 发出去时还没有自更新功能，包里没有 manifest.json。没有基线就
    只能出**全量**更新包（十几 MB），而用户明确在意下载体积，所以留了
    `--baseline-from-zip` 这条路。它做两件事最容易错：**削掉 zip 内的一级
    目录名**（不削就会把文件写到用户那边的错误位置），以及**只收程序文件**
    （收进 jre 就会去覆盖用户自己换过的 Java）。
    """
    print("--- 发布侧：从发布包反推基线 ---")
    import importlib.util  # noqa: E402

    spec = importlib.util.spec_from_file_location(
        "_mrz_for_check", ROOT / "_tools" / "make_release_zip.py"
    )
    mrz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mrz)

    top = "Mindustry启动器"
    zp = _TMP / "MindustryLauncher-v1.0.0-win64.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(f"{top}/{EXE}", b"exe-bytes")
        z.writestr(f"{top}/_internal/a.dll", b"a")
        z.writestr(f"{top}/_internal/sub/b.dll", b"b")
        z.writestr(f"{top}/jre/bin/java.exe", b"jre")      # 不参与自更新
        z.writestr(f"{top}/使用说明.txt", b"hi")            # 不参与自更新
        z.writestr(f"{top}/LICENSE", b"gpl")               # 不参与自更新
        z.writestr("readme-outside.txt", b"x")             # 不该被收

    man = mrz.manifest_from_release_zip(zp)
    got = {f["path"]: f["sha256"] for f in man["files"]}
    check("版本号从发布包文件名里拿到", man["version"] == "1.0.0", man["version"])
    check("只收 exe 与 _internal/（jre、说明、LICENSE 都排除）",
          set(got) == {EXE, "_internal/a.dll", "_internal/sub/b.dll"},
          str(sorted(got)))
    check("削掉 zip 里的一级目录名（不削就会写错位置）",
          not any(k.startswith(top + "/") for k in got))
    check("sha256 与包内字节一致",
          got[EXE] == sha(b"exe-bytes")
          and got["_internal/sub/b.dll"] == sha(b"b"))
    check("size 也量了（更新包要拿它算体积）",
          all(f.get("size") for f in man["files"]))

    # 认不出程序文件时必须报错，不能悄悄返回空清单 —— 空基线的后果是
    # 「所有文件都算没变」，更新包会是个空壳。
    junk = _TMP / "not-a-release.zip"
    with zipfile.ZipFile(junk, "w") as z:
        z.writestr("readme.txt", b"hi")
    try:
        mrz.manifest_from_release_zip(junk)
        check("不是发布包时必须报错", False, "没报错，返回了清单")
    except SystemExit:
        check("不是发布包时必须报错", True)


def test_api_override() -> None:
    """查新版的地址可被 ``MDT_SELFUPDATE_API`` 盖掉（端到端验证要用它指假接口）。"""
    print("--- 查新版地址可覆盖 ---")
    os.environ.pop(su.API_ENV, None)
    check("没设环境变量时用官方地址", su.update_api() == su.SELF_UPDATE_API)
    os.environ[su.API_ENV] = "http://127.0.0.1:9/latest.json"
    check("设了就认它", su.update_api() == "http://127.0.0.1:9/latest.json")
    os.environ[su.API_ENV] = "https://proxy.example/latest"
    check("https 也认", su.update_api() == "https://proxy.example/latest")
    os.environ[su.API_ENV] = "D:/some/file.json"
    check("非 http(s) 当没设（笔误不该让检查静默失效）",
          su.update_api() == su.SELF_UPDATE_API)
    os.environ[su.API_ENV] = "   "
    check("空白当没设", su.update_api() == su.SELF_UPDATE_API)
    os.environ.pop(su.API_ENV, None)


def test_updater_runtime() -> None:
    """执行体的运行时必须就位。

    ★ 这条是**真机端到端验证抓回来的**：onedir 打包的 exe 单独拎出来跑不起来 ——
      它得靠同目录的 ``_internal/``。当时 ``launch_updater`` 只复制了 exe，
      结果链路每一环都在报「已启动」，执行体却什么都没干（写不出日志、也不报错），
      因为它加载运行时那一步就静悄悄死了。进程内验证原来没覆盖到它。
    """
    print("--- 执行体要有自己的运行时 ---")
    app = _fresh_app("app_runtime")
    upd = _TMP / "updater_home"
    upd.mkdir(parents=True, exist_ok=True)

    check("app 目录里没有 _internal 时如实返回 False",
          su.updater_runtime_link(upd, _TMP / "根本没有这个目录") is False)
    check("接上之后返回 True", su.updater_runtime_link(upd, app) is True)
    link = upd / su.INTERNAL_DIR_NAME
    check("执行体旁边真的出现了 _internal",
          link.is_dir() and (link / "a.dll").is_file())
    check("指向的是程序目录那份（不是拷贝）",
          os.path.realpath(link) == os.path.realpath(app / su.INTERNAL_DIR_NAME))
    check("重复调用是幂等的", su.updater_runtime_link(upd, app) is True)
    check("只动 %TEMP% 那边，不在程序目录留东西",
          {p.name for p in app.iterdir()}
          == {su.INTERNAL_DIR_NAME, "config.json", "versions", EXE})

    old = upd / "stale_copy.exe"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 48 * 3600,) * 2)
    su.cleanup_updater_dir(hours=1, root=upd)
    check("过期的执行体副本会被清掉", not old.exists())
    check("目标还在时联接留着（省一次重建）", link.is_dir())

    # ★ 真机验证抓回来的第二条：程序目录**搬走/换地方**之后，%TEMP% 里那份联接
    #   就成了断链，而断链的联接 os.path.islink 给的是假、is_dir 也是假 ——
    #   当时它既没被摘掉、又挡住了新建，于是执行体再也起不来（更新静默失败）。
    #   症状是「第一次能更新，之后每次都不行」，很容易被当成偶发。
    gone = _fresh_app("app_moved_away")
    check("先接一次（模拟正常跑过一次）",
          su.updater_runtime_link(upd, gone) is True)
    shutil.rmtree(gone)                  # 用户把程序目录搬走了 / 删了
    # 判「链还在不在」不能拿两个 realpath 比 —— 目标没了以后 realpath 不查存在，
    # 两边会归一化成同一串、恒等。要看的是**透过链读得到东西吗**。
    check("程序目录搬走后联接成了断链（不是「目标还在」）",
          not Path(link).resolve().is_dir() and not (link / "a.dll").exists())
    again = _fresh_app("app_moved_away")
    check("程序目录换了地方后仍能重新接上（断链要被摘掉）",
          su.updater_runtime_link(upd, again) is True)
    check("重新接上后指向新目录",
          os.path.realpath(link) == os.path.realpath(again / su.INTERNAL_DIR_NAME))
    check("摘联接/重建都没动过被指向的那个目录",
          (again / su.INTERNAL_DIR_NAME / "a.dll").read_bytes() == b"OLD-DLL")


def main() -> int:
    print(f"项目根：{ROOT}")
    print(f"临时目录：{_TMP}")
    print()
    test_versions()
    print()
    test_manifest_safety()
    print()
    test_staging()
    print()
    test_apply_and_rollback()
    print()
    test_mode()
    print()
    test_release_baseline()
    print()
    test_api_override()
    print()
    test_updater_runtime()
    print()

    bad = [name for ok, name, _ in _results if not ok]
    print("=" * 64)
    if bad:
        print(f"失败 {len(bad)}/{len(_results)} 项：")
        for name in bad:
            print(f"  - {name}")
        return 1
    print(f"通过：{len(_results)}/{len(_results)} 项全部符合预期")
    return 0


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
