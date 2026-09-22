# -*- coding: utf-8 -*-
"""更新界面：手动/自动检查更新、关闭窗口清理。"""
import logging
import threading
from tkinter import messagebox

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
        self.root.quit()
        self.root.destroy()
        logger.info("启动器已关闭")
