#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""源码 / 便携模式入口垫片。

    python cx_sync.py doctor
    python cx_sync.py status
    python cx_sync.py push
    python cx_sync.py pull
    python cx_sync.py ui

刻意不叫 codex_sync.py：那个名字会与 src/codex_sync 包同名，
在仓库根目录执行 `python -m codex_sync.ui` 时被本文件遮蔽，
导致 `import codex_sync` 解析成单文件模块而非包。

作用：
  1. 把 ./src 加进 sys.path，免安装即可运行；
  2. 通过环境变量 CODEX_SYNC_MANIFEST_DIR 指明根目录那份 default-manifest.json，
     让用户直接编辑根目录清单即可改同步范围（不必进到包里面）。

pip 安装后无需本文件，直接用 `cx-sync <action>` 控制台命令。
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

from codex_sync.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
