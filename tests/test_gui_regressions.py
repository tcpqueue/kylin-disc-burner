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

    def _disc(self, **flags):
        dev = B.windows_dev("Z") if B.IS_WINDOWS else "/dev/sr9"
        self.win.cb_drive.addItem("disc", dev)
        self.win.cb_drive.setCurrentIndex(self.win.cb_drive.findData(dev))
        m = self.win.media
        m.present = True
        for k, v in flags.items():
            setattr(m, k, v)
        return dev

    def test_transfer_stages_into_own_folder_without_writing(self):
        stage = os.path.join(self.tmp, "stage")
        active = os.path.join(stage, "other-task")
        os.makedirs(active)
        payload = os.path.join(active, "文件.txt")
        with open(payload, "w") as f:
            f.write("another instance is burning this file")
        dev = self._disc(appendable=True, has_iso=True, udf_history=True)
        self.win.disc_entries = [B.DiscEntry("/资料", True, 0), B.DiscEntry("/资料/a.txt", False, 3)]
        self.win._iso_paths = {e.path for e in self.win.disc_entries}
        with mock.patch.object(G, "STAGE_DIR", stage), \
                mock.patch.object(G, "FuncThread") as thread, \
                mock.patch.object(G.QMessageBox, "question", return_value=G.QMessageBox.Yes), \
                mock.patch.object(self.win, "_run") as run, \
                mock.patch.object(self.win, "_release_drive") as release:
            thread.return_value.isRunning.return_value = False
            self.win.transfer_to_new_disc()
            fn, *args = thread.call_args[0]
            self.assertIs(fn, B.copy_out)
            self.assertEqual(args[1], ["/资料"])
            owned = self.win._transfer["stage"]
            self.assertNotEqual(owned, active)
            self.assertTrue(owned.startswith(stage))
            run.assert_not_called()
            release.assert_not_called()
            # 读完：读出的文件变成待刻录，原盘弹出
            os.makedirs(os.path.join(owned, "资料"))
            with open(os.path.join(owned, "资料", "a.txt"), "w") as f:
                f.write("abc")
            with mock.patch.object(G.QMessageBox, "information"), mock.patch.object(B, "unmount"):
                self.win._transfer_staged((0, "ok"))
            self.assertEqual([(p.dst, p.transfer) for p in self.win.pending], [("/资料", True)])
            self.assertEqual(run.call_args[0][0], B.eject_args(dev))
            self.win.cancel_transfer()
        self.assertFalse(os.path.exists(owned))
        self.assertTrue(os.path.exists(payload))
        self.assertEqual(self.win.pending, [])

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
        dev = self._disc(appendable=True, has_iso=True, udf_history=True)
        with mock.patch.object(G.QMessageBox, "question", return_value=G.QMessageBox.No) as ask, \
                mock.patch.object(G.QMessageBox, "warning"), \
                mock.patch.object(self.win, "_release_drive") as release, \
                mock.patch.object(self.win, "_run") as run:
            self.win._start_burn(dev, [], "", False)
            self.win.apply_renames(dev, [])
            self.assertFalse(self.win._can_hide())
            release.assert_not_called()
            run.assert_not_called()
            self.assertIn("转存到新光盘", ask.call_args[0][2])

    def test_bridge_uses_udf_names_instead_of_short_joliet_names(self):
        import test_backend
        dev = self._disc()
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

    def test_reading_udf_part_continues_with_the_pending_action(self):
        self._disc()
        done = []
        with mock.patch.object(self.win, "_udf_source", return_value=self.tmp), \
                mock.patch.object(self.win, "refresh_media") as refresh:
            self.win._mount_then = lambda: done.append("copy")
            self.win._udf_mounted((self.tmp, ""))
            refresh.assert_called_once_with()
        self.win.media.present = False
        self.win._media_shown()
        self.app.processEvents()
        self.assertEqual(done, ["copy"])

    def test_one_list_shows_disc_and_pending_items(self):
        src = os.path.join(self.tmp, "报告.docx")
        with open(src, "w") as f:
            f.write("x")
        self.win.disc_entries = [B.DiscEntry("/资料", True, 0, 1700000000.0),
                                 B.DiscEntry("/资料/报告.docx", False, 9, 1700000000.0)]
        self.win._fill_tree()
        folder = self.win.tree.topLevelItem(0)
        folder.setSelected(True)
        self.assertEqual(self.win._target_folder(), "/资料")
        self.win.add_paths([src])
        self.assertEqual(self.win.pending_items()[0][1], "/资料/报告.docx")
        folder = self.win.tree.topLevelItem(0)
        kinds = sorted((folder.child(i).data(0, G.ROLE_KIND), folder.child(i).text(3))
                       for i in range(folder.childCount()))
        self.assertEqual(kinds, [("disc", "将被新文件替换"), ("pending", G.HINT_REPLACE)])
        self.assertTrue(folder.child(0).text(2).startswith("20"))
        # 新建文件夹，再往里放文件；去掉文件夹时里面的也一起去掉
        self.win.tree.clearSelection()
        self.win.new_folder("2026-11-06")
        self.assertEqual(self.win._target_folder(), "/2026-11-06")
        self.win.add_paths([src])
        self.assertIn("/2026-11-06/报告.docx", [d for _s, d, _z, _c in self.win.pending_items()])
        self.win._select_path("/2026-11-06")
        self.win._remove_selected()
        self.assertEqual([p.dst for p in self.win.pending], ["/资料/报告.docx"])

    def test_no_drive_permission_offers_password_prompt_not_commands(self):
        if B.IS_WINDOWS:
            self.skipTest("只有麒麟需要")
        dev = self._disc()
        with mock.patch.object(G.os.path, "exists", return_value=True), \
                mock.patch.object(G.os, "access", return_value=False):
            self.win._update_banner()
        text = self.win.banner.label.text()
        self.assertIn("开机密码", text)
        self.assertNotIn("sudo", text)
        button = self.win.banner.button("授权使用光驱")
        self.assertIsNotNone(button)
        with mock.patch.object(B, "grant_cdrom_access", return_value=(True, "")) as grant, \
                mock.patch.object(G.QMessageBox, "information") as info:
            button.click()
        grant.assert_called_once_with()
        self.assertIn("注销", info.call_args[0][2])
        self.assertTrue(dev)

    def test_burn_without_eject_marks_system_view_stale(self):
        if B.IS_WINDOWS:
            self.skipTest("只有麒麟需要")
        with mock.patch.object(B, "unmount"), mock.patch.object(B, "mount_disc") as mount, \
                mock.patch.object(B, "is_mounted", return_value=True):
            self.win._release_drive("/dev/sr9")
            self.win._job_finished(0, "")
            mount.assert_not_called()
        self.assertIn("/dev/sr9", self.win._stale)
        self.win.cb_drive.addItem("disc", "/dev/sr9")
        self.win.cb_drive.setCurrentIndex(self.win.cb_drive.findData("/dev/sr9"))
        self.win._ejected()
        self.assertNotIn("/dev/sr9", self.win._stale)

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
