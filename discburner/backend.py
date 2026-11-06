# -*- coding: utf-8 -*-
"""xorriso 后端：构造命令行、解析输出。

不依赖 Qt，可单独测试。兼容 Python 3.8（麒麟 V10 SP1 自带）。

关键参数说明（这也是麒麟自带刻录软件出问题的地方）：
  -joliet on                 生成 Joliet 目录树。Windows 只认 Joliet / UDF，
                             没有 Joliet 时会回退到 ISO9660 的 8.3 名，
                             中文全部变成下划线，例如 "____.DOCX"。
  -compliance joliet_long_names:joliet_long_paths:joliet_utf16
                             Joliet 文件名放宽到 103 字符、路径不限 240 字符。
  -local_charset UTF-8       强制按 UTF-8 解释本地文件名，避免从图形界面
                             启动时 locale 为 C/POSIX 导致中文名被转坏。
  -close off                 每次刻录只关闭“会话”，不关闭“盘片”，
                             下次还能继续追加。
"""

import atexit
import getpass
import os
import platform
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    # Windows 版 xorriso 是 Cygwin 程序，按 LANG 决定命令行和文件名的字符集
    os.environ["LANG"] = "C.UTF-8"

SECTOR = 2048
# ISO9660 主卷描述符里卷标最多 32 字节；Joliet 卷标最多 16 个字符
VOLID_MAX_BYTES = 32
JOLIET_VOLID_MAX_CHARS = 16
# joliet_long_names 规则下的 Joliet 文件名上限
JOLIET_NAME_MAX = 103

COMMON_ARGS = [
    "-report_about", "UPDATE",
    "-abort_on", "FAILURE",
    "-return_with", "FAILURE", "32",
    "-local_charset", "UTF-8",
    "-iso_rr_pattern", "off",
    "-disk_pattern", "off",
]
if IS_WINDOWS:
    # libburn 不认识的 \\.\X: 会被当成普通文件直接读写。"caution" 名单里的地址如果不是
    # 刻录机就拒绝（"banned" 连真正的刻录机也会拒绝）。
    # 这条规则里反斜杠要写两遍，实际传给 xorriso 的是 \\\\.\\*
    COMMON_ARGS += ["-drive_class", "caution", r"\\\\.\\*"]

# 读取和写入都必须带上：-joliet on 放在 -dev 之前，
# 这样遇到 Windows 刻的只有 Joliet 没有 Rock Ridge 的盘也能读出正确的名字。
FS_ARGS = [
    "-joliet", "on",
    "-rockridge", "on",
    "-compliance", "joliet_long_names:joliet_long_paths:joliet_utf16",
]


class XorrisoNotFound(RuntimeError):
    pass


# ---------------------------------------------------------------- 平台差异
#
# Windows 版的 xorriso 是用 Cygwin + libcdio 编译的：光驱地址是 \\.\G:，
# 本地路径要写成 /cygdrive/c/... 的形式。

_WIN_DEV_RE = re.compile(r"^\\\\\.\\([A-Za-z]):$")


def is_drive(dev: str) -> bool:
    """光驱地址（而不是 ISO 镜像文件）。"""
    if IS_WINDOWS:
        return bool(_WIN_DEV_RE.match(dev))
    return dev.startswith("/dev/")


def drive_letter(dev: str) -> str:
    m = _WIN_DEV_RE.match(dev)
    return m.group(1).upper() if m else ""


def windows_dev(letter: str) -> str:
    return "\\\\.\\%s:" % letter.upper()


def cygwin_path(path: str) -> str:
    r"""C:\a\b → /cygdrive/c/a/b，\\server\share\x → //server/share/x"""
    p = path.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(/.*)?$", p)
    if m:
        return "/cygdrive/%s%s" % (m.group(1).lower(), m.group(2) or "/")
    return p


def xpath(path: str) -> str:
    """交给 xorriso 的本地路径（要刻的文件、镜像文件）。光驱地址原样返回。"""
    if not IS_WINDOWS or is_drive(path):
        return path
    return cygwin_path(os.path.abspath(path))


_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if IS_WINDOWS:
    # 单文件版：运行时解压到临时目录（sys._MEIPASS）；绿色版：xorriso 目录和主程序放在一起；
    # 源码运行时放在项目目录下。程序和 Cygwin DLL 在 xorriso\bin，配置在 xorriso\etc
    # （Cygwin 把 DLL 所在目录的上一级当根目录）
    BUNDLED_XORRISO = [
        os.path.join(d, "xorriso", "bin", "xorriso.exe")
        for d in (getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable), _APP_DIR) if d]
else:
    # 安装包自带的 xorriso（GNU xorriso 独立版，按 Ubuntu 16.04 / glibc 2.23 编译）
    BUNDLED_XORRISO = [
        "/usr/share/discburner/bin/xorriso",
        os.path.join(_APP_DIR, "bin", "xorriso"),
    ]
# 系统 xorriso 不低于这个版本时才优先于自带的。
# 1.5.2（麒麟 SP1 自带）读 Windows 刻的盘时，Joliet 名带着“;1”版本号读进来，
# 追加后 Windows 上 工作报告.docx 会变成 工作报告.docx_1；1.5.8 没有这个问题。
SYSTEM_XORRISO_MIN = (1, 5, 8)

_xorriso_path = None


def _no_window() -> Dict[str, int]:
    """Windows 下从界面程序启动命令行程序时不弹出黑窗口。"""
    return {"creationflags": 0x08000000} if IS_WINDOWS else {}   # CREATE_NO_WINDOW


def xorriso_version(path: str) -> Tuple[int, ...]:
    try:
        out = subprocess.run([path, "-no_rc", "-version"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=10, **_no_window()).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    m = re.search(rb"xorriso (\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else ()


def find_xorriso() -> str:
    """查找顺序：环境变量 DISCBURNER_XORRISO → 系统 xorriso（≥1.5.8）→ 自带的 → 系统的旧版本。

    自带的 1.5.8 如果被麒麟“执行控制”拦住（运行不了，版本号读不出来），
    就退回系统自带的旧版本；这时刻录前会自动修正带“;1”的文件名（见 version_fixups）。"""
    global _xorriso_path
    if _xorriso_path:
        return _xorriso_path
    forced = os.environ.get("DISCBURNER_XORRISO")
    if forced:
        _xorriso_path = forced
        return forced
    # Windows 上 PATH 里的 xorriso 多半来自 MSYS2 之类，路径写法不同，不用
    system = None if IS_WINDOWS else shutil.which("xorriso")
    if system and xorriso_version(system) >= SYSTEM_XORRISO_MIN:
        _xorriso_path = system
        return system
    for path in BUNDLED_XORRISO:
        if os.access(path, os.X_OK) and xorriso_version(path):
            _xorriso_path = path
            return path
    if system:
        _xorriso_path = system
        return system
    raise XorrisoNotFound("未找到可用的 xorriso，请重新安装本软件。")


# Windows 下参数不能直接放在命令行上：Cygwin 程序会自己再解析一遍命令行，
# 文件名里的单引号、星号等会被当成引号和通配符。所以把参数写进文件，
# 用 -options_from_file 读入。文件里每行一条命令，一行最多 5 * 4096 字节。
OPTIONS_LINE_MAX = 5 * 4096 - 2
# 这些命令后面跟着以 "--" 结尾的参数列表，列表里的词即使以“-”开头也不是新命令
_LIST_COMMANDS = {"-find", "-check_md5_r", "-file_size_limit"}
_COMMAND_RE = re.compile(r"^-[a-z_]+$")


def quote_word(word: str) -> str:
    """xorriso 的引号规则：单引号里原样，单引号本身写成 '"'"'。"""
    return "'" + word.replace("'", "'\"'\"'") + "'"


def options_file_text(args: List[str]) -> str:
    lines, cur, in_list = [], [], False
    for a in args:
        if cur and not in_list and _COMMAND_RE.match(a):
            lines.append(cur)
            cur = []
        cur.append(a)
        if a in _LIST_COMMANDS:
            in_list = True
        elif a == "--":
            in_list = False
    if cur:
        lines.append(cur)
    out = []
    for words in lines:
        line = " ".join(quote_word(w) for w in words)
        if len(line.encode("utf-8")) > OPTIONS_LINE_MAX:
            raise ValueError("参数太长：%s" % line[:200])
        out.append(line)
    return "\n".join(out) + "\n"


def xorriso_command(args: List[str]) -> Tuple[List[str], Optional[str]]:
    """返回 (命令行, 用完后要删掉的参数文件)。"""
    exe = find_xorriso()
    if not IS_WINDOWS:
        return [exe, "-no_rc"] + list(args), None
    # 空格让 Python 和 Qt 在 Windows 命令行中给整个路径加双引号，
    # 避免 Cygwin 把用户名 O'Brien 中的单引号当作引号语法。
    fd, path = tempfile.mkstemp(prefix="discburner options-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(options_file_text(args))
    return [exe, "-no_rc", "-options_from_file", cygwin_path(path)], path


def remove_quietly(path: Optional[str]) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def run_xorriso(args: List[str], timeout: int = 120) -> Tuple[int, str]:
    """同步运行 xorriso，返回 (退出码, 合并后的输出)。"""
    cmd, tmp = xorriso_command(args)
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, **_no_window())
    finally:
        remove_quietly(tmp)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- 设备

class Drive(object):
    def __init__(self, dev: str, vendor: str = "", model: str = ""):
        self.dev = dev
        self.vendor = vendor.strip()
        self.model = model.strip()

    @property
    def is_image(self) -> bool:
        return not is_drive(self.dev)

    @property
    def label(self) -> str:
        if self.is_image:
            return "ISO 镜像文件：%s" % self.dev
        name = " ".join(x for x in (self.vendor, self.model) if x)
        where = drive_letter(self.dev) + ":" if IS_WINDOWS else self.dev
        return "%s  (%s)" % (name or "光驱", where)

    def __repr__(self):
        return "Drive(%r, %r, %r)" % (self.dev, self.vendor, self.model)


_DEVICE_RE = re.compile(
    r"-dev\s+'([^']+)'\s+\S+\s*:\s*'([^']*)'\s*'([^']*)'")


def parse_devices(text: str) -> List[Drive]:
    """解析 `xorriso -devices` 的输出，例如：
    0  -dev '/dev/sr0' rwrw-- :  'HL-DT-ST' 'DVDRAM GP65NB60'
    """
    drives = []
    for line in text.splitlines():
        m = _DEVICE_RE.search(line)
        if m:
            drives.append(Drive(m.group(1), m.group(2), m.group(3)))
    return drives


# 虚拟光驱软件的设备名，排到真刻录机后面
_VIRTUAL_RE = re.compile(r"virtual|ezbsys|daemon|ultraiso|magiciso|poweriso|alcohol|wincdemu|msft|"
                         r"iso cdvd|\bvcd\b", re.I)


def is_virtual(d: Drive) -> bool:
    return bool(_VIRTUAL_RE.search(d.vendor + " " + d.model))


def sort_drives(drives: List[Drive]) -> List[Drive]:
    return sorted(drives, key=is_virtual)


def list_drives() -> List[Drive]:
    _, out = run_xorriso(["-devices"], timeout=60)
    return parse_devices(out)


def sysfs_drives(root: str = "/sys/block") -> List[Drive]:
    """直接列出 /dev/sr*。xorriso -devices 要独占打开光驱，
    光盘已被系统自动挂载时可能把它漏掉，用这个补上。"""
    drives = []
    try:
        names = sorted(n for n in os.listdir(root) if re.match(r"^sr\d+$", n))
    except OSError:
        return drives
    for name in names:
        info = []
        for field in ("vendor", "model"):
            try:
                with open(os.path.join(root, name, "device", field)) as f:
                    info.append(f.read().strip())
            except OSError:
                info.append("")
        drives.append(Drive("/dev/" + name, *info))
    return drives


def missing_drives(found: List[Drive]) -> List[str]:
    """系统里有、但 xorriso 没列出来的光驱。

    Linux 下把它们补进列表（见 sysfs_drives）。Windows 下不补：xorriso 打不开的
    \\\\.\\X: 不能拿来刻录，只在“未找到刻录机”时告诉用户有哪些光驱没能识别。"""
    known = {d.dev for d in found}
    if IS_WINDOWS:
        from . import winsys
        return [windows_dev(c) for c in winsys.cdrom_letters() if windows_dev(c) not in known]
    return [d.dev for d in sysfs_drives() if d.dev not in known and os.path.exists(d.dev)]


# ---------------------------------------------------------------- 盘片信息

_REWRITABLE_RE = re.compile(r"(CD-RW|DVD-RW|DVD\+RW|DVD-RAM|BD-RE)\b")


class MediaInfo(object):
    """`xorriso -toc` 的解析结果。"""

    def __init__(self):
        self.present = False
        self.profile = ""          # DVD+R / DVD-RW / CD-R / BD-R / stdio file ...
        self.status = ""           # 原始状态文字
        self.blank = False
        self.appendable = False
        self.closed = False
        # 有未关闭的轨道/会话：典型情况是 Windows“类似 USB 闪存驱动器”方式
        # 刻录后没有正常弹出。-toc 会在状态行后附加 ", but next track is damaged"
        self.damaged = False
        self.incomplete = False    # CD 上的未完成会话："Incomplete session encountered !"
        self.sessions = 0          # ISO 会话数
        self.has_iso = False
        self.udf_bridge = False    # 同一 ISO 会话内还有 UDF 目录
        self.udf_history = False   # 已检测到 UDF：不能保证 Windows 采用最新 ISO 目录
        self.other_sessions = 0    # 非 ISO9660 的数据轨（如 Windows UDF 实时文件系统）
        # 最后一个会话不是 ISO9660：xorriso 只读最后一个会话的目录，会当成空盘
        self.last_other = False
        self.last_iso_start = None  # type: Optional[int]  最后一个 ISO9660 会话的起始扇区
        self.readable_blocks = 0
        self.writable_blocks = 0
        self.overall_blocks = 0
        # 光驱给不出目录（TOC）：轨道没写完、没关闭时常见
        self.toc_missing = False
        # 盘上目录加载失败，xorriso 用 -indev 时连光驱都不接管，状态是改用 -outdev 读的
        self.tree_unreadable = False
        self.volume_id = ""
        self.drive_desc = ""       # 光驱厂商 型号 固件
        self.media_product = ""    # 盘片制造商代码
        self.raw = ""

    @property
    def free_bytes(self) -> int:
        return self.writable_blocks * SECTOR

    @property
    def used_bytes(self) -> int:
        return self.readable_blocks * SECTOR

    @property
    def total_bytes(self) -> int:
        return self.free_bytes + self.used_bytes

    @property
    def rewritable(self) -> bool:
        # 只匹配完整的介质名：注意 "DVD-R sequential recording" 里也有 "RE"
        return bool(_REWRITABLE_RE.match(self.profile.upper()))

    @property
    def unfinished(self) -> bool:
        """轨道写了一部分却没关闭：光驱不一定标“damaged”，但给不出目录，
        而且已写的扇区比能读的多。Windows 格式化或刻录中途失败、没正常弹出时就是这样
        （实测：格式化失败的 DVD+R，已写 288 扇区，可读 1 扇区）。"""
        written = self.overall_blocks - self.writable_blocks
        return (self.present and not self.blank and not self.closed and self.toc_missing
                and written > self.readable_blocks)

    @property
    def needs_repair(self) -> bool:
        return self.present and (self.damaged or self.incomplete or self.unfinished)

    @property
    def can_write(self) -> bool:
        return self.present and (self.blank or self.appendable) and not self.needs_repair

    @property
    def iso_hidden(self) -> bool:
        """前面是 ISO9660，后面又接着写了别的格式（典型：本软件刻过的盘，
        后来被 Windows 用“类似 USB 闪存驱动器”方式写成 UDF）。"""
        return self.present and self.has_iso and self.last_other

    @property
    def foreign_data(self) -> bool:
        """盘上有不是 ISO9660 的数据（追加会让它在新会话里不可见）。"""
        return self.present and not self.blank and (
            self.last_other or self.udf_bridge or
            (not self.has_iso and (self.readable_blocks > 0 or self.other_sessions > 0)))

    def status_text(self) -> str:
        if not self.present:
            return "无盘片"
        if self.needs_repair:
            return "会话未关闭（需修复）"
        if self.blank:
            return "空白盘"
        if self.closed:
            return "已封盘（不可再写入）"
        if self.appendable:
            return "可追加（盘上有其他格式的数据）" if self.foreign_data else "可追加"
        return self.status or "未知"


def _field(line: str) -> str:
    return line.split(":", 1)[1].strip() if ":" in line else ""


def parse_toc(text: str) -> MediaInfo:
    info = MediaInfo()
    info.raw = text
    last_iso_volid = ""
    for line in text.splitlines():
        if line.startswith("Media current:"):
            info.profile = _field(line)
            if info.profile and "not" not in info.profile.lower():
                info.present = True
        elif line.startswith("Media status :"):
            info.status = _field(line)
            s = info.status.lower()
            info.blank = "is blank" in s
            info.appendable = "is appendable" in s
            info.closed = "is closed" in s
            if "damaged" in s or "but next track" in s:
                info.damaged = True
            if "not present" in s or "is not recognizable" in s:
                info.present = False
        elif line.startswith("Incomplete session encountered"):
            info.incomplete = True
        elif line.startswith("Media blocks :"):
            m = re.search(r"(\d+)\s+readable\s*,\s*(\d+)\s+writable\s*,\s*(\d+)\s+overall", line)
            if m:
                info.readable_blocks = int(m.group(1))
                info.writable_blocks = int(m.group(2))
                info.overall_blocks = int(m.group(3))
        elif "Cannot obtain Table Of Content" in line:
            info.toc_missing = True
        elif "No input drive acquired" in line or "Cannot read ISO image tree" in line:
            info.tree_unreadable = True
        elif line.startswith("ISO session  :"):
            info.sessions += 1
            info.has_iso = True
            info.last_other = False
            m = re.match(r"ISO session\s*:\s*\d+\s*,\s*(\d+)\s*,\s*\d+s\s*,\s*(.*?)\s*$", line)
            if m:
                info.last_iso_start, last_iso_volid = int(m.group(1)), m.group(2)
        elif line.startswith("Other session:") or line.startswith("Data track   :"):
            info.other_sessions += 1
            info.last_other = True
        elif line.startswith("Volume id    :"):
            v = _field(line)
            if len(v) >= 2 and v[0] == v[-1] == "'":
                v = v[1:-1]
            info.volume_id = v
        elif line.startswith("Drive type   :"):
            info.drive_desc = " ".join(re.findall(r"'([^']*)'", line))
        elif line.startswith("Media product:"):
            info.media_product = _field(line)
    if info.closed:
        info.writable_blocks = 0
    if info.iso_hidden:
        # 最后一个会话不是 ISO 时 xorriso 报的卷标是它给空目录起的 ISOIMAGE，不是盘的卷标
        info.volume_id = last_iso_volid
    return info


# 只读时以共享方式打开光驱：系统挂载着光盘也能读，不用先卸载，文件管理器里照常能看到
READ_ACCESS = ["-drive_access", "shared:readonly"]


def toc_args(dev: str) -> List[str]:
    # 读取信息时遇到错误也不中止：会话没关好的盘加载目录可能失败，
    # 但 -toc 仍要照常输出，才能检测出“需要修复”
    return COMMON_ARGS + ["-abort_on", "NEVER"] + READ_ACCESS + FS_ARGS + \
        ["-indev", xpath(dev), "-toc"]


def outdev_toc_args(dev: str) -> List[str]:
    """不加载盘上目录，只读光驱和盘片状态。目录坏了 -indev 拿不到光驱时用。"""
    return COMMON_ARGS + ["-abort_on", "NEVER"] + READ_ACCESS + ["-outdev", xpath(dev), "-toc"]


def indev_failed(out: str) -> bool:
    return "No input drive acquired" in out


def read_media(dev: str) -> MediaInfo:
    _, out = run_xorriso(toc_args(dev), timeout=120)
    if indev_failed(out):
        _, out2 = run_xorriso(outdev_toc_args(dev), timeout=120)
        info = parse_toc(out2)
        info.tree_unreadable = True
    else:
        info = parse_toc(out)
    info.udf_history = has_udf_history(dev, info)
    info.udf_bridge = is_current_udf_bridge(dev, info)
    return info


# ---------------------------------------------------------------- 光盘内文件

class DiscEntry(object):
    def __init__(self, path: str, is_dir: bool, size: int):
        self.path = path
        self.is_dir = is_dir
        self.size = size

    def __repr__(self):
        return "DiscEntry(%r, %r, %r)" % (self.path, self.is_dir, self.size)


def _unquote(s: str) -> str:
    """xorriso 把名字里的单引号写成 '"'"'。"""
    return s.replace("'\"'\"'", "'")


_LSDL_RE = re.compile(r"^([-dlbcps])\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+.*?'(.*)'\s*$")


def parse_lsdl(text: str) -> List[DiscEntry]:
    """解析 `-find / -exec lsdl` 的输出，例如：
    -rw-r--r--    1 1000     1000            2 Oct 25 17:20 '/工作报告.docx'
    """
    entries = []
    for line in text.splitlines():
        m = _LSDL_RE.match(line)
        if not m:
            continue
        path = _unquote(m.group(3))
        if path == "/":
            continue
        entries.append(DiscEntry(path, m.group(1) == "d", int(m.group(2))))
    return entries


def load_args(sbsector: Optional[int]) -> List[str]:
    """读指定起始扇区的 ISO 会话，而不是最后一个会话。"""
    return [] if sbsector is None else ["-load", "sbsector", str(sbsector)]


def list_files_args(dev: str, sbsector: Optional[int] = None) -> List[str]:
    return COMMON_ARGS + READ_ACCESS + FS_ARGS + load_args(sbsector) + [
        "-indev", xpath(dev), "-find", "/", "-exec", "lsdl", "--"]


def walk_mount(root: str) -> List[DiscEntry]:
    """挂载目录里的文件，路径写成盘上的样子（/目录/文件）。UDF 部分只能这样列出来。"""
    entries = []
    for top, dirs, files in os.walk(root):
        rel = os.path.relpath(top, root)
        base = "" if rel == "." else "/" + rel.replace(os.sep, "/")
        for n in dirs:
            entries.append(DiscEntry(base + "/" + n, True, 0))
        for n in files:
            try:
                size = os.path.getsize(os.path.join(top, n))
            except OSError:
                size = 0
            entries.append(DiscEntry(base + "/" + n, False, size))
    return entries


def _merge_copy(src: str, target: str) -> None:
    """复制文件或文件夹；文件夹已存在时合并进去，同名文件覆盖。
    （Python 3.5 的 copytree 不能往已有的文件夹里复制）"""
    if not os.path.isdir(src):
        shutil.copy2(src, target)
        return
    os.makedirs(target, exist_ok=True)
    for name in os.listdir(src):
        _merge_copy(os.path.join(src, name), os.path.join(target, name))


def merge_entries(iso: List[DiscEntry], udf: List[DiscEntry]) -> List[DiscEntry]:
    """混合盘的 ISO 部分和 UDF 部分合成一份列表；同一路径以 UDF（后写的）为准。"""
    by_path = {e.path: e for e in iso}
    by_path.update({e.path: e for e in udf})
    return list(by_path.values())


def copy_local(srcs: List[str], dest: str) -> Tuple[int, str]:
    """从挂载目录复制到本地（UDF 部分 xorriso 读不了，只能走文件系统）。"""
    done = []
    try:
        for s in srcs:
            target = os.path.join(dest, os.path.basename(s.rstrip(os.sep)))
            _merge_copy(s, target)
            done.append(target)
    except (OSError, shutil.Error) as e:
        return 1, "%s\n已复制：%s" % (e, "、".join(done) or "无")
    return 0, "已复制 %d 项到 %s" % (len(done), dest)


def copy_out(dev: str, iso_paths: List[str], sbsector: Optional[int],
             local_srcs: List[str], dest: str) -> Tuple[int, str]:
    """混合盘上复制一批项目：ISO 部分的用 xorriso 取出，UDF 部分的从挂载目录复制。
    先取 ISO 再复制 UDF，同名时 UDF 的（后写的）为准。"""
    msgs = []
    if iso_paths:
        code, out = run_xorriso(extract_args(dev, iso_paths, dest, sbsector), timeout=3600)
        if code != 0:
            return code, out
        msgs.append(out)
    if local_srcs:
        code, out = copy_local(local_srcs, dest)
        if code != 0:
            return code, out
        msgs.append(out)
    return 0, "\n".join(msgs)


def list_disc_files(dev: str) -> List[DiscEntry]:
    _, out = run_xorriso(list_files_args(dev), timeout=300)
    return parse_lsdl(out)


# ---------------------------------------------------------------- 取出光盘上的文件

def top_level(disc_paths: List[str]) -> List[str]:
    """去掉已经包含在其他所选文件夹里的项目，免得复制两遍。"""
    keep = []
    for p in sorted(set(disc_paths)):
        if not any(p.startswith(k.rstrip("/") + "/") for k in keep):
            keep.append(p)
    return keep


def disc_basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def local_paths(dev: str, disc_paths: List[str], allow_udf: bool = True) -> Optional[List[str]]:
    """光盘被系统挂载着时，盘上路径在本机的对应路径；有任何一个对不上就返回 None。"""
    m = mount_info(dev) if is_drive(dev) else None
    if not m or (not allow_udf and m[1].lower() == "udf"):
        return None
    out = [os.path.join(m[0], *p.strip("/").split("/")) for p in disc_paths]
    return out if all(os.path.exists(p) for p in out) else None


def extract_args(dev: str, disc_paths: List[str], dest: str,
                 sbsector: Optional[int] = None) -> List[str]:
    """把光盘上的文件或文件夹取到本地目录 dest 下，不用挂载，镜像文件也行。
    auto_chmod_on：盘上只读的文件夹也能往里放文件。"""
    args = COMMON_ARGS + READ_ACCESS + FS_ARGS + load_args(sbsector) + \
        ["-indev", xpath(dev), "-osirrox", "on:auto_chmod_on"]
    for p in disc_paths:
        args += ["-extract", p, xpath(os.path.join(dest, disc_basename(p)))]
    return args


def make_writable(paths: List[str]) -> None:
    """光盘上的文件常是只读的，取出来以后给当前用户加上写权限，方便修改和删除。"""
    def fix(p):
        try:
            os.chmod(p, os.stat(p).st_mode | 0o200)
        except OSError:
            pass
    for top in paths:
        fix(top)
        for root, dirs, files in os.walk(top):
            for n in dirs + files:
                fix(os.path.join(root, n))


# ---------------------------------------------------------------- 刻录

def check_volid(volid: str) -> Optional[str]:
    """返回错误提示；None 表示合法。"""
    if len(volid.encode("utf-8")) > VOLID_MAX_BYTES:
        return "卷标太长：最多 %d 字节（约 10 个汉字或 32 个英文字母）" % VOLID_MAX_BYTES
    if len(volid) > JOLIET_VOLID_MAX_CHARS:
        return "卷标太长：Windows 最多显示 %d 个字符" % JOLIET_VOLID_MAX_CHARS
    if volid.startswith("-"):
        return "卷标不能以“-”开头"
    return None


def disc_join(folder: str, name: str) -> str:
    folder = "/" + folder.strip("/")
    return (folder.rstrip("/") + "/" + name) if folder != "/" else "/" + name


_VERSION_RE = re.compile(r";\d+$")


def strip_version(name: str) -> str:
    """去掉 ISO9660 版本号：报告.docx;1 → 报告.docx"""
    return _VERSION_RE.sub("", name)


def version_fixups(paths, skip=()) -> List["Rename"]:
    """老版本 xorriso 读 Windows 刻的盘时文件名带“;1”，写新会话前把它们改回干净的名字。
    新版本读出来的名字本来就不带，返回空列表。"""
    taken = set(paths)
    out = []
    for p in sorted(paths):
        if p in skip or not _VERSION_RE.search(p):
            continue
        new = strip_version(p)
        if new in taken:
            continue
        taken.add(new)
        out.append(Rename(p, new, -1))
    return out


def windows_perm_args(dsts: List[str]) -> List[str]:
    """Windows 上 Cygwin 按 NTFS 权限换算出的 Rock Ridge 属性是 rwxrwx---、属主是 Windows
    用户的编号，麒麟上的普通用户就读不了这些文件。统一改成 root 所有、文件 644、目录 755。"""
    args = []
    for dst in dsts:
        args += ["-chown_r", "0", dst, "--", "-chgrp_r", "0", dst, "--",
                 "-find", dst, "-type", "d", "-exec", "chmod", "0755", "--",
                 "-find", dst, "-type", "f", "-exec", "chmod", "0644", "--"]
    return args


def burn_args(dev: str, items: List[Tuple[str, str]], volid: str = "",
              close: bool = False, speed: str = "", verify: bool = True,
              eject: bool = False, fixups=(), sbsector: Optional[int] = None,
              normalize=(), name_plan=None) -> List[str]:
    """items: [(本地路径, 光盘内目标路径), ...]；fixups: 写入前要改的名字（version_fixups）

    对空白盘是首次刻录；对可追加的盘会自动导入原有目录树再写一个新会话，
    新会话里包含新旧全部文件的 Joliet 名，所以旧盘的乱码也会被修正。
    sbsector：以这个 ISO 会话的目录为底（最后一个会话不是 ISO 时）。
    normalize：这些目标统一成 root 所有、文件 644、目录 755（从 UDF 部分读出来的文件）。
    """
    args = COMMON_ARGS + FS_ARGS + [
        # 允许 >4GiB 的单个文件（ISO9660 level 3 多区段）。
        # 注意结尾的 "--" 不能省，否则 1.5.2 会把后面所有参数都当成它的值。
        "-file_size_limit", "off", "--",
    ]
    if verify:
        args += ["-md5", "on"]
    args += load_args(sbsector) + ["-dev", xpath(dev)]
    if volid:
        args += ["-volid", volid]
    if speed:
        args += ["-speed", speed]
    args += ["-close", "on" if close else "off",
             "-overwrite", "nondir"]
    for r in fixups:
        args += ["-move", r.old, r.new]
    for src, dst in items:
        args += ["-map", xpath(src), dst]
    if name_plan is not None:
        for r in name_plan.renames:
            args += ["-move", r.old, r.new]
    def final_path(path):
        return name_plan.final_paths.get(posixpath.normpath("/" + path.lstrip("/")), path) \
            if name_plan is not None else path
    if IS_WINDOWS:
        args += windows_perm_args([final_path(dst) for _, dst in items])
    elif normalize:
        args += windows_perm_args([final_path(dst) for dst in normalize])
    args += ["-commit"]
    if verify:
        for _, dst in items:     # 每项单独一条，Windows 下参数文件一行有长度限制
            args += ["-check_md5_r", "FAILURE", final_path(dst), "--"]
    if eject:
        args += ["-eject", "all"]
    return args


def blank_args(dev: str) -> List[str]:
    """按介质状态擦除。保留 DVD-RW 原有模式，不强制转换为可覆盖模式。"""
    return COMMON_ARGS + ["-outdev", xpath(dev), "-blank", "as_needed"]


def eject_args(dev: str) -> List[str]:
    return ["-outdev", xpath(dev), "-eject", "all"]


def repair_args(dev: str, keep_open: bool = True, force: bool = False) -> List[str]:
    """关闭未关闭的轨道和会话（CD-R/RW、DVD±R、DVD-RW、BD-R）。

    as_needed：只有光驱报告轨道损坏时才动手，否则什么都不写。
    force：光驱没标损坏、但轨道明显没写完（MediaInfo.unfinished）时用。
    -close off：只关会话不封盘，Windows 那边以后还能继续追加。
    """
    return COMMON_ARGS + ["-outdev", xpath(dev), "-close", "on" if not keep_open else "off",
                          "-close_damaged", "force" if force else "as_needed"]


REPAIR_OK = "ok"
REPAIR_NOT_NEEDED = "not_needed"
REPAIR_UNSUPPORTED = "unsupported"
REPAIR_FAILED = "failed"


def repair_result(code: int, output: str) -> str:
    if "Track not marked as damaged" in output:
        return REPAIR_NOT_NEEDED
    if "Cannot close damaged track on given media type" in output:
        return REPAIR_UNSUPPORTED
    return REPAIR_OK if code == 0 else REPAIR_FAILED


# ---------------------------------------------------------------- 进度

_WRITING_RE = re.compile(r"Writing:\s+\d+s\s+([\d.]+)%")
_DONE_RE = re.compile(r"([\d.]+)%\s+done")

STAGE_HINTS = [
    ("Loading ISO image tree", "正在读取光盘原有目录……"),
    ("files added", "正在整理文件……"),
    ("Thank you for being patient", "正在准备刻录……"),
    ("Blanking", "正在擦除……"),
    ("Formatting", "正在格式化……"),
    ("Writing:", "正在写入……"),
    ("Closing track/session", "正在关闭会话……"),
    ("completed successfully", "写入完成"),
    ("Re-assessing", "正在校验……"),
    ("checksums match", "校验通过"),
    ("Mismatch detected", "校验失败"),
]


def parse_progress(line: str) -> Tuple[Optional[float], Optional[str]]:
    """返回 (百分比或 None, 阶段提示或 None)。"""
    m = _WRITING_RE.search(line) or _DONE_RE.search(line)
    pct = float(m.group(1)) if m else None
    hint = None
    for key, text in STAGE_HINTS:
        if key in line:
            hint = text
            break
    return pct, hint


def md5_mismatch(output: str) -> bool:
    return "Mismatch detected" in output or "MD5 MISMATCH" in output


# ---------------------------------------------------------------- 诊断

class Track(object):
    """`-toc` 的 TOC layout 一行：ISO session / Other session / ... track"""

    def __init__(self, kind: str, index: int, start: int, blocks: int, volid: str):
        self.kind = kind
        self.index = index
        self.start = start
        self.blocks = blocks
        self.volid = volid

    @property
    def kind_text(self) -> str:
        what = "会话" if self.kind.endswith("session") else "轨道"
        if self.kind.startswith("ISO"):
            return "ISO9660 " + what
        return "其他格式" + what + "（UDF/音频等）"


_TRACK_RE = re.compile(
    r"^([A-Za-z]+ (?:session|track))\s*:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)s\s*,?\s*(.*?)\s*$")


def parse_tracks(text: str) -> List[Track]:
    tracks, seen = [], set()
    for line in text.splitlines():
        m = _TRACK_RE.match(line)
        if m:
            key = m.group(0)
            if key in seen:      # 有的版本会把 TOC 打印两遍
                continue
            seen.add(key)
            tracks.append(Track(m.group(1), int(m.group(2)), int(m.group(3)),
                                int(m.group(4)), m.group(5)))
    return tracks


def diag_toc_args(dev: str) -> List[str]:
    return COMMON_ARGS + ["-abort_on", "NEVER"] + READ_ACCESS + \
        ["-joliet", "on", "-indev", xpath(dev), "-toc"]


def _decode_udev_enc(s: str) -> str:
    """blkid -o udev 的值：老版本（麒麟的 2.27/2.34）把非 ASCII 字节写成 \\xNN，
    新版本（2.39）直接输出 UTF-8；两种都按字节还原再整体按 UTF-8 解码。"""
    out = bytearray()
    for part in re.split(r"(\\x[0-9a-fA-F]{2})", s):
        if re.match(r"^\\x[0-9a-fA-F]{2}$", part):
            out.append(int(part[2:], 16))
        else:
            out += part.encode("utf-8", "surrogateescape")
    return out.decode("utf-8", "replace")


def parse_blkid_udev(text: str) -> Dict[str, str]:
    """解析 `blkid -p -o udev`，返回 type / version / label / usage。"""
    kv = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = _decode_udev_enc(v.strip())
    label = kv.get("ID_FS_LABEL_ENC", kv.get("ID_FS_LABEL", ""))
    return {"type": kv.get("ID_FS_TYPE", ""), "version": kv.get("ID_FS_VERSION", ""),
            "label": label, "usage": kv.get("ID_FS_USAGE", "")}


def find_tool(name: str) -> Optional[str]:
    """普通用户的 PATH 里常常没有 /sbin。"""
    return shutil.which(name) or next(
        (p for p in ("/sbin/" + name, "/usr/sbin/" + name) if os.access(p, os.X_OK)), None)


def probe_blkid(dev: str) -> Tuple[Dict[str, str], str]:
    blkid = find_tool("blkid")
    if not blkid:
        return {}, "（系统没有 blkid）"
    try:
        p = subprocess.run([blkid, "-p", "-o", "udev", dev], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return {}, str(e)
    out = p.stdout.decode("utf-8", "replace")
    return (parse_blkid_udev(out) if p.returncode == 0 else {}), out


def scan_vrs(path: str, start: int = 0) -> List[str]:
    """读会话开头第 16 扇区起的卷识别序列：CD001=ISO9660，BEA01/NSR02/NSR03/TEA01=UDF。
    start 是会话的起始扇区，默认看盘的开头。"""
    ids = []
    try:
        # Windows 的 \\.\G: 只能按整扇区读
        with open(path, "rb", buffering=0) as f:
            for sector in range(start + 16, start + 32):
                f.seek(sector * SECTOR)
                block = f.read(SECTOR)
                if len(block) < 7:
                    break
                ident = block[1:6].decode("ascii", "replace")
                if ident not in ("CD001", "CDW02", "BEA01", "NSR02", "NSR03", "TEA01", "BOOT2"):
                    break
                ids.append(ident)
                if ident == "TEA01":
                    break
    except OSError:
        pass
    return ids


def udf_revision(path: str, start: int = 0) -> str:
    """从逻辑卷描述符的域标识读 UDF 版本，例如 "2.01"；读不到返回 ""。
    光盘的块大小是 2048；mkudffs 做的硬盘镜像默认是 512，两种都试。
    start：UDF 所在会话的起始扇区（前面还有别的会话时）。"""
    try:
        with open(path, "rb", buffering=0) as f:
            for bs in ((SECTOR,) if start else (SECTOR, 512)):
                rev = _udf_revision(f, bs, start)
                if rev:
                    return rev
    except (OSError, struct.error):
        pass
    return ""


def find_udf_session(path: str, tracks: List["Track"]) -> Optional[int]:
    """非 ISO 的会话里第一个以 UDF 卷识别序列开头的，返回它的起始扇区。
    Windows 实时文件系统每弹出一次都多一个会话，但它们是同一个 UDF，从第一个挂载即可。"""
    for t in tracks:
        if t.kind.startswith("Other"):
            if any(x in scan_vrs(path, t.start) for x in ("NSR02", "NSR03")):
                return t.start
    return None


def _read_block(f, bs: int, block: int) -> bytes:
    # Windows 的 \\.\G: 只能按整扇区读，512 字节的块也按 2048 读出来再截取
    pos = block * bs
    f.seek(pos - pos % SECTOR)
    data = f.read(SECTOR)
    return data[pos % SECTOR:pos % SECTOR + bs]


def _udf_revision(f, bs: int, start: int = 0) -> str:
    avdp = _read_block(f, bs, start + 256)  # 锚点卷描述符指针（AVDP），位置从会话开头算
    if len(avdp) < 24 or struct.unpack("<H", avdp[:2])[0] != 2:
        return ""
    length, loc = struct.unpack("<II", avdp[16:24])
    for i in range(min(length // bs, 64)):
        d = _read_block(f, bs, loc + i)
        tag = struct.unpack("<H", d[:2])[0] if len(d) >= 2 else 8
        if tag == 6 and len(d) >= 250:      # 逻辑卷描述符，域标识在偏移 216
            if d[217:240].rstrip(b"\0") == b"*OSTA UDF Compliant":
                rev = struct.unpack("<H", d[240:242])[0]
                return "%x.%02x" % (rev >> 8, rev & 0xff)
        if tag == 8:                            # 终止描述符
            break
    return ""


def toc_offers_joliet(toc: str) -> bool:
    return any(l.startswith("ISO offers") and "Joliet" in l for l in toc.splitlines())


def probe_fs_direct(dev: str, toc: str, vrs: List[str]) -> Dict[str, str]:
    """不靠 blkid，直接读盘识别文件系统（Windows 下用）。"""
    fs = {}
    if any(x in vrs for x in ("NSR02", "NSR03")):
        fs = {"type": "udf", "version": udf_revision(dev)}
    elif "CD001" in vrs:
        fs = {"type": "iso9660", "version": "Joliet Extension" if toc_offers_joliet(toc) else ""}
    if IS_WINDOWS and is_drive(dev):
        from . import winsys
        info = winsys.volume_info(drive_letter(dev))
        if info:
            label, name = info
            if not fs:      # 读不了原始扇区时，至少用 Windows 认出的文件系统
                fs = {"type": {"CDFS": "iso9660", "UDF": "udf"}.get(name.upper(), name.lower()),
                      "version": ""}
            fs["label"] = label
    if fs and not fs.get("label"):
        fs["label"] = parse_toc(toc).volume_id
    return fs


def describe_fs(fs: Dict[str, str], vrs: List[str]) -> str:
    t, ver = fs.get("type", ""), fs.get("version", "")
    has_iso, has_udf = "CD001" in vrs, any(x in vrs for x in ("NSR02", "NSR03"))
    if t == "udf":
        text = "UDF %s" % ver if ver else "UDF"
        return text + ("，同时带 ISO9660（桥接格式）" if has_iso else "")
    if t == "iso9660":
        text = "ISO9660"
        if "Joliet" in ver:
            text += " + Joliet（Windows 可正常显示中文名）"
        return text + ("，同时带 UDF（桥接格式）" if has_udf else "")
    if t:
        return t
    if has_udf:
        return "UDF %s" % ("1.02/1.50" if "NSR02" in vrs else "2.00 或更高")
    if has_iso:
        return "ISO9660"
    return ""


def kernel_udf_support() -> str:
    """'loaded' / 'module' / 'missing'；Windows 自带 UDF 支持，返回 'windows'"""
    if IS_WINDOWS:
        return "windows"
    try:
        with open("/proc/filesystems") as f:
            if any(l.split()[-1] == "udf" for l in f if l.strip()):
                return "loaded"
    except OSError:
        pass
    rel = platform.release()
    for base in ("/lib/modules", "/usr/lib/modules"):
        d = os.path.join(base, rel, "kernel", "fs", "udf")
        if os.path.isdir(d) and any(n.startswith("udf.ko") for n in os.listdir(d)):
            return "module"
        try:
            with open(os.path.join(base, rel, "modules.builtin")) as f:
                if "fs/udf/" in f.read():
                    return "loaded"
        except OSError:
            pass
    return "missing"


def kernel_capacity(dev: str) -> Optional[int]:
    """系统内核看到的光盘容量（字节）。会话没关好时常常是 0。"""
    name = os.path.basename(os.path.realpath(dev))
    try:
        with open("/sys/class/block/%s/size" % name) as f:
            return int(f.read().strip()) * 512
    except (OSError, ValueError):
        return None


def _unescape_mount(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def mount_info(dev: str) -> Optional[Tuple[str, str, str]]:
    """(挂载点, 文件系统, 挂载选项)，未挂载返回 None。
    Windows 自动挂载光盘：认出文件系统就算已挂载，挂载点是盘符根目录。"""
    if IS_WINDOWS:
        from . import winsys
        letter = drive_letter(dev)
        info = winsys.volume_info(letter) if letter else None
        return (letter + ":\\", info[1], "") if info else None
    try:
        real = os.path.realpath(dev)
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0].startswith("/dev/") and \
                        os.path.realpath(parts[0]) == real:
                    return _unescape_mount(parts[1]), parts[2], parts[3]
    except OSError:
        pass
    return None


def has_udf_history(dev: str, media: MediaInfo) -> bool:
    """识别当前或早先的 UDF，避免把新 ISO 会话误认为 Windows 已切换的目录。"""
    if not is_drive(dev) or not media.present or media.blank:
        return False
    mount = mount_info(dev)
    if mount and mount[1].lower() == "udf":
        return True
    if any(i in scan_vrs(dev) for i in ("NSR02", "NSR03")):
        return True
    return bool(media.other_sessions and find_udf_session(dev, parse_tracks(media.raw)) is not None)


def is_current_udf_bridge(dev: str, media: MediaInfo) -> bool:
    """区别同一会话的 UDF 桥接目录与只留在早先会话里的 UDF。"""
    return bool(is_drive(dev) and media.present and media.has_iso
                and media.last_iso_start is not None and any(
                    i in scan_vrs(dev, media.last_iso_start) for i in ("NSR02", "NSR03")))


INFO, OK, WARN, ERROR = "info", "ok", "warn", "error"

MIGRATE_HINT = ("请先把全部可读文件复制到本机，再换空白光盘刻录成 ISO9660。"
                "向含 UDF 的原盘追加 ISO 会话，Windows 仍可能显示旧 UDF 目录，不能当作文件系统转换。")


class Diagnosis(object):
    def __init__(self, dev: str):
        self.dev = dev
        self.is_image = not is_drive(dev)
        self.media = MediaInfo()
        self.tracks = []          # type: List[Track]
        self.fs = {}              # type: Dict[str, str]
        self.vrs = []             # type: List[str]
        self.fs_text = ""
        self.kernel_udf = ""
        self.kernel_bytes = None  # type: Optional[int]
        self.mount = None         # type: Optional[Tuple[str, str, str]]
        self.findings = []        # type: List[Tuple[str, str]]
        self.raw = []             # type: List[Tuple[str, str]]
        self.short_names = []     # type: List[str]  最新目录树里只有短名的文件
        # 前面是 ISO、后面接着写了 UDF 时，UDF 部分的起始扇区和版本
        self.udf_start = None     # type: Optional[int]
        self.udf_version = ""

    @property
    def iso_sessions(self) -> int:
        return sum(1 for t in self.tracks if t.kind == "ISO session")

    @property
    def can_recover_names(self) -> bool:
        """有短名、有早先的会话可查、还能追加。"""
        return (bool(self.short_names) and self.iso_sessions >= 2
                and self.media.appendable and not self.media.needs_repair and not self.media.udf_history)

    @property
    def can_mount(self) -> bool:
        # Windows 自己挂载光盘，认不出文件系统时也没有别的办法挂上
        if IS_WINDOWS or self.is_image or self.media.blank:
            return False
        if self.udf_start is not None:      # 系统挂上的是前面的 ISO 部分时，也要能改挂 UDF 部分
            return self.mount is None or self.mount[1] != "udf"
        return self.mount is None and bool(self.fs_text)

    def info_rows(self) -> List[Tuple[str, str]]:
        m = self.media
        if self.is_image:   # xorriso 给镜像文件的是虚拟光驱名，没意义
            rows = [("镜像文件", self.dev)]
        else:
            rows = [("光驱", self.dev + ("　" + m.drive_desc if m.drive_desc else ""))]
        if not m.present:
            rows.append(("光盘", "光驱未返回盘片信息"))
            if self.fs_text:
                rows += [("文件系统", self.fs_text), ("卷标", self.fs.get("label") or "—")]
            if not self.is_image:
                rows.append(("Windows 识别" if IS_WINDOWS else "挂载状态",
                             "已挂载到 %s（%s）" % (self.mount[0], self.mount[1])
                             if self.mount else "未挂载"))
            return rows
        rows += [("光盘类型", "ISO 镜像文件" if self.is_image else m.profile),
                 ("盘片制造商", m.media_product or "—"),
                 ("盘片状态", m.status_text() + "（%s）" % m.status if m.status else m.status_text()),
                 ("会话 / 轨道", "%d 个" % len(self.tracks)),
                 ("已用 / 剩余", "%s / %s" % (human_size(m.used_bytes), human_size(m.free_bytes))),
                 ("文件系统", self.fs_text or "未识别"),
                 ("卷标", self.fs.get("label") or m.volume_id or "—")]
        if m.has_iso:
            rows.append(("只剩短名的文件", "%d 个" % len(self.short_names) if self.short_names else "无"))
        if not self.is_image:
            if IS_WINDOWS:
                # Windows 的光盘卷大小只算最后一个会话，不能当“可读容量”看，不显示
                rows.append(("Windows 识别", "%s（%s）" % (self.mount[0], self.mount[1])
                             if self.mount else "没有识别出文件系统"))
                return rows
            rows.append(("系统可读容量", "—" if self.kernel_bytes is None
                         else human_size(self.kernel_bytes)))
            rows.append(("内核 UDF 支持", {"loaded": "已启用", "module": "有（未加载，挂载时自动加载）",
                                       "missing": "没有"}.get(self.kernel_udf, "未知")))
            rows.append(("挂载状态", "已挂载到 %s（%s）" % (self.mount[0], self.mount[1])
                         if self.mount else "未挂载"))
        return rows

    def report_text(self) -> str:
        lines = ["光盘诊断报告（光盘刻录助手）", ""]
        lines += ["%s：%s" % r for r in self.info_rows()]
        if self.tracks:
            lines += ["", "会话 / 轨道："]
            lines += ["  %-14s #%d  起始扇区 %d  大小 %s  %s" %
                      (t.kind, t.index, t.start, human_size(t.blocks * SECTOR), t.volid)
                      for t in self.tracks]
        lines += ["", "诊断结论："] + ["  [%s] %s" % f for f in self.findings]
        for name, text in self.raw:
            lines += ["", "==== %s ====" % name, text.rstrip()]
        return "\n".join(lines) + "\n"


def diagnose(dev: str) -> Diagnosis:
    """读取光盘的全部信息并给出结论。会阻塞数秒（光驱起转），请在后台线程调用。"""
    d = Diagnosis(dev)
    _, toc = run_xorriso(diag_toc_args(dev), timeout=180)
    d.raw.append(("xorriso -toc", toc))
    if indev_failed(toc):
        _, toc = run_xorriso(outdev_toc_args(dev), timeout=180)
        d.raw.append(("xorriso -outdev -toc（盘上目录读不出，改为只读光驱状态）", toc))
    d.media = parse_toc(toc)
    if len(d.raw) > 1:
        d.media.tree_unreadable = True
    d.tracks = parse_tracks(toc)
    if not d.is_image:
        d.mount = mount_info(dev)
        d.kernel_udf = kernel_udf_support()
        if not IS_WINDOWS:
            d.kernel_bytes = kernel_capacity(dev)
    # xorriso 读不到盘片信息时（光驱被别的程序占用等）也照样识别文件系统，能识别就还能挂载
    if (d.media.present and not d.media.blank) or \
            (not d.media.present and (is_drive(dev) or os.path.exists(dev))):
        d.vrs = scan_vrs(dev)
        d.raw.append(("卷识别序列（第 16 扇区起）", " ".join(d.vrs) or "（读不到）"))
        if IS_WINDOWS:
            d.fs = probe_fs_direct(dev, toc, d.vrs)
            d.raw.append(("文件系统", repr(d.fs)))
        else:
            d.fs, blk = probe_blkid(dev)
            d.raw.append(("blkid -p -o udev", blk))
        d.fs_text = describe_fs(d.fs, d.vrs)
        if d.media.last_other:
            # 盘开头的识别只看到第一个会话；后面的会话要一个个看
            # 整张都是 UDF 时也记下来：Windows 实时文件系统的多会话盘，Linux 默认从
            # 最后一个会话找 UDF，常常挂不上，要从第一个会话挂
            d.udf_start = find_udf_session(dev, d.tracks)
            if d.udf_start:
                d.udf_version = udf_revision(dev, d.udf_start)
                d.raw.append(("后面会话的卷识别序列（第 %d 扇区起）" % d.udf_start,
                              " ".join(scan_vrs(dev, d.udf_start))))
                udf = "UDF %s" % d.udf_version if d.udf_version else "UDF"
                where = "从第 %d 扇区起" % d.udf_start
                d.fs_text = ("%s（前面的会话）+ %s（%s）" % (d.fs_text, udf, where)
                             if d.fs_text else "%s（%s）" % (udf, where))
    if d.media.has_iso and not d.media.last_other:
        _, cur = run_xorriso(session_files_args(dev), timeout=300)
        d.short_names = sorted(p for p in parse_report_lba(cur) if is_short_name(os.path.basename(p)))
    d.media.udf_history = has_udf_history(dev, d.media)
    d.media.udf_bridge = is_current_udf_bridge(dev, d.media)
    if d.media.udf_bridge and not d.media.last_other:
        d.udf_start = d.media.last_iso_start
    d.findings = _conclude(d)
    return d


def _conclude(d: Diagnosis) -> List[Tuple[str, str]]:
    m, out = d.media, []
    if m.udf_history:
        out.append((WARN, "盘上存在 UDF。Windows 挂载的目录可能与最新 ISO 目录不同，原盘追加已停用。"))
    if not m.present:
        if not d.fs_text:
            return [(WARN, "没有检测到光盘，或光驱读不出这张盘。")]
        out.append((WARN, "光驱没有返回盘片信息（可能正被其他程序占用），"
                          "但系统识别出了文件系统，可以尝试挂载读取。"))
    if m.needs_repair:
        text = ("有会话没有关闭，所以系统读不出内容。通常是 Windows 用“类似 USB 闪存驱动器”"
                "方式刻录后没有正常弹出，或刻录、格式化中途失败。点“修复未关闭会话”关闭它，"
                "已有数据不受影响。")
        if m.unfinished and not m.damaged:
            text += ("（光驱没有标记损坏，但已写 %d 个扇区、只能读出 %d 个，且给不出目录，"
                     "修复时会强制关闭。）" % (m.overall_blocks - m.writable_blocks, m.readable_blocks))
        out.append((ERROR, text))
    if m.blank:
        out.append((INFO, "这是一张空白盘，可以直接刻录。"))
        return out
    if m.iso_hidden:
        iso_n = sum(1 for t in d.tracks if t.kind == "ISO session")
        other_n = sum(1 for t in d.tracks if t.kind.startswith("Other"))
        if d.udf_start is not None:
            how = ("Windows 资源管理器里打开的就是 UDF 部分。" if IS_WINDOWS else
                   "点“挂载并打开”读取 UDF 部分（要输入管理员密码）。")
            out.append((WARN, "这张盘前 %d 个会话是 ISO9660，后面 %d 个会话是 UDF%s，"
                              "一般是 Windows 用“类似 USB 闪存驱动器”方式接着写的，后来加的文件都在 UDF 部分里。"
                              "系统和本软件默认只能读到前面 ISO 部分的内容。%s"
                              % (iso_n, other_n, "（%s）" % d.udf_version if d.udf_version else "", how)))
        else:
            out.append((WARN, "这张盘前 %d 个会话是 ISO9660，后面 %d 个会话是认不出的格式，"
                              "系统和本软件只能读到前面 ISO 部分的内容。" % (iso_n, other_n)))
        if d.udf_start is None:
            out.append((WARN, "不要往这张盘追加：认不出后面的会话，新写的会话会盖住它们。"))
        elif m.appendable:
            out.append((INFO, MIGRATE_HINT))
    fs_type = d.fs.get("type", "")
    is_udf = fs_type == "udf" or (not fs_type and any(x in d.vrs for x in ("NSR02", "NSR03")))
    if is_udf:
        out.append((INFO, "这是 UDF 格式的盘（%s），一般是 Windows 用“类似 USB 闪存驱动器”方式刻录的。"
                          "%s。" % (d.fs_text, "可以在资源管理器里直接打开" if IS_WINDOWS
                                    else "可以挂载后直接读取")))
        if m.appendable:
            out.append((INFO, MIGRATE_HINT))
        if d.kernel_udf == "missing":
            out.append((ERROR, "系统内核没有 UDF 支持，无法挂载这张盘。请联系管理员。"))
    elif m.iso_hidden:
        pass
    elif d.fs_text.startswith("ISO9660"):
        if d.short_names:
            text = ("有 %d 个文件只剩短文件名（如 %s），Windows 和麒麟上都显示成这样。"
                    % (len(d.short_names), os.path.basename(d.short_names[0])))
            if d.can_recover_names:
                text += "早先的会话里可能还留着原来的中文名，点“恢复长文件名”查找并改回。"
            elif d.iso_sessions < 2:
                text += "盘上没有更早的会话，原名无法找回。"
            out.append((WARN, text))
        elif m.udf_history:
            out.append((WARN, "已识别 ISO 目录，但 Windows 可能仍挂载旧 UDF；请复制后换新盘。"))
        elif "Joliet" in d.fs_text:
            out.append((OK, "标准数据盘（%s），Windows 和麒麟都能正常读取。" % d.fs_text))
        else:
            out.append((WARN, "标准数据盘，但没有 Joliet 文件名，Windows 上中文名会变成下划线。"
                              "如果盘还能追加，用本软件追加任意一个文件即可修正全部文件名。"))
    elif m.tree_unreadable and not m.needs_repair:
        out.append((WARN, "光驱能看到盘，但读不出盘上的目录，可能是刻录失败或盘片损坏。"))
    elif not m.needs_repair:
        if any(t.kind.startswith("Other") for t in d.tracks) and not d.vrs:
            out.append((WARN, "没有识别出文件系统。可能是音频 CD、视频 DVD 以外的特殊格式，"
                              "或刻录中途失败的盘。"))
        else:
            out.append((WARN, "没有识别出文件系统。"))
    if not d.is_image and d.kernel_bytes == 0 and m.present and not m.needs_repair:
        out.append((WARN, "系统读到的容量是 0：内核读不出这张盘的目录。如刚刻录完，弹出再放入试试。"))
    if m.closed and m.readable_blocks == 0 and not d.fs_text:
        # 实测：格式化失败、轨道没关的 DVD+R，光驱重新上电后会自己把它关掉，结果就是这样
        out.append((ERROR, "盘片已被关闭，但上面没有能读出的数据：之前的刻录或格式化没有成功，"
                           "光驱又把没写完的轨道关掉了。这张盘已无法使用，也无法修复，请换盘。"))
    elif m.closed:
        out.append((INFO, "盘片已封盘，只能读取，不能再写入。"))
    elif m.appendable and not m.needs_repair and not is_udf and not m.foreign_data and not m.udf_history:
        out.append((OK, "盘片可追加，剩余 %s。" % human_size(m.free_bytes)))
    if d.mount:
        out.append((OK, ("Windows 已识别出这张盘（%s），可以直接打开。" if IS_WINDOWS
                         else "已挂载到 %s，可以直接打开。") % d.mount[0]))
    return out


# ---------------------------------------------------------------- 恢复长文件名
#
# 典型过程：麒麟刻了一个没有 Joliet 的会话（Rock Ridge 里有中文长名），之后 Windows
# 往同一张盘追加时只认 ISO9660 短名（2023__~1.XLS），就把短名写进了自己的 Joliet 树，
# 从此 Windows 和 Linux 看到的都是短名。但文件数据在多会话盘上是共用的，早先会话里
# 位置相同（起始扇区 + 字节数一致）的文件还带着原名，据此可以把名字改回去：
# 只追加一个新目录树，文件内容不重写。

_LBA_RE = re.compile(r"^File data lba:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*)'\s*$")
# 只由 ISO9660 允许的字符组成、带 _ 或 ~ 的名字：麒麟/xorriso（2023_____.XLS）
# 和 Windows（2023__~1.XLS）把中文等字符换掉后的样子
_SHORT_RE = re.compile(r"^[A-Z0-9_~]+(\.[A-Z0-9_~]*)?(;\d+)?$")


def is_short_name(name: str) -> bool:
    return bool(_SHORT_RE.match(name)) and ("_" in name or "~" in name)


def parse_report_lba(text: str) -> Dict[str, Tuple[int, int]]:
    """`-find / -type f -exec report_lba --` → {路径: (起始扇区, 字节数)}，只取第一个区段。"""
    out = {}
    for line in text.splitlines():
        m = _LBA_RE.match(line)
        if m and m.group(1) == "0":
            out[_unquote(m.group(5))] = (int(m.group(2)), int(m.group(4)))
    return out


def session_files_args(dev: str, sbsector: Optional[int] = None) -> List[str]:
    args = COMMON_ARGS + ["-abort_on", "NEVER"] + READ_ACCESS + ["-joliet", "on"]
    if sbsector is not None:
        args += ["-load", "sbsector", str(sbsector)]
    return args + ["-indev", xpath(dev), "-find", "/", "-type", "f", "-exec", "report_lba", "--"]


class Rename(object):
    def __init__(self, old: str, new: str, session: int):
        self.old = old
        self.new = new
        self.session = session      # 原名来自起始扇区为此值的会话

    def __repr__(self):
        return "Rename(%r -> %r @%d)" % (self.old, self.new, self.session)


class RecoveryPlan(object):
    def __init__(self):
        self.renames = []       # type: List[Rename]
        self.unresolved = []    # type: List[str]  短名但找不到原名
        self.fixups = []        # type: List[Rename]  去掉“;1”版本号（仅老版本 xorriso）
        self.sessions = 0       # 查找过的早先会话数


def plan_name_recovery(dev: str, progress=None) -> RecoveryPlan:
    """读出各个会话的文件位置，给最新目录树里的短名找回原名。会读盘很多次，较慢。"""
    plan = RecoveryPlan()
    _, toc = run_xorriso(diag_toc_args(dev), timeout=180)
    starts = [t.start for t in parse_tracks(toc) if t.kind == "ISO session"]
    _, cur = run_xorriso(session_files_args(dev), timeout=300)
    latest = parse_report_lba(cur)
    short = {p: k for p, k in latest.items() if is_short_name(os.path.basename(p))}
    if not short:
        return plan
    found = {}                      # (lba, size) -> (原名, 会话)
    ambiguous = set()               # 同一会话里几个不同的名字指向同一份数据
    older = starts[:-1]
    for i, start in enumerate(reversed(older)):        # 从新到旧，越新的原名越优先
        if progress:
            progress("正在读取早先的会话 %d / %d（起始扇区 %d）……" % (i + 1, len(older), start))
        _, out = run_xorriso(session_files_args(dev, start), timeout=300)
        names = {}
        for path, key in parse_report_lba(out).items():
            # 空文件在 ISO 上都指向同一个扇区，按位置匹配不出来
            if key[1] == 0 or is_short_name(os.path.basename(path)):
                continue
            names.setdefault(key, set()).add(strip_version(os.path.basename(path)))
        for key, candidates in names.items():
            if key in found or key in ambiguous:
                continue
            if len(candidates) > 1:
                ambiguous.add(key)
            else:
                found[key] = (candidates.pop(), start)
        plan.sessions += 1
    shared = {}                     # 最新目录里几个文件共用同一份数据时也不猜
    for key in latest.values():
        shared[key] = shared.get(key, 0) + 1
    taken = set(latest)
    for path in sorted(short):
        key = short[path]
        hit = found.get(key)
        if not hit or shared[key] > 1:
            plan.unresolved.append(path)
            continue
        new = disc_join(os.path.dirname(path), hit[0])
        if new in taken:            # 同一目录下已有这个名字，不覆盖
            plan.unresolved.append(path)
            continue
        taken.add(new)
        plan.renames.append(Rename(path, new, hit[1]))
    # 老版本 xorriso 下，其他文件名也带着“;1”，一并改干净，否则新会话里 Windows 显示成 xxx_1
    plan.fixups = version_fixups(set(latest), skip={r.old for r in plan.renames} | set(plan.unresolved))
    return plan


def rename_args(dev: str, renames: List[Rename], close: bool = False, fixups=(),
                speed: str = "") -> List[str]:
    """追加一个只改名字的会话。-move 正好两个参数，不会吞掉后面的命令。"""
    args = COMMON_ARGS + FS_ARGS + ["-dev", xpath(dev)]
    if speed:
        args += ["-speed", speed]
    args += ["-close", "on" if close else "off"]
    for r in list(renames) + list(fixups):
        args += ["-move", r.old, r.new]
    return args + ["-commit"]


def hide_args(dev: str, disc_paths: List[str], speed: str = "") -> List[str]:
    """“删除”盘上的文件：追加一个不含这些文件的新会话。一次性光盘写过的地方改不了，
    所以只是看不到了，空间不会释放，早先的会话里还留着（和 Windows 在 DVD+R 上删除 UDF 文件一样）。"""
    args = COMMON_ARGS + FS_ARGS + ["-dev", xpath(dev)]
    if speed:
        args += ["-speed", speed]
    args += ["-close", "off", "-rm_r"] + list(disc_paths) + ["--"]
    return args + ["-commit"]


def stage_udf(src_root: str, dest: str) -> Tuple[int, str]:
    """把挂载着的 UDF 部分整个复制到本机临时目录。写盘时光驱要独占，
    不能一边从挂载目录读一边刻，所以先读出来。"""
    tops = [os.path.join(src_root, n) for n in sorted(os.listdir(src_root))]
    code, msg = copy_local(tops, dest)
    if code == 0:
        make_writable([dest])
    return code, msg


# ---------------------------------------------------------------- 挂载

def udf_mount_cmd(dev: str, start: int, mountpoint: str) -> List[str]:
    # session= 在 Linux 的 udf 驱动里就是会话的起始扇区
    return ["mount", "-t", "udf", "-o", "ro,session=%d,iocharset=utf8" % start, dev, mountpoint]


def mount_udf_session(dev: str, start: int) -> Tuple[Optional[str], str]:
    """挂载从第 start 扇区开始的 UDF 部分。前面还有 ISO 会话时系统默认只认出 ISO 部分；
    udisks 不接受 session= 选项，只能以管理员身份挂载（会弹出输入密码的窗口）。"""
    manual = ("可以在终端手动挂载：\nsudo mkdir -p /mnt/cd && sudo %s"
              % " ".join(udf_mount_cmd(dev, start, "/mnt/cd")))
    m = mount_info(dev)
    if m and m[1] == "udf":
        return m[0], "已经挂载在 %s" % m[0]
    if m:       # 系统挂着前面的 ISO 部分，同一个设备只能挂一个
        unmount(dev)
        if mount_info(dev):
            return None, "光盘的 ISO 部分正挂载在 %s 且卸载不掉，请先关闭打开它的窗口。" % m[0]
    mountpoint = os.path.join("/media", getpass.getuser(), "光盘-UDF-" + os.path.basename(dev))
    cmd = ["sh", "-c", _UDF_HELPER, "sh", mountpoint, str(start), dev]
    if os.geteuid() != 0:
        pkexec = find_tool("pkexec")
        if not pkexec:
            return None, "系统没有 pkexec，无法以管理员身份挂载。" + manual
        cmd = [pkexec] + cmd
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        first = p.stdout.readline()      # 等用户输完密码、挂载完成
    except OSError as e:
        return None, "%s\n\n%s" % (e, manual)
    if first.strip() == b"MOUNTED" and mount_info(dev):
        _UDF_HELPERS[dev] = p
        return mountpoint, ""
    out = (first + p.stdout.read()).decode("utf-8", "replace").strip()
    p.wait()
    if p.returncode in (126, 127) and os.geteuid() != 0:
        out = out or "没有输入管理员密码，或当前用户没有管理员权限。"
    return None, "%s\n\n%s" % (out or "挂载失败", manual)


# 以管理员身份挂 UDF 部分：pkexec 起一个 root 的 sh，挂好以后一直等着标准输入。
# 本程序退出时（不管是正常关闭还是崩溃）管道一断，它就自己卸载、删掉挂载目录，
# 不会把挂载留在系统里，也不用退出时再输一次密码。
_UDF_HELPER = r'''
mkdir -p "$1" || exit 1
if ! mount -t udf -o "ro,session=$2,iocharset=utf8" "$3" "$1"; then rmdir "$1"; exit 1; fi
echo MOUNTED
read _ || true
umount "$1" 2>/dev/null || umount -l "$1"
rmdir "$1" 2>/dev/null
'''
_UDF_HELPERS = {}   # type: Dict[str, subprocess.Popen]
_OWN_UDISKS = set()  # 本程序用 udisks 挂上的 UDF 盘，退出时卸掉


def release_udf(dev: str) -> bool:
    """卸掉本程序挂的 UDF 部分。返回是否有要卸的。"""
    p = _UDF_HELPERS.pop(dev, None)
    if p is not None:
        try:
            p.stdin.close()
            p.wait(timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        return True
    if dev in _OWN_UDISKS:
        _OWN_UDISKS.discard(dev)
        udisksctl = find_tool("udisksctl")
        if udisksctl and mount_info(dev):
            subprocess.run([udisksctl, "unmount", "-b", dev], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        return True
    return False


def release_all_mounts() -> None:
    """程序退出前调用：本程序挂上的 UDF 全部卸掉。"""
    for dev in list(_UDF_HELPERS) + list(_OWN_UDISKS):
        release_udf(dev)


atexit.register(release_all_mounts)


def mount_disc(dev: str, udf_start: Optional[int] = None) -> Tuple[Optional[str], str]:
    """用 udisks 挂载（普通用户即可，和文件管理器自动挂载的方式一样）。
    返回 (挂载点, 说明)；失败时挂载点为 None，说明里带上手动挂载命令。
    udf_start：前面是 ISO、后面是 UDF 的盘，挂 UDF 部分。"""
    if udf_start and not IS_WINDOWS:
        return mount_udf_session(dev, udf_start)
    m = mount_info(dev)
    if udf_start == 0 and not IS_WINDOWS:
        # 整张 UDF 的盘先照常用 udisks 挂（不用密码），挂不上或挂成别的再指定会话
        if m and m[1] == "udf":
            return m[0], "已经挂载在 %s" % m[0]
        path, msg = mount_disc(dev)
        m = mount_info(dev)
        if path and m and m[1] == "udf":
            _OWN_UDISKS.add(dev)
            return path, msg
        return mount_udf_session(dev, 0)
    if m:
        return m[0], "已经挂载在 %s" % m[0]
    if IS_WINDOWS:
        return None, "Windows 没有识别出这张盘上的文件系统，无法打开。"
    udisksctl = find_tool("udisksctl")
    manual = ("可以在终端手动挂载：\nsudo mkdir -p /mnt/cd && "
              "sudo mount -o ro,iocharset=utf8 %s /mnt/cd" % dev)
    if not udisksctl:
        return None, "系统没有 udisksctl。" + manual
    try:
        p = subprocess.run([udisksctl, "mount", "-b", dev], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=120)
        out = p.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return None, "%s\n\n%s" % (e, manual)
    m = mount_info(dev)
    if m:
        return m[0], out
    hit = re.search(r" at (/.+?)\.?\s*$", out)
    if p.returncode == 0 and hit:
        return hit.group(1), out
    return None, "%s\n\n%s" % (out or "挂载失败", manual)


def unmount_disc(dev: str) -> Tuple[bool, str]:
    if not mount_info(dev):
        return True, "未挂载"
    unmount(dev)
    m = mount_info(dev)
    return (m is None), ("已卸载" if m is None else "卸载失败：%s 仍在使用中，请先关闭打开它的窗口。" % m[0])


def open_folder(path: str) -> bool:
    if IS_WINDOWS:
        from . import winsys
        return winsys.open_folder(path)
    for cmd in ("xdg-open", "peony", "caja", "nautilus"):
        exe = shutil.which(cmd)
        if exe:
            try:
                subprocess.Popen([exe, path], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return True
            except OSError:
                continue
    return False


# ---------------------------------------------------------------- 杂项

def is_mounted(dev: str) -> bool:
    return mount_info(dev) is not None


def unmount(dev: str) -> None:
    """麒麟会自动挂载光盘，挂载状态下 libburn 不能独占光驱写入，写入前先卸载。
    只读不用卸载（见 READ_ACCESS）。Windows 下光驱挂载着也能刻录，不用处理。"""
    if IS_WINDOWS or not dev.startswith("/dev/") or not is_mounted(dev):
        return
    if release_udf(dev) and not is_mounted(dev):
        return
    # 最后一条：以前的版本以管理员身份挂上、没卸掉的 UDF 部分，普通用户卸不掉
    ours = "session=" in (mount_info(dev) or ("", "", ""))[2]
    for cmd in (["udisksctl", "unmount", "-b", dev, "--no-user-interaction"],
                ["umount", dev], ["pkexec", "umount", dev]):
        if cmd[0] == "pkexec" and not ours:
            break
        if find_tool(cmd[0]):
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            if not is_mounted(dev):
                return


def refresh_system_view(dev: str) -> None:
    """写完光盘后让系统重新读目录。Windows 会一直用旧的卷缓存，要卸下卷让它重新挂载；
    麒麟刻录前已经卸载，下次访问自然是新内容。"""
    if IS_WINDOWS and is_drive(dev):
        from . import winsys
        winsys.dismount(drive_letter(dev))


def local_size(path: str) -> Tuple[int, int]:
    """返回 (按扇区取整后的字节数, 文件数)。"""
    def blocks(n):
        return (n + SECTOR - 1) // SECTOR * SECTOR
    if os.path.isfile(path):
        return blocks(os.path.getsize(path)), 1
    total, count = 0, 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += blocks(os.path.getsize(os.path.join(root, f)))
                count += 1
            except OSError:
                pass
    return total, count


def long_names(path: str, limit: int = JOLIET_NAME_MAX) -> List[str]:
    """找出超过 Joliet 长度上限的名字（Windows 上会被截断）。"""
    bad = []
    candidates = [path]
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            candidates += [os.path.join(root, n) for n in dirs + files]
    for p in candidates:
        if len(os.path.basename(p).encode("utf-16-be")) // 2 > limit:
            bad.append(p)
    return bad


def long_disc_names(paths, limit: int = JOLIET_NAME_MAX) -> List[str]:
    """检查光盘目标路径的每一级名称，不把整条路径长度当作文件名长度。"""
    return sorted({p for p in paths if any(
        len(n.encode("utf-16-be")) // 2 > limit for n in p.split("/"))})


class NamePlan(object):
    def __init__(self):
        self.renames = []
        self.final_paths = {}
        self.changes = []


def _utf16_length(name):
    return len(name.encode("utf-16-be")) // 2


def _utf16_prefix(name, limit):
    out, used = [], 0
    for char in name:
        units = _utf16_length(char)
        if used + units > limit:
            break
        out.append(char)
        used += units
    return "".join(out)


def plan_burn_names(entries, items, fixups=(), limit=JOLIET_NAME_MAX):
    """按最终目录树规划长名截短。保留扩展名，重名时加编号；不修改本地文件。"""
    paths = {"/": True}
    fixed = {r.old: r.new for r in fixups}
    def add(path, is_dir):
        path = posixpath.normpath("/" + path.lstrip("/"))
        paths[path] = is_dir
        parent = posixpath.dirname(path)
        while parent and parent not in paths:
            paths[parent] = True
            parent = posixpath.dirname(parent)
        return path
    for e in entries:
        add(fixed.get(e.path, e.path), e.is_dir)
    for src, dst in items:
        dst = add(dst, os.path.isdir(src) and not os.path.islink(src))
        if not paths[dst]:
            continue
        for top, dirs, files in os.walk(src):
            rel = os.path.relpath(top, src)
            parent = dst if rel == "." else posixpath.join(dst, rel.replace(os.sep, "/"))
            for name in dirs + files:
                local = os.path.join(top, name)
                add(posixpath.join(parent, name), os.path.isdir(local) and not os.path.islink(local))

    siblings = {}
    for path in paths:
        if path != "/":
            siblings.setdefault(posixpath.dirname(path), []).append(path)
    new_names = {}
    for parent, children in siblings.items():
        # 短名称先保留，截短的名称不得占用已有名称，Windows 下也不区分大小写重名。
        reserved = {posixpath.basename(p).casefold() for p in children
                    if _utf16_length(posixpath.basename(p)) <= limit}
        used = set()
        for path in sorted(children, key=lambda p: (
                _utf16_length(posixpath.basename(p)) > limit, posixpath.basename(p))):
            name = posixpath.basename(path)
            if _utf16_length(name) <= limit and name.casefold() not in used:
                new = name
            else:
                if _utf16_length(name) <= limit:
                    raise ValueError("Windows 下有不区分大小写的同名项，请先处理：%s" % path)
                stem, ext = (name, "") if paths[path] else posixpath.splitext(name)
                if _utf16_length(ext) >= limit:
                    raise ValueError("扩展名过长，无法保留完整扩展名：%s" % path)
                number = 1
                while True:
                    suffix = "" if number == 1 else " (%d)" % number
                    room = limit - _utf16_length(ext + suffix)
                    if room < 1:
                        raise ValueError("无法为文件生成不重名的短名称：%s" % path)
                    base = _utf16_prefix(stem, room).rstrip(" .") or "_"
                    new = base + suffix + ext
                    if new.casefold() not in used and new.casefold() not in reserved:
                        break
                    number += 1
            new_names[path] = new
            used.add(new.casefold())

    plan = NamePlan()
    plan.final_paths["/"] = "/"
    for path in sorted(new_names, key=lambda p: (p.count("/"), p)):
        parent = posixpath.dirname(path)
        final = posixpath.join(plan.final_paths[parent], new_names[path])
        plan.final_paths[path] = final
        if final != path:
            plan.changes.append((path, final))
        if new_names[path] != posixpath.basename(path):
            plan.renames.append(Rename(path, posixpath.join(parent, new_names[path]), -1))
    # 子项先改名，最后再移动父目录，避免旧路径提前失效。
    plan.renames.sort(key=lambda r: (-r.old.count("/"), r.old))
    return plan


def estimate_overhead(n_files: int) -> int:
    """估算目录树 + 会话开销：三套目录树（ISO/Joliet/RR），
    外加一次性介质每个新会话约 7MB 的引入/引出区。"""
    return n_files * 3 * 256 + 8 * 1024 * 1024


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n
