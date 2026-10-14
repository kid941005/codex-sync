# -*- coding: utf-8 -*-
"""守护进程专项自测。

在临时目录里造一套「假 Codex 数据 + 假同步盘」，用子进程真实调用
cx_sync.py daemon，验证：

    1. 单轮运行：会 push、会写日志、包落在 from-<标识>.zip
    2. 单实例锁：第二个守护启动时被挡掉（不会两个进程同时 push）
    3. 冷窗口 pull：远端有包且 Codex「未运行」时会拉下来
    4. pull 推迟：Codex「正在运行」时不会动远端包，且能正确报告待拉取数量
    5. --status / --stop 两个控制接口可用

用 DD 变量控制「Codex 是否在运行」：把 codex_process_hints 设成一个
必然存在的进程名（如 python）来模拟「Codex 运行中」。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
ENTRY = ROOT / "cx_sync.py"

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}")


def run(args, env, timeout=180, expect=None):
    p = subprocess.run([PY, str(ENTRY)] + args, cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if expect is not None and p.returncode != expect:
        print(f"    (rc={p.returncode}, expected {expect})\n{out}")
    return p.returncode, out


def build_machine(root: Path, tag: str, sessions):
    """造一个 codex home。sessions = [(sid, title, updated_ms, cwd)]"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text(f'# {tag}\nmodel = "gpt-5"\n', encoding="utf-8")
    idx = []
    for sid, title, upd, cwd in sessions:
        d = root / "sessions" / "2026" / "10" / "14"
        d.mkdir(parents=True, exist_ok=True)
        rel = f"sessions/2026/10/14/rollout-2026-10-14T00-00-00-{sid}.jsonl"
        meta = {"id": sid, "timestamp": "2026-10-14T00:00:00Z",
                "cwd": cwd, "originator": "codex_cli"}
        lines = [json.dumps({"type": "session_meta", "payload": meta},
                            ensure_ascii=False)]
        for i in range(3):
            lines.append(json.dumps(
                {"type": "message", "text": f"{tag}-{sid}-line{i}"},
                ensure_ascii=False))
        (root / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
        idx.append({"id": sid, "thread_name": title,
                    "updated_at": upd, "rollout_path": rel})
    (root / "session_index.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in idx) + "\n",
        encoding="utf-8")

    db = root / "state_5.sqlite"
    c = sqlite3.connect(str(db))
    c.execute("DROP TABLE IF EXISTS threads")
    c.execute("DROP TABLE IF EXISTS projects")
    c.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, "
              "rollout_path TEXT, updated_at_ms INTEGER)")
    c.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, "
              "updated_at_ms INTEGER)")
    for sid, title, upd, cwd in sessions:
        c.execute("INSERT INTO threads VALUES (?,?,?,?)",
                  (sid, title, f"sessions/{sid}.jsonl", upd))
    c.commit()
    c.close()


def write_cfg(cfg_dir: Path, sync_root: Path, hints=None, name="testbox"):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps({
        "sync_root": str(sync_root),
        "machine_name": name,
        "codex_process_hints": hints or ["codex", "chatgpt"],
        "daemon": {"push_interval": 600, "pull_interval": 300,
                   "start_delay": 0, "pull_on_start": True,
                   "autopull": True, "max_log_kb": 256},
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="cx-daemon-test-"))
    cfg_a = tmp / "cfg-a"          # 每台机一个配置目录 → 各自独立的 machine-id
    cfg_b = tmp / "cfg-b"
    sync = tmp / "sync"
    codex_a = tmp / "codexA"
    codex_b = tmp / "codexB"
    codex_a.mkdir(parents=True, exist_ok=True)
    codex_b.mkdir(parents=True, exist_ok=True)
    sync.mkdir(parents=True, exist_ok=True)
    write_cfg(cfg_a, sync, name="A 机")
    write_cfg(cfg_b, sync, name="B 机")

    env_a = dict(os.environ)
    env_a["CODEX_SYNC_CONFIG"] = str(cfg_a)
    env_b = dict(os.environ)
    env_b["CODEX_SYNC_CONFIG"] = str(cfg_b)

    print("\n[1] 单轮运行：push + 日志")
    build_machine(codex_a, "A", [("sid-a-1", "A 的会话", 1000, "D:/proj/a")])
    rc, out = run(["daemon", "--once", "--sync-root", str(sync),
                   "--codex-home", str(codex_a)], env_a)
    check(rc == 0, f"daemon --once 退出码 0（实际 {rc}）")
    pkgs = list((sync / "codex" / "state").glob("from-*.zip"))
    if not pkgs:
        print(f"    (无包，daemon 输出：)\n{out}")
    check(len(pkgs) == 1, f"生成了 1 个本机包（实际 {len(pkgs)}）")
    logf = cfg_a / "daemon.log"
    check(logf.is_file() and "守护进程启动" in logf.read_text(encoding="utf-8"),
          "守护进程写了启动日志")
    check("push 完成" in logf.read_text(encoding="utf-8"), "日志里有 push 完成记录")

    print("\n[2] 单实例锁：第二个守护被挡掉")
    proc = subprocess.Popen(
        [PY, str(ENTRY), "daemon", "--sync-root", str(sync),
         "--codex-home", str(codex_a), "--duration", "12",
         "--interval", "600", "--pull-interval", "600"],
        cwd=str(ROOT), env=env_a,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(4)
    rc2, out2 = run(["daemon", "--once", "--sync-root", str(sync),
                     "--codex-home", str(codex_a)], env_a)
    check("单实例锁" in out2, "第二个守护识别到已有实例并退出")
    rc3, out3 = run(["daemon", "--status"], env_a)
    check("运行中" in out3 and "是" in out3, "--status 报告守护正在运行")
    rc4, out4 = run(["daemon", "--stop"], env_a)
    check("已请求停止" in out4, "--stop 请求停止成功")
    try:
        proc.wait(timeout=30)
        stopped = True
    except subprocess.TimeoutExpired:
        proc.kill()
        stopped = False
    check(stopped, "长驻守护收到停止标志后自行退出")

    print("\n[3] 冷窗口 pull：Codex 未运行时把 B 的包拉下来")
    build_machine(codex_b, "B", [("sid-b-1", "B 的会话", 2000, "D:/proj/b")])
    rc, _ = run(["push", "--sync-root", str(sync), "--codex-home", str(codex_b)],
                env_b)
    check(rc == 0, "B 机 push 成功")
    pkgs = sorted(p.name for p in (sync / "codex" / "state").glob("from-*.zip"))
    check(len(pkgs) == 2, f"同步盘里有 2 个机器的包（实际 {pkgs}）")

    rc, out = run(["daemon", "--once", "--sync-root", str(sync),
                   "--codex-home", str(codex_a)], env_a)
    logtxt = (cfg_a / "daemon.log").read_text(encoding="utf-8")
    check("冷窗口可用" in logtxt, "日志记录了冷窗口 pull")
    a_sessions = list((codex_a / "sessions").rglob("*.jsonl"))
    got_b = any("sid-b-1" in p.name for p in a_sessions)
    check(got_b, "A 机拉到了 B 的会话文件")
    c = sqlite3.connect(str(codex_a / "state_5.sqlite"))
    ids = [r[0] for r in c.execute("SELECT id FROM threads")]
    c.close()
    check("sid-b-1" in ids, "A 机 state 库合并进 B 的 threads 行")
    check("sid-a-1" in ids, "A 机原有 threads 行未被破坏")

    print("\n[4] pull 推迟：Codex「正在运行」时不动远端包")
    build_machine(codex_b, "B2", [("sid-b-2", "B 的新会话", 3000, "D:/proj/b2")])
    run(["push", "--sync-root", str(sync), "--codex-home", str(codex_b)], env_b)
    # 用同一个 A 标识 + hints=["python"] 模拟「Codex 正在运行」（当前跑的就是 python）
    cfg_run = tmp / "cfg-a-running"
    write_cfg(cfg_run, sync, hints=["python"], name="A 机")
    mid = cfg_a / "machine-id"
    if mid.is_file():
        shutil.copy2(mid, cfg_run / "machine-id")
    else:
        # 上一步没生成标识就不用复制：这里只关心「Codex 运行中」这条分支
        print("    (注意：cfg-a/machine-id 不存在，本轮用全新标识)")
    env_run = dict(os.environ)
    env_run["CODEX_SYNC_CONFIG"] = str(cfg_run)
    rc, out = run(["daemon", "--once", "--sync-root", str(sync),
                   "--codex-home", str(codex_a)], env_run)
    logtxt2 = (cfg_run / "daemon.log").read_text(encoding="utf-8")
    check("pull 推迟" in logtxt2, "日志说明 pull 因 Codex 运行而推迟")
    a_sessions = [p.name for p in (codex_a / "sessions").rglob("*.jsonl")]
    check(not any("sid-b-2" in n for n in a_sessions),
          "Codex 运行期间没有写入远端会话（严格冷同步）")
    check("push 完成" in logtxt2, "同轮 push 仍然照常执行（只读快照）")
    n_pending = logtxt2.count("远端待拉取")
    check(n_pending >= 1, "日志报告了待拉取包数量")

    print("\n[5] 空闲态的 --status")
    rc, out = run(["daemon", "--status"], env_a)
    check("运行中" in out, "--status 有输出")
    check("否" in out, "守护未运行时报告「否」")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n结果：{passed} 项通过 / {failed} 项失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
