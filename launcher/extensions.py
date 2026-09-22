# -*- coding: utf-8 -*-
"""扩展点（可选的本地插件目录）—— 给「以后想加的功能」留稳定接口。

## 这是什么

启动器启动后会在**数据根**（exe 所在目录）下找 ``extensions/`` 目录，
把里面每个 ``*.py`` 当扩展载入。扩展可以挂在几个固定钩子上，从而在
**不改启动器本体、不重新打包**的前提下加点自己的东西（比如把每局时长
写进自己的统计文件、启动前自动多带一个 JVM 参数）。

## 为什么放在这里（而不是做成完整的插件框架）

需求只有一句：**发布以后加新功能时，别为了兼容老版本把核心代码改乱**。
所以这里刻意做得很小：

* 钩子名固定的一小张表（``HOOKS``）；
* 只支持「本地目录里的 .py」，不做包管理、不做依赖解析、不做沙箱；
* 与主程序之间只传 kwargs，**不承诺**任何内部对象的结构 —— 想要稳定的
  数据就自己读 config.json / 日志，别去摸 ``self.xxx``；
* 任何扩展出错都只记日志，**绝不影响启动器**（载入失败、回调抛异常都不行）。

⚠️ 扩展里的代码跟启动器**同权限**运行。只放自己写的或信得过的文件。

## 对扩展作者的最低要求

```python
# extensions/我的统计.py
def register(api):
    api.on("on_game_exited", lambda **kw: print(kw["version_name"]))
```
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EXT_DIR_NAME = "extensions"
# 设成 1 就完全不载入扩展（排查「是不是扩展搞的鬼」时最省事）
DISABLE_ENV = "MDT_NO_EXTENSIONS"

# ★ 钩子表就是**兼容性契约**：名字一旦发布就不再改（只增不改）。
#   每个钩子能拿到什么，写在下面每一条的注释里。
HOOKS: tuple[str, ...] = (
    # 配置读完之后（启动早期）。kwargs: config
    "on_config_loaded",
    # 版本列表刷新完之后。kwargs: versions（该列表只读，别改）
    "on_versions_refreshed",
    # 游戏进程拉起来之前。kwargs: context —— 可写字典，
    # 允许改 context["extra_vm_args"] / context["extra_program_args"]（list）
    "on_before_launch",
    # 游戏退出、收尾与自动备份都做完之后。kwargs: version_name,
    # profile_name, playtime_minutes, exit_code
    "on_game_exited",
)
_HOOK_SET = frozenset(HOOKS)


class ExtensionAPI:
    """交给扩展模块的注册入口（只有 ``on()`` 一个是稳定接口）。"""

    def __init__(self, registry: "ExtensionRegistry", name: str) -> None:
        self._registry = registry
        self.name = name
        self._bound: list[str] = []

    def on(self, hook: str, callback: Callable[..., Any]) -> None:
        """把 ``callback`` 挂到 ``hook`` 上。钩子名不认识会抛 ValueError。"""
        if hook not in _HOOK_SET:
            raise ValueError(
                f"不认识的钩子「{hook}」（可用：{'、'.join(HOOKS)}）"
            )
        if not callable(callback):
            raise TypeError("回调必须可调用")
        self._bound.append(hook)
        self._registry._add(hook, callback, self.name)

    @property
    def bound(self) -> list[str]:
        return list(self._bound)


class ExtensionRegistry:
    """载入 ``extensions/*.py`` 并分发钩子。

    ★ 线程安全：``load()`` 只跑一次，``call()`` 拿锁取一份快照再调 ——
    扩展里的回调可能会起线程回来调 ``call()``，所以不能在持锁状态下调用。
    """

    def __init__(self, base_dir: Path) -> None:
        self.dir = Path(base_dir) / EXT_DIR_NAME
        self._hooks: dict[str, list[tuple[str, Callable[..., Any]]]] = {
            name: [] for name in HOOKS
        }
        self._lock = threading.Lock()
        self._loaded = False
        self._module_names: list[str] = []
        self._failures: list[str] = []

    # ---------- 载入 ----------

    @property
    def disabled(self) -> bool:
        return bool(os.environ.get(DISABLE_ENV))

    def is_loaded(self) -> bool:
        with self._lock:
            return self._loaded

    def count(self) -> int:
        with self._lock:
            return len(self._module_names)

    def failures(self) -> list[str]:
        with self._lock:
            return list(self._failures)

    def load(self) -> int:
        """扫描并载入扩展，返回成功载入的个数。

        ★ **绝不抛异常**：这一步跑在启动路径附近的线程里，任何一个扩展
        写坏了都不能让启动器起不来。失败清单记在 ``failures()`` 里。
        """
        with self._lock:
            if self._loaded:
                return len(self._module_names)
            self._loaded = True                # 先占位，避免并发载入两遍
        if self.disabled:
            logger.info(f"扩展已按环境变量 {DISABLE_ENV} 停用")
            return 0
        try:
            files = sorted(p for p in self.dir.glob("*.py")
                           if p.is_file() and not p.name.startswith("_"))
        except OSError as e:
            logger.debug(f"扩展目录扫描失败（忽略）: {e}")
            return 0
        if not files:
            return 0
        loaded = 0
        for path in files:
            name = f"mdt_ext_{path.stem}"
            try:
                module = self._import(path, name)
            except Exception as e:                              # noqa: BLE001
                self._note_failure(path.name, e)
                continue
            register = getattr(module, "register", None)
            if register is None:
                logger.warning(f"扩展 {path.name} 没有 register(api)，已跳过")
                self._note_failure(path.name, "缺少 register(api)")
                continue
            try:
                register(ExtensionAPI(self, path.name))
            except Exception as e:                              # noqa: BLE001
                self._note_failure(path.name, e)
                continue
            with self._lock:
                self._module_names.append(path.name)
            loaded += 1
            logger.info(f"已载入扩展: {path.name}")
        if loaded:
            logger.info(f"扩展载入完成：{loaded} 个（目录 {self.dir}）")
        return loaded

    @staticmethod
    def _import(path: Path, name: str):
        """按文件路径 import 一个模块（不往 sys.modules 里留垃圾名字）。"""
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {path} 建立模块规格")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _note_failure(self, filename: str, err: Any) -> None:
        logger.error(f"扩展 {filename} 载入失败（已跳过）: {err}", exc_info=isinstance(err, Exception))
        with self._lock:
            self._failures.append(f"{filename}: {err}")

    # ---------- 分发 ----------

    def _add(self, hook: str, callback: Callable[..., Any], source: str) -> None:
        with self._lock:
            self._hooks[hook].append((source, callback))

    def has(self, hook: str) -> bool:
        with self._lock:
            return bool(self._hooks.get(hook))

    def call(self, hook: str, **kwargs: Any) -> list[Any]:
        """依次调用该钩子上的回调，返回各返回值（出错的那个返回 None）。

        ★ 每个回调单独 try —— 一个扩展崩了不能影响别的扩展，更不能
        影响启动器本体（这些钩子挂在启动/退出流程上）。
        """
        with self._lock:
            hooks = list(self._hooks.get(hook, ()))
        if not hooks:
            return []
        results: list[Any] = []
        for source, callback in hooks:
            try:
                results.append(callback(**kwargs))
            except Exception as e:                              # noqa: BLE001
                logger.error(f"扩展 {source} 的 {hook} 钩子出错（已忽略）: {e}")
                results.append(None)
        return results
