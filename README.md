# codex-sync

在多台机器之间，通过**任意共享目录**双向同步 [OpenAI Codex](https://github.com/openai/codex) 的本地资产 ——
会话（`sessions/**/rollout-*.jsonl`）、会话索引（`session_index.jsonl`）、
元数据库（`state_*.sqlite` / `memories_*.sqlite` / `goals_*.sqlite`）、技能与配置。

不需要 WebDAV，不需要 VPN，也不依赖 GitHub：同步目录可以是 OneDrive / 坚果云 / 百度网盘 /
U 盘 / 局域网共享等任意「双向同步型」目录。

> 本项目的架构起点是 **[workbuddy-sync](https://github.com/kid941005/workbuddy-sync)**（Python 引擎），
> Codex 数据层知识借鉴自 **[codex-session-sync](https://github.com/shonngithub/codex-session-sync)**。

---

## 为什么需要它

Codex（CLI / Desktop / IDE 插件）把全部会话状态存在本地 `~/.codex`。多台电脑工作时，
会话不会跟着你走。市面上已有的 `codex-session-sync` 走 WebDAV，需要额外搭服务器；
本项目复用 workbuddy-sync 已经验证过的「共享目录 + per-machine 包」路线，
用一套纯 Python、零第三方依赖的引擎解决同一问题。

## 三条设计主线

| 机制 | 从哪来 | 解决什么问题 |
|------|--------|--------------|
| **per-machine 包** | workbuddy-sync | 每台机器只写 `codex/state/from-<标识>.zip`，从根上避开网盘冲突副本 |
| **行级并集合并** | workbuddy-sync | 追加型 JSONL 日志两边各自往后写，整文件覆盖必然丢行 → 按行并集 |
| **SQLite backup API 快照** | workbuddy-sync | 直接 copy 会拷到 WAL 未合并的脏状态 → 用 backup API 取一致性快照 |
| **白名单驱动** | workbuddy-sync | 同步范围写在 JSON 清单里，改清单不用改代码 |
| **本地 Web 面板** | workbuddy-sync | 零依赖浏览器界面，可视化操作 |
| **目录结构知识** | codex-session-sync | `sessions/YYYY/MM/DD/rollout-*.jsonl` 日期分层与三处存储协同 |
| **JSONL 解析逻辑** | codex-session-sync | rollout 首行 `session_meta`（可能数百 KB）的前缀读取与正则兜底 |
| **冲突策略设计** | codex-session-sync | `manual_abort` / `prefer_local` / `prefer_cloud` 语义 |
| **冷同步理念** | codex-session-sync | 仅在 Codex 关闭时写库，杜绝写入中途损坏状态文件 |
| **后台常驻守护** | 本项目新增 | 定时 push + 自动抢冷窗口 pull；开机自启、静默无窗口 |

## Codex 的存储结构（本工具触及的部分）

| 存储 | 用途 | 本工具的合并策略 |
|------|------|------------------|
| `sessions/YYYY/MM/DD/rollout-*.jsonl` | 会话内容本体（JSONL，首行 `session_meta`） | 行级并集 |
| `session_index.jsonl` | `codex resume` 使用的索引 | 按 `id` 去重，取 `updated_at` 较新者 |
| `state_*.sqlite` → `threads` 等表 | 会话列表权威来源（标题、登录方式、归档） | 快照 + 按主键合并，取版本列较新者 |
| `memories_*.sqlite` / `goals_*.sqlite` | 记忆 / 目标 | 同上 |
| `config.toml` / `skills/**` | 配置与技能 | 按 mtime 合并，两边都改过则标记冲突 |

## 环境要求

- Python **≥ 3.9**（标准库实现，无第三方依赖）
- 已安装 Codex（存在 `~/.codex` 目录，或设置了 `CODEX_HOME`）
- Windows 实测最充分；进程检测与自启在 macOS / Linux 上需另行适配

## 安装与使用

```bash
# 免安装直接跑（源码 / 便携模式）
python cx_sync.py doctor
python cx_sync.py status
python cx_sync.py push
python cx_sync.py pull
python cx_sync.py ui

# 或 pip 安装后使用控制台命令
pip install .
cx-sync doctor
```

### 快速开始

```bash
# 1. 自检环境（先跑这个）
python cx_sync.py doctor

# 2. 指定同步目录（任意云盘 / U盘 / 局域网共享）
#    编辑 %LOCALAPPDATA%\codex-sync\config.json：
#    { "sync_root": "D:\\Cloud\\codex-sync" }

# 3. 机器 A：关闭 Codex → 上传
python cx_sync.py push

# 4. 等同步盘同步完成

# 5. 机器 B：关闭 Codex → 拉取
python cx_sync.py pull
```

日常使用不必每次手动敲 —— 装一次开机自启，守护进程会在后台自动完成，
详见下面的 [后台常驻守护](#后台常驻守护推荐用法)。

## 后台常驻守护（推荐用法）

手动 push / pull 只能解决「两台机器偶尔同步」。日常真正省事的做法是让一台常驻守护
在后台自己同步 —— 这也是本项目相对 workbuddy-sync 多出来的一层。

### 为什么是常驻进程，而不是计划任务

workbuddy-sync 用的是「登录时 pull 一次 + 计划任务定时 push」。Codex 这边多了一条硬约束：
**pull 必须写在 Codex 未运行时**。定时任务无法回答「此刻 Codex 关了没有」这个问题，
常驻进程可以 —— 它每轮都探一次进程，Codex 一关就立刻抢那个冷窗口把远端拉下来。

### 三条节奏

| 时机 | 动作 | 为什么安全 |
|------|------|------------|
| 登录后立刻（默认延迟 20s 等云盘挂载） | **pull** | Codex 不是开机自启项，此刻它一定是关着的 —— 最可靠的一次冷窗口 |
| 每 5 分钟（`pull_interval`） | 探一次冷窗口 | 仅当 `Codex 未运行` **且** `远端有未拉取的包` 时才真正执行 |
| 每 15 分钟（`push_interval`） | **push** | 只读快照，Codex 开着也照跑 |

Codex 一直开着时会持续记录 `pull 推迟：Codex 正在运行…`，等它一关就自动补上。

### 一键安装

```bash
# 双击 install-autostart.cmd 也可以
python create-autostart.py
```

它做三件事：把指向 `cx-daemon.vbs` 的快捷方式放进当前用户的启动文件夹、打印守护会用的
同步目录、立刻静默拉起一次守护。**不需要管理员权限**（这也是刻意避开计划任务的原因之一）。

### 日常命令

```bash
python cx_sync.py daemon --status     # 是否在跑 + 最近日志
python cx_sync.py daemon --stop       # 请求退出（几秒内自行收尾）
python cx_sync.py daemon              # 前台运行，可见输出（调试用）
python cx_sync.py daemon --once       # 只跑一轮
python create-autostart.py --remove   # 卸掉自启并停掉守护

python daemon.cmd          # 前台调试运行
python daemon-status.cmd   # 状态 + 日志尾部（不用记命令）
```

### 日志与可调参数

日志：`%LOCALAPPDATA%\codex-sync\daemon.log`（超过 `max_log_kb` 自动轮转一份 `.1`）。

在 `config.json` 里加一段即可调节奏：

```json
{
  "daemon": {
    "push_interval": 900,
    "pull_interval": 300,
    "start_delay": 20,
    "pull_on_start": true,
    "autopull": true,
    "max_log_kb": 1024
  }
}
```

想让守护**只推不拉**，把 `autopull` 设为 `false` 即可。

### 实现上的两个坑（已处理，值得知道）

1. **静默启动必须用 `python.exe` + 隐藏窗口，不能用 `pythonw.exe`。**
   部分 Windows 环境下 GUI 子系统的进程行为异常且极难排查，因此
   [`cx-daemon.vbs`](./cx-daemon.vbs) 一律拉起 `python.exe`，再用
   `WshShell.Run(..., 0, False)` 把控制台窗口隐藏掉（`0` = SW_HIDE）。
2. **`Codex` 的子串判定会误伤常驻服务。** `codex-windows-sandbox-service.exe`
   随系统常驻，若按 "codex" 子串判定「Codex 正在运行」，`pull` 会被永久拒绝。
   因此进程名里含 `service` 的一律忽略（见 `codex_process_ignore`）。

## CLI 命令参考

```
cx-sync doctor                环境自检
cx-sync where                 查看生效配置与清单来源
cx-sync status                查看两侧差异（只读）
cx-sync sessions [--project]  列出本机会话
cx-sync push [--dry-run]      本机 -> 同步目录
cx-sync pull [--force]        同步目录 -> 本机
cx-sync ui [--port 8766]      启动本地 Web 面板
cx-sync daemon                后台常驻：定时 push + 冷窗口 pull
cx-sync daemon --once         只跑一轮
cx-sync daemon --status       守护状态 + 最近日志
cx-sync daemon --stop         请求守护退出
cx-sync daemon --no-pull      只推不拉
cx-sync sessions --selection '{"sessions":["<id>"]}'   选择性上传（供面板使用）
```

全局参数：`--sync-root`、`--codex-home`、`--manifest-dir`、`--name`、`--dry-run`、`--force`；
守护专用：`--interval`、`--pull-interval`、`--duration`、`--quiet`。

## ⚠️ 三条铁律

1. **push 可以在 Codex 运行时执行**（只是只读快照）。
2. **pull 会写 `state_*.sqlite`，执行前必须完全退出 Codex**；确认风险可加 `--force`。
   —— 守护进程会自动判断，只在 Codex 未运行时 pull。
3. **同一时间只让一台电脑 push**。

## 同步范围与安全

同步范围由 [`default-manifest.json`](./default-manifest.json) 驱动。默认同步：

- **文件**：`config.toml`、`session_index.jsonl`、`model-catalog.json`、`.codex-global-state.json` 等
- **目录**：`sessions`、`skills`、`prompts`、`themes`、`pets`
- **数据库**：`state_*.sqlite`、`memories_*.sqlite`、`goals_*.sqlite`（仅白名单表）

默认**不同步**（`never_segments` / `sensitive_words`）：

- 运行态/缓存：`.sandbox*`、`.tmp`、`tmp`、`logs`、`browser`、`cache`、`plugins`、`sqlite`
- 系统内置技能：`skills/.system`
- 登录态与凭证：`auth.json`、任何含 `token` / `cookie` / `credential` / `apikey` 的路径

想让某个目录一起同步，编辑 `default-manifest.json` 的 `sync_dirs` 即可 —— **改清单不用改代码**。

### 冲突处理

- 追加型文件（rollout / index）：**内容合并**，不丢行，原文件备份为 `.bak-<时间戳>`
- 普通文件：按 mtime；若两边都改过（本机 mtime 晚于对方快照时间），
  取对方版本并把本机版本另存为 `.conflict-<标识>-<时间戳>`（对应 `manual_abort` 语义）
- 数据库行：同主键取版本列（`updated_at_ms` / `generated_at`）较新者

## 自测

```bash
python tools/selftest.py         # 引擎端到端（14 项）
python tools/daemon_selftest.py  # 守护进程专项（20 项）
```

`selftest.py` 模拟两台机器 A / B 经共享目录往返同步，覆盖 per-machine 包、行级并集、
索引去重合并、SQLite 快照与行合并、白名单排除。

`daemon_selftest.py` 用子进程真实调用守护进程，覆盖单轮 push、单实例锁、
冷窗口 pull、Codex 运行时的 pull 推迟、`--status` / `--stop` 控制接口。

## 项目结构

```
cx_sync.py                 源码 / 便携模式入口垫片
cx_daemon.py               守护进程入口垫片
cx-daemon.vbs              静默启动器（python.exe + 隐藏窗口）
install-autostart.cmd      安装开机自启（双击）
uninstall-autostart.cmd    卸载开机自启（双击）
daemon.cmd                 前台运行守护（调试）
daemon-status.cmd          状态 + 日志尾部
create-autostart.py        自启安装/卸载逻辑
create-shortcut.py         Windows 快捷方式创建（ctypes）
default-manifest.json      同步范围清单（可直接编辑）
config.example.json        配置示例
src/codex_sync/
  __init__.py              版本号与入口
  state.py                 运行时可变状态（集中管理，避免跨模块拿到旧值）
  utils.py                 时间戳 / 摘要 / 体积格式化
  config.py                常量、清单加载、配置加载、机器标识
  platform_win.py          Codex 进程检测（冷同步前提）
  codex.py                 Codex 数据层：目录扫描 / JSONL 解析 / SQLite 元数据合并
  core.py                  同步内核：资产收集 / 打包 / 合并远端 / 状态与自检
  daemon.py                常驻守护：单实例锁 / 日志轮转 / 三条节奏
  cli.py                   命令行入口
  ui.py                    本地 Web 面板
tools/selftest.py          引擎端到端自测
tools/daemon_selftest.py   守护进程专项自测
```

## License

MIT
