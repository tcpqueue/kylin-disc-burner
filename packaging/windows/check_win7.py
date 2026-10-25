# -*- coding: utf-8 -*-
"""检查绿色版能不能在 Windows 7 上启动：扫描所有 exe / dll / pyd 的导入表，
找出 Windows 8 以后才有的系统函数。静态导入了这类函数的程序在 Win7 上根本起不来。

    python check_win7.py <绿色版目录>      有问题时返回 1

只能发现静态导入的问题；程序运行时用 GetProcAddress 动态取的函数查不到。
需要 pefile（pip install pefile）。
"""
import os, sys
import pefile

# Windows 8 及以后才有的常见 API（静态导入就会导致 Win7 上无法启动）
WIN8_FUNCS = {
    "GetSystemTimePreciseAsFileTime", "CreateFile2", "CopyFile2", "GetOverlappedResultEx",
    "WaitOnAddress", "WakeByAddressSingle", "WakeByAddressAll", "PrefetchVirtualMemory",
    "SetThreadDescription", "GetThreadDescription", "PathCchCanonicalizeEx", "PathCchCombineEx",
    "PathCchAppendEx", "PathCchSkipRoot", "PathCchCanonicalize", "PathCchCombine",
    "GetCurrentPackageId", "GetCurrentPackageFullName", "GetPackageFamilyName",
    "RoInitialize", "RoGetActivationFactory", "WindowsCreateString", "SetProcessDpiAwareness",
    "GetDpiForMonitor", "GetDpiForWindow", "SetProcessDpiAwarenessContext", "CreateRemoteThreadEx",
    "GetFirmwareType", "MapViewOfFileFromApp", "VirtualAllocFromApp", "GetProcessMitigationPolicy",
    "SetProcessMitigationPolicy", "DiscardVirtualMemory", "OfferVirtualMemory",
    "SetDefaultDllDirectories", "AddDllDirectory", "RemoveDllDirectory",
}
# 只在 KB2533623 里补到 Win7 的（装了常规更新就有）
KB2533623 = {"SetDefaultDllDirectories", "AddDllDirectory", "RemoveDllDirectory"}
WIN8_DLLS = {"api-ms-win-core-path-l1-1-0.dll", "combase.dll", "shcore.dll",
             "api-ms-win-core-winrt-l1-1-0.dll", "api-ms-win-core-winrt-string-l1-1-0.dll",
             "api-ms-win-core-synch-l1-2-0.dll"}

root = sys.argv[1]
bundled = {f.lower() for _, _, fs in os.walk(root) for f in fs}
problems = {}
count = 0
for dirpath, _, files in os.walk(root):
    for f in files:
        if not f.lower().endswith((".exe", ".dll", ".pyd")):
            continue
        p = os.path.join(dirpath, f)
        try:
            pe = pefile.PE(p, fast_load=True)
            pe.parse_data_directories([pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        except pefile.PEFormatError:
            continue
        count += 1
        for imp in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
            dll = imp.dll.decode().lower()
            if dll in WIN8_DLLS and dll not in bundled:
                problems.setdefault(os.path.relpath(p, root), set()).add("DLL " + dll)
            for s in imp.imports:
                name = s.name.decode() if s.name else ""
                if name in WIN8_FUNCS:
                    tag = "（需 KB2533623）" if name in KB2533623 else ""
                    problems.setdefault(os.path.relpath(p, root), set()).add("%s!%s%s" % (dll, name, tag))
print("扫描了 %d 个 PE 文件" % count)
for k in sorted(problems):
    print(" ", k, "→", ", ".join(sorted(problems[k])))
if not problems:
    print("  没有发现 Windows 8 才有的静态导入")
# 只缺 KB2533623 里的函数不算问题（Win7 SP1 装了常规更新就有）
sys.exit(1 if any(not x.endswith("）") for v in problems.values() for x in v) else 0)
