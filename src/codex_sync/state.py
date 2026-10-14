# -*- coding: utf-8 -*-
"""运行时可变状态。

★ 为什么单独一个模块
    这套程序里有相当多的「全局状态」：同步目录、白名单、机器标识……
    它们都需要被多个函数共享，而且命令行参数会覆写其中一部分。

    如果拆包时让别的模块 `from .config import CODEX_HOME`，那么拿到的是
    **导入那一刻的值**；之后 cli 里 `CODEX_HOME = Path(args.codex_home)`
    只会改 config 模块自己的变量，core 里根本看不到 —— 这类 bug 极难查。

    所以：所有可变状态只在这里定义，任何地方访问都用 `state.CODEX_HOME`
    （属性查找发生在运行时，永远拿到最新值）。

不可变常量（版本号、默认清单、时钟容差……）放在 config.py。
"""
from __future__ import annotations

import os
from pathlib import Path

# ── 路径 ──

HOME = Path.home()

#: Codex 用户级数据目录。遵循 Codex 自身的约定：$CODEX_HOME 优先，否则 ~/.codex。
#: 可用 --codex-home 覆写（测试用）。
CODEX_HOME: Path = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))


# ── 白名单（由 config.apply_manifest() 按生效清单填充）──

SYNC_FILES: list = []
SYNC_DIRS: list = []
#: 需要同步的 SQLite 库（glob + 表白名单 + 版本列），见 config.DEFAULT_MANIFEST
DATABASES: list = []
NEVER_SEGMENTS: set = set()
NEVER_SUFFIX: tuple = ()
SENSITIVE_WORDS: tuple = ()
EXTRA_GLOBS: list = []
MAX_WORKSPACE_FILE_MB: int = 200


# ── 机器标识（由 config.resolve_machine_id() 填充）──

#: 用于构成包文件名，必须稳定且唯一 —— state/from-<HOST>.zip
HOST: str = ""

#: 仅供人读的展示名（默认取主机名）
MACHINE_NAME: str = ""


def snapshot() -> dict:
    """当前状态快照，仅供自检/诊断输出使用。"""
    return {
        "codex_home": str(CODEX_HOME),
        "host": HOST,
        "machine_name": MACHINE_NAME,
        "sync_files": len(SYNC_FILES),
        "sync_dirs": len(SYNC_DIRS),
        "databases": len(DATABASES),
        "never_segments": len(NEVER_SEGMENTS),
        "never_suffix": len(NEVER_SUFFIX),
        "sensitive_words": len(SENSITIVE_WORDS),
        "extra_globs": len(EXTRA_GLOBS),
        "max_file_mb": MAX_WORKSPACE_FILE_MB,
    }
