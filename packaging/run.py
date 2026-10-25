# PyInstaller 打包入口，与 python3 -m discburner 行为一致
import io
import os
import sys

if getattr(sys, "frozen", False) and os.name != "nt":
    # 自带的 Qt 插件（xcb 显示、输入法等）放在运行环境的 PyQt5/Qt/plugins 下
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    os.environ["QT_PLUGIN_PATH"] = os.path.join(base, "PyQt5", "Qt", "plugins")
    # 启动器为自带库设置了 LD_LIBRARY_PATH；本进程启动时已经生效，
    # 这里还原，免得子进程（如系统自带的 xorriso）误加载自带的旧库
    orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig is not None:
        os.environ["LD_LIBRARY_PATH"] = orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)

if "--selftest" in sys.argv:
    from discburner.selftest import run
    i = sys.argv.index("--selftest")
    keep = sys.argv[i + 1] if len(sys.argv) > i + 1 else None
    if sys.stdout is not None:
        sys.exit(run(keep))
    # Windows 绿色版是窗口程序，没有控制台，结果用对话框显示
    buf = io.StringIO()
    sys.stdout = buf
    code = run(keep)
    out = os.environ.get("DISCBURNER_SELFTEST_OUT")    # 打包脚本自动测试用
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
        sys.exit(code)
    from PyQt5.QtWidgets import QApplication, QMessageBox
    app = QApplication(sys.argv)
    (QMessageBox.information if code == 0 else QMessageBox.critical)(
        None, "光盘刻录助手 自检", buf.getvalue())
    sys.exit(code)

from discburner.gui import main

sys.exit(main())
