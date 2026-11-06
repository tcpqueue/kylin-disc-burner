# -*- coding: utf-8 -*-
"""界面冒烟测试：无界面模式下驱动主窗口，对 ISO 文件做两次刻录。

    QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py [截图输出目录]
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox, QTreeWidgetItemIterator  # noqa: E402

from discburner import backend as B  # noqa: E402
from discburner import gui  # noqa: E402

messages = []
answer = [QMessageBox.Yes]   # 模拟用户在提问对话框里点的按钮


def _record(kind):
    def f(_parent, title, text, *a, **k):
        messages.append((kind, text))
        return answer[0]
    return f


QMessageBox.information = staticmethod(_record("info"))
QMessageBox.warning = staticmethod(_record("warn"))
QMessageBox.critical = staticmethod(_record("error"))
QMessageBox.question = staticmethod(_record("question"))


def wait(app, win, timeout=60):
    end = time.time() + timeout
    idle = 0
    while time.time() < end:
        app.processEvents()
        idle = idle + 1 if not win.job.running() else 0
        if idle > 50:
            return
        time.sleep(0.01)
    raise RuntimeError("timeout")


def main():
    shot_dir = sys.argv[1] if len(sys.argv) > 1 else None
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "src")
    os.makedirs(os.path.join(src, "项目资料"))
    for rel in ("工作报告.docx", "2025年度预算表（最终版）.xlsx", "项目资料/会议纪要 第三次.doc"):
        with open(os.path.join(src, rel), "w") as f:
            f.write(rel * 100)
    iso = os.path.join(tmp, "光盘.iso")

    app = QApplication(sys.argv)
    gui.setup_app(app)
    win = gui.MainWindow()
    win.show()
    wait(app, win)

    assert win.chk_eject.isChecked(), "完成后弹出光盘应默认勾选"
    # 麒麟 2020（Qt 5.6）上不能开 Qt 的高分屏缩放，SP1（5.12）和 Windows 可以
    assert not gui.qt_scaling_follows_system("5.6.1", windows=False)
    assert gui.qt_scaling_follows_system("5.12.12", windows=False)
    assert gui.qt_scaling_follows_system("5.5.1", windows=True)
    # 选择“ISO 镜像文件”目标（跳过文件对话框）
    win.cb_drive.insertItem(0, B.Drive(iso).label, iso)
    win.cb_drive.setCurrentIndex(0)
    win.refresh_media()
    wait(app, win)
    assert win.media.blank, win.media.raw

    # 第一次刻录
    win.add_paths([os.path.join(src, "工作报告.docx"), os.path.join(src, "项目资料")])
    win.ed_volid.setText("资料盘")
    win.burn()
    wait(app, win)
    assert ("info", ) == messages[-1][:1], messages
    assert "校验通过" in messages[-1][1], messages[-1]
    assert win.media.appendable and win.media.sessions == 1, win.media.raw
    assert win.pending == []
    # 一个列表：光盘上的文件带修改时间，文件夹在前
    top = [win.tree.topLevelItem(i) for i in range(win.tree.topLevelItemCount())]
    assert [t.text(0) for t in top] == ["项目资料", "工作报告.docx"], [t.text(0) for t in top]
    assert top[1].text(2).startswith("20") and top[1].text(1), (top[1].text(1), top[1].text(2))

    # 刷新只是读，不能把系统挂载的光盘卸掉（文件管理器会读不到）
    calls = []
    real = B.unmount, B.is_mounted, B.mount_disc
    B.unmount = lambda d: calls.append(("unmount", d))
    B.is_mounted = lambda d: True
    B.mount_disc = lambda d: calls.append(("mount", d)) or (d, "")
    try:
        win.refresh_media()
        wait(app, win)
        assert calls == [], calls
    finally:
        B.unmount, B.is_mounted, B.mount_disc = real

    # 第二次追加，放进新建的日期文件夹
    win.tree.clearSelection()
    win.new_folder("2025-10-25")
    assert win._target_folder() == "/2025-10-25"
    win.add_paths([os.path.join(src, "2025年度预算表（最终版）.xlsx")])
    win.burn()
    wait(app, win)
    assert messages[-1][0] == "info", messages
    assert win.media.sessions == 2, win.media.raw
    paths = {e.path for e in win.disc_entries}
    for p in ("/工作报告.docx", "/项目资料/会议纪要 第三次.doc",
              "/2025-10-25/2025年度预算表（最终版）.xlsx"):
        assert p in paths, (p, paths)

    # 从“光盘现有内容”拖出：镜像文件没有挂载目录，先取到临时目录
    def select(*disc_paths):
        win.tree.clearSelection()
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.UserRole) in disc_paths and \
                    it.value().data(0, gui.ROLE_KIND) == gui.KIND_DISC:
                it.value().setSelected(True)
            it += 1
    select("/项目资料", "/项目资料/会议纪要 第三次.doc", "/工作报告.docx")
    assert win.tree_disc.selected_paths() == ["/工作报告.docx", "/项目资料"]
    out = win._drag_paths(win.tree_disc.selected_paths())
    assert [os.path.basename(p) for p in out] == ["工作报告.docx", "项目资料"], out
    with open(os.path.join(out[1], "会议纪要 第三次.doc")) as f:
        assert f.read() == "项目资料/会议纪要 第三次.doc" * 100
    urls = gui.DiscTree.mime_for(out).urls()
    # Windows 上 Qt 给的是 C:/... 形式，按同一路径比较
    assert [os.path.normpath(u.toLocalFile()) for u in urls] == [os.path.normpath(p) for p in out]
    # 真正的拖动要鼠标，这里换掉 QDrag，只核对交给文件管理器的内容和动作
    dragged = []

    class FakeDrag(object):
        def __init__(self, _source):
            pass

        def setMimeData(self, mime):
            dragged.append([os.path.basename(u.toLocalFile()) for u in mime.urls()])

        def exec_(self, actions, default):
            dragged.append((actions, default))
            return default
    real_drag, gui.QDrag = gui.QDrag, FakeDrag
    try:
        win.tree_disc.startDrag(Qt.CopyAction)
    finally:
        gui.QDrag = real_drag
    assert dragged == [["工作报告.docx", "项目资料"], (Qt.CopyAction, Qt.CopyAction)], dragged

    # 右键“复制到…”
    real_open = B.open_folder
    B.open_folder = lambda p: True
    try:
        dest = os.path.join(tmp, "取出")
        os.makedirs(dest)
        select("/2025-10-25")
        win.copy_out(dest)
        wait(app, win)
        got = os.path.join(dest, "2025-10-25", "2025年度预算表（最终版）.xlsx")
        assert os.path.exists(got), os.listdir(dest)
        assert os.access(got, os.W_OK)
        assert win.lb_stage.text() == "复制完成", win.lb_stage.text()
    finally:
        B.open_folder = real_open

    # 替换同名文件时应提示：新的那行写“替换”，旧的那行写“将被新文件替换”
    win.tree.clearSelection()
    win.add_paths([os.path.join(src, "工作报告.docx")], "/")
    notes = sorted(win.tree.topLevelItem(i).text(3) for i in range(win.tree.topLevelItemCount())
                   if win.tree.topLevelItem(i).text(0) == "工作报告.docx")
    assert notes == sorted([gui.HINT_REPLACE, "将被新文件替换"]), notes

    # 再放一个待刻录项，方便截图
    win.add_paths([os.path.join(src, "项目资料")], "/")
    win.chk_log.setChecked(True)
    if shot_dir:
        win.resize(1100, 760)
        app.processEvents()
        win.grab().save(os.path.join(shot_dir, "discburner.png"))

    # 模拟一张会话未关闭的光盘：刷新后应自动提示，并能一键修复
    toc_damaged = ("Media current: DVD+R\n"
                   "Media status : is written , is appendable , but next track is damaged\n"
                   "Media blocks :    20000 readable ,  2200000 writable ,  2295104 overall\n"
                   "Other session:   1 ,         0 ,     20000s ,\n")
    fake_dev = B.windows_dev("Y") if B.IS_WINDOWS else "/dev/sr9"
    win.cb_drive.insertItem(0, "测试光驱 (%s)" % fake_dev, fake_dev)
    win.cb_drive.setCurrentIndex(0)
    real_repair_args = B.repair_args
    repaired = []

    def fake_repair_args(dev, keep_open=True, force=False):
        repaired.append(dev)
        return real_repair_args(iso, keep_open)   # 对镜像文件执行，结果应为“无需修复”
    gui.B.repair_args = fake_repair_args
    del messages[:]

    # 刷新后自动弹出提示；用户先选“否”
    answer[0] = QMessageBox.No
    win._media_done(0, toc_damaged)
    assert messages and messages[0][0] == "question" and "会话没有关闭" in messages[0][1], messages
    assert win.banner.button("修复") is not None, "提示条里应有修复按钮"
    assert not repaired
    assert "会话未关闭" in win.lb_media.text()

    # 同一张盘再次刷新不应重复弹窗；此时不能刻录
    win._media_done(0, toc_damaged)
    assert len(messages) == 1, messages
    win.burn()
    assert "请先点上方提示条里的“修复”" in messages[-1][1], messages[-1]
    if shot_dir:
        app.processEvents()
        win.grab().save(os.path.join(shot_dir, "discburner_repair.png"))

    # 点“修复未关闭会话”按钮并确认
    answer[0] = QMessageBox.Yes
    win.banner.button("修复").click()
    wait(app, win)
    assert repaired == [fake_dev], repaired
    assert any("没有做任何改动" in t for k, t in messages), messages

    # 前面是 ISO、后面被 Windows 写成 UDF 的盘：提示条说明情况；不能往原盘写
    import test_backend
    answer[0] = QMessageBox.No
    win._fs_prompted = fake_dev     # 不弹“读出全部文件”的提问
    real_find = B.find_udf_session
    B.find_udf_session = lambda dev, tracks: 2256
    try:
        win._media_done(0, test_backend.TOC_ISO_THEN_UDF)
        wait(app, win)
        # 麒麟上 UDF 部分要以管理员身份挂载才读得到；Windows 自己会挂载，直接给转存
        want = "转存到新光盘…" if B.IS_WINDOWS else "读出全部文件"
        assert "UDF" in win.banner.label.text() and win.banner.button(want), win.banner.label.text()
        assert win.media.volume_id == "光盘2025-10-28"
        del messages[:]
        win.burn()
    finally:
        B.find_udf_session = real_find
        answer[0] = QMessageBox.Yes
    assert messages[-1][0] == "question" and "转存到新光盘" in messages[-1][1], messages[-1]

    # UDF 部分挂着时（Windows 上系统自己就挂着）：两部分一起列出来，可以转存、复制
    udf_dir = os.path.join(tmp, "UDF部分")
    os.makedirs(os.path.join(udf_dir, "项目资料"))
    with open(os.path.join(udf_dir, "项目资料", "现场照片.jpg"), "w") as f:
        f.write("udf" * 100)
    real_mount_info = B.mount_info
    B.mount_info = lambda dev: (udf_dir, "udf", "ro") if dev == fake_dev else real_mount_info(dev)
    del messages[:]
    try:
        win._media_done(0, test_backend.TOC_ISO_THEN_UDF)
        wait(app, win)
        assert "/项目资料/现场照片.jpg" in {e.path for e in win.disc_entries}, win.disc_entries
        assert win.banner.button("转存到新光盘…") and win.banner.button("复制到电脑…"), \
            win.banner.label.text()
        assert messages == [], messages
        if shot_dir:
            app.processEvents()
            win.grab().save(os.path.join(shot_dir, "discburner_udf.png"))
        # 复制 UDF 部分的文件
        select("/项目资料")
        dest2 = os.path.join(tmp, "取出2")
        os.makedirs(dest2)
        real_open = B.open_folder
        B.open_folder = lambda p: True
        win.copy_out(dest2)
        end = time.time() + 30
        while win.lb_stage.text() != "复制完成" and time.time() < end:
            app.processEvents()
            time.sleep(0.02)
        B.open_folder = real_open
        assert os.path.exists(os.path.join(dest2, "项目资料", "现场照片.jpg")), win.lb_stage.text()
    finally:
        B.mount_info = real_mount_info
        answer[0] = QMessageBox.Yes

    # 光盘诊断窗口：对镜像文件诊断（镜像不显示挂载/修复按钮）
    dlg = gui.DiagDialog(win, iso)
    dlg.show()
    end = time.time() + 60
    while dlg.diag is None and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    for _ in range(20):
        app.processEvents()
    assert dlg.diag is not None, dlg.lb_summary.text()
    rows = dict((dlg.tree_info.topLevelItem(i).text(0), dlg.tree_info.topLevelItem(i).text(1))
                for i in range(dlg.tree_info.topLevelItemCount()))
    assert "可追加" in rows["盘片状态"], rows
    assert dlg.tree_tracks.topLevelItemCount() == 2, dlg.tree_tracks.topLevelItemCount()
    if B.find_tool("blkid"):
        assert "Joliet" in rows["文件系统"], rows
    assert not dlg.btn_mount.isVisible() and not dlg.btn_repair.isVisible()
    dlg.copy_report()
    assert "光盘诊断报告" in QApplication.clipboard().text()
    assert os.path.exists(dlg.report_path), dlg.report_path
    if shot_dir:
        dlg.chk_raw.setChecked(False)
        app.processEvents()
        dlg.grab().save(os.path.join(shot_dir, "discburner_diag.png"))
    dlg.close()

    # 恢复长文件名：造一张“麒麟刻了无 Joliet 会话、Windows 又追加”的盘
    short_iso = os.path.join(tmp, "短名.iso")
    long_name = "2023年度《办公设备管理制度》相关表格.xls"
    with open(os.path.join(src, long_name), "w") as f:
        f.write("x")
    B.run_xorriso(B.COMMON_ARGS + ["-joliet", "off", "-dev", B.xpath(short_iso),
                                   "-map", B.xpath(os.path.join(src, long_name)), "/" + long_name, "-commit"])
    B.run_xorriso(B.COMMON_ARGS + ["-read_fs", "ecma119", "-rockridge", "off", "-joliet", "on",
                                   "-dev", B.xpath(short_iso), "-map", B.xpath(os.path.join(src, "工作报告.docx")),
                                   "/NEW.DOC", "-commit"])
    win.cb_drive.insertItem(0, B.Drive(short_iso).label, short_iso)
    win.cb_drive.setCurrentIndex(0)
    win.refresh_media()
    wait(app, win)
    del messages[:]
    answer[0] = QMessageBox.Yes
    rd = gui.RecoverDialog(win, short_iso)
    rd.show()
    end = time.time() + 60
    while rd.plan is None and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    assert rd.plan and len(rd.plan.renames) == 1, rd.lb.text()
    assert rd.tree.topLevelItem(0).text(1) == "/" + long_name
    if shot_dir:
        app.processEvents()
        rd.grab().save(os.path.join(shot_dir, "discburner_recover.png"))
    rd.write()
    wait(app, win)
    assert any("已恢复 1 个文件" in t for k, t in messages), messages
    assert "/" + long_name in {e.path for e in win.disc_entries}

    # 写入卡住的提示：进度 90 秒不动就变红，之后同样的进度行不能把提示冲掉
    class FakeJob(object):
        def running(self):
            return True
    real_job, win.job = win.job, FakeJob()
    win.burning = True
    win._on_stage(gui.STAGE_WRITING)
    win._on_progress(27.0)
    win._last_change -= gui.STALL_SECONDS + 1
    win._check_stall()
    assert "没有进展" in win.lb_stage.text(), win.lb_stage.text()
    win._on_stage(gui.STAGE_WRITING)
    win._on_progress(27.0)
    assert "没有进展" in win.lb_stage.text(), "重复的进度行冲掉了提示"
    win._on_progress(28.0)
    win._on_stage(gui.STAGE_WRITING)
    assert win.lb_stage.text() == gui.STAGE_WRITING, win.lb_stage.text()
    win.job, win.burning = real_job, False

    # 回到第一张镜像：右键“从光盘删除（隐藏）”
    win.cb_drive.setCurrentIndex(win.cb_drive.findData(iso))
    win.refresh_media()
    wait(app, win)
    select("/2025-10-25")
    del messages[:]
    answer[0] = QMessageBox.Yes
    assert win._can_hide()
    assert win.tree_disc.selected_paths() == ["/2025-10-25"], (
        win.current_dev(), win.tree_disc.selected_paths(), [e.path for e in win.disc_entries])
    win.hide_selected()
    wait(app, win)
    assert any("已删除 1 项" in t for k, t in messages), messages
    paths = {e.path for e in win.disc_entries}
    assert "/工作报告.docx" in paths and not any(p.startswith("/2025-10-25") for p in paths), paths

    # 转存的后半段：读到电脑上的文件放进待刻录，刻到另一张盘（这里是新镜像）上
    stage_src = os.path.join(tmp, "转存读出")
    os.makedirs(os.path.join(stage_src, "Windows加的"))
    with open(os.path.join(stage_src, "Windows加的", "总结.docx"), "w") as f:
        f.write("udf")
    new_iso = os.path.join(tmp, "新盘.iso")
    win.cb_drive.insertItem(0, B.Drive(new_iso).label, new_iso)
    win.cb_drive.setCurrentIndex(0)
    win.refresh_media()
    wait(app, win)
    win.pending = [gui.Pending(os.path.join(stage_src, "Windows加的"), "/", transfer=True)]
    win.pending[0].size, win.pending[0].count = B.local_size(win.pending[0].src)
    win._transfer = dict(stage=stage_src, files=1, size=3, volid="")
    win._pending_changed()
    assert "转存" in win.banner.label.text(), win.banner.label.text()
    del messages[:]
    win.burn()
    wait(app, win)
    assert messages and "转存完成" in messages[-1][1], messages
    assert "/Windows加的/总结.docx" in {e.path for e in win.disc_entries}
    assert win._transfer is None and not os.path.exists(stage_src) and win.pending == []

    print("GUI smoke OK; messages:")
    for k, t in messages:
        print("  [%s] %s" % (k, t.replace("\n", " | ")))
    shutil.copy(iso, os.path.join(shot_dir, "gui_test.iso")) if shot_dir else None
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
