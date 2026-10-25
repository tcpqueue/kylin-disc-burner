# -*- coding: utf-8 -*-
"""Windows 下的光驱和卷信息（ctypes 调 kernel32），只在 Windows 上导入。

Windows 自己会挂载光盘：盘上的文件系统能识别时，光驱盘符下就能直接打开，
所以这里不需要“挂载 / 卸载”，只需要查询卷的状态。
"""

import ctypes
import os
import string
from ctypes import wintypes

DRIVE_CDROM = 5
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOOPENFILEERRORBOX = 0x8000

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetLogicalDrives.restype = wintypes.DWORD
_k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_k32.GetDriveTypeW.restype = wintypes.UINT
_k32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
_k32.GetVolumeInformationW.restype = wintypes.BOOL
_k32.SetErrorMode.argtypes = [wintypes.UINT]
_k32.SetErrorMode.restype = wintypes.UINT


class _QuietErrors(object):
    """光驱里没盘时，查询卷信息会弹出“驱动器中没有磁盘”的系统对话框，这里把它关掉。"""

    def __enter__(self):
        self.old = _k32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX)

    def __exit__(self, *exc):
        _k32.SetErrorMode(self.old)


def cdrom_letters():
    mask = _k32.GetLogicalDrives()
    return [c for i, c in enumerate(string.ascii_uppercase)
            if mask >> i & 1 and _k32.GetDriveTypeW(c + ":\\") == DRIVE_CDROM]


def volume_info(letter):
    """(卷标, 文件系统名)，例如 ('资料盘', 'CDFS') 或 ('', 'UDF')；没有可识别的卷时返回 None。"""
    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    with _QuietErrors():
        ok = _k32.GetVolumeInformationW(letter + ":\\", label, 261, None, None, None, fs, 261)
    return (label.value, fs.value) if ok else None


def open_folder(path):
    try:
        os.startfile(path)
        return True
    except OSError:
        return False


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FSCTL_DISMOUNT_VOLUME = 0x00090020
_INVALID_HANDLE = wintypes.HANDLE(-1).value

_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                 wintypes.LPVOID]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def dismount(letter):
    """让 Windows 丢掉光盘卷的缓存。刻录追加后不这样做，资源管理器里还是旧目录，
    要弹出再放入才能看到新文件；卸下后下次访问会自动重新挂载。"""
    h = _k32.CreateFileW(r"\\.\%s:" % letter, GENERIC_READ | GENERIC_WRITE, 3, None,
                         OPEN_EXISTING, 0, None)
    if h == _INVALID_HANDLE:
        return False
    try:
        n = wintypes.DWORD()
        return bool(_k32.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0,
                                         ctypes.byref(n), None))
    finally:
        _k32.CloseHandle(h)
