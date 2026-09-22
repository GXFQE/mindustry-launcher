# -*- coding: utf-8 -*-
"""给验证沙箱预置「最新版本」，免得每次测试都去下载 100+ MB 的游戏包。

## 为什么需要

启动器默认 ``auto_update=True``，启动时会在后台悄悄检查更新，发现没有的版本
就**直接下载**。而验证沙箱的 ``versions/`` 是空的 —— 于是每跑一次
``release_check`` / ``exe_edge_check`` 都会真去 GitHub 拉一个
Mindustry 160.4（约 109 MB）+ MindustryX 最新版（约 115 MB），
既是几百 MB 流量，又让"几十秒的冒烟"变成"好几分钟"，日志还被下载进度刷满。

``gui_smoke.py`` 早就在沙箱 config 里写了 ``auto_update: False`` 来躲开这件事，
但另外两个脚本用的是**启动器自己重建的默认配置**（默认就是开），躲不掉。

## 做法

判重的依据是「本地已有哪些 (type, version)」（见
``UpdateManager.get_available_updates``），而这一步**只读清单**
（``VersionManager.get_versions`` 遍历 ``versions/manifests/*.json``），
不碰 CAS 对象。所以只要把真实数据目录里**每个类型最新的那份清单**拷进沙箱，
更新检查就会给出「已是最新」，下载自然不发生。

⚠️ 因此本工具**默认不拷对象**（``with_objects=False``）。理由：
一个版本的 CAS 对象是 ~110 MB，而验证沙箱里没有任何场景会真的启动游戏，
拷过去纯属浪费（``exe_edge_check`` 还会跑 GC，凭空多扫 5000+ 个文件）。
真要启动某个版本（比如以后写 e2e），再传 ``with_objects=True`` ——
那时优先用**硬链接**（同一 NTFS 卷上瞬时完成、不额外占空间），
CAS 对象不可变，沙箱里删掉也只是删掉一个链接，动不到真实对象池。

## 用法

    from sandbox_seed import seed_latest_versions
    seed_latest_versions(sandbox_root)                  # 只放清单
    seed_latest_versions(sandbox_root, with_objects=True)

拿不到真实数据目录（比如换台机器）时**只警告不抛异常** ——
验证脚本该跑还得跑，大不了回到"下载一次"的老行为。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# _tools/ 是 _paths.py 所在的一层
_TOOLS = Path(__file__).resolve().parent.parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))


@dataclass
class SeedResult:
    """预置结果，给调用方打印用。"""

    versions: list[str] = field(default_factory=list)
    manifests: int = 0
    objects_linked: int = 0
    objects_copied: int = 0
    objects_missing: int = 0
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.manifests > 0

    def describe(self) -> str:
        if not self.ok:
            return f"未预置（{self.skipped_reason or '未知原因'}）"
        parts = [f"{self.manifests} 份清单：{'、'.join(self.versions)}"]
        if self.objects_linked or self.objects_copied:
            parts.append(
                f"对象 硬链接 {self.objects_linked} / 拷贝 {self.objects_copied}"
            )
        if self.objects_missing:
            parts.append(f"⚠️ 缺 {self.objects_missing} 个对象")
        return "；".join(parts)


def _latest_manifests(source_manifests: Path) -> list[Path]:
    """每个类型各挑一份最新的清单。

    排序规则**直接复用启动器自己的** ``VersionManager.get_versions``
    （它按 ``(type_rank, 版本号倒序)`` 排），不在这里另写一套 ——
    两套排序迟早会不一致，到时候预置的「最新」和启动器认的「最新」对不上。
    """
    import _paths  # noqa: PLC0415  （延迟导入：让找不到数据目录时能优雅退出）

    # launcher 包在代码根（_paths.ROOT）下，不在 _tools/ 里
    if str(_paths.ROOT) not in sys.path:
        sys.path.insert(0, str(_paths.ROOT))

    from launcher.storage import CASStore, VersionManager  # noqa: PLC0415

    data_versions = _paths.DATA / "versions"
    manager = VersionManager(CASStore(data_versions), source_manifests)
    versions = manager.get_versions()

    picked: list[Path] = []
    seen: set[str] = set()
    for v in versions:                      # 已按「新 → 旧」排好
        if v["type"] in seen:
            continue
        seen.add(v["type"])
        picked.append(Path(v["manifest_path"]))
    return picked


def _link_or_copy(src: Path, dst: Path) -> str:
    """优先硬链接（同卷瞬时、不占空间），跨卷时退回真拷贝。返回 'link'/'copy'。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return "link"
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def seed_latest_versions(
    dest_root: Path,
    *,
    with_objects: bool = False,
    quiet: bool = False,
) -> SeedResult:
    """把每个类型最新的版本清单放进 ``dest_root/versions/manifests/``。

    ``dest_root`` 是沙箱根（里面应当已经有 ``versions/manifests/``；
    没有就建）。
    """
    result = SeedResult()
    dest_root = Path(dest_root)
    dest_manifests = dest_root / "versions" / "manifests"
    dest_objects = dest_root / "versions" / "objects"
    try:
        dest_manifests.mkdir(parents=True, exist_ok=True)
        dest_objects.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        result.skipped_reason = f"建目录失败: {e}"
        if not quiet:
            print(f"  [预置] {result.describe()}")
        return result

    try:
        import _paths  # noqa: PLC0415

        data_manifests = _paths.DATA / "versions" / "manifests"
        data_objects = _paths.DATA / "versions" / "objects"
        if not data_manifests.is_dir():
            raise RuntimeError(f"真实清单目录不存在：{data_manifests}")
        picked = _latest_manifests(data_manifests)
        if not picked:
            raise RuntimeError("真实数据目录里一份有效清单都没有")
    except Exception as e:  # noqa: BLE001  （找不到数据目录不该让验证脚本挂掉）
        result.skipped_reason = str(e)
        if not quiet:
            print(f"  [预置] 跳过 —— {result.describe()}")
            print("          （没预置就会走一次真实下载，不影响断言）")
        return result

    for manifest in picked:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            result.skipped_reason = f"读清单失败 {manifest.name}: {e}"
            continue
        try:
            shutil.copy2(manifest, dest_manifests / manifest.name)
        except OSError as e:
            result.skipped_reason = f"拷清单失败 {manifest.name}: {e}"
            continue
        result.manifests += 1
        result.versions.append(f"{data['type']} {data['version']}")

        if not with_objects:
            continue
        for _relpath, sha in (data.get("files") or {}).items():
            src = data_objects / str(sha)[:2] / str(sha)[2:]
            if not src.is_file():
                result.objects_missing += 1
                continue
            how = _link_or_copy(src, dest_objects / str(sha)[:2] / str(sha)[2:])
            if how == "link":
                result.objects_linked += 1
            else:
                result.objects_copied += 1

    if not quiet:
        print(f"  [预置] {result.describe()}")
    return result


if __name__ == "__main__":
    import tempfile

    sandbox = Path(tempfile.mkdtemp(prefix="mdt_seed_probe_"))
    print(f"沙箱：{sandbox}")
    seed_latest_versions(sandbox, with_objects="--objects" in sys.argv)
