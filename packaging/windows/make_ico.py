# -*- coding: utf-8 -*-
"""把 discburner.svg 转成多尺寸的 Windows 图标（.ico，内嵌 PNG）。

    python make_ico.py <输入.svg> <输出.ico>
"""

import struct
import sys

from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt5.QtGui import QGuiApplication, QImage, QPainter
from PyQt5.QtSvg import QSvgRenderer

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render_png(renderer, size):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p)
    p.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


def main(src, dst):
    QGuiApplication(sys.argv[:1] + ["-platform", "offscreen"])
    renderer = QSvgRenderer(src)
    pngs = [render_png(renderer, s) for s in SIZES]
    out = struct.pack("<HHH", 0, 1, len(SIZES))
    offset = 6 + 16 * len(SIZES)
    for size, png in zip(SIZES, pngs):
        dim = 0 if size >= 256 else size      # 0 表示 256
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    with open(dst, "wb") as f:
        f.write(out + b"".join(pngs))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
