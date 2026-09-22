# -*- coding: utf-8 -*-
"""Mindustry 启动器内部包。"""

# 版本号只有一处（launcher/version.py），这里顺手暴露一下，
# 方便外面的脚本 / 打包流程写 `launcher.__version__`。
from .version import __version__   # noqa: F401

__all__ = ["__version__"]
