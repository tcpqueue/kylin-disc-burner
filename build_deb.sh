#!/bin/bash
# 打包成不依赖任何系统软件包的 .deb（内网机器 dpkg -i 一次装好）
#
# 用法：./build_deb.sh <amd64|arm64> <自带运行环境目录> <xorriso 源码包>
#   自带运行环境目录由 packaging/build_runtime.sh 生成，内含 xorriso 和 runtime/
# 输出：dist/discburner_<版本>_<架构>.deb
set -e
cd "$(dirname "$0")"
ARCH=$1; BUNDLE=$2; XO_SRC=$3
[ -n "$ARCH" ] && [ -x "$BUNDLE/xorriso" ] && [ -x "$BUNDLE/runtime/discburner" ] && [ -f "$XO_SRC" ] || {
  echo "用法：$0 <amd64|arm64> <自带运行环境目录> <xorriso 源码包>"; exit 1; }
VERSION=$(python3 -c "import discburner; print(discburner.__version__)")
PKG=discburner
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
# mktemp 建的目录是 700，不改的话装包时可能把系统根目录权限也改成 700
chmod 755 "$ROOT"

APP="$ROOT/usr/share/$PKG"
DOC="$ROOT/usr/share/doc/$PKG"
install -d "$ROOT/DEBIAN" "$ROOT/usr/bin" "$APP/discburner" "$APP/bin" \
           "$ROOT/usr/share/applications" "$ROOT/usr/share/icons/hicolor/scalable/apps" "$DOC"
install -m644 discburner/*.py "$APP/discburner/"
install -m755 "$BUNDLE/xorriso" "$APP/bin/xorriso"
cp -a "$BUNDLE/runtime" "$APP/runtime"
install -m755 packaging/discburner.sh "$ROOT/usr/bin/discburner"
install -m644 packaging/discburner.desktop "$ROOT/usr/share/applications/"
install -m644 packaging/discburner.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/"
install -m644 README.md LICENSE "$DOC/"
install -m644 "$XO_SRC" "$DOC/"
cat > "$DOC/copyright" <<EOF
光盘刻录助手（discburner）
项目主页：https://github.com/tcpqueue/kylin-disc-burner
本程序许可证：GPL-3.0，全文见本目录 LICENSE。

自带的第三方组件：
- GNU xorriso $("$BUNDLE/xorriso" -version 2>/dev/null | sed -n 's/^xorriso \([0-9.]*\) .*/\1/p' | head -1)
  许可证 GPLv3+，版权归 Thomas Schmitt 等。完整源码：$(basename "$XO_SRC")（本目录）。
- Python 3.5、PyQt5 5.5、Qt 5.5 及其依赖库（来自 Ubuntu 16.04），位于
  /usr/share/discburner/runtime，各自遵循原有许可证（PSF、GPLv3、LGPLv3 等）。
EOF
find "$ROOT" -name __pycache__ -prune -exec rm -rf {} +

SIZE=$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)
cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Architecture: $ARCH
Maintainer: tcpqueue <44940833+tcpqueue@users.noreply.github.com>
Homepage: https://github.com/tcpqueue/kylin-disc-burner
Installed-Size: $SIZE
Section: utils
Priority: optional
Description: 光盘刻录助手 - Windows 可正常显示中文名、可多次追加的数据光盘刻录
 自带 xorriso 与 Python/PyQt5 运行环境，不依赖任何系统软件包，内网机器可直接安装。
 始终生成 Joliet 目录树（Windows 正确显示中文长文件名），默认不封盘，
 可反复追加；刻录后 MD5 校验；可修复 Windows 未关闭的会话。
EOF

cat > "$ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
command -v update-desktop-database >/dev/null && update-desktop-database -q || true
EOF
chmod 755 "$ROOT/DEBIAN/postinst"

# 旧版本启动器可能留下过 __pycache__，卸载前一并清掉
cat > "$ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
rm -rf /usr/share/discburner/discburner/__pycache__
EOF
chmod 755 "$ROOT/DEBIAN/prerm"

mkdir -p dist
OUTDEB="dist/${PKG}_${VERSION}_${ARCH}.deb"
# -Zxz：新版 dpkg 默认用 zstd 压缩，麒麟 V10（2020 版 dpkg 1.18、SP1 dpkg 1.19）解不开
dpkg-deb -Zxz --root-owner-group --build "$ROOT" "$OUTDEB" >/dev/null 2>&1 \
  || dpkg-deb -Zxz --build "$ROOT" "$OUTDEB" >/dev/null
echo "已生成 $OUTDEB（$(du -h "$OUTDEB" | cut -f1)，安装后约 $((SIZE / 1024)) MB）"
