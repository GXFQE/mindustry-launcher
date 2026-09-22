# -*- coding: utf-8 -*-
"""版本与兼容性常量 —— **全项目唯一的版本号来源**。

为什么要单独一个模块（而不是写死在别处）：

1. **发布出去以后，版本号要能一处改、处处对** —— 日志、窗口标题、
   ``--version``、User-Agent、README 都引用这里，避免出现「日志说 1.0.1、
   界面上写着 1.0.0」这种对不上的情况；
2. ``CONFIG_VERSION`` 是**配置文件的格式版本**，跟应用版本号不是一回事：
   应用每次发版都会变，配置格式只在「键的含义/结构变了」时才 +1。
   见 ``config.MIGRATIONS`` 的迁移链；
3. 这个模块**不 import 包内任何东西**（顺手也不 import 标准库），
   所以谁都能 import 它，不会绕出循环依赖。
"""

# ---- 应用标识 ----------------------------------------------------------

APP_NAME = "Mindustry 启动器"
# 机器可读的名字：日志、User-Agent、扩展点里用它做命名空间。
APP_ID = "mindustry-launcher"
__version__ = "1.0.0"

# GitHub API 要求带 User-Agent；下载走的是浏览器式的 UA（见 utils.download_file，
# 有些镜像站会按 UA 判断），两处别混用。
USER_AGENT = f"{APP_ID}/{__version__}"

# ---- 格式版本（决定「老数据能不能被新代码读懂」）------------------------

# config.json 的结构版本。改动规则：
#   * 只是**新增**一个带默认值的键 → **不用**动它（老配置缺这一项会走默认值，
#     新配置在老启动器里会被原样保留，见 ConfigManager 的「未知键保留」）；
#   * **改名、改含义、删键、换类型** → +1，并在 config.MIGRATIONS 里补一条
#     从 (旧版本 → 新版本) 的迁移函数。
# 版本号缺失 = 0（小于 1 的都当成第 0 代，正是加迁移链之前的那些配置）。
CONFIG_VERSION = 1

# 版本清单（versions/manifests/<类型>_<版本>.json）的结构版本。
# 读的时候**不校验**这个字段：清单是数据资产，新代码必须能读老清单。
# 它只用来给「将来真要改清单结构」留一个判断依据。
MANIFEST_VERSION = 1

# 需要的最低 Python（源码运行时的自检用；打包后的 exe 与之无关）。
MIN_PYTHON = (3, 10)


def version_string() -> str:
    """给人看的版本号，比如 ``1.0.0``。"""
    return __version__


def full_version() -> str:
    """带应用名的一行版本信息（日志/关于框用）。"""
    major, minor = MIN_PYTHON
    return f"{APP_NAME} {__version__}（需要 Python {major}.{minor}+）"
