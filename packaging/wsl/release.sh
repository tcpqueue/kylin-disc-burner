#!/bin/bash
# 一键重新打包并测试。在 WSL（Ubuntu-24.04）里以 root 运行：
#
#   wsl -d Ubuntu-24.04 -u root -- bash <项目目录>/packaging/wsl/release.sh [选项]
#
# 选项：
#   --skip-build   不重新构建运行环境，直接用上次的（只改了启动器/打包脚本时用）
#   --only-amd64 / --only-arm64
#
# 用到的环境（第一次搭建见 docs/development.md“构建离线安装包”）：
#   /root/build/xenial-amd64, xenial-arm64   Ubuntu 16.04 构建环境（arm64 走 qemu）
#   /root/build/focal-arm64                  Ubuntu 20.04 arm64，无 Python，模拟飞腾上的 SP1
#   /root/build/src                          xorriso / PyInstaller / altgraph 源码包
#   /root/k20/root, /root/k25/root           麒麟 V10 2020 版、SP1 2503 完整系统
#   /root/kb                                 从 SP1 提取的 xorriso 1.5.2 + Python 3.8（单元测试用）
set -o pipefail
PROJ=$(cd "$(dirname "$0")/../.." && pwd)
B=/root/build
KB=/root/kb
SHOTS=${SHOTS:-$PROJ/test-output}
XO_SRC=$(ls $B/src/xorriso-*.tar.gz | sort -V | tail -1)
QUIET='QFontDatabase|propagateSizeHints|XDG_RUNTIME_DIR|machine-id|dbus-uuidgen|Session management|xkbcommon|XKB|QT_XKB_CONFIG_ROOT|libpng warning'

SKIP_BUILD=0; ARCHES="amd64 arm64"
for a in "$@"; do
  case $a in
    --skip-build) SKIP_BUILD=1 ;;
    --only-amd64) ARCHES=amd64 ;;
    --only-arm64) ARCHES=arm64 ;;
    *) echo "未知选项 $a"; exit 2 ;;
  esac
done

RESULTS=()
ok()   { RESULTS+=("✅ $*"); echo "   ✅ $*"; }
bad()  { RESULTS+=("❌ $*"); echo "   ❌ $*"; }
step() { echo; echo "################ $*"; }

mount_root() {  # $1 根目录
  local R=$1
  mountpoint -q $R/proc || mount -t proc proc $R/proc
  mountpoint -q $R/dev || mount --bind /dev $R/dev
  mkdir -p $R/tmp/.X11-unix && chmod 1777 $R/tmp
  [ -d /mnt/wslg/.X11-unix ] && { mountpoint -q $R/tmp/.X11-unix || mount --bind /mnt/wslg/.X11-unix $R/tmp/.X11-unix; }
}
umount_root() { umount -l $1/tmp/.X11-unix $1/proc $1/dev 2>/dev/null; }

mkdir -p "$SHOTS"
cd "$PROJ" || exit 1
VERSION=$(python3 -c "import discburner; print(discburner.__version__)")
echo "光盘刻录助手 $VERSION：$PROJ"

# ------------------------------------------------------------------ 1. 源码测试
step "1. 源码测试（Python $(python3 -c 'import platform;print(platform.python_version())') + 麒麟 SP1 的 xorriso 1.5.2）"
export_path="$KB/bin:$PATH"
PATH=$export_path python3 -m unittest discover -s tests >/tmp/ut.log 2>&1 \
  && ok "单元测试 $(grep -oE 'Ran [0-9]+ tests' /tmp/ut.log)" || { bad "单元测试"; tail -20 /tmp/ut.log; }
PATH=$export_path QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py 2>&1 | grep -q 'GUI smoke OK' \
  && ok "界面流程测试" || bad "界面流程测试"
PATH=$export_path QT_QPA_PLATFORM=offscreen python3 tests/startup_smoke.py 2>&1 | grep -q 'startup OK' \
  && ok "启动测试" || bad "启动测试"
if [ -x $KB/sq/usr/bin/python3.8 ]; then
  PATH=$export_path PYTHONHOME=$KB/sq/usr $KB/sq/usr/bin/python3.8 -m unittest discover -s tests >/tmp/ut38.log 2>&1 \
    && ok "单元测试（麒麟 SP1 的 Python 3.8）" || bad "单元测试（麒麟 SP1 的 Python 3.8）"
fi
find . -name __pycache__ -prune -exec rm -rf {} +

# ------------------------------------------------------------------ 2. 构建运行环境
for ARCH in $ARCHES; do
  [ $SKIP_BUILD = 1 ] && break
  step "2. 构建 $ARCH 运行环境（xorriso + Python/PyQt5）$( [ $ARCH = arm64 ] && echo '，qemu 模拟，较慢')"
  R=$B/xenial-$ARCH
  mount_root $R
  cp /etc/resolv.conf $R/etc/resolv.conf
  if ! chroot $R dpkg -s libqt5svg5 >/dev/null 2>&1 || ! chroot $R python3 -c "import PyQt5.QtWidgets" 2>/dev/null; then
    chroot $R env DEBIAN_FRONTEND=noninteractive apt-get update -q >/dev/null
    chroot $R env DEBIAN_FRONTEND=noninteractive apt-get -f install -y -q >/dev/null
    chroot $R env DEBIAN_FRONTEND=noninteractive apt-get install -y -q libqt5svg5 >/dev/null
  fi
  rm -rf $R/build && mkdir -p $R/build
  cp -r "$PROJ" $R/build/proj && rm -rf $R/build/proj/dist $R/build/proj/test-output &&
    find $R/build/proj -name __pycache__ -prune -exec rm -rf {} +
  cp -r $B/src $R/build/src
  if chroot $R env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C.UTF-8 \
       bash /build/proj/packaging/build_runtime.sh /build/proj /build/src /build/out 2>&1 \
       | grep -vE "$QUIET" | tee /tmp/build-$ARCH.log | grep -E '^==|自检通过|界面启动|失败|缺少|Error'; then :; fi
  if grep -q '界面启动正常' /tmp/build-$ARCH.log; then
    rm -rf $B/out-$ARCH && cp -a $R/build/out $B/out-$ARCH
    ok "$ARCH 运行环境构建（$(du -sh $B/out-$ARCH/runtime | cut -f1)）"
  else
    bad "$ARCH 运行环境构建，日志 /tmp/build-$ARCH.log"
  fi
  umount_root $R
done

# ------------------------------------------------------------------ 3. 打 deb
step "3. 打包"
rm -f dist/*.deb
for ARCH in $ARCHES; do
  if [ -x $B/out-$ARCH/runtime/discburner ]; then
    bash build_deb.sh $ARCH $B/out-$ARCH "$XO_SRC" && ok "$(ls dist/*_$ARCH.deb)"
  else
    bad "$ARCH 没有可用的运行环境"
  fi
done
find . -name __pycache__ -prune -exec rm -rf {} +

# ------------------------------------------------------------------ 4. 安装测试
install_test() {  # $1 根目录  $2 名称  $3 deb  $4 界面超时秒数
  local R=$1 name=$2 deb=$3 tmo=$4
  [ -d "$R" ] || { bad "$name：环境不存在，跳过"; return; }
  step "4. 安装测试：$name"
  mount_root $R
  local E="env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C.UTF-8 DISPLAY=:0"
  rm -f $R/usr/local/bin/xorriso
  cp "$deb" $R/tmp/pkg.deb
  chroot $R $E dpkg -i /tmp/pkg.deb >/tmp/dpkg.log 2>&1 && ok "$name：dpkg -i" || { bad "$name：dpkg -i"; cat /tmp/dpkg.log; umount_root $R; return; }

  local out
  out=$(chroot $R $E discburner --selftest /tmp/selftest.iso 2>&1)
  if echo "$out" | grep -q 自检通过; then
    ok "$name：自检（$(echo "$out" | grep -oE 'Python : [^ ]+' | cut -c10-) / xorriso $(echo "$out" | grep -oE 'xorriso: [^ ]+' | cut -c10-)）"
    cp $R/tmp/selftest.iso "$SHOTS/selftest-$name.iso" 2>/dev/null
  else bad "$name：自检"; echo "$out"; fi

  if chroot $R $E python3 -c "import PyQt5.QtWidgets" >/dev/null 2>&1; then
    chroot $R $E DISCBURNER_RUNTIME=bundled discburner --selftest 2>&1 | grep -q 自检通过 \
      && ok "$name：强制自带运行环境自检" || bad "$name：强制自带运行环境自检"
  fi

  if [ -d /mnt/wslg/.X11-unix ]; then
    for rt in system bundled; do
      chroot $R $E python3 -c "import PyQt5.QtWidgets" >/dev/null 2>&1 || [ $rt = bundled ] || continue
      rm -f $R/tmp/shot.png
      chroot $R $E DISCBURNER_RUNTIME=$rt DISCBURNER_SCREENSHOT=/tmp/shot.png timeout $tmo discburner >/tmp/gui.log 2>&1
      if [ -s $R/tmp/shot.png ]; then
        cp $R/tmp/shot.png "$SHOTS/$name-$rt.png"; ok "$name：界面（$rt），截图 $name-$rt.png"
      else bad "$name：界面（$rt）"; grep -vE "$QUIET" /tmp/gui.log | head -5; fi
    done
  fi

  local pq
  pq=$(chroot $R $E python3 -c "import PyQt5, os; print(os.path.dirname(PyQt5.__file__))" 2>/dev/null)
  if [ -n "$pq" ]; then
    mv $R$pq $R$pq.hidden
    chroot $R $E discburner --selftest 2>&1 | grep -q 'runtime/discburner' \
      && ok "$name：去掉系统 PyQt5 后自动改用自带环境" || bad "$name：去掉系统 PyQt5 后自动改用自带环境"
    mv $R$pq.hidden $R$pq
  fi

  chroot $R $E dpkg -r discburner >/dev/null 2>&1
  local left; left=$(ls $R/usr/share/discburner 2>/dev/null | wc -l)
  [ "$left" = 0 ] && ok "$name：卸载无残留" || bad "$name：卸载后残留 $left 项"
  rm -f $R/tmp/pkg.deb $R/tmp/selftest.iso $R/tmp/shot.png
  umount_root $R
}

for ARCH in $ARCHES; do
  DEB=$(ls dist/*_$ARCH.deb 2>/dev/null) || continue
  if [ $ARCH = amd64 ]; then
    install_test /root/k20/root kylin2020 "$DEB" 30
    install_test /root/k25/root kylin-sp1 "$DEB" 30
  else
    install_test $B/focal-arm64 focal-arm64-nopython "$DEB" 180
    install_test $B/xenial-arm64 xenial-arm64 "$DEB" 180
  fi
done

# ------------------------------------------------------------------ 汇总
step "汇总"
printf '%s\n' "${RESULTS[@]}"
echo
echo "截图与自检镜像：$SHOTS（自检镜像可在 Windows 上打开核对中文名）"
ls -la dist/
if printf '%s\n' "${RESULTS[@]}" | grep -q '^❌'; then echo "有失败项"; exit 1; fi
echo "全部通过"
