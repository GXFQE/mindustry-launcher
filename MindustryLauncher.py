# -*- coding: utf-8 -*-
"""Mindustry 启动器入口。"""
from tkinter import messagebox

from launcher.gui import MindustryLauncher
from launcher.utils import logger


def main() -> None:
    try:
        app = MindustryLauncher()
        app.root.mainloop()
    except Exception as e:
        logger.critical(f"程序崩溃: {e}", exc_info=True)
        messagebox.showerror("错误", f"程序崩溃: {e}")


if __name__ == "__main__":
    main()
