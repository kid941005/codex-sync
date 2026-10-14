# -*- coding: utf-8 -*-
"""常驻守护进程 —— 让同步在后台自己发生（开机自启、静默运行）。

────────────────────────── 为什么是常驻进程，而不是计划任务 ──────────────────────────

workbuddy-sync 用的是「登录时 pull 一次 + 计划任务定时 push」。Codex 这边多了
一条硬约束：**pull 必须在 Codex 未运行时执行**（要重写 session_index.jsonl 与
state_*.sqlite，并发写有损坏风险）。定时任务无法知道「此刻 Codex 关了没有」，
常驻进程可以 —— 它每轮都探一次进程，Codex 一关就立刻抢那个冷窗口把远端拉下来。

────────────────────────── 三条节奏 ──────────────────────────

    1. 登录后立刻（默认延迟 20s 等云盘挂载）抢一次冷窗口 pull
       —— Codex 不是开机自启项，登录那一刻它一定是关着的，这是最可靠的时机
    2. 每 pull_interval（默认 5 分钟）探一次：Codex 未运行 且 远端有未拉取的包 → pull
    3. 每 push_interval（默认 15 分钟）push 一次：只读快照，Codex 开着也照跑

────────────────────────── 安全边界 ──────────────────────────

    · push 是只读快照，任何时候执行都安全
    · pull 只在 codex_running() == False 时执行；取不到进程列表时代码会保守拒绝
    · 单实例锁（Windows 命名互斥体）：重复启动被静默挡掉，不会两个守护同时 push
    · 停止标志：`cx-sync daemon --stop` 落一个标记文件，守护在 5 秒内自行退出
    · 整轮逻辑包在 try/except 里：单轮失败只记日志，进程不会因此死掉

★ 本机环境注意（实测结论）
    该机器上 **GUI 子系统进程（pythonw.exe）无法被连接**，且自带的后台静默启动
    写法屡屡失效。因此静默启动一律走 **python.exe + 隐藏窗口**（见 cx-daemon.vbs），
    守护进程本身不做任何监听，纯出站，两种入口都安全。
"""
from __future__ import annotations

import ctypes
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path

from . import config, core, platform_win, state
from .utils import human

#: 守护进程默认节奏，可被 config.json 的 "daemon" 段逐项覆盖
DAEMON_DEFAULTS = {
    "push_interval": 900,      # 15 分钟
    "pull_interval": 300,      # 5 分钟
    "start_delay": 20,         # 登录后等云盘客户端挂载的宽限
    "pull_on_start": True,     # 启动后先抢冷窗口 pull
    "autopull": True,          # 关闭后守护只做 push
    "max_log_kb": 1024,        # 日志超过这个大小就轮转
}

#: 睡眠切片：保证 --stop 能在几秒内生效，而不是等满一整个 interval
LOOP_TICK = 5.0

MUTEX_PREFIX = "codex-sync-daemon"
ERROR_ALREADY_EXISTS = 183


def mutex_name() -> str:
    """单实例锁的名字 —— 按配置目录派生。

    刻意**不**用一个全局常量名：同一台机器上可能有完全独立的部署（不同
    `CODEX_SYNC_CONFIG`、不同同步目录，例如测试沙盒与正式环境）。用全局名会把
    它们互相挡住；按配置目录派生则「同一套配置只允许一个守护」，语义更准确。
    """
    import hashlib
    key = str(config.CONFIG_DIR).lower().encode("utf-8")
    return f"{MUTEX_PREFIX}-{hashlib.sha1(key).hexdigest()[:16]}"


# ─────────────────────────── 路径 ───────────────────────────

def log_path() -> Path:
    return config.CONFIG_DIR / "daemon.log"


def pid_path() -> Path:
    return config.CONFIG_DIR / "daemon.pid"


def stop_flag_path() -> Path:
    return config.CONFIG_DIR / "daemon.stop"


def daemon_settings(cfg: dict) -> dict:
    d = dict(DAEMON_DEFAULTS)
    user = cfg.get("daemon")
    if isinstance(user, dict):
        for k, v in user.items():
            if k in d and v is not None:
                d[k] = v
    return d


# ─────────────────────────── 日志 ───────────────────────────

def _rotate_if_needed(max_kb: int) -> None:
    p = log_path()
    try:
        if p.is_file() and p.stat().st_size > max(64, int(max_kb)) * 1024:
            bak = p.with_name(p.name + ".1")
            if bak.exists():
                bak.unlink()
            p.replace(bak)
    except OSError:
        pass


def log(msg: str, level: str = "INFO", quiet: bool = False, max_kb: int = 1024) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level:<4} {msg}"
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(max_kb)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if not quiet:
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass


def tail_log(n: int = 12) -> list:
    p = log_path()
    if not p.is_file():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-n:]


# ─────────────────────────── 单实例锁 ───────────────────────────

def acquire_singleton(name: str = ""):
    """尝试拿到单实例锁。

    返回 (handle, status)：
        status == "ok"          → 拿到，handle 需在退出时 CloseHandle
        status == "exists"      → 已有实例在跑
        status == "unsupported" → 非 Windows，不做互斥（调用方自行决定）
    """
    if not platform_win.IS_WINDOWS:
        return None, "unsupported"
    name = name or mutex_name()
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        h = k32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not h:
            return None, "unsupported"
        if err == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(h)
            return None, "exists"
        return h, "ok"
    except Exception:  # noqa: BLE001
        return None, "unsupported"


def release_singleton(handle) -> None:
    if handle is None or not platform_win.IS_WINDOWS:
        return
    try:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass


def is_running() -> bool:
    """另一个守护进程是否正在运行（用完立刻释放，不占锁）。"""
    h, status = acquire_singleton()
    if status == "exists":
        return True
    if status == "ok":
        release_singleton(h)
    return False


# ─────────────────────────── 单轮动作 ───────────────────────────

def _resolve_root(cfg: dict, override):
    """取同步目录 + 机器标识；未就绪时返回 None。"""
    sync_root, _src = config.resolve_sync_root(cfg, override)
    if sync_root is None:
        return None
    config.resolve_machine_id(cfg, sync_root)
    return sync_root


def pending_remote(sync_root: Path) -> list:
    """远端有、但本机还没拉过的包（按上次 pull 的指纹判断）。"""
    st = sync_root / "codex" / "state"
    if not st.is_dir():
        return []
    marker = core.load_pull_marker(sync_root)
    out = []
    for zp in sorted(st.glob("from-*.zip")):
        if zp.stem == f"from-{state.HOST}":
            continue
        try:
            if marker.get(zp.name) != core.pkg_sig(zp):
                out.append(zp.name)
        except OSError:
            continue
    return out


def do_push_once(sync_root: Path, quiet: bool = False, max_kb: int = 1024) -> dict:
    """只读快照打包。失败只记日志，不抛。"""
    try:
        buf = StringIO()
        with redirect_stdout(buf):
            info = core.build_package(sync_root, False, None)
        log(f"push 完成：{info['files']} 个文件 · {human(info['bytes'])}"
            f" → {Path(info['zip']).name}", "OK", quiet, max_kb)
        return info
    except Exception as e:  # noqa: BLE001
        log(f"push 失败：{type(e).__name__}: {e}", "ERR", quiet, max_kb)
        return {}


def do_pull_once(sync_root: Path, quiet: bool = False, max_kb: int = 1024) -> int:
    """冷同步 pull。只在调用方已确认 Codex 未运行时才会走到这里。"""
    try:
        buf = StringIO()
        with redirect_stdout(buf):
            rc = core.do_pull(sync_root, False, False)
        for ln in buf.getvalue().splitlines():
            if ln.strip():
                log("  │ " + ln.rstrip(), "INFO", quiet, max_kb)
        log(f"pull 结束 rc={rc}", "OK" if rc == 0 else "ERR", quiet, max_kb)
        return rc
    except Exception as e:  # noqa: BLE001
        log(f"pull 失败：{type(e).__name__}: {e}", "ERR", quiet, max_kb)
        return 1


def try_pull(sync_root: Path, quiet: bool = False, max_kb: int = 1024,
             reason: str = "") -> bool:
    """抢冷窗口 pull。返回 True 表示真的执行了。

    · Codex 正在运行 且 确实有包待拉 → 记一条 WAIT（无待拉包时完全不吭声，避免刷屏）
    · Codex 未运行 但 无待拉包 → 静默返回（这是绝大多数轮次的情况）
    """
    pending = pending_remote(sync_root)
    if platform_win.codex_running():
        if pending:
            log(f"pull 推迟：Codex 正在运行，远端待拉取 {len(pending)} 个包"
                f"（{', '.join(pending[:3])}{' …' if len(pending) > 3 else ''}）"
                f"—— 退出 Codex 后会自动补上", "WAIT", quiet, max_kb)
        return False
    if not pending:
        return False
    head = f"（{reason}）" if reason else ""
    log(f"冷窗口可用{head}：检测到 {len(pending)} 个待拉取包，开始 pull", "OK",
        quiet, max_kb)
    do_pull_once(sync_root, quiet, max_kb)
    return True


# ─────────────────────────── 主循环 ───────────────────────────

def _interruptible_sleep(seconds: float, stop_check) -> None:
    end = time.monotonic() + max(0.0, seconds)
    while True:
        left = end - time.monotonic()
        if left <= 0 or stop_check():
            return
        time.sleep(min(LOOP_TICK, left))


def run(once: bool = False,
        push_interval=None,
        pull_interval=None,
        no_pull: bool = False,
        duration: float = 0.0,
        sync_root_override=None,
        codex_home=None,
        manifest_dir=None,
        quiet: bool = False) -> int:
    """守护进程主体。once=True 只跑一轮（供自测 / 计划任务使用）。"""
    if codex_home:
        state.CODEX_HOME = Path(codex_home)

    man, _sources = config.load_manifest(
        Path(manifest_dir) if manifest_dir else None)
    config.apply_manifest(man)
    cfg = config.load_config()
    platform_win.set_hints(cfg.get("codex_process_hints"),
                           cfg.get("codex_process_ignore"))

    st = daemon_settings(cfg)
    if push_interval:
        st["push_interval"] = max(30, int(push_interval))
    if pull_interval:
        st["pull_interval"] = max(15, int(pull_interval))
    if no_pull:
        st["autopull"] = False
    max_kb = int(st["max_log_kb"])

    # ---- 单实例锁 ----
    handle, status = acquire_singleton()
    if status == "exists":
        log("已有守护进程在运行，本次启动退出（单实例锁）", "SKIP", quiet, max_kb)
        return 0
    if status == "unsupported" and not quiet:
        print("[i] 未启用单实例锁（非 Windows 环境）")

    # ---- 停止标志清理 ----
    flag = stop_flag_path()
    if flag.exists():
        try:
            flag.unlink()
        except OSError:
            pass

    try:
        pid_path().parent.mkdir(parents=True, exist_ok=True)
        pid_path().write_text(str(_pid()), encoding="utf-8")
    except OSError:
        pass

    sync_root = _resolve_root(cfg, sync_root_override)
    if sync_root is None:
        log(f"未能确定同步目录，守护进程退出。请先创建 "
            f"{config.CONFIG_DIR / 'config.json'}", "ERR", quiet, max_kb)
        release_singleton(handle)
        return 1

    log(f"守护进程启动：本机 {state.MACHINE_NAME}（{state.HOST}）"
        f" · Codex {state.CODEX_HOME}", "BOOT", quiet, max_kb)
    log(f"节奏：push 每 {st['push_interval']}s · pull 每 {st['pull_interval']}s"
        f" · 自动拉取 {'开' if st['autopull'] else '关'}"
        + ("" if once else f" · 启动延迟 {st['start_delay']}s"),
        "BOOT", quiet, max_kb)

    t0 = time.monotonic()
    deadline_push = time.monotonic()          # 首轮立即 push
    deadline_pull = time.monotonic()          # 首轮立即抢冷窗口
    if not once and float(st["start_delay"]) > 0:
        _interruptible_sleep(float(st["start_delay"]), lambda: flag.exists())
    first_pull = bool(st["pull_on_start"])
    rc = 0

    while True:
        now = time.monotonic()

        if st["autopull"] and now >= deadline_pull:
            deadline_pull = now + float(st["pull_interval"])
            try:
                try_pull(sync_root, quiet, max_kb,
                         reason="登录冷窗口" if first_pull else "")
            except Exception as e:  # noqa: BLE001
                log(f"pull 轮次异常：{type(e).__name__}: {e}", "ERR", quiet, max_kb)
            first_pull = False

        now = time.monotonic()
        if now >= deadline_push:
            deadline_push = now + float(st["push_interval"])
            do_push_once(sync_root, quiet, max_kb)

        if once:
            break
        if duration and (time.monotonic() - t0) >= duration:
            log("已达 --duration 时限，守护进程退出", "BYE", quiet, max_kb)
            break
        if flag.exists():
            try:
                flag.unlink()
            except OSError:
                pass
            log("收到停止标志，守护进程退出", "BYE", quiet, max_kb)
            break
        _interruptible_sleep(LOOP_TICK, lambda: flag.exists())

    try:
        pid_path().unlink()
    except OSError:
        pass
    release_singleton(handle)
    return rc


def _pid() -> int:
    import os
    return os.getpid()


# ─────────────────────────── 控制接口（给 CLI 用）───────────────────────────

def request_stop(quiet: bool = False) -> int:
    """落停止标志。守护进程在 5 秒内自行退出。"""
    running = is_running()
    try:
        stop_flag_path().parent.mkdir(parents=True, exist_ok=True)
        stop_flag_path().write_text(
            datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except OSError as e:
        print(f"[!] 无法写入停止标志：{e}")
        return 1
    if running:
        print("已请求停止，守护进程将在几秒内退出。")
    else:
        print("守护进程当前没有运行（停止标志已清除）。")
        try:
            stop_flag_path().unlink()
        except OSError:
            pass
    return 0


def show_status() -> int:
    running = is_running()
    print("── 守护进程状态 ──")
    print(f"  运行中         {'是' if running else '否'}")
    if pid_path().is_file():
        try:
            print(f"  记录 PID       {pid_path().read_text(encoding='utf-8').strip()}")
        except OSError:
            pass
    print(f"  单实例锁       {mutex_name()}")
    print(f"  日志文件       {log_path()}")
    print(f"  停止标志       {stop_flag_path()}")
    lines = tail_log(10)
    print("  最近日志：")
    if lines:
        for ln in lines:
            print(f"    {ln}")
    else:
        print("    （还没有日志）")
    return 0


def main(argv=None) -> int:
    """控制台入口（`cx-daemon`）—— 等价于 `cx-sync daemon`。

    只做最朴素的参数解析，正式用法请走 cli。
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    def opt(flag, cast=str, default=None):
        if flag in argv:
            try:
                return cast(argv[argv.index(flag) + 1])
            except (ValueError, IndexError):
                return default
        return default

    if "--status" in argv:
        return show_status()
    if "--stop" in argv:
        return request_stop()
    return run(
        once="--once" in argv,
        push_interval=opt("--interval", int),
        pull_interval=opt("--pull-interval", int),
        no_pull="--no-pull" in argv,
        duration=opt("--duration", float, 0.0) or 0.0,
        sync_root_override=opt("--sync-root"),
        codex_home=opt("--codex-home"),
        manifest_dir=opt("--manifest-dir"),
        quiet="--quiet" in argv,
    )


if __name__ == "__main__":          # pragma: no cover
    sys.exit(main())
