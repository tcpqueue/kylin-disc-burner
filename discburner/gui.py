# -*- coding: utf-8 -*-
"""光盘刻录助手 图形界面（PyQt5，麒麟 V10 自带 python3-pyqt5）。"""

import datetime
import html
import os
import sys
import time

from PyQt5.QtCore import QObject, QProcess, QSettings, Qt, QThread, QTimer, pyqtSignal, qVersion
from PyQt5.QtGui import QFont, QFontDatabase, QIcon
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import __version__
from . import backend as B

APP_NAME = "光盘刻录助手"
IMAGE_ITEM = "__image__"
if B.IS_WINDOWS:
    LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                           "discburner", "logs")
else:
    LOG_DIR = os.path.expanduser("~/.local/share/discburner/logs")
# 写入进度这么久没变就提示检查光驱。xorriso 卡住时仍会每秒打印同样的进度行，
# 只看有没有输出是看不出来的
STALL_SECONDS = 90
STAGE_WRITING = "正在写入……"


class Job(QObject):
    """异步运行一次 xorriso，逐行回报输出与进度。"""

    line = pyqtSignal(str)
    progress = pyqtSignal(float)
    stage = pyqtSignal(str)
    finished = pyqtSignal(int, str)

    def __init__(self, parent=None):
        super(Job, self).__init__(parent)
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._done)
        # errorOccurred 是 Qt 5.6 才有的；自带运行环境是 Qt 5.5，用旧的 error 信号
        err_signal = getattr(self.proc, "errorOccurred", None) or self.proc.error
        err_signal.connect(self._error)
        self._buf = b""
        self._out = []
        self._tmp = None

    def start(self, args):
        self._buf = b""
        self._out = []
        cmd, self._tmp = B.xorriso_command(args)
        self.proc.start(cmd[0], cmd[1:])

    def running(self):
        return self.proc.state() != QProcess.NotRunning

    def _emit_line(self, raw):
        text = raw.decode("utf-8", "replace").rstrip()
        if not text:
            return
        self._out.append(text)
        self.line.emit(text)
        pct, hint = B.parse_progress(text)
        if pct is not None:
            self.progress.emit(pct)
        if hint:
            self.stage.emit(hint)

    def _read(self):
        self._buf += bytes(self.proc.readAllStandardOutput())
        # libburn 的进度行有时用 \r 刷新
        self._buf = self._buf.replace(b"\r", b"\n")
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            self._emit_line(raw)

    def _done(self, code, status):
        B.remove_quietly(self._tmp)
        self._tmp = None
        if self._buf:
            self._emit_line(self._buf)
            self._buf = b""
        if status == QProcess.CrashExit:
            code = -1
        self.finished.emit(code, "\n".join(self._out))

    def _error(self, err):
        if err == QProcess.FailedToStart:
            B.remove_quietly(self._tmp)
            self._tmp = None
            self.finished.emit(-1, "无法启动 xorriso")


class FuncThread(QThread):
    """在后台线程里跑一个会阻塞的函数（读盘、挂载），结果通过 done 信号送回。"""

    done = pyqtSignal(object)
    progress = pyqtSignal(str)

    def __init__(self, fn, *args, with_progress=False):
        super(FuncThread, self).__init__()
        self.fn = fn
        self.args = args
        self.with_progress = with_progress

    def run(self):
        try:
            if self.with_progress:
                result = self.fn(*self.args, progress=self.progress.emit)
            else:
                result = self.fn(*self.args)
        except Exception as e:  # 送回界面线程统一提示
            result = e
        self.done.emit(result)


LEVEL_STYLE = {B.OK: ("#1e8e3e", "✔"), B.INFO: ("#1a5fb4", "●"),
               B.WARN: ("#d35400", "▲"), B.ERROR: ("#c0392b", "✖")}


class DiagDialog(QDialog):
    """光盘诊断：盘片信息、轨道/会话、文件系统、内核视角，以及挂载 / 修复。"""

    def __init__(self, main, dev):
        super(DiagDialog, self).__init__(main)
        self.main = main
        self.dev = dev
        self.diag = None
        self.thread = None
        self.setWindowTitle("光盘诊断 - %s" % dev)
        self.resize(820, 640)
        v = QVBoxLayout(self)

        self.lb_summary = QLabel()
        self.lb_summary.setWordWrap(True)
        self.lb_summary.setTextFormat(Qt.RichText)
        self.lb_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.lb_summary)

        self.tree_info = QTreeWidget()
        self.tree_info.setHeaderLabels(["项目", "内容"])
        self.tree_info.setRootIsDecorated(False)
        self.tree_info.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        v.addWidget(self.tree_info, 3)

        v.addWidget(QLabel("会话 / 轨道："))
        self.tree_tracks = QTreeWidget()
        self.tree_tracks.setHeaderLabels(["类型", "序号", "起始扇区", "大小", "卷标"])
        self.tree_tracks.setRootIsDecorated(False)
        self.tree_tracks.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        v.addWidget(self.tree_tracks, 2)

        self.chk_raw = QCheckBox("显示原始输出")
        v.addWidget(self.chk_raw)
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setVisible(False)
        self.chk_raw.toggled.connect(self.raw.setVisible)
        v.addWidget(self.raw, 3)

        row = QHBoxLayout()
        self.btn_mount = QPushButton("挂载并打开")
        self.btn_mount.clicked.connect(self.mount)
        self.btn_open = QPushButton("打开")
        self.btn_open.clicked.connect(self.open_mounted)
        self.btn_unmount = QPushButton("卸载")
        self.btn_unmount.clicked.connect(self.unmount)
        self.btn_repair = QPushButton("修复未关闭会话…")
        self.btn_repair.setStyleSheet("color:#c0392b;font-weight:bold")
        self.btn_repair.clicked.connect(self.repair)
        self.btn_names = QPushButton("恢复长文件名…")
        self.btn_names.setStyleSheet("color:#d35400;font-weight:bold")
        self.btn_names.clicked.connect(self.recover_names)
        self.btn_copy = QPushButton("复制诊断报告")
        self.btn_copy.clicked.connect(self.copy_report)
        self.btn_again = QPushButton("重新诊断")
        self.btn_again.clicked.connect(self.start)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.reject)
        for b in (self.btn_mount, self.btn_open, self.btn_unmount, self.btn_repair,
                  self.btn_names, self.btn_copy, self.btn_again):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b_close)
        v.addLayout(row)
        self._buttons()
        self.start()

    # 后台任务 -------------------------------------------------------------

    def _run(self, text, fn, args, on_done):
        if self.thread and self.thread.isRunning():
            return
        self.lb_summary.setText("<b>%s</b>" % text)
        self._busy = True
        self._buttons()
        self.thread = FuncThread(fn, *args)
        self.thread.done.connect(lambda r: self._finished(r, on_done))
        self.thread.start()

    def _finished(self, result, on_done):
        self._busy = False
        on_done(result)
        self._buttons()

    def _buttons(self):
        busy = getattr(self, "_busy", False)
        d = self.diag
        self.btn_mount.setVisible(bool(d and d.can_mount))
        self.btn_open.setVisible(bool(d and d.mount))
        self.btn_unmount.setVisible(bool(d and d.mount) and not B.IS_WINDOWS)
        self.btn_repair.setVisible(bool(d and d.media.needs_repair and not d.is_image))
        self.btn_names.setVisible(bool(d and d.can_recover_names))
        for b in (self.btn_mount, self.btn_open, self.btn_unmount, self.btn_repair,
                  self.btn_names, self.btn_copy, self.btn_again):
            b.setEnabled(not busy)
        self.btn_copy.setEnabled(not busy and d is not None)

    # 诊断 -----------------------------------------------------------------

    def start(self):
        self._run("正在读取光盘信息……（光驱起转可能要十几秒）", B.diagnose, (self.dev,), self._show)

    def _show(self, d):
        if isinstance(d, Exception):
            self.lb_summary.setText("<b style='color:#c0392b'>诊断失败：%s</b>" % d)
            return
        self.diag = d
        parts = []
        for level, text in d.findings:
            color, mark = LEVEL_STYLE.get(level, ("black", "●"))
            parts.append("<span style='color:%s'>%s</span> %s" % (color, mark, html.escape(text)))
        self.lb_summary.setText("<br>".join(parts))
        self.tree_info.clear()
        for k, val in d.info_rows():
            self.tree_info.addTopLevelItem(QTreeWidgetItem([k, val]))
        self.tree_tracks.clear()
        for t in d.tracks:
            self.tree_tracks.addTopLevelItem(QTreeWidgetItem([
                t.kind_text, str(t.index), str(t.start),
                B.human_size(t.blocks * B.SECTOR), t.volid]))
        self.raw.setPlainText("\n\n".join("==== %s ====\n%s" % (n, txt.rstrip()) for n, txt in d.raw))
        self._save_report()

    def _save_report(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, datetime.datetime.now().strftime("diag-%Y%m%d-%H%M%S.txt"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.diag.report_text())
            self.report_path = path
        except OSError:
            self.report_path = ""

    def copy_report(self):
        if not self.diag:
            return
        QApplication.clipboard().setText(self.diag.report_text())
        QMessageBox.information(self, APP_NAME, "诊断报告已复制到剪贴板。\n\n也已保存到：\n%s" %
                                (getattr(self, "report_path", "") or "（保存失败）"))

    # 挂载 -----------------------------------------------------------------

    def mount(self):
        self._run("正在挂载……", B.mount_disc, (self.dev,), self._mounted)

    def _mounted(self, result):
        if isinstance(result, Exception):
            result = (None, str(result))
        path, msg = result
        if path:
            if not B.open_folder(path):
                QMessageBox.information(self, APP_NAME, "已挂载到 %s，但没找到文件管理器。" % path)
        else:
            QMessageBox.warning(self, APP_NAME, "挂载失败：\n\n%s" % msg)
        self.start()

    def open_mounted(self):
        if self.diag and self.diag.mount:
            B.open_folder(self.diag.mount[0])

    def unmount(self):
        self._run("正在卸载……", B.unmount_disc, (self.dev,), self._unmounted)

    def _unmounted(self, result):
        if isinstance(result, Exception):
            result = (False, str(result))
        ok, msg = result
        if not ok:
            QMessageBox.warning(self, APP_NAME, msg)
        self.start()

    def repair(self):
        self.accept()
        self.main.repair(ask=True)

    def recover_names(self):
        self.accept()
        self.main.recover_names(self.dev)

    def reject(self):
        if self.thread and self.thread.isRunning():
            return      # 读盘中途不关，免得线程悬空
        super(DiagDialog, self).reject()


class RecoverDialog(QDialog):
    """恢复长文件名：查找原名 → 预览（可取消勾选）→ 交给主窗口追加一个改名会话。"""

    def __init__(self, main, dev):
        super(RecoverDialog, self).__init__(main)
        self.main = main
        self.dev = dev
        self.plan = None
        self.setWindowTitle("恢复长文件名 - %s" % dev)
        self.resize(900, 520)
        v = QVBoxLayout(self)
        self.lb = QLabel("正在查找……")
        self.lb.setWordWrap(True)
        v.addWidget(self.lb)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["当前名字", "恢复为", "原名来自"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        v.addWidget(self.tree, 1)
        self.lb_unresolved = QLabel()
        self.lb_unresolved.setWordWrap(True)
        self.lb_unresolved.setTextFormat(Qt.PlainText)   # 内容是盘上的文件名
        v.addWidget(self.lb_unresolved)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_write = QPushButton("写入光盘（追加一个会话）")
        self.btn_write.setEnabled(False)
        self.btn_write.clicked.connect(self.write)
        b_cancel = QPushButton("取消")
        b_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_write)
        row.addWidget(b_cancel)
        v.addLayout(row)
        self.thread = FuncThread(B.plan_name_recovery, dev, with_progress=True)
        self.thread.progress.connect(self.lb.setText)
        self.thread.done.connect(self._planned)
        self.thread.start()

    def _planned(self, plan):
        if isinstance(plan, Exception):
            self.lb.setText("<b style='color:#c0392b'>查找失败：%s</b>" % plan)
            return
        self.plan = plan
        for r in plan.renames:
            it = QTreeWidgetItem([r.old, r.new, "起始扇区 %d 的会话" % r.session])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked)
            it.setData(0, Qt.UserRole, r)
            self.tree.addTopLevelItem(it)
        if plan.renames:
            self.lb.setText("查了 %d 个早先的会话，找到 %d 个文件的原名。确认无误后点“写入光盘”。<br>"
                            "只会追加一个新的目录（约几百 KB），文件内容不重写；写入后不能撤销。"
                            % (plan.sessions, len(plan.renames)))
            self.btn_write.setEnabled(True)
        else:
            self.lb.setText("查了 %d 个早先的会话，没有找到可以恢复的原名。" % plan.sessions)
        if plan.unresolved:
            self.lb_unresolved.setText("找不到原名（保持不变）：" + "、".join(plan.unresolved))

    def selected(self):
        return [self.tree.topLevelItem(i).data(0, Qt.UserRole)
                for i in range(self.tree.topLevelItemCount())
                if self.tree.topLevelItem(i).checkState(0) == Qt.Checked]

    def write(self):
        renames = self.selected()
        if not renames:
            return
        if QMessageBox.question(
                self, "确认写入",
                "将向光盘追加一个会话，把 %d 个文件改回原名。\n\n文件内容不会重写，写入后不能撤销。继续？"
                % len(renames)) != QMessageBox.Yes:
            return
        self.accept()
        self.main.apply_renames(self.dev, renames, self.plan.fixups)

    def reject(self):
        if self.thread.isRunning():
            return
        super(RecoverDialog, self).reject()


class DropTree(QTreeWidget):
    """支持从文件管理器拖入文件。"""

    dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super(DropTree, self).__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super(DropTree, self).dragEnterEvent(e)

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.dropped.emit(paths)
            e.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(self):
        super(MainWindow, self).__init__()
        self.setWindowTitle("%s %s" % (APP_NAME, __version__))
        self.setWindowIcon(app_icon())
        self.resize(1000, 700)

        self.media = B.MediaInfo()
        self.disc_entries = []
        self.drives = []
        self.burning = False
        self._outdev_retry = False
        # 已经提示过修复的设备，避免每次刷新都弹窗；换盘/弹出后清空
        self._repair_prompted = ""
        self.job = Job(self)
        self.job.line.connect(self._log)
        self.job.progress.connect(self._on_progress)
        self.job.stage.connect(self._on_stage)
        self._on_finish = None
        self.job.finished.connect(self._job_finished)
        self._last_pct = None
        self._last_stage = ""
        self._last_change = 0.0
        self._stalled = False
        self._stall_timer = QTimer(self)
        self._stall_timer.setInterval(5000)
        self._stall_timer.timeout.connect(self._check_stall)

        self._build_ui()
        self._set_busy(False)
        self.refresh_drives()

    # ------------------------------------------------------------ 界面

    def _build_ui(self):
        root = QWidget()
        v = QVBoxLayout(root)

        # 刻录机
        row = QHBoxLayout()
        row.addWidget(QLabel("刻录机："))
        self.cb_drive = QComboBox()
        self.cb_drive.setMinimumWidth(380)
        self.cb_drive.activated.connect(self._drive_chosen)
        row.addWidget(self.cb_drive, 1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh_media)
        self.btn_eject = QPushButton("弹出")
        self.btn_eject.clicked.connect(self.eject)
        self.btn_blank = QPushButton("擦除可擦写盘…")
        self.btn_blank.clicked.connect(self.blank)
        self.btn_repair = QPushButton("修复未关闭会话…")
        self.btn_repair.setStyleSheet("color:#c0392b;font-weight:bold")
        self.btn_repair.clicked.connect(lambda: self.repair(ask=True))
        self.btn_repair.setVisible(False)
        self.btn_diag = QPushButton("光盘诊断…")
        self.btn_diag.setToolTip("查看光盘类型、开闭状态、会话/轨道、文件系统，挂载读取 UDF 等光盘")
        self.btn_diag.clicked.connect(self.diagnose)
        for b in (self.btn_repair, self.btn_diag, self.btn_refresh, self.btn_eject, self.btn_blank):
            row.addWidget(b)
        v.addLayout(row)

        self.lb_media = QLabel("正在检测刻录机……")
        self.lb_media.setTextFormat(Qt.RichText)
        v.addWidget(self.lb_media)
        self.bar_capacity = QProgressBar()
        self.bar_capacity.setTextVisible(True)
        v.addWidget(self.bar_capacity)

        # 左：光盘现有内容；右：待刻录
        split = QSplitter(Qt.Horizontal)
        g_disc = QGroupBox("光盘现有内容")
        lv = QVBoxLayout(g_disc)
        self.tree_disc = QTreeWidget()
        self.tree_disc.setHeaderLabels(["名称", "大小"])
        self.tree_disc.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree_disc.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree_disc.header().setStretchLastSection(False)
        lv.addWidget(self.tree_disc)
        split.addWidget(g_disc)

        g_new = QGroupBox("待刻录（可直接把文件拖进来）")
        rv = QVBoxLayout(g_new)
        trow = QHBoxLayout()
        trow.addWidget(QLabel("放到光盘目录："))
        self.ed_target = QLineEdit("/")
        self.ed_target.textChanged.connect(self._update_pending)
        trow.addWidget(self.ed_target, 1)
        b_date = QPushButton("按日期建文件夹")
        b_date.clicked.connect(lambda: self.ed_target.setText(
            "/" + datetime.date.today().strftime("%Y-%m-%d")))
        b_root = QPushButton("根目录")
        b_root.clicked.connect(lambda: self.ed_target.setText("/"))
        trow.addWidget(b_date)
        trow.addWidget(b_root)
        rv.addLayout(trow)
        self.tree_new = DropTree()
        self.tree_new.setHeaderLabels(["名称", "大小", "光盘位置", "提示"])
        self.tree_new.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree_new.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree_new.dropped.connect(self.add_paths)
        rv.addWidget(self.tree_new)
        brow = QHBoxLayout()
        self.btn_add_files = QPushButton("添加文件…")
        self.btn_add_files.clicked.connect(self._pick_files)
        self.btn_add_dir = QPushButton("添加文件夹…")
        self.btn_add_dir.clicked.connect(self._pick_dir)
        self.btn_remove = QPushButton("移除所选")
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear = QPushButton("清空")
        self.btn_clear.clicked.connect(self._clear_pending)
        for b in (self.btn_add_files, self.btn_add_dir, self.btn_remove, self.btn_clear):
            brow.addWidget(b)
        brow.addStretch(1)
        rv.addLayout(brow)
        split.addWidget(g_new)
        split.setSizes([420, 580])
        v.addWidget(split, 1)

        # 选项
        g_opt = QGroupBox("刻录选项")
        oh = QHBoxLayout(g_opt)
        form = QFormLayout()
        self.ed_volid = QLineEdit()
        self.ed_volid.setPlaceholderText("光盘名称，Windows 中显示")
        form.addRow("卷标：", self.ed_volid)
        self.cb_speed = QComboBox()
        self.cb_speed.addItem("自动", "")
        self.cb_speed.addItem("最低速（最稳妥，适合归档）", "min")
        self.cb_speed.addItem("最高速", "max")
        form.addRow("速度：", self.cb_speed)
        oh.addLayout(form, 1)
        ov = QVBoxLayout()
        self.chk_open = QCheckBox("保持光盘可追加（不封盘，推荐）")
        self.chk_open.setChecked(True)
        self.chk_open.toggled.connect(self._open_toggled)
        self.chk_verify = QCheckBox("刻录后校验（MD5）")
        self.chk_verify.setChecked(True)
        self.chk_eject = QCheckBox("完成后弹出光盘")
        # 默认弹出：取出光盘前不用再点一次，系统下次放入时也会重新读目录
        self.chk_eject.setChecked(True)
        ov.addWidget(self.chk_open)
        ov.addWidget(self.chk_verify)
        ov.addWidget(self.chk_eject)
        oh.addLayout(ov, 1)
        self.btn_burn = QPushButton("开始刻录")
        self.btn_burn.setMinimumHeight(48)
        self.btn_burn.setMinimumWidth(140)
        f = self.btn_burn.font()
        f.setPointSize(f.pointSize() + 2)
        f.setBold(True)
        self.btn_burn.setFont(f)
        self.btn_burn.clicked.connect(self.burn)
        oh.addWidget(self.btn_burn)
        v.addWidget(g_opt)

        # 进度与日志
        prow = QHBoxLayout()
        self.lb_stage = QLabel("就绪")
        self.lb_stage.setMinimumWidth(200)
        prow.addWidget(self.lb_stage)
        self.bar_progress = QProgressBar()
        self.bar_progress.setRange(0, 1000)
        self.bar_progress.setFormat("%p%")
        prow.addWidget(self.bar_progress, 1)
        self.chk_log = QCheckBox("显示详细日志")
        prow.addWidget(self.chk_log)
        v.addLayout(prow)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setVisible(False)
        self.chk_log.toggled.connect(self.log.setVisible)
        v.addWidget(self.log, 1)

        self.setCentralWidget(root)

    def _set_busy(self, busy, burning=False):
        self.burning = burning
        for w in (self.cb_drive, self.btn_refresh, self.btn_eject, self.btn_burn, self.btn_diag,
                  self.btn_add_files, self.btn_add_dir, self.btn_remove,
                  self.btn_clear, self.ed_volid, self.ed_target, self.cb_speed,
                  self.chk_open, self.chk_verify, self.chk_eject):
            w.setEnabled(not busy)
        self.btn_blank.setEnabled(not busy and self.media.present and self.media.rewritable)
        self.btn_repair.setVisible(self.media.needs_repair)
        self.btn_repair.setEnabled(not busy)
        self.tree_new.setAcceptDrops(not busy)
        if busy:
            QApplication.setOverrideCursor(Qt.BusyCursor)
        else:
            while QApplication.overrideCursor():
                QApplication.restoreOverrideCursor()

    def _log(self, text):
        self.log.appendPlainText(text)

    def _on_progress(self, pct):
        self.bar_progress.setValue(int(pct * 10))
        if pct != self._last_pct:
            self._last_pct = pct
            self._touch()

    def _on_stage(self, text):
        if text != self._last_stage:
            self._last_stage = text
            self._touch()
        elif self._stalled:
            return      # 卡住时 xorriso 仍每秒刷同样的进度行，别把提示冲掉
        self.lb_stage.setText(text)

    def _touch(self):
        self._last_change = time.monotonic()
        self._stalled = False
        self.lb_stage.setStyleSheet("")

    def _check_stall(self):
        if not (self.burning and self.job.running() and self._last_stage == STAGE_WRITING):
            return
        idle = time.monotonic() - self._last_change
        if idle >= STALL_SECONDS:
            self._stalled = True
            self.lb_stage.setStyleSheet("color:#c0392b;font-weight:bold")
            self.lb_stage.setText("写入已 %d 秒没有进展：请检查光驱的连接和供电，"
                                  "不要拔出光驱或关闭程序" % idle)
            self.chk_log.setChecked(True)

    # ------------------------------------------------------------ 任务调度

    def _run(self, args, on_finish, stage, burning=False):
        if self.job.running():
            return
        self._on_finish = on_finish
        self.lb_stage.setText(stage)
        self.bar_progress.setValue(0)
        self._log("$ xorriso " + " ".join(args))
        self._set_busy(True, burning)
        self._last_pct, self._last_stage = None, stage
        self._touch()
        if burning:
            self._stall_timer.start()
        try:
            self.job.start(args)
        except B.XorrisoNotFound as e:
            self._stall_timer.stop()
            self._set_busy(False)
            QMessageBox.critical(self, APP_NAME, str(e))

    def _job_finished(self, code, out):
        self._stall_timer.stop()
        self.lb_stage.setStyleSheet("")
        if self.burning:
            B.refresh_system_view(self.current_dev())
        self._set_busy(False)
        cb, self._on_finish = self._on_finish, None
        if cb:
            cb(code, out)

    def current_dev(self):
        return self.cb_drive.currentData() or ""

    # ------------------------------------------------------------ 刻录机 / 盘片

    def refresh_drives(self):
        self.cb_drive.clear()
        self.lb_media.setText("正在扫描刻录机……")
        self._run(["-devices"], self._drives_done, "正在扫描刻录机……")

    def _drives_done(self, code, out):
        self.drives = B.sort_drives(B.parse_devices(out))
        unknown = B.missing_drives(self.drives)
        if not B.IS_WINDOWS:
            self.drives += [d for d in B.sysfs_drives() if d.dev in unknown]
        self.cb_drive.clear()
        for d in self.drives:
            self.cb_drive.addItem(QIcon.fromTheme("drive-optical"), d.label, d.dev)
        self.cb_drive.addItem(QIcon.fromTheme("media-optical"),
                              "生成 / 追加 ISO 镜像文件…", IMAGE_ITEM)
        self.lb_stage.setText("就绪")
        if self.drives:
            # 上次用的光驱优先（电脑上常有虚拟光驱排在真刻录机前面）
            last = self.cb_drive.findData(QSettings("discburner", "discburner").value("drive", ""))
            self.cb_drive.setCurrentIndex(max(last, 0))
            self.refresh_media()
        elif B.IS_WINDOWS:
            text = "<b>未找到刻录机。</b>请检查光驱连接和供电，然后点“刷新”。"
            if unknown:
                text += ("<br>系统里有光驱 %s，但无法以刻录方式打开（可能是虚拟光驱，"
                         "或正被其他刻录软件占用）。" % "、".join(B.drive_letter(x) + ":" for x in unknown))
            self.lb_media.setText(text)
        else:
            self.lb_media.setText(
                "<b>未找到刻录机。</b>请检查光驱连接；若已连接，确认当前用户有权限访问 "
                "/dev/sr0（<code>sudo usermod -aG cdrom $USER</code> 后重新登录）。")

    def _drive_chosen(self, _index):
        if self.cb_drive.currentData() == IMAGE_ITEM:
            path, _ = QFileDialog.getSaveFileName(
                self, "选择 ISO 镜像文件（已存在则追加）",
                os.path.expanduser("~/光盘.iso"), "ISO 镜像 (*.iso)",
                options=QFileDialog.DontConfirmOverwrite)
            if not path:
                self.cb_drive.setCurrentIndex(0)
                return
            if not path.lower().endswith(".iso"):
                path += ".iso"
            idx = self.cb_drive.findData(path)
            if idx < 0:
                self.cb_drive.insertItem(self.cb_drive.count() - 1,
                                         QIcon.fromTheme("media-optical"),
                                         B.Drive(path).label, path)
                idx = self.cb_drive.findData(path)
            self.cb_drive.setCurrentIndex(idx)
        self.refresh_media()

    def refresh_media(self):
        dev = self.current_dev()
        if not dev or dev == IMAGE_ITEM:
            return
        if B.is_drive(dev):
            QSettings("discburner", "discburner").setValue("drive", dev)
        self.media = B.MediaInfo()
        self.disc_entries = []
        self.tree_disc.clear()
        if not B.is_drive(dev) and not os.path.exists(dev):
            # 新镜像文件：当作空白盘
            self.media.present = self.media.blank = True
            self._show_media()
            return
        B.unmount(dev)
        self.lb_media.setText("正在读取光盘信息……")
        self._outdev_retry = False
        args = B.toc_args(dev) + ["-find", "/", "-exec", "lsdl", "--"]
        self._run(args, self._media_done, "正在读取光盘……")

    def _media_done(self, code, out):
        dev = self.current_dev()
        if B.indev_failed(out) and B.is_drive(dev) and not self._outdev_retry:
            # 盘上目录读不出时 -indev 连光驱都不接管；改用 -outdev 只读光驱和盘片状态
            self._outdev_retry = True
            self._run(B.outdev_toc_args(dev), self._media_done, "正在读取光驱状态……")
            return
        self.media = B.parse_toc(out)
        if self._outdev_retry:
            self.media.tree_unreadable = True
        self.disc_entries = B.parse_lsdl(out) if self.media.has_iso else []
        self.lb_stage.setText("就绪")
        self._fill_disc_tree()
        self._show_media()
        if self.media.has_iso and self.media.volume_id and not self.ed_volid.text():
            self.ed_volid.setText(self.media.volume_id)
        elif self.media.blank and not self.ed_volid.text():
            self.ed_volid.setText("光盘" + datetime.date.today().strftime("%Y-%m-%d"))
        self._update_pending()
        self._set_busy(False)
        if not self.media.present:
            self._repair_prompted = ""
        elif self.media.needs_repair and self._repair_prompted != self.current_dev():
            self._repair_prompted = self.current_dev()
            if QMessageBox.question(
                    self, "发现未关闭的会话",
                    "这张光盘有一个会话没有关闭。\n\n"
                    "通常是 Windows 用“类似 USB 闪存驱动器”方式刻录后，没有正常弹出就取出了光盘。"
                    "这种盘在麒麟和其他电脑上都可能读不出来。\n\n"
                    "现在修复吗？\n"
                    "· 修复只会关闭这个会话，不会删除已有数据\n"
                    "· 修复后光盘仍可继续追加文件\n"
                    "· 如果 Windows 当时是写到一半被打断的，最后那次写入的文件可能找不回来") \
                    == QMessageBox.Yes:
                self.repair(ask=False)

    def _show_media(self):
        m = self.media
        is_image = not B.is_drive(self.current_dev())
        if not m.present:
            self.lb_media.setText("<b>未检测到光盘</b>，请放入光盘后点“刷新”。")
        else:
            color = {"空白盘": "green", "可追加": "green"}.get(m.status_text(), "red")
            if m.needs_repair:
                color = "#d35400"
            parts = ["类型：<b>%s</b>" % ("ISO 镜像文件" if is_image else html.escape(m.profile)),
                     "状态：<b style='color:%s'>%s</b>" % (color, html.escape(m.status_text()))]
            if m.sessions:
                parts.append("已有 %d 次刻录记录" % m.sessions)
            if m.volume_id:
                parts.append("卷标：%s" % html.escape(m.volume_id))
            if not is_image:
                parts.append("已用 %s / 剩余 %s" % (B.human_size(m.used_bytes),
                                                 B.human_size(m.free_bytes)))
            text = "　".join(parts)
            if m.needs_repair:
                text += ("<br><b style='color:#d35400'>此盘有未关闭的会话，所以读不出内容。"
                         "点右上角“修复未关闭会话”即可关闭它，已有数据不受影响。</b>")
            elif m.foreign_data:
                text += ("<br><b style='color:red'>此盘上的数据不是标准 ISO9660 格式"
                         "（可能是 Windows“类似 USB 闪存驱动器”方式刻录的 UDF 盘），"
                         "本软件不能向其追加，否则原有内容会看不到。"
                         "点“光盘诊断”可以查看详情并挂载读取。</b>")
            elif m.tree_unreadable:
                text += ("<br><b style='color:red'>光驱能看到这张盘，但读不出盘上的目录，"
                         "可能是刻录失败或盘片损坏。点“光盘诊断”查看详情。</b>")
            elif m.closed:
                text += "<br><b style='color:red'>此盘已封盘，无法再写入，只能读取。</b>"
            self.lb_media.setText(text)
        self._update_capacity()

    def _fill_disc_tree(self):
        self.tree_disc.clear()
        nodes = {"/": self.tree_disc.invisibleRootItem()}
        for e in sorted(self.disc_entries, key=lambda x: x.path):
            parent_path = os.path.dirname(e.path) or "/"
            parent = nodes.get(parent_path, self.tree_disc.invisibleRootItem())
            item = QTreeWidgetItem(parent, [os.path.basename(e.path),
                                            "" if e.is_dir else B.human_size(e.size)])
            item.setIcon(0, QIcon.fromTheme("folder" if e.is_dir else "text-x-generic"))
            if e.is_dir:
                nodes[e.path] = item
        self.tree_disc.expandToDepth(0)

    # ------------------------------------------------------------ 待刻录列表

    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要刻录的文件",
                                                os.path.expanduser("~"))
        self.add_paths(paths)

    def _pick_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择要刻录的文件夹",
                                                os.path.expanduser("~"))
        if path:
            self.add_paths([path])

    def add_paths(self, paths):
        existing = {self.tree_new.topLevelItem(i).data(0, Qt.UserRole)
                    for i in range(self.tree_new.topLevelItemCount())}
        too_long = []
        for p in paths:
            p = os.path.abspath(p)
            if p in existing or not os.path.exists(p):
                continue
            size, count = B.local_size(p)
            too_long += B.long_names(p)
            item = QTreeWidgetItem([os.path.basename(p) or p, B.human_size(size), "", ""])
            item.setData(0, Qt.UserRole, p)
            item.setData(1, Qt.UserRole, size)
            item.setData(2, Qt.UserRole, count)
            item.setIcon(0, QIcon.fromTheme("folder" if os.path.isdir(p) else "text-x-generic"))
            item.setToolTip(0, p)
            self.tree_new.addTopLevelItem(item)
        self._update_pending()
        if too_long:
            QMessageBox.warning(
                self, APP_NAME,
                "以下名字超过 %d 个字符，Windows 上会被截断：\n\n%s" %
                (B.JOLIET_NAME_MAX, "\n".join(too_long[:10])))

    def _remove_selected(self):
        for item in self.tree_new.selectedItems():
            self.tree_new.takeTopLevelItem(self.tree_new.indexOfTopLevelItem(item))
        self._update_pending()

    def _clear_pending(self):
        self.tree_new.clear()
        self._update_pending()

    def pending_items(self):
        """[(本地路径, 光盘路径, 字节数, 文件数), ...]"""
        target = self.ed_target.text().strip() or "/"
        out = []
        for i in range(self.tree_new.topLevelItemCount()):
            it = self.tree_new.topLevelItem(i)
            src = it.data(0, Qt.UserRole)
            out.append((src, B.disc_join(target, os.path.basename(src)),
                        it.data(1, Qt.UserRole) or 0, it.data(2, Qt.UserRole) or 0))
        return out

    def _update_pending(self):
        on_disc = {B.strip_version(e.path): e for e in self.disc_entries}
        seen = set()
        for i, (src, dst, _s, _c) in enumerate(self.pending_items()):
            it = self.tree_new.topLevelItem(i)
            it.setText(2, dst)
            hint = ""
            if dst in seen:
                hint = "与其他项目重名"
            elif dst in on_disc:
                hint = "合并到已有文件夹" if on_disc[dst].is_dir and os.path.isdir(src) \
                    else "将替换光盘上的同名文件"
            seen.add(dst)
            it.setText(3, hint)
        self._update_capacity()

    def _update_capacity(self):
        items = self.pending_items()
        pending = sum(x[2] for x in items)
        files = sum(x[3] for x in items)
        bar = self.bar_capacity
        is_image = not B.is_drive(self.current_dev())
        if is_image or not self.media.present:
            bar.setRange(0, 1)
            bar.setValue(0)
            bar.setFormat("待刻录 %s（%d 个文件）" % (B.human_size(pending), files))
            return
        total = max(self.media.total_bytes, 1)
        need = pending + (B.estimate_overhead(files) if items else 0)
        used = self.media.used_bytes
        scale = 1024 * 1024
        bar.setRange(0, max(total // scale, 1))
        bar.setValue(min((used + need) // scale, total // scale))
        over = need > self.media.free_bytes
        bar.setFormat("已用 %s + 待刻录 %s / 共 %s%s" % (
            B.human_size(used), B.human_size(need), B.human_size(total),
            "　空间不足！" if over else ""))
        bar.setStyleSheet("QProgressBar::chunk{background:#d9534f}" if over else "")

    def _open_toggled(self, keep_open):
        if not keep_open:
            QMessageBox.warning(
                self, APP_NAME,
                "取消此项后，本次刻录会“封盘”：\n\n"
                "光盘以后只能读取，不能再追加任何文件。\n"
                "一般只有要发给老旧设备（如某些 CD 机）读取时才需要封盘。")

    # ------------------------------------------------------------ 刻录

    def burn(self):
        dev = self.current_dev()
        m = self.media
        items = self.pending_items()
        if not dev or dev == IMAGE_ITEM:
            return QMessageBox.warning(self, APP_NAME, "请先选择刻录机。")
        if not items:
            return QMessageBox.information(self, APP_NAME, "请先添加要刻录的文件。")
        if not m.present:
            return QMessageBox.warning(self, APP_NAME, "未检测到光盘，请放入光盘后点“刷新”。")
        if m.needs_repair:
            return QMessageBox.warning(
                self, APP_NAME, "此盘有未关闭的会话，请先点“修复未关闭会话”。")
        if m.closed:
            return QMessageBox.warning(self, APP_NAME, "此盘已封盘，无法再写入。请换一张光盘。")
        if m.foreign_data:
            return QMessageBox.warning(
                self, APP_NAME, "此盘上的数据不是 ISO9660 格式，追加后原内容会不可见，已停止。")
        if not m.can_write:
            return QMessageBox.warning(self, APP_NAME, "此盘当前不可写入（%s）。" % m.status_text())
        missing = [s for s, _d, _z, _c in items if not os.path.exists(s)]
        if missing:
            return QMessageBox.warning(self, APP_NAME, "以下文件已不存在：\n" + "\n".join(missing))
        dups = [it.text(2) for it in self._items() if it.text(3) == "与其他项目重名"]
        if dups:
            return QMessageBox.warning(self, APP_NAME, "光盘上会出现重名：\n" + "\n".join(dups))
        volid = self.ed_volid.text().strip()
        err = B.check_volid(volid)
        if err:
            return QMessageBox.warning(self, APP_NAME, err)

        pending = sum(x[2] for x in items)
        need = pending + B.estimate_overhead(sum(x[3] for x in items))
        if B.is_drive(dev) and need > m.free_bytes:
            return QMessageBox.warning(
                self, APP_NAME, "空间不足：需要约 %s，光盘剩余 %s。" %
                (B.human_size(need), B.human_size(m.free_bytes)))

        replaced = [it.text(2) for it in self._items() if it.text(3) == "将替换光盘上的同名文件"]
        keep_open = self.chk_open.isChecked()
        lines = ["刻录 %d 项，共 %s" % (len(items), B.human_size(pending)),
                 "目标：%s" % self.cb_drive.currentText(),
                 "卷标：%s" % (volid or "（不设置）"),
                 "刻录后：%s" % ("保持可追加" if keep_open else "封盘（以后不能再写入！）")]
        if replaced:
            lines.append("\n以下光盘上的旧文件会被新文件替换（旧文件仍占用空间）：\n" +
                         "\n".join(replaced[:10]))
        fixups = B.version_fixups([e.path for e in self.disc_entries if not e.is_dir])
        if fixups:
            lines.append("\n同时修正 %d 个带“;1”的旧文件名（系统 xorriso 版本较老，不修正的话 Windows 上会变成 xxx_1）。"
                         % len(fixups))
        shorts = [e.path for e in self.disc_entries
                  if not e.is_dir and B.is_short_name(os.path.basename(e.path))]
        if shorts:
            lines.append("\n注意：盘上有 %d 个文件只剩短文件名（如 %s）。建议先取消，用“光盘诊断 → "
                         "恢复长文件名”找回原名；否则本次追加后它们仍是短名。"
                         % (len(shorts), os.path.basename(shorts[0])))
        if QMessageBox.question(self, "确认刻录", "\n".join(lines)) != QMessageBox.Yes:
            return

        B.unmount(dev)
        args = B.burn_args(
            dev, [(s, d) for s, d, _z, _c in items], volid=volid,
            close=not keep_open, speed=self.cb_speed.currentData(),
            verify=self.chk_verify.isChecked(), eject=self._eject_after(dev),
            fixups=fixups)
        self.log.clear()
        self._run(args, self._burn_done, "正在准备刻录……", burning=True)

    def _eject_after(self, dev):
        """镜像文件没有可弹出的东西，只对光驱生效。"""
        return self.chk_eject.isChecked() and B.is_drive(dev)

    def _items(self):
        return [self.tree_new.topLevelItem(i) for i in range(self.tree_new.topLevelItemCount())]

    def _burn_done(self, code, out):
        log_path = self._save_log(out)
        if code == 0 and not B.md5_mismatch(out):
            self.bar_progress.setValue(1000)
            self.lb_stage.setText("刻录完成")
            self.tree_new.clear()
            msg = "刻录完成。"
            if self.chk_verify.isChecked():
                msg += "\n校验通过，数据与原文件一致。"
            if self.chk_open.isChecked():
                msg += "\n光盘保持可追加，下次可以继续往里加文件。"
            QMessageBox.information(self, APP_NAME, msg)
        else:
            self.lb_stage.setText("刻录失败")
            self.chk_log.setChecked(True)
            reason = "校验不一致，光盘数据可能有误。" if B.md5_mismatch(out) else \
                "xorriso 返回错误（代码 %d）。" % code
            QMessageBox.critical(self, APP_NAME, "刻录失败：%s\n\n详细日志：%s" % (reason, log_path))
        if not self._eject_after(self.current_dev()):
            self.refresh_media()
        else:
            self.media = B.MediaInfo()
            self.tree_disc.clear()
            self._show_media()

    def _save_log(self, out):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, datetime.datetime.now().strftime("%Y%m%d-%H%M%S.log"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log.toPlainText() + "\n")
            return path
        except OSError:
            return "（保存失败）"

    # ------------------------------------------------------------ 擦除 / 弹出

    def blank(self):
        dev = self.current_dev()
        if not self.media.rewritable:
            return
        if QMessageBox.warning(
                self, "确认擦除",
                "将擦除光盘上的全部数据，且无法恢复。\n\n继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        B.unmount(dev)
        self._run(B.blank_args(dev), self._blank_done, "正在擦除……", burning=True)

    def _blank_done(self, code, out):
        if code == 0:
            QMessageBox.information(self, APP_NAME, "擦除完成。")
        else:
            self.chk_log.setChecked(True)
            QMessageBox.critical(self, APP_NAME, "擦除失败（代码 %d），详见日志。" % code)
        self.refresh_media()

    def diagnose(self):
        dev = self.current_dev()
        if not dev or dev == IMAGE_ITEM:
            return QMessageBox.information(self, APP_NAME, "请先选择刻录机。")
        if not B.is_drive(dev) and not os.path.exists(dev):
            return QMessageBox.information(self, APP_NAME, "镜像文件还不存在。")
        dlg = DiagDialog(self, dev)
        dlg.exec_()
        return dlg

    def recover_names(self, dev):
        dlg = RecoverDialog(self, dev)
        dlg.exec_()
        return dlg

    def apply_renames(self, dev, renames, fixups=()):
        B.unmount(dev)
        self.log.clear()
        self._renames = list(renames) + list(fixups)
        self._run(B.rename_args(dev, renames, fixups=fixups, speed=self.cb_speed.currentData()),
                  self._renames_done,
                  "正在写入改名会话……", burning=True)

    def _renames_done(self, code, out):
        log_path = self._save_log(out)
        renames, self._renames = self._renames, []
        if code != 0:
            self.chk_log.setChecked(True)
            QMessageBox.critical(self, APP_NAME, "写入失败（代码 %d）。\n\n详细日志：%s" % (code, log_path))
            self.refresh_media()
            return
        # 读回核对：新名字都在、旧名字都没了
        dev = self.current_dev()
        try:
            names = {e.path for e in B.list_disc_files(dev)}
        except Exception as e:  # 光驱不响应等；写入本身已成功
            QMessageBox.warning(self, APP_NAME, "写入完成，但读回核对时出错：%s\n\n"
                                "请弹出光盘再放入，用“光盘诊断”确认文件名。" % e)
            self.refresh_media()
            return
        bad = [r for r in renames if r.new not in names or r.old in names]
        self.bar_progress.setValue(1000)
        if bad:
            self.lb_stage.setText("改名后核对有问题")
            QMessageBox.warning(self, APP_NAME, "写入完成，但读回核对时以下文件不对：\n" +
                                "\n".join("%s → %s" % (r.old, r.new) for r in bad))
        else:
            self.lb_stage.setText("长文件名已恢复")
            n = sum(1 for r in renames if r.session >= 0)
            QMessageBox.information(self, APP_NAME, "已恢复 %d 个文件的长文件名，读回核对无误。\n\n"
                                    "Windows 和麒麟上都会显示原来的中文名。" % n)
        self.refresh_media()

    def repair(self, ask=True):
        dev = self.current_dev()
        if not B.is_drive(dev):
            return
        if ask and QMessageBox.question(
                self, "修复未关闭会话",
                "将关闭光盘上未关闭的会话，已有数据不会被删除，光盘仍可继续追加。\n\n继续？") \
                != QMessageBox.Yes:
            return
        B.unmount(dev)
        self.log.clear()
        # 光驱没标“损坏”、但轨道明显没写完时，as_needed 会什么都不做，要强制关闭
        force = self.media.unfinished and not self.media.damaged
        self._run(B.repair_args(dev, force=force), self._repair_done, "正在关闭会话……", burning=True)

    def _repair_done(self, code, out):
        self._save_log(out)
        result = B.repair_result(code, out)
        if result == B.REPAIR_OK:
            self.lb_stage.setText("修复完成")
            QMessageBox.information(
                self, APP_NAME,
                "会话已关闭。\n\n请弹出光盘再重新放入，系统就能正常打开它了。")
        elif result == B.REPAIR_NOT_NEEDED:
            self.lb_stage.setText("无需修复")
            QMessageBox.information(
                self, APP_NAME,
                "光驱没有报告未关闭的轨道，没有做任何改动。\n\n"
                "如果光盘仍然读不出来，问题不在会话上，请打开“显示详细日志”查看光盘信息。")
        elif result == B.REPAIR_UNSUPPORTED:
            self.lb_stage.setText("无法修复")
            QMessageBox.warning(self, APP_NAME, "这种光盘类型不支持关闭会话。")
        else:
            self.lb_stage.setText("修复失败")
            self.chk_log.setChecked(True)
            QMessageBox.critical(self, APP_NAME, "修复失败（代码 %d），详见日志。" % code)
        self.refresh_media()

    def eject(self):
        dev = self.current_dev()
        if not B.is_drive(dev):
            return
        B.unmount(dev)
        self._run(B.eject_args(dev), lambda c, o: self._ejected(), "正在弹出……")

    def _ejected(self):
        self._repair_prompted = ""
        self.media = B.MediaInfo()
        self.tree_disc.clear()
        self.lb_stage.setText("就绪")
        self._show_media()

    def closeEvent(self, e):
        if self.burning and self.job.running():
            QMessageBox.warning(self, APP_NAME, "正在写入光盘，请等待完成，中途关闭会损坏光盘。")
            e.ignore()
            return
        if self.job.running():
            self.job.proc.kill()
            self.job.proc.waitForFinished(3000)
        e.accept()


def app_icon():
    icon = QIcon.fromTheme("discburner", QIcon.fromTheme("media-optical-burn"))
    if not icon.isNull():
        return icon
    # Windows 没有图标主题，用随程序带的图标
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(getattr(sys, "_MEIPASS", here), "discburner.svg"),
                 os.path.join(os.path.dirname(here), "packaging", "discburner.svg")):
        if os.path.exists(path):
            return QIcon(path)
    return icon


def setup_app(app):
    app.setApplicationName(APP_NAME)
    if B.IS_WINDOWS:
        # Qt 在中文 Windows 上默认用宋体，换成系统界面用的雅黑。9 号雅黑笔画太细，用 10 号
        for family in ("Microsoft YaHei UI", "Microsoft YaHei"):
            if family in QFontDatabase().families():
                app.setFont(QFont(family, 10))
                break
        app.setWindowIcon(app_icon())


def qt_scaling_follows_system(qt_version=None, windows=B.IS_WINDOWS):
    """Qt 自己的高分屏缩放（AA_EnableHighDpiScaling）是否和系统设置的缩放一致。

    Windows 上按系统缩放比例算，一致。X11 上 Qt 5.12 以前（麒麟 2020 版是 5.6）
    按显示器物理 DPI 取整成 2、3 倍，和桌面设的缩放无关：4K 屏或小尺寸高分屏上
    界面比系统和其他程序大一截。麒麟 SP1 的 Qt 5.12 按 Xft.dpi（即系统缩放）计算，
    125%、150% 都是实际倍数。不启用时 Qt 按 Xft.dpi 放大字体，和 GTK 程序一样。
    """
    if windows:
        return True
    v = tuple(int(x) for x in (qt_version or qVersion()).split(".")[:2])
    return v >= (5, 12)


def main():
    if B.IS_WINDOWS and "QT_QPA_PLATFORM" not in os.environ:
        # Qt 在 Windows 上默认用 GDI 画字，中文笔画又细又淡，看着发虚；
        # FreeType 会加深笔画，中文清楚得多。设了 QT_QPA_PLATFORM 时以设置的为准
        os.environ["QT_QPA_PLATFORM"] = "windows:fontengine=freetype"
    if hasattr(Qt, "AA_EnableHighDpiScaling") and qt_scaling_follows_system():   # Qt 5.6+
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        if hasattr(QApplication, "setHighDpiScaleFactorRoundingPolicy"):   # Qt 5.14+
            # 常用 125%、150% 缩放。Qt 5 默认把缩放倍数四舍五入成 1 或 2 倍，
            # 界面比系统的大或小一截；按实际倍数缩放才和其他程序一致
            QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    setup_app(app)
    # Qt 5.7 才有；麒麟 V10 2020 版是 Qt 5.6.1，没有这个接口
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName("discburner")
    try:
        B.find_xorriso()
    except B.XorrisoNotFound as e:
        QMessageBox.critical(None, APP_NAME, str(e))
        return 1
    w = MainWindow()
    w.show()
    shot = os.environ.get("DISCBURNER_SCREENSHOT")
    if shot:
        # 测试用：启动后截图并退出，用于核对各运行环境下的界面
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(4000, lambda: (w.grab().save(shot), app.quit()))
    return app.exec_()
