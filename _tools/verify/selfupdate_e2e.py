# -*- coding: utf-8 -*-
"""启动器自更新的**真机端到端**验证：起 exe → 关窗口 → 外部执行体换文件 → 重开看新版。

和 ``selfupdate_check.py`` 的分工：那边是**进程内**验证（把 ``apply_update_main``
当函数调、父进程 pid 传 0 顶替「旧进程已退出」），快、能进回归；这一支走**真链路** ——
真起 exe、真 WM_CLOSE 退出、真让 ``%TEMP%`` 里那份执行体在进程消失后动手替换。
唯一还没被覆盖的就是「等 pid 真的消失」那一段（那本来就是靠 pid 判的，且随包带得走）。

需要两个**已经打包好**的目录：

    --app-dir       装好的**旧版**（exe + ``_internal``，可带 jre）
    --newer-dir     造出来的**新版**（exe + ``_internal``，版本号必须更高）
    --newer-version 新版目录里那个 exe 自己的版本号（脚本读不出来，得你说）

不用真发版：脚本拿**真发布脚本**的 ``write_update_package`` 给它做个精简更新包，
再用本地 http 服务假装成 GitHub 的 ``/releases/latest``，把环境变量
``MDT_SELFUPDATE_API`` 指过去。所以量的是真网络路径（HTTP + sha256 + 解包 + 白名单）。

跑法（示例）::

    python _tools/verify/selfupdate_e2e.py \
        --app-dir  ../测试版本 \
        --newer-dir D:/tmp/mdt_newer \
        --newer-version 9.9.9

⚠️ 会真弹一次 GUI 窗口（两次）；全程在临时目录里，**不碰真实数据**。
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# 同目录的 exe 脚本里已经有「按 pid 找窗口 / 等窗口出现」，直接复用，
# 免得两处各写一份（那两份迟早会走样）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from exe_edge_check import WM_CLOSE, USER32, _wait_window  # noqa: E402
from sandbox_seed import seed_latest_versions  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # _tools/
from _paths import ROOT  # noqa: E402

EXE_NAME = "Mindustry启动器.exe"
INTERNAL = "_internal"
STAGING = ".update"
STATE = "update-state.json"
BACKUP_SUFFIX = ".mdt-old"

_results: list[tuple[bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    ok = bool(cond)
    _results.append((ok, name, detail))
    line = f"  {'[OK  ]' if ok else '[FAIL]'} {name}"
    if detail and not ok:
        line += f"    {detail}"
    print(line, flush=True)


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_make_release_zip():
    """把发布脚本当模块加载（它的打包/清单逻辑才是被测对象，不能另写一份）。"""
    spec = importlib.util.spec_from_file_location(
        "_mrz_e2e", ROOT / "_tools" / "make_release_zip.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def linked_or_copied(src: Path, dst: Path) -> None:
    """jre 有 60 MB，优先建目录联接；建不了再老老实实拷。

    ⚠️ ``capture_output`` 不要配 ``text=True``：``mklink`` 的输出是 GBK，
    按 UTF-8 解会在读线程里抛 UnicodeDecodeError（测试里见过，很吵）。
    """
    if os.name == "nt":
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
            capture_output=True,
        )
        if r.returncode == 0:
            return
    shutil.copytree(src, dst)


def run_env(extra: dict[str, str]) -> dict[str, str]:
    """给被起的 exe 准备环境：**摘掉**会干扰自更新的那两个，再叠上额外的。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("MDT_NO_SELFUPDATE", "MDT_LOG_FILE", "MDT_RUNTIME_DIR")}
    env.update(extra)
    return env


def internal_map(root: Path) -> dict[str, str]:
    """``root/_internal`` 下每个文件 → sha256（相对 ``_internal`` 的 posix 路径）。"""
    base = root / INTERNAL
    return {
        p.relative_to(base).as_posix(): sha_file(p)
        for p in base.rglob("*") if p.is_file()
    }


def read_startup_line(root: Path) -> str:
    """取日志里**最后一次**「启动，数据根」那一行。

    ⚠️ 必须取最后一条：日志是追加的，重启一次就多一条。取第一条会永远读到
    第一次启动的版本号 —— 正是这一条把「重启后是不是新版」误判成失败的。
    版本号也只有这里可靠（窗口标题里没有版本号）。
    """
    log = root / "launcher.log"
    if not log.is_file():
        return ""
    hit = ""
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "启动，数据根" in line:
            hit = line
    return hit


def wait_in_log(root: Path, needle: str, timeout: float = 20.0) -> bool:
    """等日志里出现某句话（最多 ``timeout`` 秒）。

    ⚠️ 不能「看到窗口就去读日志」：自更新的启动动作是 ``root.after(3000, ...)``
    排的 —— 特意等窗口先可用 3 秒。窗口出现的那一刻它还没跑，日志里自然没有
    那句「上次自更新已完成」，会误判成失败（就是被这条坑过一次）。
    """
    log = root / "launcher.log"
    end = time.time() + timeout
    while True:
        try:
            if needle in log.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
        if time.time() >= end:
            return False
        time.sleep(0.5)


def version_of(root: Path) -> str:
    """从启动日志里抠出版本号（``Mindustry 启动器 1.1.0 启动，数据根 …``）。"""
    line = read_startup_line(root)
    if not line:
        return ""
    tail = line.split("启动，数据根")[0].strip()
    return tail.rsplit(" ", 1)[-1].strip()


def start_app(root: Path, env_extra: dict[str, str], window_timeout: float):
    proc = subprocess.Popen(
        [str(root / EXE_NAME)], cwd=str(root), env=run_env(env_extra)
    )
    win = _wait_window(window_timeout, proc)
    return proc, win


def close_window(proc: subprocess.Popen, win) -> bool:
    if win is None:
        proc.terminate()
        return False
    USER32.PostMessageW(win[0], WM_CLOSE, 0, 0)
    deadline = time.perf_counter() + 40
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.2)
    proc.kill()
    return False


def wait_for(pred, timeout: float, interval: float = 0.4):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(interval)
    return None


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="自更新真机端到端验证")
    ap.add_argument("--app-dir", required=True, type=Path,
                    help="装好的旧版目录（exe + _internal）")
    ap.add_argument("--newer-dir", required=True, type=Path,
                    help="造出来的新版目录（exe + _internal）")
    ap.add_argument("--newer-version", required=True,
                    help="新版 exe 自己的版本号（要比旧版高）")
    ap.add_argument("--window-timeout", type=float, default=45.0)
    ap.add_argument("--stage-timeout", type=float, default=90.0)
    ap.add_argument("--keep", action="store_true", help="跑完保留临时目录（排查用）")
    args = ap.parse_args()

    app_src = args.app_dir.resolve()
    new_src = args.newer_dir.resolve()
    for d in (app_src, new_src):
        if not (d / EXE_NAME).is_file() or not (d / INTERNAL).is_dir():
            raise SystemExit(f"{d} 里没有 {EXE_NAME} + {INTERNAL}/ —— 不是打包产物目录")

    sys.path.insert(0, str(ROOT))
    os.environ["MDT_LOG_FILE"] = str(Path(tempfile.gettempdir()) / "mdt_e2e_driver.log")
    from launcher import selfupdate as su

    newer_version = args.newer_version.strip()
    tmp = Path(tempfile.mkdtemp(prefix="mdt_e2e_"))
    print(f"项目根  ：{ROOT}")
    print(f"临时目录：{tmp}")
    print(f"旧版目录：{app_src}")
    print(f"新版目录：{new_src}（声称 v{newer_version}）")
    print()

    # ---- 准备：假更新包 ----
    print("--- ① 用真发布脚本造一个精简更新包 ---")
    mrz = load_make_release_zip()
    old_manifest = mrz.build_manifest(mrz.program_items(app_src), "0.0.0")
    upd_zip, n_files, raw = mrz.write_update_package(
        new_src, tmp, newer_version, old_manifest, 6
    )
    print(f"  {upd_zip.name}：{n_files} 个文件，原始 {raw/1e6:.2f} MB，"
          f"打包后 {upd_zip.stat().st_size/1e6:.2f} MB")
    check("更新包带了文件（不是空壳）", n_files > 0)
    check("更新包里没有 jre/（不覆盖用户自己的 Java）",
          all(not n.startswith("files/jre/") for n in
              __import__("zipfile").ZipFile(upd_zip).namelist()))

    # ---- 准备：假的 /releases/latest ----
    fake_release = {
        "tag_name": f"v{newer_version}",
        "body": "端到端验证用的假版本（不是真的发布）",
        "html_url": "http://127.0.0.1/fake",
        "assets": [{
            "name": upd_zip.name,
            "size": upd_zip.stat().st_size,
            "digest": "sha256:" + sha_file(upd_zip),
            "browser_download_url": "",       # 下面拿到端口再补
        }],
    }
    api_file = tmp / "latest.json"

    handler = type(
        "QuietHandler",
        (http.server.SimpleHTTPRequestHandler,),
        {
            "log_message": lambda *a, **k: None,
            "__init__": lambda self, *a, **k: http.server.SimpleHTTPRequestHandler
            .__init__(self, *a, directory=str(tmp), **k),
        },
    )
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    fake_release["assets"][0]["browser_download_url"] = (
        f"http://127.0.0.1:{port}/{upd_zip.name}"
    )
    api_file.write_text(json.dumps(fake_release, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    api_url = f"http://127.0.0.1:{port}/latest.json"
    print(f"  假接口：{api_url}")
    try:
        import urllib.request
        with urllib.request.urlopen(api_url, timeout=5) as r:
            check("假接口能被取到", json.loads(r.read())["tag_name"] == f"v{newer_version}")
    except Exception as e:                                            # noqa: BLE001
        check("假接口能被取到", False, str(e))
    print()

    # ---- 沙箱 ----
    root = tmp / "app"
    root.mkdir()
    shutil.copy2(app_src / EXE_NAME, root / EXE_NAME)
    shutil.copytree(app_src / INTERNAL, root / INTERNAL)
    if (app_src / "jre").is_dir():
        linked_or_copied(app_src / "jre", root / "jre")
    (root / "versions" / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "versions" / "objects").mkdir(parents=True, exist_ok=True)
    (root / "backups" / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "backups" / "objects").mkdir(parents=True, exist_ok=True)
    # 预置最新版本清单 —— 否则 auto_update 一开机就真去下 200+ MB 的游戏
    seed_latest_versions(root)

    exe_before = sha_file(root / EXE_NAME)
    internal_before = internal_map(app_src)
    internal_new = internal_map(new_src)
    # 新旧之间**真正变了**的 _internal 文件：更新包只该带这些，也只该换这些
    changed_rels = sorted(r for r, h in internal_new.items()
                          if internal_before.get(r) != h)
    print(f"  新旧之间变了的 _internal 文件：{len(changed_rels)} 个"
          f"{'（' + ', '.join(changed_rels[:3]) + ('…' if len(changed_rels) > 3 else '') + '）' if changed_rels else ''}")
    print(f"--- ② 起一次旧版（沙箱 {root}）---")
    proc, win = start_app(root, {su.API_ENV: api_url}, args.window_timeout)
    check("旧版能起出窗口", win is not None, f"标题={win[1] if win else None}")
    if win is None:
        _dump(root)
        return _finish(tmp, args.keep)

    old_version = version_of(root)
    print(f"  日志里的旧版本：{old_version!r}")
    check("旧版版本号读得到", bool(old_version), read_startup_line(root))
    check("假版本确实更高（否则这条链根本没意义）",
          su.is_newer(newer_version, old_version),
          f"{newer_version} vs {old_version}")

    # 用户数据哨兵：替换前后必须一个字都没变
    sentinel = root / "versions" / "objects" / "user-data.bin"
    sentinel.write_bytes(b"USER-DATA-MUST-NOT-CHANGE")
    cfg_before = (root / "config.json").read_bytes()

    plan_file = root / STAGING / "plan.json"
    print("  等后台把更新包下来并暂存（走的是真 HTTP）…")
    staged = wait_for(lambda: plan_file.is_file(), args.stage_timeout)
    check("更新包已下载并暂存（.update/plan.json）", staged is not None,
          f"等了 {args.stage_timeout:.0f}s")
    log_txt = (root / "launcher.log").read_text(encoding="utf-8", errors="replace")
    check("日志里说了「等退出时应用」", "等退出时应用" in log_txt)
    check("暂存区里有解好的新 exe", (root / STAGING / "files" / EXE_NAME).is_file())

    # ---- 关窗口 → 执行体接手 ----
    print("--- ③ 关窗口（走真实 on_closing），把替换交给外部执行体 ---")
    closed = close_window(proc, win)
    check("关窗口能干净退出", closed, f"exit={proc.poll()}")
    check("退出码是 0", proc.poll() == 0, f"exit={proc.poll()}")

    state_file = root / STATE
    st = wait_for(lambda: state_file.is_file() and json.loads(
        state_file.read_text(encoding="utf-8")), 120)
    check("执行体写出了 update-state.json", st is not None, "等了 120s")
    if st:
        check("状态是 done", st.get("status") == "done", str(st))
        check("状态里的版本号是假版本", st.get("version") == newer_version, str(st))

    print("--- ④ 核对替换结果 ---")
    check("exe 已换成新版",
          sha_file(root / EXE_NAME) == sha_file(new_src / EXE_NAME))
    if changed_rels:
        swapped = [r for r in changed_rels
                   if (root / INTERNAL / r).is_file()
                   and sha_file(root / INTERNAL / r) == internal_new[r]]
        check(f"变过的 {len(changed_rels)} 个 _internal 文件都换成了新版",
              len(swapped) == len(changed_rels),
              f"只换了 {len(swapped)}/{len(changed_rels)}："
              f"{sorted(set(changed_rels) - set(swapped))[:5]}")
        untouched = [r for r, h in internal_before.items()
                     if r not in set(changed_rels)
                     and (root / INTERNAL / r).is_file()
                     and sha_file(root / INTERNAL / r) == h]
        check("没变过的 _internal 文件一个也没被动",
              len(untouched) == len(internal_before) - len(changed_rels),
              f"{len(untouched)}/{len(internal_before) - len(changed_rels)}")
    else:
        check("新旧 _internal 全等（那更新包只该带 exe）", n_files == 1, f"{n_files} 个")
    check("没留下 .mdt-old 备份", not list(root.rglob(f"*{BACKUP_SUFFIX}")))
    check("暂存区 .update 已清掉", not (root / STAGING).exists())
    check("用户数据一个字节没动（versions/objects 哨兵）",
          sentinel.read_bytes() == b"USER-DATA-MUST-NOT-CHANGE")
    check("config.json 一个字节没动",
          (root / "config.json").read_bytes() == cfg_before)
    updlog = root / "update.log"
    check("执行体留下了 update.log", updlog.is_file())
    if updlog.is_file():
        t = updlog.read_text(encoding="utf-8", errors="replace")
        check("执行体日志里有「已替换」与「更新完成」",
              "已替换" in t and "更新完成" in t)
        check("替换顺序是先 _internal 后 exe",
              t.find(f"已替换 {INTERNAL}/") < t.rfind(f"已替换 {EXE_NAME}")
              if f"已替换 {INTERNAL}/" in t else False)

    # ---- 再起一次：看到新版本 ----
    print("--- ⑤ 重开，确认跑的是新版 ---")
    proc2, win2 = start_app(root, {su.API_ENV: api_url}, args.window_timeout)
    check("新版能起出窗口", win2 is not None)
    new_version_seen = version_of(root)
    print(f"  日志里的现版本：{new_version_seen!r}")
    check("重启后日志里的版本变成假版本了", new_version_seen == newer_version,
          f"实际 {new_version_seen!r}")
    # 这条要**等**：报「上次完成」的动作排在窗口可用后 3 秒（见 wait_in_log）
    check("状态栏/日志报了「上次自更新已完成」",
          wait_in_log(root, "上次自更新已完成"))
    time.sleep(3.0)                  # 再等等，让那次「查新版」真跑完（假接口回得快）
    check("已经是最新时不再重复暂存",
          not (root / STAGING / "plan.json").exists())
    check("重开也能干净退出", close_window(proc2, win2),
          f"exit={proc2.poll()}")

    exe_unchanged = sha_file(root / EXE_NAME) == sha_file(new_src / EXE_NAME)
    check("第二次退出后 exe 没被再动（幂等）", exe_unchanged)
    check("exe 确实换过了（现在的 sha 不等于换之前那个）",
          sha_file(root / EXE_NAME) != exe_before)

    httpd.shutdown()
    return _finish(tmp, args.keep)


def _dump(root: Path) -> None:
    print("\n[沙箱状态]")
    for p in sorted(root.iterdir()):
        print(f"  {p.name}")
    log = root / "launcher.log"
    if log.is_file():
        print("\n[launcher.log 末尾]")
        print("\n".join(log.read_text(encoding="utf-8", errors="replace")
                        .splitlines()[-25:]))


def _finish(tmp: Path, keep: bool) -> int:
    bad = [n for ok, n, _ in _results if not ok]
    print()
    print("=" * 64)
    if bad:
        print(f"失败 {len(bad)}/{len(_results)} 项：")
        for n in bad:
            print(f"  - {n}")
    else:
        print(f"通过：{len(_results)}/{len(_results)} 项全部符合预期")
    if keep:
        print(f"临时目录保留在：{tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
