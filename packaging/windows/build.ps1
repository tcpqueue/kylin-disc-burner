# 打 Windows 绿色版（zip）。
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 `
#       -Python C:\discburner-build\venv38\Scripts\python.exe `
#       -Xorriso C:\discburner-build\xorriso -Src C:\discburner-build\src
#
#   -Python   装好 PyQt5 5.15、PyInstaller 5.13、pefile 的 Python 3.8（64 位）
#   -Xorriso  build_xorriso.sh 的输出目录（xorriso.exe 和几个 Cygwin DLL）。
#             要支持 Windows 7，必须用 Cygwin 3.4.10 编译（3.5 起不支持 Win7）
#   -Src      xorriso、libcdio 源码包所在目录，一起放进 zip
#   -Work     临时目录。要用短路径：Qt 的 DLL 层级深，整条路径超过 260 个字符就加载不了
#
# 输出：dist\discburner-<版本>-windows-x64.zip
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Xorriso,
    [Parameter(Mandatory = $true)][string]$Src,
    [string]$Work = "$env:TEMP\dbw"
)
$ErrorActionPreference = "Stop"
$Proj = (Resolve-Path "$PSScriptRoot\..\..").Path
$Here = $PSScriptRoot
Set-Location $Proj
$Version = (& $Python -c "import discburner; print(discburner.__version__)").Trim()
$Name = "discburner-$Version-windows"
$Dist = Join-Path $Proj "dist"
Write-Host "== 光盘刻录助手 $Version"
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
New-Item -ItemType Directory -Force $Work, $Dist | Out-Null

Write-Host "== 单元测试"
$env:DISCBURNER_XORRISO = Join-Path $Xorriso "xorriso.exe"
# 用 cmd 转一下：PowerShell 5.1 会把原生程序写到 stderr 的内容当成错误
cmd /c "`"$Python`" -B -m unittest discover -s tests 2>&1" | Select-Object -Last 3 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "单元测试失败" }
Remove-Item Env:\DISCBURNER_XORRISO

Write-Host "== 图标"
& $Python "$Here\make_ico.py" "$Proj\packaging\discburner.svg" "$Work\discburner.ico"

Write-Host "== PyInstaller"
# 经 pyinstaller_dpi.py 运行：在 exe 清单里声明支持高分辨率
& $Python "$Here\pyinstaller_dpi.py" --noconfirm --clean --log-level WARN --windowed `
    --name discburner --icon "$Work\discburner.ico" `
    --add-data "$Proj\packaging\discburner.svg;." `
    --hidden-import discburner.winsys --hidden-import PyQt5.QtSvg `
    --exclude-module tkinter --exclude-module unittest --exclude-module pydoc `
    --paths $Proj --distpath "$Work\dist" --workpath "$Work\pyi" --specpath $Work `
    "$Proj\packaging\run.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$App = Join-Path $Work $Name
Move-Item "$Work\dist\discburner" $App
# Python 3.8 依赖通用 C 运行库（UCRT）。Windows 7 没装 KB2999226 时系统里没有，
# 要随程序带上。PyInstaller 会从 PATH 里找，装了 Windows 10 SDK 的机器能找到
if (-not (Test-Path (Join-Path $App "ucrtbase.dll"))) {
    throw "没有打进 ucrtbase.dll：请安装 Windows 10 SDK，并把含 ucrtbase.dll 的目录（如 Windows Kits\10\Redist\ucrt\DLLs\x64）加到 PATH"
}
Copy-Item -Recurse $Xorriso (Join-Path $App "xorriso")
Copy-Item "$Proj\LICENSE" (Join-Path $App "LICENSE.txt")
# 记事本按带 BOM 的 UTF-8 打开最稳妥
$readme = (Get-Content -Raw -Encoding UTF8 "$Here\README.txt").Replace("{VERSION}", $Version).Replace("`n", "`r`n")
[IO.File]::WriteAllText((Join-Path $App "README.txt"), $readme, (New-Object Text.UTF8Encoding $true))

$cygver = [Diagnostics.FileVersionInfo]::GetVersionInfo((Join-Path $Xorriso "cygwin1.dll")).ProductVersion
$SrcOut = New-Item -ItemType Directory -Force (Join-Path $App "source")
Copy-Item (Get-ChildItem $Src -Include "xorriso-*.tar.gz", "libcdio-*.tar.bz2" -Recurse) $SrcOut
Copy-Item "$Here\*.patch", "$Here\build_xorriso.sh" $SrcOut
@"
xorriso\ 目录里程序的来源
========================

xorriso.exe      GNU xorriso（libburnia 项目），源码：$(Split-Path -Leaf (Get-ChildItem $Src -Filter "xorriso-*.tar.gz" | Select-Object -First 1))
                 打了补丁 libburn-libcdio-enum-free.patch
cygcdio-19.dll   GNU libcdio 2.1.0，源码：libcdio-2.1.0.tar.bz2
                 打了补丁 libcdio-windows-drives.patch
cygwin1.dll      Cygwin $cygver 运行库（LGPLv3），未修改
cygiconv-2.dll   GNU libiconv（LGPL），未修改
                 这两个的源码是 Cygwin 同版本的 -src 包（包名 cygwin、libiconv）：
                 https://cygwin.com/packages/
                 官方镜像只保留最新几个版本；3.4.x 等旧版本在 Cygwin Time Machine 存档里：
                 http://www.crouchingtigerhiddenfruitbat.org/Cygwin/timemachine.html

编译方法见 build_xorriso.sh。
"@ | Set-Content -Encoding UTF8 (Join-Path $SrcOut "SOURCES.txt")

Write-Host "== Windows 7 兼容检查"
& $Python "$Here\check_win7.py" $App
if ($LASTEXITCODE -ne 0) { throw "有 Windows 8 以后才有的系统函数：xorriso 要用 Cygwin 3.4.10 编译，见 docs\windows.md" }

Write-Host "== 打包后自检"
$exe = Join-Path $App "discburner.exe"
$out = Join-Path $Work "selftest.txt"
$env:DISCBURNER_SELFTEST_OUT = $out
Start-Process $exe -ArgumentList "--selftest" -Wait
Remove-Item Env:\DISCBURNER_SELFTEST_OUT
$text = Get-Content -Raw -Encoding UTF8 $out
Write-Host $text
if ($text -notmatch "自检通过") { throw "自检失败" }

Write-Host "== 界面启动"
$shot = Join-Path $Work "screenshot.png"
$env:DISCBURNER_SCREENSHOT = $shot
Start-Process $exe -Wait
Remove-Item Env:\DISCBURNER_SCREENSHOT
if (-not (Test-Path $shot)) { throw "界面没有启动" }

Write-Host "== zip"
$zip = Join-Path $Dist "$Name-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
& $Python -c @"
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
base = os.path.dirname(src)
with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for root, dirs, files in os.walk(src):
        for f in files:
            p = os.path.join(root, f)
            z.write(p, os.path.relpath(p, base))
"@ $App $zip
$size = "{0:N1} MB" -f ((Get-Item $zip).Length / 1MB)
Write-Host "已生成 $zip（$size）"
