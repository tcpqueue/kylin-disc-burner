# 安装指南

[← 返回首页](../README.md)

- [选择安装包](#选择安装包)
- [离线安装](#离线安装)
- [安装后自检](#安装后自检)
- [安装包里有什么](#安装包里有什么)
- [运行时用哪套环境](#运行时用哪套环境)
- [光驱权限](#光驱权限)
- [升级与卸载](#升级与卸载)
- [安装问题排查](#安装问题排查)

## 选择安装包

从 [Releases](https://github.com/tcpqueue/kylin-disc-burner/releases/latest) 下载与机器 CPU 架构对应的包：

| 架构 | 典型 CPU | 安装包 |
|---|---|---|
| `arm64` | 飞腾 D2000、D3000、FT-2000/4，鲲鹏 | `discburner_<版本>_arm64.deb` |
| `amd64` | 兆芯、海光、Intel、AMD | `discburner_<版本>_amd64.deb` |

不确定时在终端运行：

```bash
dpkg --print-architecture
```

两个包的功能完全一样，只是里面的程序编译给不同的 CPU。

每个 Release 都附带 `SHA256SUMS`，拷到内网之前可以核对一下：

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

## 离线安装

安装包**不依赖任何系统软件包**，也不需要联网，拷到机器上执行一条命令即可：

```bash
sudo dpkg -i discburner_1.5.1_arm64.deb
```

装好后：

- 开始菜单里出现“**光盘刻录助手**”（在“工具/附件”分类，也可以搜“刻录”“光盘”）；
- 终端里可以直接运行 `discburner`。

安装程序不会在桌面创建图标。需要的话，在开始菜单里右键“光盘刻录助手”，选“添加到桌面快捷方式”。

## 安装后自检

自检不需要光驱，几秒钟完成：

```bash
discburner --selftest ~/自检.iso
```

它会用带中文名的测试文件对一个 ISO 镜像做两次刻录（第一次写入 + 一次追加），再读回核对 MD5、会话数和卷标，最后打印用到的 Python 和 xorriso 版本。看到“**自检通过**”说明环境正常。

生成的 `自检.iso` 可以拷到 Windows 上双击打开，核对中文文件名是否正常。

## 安装包里有什么

| 组件 | 安装位置 | 说明 |
|---|---|---|
| 程序本身 | `/usr/share/discburner/discburner/` | Python 源码 |
| xorriso 1.5.8 | `/usr/share/discburner/bin/xorriso` | 由 GNU 官方源码编译，只依赖 glibc 2.23 |
| Python 3.5 + PyQt5 5.5 + Qt 5.5 | `/usr/share/discburner/runtime/` | PyInstaller 打包，包含 fcitx 中文输入法插件 |
| 启动器 | `/usr/bin/discburner` | 自动选择运行环境 |
| 菜单项与图标 | `/usr/share/applications/`、`/usr/share/icons/` | |
| 文档与源码 | `/usr/share/doc/discburner/` | README、许可证、xorriso 完整源码包 |

X11、显卡、字体相关的库（libX11、libGL、fontconfig、freetype 等）**用系统自带的**。这些库桌面系统一定有，而且新版本向下兼容；如果打包自带旧版本，反而容易和较新的显卡驱动冲突。

安装后约占 70～80 MB 磁盘空间。

## 运行时用哪套环境

启动器 `/usr/bin/discburner` 会自动选择：

**Python / PyQt5**

- 系统有 Python 3.5 以上并且能导入 PyQt5（麒麟 2020 版和 SP1 默认都有）→ 用系统的。界面跟随麒麟主题，中文输入法一定能用。
- 否则 → 用自带的运行环境。

**xorriso**

- 系统有 1.5.8 或更新的 xorriso → 用系统的；
- 否则用自带的 1.5.8；
- 自带的也运行不了（例如被执行控制拦截）→ 退回系统里的旧版本，并自动规避旧版本的已知问题（见[技术原理](principles.md#xorriso-152-的1-问题)）。

需要时可以用环境变量强制指定：

```bash
DISCBURNER_RUNTIME=bundled discburner    # 强制用自带的 Python/PyQt5
DISCBURNER_RUNTIME=system  discburner    # 强制用系统的 Python/PyQt5
DISCBURNER_XORRISO=/usr/share/discburner/bin/xorriso discburner   # 指定 xorriso
```

自检输出的最后几行会写明实际用的是哪套环境。

## 光驱权限

普通用户需要在 `cdrom` 组里才能刻录。麒麟默认创建的用户一般已经在这个组里；如果程序提示“未找到刻录机”而光驱确实接好了：

```bash
sudo usermod -aG cdrom $USER
```

然后**注销重新登录**。

## 升级与卸载

升级：直接安装新版本的包即可，各用户的刻录日志会保留。

```bash
sudo dpkg -i discburner_<新版本>_arm64.deb
```

卸载：

```bash
sudo dpkg -r discburner
```

卸载会删除程序的所有文件。每个用户自己的刻录日志在 `~/.local/share/discburner/`，不再需要可以手动删除。

## 安装问题排查

| 现象 | 原因与处理 |
|---|---|
| `dpkg: error: archive ... uses unknown compression` | 安装包被其他工具重新打包过。请使用 Release 页面上的原始文件 |
| 安装成功，但点开没反应 | 在终端运行 `discburner` 查看报错；也可以试试 `DISCBURNER_RUNTIME=bundled discburner` |
| 弹出“应用程序未通过验证”之类的提示 | 麒麟安全中心的执行控制拦截了未签名程序，请管理员放行，或临时改成“警告”模式 |
| 界面里无法输入中文 | 系统 PyQt5 缺少 fcitx 插件时会出现这种情况，改用自带环境：`DISCBURNER_RUNTIME=bundled discburner` |
| 提示“未找到刻录机” | 见上面的[光驱权限](#光驱权限)；外置光驱请确认 USB 已连接并且供电稳定 |
