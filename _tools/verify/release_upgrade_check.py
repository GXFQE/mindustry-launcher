# -*- coding: utf-8 -*-
"""拿**真发布资产**跑一遍升级仿真：老完整包 + 真更新包 == 新完整包？

## 补的是哪一层

- `selfupdate_check.py` 验的是**逻辑**（拿内存里现造的包跑检查/换文件/回滚）；
- `selfupdate_e2e.py` 验的是**真 exe 把自己换掉**；
- 本脚本补中间那一层：**这两个真正要传上去的 zip，配在一起能不能把老版本升成新版**。

## 为什么必须跑

更新包是按「上一版完整包里的 `manifest.json`」算差异生成的，而清单和包是
**两次独立写入**。只要其中任何一次对不上（基线挑错、清单只写了一处、
`Path.write_text` 把 `\n` 翻成 `\r\n`、payload 与声明不符），发出去的更新包就会
**少换一个文件**或**换错一个文件**。这种错在用户那儿表现为「更新完界面还是旧的」
甚至「更新完起不来」，而本地任何单测都不会红。

## 证明链（四步，每步都必要）

1. 老完整包里的**真实条目哈希** == 老 `manifest.json` 声明的哈希
   —— 证明基线清单没说谎（不然下面全白算）；
2. 新完整包同上 —— 证明「升级完应该长什么样」可信；
3. 更新包 payload 的**真实哈希** == `update.json` 声明的哈希，
   且 payload 条目集合 == 声明的 `files`（**不能多带东西**）；
4. **把更新套在老清单上**，结果逐条等于新清单 —— 这才是真正的「升级 == 重装」。

外加两条防呆：
- 更新包里每个路径都要过 `selfupdate._is_replaceable`（**直接 import 真实现**，
  不复写规则）—— 过不了就说明这个更新包有能力碰用户数据或 `jre/`；
- 完整包里**不许出现用户数据**（`config.json` / `versions/` / `Backups/` / `logs/`）
  —— 交付包带上用户数据是比升级失败更严重的事故。

★ 全程只在内存里比哈希、**不解压**：一是快（不用落 1000+ 个文件），
二是避开工具的「单轮 ≥50 文件批量删除」闸（临时目录里上千个文件，
清理时会被那条闸拦下）。

用法：
    python _tools/verify/release_upgrade_check.py              # 自动挑最新的一对
    python _tools/verify/release_upgrade_check.py --from <旧版本> --to <新版本>
    python _tools/verify/release_upgrade_check.py --releases <目录>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # _tools/
from _paths import ROOT                                          # noqa: E402

sys.path.insert(0, str(ROOT))
# ★ 白名单直接 import 真实现（selfupdate._is_replaceable），不在这里复写一份 ——
#   复写的那份会和真规则各自漂移，检查就失去意义了。
from launcher.selfupdate import _is_replaceable                  # noqa: E402

RELEASES_DIR = ROOT / "_history" / "releases"
FULL_PAT = "MindustryLauncher-v{ver}-win64.zip"
UPDATE_PAT = "mindustry-launcher-v{ver}-update.zip"

# 交付包里绝不许出现的用户数据（前缀，大小写不敏感）
FORBIDDEN_IN_PACKAGE = ("versions/", "backups/", "logs/", "extensions/")
FORBIDDEN_NAMES = ("config.json", "launcher.log", "launcher.dev.log")

_VER_RE = re.compile(r"\d+")


def _ver_key(text: str) -> tuple[int, ...]:
    """版本号比大小（和 selfupdate.parse_version 一个口径）。"""
    nums = tuple(int(x) for x in _VER_RE.findall(str(text)))
    return nums or (0,)


def _sha256_of(zf: zipfile.ZipFile, arcname: str) -> str:
    h = hashlib.sha256()
    with zf.open(arcname) as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _strip_top(names: list[str]) -> tuple[str, dict[str, str]]:
    """去掉 zip 里那层顶层目录，返回 ``(顶层名, {相对路径: 条目名})``。

    顶层名是「所有条目共有的那个前缀」——发布包的顶层叫 `Mindustry启动器/`，
    但别把它写死：将来改名了这里要照样能跑。没有共有前缀（平铺形态）就返回 ""。
    """
    files = [n for n in names if not n.endswith("/")]
    if not files:
        return "", {}
    head = files[0].split("/")[0] + "/"
    top = head if all(n.startswith(head) for n in files) else ""
    return top.rstrip("/"), {n[len(top):]: n for n in files}


def _read_json(zf: zipfile.ZipFile, arcname: str) -> dict:
    # utf-8-sig：带不带 BOM 都能读（清单是 utf-8，使用说明是 utf-8-sig）
    return json.loads(zf.read(arcname).decode("utf-8-sig"))


def _manifest_map(doc: dict) -> dict[str, str]:
    """`manifest.json` / `update.json` 的 `files` → ``{路径: sha256}``。

    ★ 历史上 `files` 一直是 list；但写成 dict 的形态也能读，
    免得将来格式真变了这里直接炸（宽容读取，严格断言）。
    """
    entries = doc.get("files")
    if isinstance(entries, dict):
        return {str(k): str(v) for k, v in entries.items()}
    out: dict[str, str] = {}
    for e in entries or []:
        out[str(e["path"])] = str(e["sha256"])
    return out


def _exe_name(manifest: dict[str, str]) -> str:
    """从清单里认顶层那个 exe（更新白名单的 `exe_name` 参数要它）。"""
    for p in manifest:
        if "/" not in p and p.lower().endswith(".exe"):
            return p
    return ""


def _check_package_truthful(label: str, path: Path, problems: list[str]) -> dict:
    """打开一个完整包，做「证明链第 1/2 步」+ 用户数据自查，返回其清单映射。"""
    print(f"  [{label}] {path.name}  ({path.stat().st_size / 1024 / 1024:.1f} MB)")
    with zipfile.ZipFile(path) as zf:
        _, rel = _strip_top(zf.namelist())
        manifest_arc = next((a for r, a in rel.items()
                             if r.split("/")[-1] == "manifest.json"), None)
        if manifest_arc is None:
            problems.append(f"{label}: 包里没有 manifest.json，无法作为升级基线")
            return {}
        doc = _read_json(zf, manifest_arc)
        declared = _manifest_map(doc)
        if not declared:
            problems.append(f"{label}: manifest.json 的 files 是空的")
            return {}

        # ---- 证明链 1/2：清单说的哈希，包里的真实内容对不对 ----
        bad = missing = 0
        for rp, want in declared.items():
            arc = rel.get(rp)
            if arc is None:
                missing += 1
                if missing <= 3:
                    problems.append(f"{label}: 清单里有、包里没有 -> {rp}")
                continue
            if _sha256_of(zf, arc) != want:
                bad += 1
                if bad <= 3:
                    problems.append(f"{label}: 内容与清单哈希不符 -> {rp}")
        if missing:
            problems.append(f"{label}: 共 {missing} 个清单条目在包里找不到")
        if bad:
            problems.append(f"{label}: 共 {bad} 个条目哈希与清单不符")

        # ---- 交付包不许夹带用户数据 ----
        carried = [r for r in rel
                   if r.lower() in FORBIDDEN_NAMES
                   or r.lower().startswith(FORBIDDEN_IN_PACKAGE)]
        if carried:
            problems.append(f"{label}: ★ 交付包里带了用户数据 -> {carried[:5]}")

        print(f"     真实内容 == 清单（{len(declared)} 条，缺失 {missing}、"
              f"不符 {bad}）；夹带用户数据 {len(carried)} 项")
        return {"version": str(doc.get("version", "")),
                "files": declared,
                "names": set(rel)}


def _check_update_package(path: Path, problems: list[str]) -> dict:
    """打开更新包，做「证明链第 3 步」+ 白名单自查。"""
    print(f"  [更新包] {path.name}  ({path.stat().st_size / 1024 / 1024:.1f} MB)")
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        doc = _read_json(zf, "update.json")
        declared = _manifest_map(doc)
        remove = [str(x) for x in (doc.get("remove") or [])]

        if doc.get("app_id") and doc.get("app_id") != "mindustry-launcher":
            problems.append(f"更新包 app_id 不是本程序: {doc.get('app_id')!r}")

        # ---- payload 集合必须与声明的 files 完全一致（不多不少）----
        payload = {n[len("files/"):] for n in names if n.startswith("files/")}
        if payload != set(declared):
            extra = sorted(payload - set(declared))[:5]
            lack = sorted(set(declared) - payload)[:5]
            problems.append(f"更新包 payload 与声明不符：多带 {extra}，缺少 {lack}")

        # key = 更新包里那条 payload 的真实路径
        arc_of = {n[len("files/"):]: n for n in names if n.startswith("files/")}

        # ---- 证明链 3：payload 的真实哈希 == 声明 ----
        bad = 0
        for rp, want in declared.items():
            arc = arc_of.get(rp)
            if arc is None:
                continue                      # 上面已经报过「缺少」
            if _sha256_of(zf, arc) != want:
                bad += 1
                if bad <= 3:
                    problems.append(f"更新包 payload 哈希与声明不符 -> {rp}")
        if bad:
            problems.append(f"更新包：共 {bad} 个 payload 哈希不符")

        # ---- 白名单：每个动作都要能过真实现 ----
        exe_name = _exe_name(declared)
        rejected = [p for p in list(declared) + remove
                    if not _is_replaceable(p.replace("\\", "/"), exe_name)]
        if rejected:
            problems.append(f"★ 更新包里有不该覆盖的路径（会碰用户数据/ jre）：{rejected[:5]}")

        print(f"     声明 {len(declared)} 个文件、删 {len(remove)} 个；"
              f"payload 实收 {len(payload)} 个；哈希不符 {bad}；白名单拒绝 {len(rejected)} 项")
        return {"version": str(doc.get("version", "")),
                "base_version": str(doc.get("base_version", "")),
                "files": declared, "remove": remove}


def _pairs(releases: Path) -> list[tuple[str, str]]:
    """扫描留档目录，列出所有「更新包 + 对应老完整包都齐」的 (base, to) 组合。"""
    out = []
    for z in sorted(releases.glob("mindustry-launcher-v*-update.zip")):
        try:
            with zipfile.ZipFile(z) as zf:
                doc = _read_json(zf, "update.json")
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
            continue
        base, to = str(doc.get("base_version", "")), str(doc.get("version", ""))
        if base and to and (releases / FULL_PAT.format(ver=base)).is_file() \
                and (releases / FULL_PAT.format(ver=to)).is_file():
            out.append((base, to))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="真发布资产的升级仿真")
    ap.add_argument("--from", dest="src", default=None, help="升级前版本号（默认自动挑）")
    ap.add_argument("--to", dest="dst", default=None, help="升级后版本号（默认自动挑）")
    ap.add_argument("--releases", default=None, help="发布资产目录（默认 _history/releases）")
    args = ap.parse_args()

    releases = Path(args.releases).expanduser() if args.releases else RELEASES_DIR
    if not releases.is_dir():
        print(f"找不到发布资产目录: {releases}")
        return 2

    available = _pairs(releases)
    if args.src and args.dst:
        src, dst = args.src, args.dst
    elif available:
        src, dst = max(available, key=lambda p: _ver_key(p[1]))
    else:
        print(f"没有可用的「老完整包 + 更新包」组合（目录 {releases}）。")
        print("  需要 MindustryLauncher-v<老版本>-win64.zip 与 "
              "mindustry-launcher-v<新版本>-update.zip 同时在。")
        return 2

    old_zip = releases / FULL_PAT.format(ver=src)
    new_zip = releases / FULL_PAT.format(ver=dst)
    upd_zip = releases / UPDATE_PAT.format(ver=dst)
    for p in (old_zip, new_zip, upd_zip):
        if not p.is_file():
            print(f"缺文件: {p}")
            if available:
                print("  当前可用的组合:", "、".join(f"{a}→{b}" for a, b in available))
            return 2

    print(f"升级仿真：{src} → {dst}")
    print(f"  老完整包 {old_zip.name}")
    print(f"  真更新包 {upd_zip.name}")
    print(f"  新完整包 {new_zip.name}")
    print()

    problems: list[str] = []
    old = _check_package_truthful(f"旧 {src}", old_zip, problems)
    upd = _check_update_package(upd_zip, problems)
    new = _check_package_truthful(f"新 {dst}", new_zip, problems)

    # ---- 版本号自洽 ----
    if old.get("version") and old["version"] != src:
        problems.append(f"老完整包里的版本号 {old['version']!r} != 文件名里的 {src!r}")
    if new.get("version") and new["version"] != dst:
        problems.append(f"新完整包里的版本号 {new['version']!r} != 文件名里的 {dst!r}")
    if upd.get("version") and upd["version"] != dst:
        problems.append(f"更新包声明升到 {upd['version']!r}，但文件名是 {dst!r}")
    if upd.get("base_version") and upd["base_version"] != src:
        problems.append(f"更新包基线是 {upd['base_version']!r}，但我们拿 {src!r} 当基线")

    # ---- 证明链 4：把更新套在老清单上 == 新清单 ----
    print()
    print("  证明链第 4 步：老清单 + 更新 == 新清单")
    if old.get("files") and new.get("files"):
        applied = dict(old["files"])
        for rp, sha in upd.get("files", {}).items():
            applied[rp] = sha
        for rp in upd.get("remove", []):
            applied.pop(rp.replace("\\", "/"), None)

        want = new["files"]
        missing = sorted(set(want) - set(applied))
        extra = sorted(set(applied) - set(want))
        diff = sorted(p for p in (set(want) & set(applied)) if want[p] != applied[p])

        if missing:
            problems.append(f"★ 升级后仍缺少 {len(missing)} 个文件（更新包漏换）: {missing[:5]}")
        if extra:
            problems.append(f"★ 升级后多出 {len(extra)} 个文件（更新包没删干净）: {extra[:5]}")
        if diff:
            problems.append(f"★ 升级后有 {len(diff)} 个文件内容不对: {diff[:5]}")

        print(f"    老清单 {len(old['files'])} 条 → 套上更新后 {len(applied)} 条；"
              f"新清单 {len(want)} 条")
        print(f"    缺 {len(missing)} / 多 {len(extra)} / 内容不符 {len(diff)}")
    else:
        problems.append("老三方清单有一方没读到，无法做第 4 步对比")

    print()
    if problems:
        print(f"不通过：{len(problems)} 个问题")
        for p in problems:
            print("  -", p)
        return 1
    print(f"通过：{src} + 真更新包 == {dst} 完整包（清单、哈希、白名单、无夹带全部对上）")
    print("      ★ 注意：这证明的是「资产之间自洽」。要证明「真 exe 能换掉自己」，"
          "另跑 selfupdate_e2e.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
