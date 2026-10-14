#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：模拟两台机器 A/B 经共享目录往返同步 Codex 资产。

覆盖：
  1. per-machine 包 —— A、B 各自只写自己的 zip
  2. rollout JSONL 行级并集 —— 共享会话两边各自追加，pull 后不丢行
  3. session_index 按 id 合并 —— 重复 id 取 updated_at 较新者
  4. state_*.sqlite 快照 + 合并 —— threads 同 id 取 updated_at_ms 较新者
  5. 白名单排除 —— .sandbox/secret.txt 之类的敏感/运行态资产不进包

以子进程方式调用真实 CLI（env 隔离每台机器的 config 目录），不做任何桩替换。
用法：python tools/selftest.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "cx_sync.py"
PY = sys.executable


# ─────────────────────────── 造数据 ───────────────────────────

def make_rollout(path: Path, sid: str, cwd: str, provider: str, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"timestamp": "2026-10-14T09:30:00.000Z", "type": "session_meta",
                         "payload": {"id": sid, "cwd": cwd, "model_provider": provider}},
                        ensure_ascii=False)]
    lines += [json.dumps(e, ensure_ascii=False) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_state_db(path: Path, threads: list) -> None:
    """threads: [(id, title, updated_at_ms, provider, cwd)]"""
    if path.exists():
        path.unlink()
    c = sqlite3.connect(str(path))
    c.execute("""CREATE TABLE threads (
        id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER,
        updated_at INTEGER, model_provider TEXT, cwd TEXT, title TEXT,
        name TEXT, archived INTEGER, updated_at_ms INTEGER)""")
    c.execute("""CREATE TABLE projects (
        id TEXT PRIMARY KEY, name TEXT, metadata TEXT, position INTEGER,
        created_at_ms INTEGER, updated_at_ms INTEGER)""")
    for tid, title, ms, prov, cwd in threads:
        c.execute("INSERT INTO threads (id, title, model_provider, cwd, archived, updated_at_ms) "
                  "VALUES (?,?,?,?,0,?)", (tid, title, prov, cwd, ms))
    c.commit()
    c.close()


def write_index(path: Path, entries: list) -> None:
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
                    encoding="utf-8")


def build_home(home: Path, sessions, index, threads, extra_files=None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    for rel, sid, cwd, prov, events in sessions:
        make_rollout(home / rel, sid, cwd, prov, events)
    write_index(home / "session_index.jsonl", index)
    make_state_db(home / "state_5.sqlite", threads)
    (home / "config.toml").write_text('model_provider = "custom"\n', encoding="utf-8")
    (home / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# demo skill\n", encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def run(action: str, codex_home: Path, sync_root: Path, cfg_dir: Path, extra=None) -> str:
    env = dict(os.environ)
    env["CODEX_SYNC_CONFIG"] = str(cfg_dir)
    env["CODEX_HOME"] = str(codex_home)
    argv = [PY, str(ENTRY), action, "--codex-home", str(codex_home),
            "--sync-root", str(sync_root)]
    if extra:
        argv += extra
    p = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=str(ROOT))
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0 and action in ("push", "pull", "doctor", "status"):
        print(f"  [cmd {action}] rc={p.returncode}\n{out}")
    return out


# ─────────────────────────── 断言 ───────────────────────────

FAILS = []


def check(cond: bool, msg: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cx-selftest-"))
    try:
        SID_A = "aaaaaaaa-0000-0000-0000-000000000001"   # A 独有
        SID_B = "bbbbbbbb-0000-0000-0000-000000000002"   # B 独有
        SID_S = "cccccccc-0000-0000-0000-000000000003"   # 共享（两边各自追加）
        R_S = "sessions/2026/10/14/rollout-2026-10-14T09-00-00-%s.jsonl" % SID_S
        R_A = "sessions/2026/10/14/rollout-2026-10-14T10-00-00-%s.jsonl" % SID_A
        R_B = "sessions/2026/10/14/rollout-2026-10-14T11-00-00-%s.jsonl" % SID_B

        homeA = tmp / "codexA"
        homeB = tmp / "codexB"
        sync = tmp / "sync"
        cfgA = tmp / "cfgA"
        cfgB = tmp / "cfgB"

        build_home(
            homeA,
            sessions=[
                (R_S, SID_S, "D:/proj/shared", "custom", [{"type": "event", "n": 1}]),
                (R_A, SID_A, "D:/proj/alpha", "custom", [{"type": "event", "n": "a"}]),
            ],
            index=[
                {"id": SID_S, "thread_name": "共享会话", "updated_at": "2026-10-14T09:00:00Z"},
                {"id": SID_A, "thread_name": "A 的会话", "updated_at": "2026-10-14T10:00:00Z"},
            ],
            threads=[
                (SID_S, "共享会话(旧标题)", 1000, "custom", "D:/proj/shared"),
                (SID_A, "A 的会话", 2000, "custom", "D:/proj/alpha"),
            ],
            extra_files={},   # A 端不放敏感文件
        )
        build_home(
            homeB,
            sessions=[
                # 共享会话：B 端多出第 2 个事件（测试行级并集）
                (R_S, SID_S, "D:/proj/shared", "custom",
                 [{"type": "event", "n": 1}, {"type": "event", "n": 2, "from": "B"}]),
                (R_B, SID_B, "D:/proj/beta", "custom", [{"type": "event", "n": "b"}]),
            ],
            index=[
                # 同 id 但 updated_at 更新 → 应胜出
                {"id": SID_S, "thread_name": "共享会话(B 改名)", "updated_at": "2026-10-14T09:30:00Z"},
                {"id": SID_B, "thread_name": "B 的会话", "updated_at": "2026-10-14T11:00:00Z"},
            ],
            threads=[
                (SID_S, "共享会话(B 新标题)", 1500, "custom", "D:/proj/shared"),
                (SID_B, "B 的会话", 3000, "custom", "D:/proj/beta"),
            ],
            extra_files={
                ".sandbox/secret.txt": "should-never-sync",
                "tmp/scratch.log": "noise",
            },
        )

        print("① 机器 A push")
        run("push", homeA, sync, cfgA)
        print("② 机器 B push")
        run("push", homeB, sync, cfgB)

        zips = sorted(p.name for p in (sync / "codex" / "state").glob("from-*.zip"))
        check(len(zips) == 2, f"per-machine 包：生成 2 个独立 zip（实得 {zips}）")

        print("③ 机器 A pull")
        run("pull", homeA, sync, cfgA, extra=["--force"])

        # 1) 新增：B 独有会话落到 A
        check((homeA / R_B).is_file(), "B 独有会话已同步到 A")

        # 2) 行级并集：共享会话含 B 端事件、且 A 端事件未丢
        text = (homeA / R_S).read_text(encoding="utf-8")
        check('"from": "B"' in text, "rollout 行级并集：B 端追加的事件已并入 A")
        check(text.count('"n": 1') >= 1, "rollout 行级并集：A 端原有事件未丢失（无覆盖丢行）")

        # 3) session_index 按 id 合并，取较新
        idx = {}
        for ln in (homeA / "session_index.jsonl").read_text(encoding="utf-8").splitlines():
            if ln.strip():
                e = json.loads(ln)
                idx[e["id"]] = e
        check(set(idx) == {SID_A, SID_B, SID_S}, "session_index：三处会话条目齐全")
        check(idx.get(SID_S, {}).get("thread_name") == "共享会话(B 改名)",
              "session_index：同 id 取 updated_at 较新者（B 改名胜出）")

        # 4) state_*.sqlite 合并：B 独有 thread + 共享 thread 取 updated_at_ms 较新者
        c = sqlite3.connect(str(homeA / "state_5.sqlite"))
        rows = dict(c.execute("SELECT id, title FROM threads").fetchall())
        ids = set(rows)
        c.close()
        check(SID_B in ids, "state 库：B 独有 thread 行已合并进 A")
        check(rows.get(SID_S) == "共享会话(B 新标题)",
              "state 库：同 id 取 updated_at_ms 较新者（B 新标题胜出）")
        check(rows.get(SID_A) == "A 的会话", "state 库：A 原有 thread 未被覆盖")

        # 5) 白名单排除：B 端才有的敏感文件，不得随 B 的包进入 A
        check(not (homeA / ".sandbox" / "secret.txt").exists(),
              "白名单：B 的 .sandbox/secret.txt 未被同步进 A")
        check(not (homeA / "tmp" / "scratch.log").exists(),
              "白名单：B 的 tmp/scratch.log 未被同步进 A")
        import zipfile
        all_names = []
        for zp in sorted((sync / "codex" / "state").glob("from-*.zip")):
            with zipfile.ZipFile(zp) as z:
                all_names += z.namelist()
        check(not any("secret.txt" in n for n in all_names), "包内不含敏感/运行态文件")
        check(any(n.endswith("config.toml") for n in all_names), "包内含白名单文件 config.toml")
        check(any(n.startswith("db/state/") for n in all_names), "包内含数据库快照 db/state/*.sqlite")

        print()
        if FAILS:
            print(f"结果：{len(FAILS)} 项失败")
            for f in FAILS:
                print("   -", f)
            return 1
        print("结果：全部通过 ✓")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
