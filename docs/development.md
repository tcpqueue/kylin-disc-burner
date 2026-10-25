# 开发与构建

[← 返回首页](../README.md)

- [代码结构](#代码结构)
- [从源码运行](#从源码运行)
- [测试](#测试)
- [构建离线安装包](#构建离线安装包)
- [一键打包并测试（release.sh）](#一键打包并测试releasesh)
- [兼容性约束](#兼容性约束)

## 代码结构

```text
kylin-disc-burner/
├── discburner/
│   ├── backend.py          # xorriso 命令构造与输出解析、诊断、文件名恢复（不依赖 Qt）
│   ├── gui.py              # PyQt5 界面
│   ├── selftest.py         # --selftest 自检
│   ├── winsys.py           # Windows 下的光驱和卷信息（ctypes）
│   ├── __main__.py         # python3 -m discburner 入口
│   └── __init__.py         # 版本号
├── tests/
│   ├── test_backend.py     # 解析单元测试 + 对 ISO 文件的真实多次刻录测试
│   ├── gui_smoke.py        # 无界面模式下驱动主窗口走完整流程
│   └── startup_smoke.py    # 走真实入口启动
├── packaging/
│   ├── build_runtime.sh    # 在 Ubuntu 16.04 构建环境里编译 xorriso、打包 Python 运行环境
│   ├── run.py              # PyInstaller 入口
│   ├── discburner.sh       # 启动器（安装为 /usr/bin/discburner）
│   ├── discburner.desktop  # 开始菜单项
│   ├── discburner.svg      # 图标
│   ├── wsl/release.sh      # 一键重新打包并测试（麒麟版）
│   └── windows/            # Windows 版：xorriso 编译脚本、补丁、打包脚本
├── build_deb.sh            # 打 deb 包
└── docs/                   # 文档
```

`backend.py` 不依赖 Qt，所有和 xorriso 打交道的逻辑都在这里，可以单独测试或在命令行脚本中复用。`gui.py` 只负责界面，耗时操作通过 `QProcess`（刻录）或后台线程（诊断、查找原名）执行，界面不会卡住。

## 从源码运行

需要 Python ≥ 3.5、PyQt5 和 xorriso：

```bash
sudo apt install python3-pyqt5 xorriso
git clone https://github.com/tcpqueue/kylin-disc-burner.git
cd kylin-disc-burner
python3 -m discburner
```

系统 xorriso 低于 1.5.8 时，程序仍能运行，但会自动修正 Windows 光盘上的“;1”文件名（见[技术原理](principles.md#xorriso-152-的1-问题)）。也可以用环境变量指定其他 xorriso：

```bash
DISCBURNER_XORRISO=/path/to/xorriso python3 -m discburner
```

## 测试

```bash
# 单元测试 + ISO 镜像上的真实刻录测试（需要 xorriso；有 blkid、mkudffs 时会多跑几项）
python3 -m unittest discover -s tests -v

# 界面流程：刻录、追加、修复提示、诊断窗口、恢复长文件名、写入卡住提示
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py [截图输出目录]

# 走真实入口启动
QT_QPA_PLATFORM=offscreen python3 tests/startup_smoke.py

# 装好的程序自检
discburner --selftest
```

测试全部使用 ISO 镜像文件和虚构的文件名，不需要光驱。和光驱相关的解析测试使用真实光驱输出的样本，样本中的设备序列号、文件名等信息已替换为虚构内容。

## 构建离线安装包

> Windows 绿色版的构建方法见 [Windows 版说明](windows.md#从源码构建)。

### 构建环境

安装包要能同时在麒麟 2020 版（glibc 2.23）和 SP1（glibc 2.31）上运行，所以必须在 **Ubuntu 16.04（xenial）** 里构建。arm64 版可以在 x86 机器上用 `qemu-user-static` 构建。

```bash
# 在任意较新的 Ubuntu / Debian 上，以 root 执行
apt install debootstrap qemu-user-static binfmt-support

debootstrap --arch=amd64 xenial /root/build/xenial-amd64 http://archive.ubuntu.com/ubuntu
debootstrap --arch=arm64 xenial /root/build/xenial-arm64 http://ports.ubuntu.com/ubuntu-ports
```

在两个构建环境里分别安装：

```bash
chroot /root/build/xenial-amd64 apt install -y \
  python3 python3-pyqt5 python3-sip python3-setuptools gcc make zlib1g-dev \
  binutils file fcitx-frontend-qt5 libqt5svg5
```

构建环境需要启用 `universe` 仓库；如果提示依赖不满足，执行一次 `apt-get -f install`。

### 源码包

放在同一个目录里（例如 `/root/build/src`）：

| 文件 | 来源 |
|---|---|
| `xorriso-1.5.8.tar.gz` | <https://ftp.gnu.org/gnu/xorriso/>，建议同时下载 `.sig` 校验签名 |
| `PyInstaller-3.6.tar.gz` | PyPI。3.6 是支持 Python 3.5 的最后一个版本 |
| `altgraph-0.17-py2.py3-none-any.whl` | PyPI |

### 构建

先把项目目录和源码包目录分别拷到构建环境的 `/build/proj`、`/build/src`，并挂载 `/proc`、`/dev`：

```bash
# 1. 在构建环境里生成运行环境（xorriso + PyInstaller 打包的 Python/PyQt5）
chroot /root/build/xenial-arm64 bash /build/proj/packaging/build_runtime.sh \
    /build/proj /build/src /build/out

# 2. 在任意 Linux 上打 deb
./build_deb.sh arm64 <输出目录> /root/build/src/xorriso-1.5.8.tar.gz
```

`build_runtime.sh` 会：

1. 编译 GNU xorriso（关闭 readline、ACL、xattr、zlib 等可选依赖，只依赖 glibc）；
2. 用 PyInstaller 3.6 打包程序。PyInstaller 对 Qt 5.8 以下的 PyQt5 不处理插件，脚本手动加入 `sip` 和全部 Qt 插件；
3. 用 `ldd` 补齐插件的依赖库，再删掉 X11、GL、字体等应该用系统版本的库；
4. 用打好的运行环境跑一遍自检，并以无界面模式启动 5 秒确认界面正常。

`build_deb.sh` 生成 `dist/discburner_<版本>_<架构>.deb`，使用 xz 压缩，不声明任何依赖。

## 一键打包并测试（release.sh）

如果在 Windows 上开发，可以在 WSL 里搭好全部构建和测试环境，以后改了代码一条命令完成重新打包和全部测试：

```bash
wsl -d Ubuntu-24.04 -u root -- bash <项目在 WSL 中的路径>/packaging/wsl/release.sh
```

依次执行：

1. 源码单元测试、界面流程测试、启动测试；
2. 构建 amd64、arm64 运行环境；
3. 打两个 deb；
4. 分别装进麒麟 2020 版、麒麟 SP1、Ubuntu 20.04 arm64（无 Python）、Ubuntu 16.04 arm64 四个环境，逐个运行：
   - `dpkg -i` 安装；
   - 自检（系统运行环境 + 强制自带运行环境）；
   - 在真实 X 显示服务（WSLg）下启动界面并截图；
   - 隐藏系统 PyQt5，确认自动改用自带环境；
   - 卸载并检查有无残留；
5. 输出汇总，有失败项时返回非零。

| 选项 | 作用 |
|---|---|
| `--skip-build` | 跳过运行环境构建，直接用上次的结果（只改了启动器或打包脚本时用） |
| `--only-amd64` / `--only-arm64` | 只处理一个架构 |

截图和自检镜像输出到项目下的 `test-output/`（可以用环境变量 `SHOTS` 修改）。

`release.sh` 用到的环境：

| 路径 | 内容 |
|---|---|
| `/root/build/xenial-amd64`、`xenial-arm64` | Ubuntu 16.04 构建环境 |
| `/root/build/focal-arm64` | Ubuntu 20.04 arm64，不装 Python，模拟飞腾上的麒麟 SP1 |
| `/root/build/src` | 上面的三个源码包 |
| `/root/k20/root`、`/root/k25/root` | 麒麟 V10 2020 版、SP1 的完整系统，从官方 ISO 的 `casper/filesystem.squashfs` 用 `unsquashfs` 解出 |
| `/root/kb` | 从 SP1 提取的 xorriso 1.5.2 和 Python 3.8，用来按麒麟原版环境跑单元测试 |

缺少某个环境时，对应的安装测试会跳过，并在汇总里记为失败。

## 兼容性约束

修改代码时请注意：

| 约束 | 原因 |
|---|---|
| 语法兼容 **Python 3.5**：不用 f-string、海象运算符、`subprocess.run(capture_output=...)` 等 | 麒麟 2020 版系统 Python 是 3.5.2，自带运行环境也是 3.5 |
| 兼容 **Qt 5.5**：新 API 先用 `hasattr` 判断（如 `setDesktopFileName`、`QProcess.errorOccurred`、`AA_EnableHighDpiScaling`） | 自带运行环境是 Qt 5.5，麒麟 2020 版系统是 Qt 5.6 |
| 兼容 **xorriso 1.5.2** 的参数解析：可变长参数后面加 `--` | SP1 系统自带 1.5.2，自带的 xorriso 被拦截时会退回使用 |
| `backend.py` 不能导入 Qt | 方便单独测试，也让 `--selftest` 不依赖图形环境 |
| 新增解析逻辑要有真实输出样本做测试 | 光驱和 xorriso 的输出格式细节很多，凭想象写的正则很容易出错 |
