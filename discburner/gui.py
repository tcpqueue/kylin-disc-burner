# -*- coding: utf-8 -*-
"""光盘刻录助手 图形界面（PyQt5，麒麟 V10 自带 python3-pyqt5）。"""

import datetime
import getpass
import html
import os
import posixpath
import shutil
import sys
import tempfile
import time

from PyQt5.QtCore import (QFileInfo, QMimeData, QObject, QProcess, QSettings, Qt, QThread, QTimer,
                          QUrl, pyqtSignal, qVersion)
from PyQt5.QtGui import QBrush, QColor, QDrag, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFileIconProvider, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QToolButton, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator,
    QVBoxLayout, QWidget)

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
        text = "正在挂载 UDF 部分……（会弹出窗口，请输入开机密码）" if udf is not None else "正在挂载……"
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


def faded_icon(icon, size=16, opacity=0.45):
    """待刻录项目用的半透明图标。"""
    src = icon.pixmap(size, size)
    if src.isNull():
        return icon
    out = QPixmap(src.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setOpacity(opacity)
    p.drawPixmap(0, 0, src)
    p.end()
    return QIcon(out)


ROLE_PATH = Qt.UserRole          # 盘上路径
ROLE_KIND = Qt.UserRole + 1      # KIND_DISC / KIND_PENDING
ROLE_PENDING = Qt.UserRole + 2   # 待刻录项目在 MainWindow.pending 里的编号
ROLE_SORT = Qt.UserRole + 3      # 排序值：第 0 列是“是不是文件夹”，第 1、2 列是大小、时间
ROLE_FOLDER = Qt.UserRole + 4    # 能不能往里放文件（光盘上的文件夹、新建的文件夹）
KIND_DISC, KIND_PENDING = "disc", "pending"
COLUMNS = ["名称", "大小", "修改时间", "说明"]


class FileItem(QTreeWidgetItem):
    """文件夹排在文件前面；大小、时间按数值排。"""

    def __lt__(self, other):
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        a, b = self.data(0, ROLE_SORT) or 0, other.data(0, ROLE_SORT) or 0
        if a != b:
            return a < b
        if col in (1, 2):
            x, y = self.data(col, ROLE_SORT) or 0, other.data(col, ROLE_SORT) or 0
            if x != y:
                return x < y
        return self.text(col if col == 3 else 0).lower() < other.text(col if col == 3 else 0).lower()


class FileTree(QTreeWidget):
    """光盘上已有的文件和待刻录的文件放在同一个列表里。
    光盘上的可以拖到文件管理器（相当于复制）；文件管理器里的可以拖进来，
    拖到某个文件夹上就放进那个文件夹。"""

    dropped = pyqtSignal(list, str)
    delete_pressed = pyqtSignal()

    def __init__(self, provider, parent=None):
        super(FileTree, self).__init__(parent)
        self.provider = provider   # 盘上路径列表 → 本机路径列表（或 None）
        self.accept_files = True
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setHeaderLabels(COLUMNS)
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.AscendingOrder)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        h = self.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            h.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        h.setStretchLastSection(False)

    def _kind_items(self, kind):
        return [it for it in self.selectedItems() if it.data(0, ROLE_KIND) == kind]

    def selected_paths(self):
        """选中的光盘上的项目（不含待刻录的）；文件夹里的不重复列。"""
        return B.top_level([it.data(0, ROLE_PATH) for it in self._kind_items(KIND_DISC)])

    def selected_pending(self):
        return sorted(it.data(0, ROLE_PENDING) for it in self._kind_items(KIND_PENDING))

    @staticmethod
    def folder_for(item):
        """新文件放到哪个光盘文件夹：选中文件夹就放进去，选中文件就放在它旁边。"""
        if item is None:
            return "/"
        path = item.data(0, ROLE_PATH) or "/"
        if item.data(0, ROLE_FOLDER):
            return path
        return posixpath.dirname(path) or "/"

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

    def _wants(self, e):
        return e.source() is not self and self.accept_files and e.mimeData().hasUrls()

    def dragEnterEvent(self, e):
        if self._wants(e):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self._wants(e):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        if not self._wants(e):
            return e.ignore()
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.dropped.emit(paths, self.folder_for(self.itemAt(e.pos())))
            e.acceptProposedAction()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Delete:
            self.delete_pressed.emit()
            return
        super(FileTree, self).keyPressEvent(e)


DiscTree = FileTree     # 旧名字


class Banner(QFrame):
    """列表上方的提示条：说明这张盘的情况，旁边放可以直接点的按钮。"""

    STYLES = {B.ERROR: ("#fdecea", "#d9534f"), B.WARN: ("#fff4e5", "#e69500"),
              B.INFO: ("#e8f1fb", "#4a86c8"), B.OK: ("#e9f7ef", "#3c9d5d")}

    def __init__(self, parent=None):
        super(Banner, self).__init__(parent)
        self.setObjectName("banner")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 6, 6)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)
        h.addWidget(self.label, 1)
        self.buttons = []
        self._actions = []
        for i in range(3):
            b = QPushButton()
            b.clicked.connect(lambda _c=False, i=i: self._actions[i][1]())
            b.setVisible(False)
            h.addWidget(b)
            self.buttons.append(b)
        self.level = ""
        self.setVisible(False)

    def show_message(self, level, text, actions=()):
        bg, border = self.STYLES.get(level, self.STYLES[B.INFO])
        self.setStyleSheet("QFrame#banner{background:%s;border:1px solid %s;border-radius:4px}"
                           "QFrame#banner QLabel{color:#202020}" % (bg, border))
        self.level = level
        self.label.setText(text)
        self._actions = list(actions)
        for i, b in enumerate(self.buttons):
            b.setVisible(i < len(self._actions))
            if i < len(self._actions):
                b.setText(self._actions[i][0])
        self.setVisible(True)

    def clear(self):
        self.level = ""
        self._actions = []
        self.label.setText("")
        self.setVisible(False)

    def button(self, text):
        for i, (label, _fn) in enumerate(self._actions):
            if label == text:
                return self.buttons[i]
        return None


class Pending(object):
    """一项待刻录的内容。src 为 None 的是在光盘上新建的空文件夹。"""

    def __init__(self, src, folder="/", name=None, size=0, count=0, transfer=False):
        self.src = src
        self.folder = folder
        self.name = name or os.path.basename(src.rstrip("\\/")) or src
        self.size = size
        self.count = count
        self.transfer = transfer   # “转存到新光盘”时从原盘读出来的
        try:
            self.mtime = os.path.getmtime(src) if src else None
        except OSError:
            self.mtime = None

    @property
    def dst(self):
        return B.disc_join(self.folder, self.name)

    @property
    def bytes(self):
        """实际大小（显示用；size 是按扇区取整的，用来算容量）。"""
        if not hasattr(self, "_bytes"):
            self._bytes = B.local_bytes(self.src) if self.src else 0
        return self._bytes

    @property
    def is_dir(self):
        return self.src is None or os.path.isdir(self.src)


HINT_DUP = "与其他待刻录的重名"
HINT_REPLACE = "替换光盘上的同名文件"
HINT_MERGE = "并入光盘上的同名文件夹"


class MainWindow(QMainWindow):
    def __init__(self):
        super(MainWindow, self).__init__()
        self.setWindowTitle("%s %s" % (APP_NAME, __version__))
        self.setWindowIcon(app_icon())
        self.resize(1060, 720)

        self.media = B.MediaInfo()
        self.disc_entries = []
        self.pending = []            # type: list  # Pending
        self.drives = []
        self.burning = False
        self._remount = ""   # 写入前被卸载、写完要挂回去的设备
        self._copied = []    # “复制到…”的目标路径，复制完核对用
        self._tree_sbsector = None   # 列的不是最后一个会话时，那个 ISO 会话的起始扇区
        self._udf_root = None        # 盘上 UDF 部分在本机的挂载目录
        self._udf_entries = []
        self._udf_newer = True       # 混合盘上 UDF 部分是不是后写的（同名时以后写的为准）
        self._udf_paths, self._iso_paths = set(), set()   # 混合盘上各项来自哪一部分
        self._transfer = None        # 正在“转存到新光盘”：dict(stage, files, size, volid)
        self._hidden = []            # “从光盘删除”的项目，写完核对用
        self._renames = []
        self._after_read = None      # 读完盘接着要做的事
        self._fs_prompted = ""       # 已经问过要不要读出全部文件的设备
        self._outdev_retry = False
        # 已经提示过修复的设备，避免每次刷新都弹窗；换盘/弹出后清空
        self._repair_prompted = ""
        self._present = None         # 上次查到的光驱里有没有盘，用来发现放入/取出
        self._stale = set()          # 刻完没弹出的光驱：系统挂载看到的是旧内容（见 _release_drive）
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
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2500)
        self._poll_timer.timeout.connect(self._poll_media)

        self._build_ui()
        self._set_busy(False)
        self._clean_old_stage()
        self.refresh_drives()
        self._poll_timer.start()

    # ------------------------------------------------------------ 界面

    def _build_ui(self):
        root = QWidget()
        v = QVBoxLayout(root)

        # 刻录机
        row = QHBoxLayout()
        row.addWidget(QLabel("刻录机："))
        self.cb_drive = QComboBox()
        self.cb_drive.setMinimumWidth(260)
        self.cb_drive.activated.connect(self._drive_chosen)
        row.addWidget(self.cb_drive, 1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setToolTip("重新查找刻录机、读取光盘。放入或取出光盘时会自动刷新")
        self.btn_refresh.clicked.connect(self.refresh_drives)
        self.btn_eject = QPushButton("弹出")
        self.btn_eject.clicked.connect(self.eject)
        self.btn_tools = QToolButton()
        self.btn_tools.setText("更多功能")
        self.btn_tools.setPopupMode(QToolButton.InstantPopup)
        self.btn_tools.setToolButtonStyle(Qt.ToolButtonTextOnly)
        menu = QMenu(self.btn_tools)
        self.act_copy_all = menu.addAction("把整张光盘复制到电脑…", lambda: self.copy_all())
        self.act_transfer = menu.addAction("转存到新光盘…", lambda: self.transfer_to_new_disc())
        menu.addSeparator()
        self.act_diag = menu.addAction("光盘诊断…", lambda: self.diagnose())
        self.act_names = menu.addAction("恢复长文件名…", lambda: self.recover_names(self.current_dev()))
        self.act_repair = menu.addAction("修复未关闭会话…", lambda: self.repair(ask=True))
        self.act_blank = menu.addAction("擦除可擦写盘…", lambda: self.blank())
        menu.addSeparator()
        menu.addAction("生成 / 追加 ISO 镜像文件…", lambda: self._choose_image())
        menu.aboutToShow.connect(self._update_tools)
        self.btn_tools.setMenu(menu)
        for w in (self.btn_refresh, self.btn_eject, self.btn_tools):
            row.addWidget(w)
        v.addLayout(row)

        self.lb_media = QLabel("正在检测刻录机……")
        self.lb_media.setTextFormat(Qt.RichText)
        v.addWidget(self.lb_media)
        self.bar_capacity = QProgressBar()
        self.bar_capacity.setTextVisible(True)
        v.addWidget(self.bar_capacity)
        self.banner = Banner()
        v.addWidget(self.banner)

        # 光盘内容 + 待刻录，同一个列表
        self.tree = FileTree(self._drag_paths)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.dropped.connect(self.add_paths)
        self.tree.delete_pressed.connect(self._remove_selected)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree_disc = self.tree   # 旧名字
        v.addWidget(self.tree, 1)

        brow = QHBoxLayout()
        self.btn_add_files = QPushButton("添加文件…")
        self.btn_add_files.clicked.connect(self._pick_files)
        self.btn_add_dir = QPushButton("添加文件夹…")
        self.btn_add_dir.clicked.connect(self._pick_dir)
        self.btn_new_dir = QPushButton("新建文件夹…")
        self.btn_new_dir.setToolTip("在光盘上新建一个文件夹（默认用今天的日期命名），再往里添加文件")
        self.btn_new_dir.clicked.connect(lambda: self.new_folder())
        self.btn_remove = QPushButton("不刻录所选")
        self.btn_remove.setToolTip("从待刻录里去掉选中的项目（光盘上已有的不受影响）")
        self.btn_remove.clicked.connect(self._remove_selected)
        for b in (self.btn_add_files, self.btn_add_dir, self.btn_new_dir, self.btn_remove):
            brow.addWidget(b)
        self.lb_target = QLabel()
        self.lb_target.setTextFormat(Qt.PlainText)
        brow.addSpacing(12)
        brow.addWidget(self.lb_target)
        self.lb_legend = QLabel("（颜色浅的是待刻录的，可以直接把文件拖到列表里的文件夹上）")
        self.lb_legend.setStyleSheet("color:gray")
        self.lb_legend.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # 窗口窄时截掉，不撑宽窗口
        self.lb_legend.setToolTip(self.lb_legend.text())
        brow.addWidget(self.lb_legend, 1)
        v.addLayout(brow)

        # 选项
        g_opt = QGroupBox("刻录选项")
        oh = QHBoxLayout(g_opt)
        form = QFormLayout()
        self.ed_volid = QLineEdit()
        self.ed_volid.setPlaceholderText("Windows“此电脑”里显示的光盘名称")
        form.addRow("光盘名称：", self.ed_volid)
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
        self._selection_changed()

    def _set_busy(self, busy, burning=False):
        self.burning = burning
        for w in (self.cb_drive, self.btn_refresh, self.btn_eject, self.btn_burn, self.btn_tools,
                  self.btn_add_files, self.btn_add_dir, self.btn_new_dir, self.btn_remove,
                  self.ed_volid, self.cb_speed, self.chk_open, self.chk_verify, self.chk_eject):
            w.setEnabled(not busy)
        for b in self.banner.buttons:
            b.setEnabled(not busy)
        self.tree.accept_files = not busy
        if busy:
            QApplication.setOverrideCursor(Qt.BusyCursor)
        else:
            while QApplication.overrideCursor():
                QApplication.restoreOverrideCursor()

    def _busy(self):
        copying = getattr(self, "_copy_thread", None)
        return self.job.running() or bool(copying and copying.isRunning())

    def _update_tools(self):
        m, dev = self.media, self.current_dev()
        drive = B.is_drive(dev)
        has_files = bool(self.disc_entries)
        self.act_copy_all.setEnabled(has_files)
        self.act_transfer.setEnabled(drive and has_files and self._transfer is None)
        self.act_diag.setEnabled(bool(dev) and dev != IMAGE_ITEM)
        self.act_names.setEnabled(bool(dev) and m.has_iso and m.appendable and not m.udf_history)
        self.act_repair.setEnabled(drive and m.needs_repair)
        self.act_blank.setEnabled(drive and m.present and m.rewritable)

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
        """写入前把光盘从系统卸下（挂载着不能独占光驱）。

        麒麟上写完不弹出时不再挂回去：内核还记着刻录前的会话位置，要等换盘（弹出再放入）
        才重新读，这时挂上看到的是旧内容。本软件自己用 xorriso 读，不受影响；
        复制、拖出也不走系统挂载目录，直到光盘被取出。"""
        self._remount = ""
        if remount and not B.IS_WINDOWS and B.is_drive(dev):
            self._stale.add(dev)
        B.unmount(dev)

    def _poll_media(self):
        """放入、取出、换了光盘时自动重新读，不用点“刷新”。"""
        dev = self.current_dev()
        if not B.is_drive(dev) or self._busy() or not self.isVisible():
            return
        present = B.media_present_quick(dev)
        if present is None:
            return
        last, self._present = self._present, present
        if last is None or present == last:
            return
        if present:
            self.refresh_media()
        else:
            self._ejected()

    # ------------------------------------------------------------ 刻录机 / 盘片

    def refresh_drives(self):
        if self.job.running():
            return
        self._refresh_selection = self.current_dev() if self.current_dev() != IMAGE_ITEM else ""
        self._refresh_images = [
            (self.cb_drive.itemText(i), self.cb_drive.itemData(i))
            for i in range(self.cb_drive.count())
            if self.cb_drive.itemData(i) and self.cb_drive.itemData(i) != IMAGE_ITEM
            and not B.is_drive(self.cb_drive.itemData(i))]
        self.lb_media.setText("正在查找刻录机……")
        self._run(["-devices"], self._drives_done, "正在查找刻录机……")

    def _drives_done(self, code, out):
        self.drives = B.sort_drives(B.parse_devices(out))
        unknown = B.missing_drives(self.drives)
        if not B.IS_WINDOWS:
            self.drives += [d for d in B.sysfs_drives() if d.dev in unknown]
        self.cb_drive.clear()
        for d in self.drives:
            self.cb_drive.addItem(QIcon.fromTheme("drive-optical"), d.label, d.dev)
        for label, path in self._refresh_images:
            self.cb_drive.addItem(QIcon.fromTheme("media-optical"), label, path)
        self.cb_drive.addItem(QIcon.fromTheme("media-optical"),
                              "生成 / 追加 ISO 镜像文件…", IMAGE_ITEM)
        self.lb_stage.setText("就绪")
        selected = self.cb_drive.findData(self._refresh_selection)
        if selected < 0:
            selected = self.cb_drive.findData(
                QSettings("discburner", "discburner").value("drive", ""))
        self.cb_drive.setCurrentIndex(max(selected, 0))
        if self.current_dev() != IMAGE_ITEM:
            self.refresh_media()
            return
        self.media = B.MediaInfo()
        self._set_entries([], [])
        self._show_media()
        self._fill_tree()
        if B.IS_WINDOWS:
            text = "<b>未找到刻录机。</b>请检查光驱连接和供电，然后点“刷新”。"
            if unknown:
                text += ("<br>系统里有光驱 %s，但无法以刻录方式打开（可能是虚拟光驱，"
                         "或正被其他刻录软件占用）。" % "、".join(B.drive_letter(x) + ":" for x in unknown))
            self.lb_media.setText(text)
        elif B.drive_permission_problem():
            self.lb_media.setText("<b>当前账户还没有使用光驱的权限。</b>")
            self.banner.show_message(
                B.WARN, "光驱已经连上了，但这个账户还不能用它。点“授权使用光驱”，"
                        "在弹出的窗口里输入开机密码；然后注销重新登录（或重启电脑），再打开本软件。",
                [("授权使用光驱", self.grant_drive_access)])
        else:
            self.lb_media.setText(
                "<b>未找到刻录机。</b>请检查光驱的数据线和电源是否插好，然后点“刷新”。")

    def grant_drive_access(self):
        ok, msg = B.grant_cdrom_access()
        if ok:
            QMessageBox.information(self, APP_NAME, "已授权。请注销后重新登录（或重启电脑），"
                                    "再打开本软件就能使用光驱了。")
        else:
            QMessageBox.warning(self, APP_NAME, msg)

    def _choose_image(self):
        idx = self.cb_drive.findData(IMAGE_ITEM)
        if idx >= 0:
            self.cb_drive.setCurrentIndex(idx)
            self._drive_chosen(idx)

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
        self._present = None
        self.refresh_media()

    def refresh_media(self):
        dev = self.current_dev()
        if not dev or dev == IMAGE_ITEM or self.job.running():
            return
        if B.is_drive(dev):
            QSettings("discburner", "discburner").setValue("drive", dev)
            self._present = B.media_present_quick(dev)
        self.media = B.MediaInfo()
        self._set_entries([], [])
        if not B.is_drive(dev) and not os.path.exists(dev):
            # 新镜像文件：当作空白盘
            self.media.present = self.media.blank = True
            self._media_shown()
            return
        self.lb_media.setText("正在读取光盘……")
        self._fill_tree()
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
        self.media = m = B.parse_toc(out)
        m.udf_history = B.has_udf_history(dev, m)
        m.udf_bridge = B.is_current_udf_bridge(dev, m)
        if self._outdev_retry:
            m.tree_unreadable = True
        iso = B.parse_lsdl(out) if m.has_iso else []
        self._tree_sbsector = None
        self._udf_root = None
        udf = []
        if m.foreign_data or m.split:
            # 盘上有 UDF：系统挂着的 UDF 部分（Windows 上就是盘符）也列出来，和 ISO 部分合在一起
            self._udf_root = self._udf_source(dev)
            udf = B.walk_mount(self._udf_root) if self._udf_root else []
        if m.udf_bridge and udf:
            iso = []    # 同一个会话里的两套目录，内容一样；UDF 里的名字不会被截短
        if not m.has_iso and m.volume_id == "ISOIMAGE":
            m.volume_id = ""    # xorriso 在没有 ISO 目录时给的默认名字，不是盘的名字
        if not m.volume_id and self._udf_root:     # UDF 盘的名字：问系统
            if B.IS_WINDOWS:
                from . import winsys
                info = winsys.volume_info(B.drive_letter(dev))
                m.volume_id = info[0] if info else ""
            else:
                m.volume_id = B.probe_blkid(dev)[0].get("label", "")
        self._set_entries(iso, udf, udf_newer=not m.split)
        if m.iso_hidden:
            # 最后一个会话不是 ISO，xorriso 读到的是空目录，再单独列出前面那个 ISO 会话
            self._tree_sbsector = m.last_iso_start
            self._run(B.list_files_args(dev, self._tree_sbsector), self._iso_part_listed,
                      "正在读取光盘的 ISO 部分……")
            return
        self._media_shown()

    def _exact_times(self, iso):
        """xorriso 对半年以前的文件只列出日期。光盘被系统挂着（ISO）时，从挂载目录补上时分。"""
        dev = self.current_dev()
        rough = [e for e in iso if not e.mtime_exact]
        if not rough or not B.is_drive(dev) or dev in self._stale:
            return
        m = B.mount_info(dev)
        if not m or m[1].lower() == "udf":
            return
        for e in rough:
            try:
                e.mtime = os.stat(os.path.join(m[0], *e.path.strip("/").split("/"))).st_mtime
                e.mtime_exact = True
            except OSError:
                pass

    def _set_entries(self, iso, udf, udf_newer=True):
        self._exact_times(iso)
        for e in iso:
            e.part = B.PART_ISO
        self._iso_paths = {e.path for e in iso}
        self._udf_entries = list(udf)
        self._udf_paths = {e.path for e in udf}
        self._udf_newer = udf_newer
        self.disc_entries = B.merge_entries(iso, udf, udf_newer) if udf else list(iso)

    def _iso_part_listed(self, code, out):
        self._set_entries(B.parse_lsdl(out), self._udf_entries, True)
        self._media_shown()

    def _media_shown(self):
        if self.job.running():
            return
        self.lb_stage.setText("就绪")
        m = self.media
        # 光盘名称跟着光盘走：用户自己改过的不动，自动填的换盘后换成新盘的
        typed = self.ed_volid.text().strip()
        if not typed or typed == getattr(self, "_auto_volid", None):
            auto = m.volume_id if m.has_iso and m.volume_id else \
                "光盘" + datetime.date.today().strftime("%Y-%m-%d") if m.blank else typed
            if self._transfer and self._transfer.get("volid") and not m.has_iso:
                auto = self._transfer["volid"]
            if auto != typed:
                self.ed_volid.setText(auto)
            self._auto_volid = auto
        self._show_media()
        self._fill_tree()
        self._set_busy(False)
        dev = self.current_dev()
        after, self._after_read = self._after_read, None
        if after:
            QTimer.singleShot(0, after)
            return
        if not m.present:
            self._repair_prompted = ""
            self._fs_prompted = ""
        elif self._udf_unread() and self._fs_prompted != dev:
            # 混合格式 / UDF 盘：不等用户去点“光盘诊断”，自动问要不要读出全部文件
            self._fs_prompted = dev
            QTimer.singleShot(0, self._offer_read_all)
        elif m.needs_repair and self._repair_prompted != dev:
            self._repair_prompted = dev
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
            self.lb_media.setText("<b>光驱里没有光盘</b>，放入光盘后会自动读取。")
        else:
            color = {"空白盘": "green", "可追加": "green"}.get(m.status_text(), "red")
            if m.needs_repair or m.udf_history or m.foreign_data:
                color = "#d35400"
            parts = ["类型：<b>%s</b>" % ("ISO 镜像文件" if is_image else html.escape(m.profile)),
                     "状态：<b style='color:%s'>%s</b>" % (color, html.escape(self._status_text()))]
            if m.sessions:
                parts.append("已刻录过 %d 次" % m.sessions)
            if m.volume_id:
                parts.append("光盘名称：%s" % html.escape(m.volume_id))
            if not is_image:
                parts.append("已用 %s / 剩余 %s" % (B.human_size(m.used_bytes),
                                                 B.human_size(m.free_bytes)))
            self.lb_media.setText("　".join(parts))
        self._update_banner()
        self._update_capacity()

    def _status_text(self):
        m = self.media
        if m.present and m.appendable and (m.udf_history or m.foreign_data) and not m.needs_repair:
            return "只能读取（含 UDF 格式，不能追加）"
        return m.status_text()

    def _udf_unread(self):
        """盘上还有 UDF 部分没读出来（麒麟上要以管理员身份单独挂载）。"""
        m, dev = self.media, self.current_dev()
        return (not B.IS_WINDOWS and B.is_drive(dev) and m.present and not m.needs_repair
                and (m.foreign_data or m.split) and not m.udf_bridge and self._udf_root is None
                and self._udf_start(dev) is not None)

    def _update_banner(self):
        m, dev, ban = self.media, self.current_dev(), self.banner
        t = self._transfer
        transfer_tail = ""
        if t:
            transfer_tail = ("转存：原盘的 %d 个文件（%s）已读到电脑上。"
                             % (t["files"], B.human_size(t["size"])))
        if not B.IS_WINDOWS and B.is_drive(dev) and os.path.exists(dev) and \
                not os.access(dev, os.R_OK | os.W_OK):
            return ban.show_message(
                B.WARN, "光驱已经连上了，但这个账户还不能用它。点“授权使用光驱”，"
                        "在弹出的窗口里输入开机密码；然后注销重新登录（或重启电脑），再打开本软件。",
                [("授权使用光驱", self.grant_drive_access)])
        if t and not m.present:
            return ban.show_message(B.INFO, transfer_tail + "请放入一张空白光盘，读出后点“开始刻录”。",
                                    [("取消转存", self.cancel_transfer)])
        if not m.present:
            return ban.clear()
        if m.needs_repair:
            return ban.show_message(
                B.ERROR, "这张盘有一个会话没有关闭（通常是 Windows“类似 USB 闪存驱动器”方式刻录后"
                         "没有正常弹出），所以读不出内容。修复只关闭这个会话，不会删除数据。",
                [("修复", lambda: self.repair(ask=True)), ("光盘诊断…", self.diagnose)])
        if t and (m.udf_history or m.foreign_data):
            return ban.show_message(B.WARN, transfer_tail + "这还是含 UDF 的那张盘，请换一张空白光盘。",
                                    [("弹出", self.eject), ("取消转存", self.cancel_transfer)])
        if t and m.can_write:
            return ban.show_message(B.INFO, transfer_tail + "点“开始刻录”刻到这张盘上，"
                                    "刻完后 Windows 和麒麟都能读，也能继续追加。",
                                    [("取消转存", self.cancel_transfer)])
        copy_actions = [("转存到新光盘…", self.transfer_to_new_disc),
                        ("复制到电脑…", self.copy_all)]
        if self._udf_unread():
            return ban.show_message(
                B.WARN, "这张盘上有一部分文件是 Windows 用 UDF 格式写的，系统默认读不到，"
                        "列表里还缺这部分。点“读出全部文件”，在弹出的窗口里输入开机密码即可。",
                [("读出全部文件", self._offer_read_all)])
        if m.split:
            system = "Windows 资源管理器" if B.IS_WINDOWS else "麒麟文件管理器"
            return ban.show_message(
                B.WARN, "这张盘先用 UDF 格式刻过（一般是 Windows 刻的），后来又追加过：Windows 只显示"
                        "早先的文件，麒麟只显示后来的文件。列表里两部分都有，“说明”一栏标出了%s"
                        "里看不到的。这张盘不能再追加，建议转存到新光盘。" % system, copy_actions)
        if m.iso_hidden:
            return ban.show_message(
                B.WARN, "这张盘先用 ISO9660 格式刻过，后来又被 Windows 用“类似 USB 闪存驱动器”方式"
                        "（UDF 格式）接着写过：Windows 只显示后写的 UDF 文件，麒麟默认只能读到前面的部分。"
                        "列表里两部分都有。这张盘不能再追加，建议转存到新光盘。", copy_actions)
        if m.foreign_data and not m.udf_bridge:
            return ban.show_message(
                B.WARN, "这是 Windows 用“类似 USB 闪存驱动器”方式（UDF 格式）刻的盘，"
                        "两边都能读，但本软件不能往里追加：追加的内容 Windows 上看不到。"
                        "要加文件，请转存到新光盘。", copy_actions)
        if m.udf_bridge:
            return ban.show_message(
                B.INFO, "这是 Windows 自带刻录刻的盘，Windows 和麒麟都能读。本软件不能往里追加"
                        "（追加的内容 Windows 上看不到），要加文件请转存到新光盘。", copy_actions)
        if m.tree_unreadable:
            return ban.show_message(B.ERROR, "光驱能看到这张盘，但读不出盘上的目录，"
                                             "可能是刻录失败或盘片损坏。",
                                    [("光盘诊断…", self.diagnose)])
        shorts = [e for e in self.disc_entries
                  if not e.is_dir and B.is_short_name(posixpath.basename(e.path))]
        if shorts and m.sessions >= 2 and m.appendable and not m.udf_history:
            return ban.show_message(
                B.WARN, "有 %d 个文件只剩短文件名（如 %s），一般是 Windows 往麒麟刻的盘上追加造成的。"
                        "早先刻录的记录里可能还留着原名。" % (len(shorts), posixpath.basename(shorts[0].path)),
                [("恢复长文件名…", lambda: self.recover_names(self.current_dev()))])
        if m.closed:
            return ban.show_message(B.INFO, "这张盘已封盘，只能读取，不能再写入。",
                                    [("转存到新光盘…", self.transfer_to_new_disc)]
                                    if B.is_drive(dev) and self.disc_entries else [])
        ban.clear()

    # ------------------------------------------------------------ 文件列表

    def _icon(self, path, is_dir, local=None):
        if is_dir:
            return self._icons.icon(QFileIconProvider.Folder)
        if local:
            return self._icons.icon(QFileInfo(local))
        return self._icons.icon(QFileIconProvider.File)

    def _part_note(self, e):
        """混合盘上，标出系统文件管理器里看不到的那部分。"""
        m = self.media
        if not (self._udf_paths and self._iso_paths) or e.is_dir and \
                e.path in self._udf_paths and e.path in self._iso_paths:
            return ""
        if m.split:
            if B.IS_WINDOWS:
                return "资源管理器里看不到" if e.path not in self._udf_paths else ""
            return "文件管理器里看不到" if e.path not in self._iso_paths else ""
        if m.iso_hidden:
            return "系统默认看不到" if e.path not in self._udf_paths else ""
        return ""

    def _fill_tree(self):
        t = self.tree
        if not hasattr(self, "_icons"):
            self._icons = QFileIconProvider()
        expanded = set()
        it = QTreeWidgetItemIterator(t)
        while it.value():
            if it.value().isExpanded():
                expanded.add(it.value().data(0, ROLE_PATH))
            it += 1
        selected = {(i.data(0, ROLE_KIND), i.data(0, ROLE_PATH)) for i in t.selectedItems()}
        scroll = t.verticalScrollBar().value()
        t.setSortingEnabled(False)
        t.clear()
        nodes = {"/": t.invisibleRootItem()}
        hints = self._pending_hints()
        replaced = {self.pending[i].dst for i, h in hints.items() if h == HINT_REPLACE}
        first_level = not expanded
        for e in sorted(self.disc_entries, key=lambda x: x.path):
            parent = nodes.get(posixpath.dirname(e.path) or "/", t.invisibleRootItem())
            item = FileItem(parent, [posixpath.basename(e.path),
                                     "" if e.is_dir else B.human_size(e.size),
                                     B.format_mtime(e.mtime, e.mtime_exact),
                                     "将被新文件替换" if e.path in replaced else self._part_note(e)])
            item.setIcon(0, self._icon(e.path, e.is_dir))
            item.setData(0, ROLE_PATH, e.path)
            item.setData(0, ROLE_KIND, KIND_DISC)
            item.setData(0, ROLE_SORT, 0 if e.is_dir else 1)
            item.setData(0, ROLE_FOLDER, e.is_dir)
            item.setData(1, ROLE_SORT, e.size)
            item.setData(2, ROLE_SORT, e.mtime or 0)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if item.text(3):
                item.setForeground(3, QBrush(QColor("#d35400")))
            if e.is_dir:
                nodes[e.path] = item
        faded = self.palette().color(QPalette.Text)
        faded.setAlpha(110)
        pending_brush, note_brush = QBrush(faded), QBrush(QColor("#2f6fb5"))
        order = sorted(range(len(self.pending)), key=lambda i: self.pending[i].dst.count("/"))
        for i in order:
            p = self.pending[i]
            parent = nodes.get(p.folder, t.invisibleRootItem())
            note = hints.get(i) or ("新建文件夹" if p.src is None else
                                    "转存" if p.transfer else "待刻录")
            if p.count > 1:
                note += "（%d 个文件）" % p.count
            item = FileItem(parent, [p.name, "" if p.src is None else B.human_size(p.bytes),
                                     B.format_mtime(p.mtime), note])
            item.setIcon(0, faded_icon(self._icon(p.dst, p.is_dir, p.src)))
            item.setData(0, ROLE_PATH, p.dst)
            item.setData(0, ROLE_KIND, KIND_PENDING)
            item.setData(0, ROLE_PENDING, i)
            item.setData(0, ROLE_SORT, 0 if p.is_dir else 1)
            item.setData(0, ROLE_FOLDER, p.src is None)
            item.setData(1, ROLE_SORT, p.bytes)
            item.setData(2, ROLE_SORT, p.mtime or 0)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            for col in (0, 1, 2):
                item.setForeground(col, pending_brush)
            item.setForeground(3, QBrush(QColor("#c0392b")) if note.startswith(HINT_DUP) else note_brush)
            item.setToolTip(0, p.src or "在光盘上新建的文件夹")
            if p.src is None:
                nodes[p.dst] = item
            anc = parent
            while anc is not None and anc is not t.invisibleRootItem():
                expanded.add(anc.data(0, ROLE_PATH))
                anc = anc.parent()
        t.setSortingEnabled(True)
        it = QTreeWidgetItemIterator(t)
        while it.value():
            node = it.value()
            path = node.data(0, ROLE_PATH)
            if path in expanded or (first_level and node.parent() is None):
                node.setExpanded(True)
            if (node.data(0, ROLE_KIND), path) in selected:
                node.setSelected(True)
            it += 1
        t.verticalScrollBar().setValue(scroll)
        self._selection_changed()

    def _selection_changed(self):
        folder = self._target_folder()
        self.lb_target.setText("新文件放到：%s" % ("光盘根目录" if folder == "/" else folder))
        self.btn_remove.setEnabled(bool(self.tree.selected_pending()) and not self._busy())

    def _target_folder(self):
        items = self.tree.selectedItems()
        return FileTree.folder_for(items[0]) if items else "/"

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
        if self._udf_paths:
            iso, local = self._split_sources(dev, disc_paths)
            if not iso:
                return local        # 全是 UDF 部分的，直接用挂载目录
        else:
            stale = dev in self._stale
            local = None if stale else B.local_paths(dev, disc_paths, allow_udf=False)
            if local is None and B.is_drive(dev) and not B.IS_WINDOWS and not B.is_mounted(dev) \
                    and not stale:
                B.mount_disc(dev)   # 和放入光盘时系统自动挂载一样
                local = B.local_paths(dev, disc_paths, allow_udf=False)
            if local is not None:
                return local
            iso, local = disc_paths, []
        dest = self._drag_dir()
        QApplication.setOverrideCursor(Qt.BusyCursor)
        self.lb_stage.setText("正在从光盘取出文件……")
        QApplication.processEvents()
        try:
            code, out = B.copy_out(dev, iso, self._tree_sbsector, local, dest, self._udf_newer)
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
        if not udf or not self._udf_root:
            return iso, []
        return iso, [os.path.join(self._udf_root, *p.strip("/").split("/")) for p in udf]

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

    def _tree_menu(self, pos):
        menu = QMenu(self)
        disc = self.tree.selected_paths()
        pending = self.tree.selected_pending()
        if disc:
            menu.addAction("复制到电脑…", lambda: self.copy_out())
            act = menu.addAction("从光盘删除（隐藏）…", self.hide_selected)
            act.setEnabled(self._can_hide())
        if pending:
            menu.addAction("不刻录了（从待刻录里去掉）", self._remove_selected)
        if disc or pending:
            menu.addSeparator()
        folder = self._target_folder()
        where = "光盘根目录" if folder == "/" else posixpath.basename(folder)
        menu.addAction("添加文件到“%s”…" % where, self._pick_files)
        menu.addAction("在“%s”里新建文件夹…" % where, lambda: self.new_folder())
        for a in menu.actions():
            if not a.isSeparator() and self._busy():
                a.setEnabled(False)
        menu.exec_(self.tree.viewport().mapToGlobal(pos))

    def _can_hide(self):
        m = self.media
        return (m.has_iso and m.appendable and not m.foreign_data and not m.udf_history
                and not m.needs_repair and not self.job.running())

    def hide_selected(self):
        """一次性光盘上的“删除”：追加一个不含这些文件的新会话。"""
        dev = self.current_dev()
        paths = self.tree.selected_paths()
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
        if self._udf_write_blocked(dev):
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

        def check():    # 刷新时读出的目录就用来核对，不再单独读一遍
            left = {e.path for e in self.disc_entries} & set(hidden)
            self.bar_progress.setValue(1000)
            if not self.media.present:
                QMessageBox.warning(self, APP_NAME, "写入完成，但读回核对时读不出光盘。")
            elif left:
                QMessageBox.warning(self, APP_NAME, "写入完成，但以下项目还在：\n" + "\n".join(sorted(left)))
            else:
                self.lb_stage.setText("已从光盘删除")
                QMessageBox.information(self, APP_NAME, "已删除 %d 项，读回核对无误。" % len(hidden))
        self._after_read = check
        self.refresh_media()

    def copy_all(self):
        """把整张光盘复制到电脑上（混合盘的两部分都复制）。"""
        if self._udf_unread():
            return self._offer_read_all(then=self.copy_all)
        if not self.disc_entries:
            return QMessageBox.information(self, APP_NAME, "光盘上没有读出文件。")
        self.tree.clearSelection()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.data(0, ROLE_KIND) == KIND_DISC:
                it.setSelected(True)
        self.copy_out()

    def copy_out(self, dest=None):
        dev = self.current_dev()
        paths = self.tree.selected_paths()
        if not paths or self._busy():
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
        if self._udf_paths:
            # 混合盘：ISO 部分的用 xorriso 取，UDF 部分的从挂载目录复制
            iso, local = self._split_sources(dev, paths)
            job = (B.copy_out, dev, iso, self._tree_sbsector, local, dest, self._udf_newer)
        else:
            local = None if dev in self._stale else B.local_paths(dev, paths, allow_udf=False)
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

    # ------------------------------------------------------------ 待刻录

    def _pick_files(self):
        folder = self._target_folder()
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要刻录的文件",
                                                os.path.expanduser("~"))
        self.add_paths(paths, folder)

    def _pick_dir(self):
        folder = self._target_folder()
        path = QFileDialog.getExistingDirectory(self, "选择要刻录的文件夹",
                                                os.path.expanduser("~"))
        if path:
            self.add_paths([path], folder)

    def add_paths(self, paths, folder=None):
        folder = folder or self._target_folder()
        existing = {(p.src, p.folder) for p in self.pending}
        too_long = []
        for p in paths:
            p = os.path.abspath(p)
            if (p, folder) in existing or not os.path.exists(p):
                continue
            size, count = B.local_size(p)
            too_long += B.long_names(p)
            self.pending.append(Pending(p, folder, size=size, count=count))
        self._pending_changed()
        if too_long:
            QMessageBox.warning(
                self, APP_NAME,
                "以下名字太长（Windows 光盘最多 %d 个字符，麒麟最多 85 个汉字），"
                "刻到光盘上时会截短并保留扩展名，刻录前会显示改名对照。本机的原文件不改名：\n\n%s" %
                (B.JOLIET_NAME_MAX, "\n".join(too_long[:10])))

    def new_folder(self, name=None):
        folder = self._target_folder()
        if name is None:
            name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称：", QLineEdit.Normal,
                                            datetime.date.today().strftime("%Y-%m-%d"))
            if not ok:
                return
        name = name.strip().strip("/\\")
        if not name or "/" in name or "\\" in name:
            return QMessageBox.warning(self, APP_NAME, "文件夹名称不能为空，也不能含“/”或“\\”。")
        dst = B.disc_join(folder, name)
        on_disc = {e.path: e for e in self.disc_entries}
        if dst in on_disc and not on_disc[dst].is_dir:
            return QMessageBox.warning(self, APP_NAME, "光盘上已经有同名的文件：%s" % dst)
        if dst not in on_disc and all(p.dst != dst for p in self.pending):
            self.pending.append(Pending(None, folder, name=name))
            self._pending_changed()
        self._select_path(dst)

    def _select_path(self, path):
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            if it.value().data(0, ROLE_PATH) == path:
                self.tree.clearSelection()
                it.value().setSelected(True)
                self.tree.setCurrentItem(it.value())
                self.tree.scrollToItem(it.value())
                return
            it += 1

    def _remove_selected(self):
        drop = set(self.tree.selected_pending())
        if not drop:
            return
        # 去掉新建的文件夹时，放进去的也一起去掉
        gone = [self.pending[i].dst for i in drop if self.pending[i].src is None]
        for i, p in enumerate(self.pending):
            if any(p.folder == g or p.folder.startswith(g + "/") for g in gone):
                drop.add(i)
        self.pending = [p for i, p in enumerate(self.pending) if i not in drop]
        if self._transfer and not any(p.transfer for p in self.pending):
            self._drop_transfer()
        self._pending_changed()

    def _clear_pending(self):
        self.pending = []
        self._drop_transfer()
        self._pending_changed()

    def _pending_changed(self):
        self._fill_tree()
        self._update_capacity()
        self._update_banner()

    def pending_items(self):
        """[(本地路径, 光盘路径, 字节数, 文件数), ...]；新建的空文件夹用一个空目录代替。"""
        return [(p.src or self._empty_dir(), p.dst, p.size, p.count) for p in self.pending]

    def _empty_dir(self):
        path = os.path.join(STAGE_DIR, "empty")
        os.makedirs(path, exist_ok=True)
        for name in os.listdir(path):
            shutil.rmtree(os.path.join(path, name), ignore_errors=True)
        return path

    def _pending_hints(self):
        """{待刻录编号: 提示}：重名、替换光盘上的文件、并入已有文件夹。"""
        on_disc = {B.strip_version(e.path): e for e in self.disc_entries}
        hints, seen = {}, set()
        for i, p in enumerate(self.pending):
            dst = p.dst
            if dst.casefold() in seen:
                hints[i] = HINT_DUP
            elif dst in on_disc:
                hints[i] = HINT_MERGE if on_disc[dst].is_dir and p.is_dir else HINT_REPLACE
            seen.add(dst.casefold())
        return hints

    def _update_capacity(self):
        items = self.pending
        pending = sum(p.size for p in items)
        files = sum(p.count for p in items)
        bar = self.bar_capacity
        is_image = not B.is_drive(self.current_dev())
        if is_image or not self.media.present:
            bar.setRange(0, 1)
            bar.setValue(0)
            bar.setFormat("待刻录 %s（%d 个文件）" % (B.human_size(pending), files))
            bar.setStyleSheet("")
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

    # ------------------------------------------------------------ 转存到新光盘

    def transfer_to_new_disc(self):
        """含 UDF 的盘不能追加：先把盘上全部文件读到电脑上，再换空白盘刻成 ISO9660。"""
        dev, m = self.current_dev(), self.media
        if not B.is_drive(dev) or not m.present or self._busy() or self._transfer:
            return
        if self._udf_unread():
            return self._offer_read_all(then=self.transfer_to_new_disc)
        files = [e for e in self.disc_entries if not e.is_dir]
        if not files:
            return QMessageBox.information(self, APP_NAME, "光盘上没有读出文件。")
        size = sum(e.size for e in files)
        os.makedirs(STAGE_DIR, exist_ok=True)
        free = shutil.disk_usage(STAGE_DIR).free
        if free < size + 200 * 1024 * 1024:
            return QMessageBox.warning(
                self, APP_NAME, "要先把盘上 %s 的文件读到电脑上，但 %s 所在的磁盘只剩 %s。"
                % (B.human_size(size), STAGE_DIR, B.human_size(free)))
        if QMessageBox.question(
                self, "转存到新光盘",
                "把这张盘上的全部 %d 个文件（%s）转存到一张新光盘上：\n\n"
                "1. 先把文件读到电脑上的临时文件夹\n"
                "2. 读完自动弹出这张盘，请换一张空白光盘\n"
                "3. 点“开始刻录”，刻成 Windows 和麒麟都能直接读、以后也能继续追加的格式\n\n"
                "原来这张盘不会改动。待刻录里已有的文件也会一起刻上去。\n\n现在开始吗？"
                % (len(files), B.human_size(size))) != QMessageBox.Yes:
            return
        stage = tempfile.mkdtemp(prefix="transfer-", dir=STAGE_DIR)
        tops = B.top_level(["/" + e.path.strip("/").split("/")[0] for e in self.disc_entries])
        if self._udf_paths:
            iso, local = self._split_sources(dev, tops)
            job = (B.copy_out, dev, iso, self._tree_sbsector, local, stage, self._udf_newer)
        else:
            job = (B.copy_out, dev, tops, self._tree_sbsector, [], stage)
        self._transfer = dict(stage=stage, files=len(files), size=size, volid=m.volume_id)
        self._set_busy(True)
        self.lb_stage.setText("正在把光盘上的文件读到电脑上……")
        self._copy_thread = FuncThread(*job)
        self._copy_thread.done.connect(self._transfer_staged)
        self._copy_thread.start()

    def _transfer_staged(self, result):
        self._set_busy(False)
        code, msg = (1, str(result)) if isinstance(result, Exception) else result
        self._log(msg)
        t = self._transfer
        if code != 0 or t is None:
            self._drop_transfer()
            self.chk_log.setChecked(True)
            self.lb_stage.setText("读取失败")
            return QMessageBox.critical(self, APP_NAME, "从光盘读出文件失败，原盘没有改动：\n\n%s" % msg)
        B.make_writable([t["stage"]])
        staged = [Pending(os.path.join(t["stage"], n), "/", transfer=True)
                  for n in sorted(os.listdir(t["stage"]))]
        for p in staged:
            p.size, p.count = B.local_size(p.src)
        self.pending = staged + [p for p in self.pending if not p.transfer]
        if t["volid"] and not B.check_volid(t["volid"]):
            self.ed_volid.setText(t["volid"])
        self.lb_stage.setText("已读到电脑上")
        self._pending_changed()
        dev = self.current_dev()
        B.unmount(dev)
        self._run(B.eject_args(dev), lambda c, o: self._ejected(), "正在弹出……")
        QMessageBox.information(
            self, "转存到新光盘",
            "已把原盘的 %d 个文件（%s）读到电脑上，原盘已弹出。\n\n"
            "请放入一张空白光盘，软件读出后点“开始刻录”。" % (t["files"], B.human_size(t["size"])))

    def cancel_transfer(self):
        self.pending = [p for p in self.pending if not p.transfer]
        self._drop_transfer()
        self._pending_changed()

    def _drop_transfer(self):
        t, self._transfer = self._transfer, None
        if t:
            shutil.rmtree(t["stage"], ignore_errors=True)

    def _clean_old_stage(self):
        """上次异常退出留下的临时文件：两天以前的删掉（不动其他正在运行的实例的）。"""
        if not os.path.isdir(STAGE_DIR):
            return
        now = time.time()
        for name in os.listdir(STAGE_DIR):
            p = os.path.join(STAGE_DIR, name)
            try:
                if name != "empty" and now - os.path.getmtime(p) > 2 * 86400:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

    # ------------------------------------------------------------ 刻录

    def burn(self):
        dev = self.current_dev()
        m = self.media
        items = self.pending_items()
        if not dev or dev == IMAGE_ITEM:
            return QMessageBox.warning(self, APP_NAME, "请先选择刻录机。")
        if not items:
            return QMessageBox.information(self, APP_NAME, "请先添加要刻录的文件：点“添加文件…”，"
                                           "或者把文件直接拖到列表里。")
        if not m.present:
            return QMessageBox.warning(self, APP_NAME, "光驱里没有光盘，请放入光盘。")
        if m.needs_repair:
            return QMessageBox.warning(
                self, APP_NAME, "此盘有未关闭的会话，请先点上方提示条里的“修复”。")
        if m.closed:
            return QMessageBox.warning(self, APP_NAME, "此盘已封盘，无法再写入。请换一张光盘。")
        if self._udf_write_blocked(dev):
            return
        if not m.can_write:
            return QMessageBox.warning(self, APP_NAME, "此盘当前不可写入（%s）。" % m.status_text())
        missing = [p.src for p in self.pending if p.src and not os.path.exists(p.src)]
        if missing:
            return QMessageBox.warning(self, APP_NAME, "以下文件已不存在：\n" + "\n".join(missing))
        hints = self._pending_hints()
        dups = [self.pending[i].dst for i, h in hints.items() if h == HINT_DUP]
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

        replaced = [self.pending[i].dst for i, h in hints.items() if h == HINT_REPLACE]
        keep_open = self.chk_open.isChecked()
        lines = ["刻录 %d 项，共 %s" % (len(items), B.human_size(pending)),
                 "目标：%s" % self.cb_drive.currentText(),
                 "光盘名称：%s" % (volid or "（不设置）"),
                 "刻录后：%s" % ("保持可追加" if keep_open else "封盘（以后不能再写入！）")]
        if self._transfer:
            lines.append("\n其中包括从原盘转存的 %d 个文件。" % self._transfer["files"])
        if replaced:
            lines.append("\n以下光盘上的旧文件会被新文件替换（旧文件仍占用空间）：\n" +
                         "\n".join(replaced[:10]))
        fixups = B.version_fixups([e.path for e in self.disc_entries if not e.is_dir])
        shorts = [e.path for e in self.disc_entries
                  if not e.is_dir and B.is_short_name(os.path.basename(e.path))]
        if fixups:
            lines.append("\n同时修正 %d 个带“;1”的旧文件名（系统 xorriso 版本较老，不修正的话 Windows 上会变成 xxx_1）。"
                         % len(fixups))
        if shorts:
            lines.append("\n注意：盘上有 %d 个文件只剩短文件名（如 %s）。建议先取消，用“更多功能 → "
                         "恢复长文件名”找回原名；否则本次追加后它们仍是短名。"
                         % (len(shorts), os.path.basename(shorts[0])))
        if QMessageBox.question(self, "确认刻录", "\n".join(lines)) != QMessageBox.Yes:
            return
        normalize = [p.dst for p in self.pending if p.transfer]
        self._start_burn(dev, [(s, d) for s, d, _z, _c in items], volid, not keep_open,
                         fixups=fixups, normalize=normalize)

    def _udf_write_blocked(self, dev):
        if not B.is_drive(dev):
            return False
        if not self.media.udf_history:
            self.media.udf_history = B.has_udf_history(dev, self.media)
        if not (self.media.udf_history or self.media.foreign_data):
            return False
        if self._transfer:
            QMessageBox.warning(self, APP_NAME, "这还是含 UDF 格式的那张盘，不能往里写。请换一张空白光盘。")
            return True
        if QMessageBox.question(
                self, APP_NAME,
                "这张盘含有 UDF 格式（一般是 Windows 刻的），不能再往里追加："
                "追加的内容在 Windows 上看不到（Windows 自带的刻录往这种盘上追加也一样）。\n\n"
                "可以“转存到新光盘”：把这张盘上的文件和这次要刻的文件一起刻到一张空白光盘上，"
                "原盘不改动。\n\n现在转存吗？") == QMessageBox.Yes:
            QTimer.singleShot(0, self.transfer_to_new_disc)
        return True

    def _start_burn(self, dev, items, volid, close, fixups=(), sbsector=None, normalize=()):
        if self._udf_write_blocked(dev):
            return
        try:
            name_plan = B.plan_burn_names(self.disc_entries, items, fixups)
        except (OSError, ValueError) as e:
            return QMessageBox.warning(self, APP_NAME, str(e))
        if name_plan.changes:
            preview = "\n".join("%s → %s" % pair for pair in name_plan.changes[:20])
            if len(name_plan.changes) > 20:
                preview += "\n……共 %d 项路径" % len(name_plan.changes)
            if QMessageBox.question(
                    self, "确认光盘文件名",
                    "以下名称将按 Joliet 上限截短，保留扩展名；重名时加编号。"
                    "本机原文件不改名：\n\n%s\n\n按这些名称刻录吗？" % preview) != QMessageBox.Yes:
                return
        exclude = ()
        if B.IS_WINDOWS:
            os.makedirs(STAGE_DIR, exist_ok=True)
            self._alias_dir = tempfile.mkdtemp(prefix="alias-", dir=STAGE_DIR)
            try:
                items, exclude, problems = B.cygwin_safe_items(items, self._alias_dir)
            except OSError as e:
                self._drop_alias()
                return QMessageBox.warning(self, APP_NAME, "准备超长文件名时出错：%s" % e)
            if problems:
                self._drop_alias()
                return QMessageBox.warning(
                    self, APP_NAME, "以下文件夹的名字太长（超过 85 个汉字），刻录程序打不开，"
                    "请先改短再刻录：\n\n%s" % "\n".join(problems[:10]))
        self._release_drive(dev, remount=not self._eject_after(dev))
        args = B.burn_args(
            dev, items, volid=volid, close=close, speed=self.cb_speed.currentData(),
            verify=self.chk_verify.isChecked(), eject=self._eject_after(dev),
            fixups=fixups, sbsector=sbsector, normalize=normalize, name_plan=name_plan,
            exclude=exclude)
        self.log.clear()
        self._run(args, self._burn_done, "正在准备刻录……", burning=True)

    def _drop_alias(self):
        path, self._alias_dir = getattr(self, "_alias_dir", None), None
        if path:
            shutil.rmtree(path, ignore_errors=True)

    # 盘上的 UDF 部分：挂载后才能列出、复制

    def _udf_source(self, dev):
        m = B.mount_info(dev) if B.is_drive(dev) else None
        return m[0] if m and m[1].lower() == "udf" else None

    def _udf_start(self, dev):
        if self.media.udf_bridge:
            return self.media.last_iso_start
        return B.find_udf_session(dev, B.parse_tracks(self.media.raw)) if B.is_drive(dev) else None

    def _offer_read_all(self, then=None):
        dev = self.current_dev()
        if not self._udf_unread() or self._busy():
            return
        start = self._udf_start(dev)
        what = ("这张光盘上有一部分文件是 Windows 用 UDF 格式写的（一般是“类似 USB 闪存驱动器”方式），"
                "系统默认读不到。\n\n现在把它们也读出来吗？" if self.media.has_iso else
                "这张光盘是 Windows 用 UDF 格式写的（一般是“类似 USB 闪存驱动器”方式），"
                "系统还没有打开它。\n\n现在读出盘上的文件吗？")
        if QMessageBox.question(
                self, "读出光盘上的全部文件",
                what + "\n\n接下来会弹出一个窗口，输入开机密码（登录这台电脑用的密码）即可。"
                "本软件关闭时会自动收尾，不会改动系统。") != QMessageBox.Yes:
            return
        self._mount_udf(dev, start, then)

    def _mount_udf(self, dev, start, then=None):
        self._set_busy(True)
        self.lb_stage.setText("正在读取 UDF 部分……")
        self._mount_then = then
        self._mount_thread = FuncThread(B.mount_disc, dev, start)
        self._mount_thread.done.connect(self._udf_mounted)
        self._mount_thread.start()

    def _udf_mounted(self, result):
        self._set_busy(False)
        self.lb_stage.setText("就绪")
        path, msg = (None, str(result)) if isinstance(result, Exception) else result
        if not path or self._udf_source(self.current_dev()) is None:
            return QMessageBox.warning(self, APP_NAME, "读取 UDF 部分失败：\n\n%s" % msg)
        then, self._mount_then = self._mount_then, None
        self._after_read = then     # 重新列出全部文件后，接着做原来要做的事
        self.refresh_media()

    def _eject_after(self, dev):
        """镜像文件没有可弹出的东西，只对光驱生效。"""
        return self.chk_eject.isChecked() and B.is_drive(dev)

    def _burn_done(self, code, out):
        self._drop_alias()
        log_path = self._save_log(out)
        ok = code == 0 and not B.md5_mismatch(out)
        if ok:
            transfer = self._transfer
            self.bar_progress.setValue(1000)
            self.lb_stage.setText("刻录完成")
            self.pending = []
            self._drop_transfer()
            if transfer:
                msg = ("转存完成：原盘的 %d 个文件已刻到新光盘上%s。\n\n"
                       "新光盘在 Windows 和麒麟上都能直接打开，以后也能继续追加。"
                       "原来那张盘没有改动，可以留作备份。"
                       % (transfer["files"], "，校验通过" if self.chk_verify.isChecked() else ""))
            else:
                msg = "刻录完成。"
                if self.chk_verify.isChecked():
                    msg += "\n校验通过，数据与原文件一致。"
                if self.chk_open.isChecked():
                    msg += "\n光盘保持可追加，下次可以继续往里加文件。"
                if self.current_dev() in self._stale:
                    msg += "\n\n光盘没有弹出：文件管理器要弹出光盘再放入，才会显示刚刻的文件。"
            QMessageBox.information(self, APP_NAME, msg)
        else:
            self.lb_stage.setText("刻录失败")
            self.chk_log.setChecked(True)
            reason = "校验不一致，光盘数据可能有误。" if B.md5_mismatch(out) else \
                "xorriso 返回错误（代码 %d）。" % code
            QMessageBox.critical(self, APP_NAME, "刻录失败：%s\n\n待刻录的文件还在列表里，"
                                 "可以换一张光盘再试。\n\n详细日志：%s" % (reason, log_path))
        if not self._eject_after(self.current_dev()):
            self.refresh_media()
        else:
            self._ejected()

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
            self.refresh_media()    # 挂上了 UDF 部分时，列表里补上它的文件
        return dlg

    def recover_names(self, dev):
        dlg = RecoverDialog(self, dev)
        dlg.exec_()
        return dlg

    def apply_renames(self, dev, renames, fixups=()):
        if self._udf_write_blocked(dev):
            return
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

        def check():    # 读回核对：新名字都在、旧名字都没了
            names = {e.path for e in self.disc_entries}
            if not self.media.present:
                return QMessageBox.warning(self, APP_NAME, "写入完成，但读回核对时读不出光盘。\n\n"
                                           "请弹出光盘再放入，用“光盘诊断”确认文件名。")
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
        self._after_read = check
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
        if not B.is_drive(dev) or self._busy():
            return
        B.unmount(dev)
        self._run(B.eject_args(dev), lambda c, o: self._ejected(), "正在弹出……")

    def _ejected(self):
        self._stale.discard(self.current_dev())
        self._repair_prompted = ""
        self._fs_prompted = ""
        self._present = False
        self.media = B.MediaInfo()
        self._set_entries([], [])
        self.lb_stage.setText("就绪")
        self._show_media()
        self._fill_tree()

    def closeEvent(self, e):
        if self.burning and self.job.running():
            QMessageBox.warning(self, APP_NAME, "正在写入光盘，请等待完成，中途关闭会损坏光盘。")
            e.ignore()
            return
        copying = getattr(self, "_copy_thread", None)
        if copying and copying.isRunning():
            QMessageBox.warning(self, APP_NAME, "正在从光盘读取文件，请等读完再关闭。")
            e.ignore()
            return
        if self._transfer and QMessageBox.question(
                self, APP_NAME, "转存的文件还没刻到新光盘上。关闭后会删除已读到电脑上的临时文件"
                "（原盘不受影响，以后可以重新转存）。\n\n确定关闭吗？") != QMessageBox.Yes:
            e.ignore()
            return
        self._poll_timer.stop()
        if self.job.running():
            self.job.proc.kill()
            self.job.proc.waitForFinished(3000)
        self._drop_transfer()
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
