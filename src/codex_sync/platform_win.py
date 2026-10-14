# -*- coding: utf-8 -*-
"""Windows 平台相关能力：Codex 进程检测（冷同步的前提）。

设计原则：**判断不了就当作「Codex 正在运行」**。
    写 state_*.sqlite 是有风险的操作（并发写会损坏库），所以检测失败时
    宁可拒绝也不放行 —— 用户确认已退出时可以显式加 --force。

Codex 的进程名知识来自 codex-session-sync（codex.exe / codex-windows-sandbox）；
考虑到 Codex Desktop 由 ChatGPT 桌面客户端承载，这里额外纳入 chatgpt。
"""
from __future__ import annotations

import subprocess
import sys

#: 是否运行在 Windows 上（其他平台进程检测不可用，会退化为「保守拒绝」）
IS_WINDOWS = sys.platform == "win32"

#: 进程名（小写）里出现这些关键字即认为 Codex 在运行。
#: 可用配置 config.json 的 "codex_process_hints" 覆写。
#: 注意：Codex Desktop 由 ChatGPT 桌面客户端承载，故纳入 chatgpt。
DEFAULT_PROCESS_HINTS = ("codex", "chatgpt")

#: 名称命中即排除 —— 这些是**常驻后台服务**，不代表「应用正在使用数据」。
#: 例如 codex-windows-sandbox-service.exe 随系统常驻，若计入会导致 pull 永远被拒。
#: 可用配置 config.json 的 "codex_process_ignore" 覆写。
DEFAULT_PROCESS_IGNORE = ("service",)

_hints = list(DEFAULT_PROCESS_HINTS)
_ignore = list(DEFAULT_PROCESS_IGNORE)


def set_hints(hints=None, ignore=None) -> None:
    """由 cli 依据 config.json 覆写检测关键字 / 忽略关键字。"""
    global _hints, _ignore
    if hints:
        _hints = [str(h).strip().lower() for h in hints if str(h).strip()]
    if ignore is not None:
        _ignore = [str(h).strip().lower() for h in ignore if str(h).strip()]


def process_hints() -> tuple:
    return tuple(_hints)


def process_ignore() -> tuple:
    return tuple(_ignore)


def _names_via_tasklist() -> list:
    """tasklist /FO CSV /NH → ['chrome.exe', 'python.exe', ...]（小写）"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    if not out or not out.strip():
        return []
    return [line.split(",")[0].strip('"').lower()
            for line in out.splitlines() if line.strip()]


def _names_via_powershell() -> list:
    """tasklist 被拦时的退路。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Select-Object -ExpandProperty ProcessName"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    return [ln.strip().lower() for ln in out.splitlines() if ln.strip()]


def list_process_names() -> tuple:
    """返回 (进程名列表, 取得方式)。两种方式都失败时列表为空。"""
    names = _names_via_tasklist()
    if names:
        return names, "tasklist"
    names = _names_via_powershell()
    if names:
        return names, "powershell"
    return [], ""


def codex_running() -> bool:
    """Codex 是否在运行（决定能不能写 state_*.sqlite）。

    取不到进程列表时返回 True —— 保守拒绝，详见模块开头说明。
    """
    names, _how = list_process_names()
    if not names:
        return True
    for n in names:
        if any(x in n for x in _ignore):        # 常驻服务不算
            continue
        if any(hint in n for hint in _hints):
            return True
    return False
