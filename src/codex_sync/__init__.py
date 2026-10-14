# -*- coding: utf-8 -*-
"""Codex 全资产双向同步。

在多台机器之间，通过一个共享的「同步目录」双向同步 OpenAI Codex 的本地资产：
会话（sessions/**/rollout-*.jsonl）、会话索引（session_index.jsonl）、
元数据库（state_*.sqlite 的 threads / projects …）、技能与配置。

本项目的架构起点是 workbuddy-sync（Python 引擎）：
    · per-machine 包 —— 每台机器只写自己的 state/from-<标识>.zip，绕开网盘冲突副本
    · 行级并集合并 —— 追加型 JSONL 日志不丢行
    · SQLite backup API 快照 —— 一致性快照，规避 WAL 脏读
    · 白名单驱动 —— 同步范围写在 default-manifest.json，改清单不改代码
    · 本地 Web 面板 —— 零依赖可视化操作

Codex 数据层知识借鉴自 codex-session-sync：
    · ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl 日期分层
    · rollout 首行 session_meta 的解析（含超大首行的前缀读取）
    · session_index / state_*.sqlite 三处存储的协同
    · 冷同步（仅 Codex 关闭时写库）与冲突策略（manual_abort / prefer_local / prefer_cloud）

包内结构：
    state.py          运行时可变状态（集中在一处，避免跨模块拿到旧值）
    utils.py          通用工具：时间戳 / 摘要 / 体积格式化
    config.py         常量、清单加载、配置加载、机器标识
    platform_win.py   Windows 平台相关：Codex 进程检测
    codex.py          Codex 数据层：目录扫描 / JSONL 解析 / SQLite 元数据合并
    core.py           同步内核：资产收集 / 打包 / 合并远端 / 状态与自检
    cli.py            命令行入口

对外接口稳定在 `cli.main()`；模块内部结构可能随版本调整。
"""

__all__ = ["__version__", "ENGINE_VERSION", "MANIFEST_SCHEMA", "main"]

__version__ = "0.1.0"

# 引擎版本：写入包内 manifest.json，pull 时用来判断两端是否同版本。
ENGINE_VERSION = __version__

# 包内 manifest.json 的 schema 版本。结构不兼容变更时递增。
MANIFEST_SCHEMA = 1


def main(argv=None) -> int:
    """延迟导入，避免 `import codex_sync`（只为读版本号）时拉起重依赖。"""
    from .cli import main as _main

    return _main(argv)
