#!/bin/bash
# 在 Ubuntu 16.04（glibc 2.23）构建环境里运行，生成自带运行环境：
#   $OUT/xorriso        GNU xorriso 独立版，只依赖 glibc
#   $OUT/runtime/       PyInstaller 打包的 Python 3.5 + PyQt5 5.5 + 本程序
# 按 glibc 2.23 编译，所以麒麟 V10 2020 版（2.23）和 SP1（2.31）都能运行。
#
# 用法（在构建环境内）：build_runtime.sh <项目目录> <源码包目录> <输出目录>
set -e
PROJ=$1; SRC=$2; OUT=$3
XO_TGZ=$(ls "$SRC"/xorriso-*.tar.gz | sort -V | tail -1)
PYI_TGZ="$SRC/PyInstaller-3.6.tar.gz"
ALTGRAPH="$SRC/altgraph-0.17-py2.py3-none-any.whl"
WORK=/tmp/discburner-build
rm -rf "$WORK" "$OUT"; mkdir -p "$WORK" "$OUT"
export LANG=C.UTF-8

echo "== [$(uname -m)] xorriso: $(basename "$XO_TGZ")"
tar -xzf "$XO_TGZ" -C "$WORK"
cd "$WORK"/xorriso-*/
./configure --quiet --disable-libreadline --disable-libedit --disable-libacl \
            --disable-xattr --disable-zlib --disable-libbz2 --disable-libcdio \
            --disable-jtethreads >/dev/null
make -s -j"$(nproc)" >/dev/null 2>&1 || make -j"$(nproc)"
cp xorriso/xorriso "$OUT/xorriso"
strip "$OUT/xorriso"
"$OUT/xorriso" -version 2>&1 | head -1
echo "   动态依赖：$(ldd "$OUT/xorriso" | awk '{print $1}' | tr '\n' ' ')"

echo "== [$(uname -m)] PyInstaller 3.6"
tar -xzf "$PYI_TGZ" -C "$WORK"
PYI=$(echo "$WORK"/PyInstaller-3.6)
# altgraph 要解开带上 dist-info，PyInstaller 会用 pkg_resources 检查它是否“已安装”
python3 -m zipfile -e "$ALTGRAPH" "$WORK/site"
export PYTHONPATH="$WORK/site:$PYI"
if ! ls "$PYI"/PyInstaller/bootloader/Linux-64bit*/run >/dev/null 2>&1 ||
   [ "$(uname -m)" != "x86_64" ]; then
  echo "   编译 bootloader"
  (cd "$PYI/bootloader" && python3 ./waf distclean all --no-lsb >/dev/null 2>&1 ||
                           python3 ./waf distclean all >/dev/null)
fi
ls -d "$PYI"/PyInstaller/bootloader/Linux-64bit*

# PyInstaller 3.6 的 PyQt5 钩子靠 QLibraryInfo.version()（Qt 5.8 才有）判断版本，
# 在 Qt 5.5 上整个钩子被跳过，sip 和 Qt 插件都不会收集。这里手动指定。
# 插件本身的依赖库（libQt5XcbQpa、libxcb-* 等）PyInstaller 不会跟进，下面打包后再补。
QTPLUG=$(ls -d /usr/lib/*/qt5/plugins)
PLUGIN_ARGS=()
for sub in platforms xcbglintegrations imageformats iconengines platforminputcontexts; do
  for so in "$QTPLUG/$sub"/*.so; do
    [ -e "$so" ] || continue
    case "$(basename "$so")" in
      libqeglfs.so|libqlinuxfb.so|libqminimalegl.so) continue ;;  # 桌面用不到
    esac
    PLUGIN_ARGS+=(--add-binary "$so:PyQt5/Qt/plugins/$sub")
  done
done
cd "$WORK"
python3 "$PYI/pyinstaller.py" --noconfirm --clean --onedir --name discburner \
    --distpath "$WORK/dist" --workpath "$WORK/w" --specpath "$WORK/w" \
    --paths "$PROJ" --hidden-import sip \
    "${PLUGIN_ARGS[@]}" \
    --exclude-module tkinter --exclude-module PyQt5.QtNetwork \
    --log-level WARN \
    "$PROJ/packaging/run.py"
mv "$WORK/dist/discburner" "$OUT/runtime"

# 整理共享库：
# 1. 补齐手动加入的 Qt 插件的依赖（PyInstaller 3.6 不会跟进 --add-binary 的依赖）
# 2. 删掉必须用系统自带版本的库。这些库系统里一定有，且新版本向下兼容；
#    若用 16.04 的旧版本，会和系统较新的显卡驱动、字体库混用，导致符号缺失崩溃。
#    名单参考 AppImage 项目的 excludelist。
HOST_LIBS='^(ld-linux.*|linux-vdso|libc|libm|libdl|libpthread|librt|libresolv|libutil|libnsl|libanl|libBrokenLocale|libnss_.*|libGL|libEGL|libGLX|libGLdispatch|libGLESv2|libOpenGL|libglapi|libdrm|libgbm|libxshmfence|libX11|libX11-xcb|libxcb|libxcb-dri2|libxcb-dri3|libxcb-glx|libxcb-present|libXext|libXau|libXdmcp|libXdamage|libXfixes|libXxf86vm|libXrender|libfontconfig|libfreetype|libharfbuzz|libgraphite2|libexpat|libz|libglib-2\.0|libgobject-2\.0|libgthread-2\.0|libgmodule-2\.0|libpcre|libstdc\+\+|libgcc_s|libuuid|libasound|libjack)\.so'
is_host_lib() { basename "$1" | grep -qE "$HOST_LIBS"; }
RT="$OUT/runtime"
for pass in 1 2 3 4 5; do
  added=0
  while read -r lib; do
    name=$(basename "$lib")
    is_host_lib "$name" && continue
    [ -e "$RT/$name" ] && continue
    cp -L "$lib" "$RT/$name"; added=$((added + 1))
  done < <(find "$RT" -type f \( -name '*.so*' -o -name discburner \) -print0 |
           xargs -0 -n1 env LD_LIBRARY_PATH="$RT" ldd 2>/dev/null |
           awk '/=> \//{print $3}' | sort -u)
  [ $added -eq 0 ] && break
  echo "   第 $pass 轮补齐依赖库 $added 个"
done
removed=$(cd "$RT" && for f in *.so*; do if is_host_lib "$f"; then echo "$f"; fi; done)
(cd "$RT" && rm -f $removed)
echo "   改用系统版本的库：$(echo $removed | tr '\n' ' ')"
missing=$(find "$RT" -type f -name '*.so*' -print0 | xargs -0 -n1 env LD_LIBRARY_PATH="$RT" ldd 2>/dev/null |
          grep 'not found' | sort -u)
[ -z "$missing" ] || { echo "仍缺少依赖："; echo "$missing"; exit 1; }
echo "   运行环境大小：$(du -sh "$OUT/runtime" | cut -f1)"
echo "   Qt 插件：$(cd "$OUT/runtime/PyQt5/Qt/plugins" && ls */*.so | tr '\n' ' ')"
[ -e "$OUT/runtime/PyQt5/Qt/plugins/platforms/libqxcb.so" ] || { echo "缺少 xcb 显示插件"; exit 1; }
[ -n "$(ls "$OUT"/runtime/libQt5XcbQpa.so* 2>/dev/null)" ] || { echo "缺少 libQt5XcbQpa"; exit 1; }

echo "== [$(uname -m)] 自检（自带 Python + 新编的 xorriso）"
DISCBURNER_XORRISO="$OUT/xorriso" "$OUT/runtime/discburner" --selftest "$OUT/selftest.iso"
echo "== [$(uname -m)] 界面启动（offscreen，5 秒后仍在运行即正常）"
set +e
DISCBURNER_XORRISO="$OUT/xorriso" QT_QPA_PLATFORM=offscreen timeout 5 "$OUT/runtime/discburner" >"$WORK/gui.log" 2>&1
rc=$?
set -e
grep -vE 'QFontDatabase|propagateSizeHints' "$WORK/gui.log" || true
[ $rc -eq 124 ] && echo "   界面启动正常" || { echo "   界面启动失败，退出码 $rc"; exit 1; }
