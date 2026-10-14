#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守护进程入口垫片（源码 / 便携模式）。

    python cx_daemon.py                 前台常驻（Ctrl+C 退出，调试用）
    python cx_daemon.py --once          只跑一轮（自测 / 计划任务用）
    python cx_daemon.py --no-pull       只做 push
    python cx_daemon.py --interval 600  覆盖 push 间隔（秒）

静默后台运行请用同目录的 cx-daemon.vbs（开机自启指向它）。
等价写法：python cx_sync.py daemon [...] / cx-sync daemon [...]
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

# 根目录清单优先（存在才生效）
if (ROOT / "default-manifest.json").is_file():
    os.environ.setdefault("CODEX_SYNC_MANIFEST_DIR", str(ROOT))

from codex_sync.daemon import run  # noqa: E402


def _arg(flag, cast=int, default=None):
    if flag in sys.argv:
        try:
            return cast(sys.argv[sys.argv.index(flag) + 1])
        except (ValueError, IndexError):
            return default
    return default


if __name__ == "__main__":
    sys.exit(run(
        once="--once" in sys.argv,
        push_interval=_arg("--interval"),
        pull_interval=_arg("--pull-interval"),
        no_pull="--no-pull" in sys.argv,
        duration=float(_arg("--duration", float, 0.0) or 0.0),
        sync_root_override=(sys.argv[sys.argv.index("--sync-root") + 1]
                            if "--sync-root" in sys.argv else None),
        codex_home=(sys.argv[sys.argv.index("--codex-home") + 1]
                    if "--codex-home" in sys.argv else None),
        manifest_dir=(sys.argv[sys.argv.index("--manifest-dir") + 1]
                      if "--manifest-dir" in sys.argv else None),
    ))
