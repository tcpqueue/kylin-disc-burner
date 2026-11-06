# -*- coding: utf-8 -*-
"""设备刷新、后台任务暂存和 Windows 进程启动的回归测试。"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt5.QtWidgets import QApplication
    from discburner import gui as G
    from discburner import backend as B
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "需要 PyQt5")
class GuiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with mock.patch.object(G.MainWindow, "_run"):
            self.win = G.MainWindow()
        self.win._offer_read_all = lambda: None
        self.win._offer_fs_fix = lambda: None

    def tearDown(self):
        self.win.close()
        shutil.rmtree(self.tmp)

    def test_refresh_detects_hotplugged_drive(self):
        dev = B.windows_dev("Z") if B.IS_WINDOWS else "/dev/sr9"
        self.win.cb_drive.addItem("ISO", G.IMAGE_ITEM)
        out = "0 -dev '%s' rwrw-- : 'TEST' 'USB DVD'" % dev
        def scan(args, done, stage):
            self.assertEqual(args, ["-devices"])
            done(0, out)
        with mock.patch.object(B, "missing_drives", return_value=[]), \
                mock.patch.object(B, "sysfs_drives", return_value=[]), \
                mock.patch.object(self.win, "_run", side_effect=scan), \
                mock.patch.object(self.win, "refresh_media") as refresh:
            self.win.btn_refresh.click()
        self.assertEqual(self.win.current_dev(), dev)
        refresh.assert_called_once_with()

    def test_refresh_keeps_selected_image(self):
        image = os.path.join(self.tmp, "资料.iso")
        self.win.cb_drive.addItem("资料.iso", image)
        self.win.cb_drive.addItem("ISO", G.IMAGE_ITEM)
        self.win.cb_drive.setCurrentIndex(0)
        with mock.patch.object(B, "missing_drives", return_value=[]), \
                mock.patch.object(B, "sysfs_drives", return_value=[]), \
                mock.patch.object(self.win, "_run", side_effect=lambda a, cb, s: cb(0, "")), \
                mock.patch.object(self.win, "refresh_media") as refresh:
            self.win.btn_refresh.click()
        self.assertEqual(self.win.current_dev(), image)
        self.assertEqual(self.win.cb_drive.count(), 2)
        refresh.assert_called_once_with()

    def test_staging_preserves_other_active_tasks(self):
        stage = os.path.join(self.tmp, "stage")
        active = os.path.join(stage, "other-task")
        os.makedirs(active)
        payload = os.path.join(active, "文件.txt")
        with open(payload, "w") as f:
            f.write("another instance is burning this file")
        with mock.patch.object(G, "STAGE_DIR", stage), mock.patch.object(G, "FuncThread") as thread:
            thread.return_value.isRunning.return_value = False
            self.win._stage_then_burn("unused", {})
            owned = self.win._stage
            self.assertNotEqual(owned, active)
            self.assertTrue(os.path.exists(payload))
            self.win._drop_stage()
        self.assertFalse(os.path.exists(owned))
        self.assertTrue(os.path.exists(payload))

    def test_cancel_shortened_names_stops_before_drive_access(self):
        source = os.path.join(self.tmp, "a" * 104 + ".exe")
        with open(source, "w") as f:
            f.write("keep this file")
        with mock.patch.object(G.QMessageBox, "question", return_value=G.QMessageBox.No) as question, \
                mock.patch.object(self.win, "_release_drive") as release, \
                mock.patch.object(self.win, "_run") as run:
            self.win._start_burn("test", [(source, "/" + os.path.basename(source))], "", False)
            self.assertEqual(question.call_count, 1)
            release.assert_not_called()
            run.assert_not_called()
        self.assertTrue(os.path.exists(source))

    def test_confirmed_names_are_used_for_verification(self):
        name = "a" * 104 + ".exe"
        source = os.path.join(self.tmp, name)
        with open(source, "w") as f:
            f.write("data")
        with mock.patch.object(G.QMessageBox, "question", return_value=G.QMessageBox.Yes), \
                mock.patch.object(self.win, "_release_drive"), \
                mock.patch.object(self.win, "_run") as run:
            self.win._start_burn("test", [(source, "/" + name)], "", False)
        args = run.call_args[0][0]
        self.assertIn("-move", args)
        idx = args.index("-check_md5_r")
        self.assertEqual(args[idx + 2], "/" + "a" * 99 + ".exe")
        self.assertTrue(os.path.exists(source))

    def test_udf_history_blocks_all_original_disc_writes(self):
        dev = B.windows_dev("Z") if B.IS_WINDOWS else "/dev/sr9"
        self.win.cb_drive.addItem("disc", dev)
        self.win.media.present = self.win.media.appendable = self.win.media.has_iso = True
        self.win.media.udf_history = True
        with mock.patch.object(G.QMessageBox, "warning"), \
                mock.patch.object(self.win, "_release_drive") as release, \
                mock.patch.object(self.win, "_run") as run:
            self.win._start_burn(dev, [], "", False)
            self.win.apply_renames(dev, [])
            self.assertFalse(self.win._can_hide())
            release.assert_not_called()
            run.assert_not_called()

    def test_udf_conversion_copies_without_writing_original(self):
        dev = B.windows_dev("Z") if B.IS_WINDOWS else "/dev/sr9"
        self.win.cb_drive.addItem("disc", dev)
        self.win.media.present = self.win.media.udf_history = True
        self.win.disc_entries = [B.DiscEntry("/file.txt", False, 1)]
        with mock.patch.object(G.QMessageBox, "information"), \
                mock.patch.object(self.win, "copy_out") as copy, \
                mock.patch.object(self.win, "_stage_then_burn") as write:
            self.win.convert_to_iso()
            copy.assert_called_once_with()
            write.assert_not_called()

    def test_bridge_uses_udf_names_instead_of_short_joliet_names(self):
        import test_backend
        dev = B.windows_dev("Z") if B.IS_WINDOWS else "/dev/sr9"
        self.win.cb_drive.addItem("disc", dev)
        name = "a" * 110 + ".exe"
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write("full UDF name")
        with mock.patch.object(B, "has_udf_history", return_value=True), \
                mock.patch.object(B, "is_current_udf_bridge", return_value=True), \
                mock.patch.object(B, "mount_info", return_value=(self.tmp, "UDF", "")):
            self.win._media_done(0, test_backend.TOC_APPENDABLE)
        self.assertTrue(self.win.media.udf_bridge)
        self.assertIn("/" + name, [e.path for e in self.win.disc_entries])
        self.assertEqual(self.win._udf_start(dev), self.win.media.last_iso_start)

    def test_mount_finishes_reading_before_copying(self):
        dev = B.windows_dev("Z") if B.IS_WINDOWS else "/dev/sr9"
        self.win.cb_drive.addItem("disc", dev)
        m = self.win.media
        m.present = m.appendable = True
        m.other_sessions = 1
        m.udf_history = True
        self.win._fs_prompted = dev
        with mock.patch.object(self.win, "_udf_source", return_value=None), \
                mock.patch.object(self.win, "_offer_udf_mount") as offer, \
                mock.patch.object(self.win, "refresh_media") as refresh:
            self.win.convert_to_iso()
            ready = offer.call_args[0][1]
            ready()
            refresh.assert_called_once_with()
            with mock.patch.object(self.win, "convert_to_iso") as copy:
                self.win._media_shown()
                self.app.processEvents()
                copy.assert_called_once_with(ask=True)

    @unittest.skipUnless(os.name == "nt", "需要 Windows Cygwin 后端")
    def test_qprocess_options_path_with_apostrophe(self):
        try:
            B.find_xorriso()
        except B.XorrisoNotFound:
            self.skipTest("需要 xorriso")
        for name in ("O'Brien", "O'Brien 中文路径"):
            scratch = os.path.join(self.tmp, name)
            os.mkdir(scratch)
            job = G.Job()
            results = []
            job.finished.connect(lambda code, out: results.append((code, out)))
            with mock.patch.object(tempfile, "tempdir", scratch):
                job.start(["-print", "QPROCESS_PATH_OK"])
                end = time.monotonic() + 20
                while not results and time.monotonic() < end:
                    self.app.processEvents()
                    time.sleep(0.01)
            if not results:
                job.proc.kill()
                job.proc.waitForFinished(3000)
            self.assertTrue(results, "QProcess timeout")
            code, out = results[0]
            self.assertEqual(code, 0, out)
            self.assertIn("QPROCESS_PATH_OK", out)
            self.assertEqual(os.listdir(scratch), [])


if __name__ == "__main__":
    unittest.main()
