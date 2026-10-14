# -*- coding: utf-8 -*-
"""Codex 同步助手 —— 本地 Web 面板。

零第三方依赖，复用 codex_sync 引擎。启动后浏览器打开 localhost 面板，
可视化执行 doctor / status / sessions / push / pull 并查看输出。

用法：
    cx-sync ui                 # pip 安装后启动面板（默认端口 8766）
    python -m codex_sync.ui --port 9001
"""
import json
import sys
import webbrowser
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path

from . import ENGINE_VERSION, codex, config, core, platform_win, state

DEFAULT_PORT = 8766


def _init_env():
    """复刻 cli.main 的初始化，供 /api/info 读取当前状态。"""
    man, sources = config.load_manifest()
    config.apply_manifest(man)
    cfg = config.load_config()
    platform_win.set_hints(cfg.get("codex_process_hints"),
                           cfg.get("codex_process_ignore"))
    sync_root, root_src = config.resolve_sync_root(cfg, None)
    if sync_root is not None:
        config.resolve_machine_id(cfg, sync_root)
    return cfg, sync_root, root_src, sources


def build_info() -> dict:
    cfg, sync_root, root_src, sources = _init_env()
    sessions = codex.list_sessions(state.CODEX_HOME, config.SESSION_INDEX_REL)
    return {
        "engine": ENGINE_VERSION,
        "host": state.HOST,
        "name": state.MACHINE_NAME,
        "codex_home": str(state.CODEX_HOME),
        "sync_root": str(sync_root) if sync_root else None,
        "sync_src": root_src,
        "files": len(state.SYNC_FILES),
        "dirs": len(state.SYNC_DIRS),
        "databases": len(state.DATABASES),
        "sessions": len(sessions),
        "running": platform_win.codex_running(),
        "manifest": " < ".join(sources),
    }


def get_config() -> dict:
    cfg = config.load_config()
    return {
        "sync_root": cfg.get("sync_root"),
        "machine_name": cfg.get("machine_name"),
        "config_file": str(config.CONFIG_DIR / "config.json"),
    }


def set_sync_root(path: str) -> dict:
    """把同步目录写入 config.json（支持任意云盘 / U 盘 / 局域网共享路径）。"""
    path = (path or "").strip()
    if not path:
        return {"ok": False, "error": "路径为空"}
    p = Path(path)
    if not p.is_dir():
        return {"ok": False, "error": f"目录不存在：{path}"}
    cfg = config.load_config()
    cfg["sync_root"] = str(p)
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (config.CONFIG_DIR / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "sync_root": str(p)}


def run_action(action: str, selection: dict | None = None) -> dict:
    """执行一个动作，复用 cli.main 的完整逻辑，捕获 stdout。"""
    from . import cli
    buf = StringIO()
    code = 0
    argv = [action]
    if selection and action == "push":
        argv += ["--selection", json.dumps(selection, ensure_ascii=False)]
    try:
        with redirect_stdout(buf):
            code = cli.main(argv)
    except SystemExit as e:
        code = int(e.code or 0)
    except Exception as e:  # noqa: BLE001
        buf.write(f"[异常] {e}\n")
        code = 1
    return {"action": action, "code": code, "output": buf.getvalue()}


# ─────────────────────────── 前端页面 ───────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex 同步助手</title>
<style>
  :root {
    --bg: #f4f5f7; --card: #ffffff; --line: #e6e8eb;
    --text: #1f2329; --muted: #8a919c;
    --brand: #0b7285; --brand-soft: #e3f3f5;
    --ok: #16a34a; --warn: #d97706; --err: #dc2626;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; padding: 32px 20px;
  }
  .wrap { max-width: 880px; margin: 0 auto; }
  header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 20px; }
  header h1 { font-size: 24px; font-weight: 700; letter-spacing: -.3px; }
  header .ver {
    font-size: 12px; color: var(--brand); background: var(--brand-soft);
    padding: 2px 8px; border-radius: 999px; font-weight: 600;
  }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 16px 18px;
  }
  .card .k { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .card .v { font-size: 15px; font-weight: 600; word-break: break-all; line-height: 1.4; }
  .edit-link { color: var(--brand); cursor: pointer; font-size: 12px; font-weight: 500; margin-left: 6px; }
  .edit-link:hover { text-decoration: underline; }
  .actions { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0; }
  button {
    appearance: none; border: 1px solid var(--line); background: var(--card);
    border-radius: 14px; padding: 18px 12px; cursor: pointer; font-size: 14px;
    font-weight: 600; color: var(--text); transition: all .15s; font-family: inherit;
  }
  button:hover { border-color: var(--brand); background: var(--brand-soft); color: var(--brand); }
  button .ico { display: block; font-size: 24px; margin-bottom: 6px; }
  button .sub { display: block; font-size: 11px; color: var(--muted); font-weight: 400; margin-top: 3px; }
  button.busy { opacity: .5; pointer-events: none; }
  button.pull { border-color: #f0c7a0; }
  .log {
    background: #14161a; color: #d6dbe2; border-radius: 14px; padding: 16px 18px;
    font-family: "Cascadia Code", "JetBrains Mono", Consolas, monospace;
    font-size: 12.5px; line-height: 1.6; white-space: pre-wrap; min-height: 180px;
    overflow: auto; max-height: 440px;
  }
  .log .dim { color: #6b7280; }
  .log .ok { color: #4ade80; } .log .warn { color: #fbbf24; } .log .err { color: #f87171; }
  .badge { font-size: 12px; padding: 2px 8px; border-radius: 6px; font-weight: 600; }
  .badge.ok { background: #dcfce7; color: var(--ok); }
  .badge.err { background: #fee2e2; color: var(--err); }
  .badge.warn { background: #fef3c7; color: var(--warn); }
  .select-card { margin: 16px 0; }
  .select-head { display: flex; justify-content: space-between; align-items: center; cursor: pointer; padding: 14px 18px; }
  .select-head .t { font-weight: 700; }
  .select-head .hint { font-size: 12px; color: var(--muted); }
  .select-body { padding: 0 18px 16px; }
  .select-toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
  .select-toolbar input { flex: 1; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; font-size: 13px; font-family: inherit; }
  .select-toolbar .mini { padding: 6px 12px; font-size: 12px; }
  .check-list { display: grid; grid-template-columns: 1fr; gap: 4px; max-height: 400px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; padding: 8px; background: #fafbfc; }
  .check-list label { display: flex; align-items: center; gap: 8px; font-size: 13px; padding: 4px 6px; border-radius: 6px; cursor: pointer; }
  .check-list label:hover { background: var(--brand-soft); }
  .check-list input { accent-color: var(--brand); }
  .check-list .sess-main { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .check-list .ws-tag { font-size: 10.5px; color: var(--brand); background: var(--brand-soft); padding: 1px 6px; border-radius: 999px; white-space: nowrap; flex: none; }
  .check-list .time { color: var(--muted); font-size: 11px; white-space: nowrap; flex: none; }
  @media (max-width: 640px) { .actions { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Codex 同步助手</h1>
    <span class="ver" id="ver">加载中…</span>
    <span id="runbadge"></span>
  </header>

  <div class="card help-card" style="margin-bottom:16px">
    <div class="select-head" onclick="toggleHelp()">
      <span class="t">📖 使用说明</span>
      <span class="hint">点开看步骤</span>
    </div>
    <div id="help-body" style="display:none;padding:4px 18px 14px">
      <ol style="font-size:13px;line-height:2.1;color:#3a3f47;margin:0;padding-left:20px">
        <li><b>自检</b>：第一次先用它确认环境正常。</li>
        <li><b>看差异</b>：对比本机和同步目录，只看不改。</li>
        <li><b>选择同步范围</b>：勾选要同步的会话（不勾 = 全量）。</li>
        <li><b>上传</b>：本机 → 同步目录，只传勾选的内容。</li>
        <li><b>拉取</b>：同步目录 → 本机，<b style="color:#dc2626">需先完全退出 Codex</b>。</li>
      </ol>
      <div style="font-size:12px;color:#8a919c;margin-top:6px">
        双机同步：数据源那台「勾选 + 上传」，目标那台「拉取」。
      </div>
    </div>
  </div>

  <div class="grid">
    <div class="card"><div class="k">本机标识</div><div class="v" id="host">—</div></div>
    <div class="card"><div class="k">同步目录 <span class="edit-link" onclick="editSyncRoot()">✎ 修改</span></div><div class="v" id="sync">—</div></div>
    <div class="card"><div class="k">同步范围</div><div class="v" id="scope">—</div></div>
    <div class="card"><div class="k">Codex 目录</div><div class="v" id="chome">—</div></div>
  </div>

  <div class="actions">
    <button data-action="doctor"><span class="ico">🔍</span>自检<span class="sub">环境与白名单</span></button>
    <button data-action="status"><span class="ico">📊</span>看差异<span class="sub">两侧对比，不动文件</span></button>
    <button data-action="push"><span class="ico">⬆️</span>上传<span class="sub">本机 → 同步目录</span></button>
    <button data-action="pull" class="pull"><span class="ico">⬇️</span>拉取<span class="sub">同步目录 → 本机</span></button>
  </div>

  <div class="card select-card">
    <div class="select-head" onclick="toggleSelect()">
      <span class="t">🎯 选择同步范围</span>
      <span class="hint" id="sel-hint">默认全量</span>
    </div>
    <div class="select-body" id="sel-body" style="display:none">
      <div class="select-toolbar">
        <input id="sel-search" placeholder="搜索会话 / 项目…" oninput="renderItems()">
        <button class="mini" onclick="selAll(true)">全选</button>
        <button class="mini" onclick="selAll(false)">清空</button>
      </div>
      <div class="check-list" id="sess-list"></div>
      <div class="dim" style="font-size:12px;color:var(--muted);margin-top:8px">勾选后点「上传」只同步勾选的会话（含正文、索引行与元数据行）；不勾选 = 全量同步。</div>
    </div>
  </div>

  <div class="log" id="log"><span class="dim">点击上方按钮执行操作，输出会显示在这里。</span></div>
</div>

<script>
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escAttr(s) { return String(s).replace(/"/g, '&quot;'); }
function fmtTime(ms) {
  if (!ms) return '';
  const d = new Date(ms > 1e11 ? ms : ms * 1000);
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

async function loadInfo() {
  try {
    const r = await fetch('/api/info'); const d = await r.json();
    document.getElementById('ver').textContent = 'v' + d.engine;
    document.getElementById('host').textContent = d.host + '（' + d.name + '）';
    document.getElementById('sync').textContent = d.sync_root || '(未探测到)';
    document.getElementById('scope').textContent = d.files + ' 文件 · ' + d.dirs + ' 目录 · ' + d.databases + ' 库 · ' + d.sessions + ' 会话';
    document.getElementById('chome').textContent = d.codex_home;
    document.getElementById('runbadge').innerHTML = d.running
      ? '<span class="badge warn">Codex 运行中 · 拉取前请关闭</span>'
      : '<span class="badge ok">Codex 未运行</span>';
  } catch (e) { document.getElementById('ver').textContent = '连接失败'; }
}

let items = { sessions: [] };
let checked = new Set();

async function loadItems() {
  try { const r = await fetch('/api/items'); items = await r.json(); renderItems(); } catch (e) {}
}

function renderItems() {
  const q = (document.getElementById('sel-search').value || '').toLowerCase();
  const ss = (items.sessions || []).filter(s =>
    (s.name || '').toLowerCase().includes(q) || (s.project || '').toLowerCase().includes(q));
  document.getElementById('sess-list').innerHTML = ss.map(s =>
    '<label><input type="checkbox" value="' + escAttr(s.id) + '"' + (checked.has(s.id) ? ' checked' : '') + ' onchange="onSess(this)">' +
    '<span class="sess-main" title="' + escAttr(s.name) + '">' + esc(s.name) + '</span>' +
    (s.project ? '<span class="ws-tag">📁 ' + esc(s.project) + '</span>' : '') +
    '<span class="time">' + fmtTime(s.updated) + '</span></label>').join('') || '<span class="dim" style="padding:8px">未发现会话。</span>';
  updateHint();
}

function onSess(cb) { if (cb.checked) checked.add(cb.value); else checked.delete(cb.value); updateHint(); }
function updateHint() {
  document.getElementById('sel-hint').textContent = checked.size ? ('已选 ' + checked.size + ' 项') : '默认全量';
}
function toggleSelect() {
  const b = document.getElementById('sel-body');
  b.style.display = (b.style.display === 'none') ? 'block' : 'none';
}
function toggleHelp() {
  const b = document.getElementById('help-body');
  b.style.display = (b.style.display === 'none') ? 'block' : 'none';
}
function selAll(v) {
  if (v) (items.sessions || []).forEach(s => checked.add(s.id)); else checked.clear();
  renderItems();
}

async function editSyncRoot() {
  const cur = document.getElementById('sync').textContent;
  const def = (cur && cur !== '—' && cur !== '(未探测到)') ? cur : '';
  const p = prompt('输入同步目录完整路径（任意云盘 / U盘 / 局域网共享，例如 D:\\坚果云\\codex-sync）：', def);
  if (p === null) return;
  const r = await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({sync_root: p})
  });
  const d = await r.json();
  if (d.ok) { alert('已设置同步目录：' + d.sync_root); loadInfo(); }
  else { alert('设置失败：' + d.error); }
}

async function run(btn) {
  const action = btn.dataset.action;
  if (action === 'pull') {
    if (!confirm('拉取会写入 state_*.sqlite，需先完全退出 Codex。\n确认要拉取吗？')) return;
  }
  document.querySelectorAll('button').forEach(b => b.classList.add('busy'));
  const log = document.getElementById('log');
  log.innerHTML = '<span class="dim">执行 ' + action + ' 中…</span>';
  try {
    const body = {action: action};
    if (action === 'push' && checked.size) body.selection = {sessions: [...checked]};
    const r = await fetch('/api/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    const ok = d.code === 0;
    const tag = ok ? '<span class="badge ok">完成</span>' : '<span class="badge err">退出码 ' + d.code + '</span>';
    log.innerHTML = '<span class="dim">$ cx-sync ' + action + '　' + tag + '</span>\n' + esc(d.output);
  } catch (e) {
    log.innerHTML = '<span class="err">请求失败：' + esc(String(e)) + '</span>';
  } finally {
    document.querySelectorAll('button').forEach(b => b.classList.remove('busy'));
    if (action === 'pull' || action === 'doctor') loadInfo();
  }
}

document.querySelectorAll('button').forEach(b => b.addEventListener('click', () => run(b)));
loadInfo();
loadItems();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/info":
            self._send(200, json.dumps(build_info(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif self.path == "/api/items":
            self._send(200, json.dumps(core.list_selectable(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif self.path == "/api/config":
            self._send(200, json.dumps(get_config(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            payload = {}

        if self.path == "/api/config":
            result = set_sync_root(payload.get("sync_root", ""))
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if self.path != "/api/run":
            self._send(404, b"not found", "text/plain")
            return
        action = payload.get("action", "")
        selection = payload.get("selection")
        if action not in ("doctor", "status", "sessions", "push", "pull", "where"):
            self._send(400, json.dumps({"error": "bad action"}).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        result = run_action(action, selection)
        self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, *args):  # 静默，不打印每次请求
        pass


def main(port: int | None = None) -> int:
    if port is None:
        port = DEFAULT_PORT
        if "--port" in sys.argv:
            try:
                port = int(sys.argv[sys.argv.index("--port") + 1])
            except (ValueError, IndexError):
                pass
    # 只用 127.0.0.1，局域网不可达。
    server = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"

    def _say(msg: str) -> None:
        try:
            print(msg)
        except Exception:  # noqa: BLE001
            pass

    _say(f"Codex 同步助手 面板已启动：{url}")
    _say("按 Ctrl+C 停止。")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _say("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
