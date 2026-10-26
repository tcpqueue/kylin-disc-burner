# Windows 版说明

[← 返回首页](../README.md)

- [为什么要有 Windows 版](#为什么要有-windows-版)
- [下载和运行](#下载和运行)
- [和麒麟版的区别](#和麒麟版的区别)
- [限制和验证情况](#限制和验证情况)
- [常见问题](#常见问题)
- [技术实现](#技术实现)
- [从源码构建](#从源码构建)

## 为什么要有 Windows 版

麒麟和 Windows 电脑混用的单位里，同一张光盘经常在两边来回追加。用 Windows 自带的刻录往**麒麟刻的盘**里追加时会出问题：

| 情况 | Windows 自带刻录 | 光盘刻录助手 Windows 版 |
|---|---|---|
| 往麒麟自带刻录刻的盘（没有 Joliet）里追加 | 只认 ISO9660 短名，从此两边都显示 `2023__~1.XLS` | 读 Rock Ridge 长名，两边都显示原名 |
| 刻出来的盘在麒麟上 | 能读 | 能读，文件权限正常（644 / 755），普通用户直接打开 |
| 用 xorriso 1.5.2（麒麟 SP1 自带）追加 Windows 刻的盘 | — | 自动处理文件名的“;1”版本号 |
| 已经变成短名的盘 | 无法修复 | “光盘诊断 → 恢复长文件名” |
| 刻录后校验 | 无 | 逐个文件 MD5 |

Windows 版和麒麟版用同一套代码、同样的刻录参数，**两边可以交替往同一张盘里追加**。

## 下载和运行

从 [Releases](https://github.com/tcpqueue/kylin-disc-burner/releases?q=windows) 下载，有两种形式，功能完全一样：

| | 单文件版 | 绿色版 |
|---|---|---|
| 文件 | `discburner-<版本>-portable.exe`（约 40 MB） | `discburner-<版本>-windows-x64.zip`（约 42 MB） |
| 用法 | 拷到任意位置，双击即可 | 解压到任意位置，双击里面的 `discburner.exe` |
| 启动速度 | 每次启动先解压到临时目录，要多等几秒 | 快 |
| 适合 | 拷到 U 盘随身带、发给别人 | 经常使用的电脑 |
| 源码和许可证 | 打在 exe 里；源码包见绿色版 zip 或项目主页 | 在 `source\`、`LICENSE.txt` |

绿色版不要解压到层级特别深的文件夹里（完整路径超过 180 个字符时程序会打不开）。单文件版个别杀毒软件可能误报（运行时解压程序是这类工具的通病），添加信任即可。

**系统要求**：Windows 7 SP1 / 8.1 / 10 / 11，64 位。Windows 7 需要装过系统更新 KB2533623（随常规 Windows 更新一般已经装好）；程序自带通用 C 运行库，不用另装 VC 运行库。

不需要安装，不写注册表（只在 `HKEY_CURRENT_USER\Software\discburner` 记住上次用的光驱）。删除文件夹就是卸载。

**自检**（不需要光驱）：

```bat
discburner.exe --selftest
```

几秒钟后弹窗显示“自检通过”。

![Windows 版](images/windows.png)

## 和麒麟版的区别

| | 麒麟版 | Windows 版 |
|---|---|---|
| 形式 | deb 安装包 | 绿色版 zip |
| 光驱名称 | `/dev/sr0` | 盘符 `G:` |
| 挂载 | 刻录前自动卸载；诊断时可“挂载并打开” | Windows 自己挂载，不需要卸载；诊断时“打开”即在资源管理器中打开 |
| 刻录后 | 弹出再放入可看到新内容 | 自动刷新卷缓存，资源管理器里直接能看到新文件 |
| 日志位置 | `~/.local/share/discburner/logs/` | `%LOCALAPPDATA%\discburner\logs\` |
| UDF 支持 | 取决于内核 | Windows 自带 |

其他功能——刻录、追加、校验、诊断、恢复长文件名、修复未关闭会话、ISO 镜像、擦除可擦写盘——完全一样，用法见[使用指南](usage.md)和[光盘诊断与修复](diagnose.md)。

## 限制和验证情况

| 项目 | 状态 |
|---|---|
| Windows 11 + USB 外置刻录机：列出光驱、读取光盘、诊断 | ✅ 实测 |
| 往 Windows 刻的多会话 DVD+R 追加（中文名、长文件名、带单引号和空格的文件名） | ✅ 实测：MD5 校验一致，原有文件不变，资源管理器中显示正确，读出的文件与原文件逐字节一致 |
| 自动化测试（多次追加、恢复长文件名、带“;1”的 Windows 盘追加等） | ✅ Windows 上全部通过 |
| 用 Win7 兼容方式构建的版本（Cygwin 3.4.10）在 Windows 11 上实测刻录 | ✅ 追加刻录、MD5 校验、资源管理器读回逐字节一致 |
| Windows 7 | ⚠️ 已按 Win7 兼容方式构建；打包时检查了全部 98 个 exe / dll 的导入表，没有 Windows 8 以后才有的系统函数；**尚未在 Win7 真机上运行** |
| 普通用户（非管理员）账户 | ⚠️ 尚未验证。访问刻录机可能需要管理员权限 |
| 32 位 Windows | ❌ 不支持 |

## 常见问题

<details>
<summary><b>找不到刻录机</b></summary>

- 检查连接和供电，点“刷新”；
- 关闭其他正在使用光驱的刻录软件；
- 如果当前是普通用户账户，右键 `discburner.exe` 选“以管理员身份运行”再试；
- 状态栏如果提示“系统里有光驱 X:，但无法以刻录方式打开”，说明那是虚拟光驱或正被占用。
</details>

<details>
<summary><b>刻录机下拉框里有好几个光驱</b></summary>

虚拟光驱（如 UltraISO、DAEMON Tools 等软件创建的）会排在真正的刻录机后面。程序会记住你上次选的光驱。
</details>

<details>
<summary><b>刻完后资源管理器里看不到新文件</b></summary>

程序刻完会自动让 Windows 重新读取光盘目录。如果资源管理器窗口里还是旧内容，按 F5 刷新，或关掉窗口重新打开光驱。
</details>

<details>
<summary><b>Windows 自带的“刻录到光盘”还能用吗？</b></summary>

可以用，但往麒麟刻的盘里追加时最好用本软件。如果用 Windows 自带的方式，选“**带有 CD/DVD 播放器**”，不要选“类似于 USB 闪存驱动器”。
</details>

## 技术实现

Windows 版的界面和逻辑与麒麟版是同一套 Python 代码，平台差异集中在 `backend.py` 的几个函数和 `winsys.py`：

| 问题 | 做法 |
|---|---|
| 刻录引擎 | GNU xorriso 1.5.8，用 Cygwin 编译，通过 libcdio 向光驱发 SCSI 命令（Windows SPTI），不依赖 Windows 的 IMAPI 刻录服务 |
| 光驱地址 | `\\.\G:` |
| 本地路径 | Cygwin 程序要用 `/cygdrive/c/...` 形式，由 `cygwin_path()` 转换 |
| 命令行参数 | Cygwin 程序会自己再解析一遍 Windows 命令行，文件名里的单引号、星号会被误当成引号和通配符。所以参数一律写进 UTF-8 文本文件，用 `-options_from_file` 读入，每行一条命令 |
| 中文文件名 | 运行 xorriso 时设置 `LANG=C.UTF-8` |
| 文件权限 | Cygwin 按 NTFS 权限换算出的 Rock Ridge 属性是 `rwxrwx---`、属主是 Windows 用户编号，麒麟普通用户读不了。刻录时用 `-chown_r` / `-chgrp_r` / `-find ... -exec chmod` 统一改成 root、644、755 |
| 误把硬盘当光驱 | libburn 遇到不认识的 `\\.\X:` 会当成普通文件读写。用 `-drive_class caution '\\\\.\\*'` 让这类地址只要不是刻录机就拒绝 |
| 卷缓存 | 刻录后对光驱卷发 `FSCTL_DISMOUNT_VOLUME`，Windows 下次访问时重新挂载 |
| 取出文件的权限 | Cygwin 默认把光盘上的 Linux 权限翻译成 NTFS 权限，写出带“拒绝”项的 ACL。自带的 `xorriso\etc\fstab` 给 `/cygdrive` 加了 `noacl`，取出的文件按 Windows 方式继承文件夹权限。Cygwin 把 DLL 所在目录的上一级当根目录，所以程序和 DLL 放在 `xorriso\bin` |
| 字体发虚 | Qt 在 Windows 上默认用 GDI 画字，中文笔画细、对比度低。改用 FreeType（`QT_QPA_PLATFORM=windows:fontengine=freetype`，自己设了这个环境变量时以设置的为准），微软雅黑 10 号 |
| 无控制台 | 绿色版是窗口程序，`--selftest` 的结果用对话框显示；调用 xorriso 时不弹出黑窗口 |
| Windows 7 | Cygwin 从 3.5 版起不支持 Win7，xorriso 用 Cygwin 3.4.10 编译；Python 用 3.8（最后支持 Win7 的版本）；随程序带上 UCRT。打包脚本用 `check_win7.py` 扫描导入表把关 |

### 修复的两个上游问题

构建时给 libcdio 和 libburn 各打了一个补丁（在 `packaging/windows/`）：

1. **libcdio 只能看到第一个光驱**（`libcdio-windows-drives.patch`）：libcdio 列光驱时用 `realpath` 去重，在 Cygwin 下所有 `\\.\X:` 换算出来都一样，于是只保留第一个。电脑上装了虚拟光驱时，真正的刻录机就被丢掉了。补丁让 Windows 设备名直接按字符串比较。
2. **libburn 偶发崩溃**（`libburn-libcdio-enum-free.patch`）：`sg-libcdio.c` 在释放光驱列表后没有清空游标，接着又读了一次已释放的内存。是否崩溃取决于内存布局，参数分行执行时必现。补丁在释放后清空游标并返回。

## 从源码构建

需要：

- [Cygwin](https://cygwin.com/)（64 位），安装 `gcc-core make libiconv-devel pkg-config patch bzip2`。要支持 Windows 7，必须用 **Cygwin 3.4.10**（见下文）；
- Python 3.8（64 位），安装 `PyQt5==5.15.10 PyQt5-Qt5==5.15.2 pyinstaller==5.13.2 pefile`；
- Windows 10 SDK：打包时从里面带上通用 C 运行库（`ucrtbase.dll` 等），Windows 7 上才能运行；
- 源码包：`xorriso-1.5.8.tar.gz`（<https://ftp.gnu.org/gnu/xorriso/>）、`libcdio-2.1.0.tar.bz2`（<https://ftp.gnu.org/gnu/libcdio/>），建议同时下载 `.sig` 校验签名。

```bat
:: 1. 在 Cygwin 里编译 xorriso（含两个补丁），输出到 C:\build\xorriso
C:\cygwin64\bin\bash.exe --login -c "bash /cygdrive/c/<项目>/packaging/windows/build_xorriso.sh /cygdrive/c/build/src /cygdrive/c/build/xorriso"

:: 2. 打绿色版：跑测试 → PyInstaller → 带上 xorriso 和源码 → 打包后自检 → 启动界面 → zip
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 ^
    -Python C:\build\venv38\Scripts\python.exe -Xorriso C:\build\xorriso -Src C:\build\src
```

输出 `dist\discburner-<版本>-windows-x64.zip` 和 `dist\discburner-<版本>-portable.exe`，两种都会自动跑自检和界面启动测试。打包在系统临时目录下进行：Qt 的 DLL 层级较深，放在路径很长的目录里会超过 Windows 260 个字符的路径限制，导致界面无法启动。

### 用 Cygwin 3.4.10 编译（支持 Windows 7）

Cygwin 3.4.10 是最后一个支持 Windows 7 / 8 的版本，官方镜像上已经没有了，要从 Cygwin 安装页面推荐的 [Cygwin Time Machine](http://www.crouchingtigerhiddenfruitbat.org/Cygwin/timemachine.html) 安装，装到单独的目录，不影响已有的 Cygwin：

```bat
setup-x86_64.exe --quiet-mode --no-verify --root C:\cygwin-w7 --local-package-dir C:\cygwin-w7-pkgs ^
    --only-site --site http://ctm.crouchingtigerhiddenfruitbat.org/pub/cygwin/circa/64bit/2024/01/30/231215 ^
    --packages gcc-core,make,libiconv-devel,pkg-config,patch,bzip2,binutils
```

这个存档没有签名文件，所以要加 `--no-verify`。可以这样核对它的可信度：用 Cygwin 公钥验证当前官方镜像 `setup.ini` 的签名，再把存档 `setup.ini` 里和官方重合的包逐个比对 SHA512（本项目构建时比对了 27810 个包，全部一致）；安装程序会按存档 `setup.ini` 里的 SHA512 校验每个下载的包。

然后用 `C:\cygwin-w7\bin\bash.exe` 运行 `build_xorriso.sh` 即可。

源码运行（开发时）：

```bat
set DISCBURNER_XORRISO=C:\build\xorriso\xorriso.exe
python -m discburner
python -m unittest discover -s tests -v
```
