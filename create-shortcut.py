# -*- coding: utf-8 -*-
"""创建 Windows 快捷方式（纯标准库，ctypes 调 ShellLink COM）。

本文件既是可复用模块（`create_shortcut()` 供 create-autostart.py 调用），
也能直接运行 —— 不带参数时在桌面创建「Codex 同步面板」快捷方式。

    python create-shortcut.py                     # 桌面 → 面板(ui.cmd)
    python create-shortcut.py --target xxx.lnk    # 指定目标与输出
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

HRESULT = wintypes.LONG          # HRESULT 本质是 32 位有符号整数

HERE = Path(__file__).resolve().parent
DEFAULT_TARGET = HERE / "ui.cmd"
DEFAULT_DESC = "Codex 同步助手"


def _guid(d1, d2, d3, b4):
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]
    return GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*b4))


CLSID_ShellLink = _guid(0x00021401, 0, 0, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
IID_IShellLinkW = _guid(0x000214F9, 0, 0, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
IID_IPersistFile = _guid(0x0000010B, 0, 0, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
CLSCTX_INPROC_SERVER = 1


def create_shortcut(lnk_path, target, workdir=None, description=None,
                    icon=None):
    """写出一个 .lnk，指向 target。返回 lnk 路径。

    lnk_path / target / workdir 均可传 str 或 Path。
    """
    lnk_path = Path(lnk_path)
    target = Path(target)
    workdir = Path(workdir) if workdir else target.parent
    description = description or DEFAULT_DESC

    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    try:
        shell_link = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_ShellLink), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_IShellLinkW), ctypes.byref(shell_link))
        if hr != 0 or not shell_link:
            raise RuntimeError(f"CoCreateInstance 失败 HRESULT={hr}")

        vtable = ctypes.cast(
            shell_link,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

        # IShellLinkW vtable：SetDescription=7, SetWorkingDirectory=9,
        #                     SetIconLocation=11, SetPath=20
        SetDescription = ctypes.WINFUNCTYPE(
            HRESULT, ctypes.c_void_p, wintypes.LPCWSTR)(vtable[7])
        SetWorkingDirectory = ctypes.WINFUNCTYPE(
            HRESULT, ctypes.c_void_p, wintypes.LPCWSTR)(vtable[9])
        SetIconLocation = ctypes.WINFUNCTYPE(
            HRESULT, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_int)(vtable[11])
        SetPath = ctypes.WINFUNCTYPE(
            HRESULT, ctypes.c_void_p, wintypes.LPCWSTR)(vtable[20])

        SetPath(shell_link, str(target))
        SetWorkingDirectory(shell_link, str(workdir))
        SetDescription(shell_link, description)
        if icon:
            SetIconLocation(shell_link, str(icon), 0)

        # QueryInterface → IPersistFile（vtable index 0）
        persist = ctypes.c_void_p()
        QueryInterface = ctypes.WINFUNCTYPE(
            HRESULT, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p))(vtable[0])
        qi_hr = QueryInterface(shell_link, ctypes.byref(IID_IPersistFile),
                               ctypes.byref(persist))
        if qi_hr != 0 or not persist:
            raise RuntimeError(f"QueryInterface(IPersistFile) 失败 HRESULT={qi_hr}")

        # IPersistFile vtable：Save=6
        pvtable = ctypes.cast(
            persist,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        Save = ctypes.WINFUNCTYPE(
            HRESULT, ctypes.c_void_p, wintypes.LPCWSTR, wintypes.BOOL)(pvtable[6])
        save_hr = Save(persist, str(lnk_path), True)
        if save_hr != 0:
            raise RuntimeError(f"Save 失败 HRESULT={save_hr}")
    finally:
        ole32.CoUninitialize()

    return lnk_path


def _known_folder(csidl: int, fallback: Path) -> Path:
    """SHGetFolderPathW(CSIDL) → 目录。取不到就用 fallback。"""
    buf = ctypes.create_unicode_buffer(260)
    try:
        ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    except Exception:  # noqa: BLE001
        return fallback
    p = Path(buf.value)
    return p if p.is_dir() else fallback


def desktop_dir() -> Path:
    CSIDL_DESKTOPDIRECTORY = 0x0010
    return _known_folder(CSIDL_DESKTOPDIRECTORY, Path.home() / "Desktop")


def startup_dir() -> Path:
    """当前用户的开机启动文件夹。"""
    CSIDL_STARTUP = 0x0007
    fallback = (Path(os.environ.get("APPDATA", Path.home()))
                / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
    return _known_folder(CSIDL_STARTUP, fallback)


def remove_shortcut(lnk_path) -> bool:
    p = Path(lnk_path)
    try:
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        pass
    return False


if __name__ == "__main__":
    target = DEFAULT_TARGET
    out = None
    if "--target" in sys.argv:
        try:
            target = Path(sys.argv[sys.argv.index("--target") + 1])
        except IndexError:
            pass
    if "--out" in sys.argv:
        try:
            out = Path(sys.argv[sys.argv.index("--out") + 1])
        except IndexError:
            pass

    if not Path(target).is_file():
        print(f"[!] 找不到目标：{target}")
        raise SystemExit(1)

    lnk = out or (desktop_dir() / f"{DEFAULT_DESC}.lnk")
    try:
        create_shortcut(lnk, target)
        print(f"✓ 已创建快捷方式：{lnk}")
        print(f"  指向：{target}")
    except Exception as e:  # noqa: BLE001
        print(f"[!] 创建失败：{e}")
        raise SystemExit(1)
