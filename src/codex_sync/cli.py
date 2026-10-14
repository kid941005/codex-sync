# -*- coding: utf-8 -*-
"""命令行入口。

    cx-sync doctor     环境自检（先跑这个）
    cx-sync where      看当前生效的配置与清单来源
    cx-sync status     看两侧差异，不动文件
    cx-sync sessions   列出本机会话（按项目分组）
    cx-sync push       本机 -> 同步目录
    cx-sync pull       同步目录 -> 本机
    cx-sync ui         打开本地 Web 面板（可视化操作）
    cx-sync daemon     后台常驻：定时 push + 抢冷窗口 pull（开机自启用这个）

⚠️ 铁律
    1. push 可以在 Codex 运行时执行（只读快照）
    2. pull 会写 state_*.sqlite，执行前必须完全退出 Codex
       —— 守护进程会自动判断，只在 Codex 未运行时 pull
    3. 同一时间只让一台电脑 push
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ENGINE_VERSION
from . import codex
from . import config
from . import core
from . import platform_win
from . import state
from .utils import human


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cx-sync",
        description="Codex 全资产双向同步 —— 通过任意共享目录在机器之间同步会话、"
                    "索引、元数据库、技能与配置",
    )
    ap.add_argument(
        "action",
        choices=["doctor", "status", "sessions", "push", "pull", "where", "ui",
                 "daemon"],
        help="doctor 自检 / where 看配置 / status 看差异 / sessions 列会话 / "
             "push 上传 / pull 拉取 / ui 面板 / daemon 后台常驻",
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    ap.add_argument("--force", action="store_true",
                    help="Codex 运行时仍强制写库（有损坏风险）")
    ap.add_argument("--sync-root", help="覆盖同步根目录（默认取 config.json）")
    ap.add_argument("--name", help="覆盖本机显示名")
    ap.add_argument("--manifest-dir", help="指定 default-manifest.json 所在目录")
    ap.add_argument("--codex-home", help="覆盖 Codex 目录（测试用）")
    ap.add_argument("--project", help="sessions 动作：只看某个项目")
    ap.add_argument("--port", type=int, default=None,
                    help="ui 动作：面板端口（默认 8766）")
    ap.add_argument("--selection",
                    help='选择性同步 JSON：{"sessions":[...]}（供 UI 使用）')

    # ── daemon 动作专用 ──
    ap.add_argument("--once", action="store_true",
                    help="daemon：只跑一轮就退出（自测 / 计划任务用）")
    ap.add_argument("--interval", type=int, default=None,
                    help="daemon：覆盖 push 间隔秒数（默认 900）")
    ap.add_argument("--pull-interval", type=int, default=None,
                    help="daemon：覆盖冷窗口探测间隔秒数（默认 300）")
    ap.add_argument("--no-pull", action="store_true",
                    help="daemon：只做 push，不自动 pull")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="daemon：跑满这么多秒后自行退出（0 = 永久常驻）")
    ap.add_argument("--stop", action="store_true", help="daemon：请求正在运行的守护退出")
    ap.add_argument("--status", action="store_true",
                    help="daemon：查看守护状态与最近日志")
    ap.add_argument("--quiet", action="store_true",
                    help="daemon：不往标准输出打印（只写日志）")
    ap.add_argument("--version", action="version", version=f"cx-sync {ENGINE_VERSION}")
    return ap


def _init(args) -> tuple:
    """公共初始化：路径覆写 -> 清单 -> 配置 -> 机器标识。返回 (sync_root, root_src, man_sources)。"""
    if args.codex_home:
        state.CODEX_HOME = Path(args.codex_home)

    man, man_sources = config.load_manifest(
        Path(args.manifest_dir) if args.manifest_dir else None)
    config.apply_manifest(man)

    cfg = config.load_config()
    if args.name:
        cfg = dict(cfg, machine_name=args.name)
    platform_win.set_hints(cfg.get("codex_process_hints"),
                           cfg.get("codex_process_ignore"))

    sync_root, root_src = config.resolve_sync_root(cfg, args.sync_root)
    if sync_root is None:
        print(root_src)          # root_src 此时是引导文本
        return None, None, man_sources, cfg
    config.resolve_machine_id(cfg, sync_root)
    return sync_root, root_src, man_sources, cfg


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    # ui / daemon 是独立入口：它们自己会做初始化，不参与同步流程的加载。
    if args.action == "ui":
        from . import ui
        return ui.main(args.port)

    if args.action == "daemon":
        from . import daemon
        if args.status:
            return daemon.show_status()
        if args.stop:
            return daemon.request_stop()
        return daemon.run(
            once=args.once,
            push_interval=args.interval,
            pull_interval=args.pull_interval,
            no_pull=args.no_pull,
            duration=args.duration,
            sync_root_override=args.sync_root,
            codex_home=args.codex_home,
            manifest_dir=args.manifest_dir,
            quiet=args.quiet,
        )

    sync_root, root_src, man_sources, cfg = _init(args)
    if sync_root is None:
        return 1

    first_run = not (config.CONFIG_DIR / "machine-id").is_file()
    if first_run and args.action in ("push", "pull"):
        print("首次运行，已为本机分配标识")
        config.describe_scope()
        print(f"  同步目录：{sync_root}   ← {root_src}")
        print()

    if args.action == "where":
        print(f"引擎版本   {ENGINE_VERSION}")
        print(f"本机标识   {state.HOST}    （展示名 {state.MACHINE_NAME}）")
        print(f"Codex 目录 {state.CODEX_HOME}")
        print(f"配置目录   {config.CONFIG_DIR}")
        print(f"同步目录   {sync_root}   ← {root_src}")
        print(f"清单来源   {' < '.join(man_sources)}")
        print(f"同步范围   {len(state.SYNC_FILES)} 个文件 · {len(state.SYNC_DIRS)} 个目录 · "
              f"{len(state.DATABASES)} 个库")
        print(f"排除规则   {len(state.NEVER_SEGMENTS)} 段 · {len(state.NEVER_SUFFIX)} 后缀 · "
              f"{len(state.EXTRA_GLOBS)} glob · 敏感词 {len(state.SENSITIVE_WORDS)}")
        return 0

    if args.action == "doctor":
        return core.do_doctor(sync_root, root_src, man_sources)
    if args.action == "status":
        return core.do_status(sync_root, root_src)

    if args.action == "sessions":
        sessions = codex.list_sessions(state.CODEX_HOME, config.SESSION_INDEX_REL)
        if args.project:
            sessions = [s for s in sessions if s["project"] == args.project]
        if not sessions:
            print("  未发现会话文件（sessions/**/rollout-*.jsonl）")
            return 0
        print(f"  共 {len(sessions)} 个会话：")
        for s in sessions[:200]:
            when = ""
            if s.get("updated"):
                from datetime import datetime
                when = datetime.fromtimestamp(s["updated"]).strftime("%Y-%m-%d %H:%M")
            print(f"  {when:<16} {s['id'][:8]}…  {s['project']:<18} "
                  f"{human(s['size']):>8}  {s['name']}")
        return 0

    if args.action == "push":
        selection = None
        if args.selection:
            try:
                selection = json.loads(args.selection)
            except ValueError:
                print("[!] --selection 不是合法 JSON，已按全量同步处理")
        print(f"方向：{state.HOST} -> {sync_root}")
        info = core.build_package(sync_root, args.dry_run, selection)
        if args.dry_run:
            return 0
        print(f"  包已生成：{info['zip']}")
        print(f"  {info['files']} 个文件，压缩后 {human(info['bytes'])}")
        print("  下一步：等同步盘同步完成，再到另一台电脑执行 pull")
        return 0

    if args.action == "pull":
        if platform_win.codex_running() and not args.force:
            print("[!] 请先完全退出 Codex 再执行 pull")
            print("    （数据库需要独占写入；确认风险可加 --force）")
            return 1
        print(f"方向：{sync_root} -> {state.HOST}")
        return core.do_pull(sync_root, args.dry_run, args.force)

    return 0


if __name__ == "__main__":
    sys.exit(main())
