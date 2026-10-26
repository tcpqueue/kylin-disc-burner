# 常见问题

[← 返回首页](../README.md)

### 和 Windows 混用

<details>
<summary><b>在 Windows 上刻盘时，应该选“类似于 USB 闪存驱动器”还是“带有 CD/DVD 播放器”？</b></summary>

如果这张盘以后还要在麒麟上用本软件追加，选“**带有 CD/DVD 播放器**”。这种方式写的是 ISO9660 + Joliet，麒麟可以直接读，本软件也能接着追加，Windows 上的文件名保持不变。

“类似于 USB 闪存驱动器”写的是 UDF 实时文件系统。麒麟可以读（见下一条），但本软件不能往里追加；而且拷完文件必须在 Windows 上正常弹出，否则会留下未关闭的会话。
</details>

<details>
<summary><b>麒麟能读 Windows 刻的 UDF 光盘吗？</b></summary>

可以。麒麟 V10 2020 版和 SP1 的内核都能读取 UDF 2.50 及以下版本，Windows 默认的 UDF 2.01 没有问题。如果没有自动挂载，用“光盘诊断 → 挂载并打开”。详见[挂载 UDF 光盘](diagnose.md#挂载-udf-光盘)。
</details>

<details>
<summary><b>麒麟自带刻录软件刻的旧盘，在 Windows 上名字是乱的，还能救吗？</b></summary>

- **没封盘的**：用本软件往里追加任意一个文件，新会话会给盘上全部文件生成 Joliet 名，Windows 上的旧文件名也会恢复正常。
- **已经被 Windows 追加过、只剩 `2023__~1.XLS` 这种短名的**：用“光盘诊断 → 恢复长文件名”，详见[恢复长文件名](diagnose.md#恢复长文件名)。
- **已封盘的**：无法再写入，只能在麒麟上读出来（麒麟读 Rock Ridge，名字是正常的），再刻到新盘。
</details>

<details>
<summary><b>本软件刻的盘在 Windows 上追加过，再回到麒麟追加会有问题吗？</b></summary>

没有问题。本软件会读入 Windows 写的目录，保留原有文件名继续追加，也会处理 Windows 文件名里的“;1”版本号。
</details>

### 光盘与刻录

<details>
<summary><b>本软件能像 Windows“格式化”那样改变 DVD+R 的文件系统吗？</b></summary>

不能，也没有必要。DVD+R 是一次性光盘，Windows 所谓的“格式化”其实是在盘上写入 UDF 实时文件系统的开头，写过之后这张盘就固定是 UDF 格式了。

本软件始终写 ISO9660 + Joliet + Rock Ridge，兼容性最好，并且同样可以多次追加。可擦写光盘（DVD±RW 等）可以用“擦除可擦写盘”清空后重新使用。
</details>

<details>
<summary><b>一张盘最多能追加多少次？</b></summary>

取决于光盘类型和剩余空间。DVD±R 每次追加的会话开销只有几 MB，追加几十次没有问题；CD-R 每次约 7～14 MB，次数多了空间损失明显。可擦写盘次数不限。
</details>

<details>
<summary><b>需要封盘吗？</b></summary>

一般不需要。普通电脑读取不封盘的光盘没有任何问题，封盘以后反而不能再写。只有极少数老式光驱或播放器要求光盘必须封盘。
</details>

<details>
<summary><b>替换了光盘上的同名文件，为什么剩余空间没增加？</b></summary>

一次性光盘不能改写。替换后新目录指向新文件，旧文件看不到了，但数据还在盘上，空间不会释放。
</details>

<details>
<summary><b>能刻单个超过 4 GB 的文件吗？</b></summary>

可以。Windows 10/11 和麒麟都能正常读取。
</details>

### 故障排查

<details>
<summary><b>提示“未找到刻录机”</b></summary>

1. 确认光驱已连接，外置光驱的 USB 线和电源都已插好；
2. 当前用户需要在 `cdrom` 组里：`sudo usermod -aG cdrom $USER`，然后注销重新登录；
3. 点“刷新”。
</details>

<details>
<summary><b>刻录进度卡住不动</b></summary>

90 秒没有进展时状态行会变红提示。最常见的原因是外置 USB 光驱供电不足：

- 换到机箱背面的 USB 口，或者使用带外接电源的 USB 集线器 / 给光驱接独立电源；
- 速度选“最低速”。

写入失败时程序会如实报告。数据还没开始落盘时，光盘通常没有任何变化，处理好供电后重试即可。
</details>

<details>
<summary><b>刻完了，但文件管理器里看不到新文件</b></summary>

系统缓存了旧的光盘目录。弹出再放入即可。“完成后弹出光盘”默认勾选，就是为了避免这种情况。
</details>

<details>
<summary><b>打开刻录助手后，文件管理器里的光盘不见了 / 读不出来</b></summary>

1.5.1 及以前的版本每次读取光盘信息前会把光盘从系统卸载，麒麟不会自动挂回去，文件管理器就读不到了。请升级到 1.5.2 或更新版本：只读取时不再卸载，刻录等写入操作完成后也会自动挂回去。

不用卸载本软件。已经读不到的光盘，弹出再放入即可；或者在终端执行 `udisksctl mount -b /dev/sr0`。
</details>

<details>
<summary><b>放入光盘后，状态显示“会话未关闭（需修复）”</b></summary>

通常是 Windows 用“类似于 USB 闪存驱动器”方式刻录后没有正常弹出造成的。按提示点“修复未关闭会话”，详见[修复未关闭的会话](diagnose.md#修复未关闭的会话)。**修复之前不要反复弹出、断电或在其他电脑上尝试格式化。**
</details>

<details>
<summary><b>如何反馈问题？</b></summary>

在 [Issues](https://github.com/tcpqueue/kylin-disc-burner/issues) 提交，最好附上：

- 系统版本（`cat /etc/os-release`）和 CPU 架构（`dpkg --print-architecture`）；
- “光盘诊断 → 复制诊断报告”的内容；
- 出错那次的日志（`~/.local/share/discburner/logs/`）。

提交前请检查日志和报告里的文件名、卷标是否包含不便公开的信息。
</details>
