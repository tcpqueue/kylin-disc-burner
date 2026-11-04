# -*- coding: utf-8 -*-
"""光盘刻录助手 图形界面（PyQt5，麒麟 V10 自带 python3-pyqt5）。"""

import datetime
import getpass
import html
import os
import shutil
import sys
import tempfile
import time

from PyQt5.QtCore import (QMimeData, QObject, QProcess, QSettings, Qt, QThread, QTimer, QUrl,
                          pyqtSignal, qVersion)
from PyQt5.QtGui import QDrag, QFont, QFontDatabase, QIcon
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
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
# 往 UDF 盘追加前，先把 UDF 里的文件读到这里（不放 /tmp：有的系统 /tmp 在内存里）
STAGE_DIR = os.path.join(os.path.dirname(LOG_DIR), "stage") if B.IS_WINDOWS else \
    os.path.expanduser("~/.cache/discburner/stage")
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
        self.mount_changed = False   # 挂载或卸载过，关掉后主界面要重新读一遍
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
        self.btn_mount.setText("挂载 UDF 部分并打开" if d and d.udf_start is not None else "挂载并打开")
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
        udf = self.diag.udf_start if self.diag else None
        text = "正在挂载 UDF 部分……（会弹出输入管理员密码的窗口）" if udf is not None else "正在挂载……"
        self._run(text, B.mount_disc, (self.dev, udf), self._mounted)

    def _mounted(self, result):
        self.mount_changed = True
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
        self.mount_changed = True
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


class DiscTree(QTreeWidget):
    """光盘现有内容。选中的文件可以拖到文件管理器或桌面，相当于复制出来。"""

    def __init__(self, provider, parent=None):
        super(DiscTree, self).__init__(parent)
        self.provider = provider   # 盘上路径列表 → 本机路径列表（或 None）
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)

    def selected_paths(self):
        return B.top_level([it.data(0, Qt.UserRole) for it in self.selectedItems()
                            if it.data(0, Qt.UserRole)])

    @staticmethod
    def mime_for(paths):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        return mime

    def startDrag(self, _actions):
        paths = self.provider(self.selected_paths())
        if not paths:
            return
        drag = QDrag(self)
        drag.setMimeData(self.mime_for(paths))
        drag.exec_(Qt.CopyAction, Qt.CopyAction)


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
        self._remount = ""   # 写入前被卸载、写完要挂回去的设备
        self._copied = []    # “复制到…”的目标路径，复制完核对用
        self._tree_sbsector = None   # 左边列的不是最后一个会话时，那个 ISO 会话的起始扇区
        self._stage = None           # 往 UDF 盘追加时读出 UDF 文件的临时目录
        self._pending_burn = None
        self._hidden = []            # “从光盘删除”的项目，写完核对用
        self._udf_paths, self._iso_paths = set(), set()   # 混合盘上各项来自哪一部分
        self._fs_prompted = ""       # 已经问过要不要读出全部文件的设备
        self._after_read_all = False  # 刚挂上 UDF 部分，刷新完接着问要不要修复
        self._converting = False     # 正在“修复光盘文件系统”（没有新文件的追加）
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
        self.btn_fsfix = QPushButton("修复光盘文件系统…")
        self.btn_fsfix.setStyleSheet("color:#d35400;font-weight:bold")
        self.btn_fsfix.setToolTip("盘上有 Windows 写的 UDF 文件时，把全部文件统一刻成 ISO9660，"
                                  "Windows 和麒麟都能直接读，以后也能一直追加")
        self.btn_fsfix.clicked.connect(lambda: self.convert_to_iso(ask=True))
        self.btn_fsfix.setVisible(False)
        self.btn_diag = QPushButton("光盘诊断…")
        self.btn_diag.setToolTip("查看光盘类型、开闭状态、会话/轨道、文件系统，挂载读取 UDF 等光盘")
        self.btn_diag.clicked.connect(self.diagnose)
        for b in (self.btn_repair, self.btn_fsfix, self.btn_diag, self.btn_refresh, self.btn_eject,
                  self.btn_blank):
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
        g_disc = QGroupBox("光盘现有内容（可拖出复制）")
        lv = QVBoxLayout(g_disc)
        self.tree_disc = DiscTree(self._drag_paths)
        self.tree_disc.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree_disc.customContextMenuRequested.connect(self._disc_menu)
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
        self.btn_fsfix.setVisible(self._can_fix_fs())
        self.btn_fsfix.setEnabled(not busy)
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
            if self._remount:
                B.mount_disc(self._remount)
                self._remount = ""
            self._set_busy(False)
            QMessageBox.critical(self, APP_NAME, str(e))

    def _job_finished(self, code, out):
        self._stall_timer.stop()
        self.lb_stage.setStyleSheet("")
        if self.burning:
            B.refresh_system_view(self.current_dev())
        if self._remount:
            dev, self._remount = self._remount, ""
            B.mount_disc(dev)
        self._set_busy(False)
        cb, self._on_finish = self._on_finish, None
        if cb:
            cb(code, out)

    def current_dev(self):
        return self.cb_drive.currentData() or ""

    def _release_drive(self, dev, remount=True):
        """写入前把光盘从系统卸下（挂载着不能独占光驱）。原来挂载着的，写完再挂回去，
        文件管理器里照常能看到；写完要弹出的就不用了。"""
        self._remount = dev if remount and not B.IS_WINDOWS and B.is_mounted(dev) else ""
        B.unmount(dev)

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
        self._tree_sbsector = None
        self._udf_paths, self._iso_paths = set(), set()
        if self.media.foreign_data:
            # 最后一个会话不是 ISO，xorriso 读到的是空目录。UDF 部分挂着就列挂载目录；
            # 前面还有 ISO 会话的，再用 xorriso 列出那个会话，两部分合在一起显示
            udf = self._udf_source(dev)
            self.disc_entries = B.walk_mount(udf) if udf else []
            self._udf_paths = {e.path for e in self.disc_entries}
            if self.media.iso_hidden:
                self._tree_sbsector = self.media.last_iso_start
                self._run(B.list_files_args(dev, self._tree_sbsector), self._iso_part_listed,
                          "正在读取光盘的 ISO 部分……")
        self._media_shown()

    def _iso_part_listed(self, code, out):
        iso = B.parse_lsdl(out)
        self._iso_paths = {e.path for e in iso}
        self.disc_entries = B.merge_entries(iso, self.disc_entries)
        self._media_shown()

    def _media_shown(self):
        if self.job.running():
            return
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
            self._fs_prompted = ""
        elif self.media.foreign_data and not self.media.needs_repair:
            # 混合格式 / UDF 盘：不等用户去点“光盘诊断”，自动问要不要读出全部文件、修复
            if self._after_read_all:
                self._after_read_all = False
                QTimer.singleShot(0, self._offer_fs_fix)
            elif self._fs_prompted != self.current_dev():
                self._fs_prompted = self.current_dev()
                QTimer.singleShot(0, self._offer_read_all)
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
            if m.foreign_data and m.appendable:
                color = "#d35400"
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
            elif m.iso_hidden:
                udf = self._tree_sbsector is None
                text += ("<br><b style='color:#d35400'>这张盘后来又被别的软件（一般是 Windows"
                         "“类似 USB 闪存驱动器”方式）用 UDF 格式接着写过。%s"
                         "追加时会把 UDF 里的文件一起刻进新的 ISO9660 会话。</b>"
                         % ("左边列出的是 UDF 部分的文件。" if udf else
                            "左边只列出了前面 ISO 部分的文件；后来写的文件在 UDF 部分，"
                            "点“光盘诊断 → 挂载 UDF 部分并打开”读取。"))
            elif m.foreign_data and self._udf_source(self.current_dev()):
                text += ("<br><b style='color:#d35400'>这是 UDF 格式的盘（一般是 Windows"
                         "“类似 USB 闪存驱动器”方式刻录的）。追加时会把盘上的文件"
                         "一起刻进新的 ISO9660 会话。</b>")
            elif m.foreign_data:
                text += ("<br><b style='color:red'>此盘上的数据不是标准 ISO9660 格式"
                         "（可能是 Windows“类似 USB 闪存驱动器”方式刻录的 UDF 盘）。"
                         "点“光盘诊断”查看详情并挂载读取；是 UDF 的话，挂上以后也能追加。</b>")
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
            item.setData(0, Qt.UserRole, e.path)
            if e.is_dir:
                nodes[e.path] = item
        self.tree_disc.expandToDepth(0)

    # ------------------------------------------------------------ 从光盘复制出来

    def _drag_paths(self, disc_paths):
        """拖出时交给文件管理器的本机路径。光盘挂载着就直接用挂载目录里的文件；
        否则（镜像文件、没挂载上）先取到临时目录。"""
        dev = self.current_dev()
        if not disc_paths or not dev or dev == IMAGE_ITEM:
            return None
        if self.burning:
            self.lb_stage.setText("正在写入光盘，请等写完再复制")
            return None
        if self.media.foreign_data:
            iso, local = self._split_sources(dev, disc_paths)
            if not iso:
                return local        # 全是 UDF 部分的，直接用挂载目录
        else:
            local = B.local_paths(dev, disc_paths)
            if local is None and B.is_drive(dev) and not B.IS_WINDOWS and not B.is_mounted(dev):
                B.mount_disc(dev)   # 和放入光盘时系统自动挂载一样
                local = B.local_paths(dev, disc_paths)
            if local is not None:
                return local
            iso, local = disc_paths, []
        dest = self._drag_dir()
        QApplication.setOverrideCursor(Qt.BusyCursor)
        self.lb_stage.setText("正在从光盘取出文件……")
        QApplication.processEvents()
        try:
            code, out = B.copy_out(dev, iso, self._tree_sbsector, local, dest)
        finally:
            QApplication.restoreOverrideCursor()
        paths = [os.path.join(dest, B.disc_basename(p)) for p in disc_paths]
        paths = [p for p in paths if os.path.exists(p)]
        B.make_writable(paths)
        self.lb_stage.setText("就绪")
        if code != 0 or not paths:
            self._log(out)
            QMessageBox.warning(self, APP_NAME, "从光盘取出文件失败（代码 %d），详见日志。" % code)
            return None
        return paths

    def _split_sources(self, dev, paths):
        """混合盘上选中的项目分成两份：ISO 部分的盘上路径、UDF 部分在挂载目录里的路径。
        两部分都有的同名文件夹两边都取，复制时合并。"""
        iso = [p for p in paths if p in self._iso_paths]
        udf = [p for p in paths if p in self._udf_paths]
        return iso, (B.local_paths(dev, udf) or []) if udf else []

    def _drag_dir(self):
        """拖出用的临时目录。文件管理器复制完才算用完，所以不马上删，下次拖出时清掉一小时前的。"""
        base = os.path.join(tempfile.gettempdir(), "discburner-drag-%s" % getpass.getuser())
        now = time.time()
        if os.path.isdir(base):
            for name in os.listdir(base):
                p = os.path.join(base, name)
                try:
                    if now - os.path.getmtime(p) > 3600:
                        shutil.rmtree(p, ignore_errors=True)
                except OSError:
                    pass
        os.makedirs(base, exist_ok=True)
        return tempfile.mkdtemp(dir=base)

    def _disc_menu(self, pos):
        if not self.tree_disc.selected_paths():
            return
        menu = QMenu(self)
        menu.addAction("复制到…", self.copy_out)
        act = menu.addAction("从光盘删除（隐藏）…", self.hide_selected)
        act.setEnabled(self._can_hide())
        menu.exec_(self.tree_disc.viewport().mapToGlobal(pos))

    def _can_hide(self):
        m = self.media
        return (m.has_iso and m.appendable and not m.foreign_data and not m.needs_repair
                and not self.job.running())

    def hide_selected(self):
        """一次性光盘上的“删除”：追加一个不含这些文件的新会话。"""
        dev = self.current_dev()
        paths = self.tree_disc.selected_paths()
        if not paths or not self._can_hide():
            return
        inside = [e for e in self.disc_entries if not e.is_dir and any(
            e.path == p or e.path.startswith(p + "/") for p in paths)]
        overhead = B.estimate_overhead(len(self.disc_entries))
        if B.is_drive(dev) and overhead > self.media.free_bytes:
            return QMessageBox.warning(self, APP_NAME, "光盘剩余空间不够再写一个会话。")
        text = ("将从光盘上删除 %d 项（共 %d 个文件，%s）：\n%s%s\n\n"
                "一次性光盘写过的地方不能改，“删除”是追加一个不含这些文件的新会话：\n"
                "· 以后 Windows 和麒麟上都看不到它们\n"
                "· 空间不会释放，剩余空间反而会少约 %s\n"
                "· 早先的会话里还留着，不适合用来删除需要保密的文件\n\n继续？"
                % (len(paths), len(inside), B.human_size(sum(e.size for e in inside)),
                   "\n".join(paths[:10]), "\n……" if len(paths) > 10 else "",
                   B.human_size(overhead)))
        if QMessageBox.question(self, "从光盘删除", text) != QMessageBox.Yes:
            return
        self._hidden = paths
        self._release_drive(dev)
        self.log.clear()
        self._run(B.hide_args(dev, paths, speed=self.cb_speed.currentData()), self._hide_done,
                  "正在写入删除记录……", burning=True)

    def _hide_done(self, code, out):
        log_path = self._save_log(out)
        hidden, self._hidden = self._hidden, []
        if code != 0:
            self.chk_log.setChecked(True)
            QMessageBox.critical(self, APP_NAME, "写入失败（代码 %d）。\n\n详细日志：%s" % (code, log_path))
            return self.refresh_media()
        try:
            left = {e.path for e in B.list_disc_files(self.current_dev())} & set(hidden)
        except Exception as e:  # 光驱不响应等；写入本身已成功
            left, err = None, e
        self.bar_progress.setValue(1000)
        if left is None:
            QMessageBox.warning(self, APP_NAME, "写入完成，但读回核对时出错：%s" % err)
        elif left:
            QMessageBox.warning(self, APP_NAME, "写入完成，但以下项目还在：\n" + "\n".join(sorted(left)))
        else:
            self.lb_stage.setText("已从光盘删除")
            QMessageBox.information(self, APP_NAME, "已删除 %d 项，读回核对无误。" % len(hidden))
        self.refresh_media()

    def copy_out(self, dest=None):
        dev = self.current_dev()
        paths = self.tree_disc.selected_paths()
        copying = getattr(self, "_copy_thread", None)
        if not paths or self.job.running() or (copying and copying.isRunning()):
            return
        if dest is None:
            dest = QFileDialog.getExistingDirectory(self, "复制到", os.path.expanduser("~"))
            if not dest:
                return
        clash = [B.disc_basename(p) for p in paths
                 if os.path.exists(os.path.join(dest, B.disc_basename(p)))]
        if clash and QMessageBox.question(
                self, APP_NAME, "目标文件夹里已经有同名的：\n%s\n\n覆盖吗？"
                % "\n".join(clash[:10])) != QMessageBox.Yes:
            return
        self._copied = [os.path.join(dest, B.disc_basename(p)) for p in paths]
        if self.media.foreign_data:
            # 混合盘：ISO 部分的用 xorriso 取，UDF 部分的从挂载目录复制
            iso, local = self._split_sources(dev, paths)
            job = (B.copy_out, dev, iso, self._tree_sbsector, local, dest)
        else:
            local = B.local_paths(dev, paths)
            job = (B.copy_local, local, dest) if local is not None else None
        if job:
            self._set_busy(True)
            self.lb_stage.setText("正在从光盘复制……")
            self._copy_thread = FuncThread(*job)
            self._copy_thread.done.connect(self._copy_local_done)
            self._copy_thread.start()
            return
        self._run(B.extract_args(dev, paths, dest, self._tree_sbsector), self._copy_done,
                  "正在从光盘复制……")

    def _copy_local_done(self, result):
        code, msg = (1, str(result)) if isinstance(result, Exception) else result
        self._set_busy(False)
        self._log(msg)
        self._copy_done(code, msg)

    def _copy_done(self, code, out):
        copied = [p for p in self._copied if os.path.exists(p)]
        B.make_writable(copied)
        if code == 0 and len(copied) == len(self._copied):
            self.lb_stage.setText("复制完成")
            if B.open_folder(os.path.dirname(copied[0])) is False:
                QMessageBox.information(self, APP_NAME, "已复制到 %s" % os.path.dirname(copied[0]))
        else:
            self.lb_stage.setText("复制失败")
            self.chk_log.setChecked(True)
            QMessageBox.critical(self, APP_NAME, "复制失败（代码 %d），详见日志。" % code)

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
        udf_src = None
        if m.foreign_data:
            # 盘上有 UDF：只能追加 ISO9660 会话，得先把 UDF 里的文件读出来一起刻进去
            udf_src = self._udf_source(dev)
            if udf_src is None:
                return self._offer_udf_mount(dev)
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
        udf_n = udf_bytes = 0
        if udf_src:
            plan = self._udf_plan(udf_src, extra=need)
            if plan is None:
                return
            udf_n, udf_bytes = plan
        elif B.is_drive(dev) and need > m.free_bytes:
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
        fixups, shorts = [], []
        if udf_src:
            # 左边列的是 UDF 里的文件，不是要导入的 ISO 目录，“;1”和短名的检查都不适用
            lines.append("\n这张盘上有 UDF 格式的文件（一般是 Windows 写的）。本软件只能追加 ISO9660 会话："
                         "会先把 UDF 里现有的 %d 个文件（%s）读到本机临时目录，再和这次的文件一起刻进新会话%s。"
                         "\nUDF 里的文件要再占 %s；刻完后 Windows 和麒麟上都能看到全部文件，"
                         "以后这张盘也能一直用本软件追加。"
                         % (udf_n, B.human_size(udf_bytes),
                            "，前面 ISO 部分的文件也都保留" if m.iso_hidden else "",
                            B.human_size(udf_bytes)))
        else:
            fixups = B.version_fixups([e.path for e in self.disc_entries if not e.is_dir])
            shorts = [e.path for e in self.disc_entries
                      if not e.is_dir and B.is_short_name(os.path.basename(e.path))]
        if fixups:
            lines.append("\n同时修正 %d 个带“;1”的旧文件名（系统 xorriso 版本较老，不修正的话 Windows 上会变成 xxx_1）。"
                         % len(fixups))
        if shorts:
            lines.append("\n注意：盘上有 %d 个文件只剩短文件名（如 %s）。建议先取消，用“光盘诊断 → "
                         "恢复长文件名”找回原名；否则本次追加后它们仍是短名。"
                         % (len(shorts), os.path.basename(shorts[0])))
        if QMessageBox.question(self, "确认刻录", "\n".join(lines)) != QMessageBox.Yes:
            return

        burn = dict(dev=dev, items=[(s, d) for s, d, _z, _c in items], volid=volid,
                    close=not keep_open, fixups=fixups,
                    sbsector=m.last_iso_start if m.iso_hidden else None)
        if udf_src:
            self._stage_then_burn(udf_src, burn)
        else:
            self._start_burn(**burn)

    def _start_burn(self, dev, items, volid, close, fixups=(), sbsector=None, normalize=()):
        self._release_drive(dev, remount=not self._eject_after(dev))
        args = B.burn_args(
            dev, items, volid=volid, close=close, speed=self.cb_speed.currentData(),
            verify=self.chk_verify.isChecked(), eject=self._eject_after(dev),
            fixups=fixups, sbsector=sbsector, normalize=normalize)
        self.log.clear()
        self._run(args, self._burn_done, "正在准备刻录……", burning=True)

    # UDF 盘的追加：挂载 UDF 部分 → 读到本机临时目录 → 和新文件一起刻成 ISO 会话

    def _udf_source(self, dev):
        m = B.mount_info(dev) if B.is_drive(dev) else None
        return m[0] if m and m[1].lower() == "udf" else None

    def _udf_start(self, dev):
        return B.find_udf_session(dev, B.parse_tracks(self.media.raw)) if B.is_drive(dev) else None

    def _offer_udf_mount(self, dev, then=None, why="要往里追加，得先挂载 UDF 部分，把这些文件一起刻进新会话"):
        start = self._udf_start(dev)
        if start is None:
            return QMessageBox.warning(
                self, APP_NAME, "此盘上的数据不是 ISO9660 格式，也没认出是 UDF，追加后原内容会不可见，已停止。")
        if QMessageBox.question(
                self, APP_NAME, "这张盘上有 UDF 格式的文件（一般是 Windows 写的）。%s%s。\n\n现在挂载吗？"
                % (why, "（会弹出输入管理员密码的窗口）" if start else "")) != QMessageBox.Yes:
            return
        self._mount_udf(dev, start, then or self.burn)

    def _mount_udf(self, dev, start, then):
        self._set_busy(True)
        self.lb_stage.setText("正在挂载 UDF 部分……")
        self._mount_then = then
        self._mount_thread = FuncThread(B.mount_disc, dev, start)
        self._mount_thread.done.connect(self._udf_mounted)
        self._mount_thread.start()

    def _udf_mounted(self, result):
        self._set_busy(False)
        self.lb_stage.setText("就绪")
        path, msg = (None, str(result)) if isinstance(result, Exception) else result
        if not path or self._udf_source(self.current_dev()) is None:
            return QMessageBox.warning(self, APP_NAME, "挂载 UDF 部分失败：\n\n%s" % msg)
        then, self._mount_then = self._mount_then, None
        then()      # 挂上了，接着做原来要做的事

    # 混合格式 / UDF 盘：放进来就自动读出全部文件，再问要不要修复成 ISO9660

    def _can_fix_fs(self):
        m = self.media
        return (B.is_drive(self.current_dev()) and m.foreign_data and m.appendable
                and not m.needs_repair and not self.job.running())

    def _offer_read_all(self):
        dev = self.current_dev()
        if not self.media.foreign_data or self.job.running():
            return
        if self._udf_source(dev):
            return self._offer_fs_fix()
        start = self._udf_start(dev)
        if start is None:
            return
        part = "前面 ISO 部分的文件已列在左边，后来" if self.media.iso_hidden else ""
        if QMessageBox.question(
                self, "读出光盘上的全部文件",
                "这张光盘有 Windows 用 UDF 格式写的文件（一般是“类似 USB 闪存驱动器”方式），"
                "%s写的这些文件系统默认读不到。\n\n现在读出全部文件吗？%s"
                % (part, "\n（要输入管理员密码。程序关闭时会自动卸载。）" if start else "")) \
                != QMessageBox.Yes:
            return
        self._mount_udf(dev, start, self._read_all_done)

    def _read_all_done(self):
        self._after_read_all = True     # 刷新完列出全部文件后，接着问要不要修复
        self.refresh_media()

    def _offer_fs_fix(self):
        if self._can_fix_fs() and self._udf_source(self.current_dev()):
            self.convert_to_iso(ask=True)

    def _udf_plan(self, udf_src, extra=0):
        """要读出来重刻的 UDF 文件：(文件数, 字节数)；空间不够时提示并返回 None。"""
        files = [e for e in B.walk_mount(udf_src) if not e.is_dir]
        size = sum(e.size for e in files)
        need = size + extra + B.estimate_overhead(len(files))
        m = self.media
        if need > m.free_bytes:
            QMessageBox.warning(self, APP_NAME, "空间不足：需要约 %s，光盘剩余 %s。"
                                % (B.human_size(need), B.human_size(m.free_bytes)))
            return None
        os.makedirs(STAGE_DIR, exist_ok=True)
        free = shutil.disk_usage(STAGE_DIR).free
        if free < size + 200 * 1024 * 1024:
            QMessageBox.warning(self, APP_NAME, "要先把 UDF 里的 %s 文件读到本机，但 %s 所在的磁盘只剩 %s。"
                                % (B.human_size(size), STAGE_DIR, B.human_size(free)))
            return None
        return len(files), size

    def convert_to_iso(self, ask=True):
        """修复光盘文件系统：不加新文件，把盘上全部文件统一刻成一个新的 ISO9660 会话。"""
        dev, m = self.current_dev(), self.media
        if not self._can_fix_fs():
            return
        udf_src = self._udf_source(dev)
        if udf_src is None:
            return self._offer_udf_mount(dev, self.convert_to_iso,
                                         "修复前得先挂载 UDF 部分，把里面的文件读出来")
        plan = self._udf_plan(udf_src)
        if plan is None:
            return
        n, size = plan
        iso_n = sum(1 for e in self.disc_entries if not e.is_dir and e.path in self._iso_paths
                    and e.path not in self._udf_paths)
        if ask and QMessageBox.question(
                self, "修复光盘文件系统",
                "这张盘%s有 Windows 写的 UDF 文件，所以系统默认读不全。\n\n"
                "修复：把盘上全部文件%s统一刻成一个新的 ISO9660 会话。"
                "修复后 Windows 和麒麟上直接就能看到全部文件，以后也能一直用本软件追加。\n\n"
                "· UDF 里的 %d 个文件（%s）要再写一遍%s；光盘剩余 %s\n"
                "· 不会删除盘上的任何数据\n\n现在修复吗？"
                % ("前面是 ISO9660，后面" if m.iso_hidden else "",
                   "（ISO 部分 %d 个 + UDF 部分 %d 个）" % (iso_n, n) if m.iso_hidden else "",
                   n, B.human_size(size), "，ISO 部分的不用重写" if m.iso_hidden else "",
                   B.human_size(m.free_bytes))) != QMessageBox.Yes:
            return
        volid = m.volume_id or self.ed_volid.text().strip() or \
            "光盘" + datetime.date.today().strftime("%Y-%m-%d")
        self._converting = True
        self._stage_then_burn(udf_src, dict(
            dev=dev, items=[], volid=volid if not B.check_volid(volid) else "", close=False,
            sbsector=m.last_iso_start if m.iso_hidden else None))

    def _stage_then_burn(self, udf_src, burn):
        os.makedirs(STAGE_DIR, exist_ok=True)
        for name in os.listdir(STAGE_DIR):      # 上次异常退出留下的
            shutil.rmtree(os.path.join(STAGE_DIR, name), ignore_errors=True)
        self._stage = tempfile.mkdtemp(dir=STAGE_DIR)
        self._pending_burn = burn
        self._set_busy(True, True)
        self.lb_stage.setText("正在把 UDF 里的文件读到本机……")
        self._stage_thread = FuncThread(B.stage_udf, udf_src, self._stage)
        self._stage_thread.done.connect(self._staged)
        self._stage_thread.start()

    def _staged(self, result):
        self._set_busy(False)
        code, msg = (1, str(result)) if isinstance(result, Exception) else result
        self._log(msg)
        burn, self._pending_burn = self._pending_burn, None
        if code != 0:
            self._drop_stage()
            self._converting = False
            self.chk_log.setChecked(True)
            return QMessageBox.critical(self, APP_NAME, "从光盘读出 UDF 里的文件失败，没有写盘：\n\n%s" % msg)
        staged = [(os.path.join(self._stage, n), "/" + n) for n in sorted(os.listdir(self._stage))]
        # UDF 的文件在前，这次加的在后：同名时以这次加的为准
        burn["items"] = staged + burn["items"]
        burn["normalize"] = [d for _, d in staged]
        self._start_burn(**burn)

    def _drop_stage(self):
        if self._stage:
            shutil.rmtree(self._stage, ignore_errors=True)
            self._stage = None

    def _eject_after(self, dev):
        """镜像文件没有可弹出的东西，只对光驱生效。"""
        return self.chk_eject.isChecked() and B.is_drive(dev)

    def _items(self):
        return [self.tree_new.topLevelItem(i) for i in range(self.tree_new.topLevelItemCount())]

    def _burn_done(self, code, out):
        self._drop_stage()
        converting, self._converting = self._converting, False
        log_path = self._save_log(out)
        if code == 0 and not B.md5_mismatch(out) and converting:
            self.bar_progress.setValue(1000)
            self.lb_stage.setText("光盘文件系统已修复")
            QMessageBox.information(
                self, APP_NAME, "修复完成：盘上全部文件已统一刻成 ISO9660，校验通过。\n\n"
                "Windows 和麒麟上都能直接看到全部文件，以后也能继续用本软件追加。")
        elif code == 0 and not B.md5_mismatch(out):
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
        self._release_drive(dev, remount=False)   # 擦除后是空白盘，没有可挂载的
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
        if dlg.mount_changed and not self.job.running():
            self.refresh_media()    # 挂上了 UDF 部分时，左边改列它的文件
        return dlg

    def recover_names(self, dev):
        dlg = RecoverDialog(self, dev)
        dlg.exec_()
        return dlg

    def apply_renames(self, dev, renames, fixups=()):
        self._release_drive(dev)
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
        self._release_drive(dev)
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
        staging = getattr(self, "_stage_thread", None)
        if staging and staging.isRunning():
            QMessageBox.warning(self, APP_NAME, "正在读出 UDF 里的文件，读完就开始刻录，请稍候。")
            e.ignore()
            return
        if self.job.running():
            self.job.proc.kill()
            self.job.proc.waitForFinished(3000)
        B.release_all_mounts()      # 本程序挂上的 UDF 部分卸掉，不留在系统里
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
