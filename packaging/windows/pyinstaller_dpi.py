# -*- coding: utf-8 -*-
"""运行 PyInstaller，并在它生成的 exe 清单里声明支持高分辨率。

    python pyinstaller_dpi.py <PyInstaller 的参数……>

不声明的话要等 Qt 启动后才设置，有的系统（Win7、老版本 Win10）已经把整个窗口
当图片拉伸了，字会发虚。PyInstaller 5 按固定模板重写清单，--manifest 里的
dpiAware 会被丢掉，所以在模板生成时补上。

true/pm 在 Win8.1 以后是“每个显示器单独缩放”，更早的系统当作 true（按系统缩放）；
PerMonitor（v1）和 Qt 5 自己设置的一致。
"""

import sys

from PyInstaller.utils.win32 import winmanifest

DPI_SETTINGS = [
    ("dpiAware", "http://schemas.microsoft.com/SMI/2005/WindowsSettings", "true/pm"),
    ("dpiAwareness", "http://schemas.microsoft.com/SMI/2016/WindowsSettings", "PerMonitor"),
]

_todom = winmanifest.Manifest.todom


def todom(self):
    doc = _todom(self)
    for ws in doc.getElementsByTagName("windowsSettings"):
        for tag, ns, value in DPI_SETTINGS:
            e = doc.createElement(tag)
            e.setAttribute("xmlns", ns)
            e.appendChild(doc.createTextNode(value))
            ws.appendChild(e)
    return doc


winmanifest.Manifest.todom = todom

if __name__ == "__main__":
    from PyInstaller.__main__ import run
    run(sys.argv[1:])
