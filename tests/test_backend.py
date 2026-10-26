# -*- coding: utf-8 -*-
"""后端测试。解析部分随处可跑；集成部分需要 PATH 中有 xorriso，
对 ISO 镜像文件做真实的多次追加刻录（不需要光驱）。

    python3 -m unittest discover -s tests -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discburner import backend as B  # noqa: E402

# 拼参数时用的光驱地址（Windows 版是 \\.\G: 这种形式）
DEV = B.windows_dev("G") if B.IS_WINDOWS else "/dev/sr0"


def have_xorriso():
    try:
        return bool(B.find_xorriso())
    except B.XorrisoNotFound:
        return False

TOC_BLANK = """\
Drive current: -dev '/dev/sr0'
Drive type   : vendor 'HL-DT-ST' product 'DVDRAM GP65NB60' revision 'PF00'
Media current: DVD+R
Media product: CMC MAG E01 , CMC Magnetics Corporation
Media status : is blank
Media blocks :        0 readable ,  2295104 writable ,  2295104 overall
Media summary: 0 sessions, 0 data blocks, 0 data, 4483m free
"""

TOC_APPENDABLE = """\
Drive current: -dev '/dev/sr0'
Media current: DVD+R
Media status : is written , is appendable
Media blocks :   117888 readable ,  2177216 writable ,  2295104 overall
TOC layout   : Idx ,  sbsector ,       Size , Volume Id
ISO session  :   1 ,         0 ,    117440s , 资料盘
ISO session  :   2 ,    117888 ,        30s , 资料盘
Media summary: 2 sessions, 117470 data blocks,  229m data, 4252m free
Volume id    : '资料盘'
"""

TOC_CLOSED = """\
Media current: CD-R
Media status : is written , is closed
Media blocks :   300000 readable ,        0 writable ,   359844 overall
TOC layout   : Idx ,  sbsector ,       Size , Volume Id
ISO session  :   1 ,         0 ,    299850s , KYLIN
Media summary: 1 session, 299850 data blocks,  586m data, 0 free
Volume id    : 'KYLIN'
"""

TOC_FOREIGN = """\
Media current: DVD-R sequential recording
Media status : is written , is appendable
Media blocks :    20000 readable ,  2200000 writable ,  2297888 overall
TOC layout   : Idx ,  sbsector ,       Size , Volume Id
Other session:   1 ,         0 ,     20000s ,
Media summary: 1 session, 20000 data blocks, 39.1m data, 4296m free
"""

# Windows“类似 USB 闪存驱动器”方式刻录后没正常弹出的 DVD+R
TOC_DAMAGED = """\
Media current: DVD+R
Media status : is written , is appendable , but next track is damaged
Media blocks :    20000 readable ,  2200000 writable ,  2295104 overall
TOC layout   : Idx ,  sbsector ,       Size , Volume Id
Other session:   1 ,         0 ,     20000s ,
Media summary: 1 session, 20000 data blocks, 39.1m data, 4296m free
"""

TOC_INCOMPLETE_CD = """\
Media current: CD-R
Media status : is written , is appendable
Media blocks :    20000 readable ,   300000 writable ,   359844 overall
Incomplete session encountered !
"""

# 真实输出：Windows 把空白 DVD+R 格式化为 UDF 2.01 时失败，轨道写了 288 扇区没关闭。
# 光驱没标 damaged；-indev 拿不到光驱，只有 -outdev 能读出状态。
INDEV_FAILED = """\
xorriso : FAILURE : Cannot read ISO image tree
xorriso : NOTE : Tolerated problem event of severity 'FAILURE'
xorriso : FAILURE : No input drive acquired on attempt to print Table Of Content
"""
TOC_UNFINISHED = """\
Drive current: -outdev '/dev/sr0'
Drive access : shared:readonly
Media current: DVD+R
Media status : is written , is appendable
Drive current: -outdev '/dev/sr0'
Drive access : shared:readonly
Drive type   : vendor 'USBDVD' product 'DVDRW-EXT' revision '1.00'
Drive id     : 'SN0000000000'
Media current: DVD+R
Media product: MCC/004/48 , Mitsubishi Chemical Corporation
Media status : is written , is appendable
Media blocks : 1 readable , 2294816 writable , 2295104 overall
TOC layout   : Idx ,  sbsector ,       Size , Volume Id
xorriso : SORRY : Cannot obtain Table Of Content
"""
# 真实输出：上面那张盘拔线重接后，光驱自己把没写完的轨道关掉了，盘上没有任何可读数据
TOC_CLOSED_EMPTY = """\
Media current: DVD+R
Media status : is written , is closed
Media current: DVD+R
Media product: MCC/004/48 , Mitsubishi Chemical Corporation
Media status : is written , is closed
Media blocks : 0 readable , 0 writable , 0 overall
TOC layout   : Idx ,  sbsector ,       Size , Volume Id
xorriso : SORRY : Cannot obtain Table Of Content
"""
# 真实输出：一张正常的、追加过 11 次的 DVD+R（已写比可读多 2048 扇区，但目录完好）
TOC_REAL_APPENDABLE = """\
Media current: DVD+R
Media status : is written , is appendable
Media blocks : 286384 readable , 2006672 writable , 2295104 overall
TOC layout   : Idx ,  sbsector ,       Size , Volume Id
ISO session  :   1 ,         0 ,    248638s , 资料盘
ISO session  :  11 ,    284800 ,      1581s , DATA_20250101
"""

TOC_EMPTY = """\
libburn : SORRY : No media detected
Media current: is not present
Media status : is not present
"""

DEVICES = """\
xorriso 1.5.2 : RockRidge filesystem manipulator, libburnia project.

Beginning to scan for devices ...
Full drive scan done
-----------------------------------------------------------------------------
0  -dev '/dev/sr0' rwrw-- :  'HL-DT-ST' 'DVDRAM GP65NB60'
1  -dev '/dev/sr1' rwrw-- :  'ASUS    ' 'SDRW-08D2S-U    '
-----------------------------------------------------------------------------
"""

LSDL = """\
drwxr-xr-x    1 0        0               0 Oct 25 17:20 '/'
-rw-r--r--    1 1000     1000            2 Oct 25 17:20 '/工作报告.docx'
drwxr-xr-x    1 1000     1000            0 Oct 25 17:20 '/项目资料'
-rw-r--r--    1 1000     1000         4096 Oct 25 17:20 '/项目资料/会议纪要 第三次.doc'
-rw-r--r--    1 1000     1000           10 Oct 25 17:20 '/it'"'"'s.txt'
"""


TOC_MIXED = """\
Drive current: -dev '/dev/sr0'
Drive type   : vendor 'HL-DT-ST' product 'DVDRAM GP65NB60' revision 'PF00'
Media current: DVD+R
Media product: CMC MAG E01 , CMC Magnetics Corporation
Media status : is written , is appendable
Media blocks :   117888 readable ,  2177216 writable ,  2295104 overall
TOC layout   : Idx ,  sbsector ,       Size , Volume Id
ISO session  :   1 ,         0 ,    117440s , 资料盘
ISO session  :   2 ,    117888 ,        30s , 资料盘
Other session:   3 ,    120000 ,      2000s ,
ISO session  :   1 ,         0 ,    117440s , 资料盘
"""


class DiagParseTests(unittest.TestCase):
    def test_tracks(self):
        t = B.parse_tracks(TOC_MIXED)
        self.assertEqual([(x.kind, x.index, x.start, x.blocks, x.volid) for x in t], [
            ("ISO session", 1, 0, 117440, "资料盘"),
            ("ISO session", 2, 117888, 30, "资料盘"),
            ("Other session", 3, 120000, 2000, "")])
        self.assertIn("UDF", t[2].kind_text)

    def test_drive_and_media_product(self):
        m = B.parse_toc(TOC_MIXED)
        self.assertEqual(m.drive_desc, "HL-DT-ST DVDRAM GP65NB60 PF00")
        self.assertTrue(m.media_product.startswith("CMC MAG E01"))

    def test_blkid_udev(self):
        fs = B.parse_blkid_udev(
            "ID_FS_LABEL=______\nID_FS_LABEL_ENC=\\xe8\\xb5\\x84\\xe6\\x96\\x99\\x20A\n"
            "ID_FS_VERSION=2.01\nID_FS_TYPE=udf\nID_FS_USAGE=filesystem\n")
        self.assertEqual(fs, {"type": "udf", "version": "2.01", "label": "资料 A",
                              "usage": "filesystem"})
        # 新版 blkid（2.39）直接输出 UTF-8，VERSION 里的空格写成 \x20
        fs = B.parse_blkid_udev("ID_FS_VERSION=Joliet\\x20Extension\nID_FS_LABEL=资料盘\n"
                                "ID_FS_LABEL_ENC=资料盘\nID_FS_TYPE=iso9660\n")
        self.assertEqual((fs["label"], fs["version"]), ("资料盘", "Joliet Extension"))

    def test_describe_fs(self):
        self.assertEqual(B.describe_fs({"type": "udf", "version": "2.01"}, []), "UDF 2.01")
        self.assertIn("桥接", B.describe_fs({"type": "udf", "version": "1.02"}, ["CD001", "BEA01", "NSR02"]))
        self.assertIn("Joliet", B.describe_fs({"type": "iso9660", "version": "Joliet Extension"}, ["CD001"]))
        self.assertEqual(B.describe_fs({"type": "iso9660"}, ["CD001"]), "ISO9660")
        self.assertEqual(B.describe_fs({}, ["BEA01", "NSR03", "TEA01"]), "UDF 2.00 或更高")
        self.assertEqual(B.describe_fs({}, []), "")

    def test_scan_vrs(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"\0" * 16 * 2048)
            for ident in (b"BEA01", b"NSR03", b"TEA01"):
                f.write((b"\0" + ident + b"\1").ljust(2048, b"\0"))
            f.write(b"\0" * 2048)
        try:
            self.assertEqual(B.scan_vrs(f.name), ["BEA01", "NSR03", "TEA01"])
        finally:
            os.unlink(f.name)
        self.assertEqual(B.scan_vrs("/nonexistent/x"), [])

    def test_copy_out_helpers(self):
        self.assertEqual(B.top_level(["/a/b.txt", "/a", "/c", "/ab", "/c/d/e"]), ["/a", "/ab", "/c"])
        self.assertEqual(B.disc_basename("/项目资料/会议纪要.doc"), "会议纪要.doc")
        self.assertEqual(B.disc_basename("/项目资料/"), "项目资料")
        s = " ".join(B.extract_args(DEV, ["/a.doc"], "/home/u/取出"))
        self.assertIn("-drive_access shared:readonly", s)
        self.assertIn("-osirrox on:auto_chmod_on", s)

    def test_local_paths_from_mount(self):
        mnt = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, mnt)
        os.makedirs(os.path.join(mnt, "项目资料"))
        real = B.mount_info
        B.mount_info = lambda dev: (mnt, "iso9660", "ro")
        try:
            self.assertEqual(B.local_paths(DEV, ["/项目资料"]), [os.path.join(mnt, "项目资料")])
            self.assertIsNone(B.local_paths(DEV, ["/项目资料", "/没有这个"]))   # 对不上就不用挂载目录
            self.assertIsNone(B.local_paths("/tmp/x.iso", ["/项目资料"]))      # 镜像文件不看挂载
        finally:
            B.mount_info = real

    def test_unescape_mount(self):
        self.assertEqual(B._unescape_mount("/media/u/My\\040Disc"), "/media/u/My Disc")

    def test_conclusions(self):
        d = B.Diagnosis(DEV)
        d.media = B.parse_toc(TOC_DAMAGED)
        self.assertEqual(B._conclude(d)[0][0], B.ERROR)
        d.media = B.parse_toc(TOC_FOREIGN)
        d.fs, d.vrs = {"type": "udf", "version": "2.01"}, ["BEA01", "NSR03", "TEA01"]
        d.fs_text = B.describe_fs(d.fs, d.vrs)
        d.kernel_udf = "missing"
        levels = [lvl for lvl, _ in B._conclude(d)]
        self.assertIn(B.ERROR, levels)          # 内核没有 UDF
        d.kernel_udf = "module"
        text = " ".join(t for _, t in B._conclude(d))
        self.assertIn("UDF", text)
        self.assertNotIn("可追加", text)        # UDF 盘不能用本软件追加
        d.media = B.parse_toc(TOC_APPENDABLE)
        d.fs, d.vrs = {"type": "iso9660", "version": ""}, ["CD001"]
        d.fs_text = B.describe_fs(d.fs, d.vrs)
        self.assertEqual(B._conclude(d)[0][0], B.WARN)   # 没有 Joliet


class PlatformTests(unittest.TestCase):
    """Windows 版用到的路径转换和参数文件，纯字符串处理，任何平台都能测。"""

    def test_cygwin_path(self):
        self.assertEqual(B.cygwin_path(r"C:\Users\张三\资料.docx"), "/cygdrive/c/Users/张三/资料.docx")
        self.assertEqual(B.cygwin_path("D:\\"), "/cygdrive/d/")
        self.assertEqual(B.cygwin_path("D:"), "/cygdrive/d/")
        self.assertEqual(B.cygwin_path(r"\\server\share\a.txt"), "//server/share/a.txt")
        self.assertEqual(B.drive_letter(B.windows_dev("g")), "G")
        self.assertEqual(B.drive_letter("/dev/sr0"), "")

    def test_options_file(self):
        args = ["-abort_on", "FAILURE", "-dev", "/x.iso", "-volid", "资料盘",
                "-map", "/cygdrive/c/it's 文件 (1).txt", "/it's 文件 (1).txt",
                "-find", "/a", "-type", "d", "-exec", "chmod", "0755", "--",
                "-file_size_limit", "off", "--", "-commit"]
        lines = B.options_file_text(args).splitlines()
        self.assertEqual(lines[0], "'-abort_on' 'FAILURE'")
        self.assertEqual(lines[3], "'-map' '/cygdrive/c/it'\"'\"'s 文件 (1).txt' '/it'\"'\"'s 文件 (1).txt'")
        self.assertEqual(lines[4], "'-find' '/a' '-type' 'd' '-exec' 'chmod' '0755' '--'")
        self.assertEqual(lines[5], "'-file_size_limit' 'off' '--'")
        self.assertEqual(lines[-1], "'-commit'")
        with self.assertRaises(ValueError):
            B.options_file_text(["-map", "/" + "长" * 8000, "/x"])

    def test_windows_perm_args(self):
        s = " ".join(B.windows_perm_args(["/资料"]))
        self.assertIn("-chown_r 0 /资料 --", s)
        self.assertIn("-find /资料 -type d -exec chmod 0755 --", s)
        self.assertIn("-find /资料 -type f -exec chmod 0644 --", s)

    def test_virtual_drives_last(self):
        d = B.sort_drives([B.Drive("a", "EZBSYS", "ISO CDVD DRIVE"), B.Drive("b", "Verbatim", "66807"),
                           B.Drive("c", "Msft", "Virtual DVD-ROM")])
        self.assertEqual([x.dev for x in d], ["b", "a", "c"])

    def test_volid_dash(self):
        self.assertIsNotNone(B.check_volid("-commit"))


class ShortNameTests(unittest.TestCase):
    def test_is_short_name(self):
        for n in ("2023__~1.XLS", "2023__~1.XLS;1", "______~1.DOC", "9_28____.DOC",
                  "2023______________________.XLS", "________.XLSX", "_CHECKSU.MD5"):
            self.assertTrue(B.is_short_name(n), n)
        for n in ("Qt5Core.dll", "季度调研表.zip", "README.TXT", "DATA.ZIP",
                  "填报表(第二季度).DOC", "new.txt", "report_v2.docx"):
            self.assertFalse(B.is_short_name(n), n)

    def test_parse_report_lba(self):
        out = ("File data lba:  0 ,       31 ,       28 ,    57344 , '/2023__~1.XLS'\n"
               "File data lba:  0 ,   265865 ,      153 ,   311555 , '/目录/it'\"'\"'s.doc'\n"
               "File data lba:  1 ,   999999 ,        1 ,     2048 , '/big.iso'\n"
               "File data lba:  0 ,      100 ,        1 ,     2048 , '/big.iso'\n")
        self.assertEqual(B.parse_report_lba(out), {
            "/2023__~1.XLS": (31, 57344), "/目录/it's.doc": (265865, 311555), "/big.iso": (100, 2048)})

    def test_version_fixups(self):
        f = B.version_fixups(["/a.docx;1", "/b.xls", "/目录/c.txt;1", "/d.zip;1", "/d.zip"])
        self.assertEqual([(r.old, r.new) for r in f], [("/a.docx;1", "/a.docx"), ("/目录/c.txt;1", "/目录/c.txt")])
        self.assertEqual(B.version_fixups(["/a.docx", "/b"]), [])
        self.assertEqual(B.version_fixups(["/a;1"], skip={"/a;1"}), [])
        s = " ".join(B.burn_args(DEV, [("/x", "/x")], fixups=f))
        self.assertIn("-move /a.docx;1 /a.docx", s)
        self.assertLess(s.index("-move"), s.index("-map"))

    def test_rename_args(self):
        a = B.rename_args(DEV, [B.Rename("/A__~1.XLS", "/原名.xls", 0)])
        s = " ".join(a)
        self.assertIn("-move /A__~1.XLS /原名.xls -commit", s)
        self.assertIn("-close off", s)
        self.assertLess(a.index("-joliet"), a.index("-dev"))
        a = B.rename_args(DEV, [], speed="min")
        self.assertLess(a.index("-dev"), a.index("-speed"))
        self.assertEqual(a[a.index("-speed") + 1], "min")


class SysfsDriveTests(unittest.TestCase):
    def test_sysfs_drives(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        for name, vendor, model in (("sr0", "USBDVD", "DVDRW-EXT   "), ("sda", "ATA", "SSD"), ("sr1", "", "")):
            os.makedirs(os.path.join(root, name, "device"))
            if vendor:
                for field, val in (("vendor", vendor), ("model", model)):
                    with open(os.path.join(root, name, "device", field), "w") as f:
                        f.write(val + "\n")
        d = B.sysfs_drives(root)
        self.assertEqual([(x.dev, x.vendor, x.model) for x in d],
                         [("/dev/sr0", "USBDVD", "DVDRW-EXT"), ("/dev/sr1", "", "")])
        self.assertEqual(B.sysfs_drives("/nonexistent"), [])


class MountTests(unittest.TestCase):
    """用假的 udisksctl 验证挂载成功/失败两条路径。"""

    def _fake(self, body):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "udisksctl")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        self.addCleanup(shutil.rmtree, d)
        old = os.environ["PATH"]
        os.environ["PATH"] = d + os.pathsep + old
        self.addCleanup(os.environ.__setitem__, "PATH", old)

    @unittest.skipUnless(os.name == "posix", "需要 Linux")
    def test_mount_ok(self):
        self._fake('echo "Mounted /dev/sr9 at /media/u/资料盘."')
        path, msg = B.mount_disc("/dev/sr9")
        self.assertEqual(path, "/media/u/资料盘")

    @unittest.skipUnless(os.name == "posix", "需要 Linux")
    def test_mount_fail(self):
        self._fake('echo "Error mounting /dev/sr9: wrong fs type" >&2; exit 1')
        path, msg = B.mount_disc("/dev/sr9")
        self.assertIsNone(path)
        self.assertIn("wrong fs type", msg)
        self.assertIn("sudo mount", msg)


class ParseTests(unittest.TestCase):
    def test_devices(self):
        d = B.parse_devices(DEVICES)
        self.assertEqual([x.dev for x in d], ["/dev/sr0", "/dev/sr1"])
        self.assertEqual(d[1].model, "SDRW-08D2S-U")
        self.assertIn("/dev/sr0", d[0].label)

    def test_blank(self):
        m = B.parse_toc(TOC_BLANK)
        self.assertTrue(m.present and m.blank and m.can_write)
        self.assertEqual(m.free_bytes, 2295104 * 2048)
        self.assertEqual(m.status_text(), "空白盘")
        self.assertFalse(m.foreign_data)

    def test_appendable(self):
        m = B.parse_toc(TOC_APPENDABLE)
        self.assertTrue(m.appendable and m.can_write and m.has_iso)
        self.assertEqual(m.sessions, 2)
        self.assertEqual(m.volume_id, "资料盘")
        self.assertFalse(m.rewritable)

    def test_closed(self):
        m = B.parse_toc(TOC_CLOSED)
        self.assertTrue(m.closed)
        self.assertFalse(m.can_write)
        self.assertEqual(m.free_bytes, 0)

    def test_foreign(self):
        m = B.parse_toc(TOC_FOREIGN)
        self.assertTrue(m.foreign_data)

    def test_damaged(self):
        m = B.parse_toc(TOC_DAMAGED)
        self.assertTrue(m.damaged and m.needs_repair)
        self.assertFalse(m.can_write)
        self.assertEqual(m.status_text(), "会话未关闭（需修复）")
        for good in (TOC_BLANK, TOC_APPENDABLE, TOC_CLOSED, TOC_FOREIGN, TOC_EMPTY):
            self.assertFalse(B.parse_toc(good).needs_repair)

    def test_unfinished_track_without_damaged_flag(self):
        m = B.parse_toc(TOC_UNFINISHED)
        self.assertTrue(m.present and m.appendable and m.toc_missing)
        self.assertFalse(m.damaged)                 # 光驱没标损坏
        self.assertTrue(m.unfinished and m.needs_repair)
        self.assertFalse(m.can_write)
        self.assertEqual(m.overall_blocks - m.writable_blocks, 288)
        # 正常的多会话盘不能被误判
        ok = B.parse_toc(TOC_REAL_APPENDABLE)
        self.assertFalse(ok.unfinished or ok.needs_repair)
        self.assertTrue(ok.can_write)
        for good in (TOC_BLANK, TOC_APPENDABLE, TOC_CLOSED, TOC_FOREIGN):
            self.assertFalse(B.parse_toc(good).unfinished)

    def test_reads_do_not_need_unmount(self):
        """只读的命令都以共享只读方式打开光驱：系统挂载着也能读，不用卸载。"""
        for args in (B.toc_args(DEV), B.list_files_args(DEV), B.diag_toc_args(DEV),
                     B.session_files_args(DEV), B.outdev_toc_args(DEV)):
            self.assertIn("-drive_access shared:readonly", " ".join(args))
        self.assertNotIn("shared:readonly", " ".join(B.burn_args(DEV, [("/x", "/x")])))

    def test_indev_failed(self):
        self.assertTrue(B.indev_failed(INDEV_FAILED))
        self.assertTrue(B.parse_toc(INDEV_FAILED).tree_unreadable)
        self.assertFalse(B.parse_toc(INDEV_FAILED).present)
        s = " ".join(B.outdev_toc_args(DEV))
        self.assertIn("-drive_access shared:readonly -outdev %s -toc" % DEV, s)

    def test_repair_force(self):
        self.assertIn("-close_damaged force", " ".join(B.repair_args(DEV, force=True)))
        self.assertIn("-close_damaged as_needed", " ".join(B.repair_args(DEV)))

    def test_diagnose_falls_back_to_outdev(self):
        """-indev 失败时诊断改用 -outdev，并给出“需要修复”的结论，而不是“没有光盘”。"""
        calls = []

        def fake(args, timeout=120):
            calls.append(args)
            return (32, INDEV_FAILED) if "-indev" in args else (0, TOC_UNFINISHED)
        real = B.run_xorriso
        B.run_xorriso = fake
        try:
            d = B.diagnose("/dev/sr-test-nonexistent")
        finally:
            B.run_xorriso = real
        self.assertTrue(any("-outdev" in a for a in calls))
        self.assertTrue(d.media.present and d.media.tree_unreadable and d.media.needs_repair)
        self.assertEqual(d.findings[0][0], B.ERROR)
        self.assertIn("强制关闭", d.findings[0][1])
        self.assertIn("-outdev -toc", d.report_text())

    def test_closed_empty_disc(self):
        """关闭了但没有可读数据的盘：不能说“只能读取”，要说清楚已无法使用。"""
        def fake(args, timeout=120):
            return (32, INDEV_FAILED) if "-indev" in args else (0, TOC_CLOSED_EMPTY)
        real = B.run_xorriso
        B.run_xorriso = fake
        try:
            d = B.diagnose("/dev/sr-test-nonexistent")
        finally:
            B.run_xorriso = real
        self.assertTrue(d.media.closed and not d.media.needs_repair)
        texts = " ".join(t for _, t in d.findings)
        self.assertIn("无法使用", texts)
        self.assertNotIn("只能读取", texts)
        self.assertIn(B.ERROR, [lvl for lvl, _ in d.findings])

    def test_incomplete_cd(self):
        m = B.parse_toc(TOC_INCOMPLETE_CD)
        self.assertTrue(m.incomplete and m.needs_repair)

    def test_repair_args(self):
        s = " ".join(B.repair_args(DEV))
        self.assertIn("-outdev %s -close off -close_damaged as_needed" % DEV, s)
        self.assertIn("-close on", " ".join(B.repair_args(DEV, keep_open=False)))

    def test_repair_result(self):
        self.assertEqual(B.repair_result(0, "libburn : NOTE : Track not marked as damaged. No action taken."),
                         B.REPAIR_NOT_NEEDED)
        self.assertEqual(B.repair_result(5, "libburn : SORRY : Cannot close damaged track on given media type"),
                         B.REPAIR_UNSUPPORTED)
        self.assertEqual(B.repair_result(0, "xorriso : UPDATE : Closing track/session."), B.REPAIR_OK)
        self.assertEqual(B.repair_result(32, "xorriso : FAILURE : Cannot acquire drive"), B.REPAIR_FAILED)

    def test_empty(self):
        m = B.parse_toc(TOC_EMPTY)
        self.assertFalse(m.present)
        self.assertEqual(m.status_text(), "无盘片")

    def test_rewritable(self):
        def profile(p):
            return B.parse_toc(TOC_BLANK.replace("DVD+R\n", p + "\n"))
        self.assertTrue(profile("DVD-RW restricted overwrite").rewritable)
        self.assertTrue(profile("BD-RE").rewritable)
        self.assertTrue(profile("DVD+RW").rewritable)
        self.assertFalse(profile("DVD-R sequential recording").rewritable)
        self.assertFalse(profile("stdio file, overwriteable").rewritable)
        self.assertFalse(profile("BD-R sequential recording").rewritable)
        self.assertFalse(profile("DVD+R/DL").rewritable)

    def test_lsdl(self):
        e = B.parse_lsdl(LSDL)
        self.assertEqual([x.path for x in e],
                         ["/工作报告.docx", "/项目资料", "/项目资料/会议纪要 第三次.doc", "/it's.txt"])
        self.assertTrue(e[1].is_dir)
        self.assertEqual(e[2].size, 4096)

    def test_progress(self):
        self.assertEqual(B.parse_progress(
            "xorriso : UPDATE : Writing:   1696s    0.7%   fifo 100%  buf  50%   1.1xD")[0], 0.7)
        self.assertEqual(B.parse_progress("xorriso : UPDATE :  45.12% done")[0], 45.12)
        self.assertEqual(B.parse_progress(
            "xorriso : UPDATE : Closing track/session.")[1], "正在关闭会话……")

    def test_volid(self):
        self.assertIsNone(B.check_volid("资料盘2025"))
        self.assertIsNotNone(B.check_volid("这是一个特别长的光盘卷标名字"))
        self.assertIsNotNone(B.check_volid("A" * 17))

    def test_burn_args(self):
        a = B.burn_args(DEV, [("/home/u/报告.docx", "/报告.docx")], volid="X")
        s = " ".join(a)
        self.assertLess(a.index("-joliet"), a.index("-dev"))
        self.assertIn("-close off", s)
        self.assertIn("-local_charset UTF-8", s)
        self.assertIn("joliet_long_names", s)
        self.assertIn("-check_md5_r FAILURE /报告.docx --", s)
        self.assertIn("-close on", " ".join(B.burn_args(DEV, [], close=True)))

    def test_disc_join(self):
        self.assertEqual(B.disc_join("/", "a.txt"), "/a.txt")
        self.assertEqual(B.disc_join("备份/2025/", "a.txt"), "/备份/2025/a.txt")


@unittest.skipUnless(have_xorriso(), "需要 xorriso")
class IntegrationTests(unittest.TestCase):
    """对 ISO 文件模拟“第一次刻录 → 追加 → 读回”的完整流程。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(self.src, "项目资料"))
        self._w("工作报告.docx", "a")
        self._w("项目资料/会议纪要 第三次.doc", "b" * 5000)
        self._w("长" * 80 + ".txt", "c")
        self._w("it's 文件 (1).txt", "d")
        self.iso = os.path.join(self.tmp, "disc.iso")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _w(self, rel, data):
        with open(os.path.join(self.src, rel), "w", encoding="utf-8") as f:
            f.write(data)

    def _burn(self, items, **kw):
        code, out = B.run_xorriso(B.burn_args(self.iso, items, **kw))
        self.assertEqual(code, 0, out)
        return out

    def test_multisession_roundtrip(self):
        self._burn([(os.path.join(self.src, "工作报告.docx"), "/工作报告.docx"),
                    (os.path.join(self.src, "项目资料"), "/项目资料"),
                    (os.path.join(self.src, "it's 文件 (1).txt"), "/it's 文件 (1).txt")],
                   volid="资料盘2025")
        m = B.read_media(self.iso)
        self.assertTrue(m.appendable, m.raw)
        self.assertEqual(m.volume_id, "资料盘2025")

        # 第二次追加，放到子目录里
        name = "长" * 80 + ".txt"
        out = self._burn([(os.path.join(self.src, name), B.disc_join("/追加", name))])
        self.assertFalse(B.md5_mismatch(out))

        m = B.read_media(self.iso)
        self.assertTrue(m.appendable)
        files = {e.path for e in B.list_disc_files(self.iso)}
        self.assertIn("/工作报告.docx", files)
        self.assertIn("/项目资料/会议纪要 第三次.doc", files)
        self.assertIn("/追加/" + name, files)
        self.assertIn("/it's 文件 (1).txt", files)

    def test_extract(self):
        """从盘上取出文件和整个文件夹（不挂载），名字、内容不变，取出来的可写。"""
        self._burn([(os.path.join(self.src, "工作报告.docx"), "/工作报告.docx"),
                    (os.path.join(self.src, "项目资料"), "/项目资料")])
        dest = os.path.join(self.tmp, "取出")
        os.makedirs(dest)
        code, out = B.run_xorriso(B.extract_args(self.iso, ["/工作报告.docx", "/项目资料"], dest))
        self.assertEqual(code, 0, out)
        got = os.path.join(dest, "项目资料", "会议纪要 第三次.doc")
        with open(got, encoding="utf-8") as f:
            self.assertEqual(f.read(), "b" * 5000)
        self.assertTrue(os.path.exists(os.path.join(dest, "工作报告.docx")))
        B.make_writable([os.path.join(dest, "项目资料")])
        self.assertTrue(os.access(got, os.W_OK))

    def test_close_disc(self):
        self._burn([(os.path.join(self.src, "工作报告.docx"), "/a.docx")], close=True)
        m = B.read_media(self.iso)
        # 镜像文件属于“可覆盖介质”，xorriso 不会真的封闭它；这里只确认命令能成功
        self.assertTrue(m.present, m.raw)

    def test_repair_on_healthy_disc_is_noop(self):
        self._burn([(os.path.join(self.src, "工作报告.docx"), "/a.docx")])
        with open(self.iso, "rb") as f:
            before = f.read()
        code, out = B.run_xorriso(B.repair_args(self.iso))
        self.assertEqual(B.repair_result(code, out), B.REPAIR_NOT_NEEDED, out)
        with open(self.iso, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(B.read_media(self.iso).needs_repair)

    @unittest.skipUnless(B.IS_WINDOWS or B.find_tool("blkid"), "需要 blkid")
    def test_diagnose_iso(self):
        self._burn([(os.path.join(self.src, "工作报告.docx"), "/a.docx")], volid="资料盘")
        self._burn([(os.path.join(self.src, "项目资料"), "/项目资料")])
        d = B.diagnose(self.iso)
        self.assertTrue(d.media.present and d.media.appendable, d.report_text())
        self.assertEqual(len(d.tracks), 2)
        self.assertIn("Joliet", d.fs_text)
        self.assertEqual(d.fs.get("label"), "资料盘")
        self.assertIn("CD001", d.vrs)
        self.assertEqual(d.findings[0][0], B.OK)
        self.assertIn("ISO9660", d.report_text())

    @unittest.skipUnless(shutil.which("mkudffs") and B.find_tool("blkid"), "需要 mkudffs 和 blkid")
    def test_diagnose_udf(self):
        img = os.path.join(self.tmp, "udf.img")
        with open(img, "wb") as f:
            f.truncate(20 * 1024 * 1024)
        subprocess.run(["mkudffs", "--udfrev=0x0201", "--label=WINUDF", img],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        d = B.diagnose(img)
        self.assertEqual(d.fs.get("type"), "udf", d.report_text())
        self.assertEqual(d.fs_text, "UDF 2.01")
        self.assertEqual(d.vrs, ["BEA01", "NSR03", "TEA01"])
        self.assertEqual(B.udf_revision(img), "2.01")
        self.assertTrue(any("UDF" in t for _, t in d.findings))

    def make_short_name_disc(self):
        """模拟一张典型的问题盘：先像麒麟一样刻一个只有 Rock Ridge、没有 Joliet 的会话，
        再像 Windows 一样只读 ISO9660 短名追加一个会话（短名被写进 Joliet）。"""
        names = ["2023年度《办公设备管理制度》相关表格.xls", "部门通讯录（内部）.xlsx"]
        for n in names:
            self._w(n, n)
        args = B.COMMON_ARGS + ["-joliet", "off", "-dev", B.xpath(self.iso)]
        for n in names:
            args += ["-map", B.xpath(os.path.join(self.src, n)), "/" + n]
        code, out = B.run_xorriso(args + ["-commit"])
        self.assertEqual(code, 0, out)
        code, out = B.run_xorriso(B.COMMON_ARGS + [
            "-read_fs", "ecma119", "-rockridge", "off", "-joliet", "on", "-dev", B.xpath(self.iso),
            "-map", B.xpath(os.path.join(self.src, "工作报告.docx")), "/NEW.DOC", "-commit"])
        self.assertEqual(code, 0, out)
        return names

    def test_recover_long_names(self):
        names = self.make_short_name_disc()
        d = B.diagnose(self.iso)
        self.assertEqual(len(d.short_names), 2, d.report_text())
        self.assertTrue(d.can_recover_names)
        plan = B.plan_name_recovery(self.iso)
        self.assertEqual(sorted(os.path.basename(r.new) for r in plan.renames), sorted(names))
        self.assertEqual(plan.unresolved, [])
        code, out = B.run_xorriso(B.rename_args(self.iso, plan.renames))
        self.assertEqual(code, 0, out)
        files = {e.path for e in B.list_disc_files(self.iso)}
        self.assertEqual(files, {"/" + n for n in names} | {"/NEW.DOC"})
        # 再诊断：没有短名了，内容也没变
        self.assertEqual(B.diagnose(self.iso).short_names, [])
        code, out = B.run_xorriso(B.COMMON_ARGS + ["-indev", B.xpath(self.iso), "-osirrox", "on",
                                                   "-extract", "/" + names[0], B.xpath(os.path.join(self.tmp, "x"))])
        with open(os.path.join(self.tmp, "x"), encoding="utf-8") as f:
            self.assertEqual(f.read(), names[0])

    def test_recovery_skips_ambiguous(self):
        """空文件在 ISO 上共用一个扇区，按位置找原名会张冠李戴，必须跳过。"""
        names = ["空文件甲.txt", "空文件乙.txt", "有内容的文件.txt"]
        for n in names:
            self._w(n, "" if n.startswith("空") else "data")
        args = B.COMMON_ARGS + ["-joliet", "off", "-dev", B.xpath(self.iso)]
        for n in names:
            args += ["-map", B.xpath(os.path.join(self.src, n)), "/" + n]
        self.assertEqual(B.run_xorriso(args + ["-commit"])[0], 0)
        code, out = B.run_xorriso(B.COMMON_ARGS + [
            "-read_fs", "ecma119", "-rockridge", "off", "-joliet", "on", "-dev", B.xpath(self.iso),
            "-map", B.xpath(os.path.join(self.src, "工作报告.docx")), "/NEW.DOC", "-commit"])
        self.assertEqual(code, 0, out)
        plan = B.plan_name_recovery(self.iso)
        self.assertEqual([os.path.basename(r.new) for r in plan.renames], ["有内容的文件.txt"])
        self.assertEqual(len(plan.unresolved), 2, plan.unresolved)   # 两个空文件

    def joliet_names(self, iso):
        """读镜像当前超级块（LBA 0 起）的 Joliet 根目录原始名字，即 Windows 看到的名字。"""
        import struct
        with open(iso, "rb") as f:
            def read(lba, n=1):
                f.seek(lba * 2048)
                return f.read(2048 * n)
            for i in range(16, 32):
                vd = read(i)
                if vd[0] == 2 and vd[88:91] == b"%/E":
                    ext, size = struct.unpack("<I", vd[158:162])[0], struct.unpack("<I", vd[166:170])[0]
                    data, pos, names = read(ext, (size + 2047) // 2048), 0, []
                    while pos < size:
                        ln = data[pos]
                        if ln == 0:
                            pos = (pos // 2048 + 1) * 2048
                            continue
                        name = data[pos + 33:pos + 33 + data[pos + 32]]
                        if name not in (b"\0", b"\1"):
                            names.append(name.decode("utf-16-be"))
                        pos += ln
                    return sorted(names)
        return []

    def test_append_to_windows_disc_keeps_names(self):
        """Windows 刻的盘（只有 Joliet，名字带 ;1）追加后，Windows 上旧文件名不能变成 xxx_1。"""
        code, out = B.run_xorriso(B.COMMON_ARGS + [
            "-rockridge", "off", "-joliet", "on", "-compliance", "clear:joliet_long_names",
            "-dev", B.xpath(self.iso), "-map", B.xpath(os.path.join(self.src, "工作报告.docx")), "/工作报告.docx", "-commit"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.joliet_names(self.iso), ["工作报告.docx;1"])   # 像 IMAPI2 一样带 ;1
        entries = [e.path for e in B.list_disc_files(self.iso) if not e.is_dir]
        fixups = B.version_fixups(entries)
        code, out = B.run_xorriso(B.burn_args(
            self.iso, [(os.path.join(self.src, "项目资料"), "/项目资料")], fixups=fixups))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.joliet_names(self.iso), ["工作报告.docx", "项目资料"])
        self.assertIn("/工作报告.docx", {e.path for e in B.list_disc_files(self.iso)})

    def test_overlong_volid_rejected_before_burn(self):
        self.assertIsNotNone(B.check_volid("这是一个特别长的光盘卷标名字"))


if __name__ == "__main__":
    unittest.main()
