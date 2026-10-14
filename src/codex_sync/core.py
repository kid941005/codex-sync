# -*- coding: utf-8 -*-
"""同步内核：资产收集 / 打包 / 合并远端 / 状态与自检。

这一层与「机器是谁」无关，是整个方案的护城河（继承 workbuddy-sync）：

· per-machine 包：每台机只写自己的 codex/state/from-<标识>.zip，
  从根上避开同步盘的冲突副本问题
· 追加型日志（rollout *.jsonl）走**行级并集**，不再整文件覆盖丢行
· session_index.jsonl 走**按 id 去重合并**（取 updated_at 较新者）
· state_*.sqlite 用 backup API 取快照，pull 时按表白名单合并
· 真冲突时对方版本胜出，本机版本另存 .conflict-<标识>-<时间戳>

★ 所有可变状态都通过 state 模块读写（见 state.py 的说明）。
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import ENGINE_VERSION, MANIFEST_SCHEMA
from . import codex
from . import config
from . import state
from . import platform_win
from .platform_win import codex_running
from .utils import human, now_iso, sha256, ts


# ─────────────────────────── 资产收集 ───────────────────────────

def _expand(base: Path, pattern: str) -> list:
    """把带通配符的条目展开成实际路径；无通配符时按原样返回。"""
    if "*" in pattern or "?" in pattern:
        try:
            return sorted(base.glob(pattern))
        except (OSError, ValueError):
            return []
    return [base / pattern]


def is_excluded(rel: str) -> bool:
    norm = rel.replace("\\", "/")
    parts = norm.split("/")
    if any(p in state.NEVER_SEGMENTS for p in parts):
        return True
    low = norm.lower()
    if low.endswith(state.NEVER_SUFFIX):
        return True
    if ".bak" in low or ".conflict-" in low or low.endswith("~"):
        return True
    if any(fnmatch.fnmatch(norm, g) for g in state.EXTRA_GLOBS):
        return True
    return any(w in low for w in state.SENSITIVE_WORDS)


def iter_assets(base: Path, dirs=None, files=None):
    """产出 (相对路径, 绝对路径)，按白名单收集。sync_files / sync_dirs 均支持 glob。"""
    if files:
        for rel in files:
            for p in _expand(base, rel):
                if p.is_file():
                    r = p.relative_to(base).as_posix()
                    if not is_excluded(r):
                        yield r, p
    if dirs:
        for top in dirs:
            for d in _expand(base, top):
                if not d.is_dir():
                    continue
                for p in d.rglob("*"):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(base).as_posix()
                    if is_excluded(rel):
                        continue
                    try:
                        if p.stat().st_size > state.MAX_WORKSPACE_FILE_MB * 1024 * 1024:
                            continue
                    except OSError:
                        continue
                    yield rel, p


def asset_summary() -> dict:
    n = 0
    size = 0
    for _, p in iter_assets(state.CODEX_HOME, state.SYNC_DIRS, state.SYNC_FILES):
        n += 1
        try:
            size += p.stat().st_size
        except OSError:
            pass
    db_size = 0
    for dbc in state.DATABASES:
        db = codex.find_db(state.CODEX_HOME, dbc.get("glob") or "")
        if db is not None:
            try:
                db_size += db.stat().st_size
            except OSError:
                pass
    return {"files": n, "size": size, "db_size": db_size}


# ─────────────────────────── 数据库快照 ───────────────────────────

def _db_conf(label: str) -> dict | None:
    for c in state.DATABASES:
        if c.get("label") == label:
            return c
    return None


def _safe_sql(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def db_snapshot_filtered(src: Path, dst: Path, tables, filters: dict) -> None:
    """生成只含勾选会话相关行的「精简数据库快照」。

    filters: {table: (column, set(values))}。未在 filters 中的白名单表一律清空，
    语义与「勾选什么就同步什么」一致。pull 端 db_merge 只对存在且同列的表合并。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
        d.execute("PRAGMA foreign_keys=OFF")
        existing = [r[0] for r in d.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in existing:
            if t not in tables:
                d.execute(f'DROP TABLE IF EXISTS "{t}"')
                continue
            filt = filters.get(t)
            if filt is None:
                d.execute(f'DELETE FROM "{t}"')
                continue
            col, values = filt
            if not values:
                d.execute(f'DELETE FROM "{t}"')
                continue
            ph = ",".join("?" for _ in values)
            d.execute(f'DELETE FROM "{t}" WHERE "{col}" NOT IN ({ph})', list(values))
        d.commit()
        d.execute("VACUUM")
    finally:
        d.close()
        s.close()


def db_row_counts(path: Path) -> dict:
    out = {}
    for dbc in state.DATABASES:
        label = dbc.get("label") or "db"
        # 包内 db 按 label 命名目录，文件名保留原库名
        candidates = list(path.glob(f"{label}/*.sqlite")) if path.is_dir() else []
        if not candidates:
            continue
        out[label] = codex.db_row_counts(candidates[0], dbc.get("tables") or [])
    return out


# ─────────────────────────── 选择性清单（UI 用）───────────────────────────

def list_selectable() -> dict:
    """列出可勾选的同步条目：按 cwd/项目分组的会话。"""
    sessions = codex.list_sessions(state.CODEX_HOME, config.SESSION_INDEX_REL)
    projects: dict = {}
    for s in sessions:
        key = s.get("project") or "(未知项目)"
        projects.setdefault(key, []).append(s)
    groups = [{"name": k, "count": len(v)} for k, v in sorted(projects.items())]
    return {"projects": groups, "sessions": sessions}


# ─────────────────────────── 打包 ───────────────────────────

def build_package(sync_root: Path, dry: bool, selection: dict | None = None) -> dict:
    """把本机 Codex 资产打包成 codex/state/from-<标识>.zip。

    selection = {"sessions": [id, ...]}：非空时进入「选择性模式」，只打包勾选会话
    的 rollout 正文 + 对应 session_index 行 + 精简库快照（仅相关行）；其余白名单
    资产不打包（包小、传输快）。None 或空 = 全量。
    """
    sel_sessions = set((selection or {}).get("sessions") or [])
    has_sel = bool(sel_sessions)

    state_dir = sync_root / "codex" / "state"
    zip_path = state_dir / f"from-{state.HOST}.zip"
    staging = Path(tempfile.mkdtemp(prefix="cx-pack-"))
    info = {"host": state.HOST, "zip": str(zip_path), "files": 0, "bytes": 0}

    try:
        files_root = staging / "files"

        if has_sel:
            index = codex.list_sessions(state.CODEX_HOME, config.SESSION_INDEX_REL)
            by_id = {s["id"]: s for s in index}
            for sid in sel_sessions:
                s = by_id.get(sid)
                if not s:
                    continue
                sp = state.CODEX_HOME / s["file"]
                if not sp.is_file():
                    continue
                dst = files_root / s["file"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sp, dst)
                info["files"] += 1
            # session_index：只保留勾选 id 的行
            _write_filtered_index(staging, sel_sessions)
            # 精简库快照
            _snapshot_dbs(staging, sel_sessions if sel_sessions else None)
        else:
            # ── 全量：白名单资产 + 全部库快照 ──
            for rel, p in iter_assets(state.CODEX_HOME, state.SYNC_DIRS, state.SYNC_FILES):
                dst = files_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                info["files"] += 1
            _snapshot_dbs(staging, None)

        manifest = {
            "schema": MANIFEST_SCHEMA,
            "engine_version": ENGINE_VERSION,
            "host": state.HOST,
            "machine_name": state.MACHINE_NAME,
            "os": sys.platform,
            "codex_home": str(state.CODEX_HOME),
            "exported_at": now_iso(),
            "selective": has_sel,
            "db_rows": db_row_counts(staging / "db"),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # 清掉快照可能残留的 -shm / -wal（运行时索引，不属于快照内容）
        for junk in (staging / "db").rglob("*.sqlite-*"):
            try:
                junk.unlink()
            except OSError:
                pass

        if dry:
            total = sum(f.stat().st_size for f in staging.rglob("*") if f.is_file())
            info["bytes"] = total
            print(f"  [dry-run] 将打包 {info['files']} 个文件，未压缩约 {human(total)}")
            return info

        state_dir.mkdir(parents=True, exist_ok=True)
        tmp_zip = zip_path.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in staging.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(staging).as_posix())
        tmp_zip.replace(zip_path)
        info["bytes"] = zip_path.stat().st_size
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return info


def _write_filtered_index(staging: Path, sel_ids: set) -> None:
    """选择性模式：把 session_index.jsonl 过滤成只含勾选 id 的行，写入 staging。"""
    src = state.CODEX_HOME / config.SESSION_INDEX_REL
    if not src.is_file():
        return
    keep = []
    for e in codex.read_index(src):
        if isinstance(e, dict) and e.get("id") in sel_ids:
            keep.append(json.dumps(e, ensure_ascii=False, separators=(",", ":")))
    if not keep:
        return
    dst = staging / "files" / config.SESSION_INDEX_REL
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(keep) + "\n", encoding="utf-8")


def _snapshot_dbs(staging: Path, sel_ids) -> None:
    """把白名单里的每个库做一致性快照，放到 db/<label>/<原库名>.sqlite。

    sel_ids 非空时走「精简快照」：只保留与勾选会话相关的表行。
    """
    for dbc in state.DATABASES:
        glob_pat = dbc.get("glob") or ""
        label = dbc.get("label") or "db"
        src = codex.find_db(state.CODEX_HOME, glob_pat)
        if src is None:
            continue
        dst = staging / "db" / label / src.name
        if sel_ids:
            filters = _selective_filters(dbc, sel_ids)
            db_snapshot_filtered(src, dst, dbc.get("tables") or [], filters)
        else:
            codex.db_snapshot(src, dst)


def _selective_filters(dbc: dict, sel_ids) -> dict:
    """按表的主键/外键列，构造「只保留勾选会话」的过滤条件。"""
    ids = set(sel_ids)
    f = {}
    for t in dbc.get("tables") or []:
        if t == "threads":
            f[t] = ("id", ids)
        elif t in ("thread_dynamic_tools", "thread_attachments", "thread_goals"):
            f[t] = ("thread_id", ids)
        elif t == "thread_spawn_edges":
            f[t] = ("parent_thread_id", ids)
        elif t == "stage1_outputs":
            f[t] = ("thread_id", ids)
        else:
            f[t] = None      # 无明确归属的表：选择性模式下清空
    return f


def unpack(zip_path: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="cx-unpack-"))
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
        # zipfile 不还原 mtime（解压出来全是"现在"），会让「谁更新」的判断彻底失效，
        # 退化成"对方无条件覆盖本机" → 必须按包内记录的时间手动补回。
        for zi in z.infolist():
            if zi.is_dir():
                continue
            f = tmp / zi.filename
            if f.is_file():
                t = datetime(*zi.date_time).timestamp()
                os.utime(f, (t, t))
    return tmp


# ─────────────────────────── 合并远端 ───────────────────────────

def is_append_log(rel: str) -> bool:
    """追加型文本：rollout / ndjson 会话流。这类文件两边会各自往后追加，
    整文件覆盖必然丢行，只能做行级并集。"""
    return rel.lower().endswith(config.UNION_SUFFIX)


def merge_kind(rel: str) -> str:
    """决定合并策略：index（按 id 合并）/ union（行级并集）/ file（按 mtime）。"""
    if rel == config.SESSION_INDEX_REL:
        return "index"
    if is_append_log(rel):
        return "union"
    return "file"


def union_lines(remote_text: str, local_text: str) -> str | None:
    """行级并集：本机内容原样保留，只把对方独有的行补到末尾。
    用计数而不是集合做去重，同一文件内的重复行（空行、分隔线）不会被折叠。
    对方没有本机缺的行时返回 None，表示无需写入。"""
    local = local_text.splitlines()
    rest = Counter(local)
    merged = list(local)
    added = False
    for ln in remote_text.splitlines():
        if rest.get(ln, 0) > 0:
            rest[ln] -= 1          # 视为已存在（多数是公共前缀里的同一行）
            continue
        merged.append(ln)          # 本机没有 → 补进来
        added = True
    if not added:
        return None
    return "\n".join(merged) + "\n"


def merge_tree(src_root: Path, dst_root: Path, dry: bool, tag: str,
               pkg_time: float = 0.0) -> list:
    """合并目录树。追加型/索引文件走内容合并；其余按 mtime：
    缺失则补，本机旧则更新，本机新则保留。非日志文件若在对方出快照之后本机
    也改过（两边都动过），标记为冲突而不是默默覆盖。"""
    if not src_root.is_dir():
        return []
    actions = []
    for p in src_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src_root).as_posix()
        if is_excluded(rel):
            continue
        dp = dst_root / rel
        try:
            if not dp.exists():
                actions.append(("新增", tag, rel, p, dp))
                continue
            if sha256(p) == sha256(dp):
                continue
            kind = merge_kind(rel)
            if kind in ("union", "index"):
                actions.append(("行合并" if kind == "union" else "索引合并",
                                tag, rel, p, dp))
            elif p.stat().st_mtime > dp.stat().st_mtime:
                both = bool(pkg_time) and dp.stat().st_mtime > pkg_time + config.CLOCK_GRACE
                actions.append(("冲突·更新" if both else "更新", tag, rel, p, dp))
            else:
                actions.append(("本机较新·保留", tag, rel, p, dp))
        except OSError:
            continue
    return actions


def apply_actions(actions: list, dry: bool, stamp: str) -> tuple:
    """返回 (写入, 本机较新保留, 内容合并文件数, 补齐行数, 冲突数)。"""
    written = kept = merged = merged_lines = conflicts = 0
    for kind, _tag, rel, sp, dp in actions:
        if kind == "本机较新·保留":
            kept += 1
            continue

        if kind in ("行合并", "索引合并"):
            try:
                remote_text = sp.read_text(encoding="utf-8", errors="replace")
                local_text = dp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if kind == "索引合并":
                merged_text = codex.merge_session_index(remote_text, local_text)
                gained = 0
            else:
                merged_text = union_lines(remote_text, local_text)
                gained = (max(merged_text.count("\n") - local_text.count("\n"), 0)
                          if merged_text else 0)
            if merged_text is None:
                kept += 1
                continue
            merged += 1
            merged_lines += gained
            if dry:
                print(f"  [dry-run] {kind}  {rel}  (+{gained} 行)")
                continue
            dp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dp, dp.with_name(dp.name + f".bak-{stamp}"))
            dp.write_text(merged_text, encoding="utf-8")
            continue

        if dry:
            print(f"  [dry-run] {kind:<14} {rel}")
            continue
        dp.parent.mkdir(parents=True, exist_ok=True)
        suffix = (f".conflict-{state.HOST}-{stamp}" if kind == "冲突·更新"
                  else f".bak-{stamp}")
        if dp.exists():
            shutil.copy2(dp, dp.with_name(dp.name + suffix))
        shutil.copy2(sp, dp)
        written += 1
        if kind == "冲突·更新":
            conflicts += 1
            print(f"  ⚠ 冲突：{rel}\n"
                  f"     两边都改过 → 取 mtime 较新的对方版本，"
                  f"本机版本存为 {dp.name}{suffix}")
    return written, kept, merged, merged_lines, conflicts


def heal_append_logs(dry: bool, stamp: str) -> tuple:
    """自愈：历史上被整文件覆盖过的追加型日志，用同目录的 .bak-* 把丢掉的行并回来。
    只扫 sessions/** 与 session_index，幂等，重复执行不会产生新内容。
    返回 (文件数, 找回行数)。"""
    targets = []
    sessions = state.CODEX_HOME / "sessions"
    if sessions.is_dir():
        targets += [p for p in sessions.rglob("*.bak-*")]
    idx_dir = state.CODEX_HOME
    targets += [p for p in idx_dir.glob(f"{config.SESSION_INDEX_REL}.bak-*")]

    files = lines = 0
    for bak in targets:
        if not bak.is_file():
            continue
        orig = bak.with_name(bak.name.split(".bak-")[0])
        if not orig.is_file():
            continue
        try:
            rel = orig.relative_to(state.CODEX_HOME).as_posix()
        except ValueError:
            continue
        if merge_kind(rel) == "file":
            continue
        try:
            old_text = bak.read_text(encoding="utf-8", errors="replace")
            cur_text = orig.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if rel == config.SESSION_INDEX_REL:
            merged_text = codex.merge_session_index(old_text, cur_text)
            gained = 0
        else:
            merged_text = union_lines(old_text, cur_text)
            gained = (max(merged_text.count("\n") - cur_text.count("\n"), 0)
                      if merged_text else 0)
        if merged_text is None:
            continue
        files += 1
        lines += gained
        print(f"  自愈 {rel}: 从 .bak 找回 {gained} 行")
        if dry:
            continue
        shutil.copy2(orig, orig.with_name(orig.name + f".bak-{stamp}"))
        orig.write_text(merged_text, encoding="utf-8")
    return files, lines


# ─────────────────────────── pull ───────────────────────────

def pkg_sig(zp: Path) -> str:
    """包的「身份指纹」：大小 + 修改时间。用来判断这个包上次是否已经拉过。"""
    st = zp.stat()
    return f"{st.st_size}-{int(st.st_mtime)}"


def pull_marker_path(sync_root: Path) -> Path:
    return sync_root / "codex" / "state" / f".last-pull-{state.HOST}.json"


def load_pull_marker(sync_root: Path) -> dict:
    f = pull_marker_path(sync_root)
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {}


def save_pull_marker(sync_root: Path, marker: dict) -> None:
    try:
        pull_marker_path(sync_root).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _check_peer_version(meta: dict) -> None:
    peer = str(meta.get("engine_version") or "").strip()
    if peer and peer != ENGINE_VERSION:
        print(f"   ⚠ 对方引擎 {peer} · 本机 {ENGINE_VERSION} —— 版本不同，"
              f"建议两端升级到同一版本后再持续同步")
    schema = meta.get("schema")
    if schema is not None and schema != MANIFEST_SCHEMA:
        print(f"   ⚠ 对方包结构 v{schema} · 本机 v{MANIFEST_SCHEMA} —— "
              f"包格式不同，请联系维护者确认兼容性")


def merge_databases(tmp: Path, dry: bool) -> dict:
    """把包内 db/<label>/*.sqlite 合并进本机对应的库。"""
    total = {}
    db_root = tmp / "db"
    if not db_root.is_dir():
        return total
    for sub in sorted(p for p in db_root.iterdir() if p.is_dir()):
        dbc = _db_conf(sub.name)
        tables = (dbc or {}).get("tables") or []
        upsert = (dbc or {}).get("upsert") or {}
        glob_pat = (dbc or {}).get("glob")
        for inc in sorted(sub.glob("*.sqlite")):
            local = codex.find_db(state.CODEX_HOME, glob_pat) if glob_pat else None
            if local is None:
                # 本机还没有这个库（新机首次拉取）→ 直接落盘
                dst = state.CODEX_HOME / inc.name
                if dry:
                    print(f"  [dry-run] 新建数据库 {inc.name}")
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(inc, dst)
                total[sub.name] = total.get(sub.name, 0) + 1
                continue
            if dry:
                before = codex.db_row_counts(local, tables)
                after = codex.db_row_counts(inc, tables)
                for t in tables:
                    if t in after and after[t] != before.get(t, 0):
                        print(f"  [dry-run] 数据库 {sub.name}.{t}: "
                              f"本机 {before.get(t,0)} 行 / 对方 {after[t]} 行")
                continue
            stats = codex.db_merge(local, inc, tables, upsert)
            for t, v in stats.items():
                print(f"  数据库 {sub.name}.{t}: ~{v} 行")
                total[f"{sub.name}.{t}"] = v
    return total


def do_pull(sync_root: Path, dry: bool, force: bool = False) -> int:
    sync_state = sync_root / "codex" / "state"
    if not sync_state.is_dir():
        print(f"[!] 同步目录不存在：{sync_state}")
        print("    先在本机执行一次 push，或检查同步盘是否已就绪")
        return 1

    others = sorted(p for p in sync_state.glob("from-*.zip")
                    if p.stem != f"from-{state.HOST}")
    if not others:
        print("  没有发现其他机器的包（本机是第一个）")
        return 0

    running = codex_running()
    allow_db = (not running) or force
    if running and force:
        print("[!] Codex 仍在运行，--force 已生效，即将写入数据库（有损坏风险）")

    stamp = ts()
    total_added = total_written = total_kept = 0
    total_merged = total_merged_lines = 0
    total_conflicts = 0
    skipped = 0

    marker = load_pull_marker(sync_root)
    marker_dirty = False
    pending_seen = False

    heal_files, heal_lines = heal_append_logs(dry, stamp)

    for zp in others:
        sig = pkg_sig(zp)
        if not dry and not force and marker.get(zp.name) == sig:
            print(f"\n── {zp.name}  与上次拉取时相同，跳过 ──")
            skipped += 1
            continue
        pending_seen = True
        print(f"\n── 处理 {zp.name}  ({human(zp.stat().st_size)}) ──")
        tmp = unpack(zp)
        try:
            mf = tmp / "manifest.json"
            pkg_time = 0.0
            if mf.is_file():
                meta = json.loads(mf.read_text(encoding="utf-8"))
                print(f"   来源 {meta.get('host')} · {meta.get('exported_at')}")
                _check_peer_version(meta)
                try:
                    pkg_time = datetime.fromisoformat(meta["exported_at"]).timestamp()
                except (KeyError, ValueError, TypeError):
                    pkg_time = 0.0

            # 1) 文件层
            acts = merge_tree(tmp / "files", state.CODEX_HOME, dry, "文件", pkg_time)
            if acts:
                w, k, m, ml, cf = apply_actions(acts, dry, stamp)
                total_written += w
                total_kept += k
                total_merged += m
                total_merged_lines += ml
                total_conflicts += cf

            # 2) 数据库层
            if dry:
                merge_databases(tmp, True)
            elif not allow_db:
                print("  [跳过] 未关闭 Codex，数据库合并已跳过")
            else:
                stats = merge_databases(tmp, False)
                total_added += sum(v for v in stats.values() if isinstance(v, int))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        if not dry:
            marker[zp.name] = sig
            marker_dirty = True

    if not dry and marker_dirty:
        save_pull_marker(sync_root, marker)

    if not dry and not pending_seen:
        print(f"\n合计：{skipped} 个远端包均与上次拉取时相同，无需处理"
              f"（要强制重跑加 --force）")
        return 0

    print(f"\n合计：文件写入 {total_written} 个，本机较新保留 {total_kept} 个，"
          f"数据库变更 {total_added} 行")
    if skipped:
        print(f"      另有 {skipped} 个包未变化，已跳过")
    if total_merged:
        print(f"      会话/索引内容合并 {total_merged} 个，补齐 {total_merged_lines} 行")
    if heal_files:
        print(f"      从历史 .bak 自愈 {heal_files} 个文件，找回 {heal_lines} 行")
    if total_conflicts:
        print(f"      ⚠ {total_conflicts} 个文件两边都改过，本机版本已存为 "
              f".conflict-{state.HOST}-{stamp}")
    if not dry and (total_written or total_merged or heal_files):
        print(f"      被覆盖的原文件均已备份为 .bak-{stamp}")
    return 0


# ─────────────────────────── 状态 / 自检 ───────────────────────────

def do_status(sync_root: Path, root_src: str = "") -> int:
    s = asset_summary()
    sessions = codex.list_sessions(state.CODEX_HOME, config.SESSION_INDEX_REL)
    print(f"本机：{state.MACHINE_NAME}    标识 {state.HOST}    引擎 {ENGINE_VERSION}")
    print(f"  Codex 目录       {state.CODEX_HOME}")
    print(f"  可同步文件       {s['files']} 个 / {human(s['size'])}")
    print(f"  会话数           {len(sessions)}")
    print(f"  数据库           {human(s['db_size'])}")
    sync_state = sync_root / "codex" / "state"
    print(f"\n同步目录：{sync_root}" + (f"   ← {root_src}" if root_src else ""))
    if not sync_state.is_dir():
        print("  （尚未创建）")
        return 0
    marker = load_pull_marker(sync_root)
    found = False
    for zp in sorted(sync_state.glob("from-*.zip")):
        if zp.stem == f"from-{state.HOST}":
            tag = "[本机]"
        elif marker.get(zp.name) == pkg_sig(zp):
            tag = "[远端·已同步]"
        else:
            tag = "[远端·待拉取]"
        print(f"  {tag:<14} {zp.name:<28} "
              f"{human(zp.stat().st_size):>9}  "
              f"{datetime.fromtimestamp(zp.stat().st_mtime):%Y-%m-%d %H:%M}")
        found = True
    if not found:
        print("  （还没有任何机器的包）")
    return 0


def detect_unknown_assets() -> list:
    """扫描 ~/.codex 顶层，找出「真正会被漏掉」的目录（Codex 升级新增资产的提示）。"""
    covered = set()
    for rel, _p in iter_assets(state.CODEX_HOME, state.SYNC_DIRS, state.SYNC_FILES):
        covered.add(rel.split("/")[0])
    covered |= state.NEVER_SEGMENTS

    unknown = []
    if not state.CODEX_HOME.is_dir():
        return unknown
    try:
        entries = sorted(state.CODEX_HOME.iterdir())
    except OSError:
        return unknown

    for p in entries:
        try:
            if not p.is_dir() or p.name.startswith(".") or p.name in covered:
                continue
            has_syncable = False
            for f in p.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(state.CODEX_HOME).as_posix()
                if is_excluded(rel):
                    continue
                try:
                    if f.stat().st_size > state.MAX_WORKSPACE_FILE_MB * 1024 * 1024:
                        continue
                except OSError:
                    continue
                has_syncable = True
                break
        except OSError:
            continue
        if has_syncable:
            unknown.append(p.name)
    return unknown


def do_doctor(sync_root: Path, root_src: str = "", man_sources=None) -> int:
    problems = []
    print("── 环境自检 ──")
    print(f"  本机           {state.MACHINE_NAME}   标识 {state.HOST}")
    print(f"  引擎版本       {ENGINE_VERSION}")
    print(f"  Python         {sys.version.split()[0]}")
    print(f"  配置目录       {config.CONFIG_DIR}")
    if man_sources:
        shown = " < ".join(
            "内置默认" if s == "内置默认" else Path(s).name for s in man_sources)
        print(f"  清单来源       {shown}")

    if state.CODEX_HOME.is_dir():
        print(f"  Codex 目录     {state.CODEX_HOME}  ✓")
    else:
        problems.append(f"Codex 目录不存在：{state.CODEX_HOME}")

    # 数据库可读性
    for dbc in state.DATABASES:
        src = codex.find_db(state.CODEX_HOME, dbc.get("glob") or "")
        if src is None:
            continue
        try:
            c = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()
            c.close()
            print(f"  数据库 {src.name:<18} ✓")
        except sqlite3.Error as e:
            problems.append(f"数据库读取失败 {src.name}：{e}")

    sessions = codex.list_sessions(state.CODEX_HOME, config.SESSION_INDEX_REL)
    print(f"  会话文件        {len(sessions)} 个")

    if codex_running():
        print("  Codex 进程      正在运行 —— pull 前需要关闭")
    else:
        print("  Codex 进程      未运行  ✓")

    print(f"  进程检测关键字  {', '.join(platform_win.process_hints())}"
          f"   （忽略：{', '.join(platform_win.process_ignore()) or '无'}）")

    od = config.find_onedrive()
    print(f"  同步盘探测     {od if od else '未自动发现（已用 config.json 指定则无妨）'}")

    try:
        sync_root.mkdir(parents=True, exist_ok=True)
        probe = sync_root / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"  同步目录可写   {sync_root}  ✓" + (f"   ← {root_src}" if root_src else ""))
    except OSError as e:
        problems.append(f"同步目录不可写：{e}")

    unknown = detect_unknown_assets()
    if unknown:
        print(f"\n⚠ 有 {len(unknown)} 个目录不在同步白名单里：")
        for u in unknown:
            print(f"      {u}")
        print("  可能是 Codex 升级后新增的资产。需要一起同步的话，")
        print(f"  把它们加进 {config.CONFIG_DIR / 'manifest.json'} 的 \"sync_dirs\" 即可。")

    if problems:
        print("\n发现以下问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("\n全部检查通过 ✓")
    return 0
