# -*- coding: utf-8 -*-
"""Codex 数据层 —— 目录结构、JSONL 会话、SQLite 元数据。

★ 这一层是本项目区别于 workbuddy-sync 的「领域知识」，主要借鉴
  codex-session-sync 的实测结论，结合本机 ~/.codex 的真实结构落地：

  存储三处（重命名 / 删除 / 合并需协同，本工具只做同步与合并）
    1. sessions/YYYY/MM/DD/rollout-<ISO ts>-<uuid>.jsonl   会话内容本体
    2. session_index.jsonl                                  codex resume 索引
    3. state_*.sqlite → threads 表                          会话列表权威来源

  JSONL 解析要点
    · rollout 首行是 session_meta，**可能数百 KB**（含 base_instructions），
      因此只读取前缀，不做整行 JSON.parse —— 失败时退化为正则提取。
    · session_index.jsonl 每行一个 JSON 对象，以 id 为键；两边各自追加，
      合并必须**按 id 去重并取 updated_at 较新者**，纯行并集会产生重复行。
    · rollout 是追加型事件流，行级并集即可，无需解析语义。

  冷同步：Codex 运行时会持有 state_*.sqlite 的写锁，pull 前必须关闭
  （见 platform_win.codex_running）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

#: 会话文件相对路径：sessions/YYYY/MM/DD/rollout-*.jsonl
SESSION_REL_RE = re.compile(r"^sessions/(\d{4})/(\d{2})/(\d{2})/(rollout-.+\.jsonl)$")

#: 从 rollout 文件名里抽 UUID：rollout-2026-10-14T09-30-00-<uuid>.jsonl
_ROLLOUT_UUID_RE = re.compile(r"rollout-.+?-([0-9a-fA-F-]{36})\.jsonl$")

#: 首行前缀读取上限（字节）：session_meta 常含大段 base_instructions
_FIRST_LINE_LIMIT = 64 * 1024


# ─────────────────────────── 路径与命名 ───────────────────────────

def is_session_file(rel: str) -> bool:
    """rel 是否为 sessions/**/rollout-*.jsonl。"""
    return bool(SESSION_REL_RE.match(rel.replace("\\", "/")))


def session_id_from_rel(rel: str) -> str:
    """从相对路径推导会话 id（优先文件名里的 UUID，退化则用整段文件名）。"""
    name = rel.replace("\\", "/").rsplit("/", 1)[-1]
    m = _ROLLOUT_UUID_RE.match(name)
    return m.group(1) if m else name[:-len(".jsonl")] if name.endswith(".jsonl") else name


def session_project(rel: str) -> str:
    """用日期目录做「项目分组」退路（Y-M-D）。"""
    m = SESSION_REL_RE.match(rel.replace("\\", "/"))
    if not m:
        return "unknown"
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


# ─────────────────────────── JSONL 读取与解析 ───────────────────────────

def read_first_line(path: Path, limit: int = _FIRST_LINE_LIMIT) -> str:
    """只读文件前缀，返回第一行（可能被截断）。返回 "" 表示读不到。"""
    try:
        with path.open("rb") as f:
            raw = f.read(limit)
    except OSError:
        return ""
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    nl = text.find("\n")
    return text if nl == -1 else text[:nl]


def extract_session_meta(first_line: str) -> dict:
    """从 rollout 首行提取关键字段。

    首行是 {"timestamp":..., "type":"session_meta", "payload":{...}}。
    因首行可能被前缀截断而无法完整 parse，这里先尝试 JSON，失败退化为正则。
    """
    meta: dict = {"id": None, "cwd": None, "model_provider": None, "timestamp": None}
    if not first_line:
        return meta
    try:
        obj = json.loads(first_line)
        payload = obj.get("payload") if isinstance(obj, dict) else None
        src = payload if isinstance(payload, dict) else obj
        if isinstance(src, dict):
            meta["id"] = src.get("id") or src.get("session_id") or obj.get("id")
            meta["cwd"] = src.get("cwd") or src.get("workdir") or obj.get("cwd")
            meta["model_provider"] = src.get("model_provider") or obj.get("model_provider")
        meta["timestamp"] = obj.get("timestamp") if isinstance(obj, dict) else None
        if meta["cwd"] or meta["model_provider"]:
            return meta
    except (ValueError, TypeError):
        pass
    # 正则兜底（首行被截断）
    for key in ("cwd", "model_provider", "id"):
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', first_line)
        if m:
            try:
                meta[key] = json.loads(f'"{m.group(1)}"')
            except ValueError:
                meta[key] = m.group(1)
    return meta


def read_index(index_path: Path) -> list:
    """读取 session_index.jsonl，返回条目数组（跳过坏行）。"""
    if not index_path.is_file():
        return []
    try:
        text = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def index_map(index_path: Path) -> dict:
    """{id: entry}。无 id 的条目跳过。"""
    m = {}
    for e in read_index(index_path):
        if isinstance(e, dict) and e.get("id"):
            m[e["id"]] = e
    return m


# ─────────────────────────── session_index 合并 ───────────────────────────

def _updated_key(entry: dict) -> float:
    """把 updated_at 归一为可比较的浮点秒；无法解析返回 0。"""
    v = entry.get("updated_at")
    if isinstance(v, (int, float)):
        # 毫秒 / 秒两种可能：> 1e11 视为毫秒
        return float(v) / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def merge_session_index(remote_text: str, local_text: str) -> str | None:
    """按 id 合并 session_index.jsonl：本机条目保留原顺序，对方独有的条目追加，
    同 id 冲突取 updated_at 较新者。无需写入时返回 None。"""
    local_lines = [ln for ln in local_text.splitlines() if ln.strip()]
    remote_lines = [ln for ln in remote_text.splitlines() if ln.strip()]
    if not local_lines:
        merged = remote_lines
        return ("\n".join(merged) + "\n") if merged else None

    order: list = []
    by_id: dict = {}

    def _load(lines):
        for ln in lines:
            try:
                e = json.loads(ln)
            except ValueError:
                e = None
            if isinstance(e, dict) and e.get("id"):
                key = e["id"]
            else:
                key = "@raw:" + ln          # 坏行按原文做键，保证不丢
                e = {"__raw__": ln}
            if key not in by_id:
                by_id[key] = e
                order.append(key)
            else:
                # 同 id：取 updated_at 更新者
                if _updated_key(e) > _updated_key(by_id[key]):
                    by_id[key] = e

    _load(local_lines)
    before = {k: json.dumps(by_id[k], sort_keys=True, ensure_ascii=False) for k in order}
    _load(remote_lines)

    changed = False
    for k in order:
        cur = json.dumps(by_id[k], sort_keys=True, ensure_ascii=False)
        if k not in before or before[k] != cur:
            changed = True
            break
    if not changed:
        return None

    out = []
    for k in order:
        e = by_id[k]
        out.append(e["__raw__"] if "__raw__" in e else
                   json.dumps(e, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(out) + "\n"


# ─────────────────────────── SQLite 元数据 ───────────────────────────

def find_db(codex_home: Path, glob_pat: str) -> Path | None:
    """在 codex home 顶层按 glob 找库，多个时取版本号最大的那个。"""
    try:
        cands = sorted(codex_home.glob(glob_pat))
    except (OSError, ValueError):
        return None
    files = [p for p in cands if p.is_file()]
    if not files:
        return None

    def ver(p: Path):
        m = re.search(r"_(\d+)\.sqlite$", p.name)
        return int(m.group(1)) if m else -1

    return max(files, key=lambda p: (ver(p), p.name))


def db_snapshot(src: Path, dst: Path) -> None:
    """用 SQLite backup API 取一致性快照 —— 直接 copy 会拷到 WAL 未合并的脏状态。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    for suf in ("-wal", "-shm"):
        leftover = Path(str(dst) + suf)
        if leftover.exists():
            leftover.unlink()


def table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list:
    try:
        cur = conn.execute(f"PRAGMA {schema}.table_info('{table}')")
        return [r[1] for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def table_pk(conn: sqlite3.Connection, schema: str, table: str) -> list:
    """按 pk 次序返回主键列；无主键时返回 []。"""
    try:
        rows = conn.execute(f"PRAGMA {schema}.table_info('{table}')").fetchall()
    except sqlite3.Error:
        return []
    pks = [(r[5], r[1]) for r in rows if r[5]]
    return [name for _ord, name in sorted(pks)]


def db_merge(target: Path, incoming: Path, tables, upsert: dict | None = None) -> dict:
    """把 incoming 库合并进 target（仅限白名单表）。

    - 默认 INSERT OR IGNORE：幂等、可重复、源端加列不炸。
    - upsert 里指定的表（含版本列）改为「同主键取版本列较新者胜出」——
      这样改名 / 归档 / 置顶这类**行内更新**才能跨机生效。
    """
    if not incoming.is_file() or not target.is_file():
        return {}
    upsert = upsert or {}
    stats = {}
    conn = sqlite3.connect(str(target), timeout=30)
    try:
        conn.execute("ATTACH DATABASE ? AS inc", (str(incoming),))
        for t in tables:
            try:
                if not conn.execute(
                    "SELECT name FROM inc.sqlite_master WHERE type='table' AND name=?",
                    (t,),
                ).fetchone():
                    continue
                inc_cols = table_columns(conn, "inc", t)
                main_cols = table_columns(conn, "main", t)
                if not main_cols:
                    continue
                cols = [c for c in main_cols if c in inc_cols]
                if not cols:
                    continue
                cl = ", ".join(f'"{c}"' for c in cols)
                pk = table_pk(conn, "main", t)
                ver = upsert.get(t)

                changes_before = conn.total_changes
                if ver and ver in cols and pk:
                    set_clause = ", ".join(
                        f'"{c}"=excluded."{c}"' for c in cols if c not in pk)
                    conflict = ", ".join(f'"{c}"' for c in pk)
                    sql = (
                        f'INSERT INTO main."{t}" ({cl}) '
                        f'SELECT {cl} FROM inc."{t}" WHERE 1=1 '
                        f'ON CONFLICT({conflict}) DO UPDATE SET {set_clause} '
                        f'WHERE excluded."{ver}" > "{ver}"'
                    )
                else:
                    sql = (f'INSERT OR IGNORE INTO main."{t}" ({cl}) '
                           f'SELECT {cl} FROM inc."{t}"')
                conn.execute(sql)
                changed = conn.total_changes - changes_before
                if changed:
                    stats[t] = changed
            except sqlite3.Error as e:
                stats[t] = f"skip({e})"
        conn.commit()
        conn.execute("DETACH DATABASE inc")
    finally:
        conn.close()
    return stats


def db_row_counts(path: Path, tables) -> dict:
    if not path.is_file():
        return {}
    out = {}
    try:
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        for t in tables:
            try:
                out[t] = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                pass
        c.close()
    except sqlite3.Error:
        pass
    return out


def db_threads(codex_home: Path, state_glob: str = "state_*.sqlite",
               limit: int = 500) -> list:
    """从 state_*.sqlite 的 threads 表读会话列表（UI 用；会话文件缺失时的权威来源）。"""
    db = find_db(codex_home, state_glob)
    if db is None:
        return []
    out = []
    try:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info('threads')")}
            want = [x for x in ("id", "rollout_path", "created_at_ms", "updated_at_ms",
                                "model_provider", "cwd", "title", "name", "archived")
                    if x in cols]
            sel = ", ".join(f'"{x}"' for x in want)
            order = '"updated_at_ms"' if "updated_at_ms" in cols else "rowid"
            for row in c.execute(f'SELECT {sel} FROM threads ORDER BY {order} DESC '
                                 f'LIMIT {int(limit)}'):
                d = dict(zip(want, row))
                out.append({
                    "id": d.get("id"),
                    "title": (d.get("name") or d.get("title") or "").strip(),
                    "provider": d.get("model_provider"),
                    "cwd": d.get("cwd"),
                    "created": d.get("created_at_ms"),
                    "updated": d.get("updated_at_ms"),
                    "archived": d.get("archived"),
                    "rollout_path": d.get("rollout_path"),
                })
        finally:
            c.close()
    except sqlite3.Error:
        pass
    return out


# ─────────────────────────── 会话列表（UI / 选择性同步）───────────────────────────

def list_sessions(codex_home: Path, index_rel: str = "session_index.jsonl") -> list:
    """汇总本机会话：扫描 sessions/**/rollout-*.jsonl，用 session_index 补名称。

    返回 [{id, name, project, cwd, provider, updated, file(相对), size}]，
    按 updated 倒序。会话文件的 cwd / provider 从首行 session_meta 提取。
    """
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.is_dir():
        return []
    idx = index_map(codex_home / index_rel)
    out = []
    try:
        files = list(sessions_dir.rglob("*.jsonl"))
    except OSError:
        return []
    for p in files:
        try:
            rel = p.relative_to(codex_home).as_posix()
        except ValueError:
            continue
        if not is_session_file(rel):
            continue
        sid = session_id_from_rel(rel)
        entry = idx.get(sid, {})
        meta = extract_session_meta(read_first_line(p))
        cwd = meta.get("cwd") or ""
        project = cwd.replace("\\", "/").rstrip("/").split("/")[-1] if cwd else ""
        try:
            st = p.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        updated = entry.get("updated_at")
        if isinstance(updated, str):
            try:
                updated = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
            except ValueError:
                updated = None
        out.append({
            "id": sid,
            "name": (entry.get("thread_name") or meta.get("title") or "").strip()
                    or "(未命名会话)",
            "project": project or session_project(rel),
            "cwd": cwd,
            "provider": meta.get("model_provider") or entry.get("model_provider") or "",
            "updated": updated or mtime,
            "file": rel,
            "size": size,
        })
    out.sort(key=lambda s: s.get("updated") or 0, reverse=True)
    return out
