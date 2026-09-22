# -*- coding: utf-8 -*-
"""把各功能 Mixin 组装成完整的主类。"""
from .gui_core import CoreMixin
from .gui_main import MainMixin
from .gui_profiles import ProfilesMixin
from .gui_game import GameMixin
from .gui_backup import BackupMixin
from .gui_dialog import DialogMixin
from .gui_log import LogMixin
from .gui_versions import VersionsMixin
from .gui_updates import UpdatesMixin


class MindustryLauncher(
    CoreMixin,
    MainMixin,
    ProfilesMixin,
    GameMixin,
    BackupMixin,
    DialogMixin,
    LogMixin,
    VersionsMixin,
    UpdatesMixin,
):
    """Mindustry 多版本启动器主类，功能由各 Mixin 组合而成。"""

