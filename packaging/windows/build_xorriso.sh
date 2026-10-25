#!/bin/bash
# 在 Cygwin 里编译 Windows 版 xorriso（访问光驱用 libcdio）。
#
#   bash build_xorriso.sh <源码包目录> <输出目录>
#
# 源码包目录里要有 xorriso-*.tar.gz 和 libcdio-2.1.0.tar.bz2。
# Cygwin 需要的包：gcc-core make libiconv-devel pkg-config
#
# 打了两个补丁：
#   libcdio-windows-drives.patch    原版 libcdio 在 Cygwin 下列光驱时用 realpath 去重，
#                                   所有 \\.\X: 都被当成同一个，只剩第一个光驱。电脑上装了
#                                   虚拟光驱时，真正的刻录机就看不到了。
#   libburn-libcdio-enum-free.patch libburn 释放光驱列表后又读了一次旧指针。
#
# libcdio 编成独立的 DLL：静态链接时它和 libburn 有同名函数，链接不过。
set -e
SRC=$(cd "$1" && pwd)
OUT=$2
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=/tmp/discburner-xorriso
PFX=$WORK/cdio
rm -rf "$WORK" && mkdir -p "$WORK"

echo "== libcdio 2.1.0（打补丁）"
tar -xjf "$SRC/libcdio-2.1.0.tar.bz2" -C "$WORK"
cd "$WORK/libcdio-2.1.0"
patch -p1 < "$HERE/libcdio-windows-drives.patch"
./configure --quiet --prefix="$PFX" --enable-shared --disable-static --disable-cxx \
    --disable-cddb --disable-vcd-info --without-cd-drive --without-cd-info --without-cdda-player \
    --without-cd-read --without-iso-info --without-iso-read >/dev/null
make -s -C lib/driver >/dev/null 2>&1 || make -C lib/driver
make -s -C lib/driver install >/dev/null
make -s -C include install >/dev/null

XO_TGZ=$(ls "$SRC"/xorriso-*.tar.gz | sort -V | tail -1)
echo "== $(basename "$XO_TGZ")"
tar -xzf "$XO_TGZ" -C "$WORK"
cd "$WORK"/xorriso-*/
# libburn 的 libcdio 适配层在释放光驱列表后还会读一次旧指针，偶尔崩溃（SIGSEGV）
patch -p1 < "$HERE/libburn-libcdio-enum-free.patch"
CPPFLAGS="-I$PFX/include" LDFLAGS="-L$PFX/lib" LIBS="-liconv -lwinmm" \
LIBCDIO_CFLAGS="-I$PFX/include" LIBCDIO_LIBS="-L$PFX/lib -lcdio -liconv -lwinmm" \
./configure --quiet --disable-shared --enable-static --enable-libcdio --disable-libreadline \
            --disable-libedit --disable-libacl --disable-xattr --disable-zlib --disable-libbz2 \
            --disable-jtethreads >/dev/null
make -s -j"$(nproc)" >/dev/null 2>&1 || make -j"$(nproc)"

rm -rf "$OUT" && mkdir -p "$OUT"
cp xorriso/xorriso.exe "$OUT/"
strip "$OUT/xorriso.exe"
# 带上用到的 Cygwin DLL（cygwin1.dll、cygiconv-2.dll）和打过补丁的 cygcdio-19.dll
for dll in $(PATH="$PFX/bin:$PATH" cygcheck "$OUT/xorriso.exe" | grep -iE 'cygwin64|discburner-xorriso' \
             | grep -vi 'xorriso.exe' | sed 's/^ *//'); do
  cp "$(cygpath -u "$dll")" "$OUT/"
done
"$OUT/xorriso.exe" -version 2>&1 | head -1
echo "   Cygwin $(uname -r)，文件：$(cd "$OUT" && ls | tr '\n' ' ')"
