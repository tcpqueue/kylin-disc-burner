#!/bin/sh
# 光盘刻录助手启动器
#
# 系统已有能用的 Python 3 + PyQt5 时优先用系统的：界面跟随麒麟主题，
# 卷标框里也一定能用系统的中文输入法。没有就用安装包自带的运行环境。
# 强制指定：DISCBURNER_RUNTIME=system 或 DISCBURNER_RUNTIME=bundled
APP=/usr/share/discburner
BUNDLED="$APP/runtime/discburner"

use_system() {
    # -B：不在安装目录里写 __pycache__，卸载时才能删干净
    PYTHONPATH="$APP" exec python3 -B -m discburner "$@"
}

use_bundled() {
    # 自带的 Qt 5.5 没有 Wayland 支持，Wayland 会话下走 XWayland
    case "$QT_QPA_PLATFORM" in
        offscreen*|minimal*|xcb*) ;;
        *) QT_QPA_PLATFORM=xcb; export QT_QPA_PLATFORM ;;
    esac
    exec "$BUNDLED" "$@"
}

case "$DISCBURNER_RUNTIME" in
    system)  use_system "$@" ;;
    bundled) use_bundled "$@" ;;
esac

if command -v python3 >/dev/null 2>&1 &&
   python3 -c 'import sys; assert sys.version_info >= (3, 5); from PyQt5 import QtWidgets' >/dev/null 2>&1; then
    use_system "$@"
fi
use_bundled "$@"
