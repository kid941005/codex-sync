# -*- coding: utf-8 -*-
"""安装开机自启：把「静默启动守护进程」的快捷方式放进启动文件夹。

    python create-autostart.py             # 安装自启 + 立即启动守护
    python create-autostart.py --no-start  # 只安装自启，不立刻启动
    python create-autostart.py --remove    # 卸载自启并停掉守护

为什么用「启动文件夹 + 快捷方式」而不是计划任务：
    · 不需要管理员权限（计划任务注册在部分环境会失败）
    · 登录时机正好 —— Codex 不是开机自启项，此刻它一定是关着的，
      守护进程可以立刻抢到最可靠的一次冷窗口 pull
    · 想撤销时删掉那个 .lnk 即可，不留系统级痕迹
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# create-shortcut.py 文件名含连字符，无法用 import 语句，按路径加载
_spec = importlib.util.spec_from_file_location(
    "create_shortcut", HERE / "create-shortcut.py")
_cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cs)

VBS = HERE / "cx-daemon.vbs"
LNK_NAME = "Codex 同步守护.lnk"
CONFIG_DIR = Path(
    os.environ.get("CODEX_SYNC_CONFIG")
    or (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "codex-sync")
)


def find_python() -> str:
    """优先用 WorkBuddy 自带 Python（与 VBS 里的查找逻辑保持一致）。"""
    ver_dir = Path(os.environ.get("USERPROFILE", Path.home())) \
        / ".workbuddy" / "binaries" / "python" / "versions"
    if ver_dir.is_dir():
        for d in sorted(ver_dir.iterdir(), reverse=True):
            exe = d / "python.exe"
            if exe.is_file():
                return str(exe)
    return sys.executable or "python"


def _lnk_path() -> Path:
    return _cs.startup_dir() / LNK_NAME


def stop_daemon() -> None:
    """请求正在运行的守护退出（落停止标志，守护自己收尾）。"""
    py = find_python()
    try:
        subprocess.run([py, str(HERE / "cx_sync.py"), "daemon", "--stop"],
                       cwd=str(HERE), timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def _describe_sync_root() -> None:
    """报告守护进程会往哪写 —— 显式配置优先，否则展示自动探测结果。"""
    cfg = CONFIG_DIR / "config.json"
    if cfg.is_file():
        print(f" 配置文件     {cfg}")
        return
    # 没有 config.json 时，引擎会走环境变量 / 自动探测；这里复刻同一逻辑
    try:
        sys.path.insert(0, str(HERE / "src"))
        from codex_sync import config as cxconfig   # noqa: PLC0415
        root, src = cxconfig.resolve_sync_root({}, None)
    except Exception:  # noqa: BLE001
        root, src = None, ""
    if root is not None:
        print(f" 配置文件     (无) —— 将自动使用 {root}")
        print(f"              来源：{src}")
        print("              建议把它固定下来：把 config.example.json 复制成")
        print(f"              {cfg} 并填写 sync_root")
    else:
        print(f" [!] 还没有配置文件：{cfg}")
        print("     守护进程会因找不到同步目录而退出。先把 config.example.json")
        print("     复制到上面的路径并填好 sync_root（任意双向同步盘 / U 盘 / 局域网共享）。")


def install(start: bool = True) -> int:
    print()
    print("=" * 52)
    print(" codex-sync  开机自启 - 安装")
    print("=" * 52)

    if not VBS.is_file():
        print(f"[ERROR] 缺少 {VBS}")
        print("        请确认本目录已从云端完整同步下来。")
        return 1

    _describe_sync_root()

    startup = _cs.startup_dir()
    startup.mkdir(parents=True, exist_ok=True)
    lnk = startup / LNK_NAME
    try:
        _cs.create_shortcut(lnk, VBS, workdir=HERE,
                            description="Codex 资产后台自动同步（静默）")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 创建启动快捷方式失败：{e}")
        return 1
    print(f" 自启快捷方式 {lnk}")
    print(f" 指向         {VBS}")
    print(f" 登录后的效果 后台无窗口启动守护：定时 push + Codex 关闭时自动 pull")

    if start:
        print()
        print(" 立即启动守护 ...")
        try:
            os.startfile(str(VBS))          # 走 .vbs 关联 → wscript.exe
            print(" 已拉起（静默，无窗口）")
        except OSError as e:
            print(f" [!] 拉起失败：{e}（下次登录仍会自动启动）")

    print()
    print(f" 日志   {CONFIG_DIR / 'daemon.log'}")
    print(" 查看   python cx_sync.py daemon --status")
    print(" 停止   python cx_sync.py daemon --stop")
    print(" 卸载   python create-autostart.py --remove")
    print("=" * 52)
    print()
    return 0


def uninstall() -> int:
    print()
    print("=" * 52)
    print(" codex-sync  开机自启 - 卸载")
    print("=" * 52)
    lnk = _lnk_path()
    if _cs.remove_shortcut(lnk):
        print(f" 已删除自启快捷方式 {lnk}")
    else:
        print(f" 自启快捷方式不存在（{lnk}）")

    stop_daemon()
    print(" 已请求守护进程退出（若在运行，几秒内自行结束）")
    print()
    print(" 桌面快捷方式/面板不受影响；此后不再自动同步。")
    print("=" * 52)
    print()
    return 0


if __name__ == "__main__":
    if "--remove" in sys.argv or "--uninstall" in sys.argv:
        raise SystemExit(uninstall())
    raise SystemExit(install(start="--no-start" not in sys.argv))
