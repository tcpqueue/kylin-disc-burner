# -*- coding: utf-8 -*-
"""自检：`discburner --selftest [输出ISO路径]`

不需要光驱、不启动界面。用当前实际加载的 Python 和 xorriso，
对一个 ISO 镜像文件做“首次刻录 → 追加”，再读回核对文件名和 MD5。
内网机器装好后可以用它确认环境正常。
"""

import os
import platform
import shutil
import sys
import tempfile

from . import __version__
from . import backend as B

NAMES = ["工作报告.docx", "2025年度预算表（最终版）.xlsx", "项目资料/会议纪要 第三次.doc"]
APPENDED = "追加/" + "长" * 80 + ".txt"


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
    return path


def run(keep_iso=None):
    print("光盘刻录助手 %s 自检" % __version__)
    print("  Python : %s (%s)" % (platform.python_version(), sys.executable))
    print("  架构   : %s" % platform.machine())
    try:
        xo = B.find_xorriso()
    except B.XorrisoNotFound as e:
        print("失败：%s" % e)
        return 1
    print("  xorriso: %s (%s)" % (".".join(map(str, B.xorriso_version(xo))), xo))

    tmp = tempfile.mkdtemp(prefix="discburner-selftest-")
    try:
        src = os.path.join(tmp, "src")
        for rel in NAMES:
            _write(src, rel, rel * 200)
        extra = _write(src, APPENDED, "appended")
        iso = os.path.join(tmp, "selftest.iso")

        code, out = B.run_xorriso(B.burn_args(
            iso, [(os.path.join(src, "工作报告.docx"), "/工作报告.docx"),
                  (os.path.join(src, "2025年度预算表（最终版）.xlsx"), "/2025年度预算表（最终版）.xlsx"),
                  (os.path.join(src, "项目资料"), "/项目资料")],
            volid="自检光盘"))
        if code != 0 or "checksums match" not in out:
            print("失败：第一次刻录出错\n" + out)
            return 1
        code, out = B.run_xorriso(B.burn_args(iso, [(extra, "/" + APPENDED)]))
        if code != 0 or "checksums match" not in out:
            print("失败：追加刻录出错\n" + out)
            return 1

        media = B.read_media(iso)
        files = {e.path for e in B.list_disc_files(iso)}
        expect = {"/" + n for n in NAMES} | {"/" + APPENDED}
        missing = expect - files
        if missing or media.sessions != 2 or not media.appendable or media.volume_id != "自检光盘":
            print("失败：读回结果不对")
            print("  缺少文件：%s" % sorted(missing))
            print("  会话数 %d，可追加 %s，卷标 %r" % (media.sessions, media.appendable, media.volume_id))
            return 1
        print("  首次刻录 + 追加：通过（%d 个会话，MD5 校验一致）" % media.sessions)
        print("  中文文件名读回：通过（%d 个文件）" % len(expect))
        if keep_iso:
            shutil.copy(iso, keep_iso)
            print("  测试镜像已保存：%s（可拿到 Windows 上打开核对文件名）" % keep_iso)
        print("自检通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
