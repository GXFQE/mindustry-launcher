# -*- coding: utf-8 -*-
"""更新界面：手动/自动检查更新、关闭窗口清理。

★ 这里有两套完全不同的「更新」，别混：

  * ``UpdateManager``（``updates.py``）—— **游戏**版本（Anuken 的 jar）；
  * ``selfupdate``（``selfupdate.py``）—— **启动器自己**（本项目的 Release）。

前者下载完就是一堆文件，后者要**替换正在运行的程序**，所以多了一道
「退出时才动手」的流程，见 ``_apply_staged_update``。
"""
import logging
import threading
from pathlib import Path
from tkinter import messagebox

from . import selfupdate
from .version import __version__

logger = logging.getLogger(__name__)


class UpdatesMixin:
    def check_updates(self) -> None:
        with self._proc_lock:
            running = bool(
                self.current_process and self.current_process.poll() is None
            )
        # 原来这里判的是 self.current_process（=游戏在跑），文案却写
        # 「下载正在进行」—— 游戏一开就点不动检查更新，还弹一句驴唇不对马嘴的话
        if running:
            messagebox.showinfo("提示", "游戏运行中，请先关闭游戏再检查更新")
            return
        if self.update_manager.downloading.is_set():
            messagebox.showinfo("提示", "下载正在进行")
            return
        self.set_status("🔍 正在检查更新...")
        # 网络请求不能放在 GUI 线程里同步跑——断网时界面会一直僵着
        # 直到超时。丢到后台线程去，回来了再弹窗。
        threading.Thread(target=self._check_updates_task, daemon=True).start()

    def _check_updates_task(self) -> None:
        try:
            updates = self.update_manager.get_available_updates()
        except Exception as e:
            logger.error(f"检查更新失败: {e}")
            self.run_on_gui(
                lambda: self.set_status("❌ 检查更新失败，请检查网络")
            )
            return
        self.run_on_gui(lambda: self._show_update_result(updates))

    def _show_update_result(self, updates: list) -> None:
        """回到 GUI 线程里弹窗询问（Tk 的控件只能在主线程碰）。"""
        if not updates:
            self.set_status("✅ 所有版本已是最新")
            messagebox.showinfo("检查更新", "所有版本已是最新")
            return
        msg = (
            "发现新版本：\n"
            + "\n".join(f"- {u['type']} {u['version']}" for u in updates)
            + "\n\n是否下载？"
        )
        if messagebox.askyesno("发现更新", msg):
            self.set_status("开始下载...")
            self.update_manager.start_download_updates(
                updates, status_callback=self.set_status
            )
        else:
            self.set_status("已取消更新")

    def auto_update_check(self) -> None:
        if self.current_process:
            return
        self.set_status("🔍 后台检查更新...")
        # 故意用 daemon 线程而不是 self.executor：
        # executor 的线程是非 daemon 的，解释器退出时会 join 它们，
        # 而这里要发网络请求、断网时会一直卡到超时 ——
        # 放进 executor 的代价就是关窗口后还要干等十几秒才真的退出。
        threading.Thread(
            target=self.update_manager.check_for_updates,
            args=(True, self.set_status),
            daemon=True,
        ).start()

    # ---------- 启动器自身更新（跟上面那套游戏版本更新无关）----------

    def _self_update_startup(self) -> None:
        """窗口已经能用之后才做：清执行体残留 → 报上次结果 → 后台检查。

        整段包在 try 里 —— 自更新是**锦上添花**，任何一步出问题都不许
        影响启动器本身。
        """
        try:
            selfupdate.cleanup_updater_dir()
            self._report_last_self_update()
            if selfupdate.mode_of(self.config) == selfupdate.MODE_OFF:
                logger.info("启动器自更新已停用（源码运行 / 环境变量 / 设置里关掉）")
                return
            threading.Thread(
                target=self._self_update_task, daemon=True
            ).start()
        except Exception as e:                                       # noqa: BLE001
            logger.error(f"启动器自更新初始化失败（不影响使用）: {e}")

    def _self_update_task(self) -> None:
        """后台线程：检查 + 下载 + 解包。绝不占用 GUI 线程（要发网络请求）。"""
        result = selfupdate.check_and_stage(
            self.config,
            self.base_dir,
            stop_event=self.stop_event,
            on_status=self.set_status,
        )
        self.run_on_gui(lambda: self._on_self_update_result(result))

    def _on_self_update_result(self, result: dict) -> None:
        """回到 GUI 线程收尾（这里只写状态栏，**不弹模态框** —— 自更新
        失败不该打断用户，而且模态框嵌进事件循环容易出别的问题）。"""
        status = result.get("status")
        version = str(result.get("version") or "")
        if status == "staged":
            # 记下来，退出时交给执行体（见 _apply_staged_update）
            self.self_update_plan = result.get("plan_path")
            self.set_status(
                f"✅ 启动器更新 v{version} 已就绪，关闭启动器时自动替换"
            )
            logger.info(f"启动器更新 v{version} 已下载，等退出时应用")
        elif status == "available":
            self.set_status(f"ℹ️ 启动器有新版本 v{version}（可在设置里开启自动更新）")
            url = str(result.get("html_url") or "")
            if url:
                logger.info(f"启动器新版本下载页：{url}")
        elif status == "failed":
            self.set_status(
                f"⚠️ 启动器更新下载失败：{result.get('message') or '未知原因'}"
            )
        # latest / off：什么都不说（静默是默认状态）

    def _report_last_self_update(self) -> None:
        """读上次更新留下的状态，把结果告诉用户。

        这是「更新到底成没成」的唯一线索：执行体是在本程序退出之后跑的，
        它的输出没人看得见，只能靠它留下的 ``update-state.json``。
        """
        state = selfupdate.read_state(self.base_dir)
        if not state:
            return
        status = str(state.get("status") or "")
        version = str(state.get("version") or "?")
        selfupdate.clear_state(self.base_dir)
        if status == "done" and version == __version__:
            self.set_status(f"✅ 启动器已更新到 v{version}")
            logger.info(f"上次自更新已完成：v{version}")
        elif status == "done":
            # 文件换了、版本号却没变：被杀软拦下，或者用户自己又换回旧版
            logger.warning(
                f"上次自更新写入了 v{version}，但当前仍是 v{__version__}"
            )
            self.set_status(f"⚠️ 上次启动器更新（v{version}）没生效")
        elif status == "failed":
            logger.warning(f"上次自更新没完成：{state.get('message')}")
            self.set_status("⚠️ 上次启动器更新没完成（详见 launcher.log）")

    def _apply_staged_update(self) -> None:
        """退出前把已下载好的更新交给执行体。

        ★ 必须等到这里：Windows 不允许覆盖正在运行的 exe，所以只能
          「先退出、再由外部进程替换」。执行体是 %TEMP% 里那份 exe 副本，
          以 ``--apply-update`` 启动，等本进程退出之后才动手。
        ★ 调用点在 ``on_closing`` 里、``config.save()`` **之后** ——
          先把配置安全落地，再谈换程序。
        """
        plan_path = self.self_update_plan
        if not plan_path:
            return
        try:
            if selfupdate.launch_updater(
                Path(plan_path), Path(self.base_dir)
            ):
                logger.info("已把更新交给执行体，本进程退出后替换")
            else:
                logger.warning("更新执行体没起来，本次不更新（下次启动会再试）")
        except Exception as e:                                       # noqa: BLE001
            logger.error(f"启动更新执行体失败（本次不更新）: {e}")

    def on_closing(self) -> None:
        self.stop_event.set()
        # 日志窗口的 after 定时器要先撤掉，否则它会在窗口销毁后再触发一次
        self._close_log_window()
        # 游戏输出先收尾（游戏可能还在跑 —— 那时只是关掉文件句柄，
        # 读线程是 daemon，不会拖住退出）
        self.game_log.close()
        # 预热拼到一半的话没必要留着；已拼好的临时 jar 也要清掉
        self.cleanup_preheat()
        # 这时 executor 里只剩下载任务和可能还在拼的预热，两者都会尽快收手
        #（下载循环里有 stop_event 检查，预热拼完也会因 stop_event 丢掉结果），
        # 所以这里等它一下是安全的。
        self.executor.shutdown(wait=True)
        # 再清一次：万一刚才那次预热是「卡在拼的中途」，它的临时文件是在
        # 上面那次 cleanup_preheat() 之后才被删掉的，目录此时才空得下来。
        self.cleanup_preheat()
        self.config.save()
        # ★ 自更新最后一步：配置落地之后，才把「换程序」交给执行体。
        #   它在另一个进程里等本进程退出，所以这里调用完照常往下走。
        self._apply_staged_update()
        self.root.quit()
        self.root.destroy()
        logger.info("启动器已关闭")
