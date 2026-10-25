# -*- coding: utf-8 -*-
"""启动冒烟测试：走真实入口 `python3 -m discburner`，主窗口出现 3 秒后退出。

gui_smoke.py 直接构造 MainWindow，不经过 main()；这里专门覆盖 main()，
用来发现老版本 Qt 缺接口这类问题（麒麟 V10 2020 版是 Qt 5.6.1）。

    QT_QPA_PLATFORM=offscreen python3 tests/startup_smoke.py
"""

import os
import runpy
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from discburner import gui  # noqa: E402

shown = []
_orig_show = gui.MainWindow.show


def _show(self):
    _orig_show(self)
    shown.append(self.windowTitle())
    QTimer.singleShot(3000, QApplication.quit)


gui.MainWindow.show = _show
sys.argv = ["discburner"]
try:
    runpy.run_module("discburner", run_name="__main__")
except SystemExit as e:
    assert e.code == 0, "退出码 %r" % e.code
assert shown, "主窗口没有出现"
print("startup OK:", shown[0])
