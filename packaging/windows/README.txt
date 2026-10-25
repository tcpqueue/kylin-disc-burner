光盘刻录助手 {VERSION}（Windows 绿色版）
========================================

双击“discburner.exe”即可运行，不需要安装。整个文件夹可以拷到任何位置或 U 盘里使用，
删除文件夹就是卸载。
请不要解压到层级特别深的文件夹里（完整路径超过 180 个字符时程序会打不开）。

系统要求
  Windows 7 SP1 / 8.1 / 10 / 11（64 位）
  Windows 7 需要装过系统更新 KB2533623（一般随常规更新已装好）

功能
  · 刻出来的盘在 Windows 和银河麒麟上都显示完整的中文长文件名
  · 默认不封盘，同一张盘可以多次追加；刻录后逐个文件 MD5 校验
  · 往麒麟刻的光盘追加时，不会把文件名写坏（Windows 自带刻录会）
  · 光盘诊断；找回已经变成 2023__~1.XLS 这类短名的原文件名
  · 检测并关闭 Windows 遗留的未关闭会话

自检
  在本文件夹打开命令提示符，运行：
      discburner.exe --selftest
  不需要光驱，几秒钟后弹窗显示“自检通过”即可。

日志
  %LOCALAPPDATA%\discburner\logs\

找不到刻录机？
  · 检查光驱连接和供电，点“刷新”
  · 普通用户账户如果看不到刻录机，右键程序选“以管理员身份运行”再试
  · 刻录时关闭其他刻录软件

文件夹内容
  discburner.exe    主程序（光盘刻录助手）
  xorriso\          刻录引擎 GNU xorriso 1.5.8（Cygwin 编译）及其运行库
  source\           xorriso、libcdio 的源码和补丁（GPL 要求随程序提供）
  LICENSE.txt       许可证（GPL-3.0）

项目主页：https://github.com/tcpqueue/kylin-disc-burner
