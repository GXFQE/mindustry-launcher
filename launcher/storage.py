# -*- coding: utf-8 -*-
"""版本与备份存储：CAS 对象池、版本清单、备份清单与垃圾回收。"""
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ConfigManager, delete_path, sanitize_version_name
from .sources import SourceRegistry
from .utils import atomic_write_json, zip_directory
from .version import MANIFEST_VERSION

logger = logging.getLogger(__name__)

# 组装 runtime jar 时读 CAS 对象的并行度。
# 一个版本摊开是 115 MB 量级、几千个几十 KB 的小对象，顺序读的瓶颈在
# 每次 open/read 的往返上；机器内存不宽裕时这批对象还会被挤出系统缓存，
# 退化成随机落盘读，单次等待长得多 —— 这时并行度越高越能把等待重叠掉。
# 实测缓存吃紧时 8 线程约 2.4 s、32 线程约 1.4 s；缓存热时各档都在
# 1.7~1.8 s，几乎没有差别。所以取偏大的 24。
_JAR_READ_WORKERS = 24
# 每批读多少个文件就写一次 zip。单纯为压内存峰值：
# 一批最多这么多文件同时驻留内存，而不是整个 jar（115MB+）。
_JAR_BATCH = 512

# ---------------------------------------------------------------------------
# ★ 批量写入 vs 垃圾回收
#
# 「导入版本」和「创建备份」都是**先写对象、最后才写清单**的。在这中间
# 那段时间里，刚写进去的对象在 GC 眼里全是「无人引用」—— 清理逻辑会
# 把它们整批删掉，而清单随后照样写成功。症状极难查：版本列表正常、
# 备份看起来也在，一点启动/恢复才报「对象丢失: <sha>」。
#
# 所以加两道保险：
#   1) _BULK_WRITE_LOCK —— 写入区间持锁，GC 拿到锁才开始扫（等它写完，不删一半）
#   2) _GC_GRACE_SECONDS —— 太新的对象一律不删。锁只管本进程；两个实例
#      同时开着、或上次被强杀留下的半截状态，只能靠对象年龄兜住。
# ---------------------------------------------------------------------------
_BULK_WRITE_LOCK = threading.RLock()
# 必须远大于「一次导入/备份要花的时间」（本项目导入 115MB jar 实测约 4 秒）
_GC_GRACE_SECONDS = 600.0

class CASStore:
    def __init__(self, base_path: Path) -> None:
        self.base_path = Path(base_path)
        self.objects_dir = self.base_path / "objects"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"CAS 存储初始化: {self.objects_dir}")

    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def store_file(self, data: bytes) -> str:
        sha = self.sha256(data)
        obj_path = self._object_path(sha)
        obj_path.parent.mkdir(parents=True, exist_ok=True)
        if not obj_path.exists():
            fd, tmp = tempfile.mkstemp(dir=obj_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, obj_path)
                logger.debug(f"存储对象 {sha[:8]}... 大小 {len(data)} 字节")
            except Exception as e:
                os.unlink(tmp)
                logger.error(f"存储对象失败 {sha}: {e}")
                raise
        return sha

    def store_directory(self, dir_path: Path) -> dict[str, str]:
        # 整目录入库属于「先落对象再落引用」的批量写，GC 期间不许跑
        mapping = {}
        with _BULK_WRITE_LOCK:
            for root, dirs, files in dir_path.walk():
                for file in files:
                    f = root / file
                    rel = f.relative_to(dir_path).as_posix()
                    data = f.read_bytes()
                    sha = self.store_file(data)
                    mapping[rel] = sha
        logger.debug(f"存储目录 {dir_path}，共 {len(mapping)} 个文件")
        return mapping

    def _object_path(self, sha: str) -> Path:
        return self.objects_dir / sha[:2] / sha[2:]

    def get_file(self, sha: str) -> bytes:
        # 不做 exists() 预检——那是每个对象多一次 stat。读一个 5000+ 文件的
        # jar 时要多花约 1 秒（实测顺序读 2973ms → 1871ms）。
        # 直接读，让 open() 自己报 FileNotFoundError 就行。
        try:
            return self._object_path(sha).read_bytes()
        except FileNotFoundError:
            logger.error(f"对象丢失: {sha}")
            raise FileNotFoundError(f"对象丢失: {sha}") from None

    def restore_directory(
        self, mapping: dict[str, str], dest_dir: Path
    ) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, sha in mapping.items():
            data = self.get_file(sha)
            target = dest_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, target)
            except Exception as e:
                os.unlink(tmp)
                logger.error(f"恢复文件失败 {target}: {e}")
                raise
        logger.info(f"恢复目录完成: {dest_dir}，共 {len(mapping)} 个文件")

    def garbage_collect(
        self,
        referenced_shas: set[str],
        stop_event: threading.Event | None = None,
        grace_seconds: float = _GC_GRACE_SECONDS,
    ) -> int:
        """删除未被引用的 CAS 对象。对象池是两层结构：
        objects/<sha前2位>/<sha后62位>，所以只需要两层 scandir，不用递归。

        用 os.scandir 而不是 Path.iterdir：DirEntry 缓存了类型信息，
        判 is_file 不需要额外 stat。对象多时（本项目 3 万+）差距很明显。

        stop_event 用于在程序退出时提前中断，避免关窗口卡住。

        ★ 两道防误删：整段删除循环持 `_BULK_WRITE_LOCK`（正在导入/备份就等它
        写完），并且**不删比 grace_seconds 更新的对象**（见模块顶部说明）。
        少了这两条，导入进度条走到一半被后台 GC 撞上就会留下一份坏版本。
        """
        removed = 0
        skipped = 0
        empty_dirs: list[str] = []
        interrupted = False
        cutoff = time.time() - grace_seconds
        with _BULK_WRITE_LOCK:
            with os.scandir(self.objects_dir) as prefixes:
                for prefix in prefixes:
                    if stop_event is not None and stop_event.is_set():
                        interrupted = True
                        break
                    if not prefix.is_dir(follow_symlinks=False):
                        continue
                    if len(prefix.name) != 2:
                        continue
                    has_leftover = False
                    with os.scandir(prefix.path) as objects:
                        for obj in objects:
                            if not obj.is_file(follow_symlinks=False):
                                has_leftover = True
                                continue
                            if (prefix.name + obj.name) in referenced_shas:
                                has_leftover = True
                                continue
                            # 太新的对象先留着：它多半是某次还没写完的导入/备份
                            try:
                                if obj.stat().st_mtime > cutoff:
                                    skipped += 1
                                    has_leftover = True
                                    continue
                            except OSError:
                                has_leftover = True
                                continue
                            try:
                                os.unlink(obj.path)
                                removed += 1
                                logger.debug(f"垃圾回收删除对象: {obj.name}")
                            except OSError:
                                # 删不掉说明它还在，也算这个目录非空
                                has_leftover = True
                    if not has_leftover:
                        empty_dirs.append(prefix.path)
        # 空的 sha 前缀目录收拾掉。放在循环外做，避免边遍历边改目录。
        for path in empty_dirs:
            try:
                os.rmdir(path)
            except OSError:
                pass
        if skipped:
            logger.info(
                f"垃圾回收跳过 {skipped} 个新对象（{grace_seconds:.0f}s 内）"
            )
        if interrupted:
            logger.info(f"垃圾回收被中断（程序退出中），已删除 {removed} 个对象")
        else:
            logger.info(f"垃圾回收完成，删除 {removed} 个对象")
        return removed

class VersionManager:
    def __init__(
        self,
        store: CASStore,
        manifests_dir: Path,
        sources: Any | None = None,
    ) -> None:
        """``sources`` 是 `sources.SourceRegistry`（不给就用内置那两个来源）。

        ★ 有了这张表，「版本类型怎么排序、版本号怎么解析」就不再写死在
        ``get_versions`` 里 —— 加一个新来源（官方每日构建、别的分支）
        不用动这个类的代码。见 sources.py 顶部的说明。
        """
        self.store = store
        self.manifests_dir = manifests_dir
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        # 不给就用内置那两个来源（sources 只依赖 utils/标准库，不绕回本模块）。
        self.sources = sources if sources is not None else SourceRegistry()

    def manifest_path(self, vtype: str, version: str) -> Path:
        """清单文件路径 —— **所有**按 (类型, 版本号) 定位清单的地方都走这里。

        版本号是用户填的（导入对话框）或 GitHub tag 来的，直接拼进文件名
        会让 ``..\\..\\..\\evil`` 这类输入写到 manifests 之外；删除版本
        用的是同一个拼接方式，那就成了"按输入删任意文件"。
        """
        return self.manifests_dir / (
            f"{sanitize_version_name(vtype)}_"
            f"{sanitize_version_name(version)}.json"
        )

    def get_versions(self) -> list[dict[str, Any]]:
        versions = []
        for mf in self.manifests_dir.glob("*.json"):
            try:
                # 清单是 atomic_write_json 写的（utf-8），读也得显式指定，
                # 否则在非 UTF-8 默认编码的环境里会乱码甚至 UnicodeDecodeError
                data = json.loads(mf.read_text(encoding="utf-8"))
                vtype = str(data["type"])
                raw_ver = str(data["version"])
                # 类型 -> 排序档位 + 版本号解析规则，都查来源注册表。
                # 没登记的类型走通用规则（取所有数字、档位 1），
                # 跟加这张表之前的行为完全一致。
                versions.append(
                    {
                        "name": f"{vtype} {raw_ver}",
                        "type": vtype,
                        "raw_version": raw_ver,
                        "parsed_version": self.sources.parse_version(
                            vtype, raw_ver
                        ),
                        "type_rank": self.sources.rank_of(vtype),
                        "manifest_path": mf,
                    }
                )
            except (
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
                OSError,
                UnicodeDecodeError,
            ) as e:
                # 单个坏清单不能把整个启动器带崩（get_versions 在启动路径上）
                logger.warning(f"跳过无效清单 {mf}: {e}")
                continue
        versions.sort(
            key=lambda x: (
                x["type_rank"],
                tuple(-p for p in x["parsed_version"]),
            )
        )
        logger.debug(f"获取到 {len(versions)} 个版本")
        return versions

    def add_version_from_jar(
        self, jar_path: Path, version: str, vtype: str
    ) -> None:
        # 先定路径 = 先把版本号校验掉，别等几千个对象都写完了才发现名字非法
        manifest_path = self.manifest_path(vtype, version)
        manifest = {
            "type": vtype,
            "version": version,
            # 清单结构的版本号：**给将来用**的（要改清单结构时好判断该按
            # 哪一代读）。读取方一律忽略它 —— 清单是用户的数据资产，
            # 新代码必须读得懂老清单，老代码也得读得懂新清单。
            "manifest_version": MANIFEST_VERSION,
            "files": {},
        }
        try:
            # ★ 整个「写对象 → 写清单」区间持批量写锁：中间任何一个时刻
            #   这些对象都还没有引用者，后台 GC 撞进来会把它们当孤儿删掉。
            with _BULK_WRITE_LOCK:
                with zipfile.ZipFile(jar_path, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        data = zf.read(info)
                        sha = self.store.store_file(data)
                        manifest["files"][info.filename] = sha
                atomic_write_json(manifest_path, manifest)
        except zipfile.BadZipFile as e:
            logger.error(f"无效的 JAR 文件: {jar_path} - {e}")
            raise ValueError(f"无效的 JAR 文件: {e}")
        logger.info(f"添加版本 {vtype} {version}，清单: {manifest_path}")

    def build_runtime_jar(
        self, vtype: str, version: str, output: Path
    ) -> Path:
        manifest_path = self.manifest_path(vtype, version)
        if not manifest_path.exists():
            logger.error(f"清单不存在: {manifest_path}")
            raise FileNotFoundError(f"清单不存在: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = list(manifest["files"].items())
        # 顺序读几千个小对象时，瓶颈是每次 open/read 的往返而不是带宽，
        # 多线程并行能吃掉一部分（实测组装 3.1s → 1.8s）。
        # 顺序：先把数据并行读出来，再往同一个 zip 里顺序写 ——
        # 写 zip 是共享状态，并行写反而要加锁，得不偿失。
        # 分批是为了压内存峰值：一批最多 _JAR_BATCH 个文件在内存里，
        # 而不是一次性把整个 115MB 的 jar 全读进来。
        # 把「读对象」和「写 zip」分开计时。这两段的瓶颈完全不同：
        # 读受限于每个小文件的 open/read 往返（也容易被杀毒软件拦），
        # 写受限于 zip 的 CRC 计算。混在一起计时就没法判断该优化哪边，
        # 所以日志里分开报。
        read_s = 0.0
        write_s = 0.0
        try:
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as zf:
                if len(items) <= _JAR_BATCH:
                    for rel, sha in items:
                        t0 = time.perf_counter()
                        data = self.store.get_file(sha)
                        t1 = time.perf_counter()
                        zf.writestr(rel, data)
                        t2 = time.perf_counter()
                        read_s += t1 - t0
                        write_s += t2 - t1
                else:
                    with ThreadPoolExecutor(
                        max_workers=_JAR_READ_WORKERS
                    ) as pool:
                        for start in range(0, len(items), _JAR_BATCH):
                            batch = items[start : start + _JAR_BATCH]
                            shas = [sha for _, sha in batch]
                            t0 = time.perf_counter()
                            datas = list(pool.map(self.store.get_file, shas))
                            t1 = time.perf_counter()
                            for (rel, _), data in zip(batch, datas):
                                zf.writestr(rel, data)
                            t2 = time.perf_counter()
                            read_s += t1 - t0
                            write_s += t2 - t1
        except Exception:
            # 中途失败会在磁盘上留半个 jar。调用方可能只看文件存在就当它可用，
            # 拿它去启动游戏会得到莫名其妙的报错，所以这里清掉再往上抛。
            output.unlink(missing_ok=True)
            raise
        logger.info(
            f"[JAR] {vtype} {version}: {len(items)} 文件 "
            f"{output.stat().st_size / 1048576:.1f} MB，"
            f"读 {read_s * 1000:.0f}ms + 写 {write_s * 1000:.0f}ms "
            f"= {(read_s + write_s) * 1000:.0f}ms"
        )
        return output

    def delete_version(self, vtype: str, version: str) -> None:
        manifest_path = self.manifest_path(vtype, version)
        try:
            manifest_path.unlink(missing_ok=True)
            logger.info(f"删除版本 {vtype} {version}")
        except OSError as e:
            logger.warning(f"删除清单失败 {manifest_path}: {e}")

    def manifest_digest(self, vtype: str, version: str) -> str:
        """清单内容的哈希，用来判断「预热好的 jar 还有效吗」。

        比 mtime + 大小可靠：清单被重写但大小不变虽然罕见，可一旦漏判，
        就会拿旧 jar 去启动新版本，症状是很难查的类找不到。
        成本只是读一个几百 KB 的文件（约 2 ms）。

        版本号非法/清单不存在都返回空串（不抛）：调用方在 GUI 线程上
        （_preheat_key_of），抛出去只会变成 Tk 回调里的一个噪音异常。
        """
        try:
            return hashlib.sha256(
                self.manifest_path(vtype, version).read_bytes()
            ).hexdigest()
        except (OSError, ValueError):
            return ""

    def garbage_collect(
        self, stop_event: threading.Event | None = None
    ) -> int:
        referenced = set()
        for mf in self.manifests_dir.glob("*.json"):
            try:
                manifest = json.loads(mf.read_text(encoding="utf-8"))
                for sha in manifest.get("files", {}).values():
                    referenced.add(sha)
            except (
                json.JSONDecodeError,
                KeyError,
                OSError,
                UnicodeDecodeError,
            ):
                # 读不出来的清单先留着：宁可少删也不能误删
                logger.warning(f"清单无法解析，跳过其引用统计: {mf}")
        return self.store.garbage_collect(referenced, stop_event)

def collect_referenced_objects(backup_base: Path) -> set[str]:
    """收集所有存档分类（含回收站）引用到的 CAS 对象，避免误删。"""
    referenced: set[str] = set()
    base = Path(backup_base)
    if not base.is_dir():
        return referenced
    for mf in base.rglob("backup_*.json"):
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
            for sha in manifest.get("files", {}).values():
                referenced.add(sha)
        except (json.JSONDecodeError, KeyError, OSError, UnicodeDecodeError):
            continue
    return referenced

def _extract_zip_into(zip_path: Path, dest_dir: Path) -> None:
    """把 zip 解到目录里，并挡住条目名越界（zip slip）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_root = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            target = (dest_dir / name).resolve()
            if target != dest_root and not target.is_relative_to(dest_root):
                raise ValueError(f"快照条目路径越界，已拒绝解压: {name}")
        zf.extractall(dest_dir)


class BackupManager:
    """每个存档分类一套独立的备份清单目录，CAS 对象池全局共享。"""

    def __init__(
        self,
        store: CASStore,
        config: ConfigManager,
        backup_base: Path,
        profile_name: str,
    ) -> None:
        self.store = store
        self.config = config
        self.backup_base = Path(backup_base)
        self.profile_name = profile_name
        self.profile_dir = self.backup_base / profile_name
        self.manifests_dir = self.profile_dir / "manifests"
        self.manifests_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self, description: str = "") -> Path:
        save_dir = Path(self.config.get_save_path(self.profile_name))
        if not save_dir.exists():
            logger.error(f"存档目录不存在: {save_dir}")
            raise FileNotFoundError(f"存档数据目录不存在: {save_dir}")
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        files = {}
        # ★ 与导入同理：对象先落盘、清单最后才写，中间不能有 GC 来扫
        with _BULK_WRITE_LOCK:
            for root, dirs, files_names in save_dir.walk():
                for file in files_names:
                    f = root / file
                    if (
                        f.is_file()
                        and not f.name.startswith(".")
                        and not f.name.endswith(".lock")
                    ):
                        rel = f.relative_to(save_dir).as_posix()
                        files[rel] = self.store.store_file(f.read_bytes())
            manifest = {
                "description": description,
                "timestamp": timestamp,
                "files": files,
            }
            manifest_path = self.manifests_dir / f"backup_{timestamp}.json"
            # 同一秒内可能连续备份（手动 + 自动），加序号避免互相覆盖
            counter = 1
            while manifest_path.exists():
                manifest_path = (
                    self.manifests_dir / f"backup_{timestamp}_{counter}.json"
                )
                counter += 1
            atomic_write_json(manifest_path, manifest)
        logger.info(
            f"创建备份[分类:{self.profile_name}]: {manifest_path}，"
            f"描述: {description or '无'}"
        )
        self._cleanup_old_backups()
        return manifest_path

    def restore_backup(self, manifest_path: Path) -> None:
        """把某个备份恢复进该分类的数据目录。

        ★ 两条硬规矩（都是踩出来的）：
          1. 先把当前存档打包成回滚快照，**快照建不成功就直接中止**，
             一个文件都不删 —— 没有退路就不许动用户的存档；
          2. 清空旧内容默认走回收站（``permanent_delete`` 打开时才是直接删），
             不用 shutil.rmtree —— 这样即使恢复失败，用户还有两处能找回数据
             （快照 zip + 回收站）。
        """
        save_dir = Path(self.config.get_save_path(self.profile_name))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest.get("files"), dict):
            raise ValueError(f"备份清单损坏（缺少 files 字段）: {manifest_path}")

        snapshot = self._make_rollback_snapshot(save_dir)
        keep_snapshot = False
        try:
            if snapshot is not None:
                self._clear_save_dir(save_dir)
            self.store.restore_directory(manifest["files"], save_dir)
            logger.info(f"恢复备份成功: {manifest_path}")
        except Exception as e:
            logger.error(f"恢复备份失败: {e}")
            if snapshot is None:
                raise
            logger.info("尝试回滚到之前状态...")
            try:
                self._clear_save_dir(save_dir)
                _extract_zip_into(snapshot, save_dir)
                logger.info("回滚成功")
            except Exception as rollback_e:
                keep_snapshot = True
                logger.critical(
                    f"回滚也失败，恢复前的快照保留在 {snapshot}: {rollback_e}"
                )
                raise RuntimeError(
                    "恢复备份失败，且回滚也失败。\n\n"
                    f"恢复前的完整快照已保留在：\n{snapshot}\n\n"
                    "可以手动把它解压回数据目录。"
                ) from e
            raise
        finally:
            if snapshot is not None and not keep_snapshot:
                snapshot.unlink(missing_ok=True)

    def _make_rollback_snapshot(self, save_dir: Path) -> Path | None:
        """把当前存档打包成回滚快照；目录空或不存在时返回 None。

        失败一律抛异常让调用方中止恢复：宁可这次恢复没做成，
        也不能在没有退路的情况下先把用户的存档清掉。
        """
        if not save_dir.exists() or not any(save_dir.iterdir()):
            return None
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        snapshot = self.profile_dir / f"rollback_{timestamp}.zip"
        try:
            zip_directory(save_dir, snapshot)
            # 磁盘写满/被中断会留下一个半截 zip —— 那种"快照"等于没有
            if not zipfile.is_zipfile(snapshot):
                raise OSError("生成的快照不是有效的 zip（可能磁盘空间不足）")
        except Exception as e:
            snapshot.unlink(missing_ok=True)
            logger.error(f"创建回滚快照失败，已取消恢复: {e}")
            raise RuntimeError(
                "无法为当前存档创建安全快照，已取消恢复"
                f"（存档目录未被改动）。\n\n{e}"
            ) from e
        logger.debug(f"回滚快照已就绪: {snapshot}")
        return snapshot

    def _clear_save_dir(self, save_dir: Path) -> None:
        """清空存档目录里的内容（目录本身留着）。

        ★ 默认走回收站。原来是 shutil.rmtree：一旦随后的恢复失败，用户的存档
        就彻底没了，连回收站都翻不到 —— 而且违反了「删任何东西走回收站」。
        设置里把 ``permanent_delete`` 打开（默认关）才直接删，那种情况下
        上面 ``restore_backup`` 的回滚快照就是唯一的退路。
        """
        if not save_dir.exists():
            return
        permanent = bool(self.config.get("permanent_delete"))
        for item in save_dir.iterdir():
            if item.is_symlink():
                # 只删链接本身。delete_path/move_to_recycle_bin 内部也不能解引用，
                # 否则"删这个链接"会变成"删链接指向的目录"。
                item.unlink()
                continue
            delete_path(item, permanent=permanent)

    def list_backups(self) -> list[Path]:
        paths = list(self.manifests_dir.glob("backup_*.json"))

        def _extract_timestamp(p: Path) -> str:
            name = p.stem
            return name[len("backup_") :]

        paths.sort(key=_extract_timestamp, reverse=True)
        return paths

    def get_backup_display(self, manifest_path: Path) -> str:
        """备份列表里显示的那一行。

        ★ 容错范围要够宽：这个方法在**界面列表里逐个调用**，清单被占用
        （OSError）、内容被写坏（JSONDecodeError）、description 是个数字
        （AttributeError/TypeError）都不该让整个「管理备份」窗口打不开 ——
        最差也要退回文件名。
        """
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            desc = manifest.get("description", "")
            if isinstance(desc, str) and desc.strip():
                return desc.strip()
            ts = manifest.get("timestamp", "")
            if isinstance(ts, str) and ts:
                dt = datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")
                return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, json.JSONDecodeError, ValueError, KeyError,
                TypeError, AttributeError) as e:
            logger.warning(f"读取备份描述失败 {manifest_path}: {e}")
        return manifest_path.stem

    def rename_backup(self, manifest_path: Path, new_description: str) -> None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["description"] = new_description
            atomic_write_json(manifest_path, manifest)
            logger.debug(f"备份备注更新: {manifest_path} -> {new_description}")
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"更新备份备注失败: {e}")

    def _cleanup_old_backups(self) -> None:
        backups = self.list_backups()
        max_keep = self.config.get_profile_setting(
            self.profile_name, "max_backups"
        )
        while len(backups) > max_keep:
            oldest = backups.pop()
            try:
                oldest.unlink(missing_ok=True)
                logger.debug(f"删除旧备份: {oldest}")
            except OSError as e:
                logger.warning(f"删除旧备份失败 {oldest}: {e}")
        # 注意：CAS 对象池是全局共享的，回收时必须统计所有分类的引用，
        # 否则会把其它分类的备份对象一起删掉。
        referenced = collect_referenced_objects(self.backup_base)
        self.store.garbage_collect(referenced)
