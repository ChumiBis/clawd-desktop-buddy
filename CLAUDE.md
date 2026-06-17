# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概要

`clawd-mood` 是一个 ESP32-C3 + ST7789 1.54" TFT 桌面摆件，通过 USB 串口接收 **Claude Code / OpenAI Codex CLI** 的运行状态，实时切换像素眼睛表情（`idle / working / waiting / done / error`，外加固件本地的 `sleeping`；**无 thinking**，已并入 working）+ 负载背景色 + 并发计数。

**跨平台桌面摆件（macOS / Linux / Windows）**。明确不做：WiFi 控制、OTA、launchd/systemd 系统级自启、自动化单元测试、蜂鸣器、多设备。Windows 端代码完成但 **untested on Windows** —— 欢迎社区验证（见 README）。Linux 同样未在 CI 验证，但 POSIX 路径与 macOS 共用，回归风险低。

## 三段式架构

整个数据流单向，**Mac → ESP32**：

```
Claude Code event ─┐
                   │
Codex CLI event ───┤
                   ↓
plugin/scripts/hook.py        (uv run; 标准库 only; 事件名 → state JSON)
       ↓
TCP 127.0.0.1:<port>          (portfile: <tempdir>/clawd-mood.port)
       ↓
plugin/scripts/daemon.py      (uv 启动；常驻；TCP server 写串口)
       ↓ USB CDC 115200 baud, NDJSON
firmware/clawd_mood/*.ino     (Arduino 状态机 + 像素眼渲染)
```

三个组件**互相独立**，可单独测试：
- 不启动 daemon 时，hook.py 读 portfile 拿不到端口（或连不上）直接静默退出（不阻塞 Claude Code / Codex）
- 不启动 CLI 时，可直接 `printf '{"state":"working"}\n' | nc 127.0.0.1 $(cat /tmp/clawd-mood.port)` 测 daemon + 固件（Windows 见下文"手动测试"）
- 不接 ESP32 时，daemon 找不到 USB CDC 设备直接退出
- 不挂 Claude Code 时，可只挂 Codex（`codex plugin add clawd-mood@clawd-mood`），或反之；两端可独立、可同装

## 关键约束（容易踩坑）

### 1. 显示器 VCC 接 3.3V，**不要接 5V**
ST7789 的 VCC 接到 ESP32-C3 的 3V3 引脚。接 5V 会烧屏。README 烧录段已写明，改文档时不要弄错。

### 2. `USB CDC On Boot` 必须 Enabled
板子在 Arduino IDE 配置里必须开 USB CDC On Boot。关掉了 daemon 看不到串口、屏幕也不会更新。`-b esp32:esp32:esp32c3:CDCOnBoot=cdc,...` 这串参数里的 `CDCOnBoot=cdc` 就是这意思。

### 3. SPI 引脚需要手动 remap
ESP32-C3 用 `SPI.begin(8, -1, 10, TFT_CS)` 显式指定 SCK=8 / MOSI=10，不要用默认值。

### 4. 屏幕方向
`tft.setRotation(1)` 是当前用户面板的安装方向。`rotation` 改 0/2/3 都是合法值，但**只在用户报告方向不对时**才改。参考 commit `e8f6e6e`（之前从 2 改到 1）。

### 5. 多 USB CDC 设备
连了多台 ESP32 时，daemon 默认拿排序后的第一个，扫描规则跨平台：
- macOS: `/dev/cu.usbmodem*`
- Linux: `/dev/ttyACM*`（ESP32-C3 原生 CDC）或 `/dev/ttyUSB*`（CH340/CP2102 桥）
- Windows: `COM*`（仅当 USB VID 不为空，过滤蓝牙 / 调制解调器虚拟口）

指定其他端口用环境变量：`CLAWD_MOOD_PORT=/dev/cu.usbmodemXXX`（mac/linux）或 `CLAWD_MOOD_PORT=COM7`（Windows）。

### 6. 插件级 daemon 自启 ≠ 系统级自启

`hook.py` 在 `SessionStart` 事件里会先 `probe_daemon()` 探活，没活就用 `subprocess.Popen` 拉脱离的后台进程：
- mac/linux: `start_new_session=True`
- Windows: `creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW`

这是**插件级 / 会话触发**的自启：只在 CLI 第一次启动时拉起 daemon，不依赖系统服务。"明确不做：launchd/systemd 系统级自启"指的仍是写 LaunchAgents / `.service` 让 daemon 跟随开机/登录启动 —— 两者不冲突。

实现上的取舍：
- **Singleton 用 portfile + connect() 探活而非 PID 文件**：避免脏 PID、避免 `pgrep -f` 在 Windows 上不存在的问题。代价是 daemon 异常崩了 portfile 可能残留，但下次启动会自动覆盖（`check_singleton()` 探不到活就 take over）。
- **首条 SessionStart 状态消息会丢**：daemon 冷启动约 2–3 秒（uv 首次装 pyserial），此时 portfile 还没出现，`hook.py` 走 `port is None` 路径静默退出。下一条 `UserPromptSubmit` 会把状态补推上去，所以表情滞后但不会卡。
- **daemon 永驻**：不在 `Stop` 事件里关 daemon，避免多个 CLI 会话相互踩踏。需要重启 daemon 就 `pkill -f scripts/daemon.py`（mac/linux）；Windows 上更稳的做法是按 PID 杀（避免 `taskkill /F /IM python.exe` 误杀其他 python 进程）。
- **日志位置**：`<tempfile.gettempdir()>/clawd-mood-daemon.log` —— mac/linux 上 `/tmp/clawd-mood-daemon.log`，Windows 上 `%TEMP%\clawd-mood-daemon.log`。

### 7. portfile 服务发现 + 端口自适应

daemon 启动时尝试 bind 默认端口 `48756`，被占则回退到 `bind(0)` 让 OS 分配空闲端口。实际监听端口写入 portfile：`<tempfile.gettempdir()>/clawd-mood.port`（mac/linux: `/tmp/clawd-mood.port`，Windows: `%TEMP%\clawd-mood.port`）。

hook 端不直接读 `48756`，而是读 portfile 拿实际端口。这样：
- 默认端口冲突时自动避让，零配置
- hook 不需要管 daemon 是不是 singleton（read portfile → connect → 通了就发，连不上就静默退）
- 没有 pgrep / tasklist 依赖，三平台一致

显式覆盖：`CLAWD_MOOD_PORT_TCP=N` 强制 daemon 用 N。此时冲突直接报错退出（既然显式指定，应让用户知晓冲突）。

## 状态机设计原则

**5 个会话状态**：`idle | working | waiting | done | error`（小写严格匹配，固件 `parseMood` 用 `strcmp` 大小写敏感）。**没有 thinking**——并入 `working`（daemon 把收到的 `thinking` 归一化成 `working`）。`sleeping` 是固件本地态，daemon 从不下发。

**固件本地两个转移**（不靠上游）：
- `done` 进入后 3 秒（`DONE_REVERT_MS`）自动回 `idle`（done 是瞬态）
- 任何状态 5 分钟（`SLEEP_IDLE_MS`）无串口消息进 `sleeping`，任何消息立即唤醒

**事件→状态收敛**：`PreToolUse`/`PostToolUse` → working；`UserPromptSubmit` → working（原 thinking）；`PostToolUseFailure` → error（CC 独有）；`Stop` → done；`PreCompact` → working、`PostCompact` → idle（两端注册；手动 `/compact` 后还有 `SessionStart(compact)`→idle）；`PermissionRequest` → waiting（Codex 独有，对标 CC 的 `Notification`）。

**`SubagentStart`/`SubagentStop` 故意不映射（忽略）**：真 subagent 用 `Task` 工具起，其 `Pre`/`PostToolUse` 已表达 working；而 **recap/away_summary**（用户离开时自动生成的摘要）是**内部 subagent**，完成发 `SubagentStop` 但前面**无 Task PreToolUse**——若映射成 working 会把**空闲会话**翻成 working 且无后续 Stop 而卡死（实测真凶）。忽略它从根上修掉，顺带覆盖 ai-title 等内部 subagent。**改 `EVENT_TO_STATE` 时别把 Subagent\* 加回去。**

**`Notification`（仅 CC）按 `notification_type` 分类**：`permission_prompt` / `elicitation_dialog` → `waiting`（等我确认）；`idle_prompt`（闲置 ~60s）→ `idle`（兼作打断后自愈）；其余忽略。无 `notification_type` 时按 message 文本兜底（见 `classify_notification`）。

**打断（Esc）无 hook**：按 Esc 不触发任何 hook，被打断会话卡在 `working`；靠 `idle_prompt`（~60s，仅 CC）/ TTL（600s）自愈，无法即时归零（平台限制）。

⚠️ **Codex 没有** `Notification` / `PostToolUseFailure` / `SessionEnd`：idle 自愈弱、关窗不立即移除（等 TTL）。

**多会话聚合**（daemon 按 `session_id` 维护每会话最新态，聚合成 `{state,count,color,blink,bottom}` 推串口）：
- `count` = 同时 `{working, error, waiting}` 的会话数。`waiting` **计入**（等确认是真任务）；`error` 计入（避免工具失败抖动）；不计 idle/done。固件 `count>=1` 显示「数字+滚动点」（`1.`/`1..`/`1...`、`2..`，点 1/2/3 每 ~600ms 轮换）。
- 脸 = 最高优先级：`waiting > error > working > done > idle`。
- 显示策略（`daemon.display` 纯函数，可测、改阈值不重烧）：`color` 0绿/1橙/≥2红；`blink` 仅 `waiting`（背景按负载色闪，无 `?`）；`bottom` count 字符串。固件配色 `green(0,150,70)` / `orange(218,17,0)` / `red(255,0,0)`。
- 清理：`SessionEnd` 立即移除；idle_prompt(60s，CC)；TTL 600s 兜底；**陈旧降级**（`working`/`error` 静默超 `CLAWD_MOOD_STALE_SEC` 秒当 idle、收到事件即恢复、`waiting` 豁免）**默认关**——会误降长静默真任务，且 recap 根因已修、平时不需要。
- 排障：状态快照 `<tempdir>/clawd-mood-status.log`（逐会话 + 聚合，`./plugin/scripts/watch.sh` 1 秒刷新）+ 逐事件日志 `<tempdir>/clawd-mood-events.log`。
- 完整规格见 `docs/superpowers/specs/2026-06-17-multi-session-state-machine-design.md`，纯逻辑单测 `plugin/scripts/test_state_machine.py`（230 用例，`uv run` 直接跑）。

## 双 CLI 并发

两端可同时挂载、同时运行，**无冲突**：

- TCP server 接受多客户端：两个 CLI 的 hook 进程各自短连接写 `127.0.0.1:<port>`，daemon `accept()` 循环串行消费
- daemon singleton 不变：`SessionStart` 拉 daemon 时 hook 走 `probe_daemon()`（portfile + 试连）防重复
- 串口由 daemon 独占：两个 CLI 共享同一个 daemon → 共享同一个串口
- daemon 按 `session_id` 聚合：维护每会话最新状态，按优先级选一张脸 + `{working,error,waiting}` 计数（见上「多会话聚合」与 spec）。多端长任务并发时脸仍会随事件到达切换，是预期行为

如果觉得"鬼畜"，停掉其中一端的会话即可（不需要卸载插件）。

## 串口协议

- 方向：**单向** Mac → ESP32，ESP32 不回包
- 格式：NDJSON（换行分隔），UTF-8，115200 baud
- 必需字段：`state`
- 可选调试字段：`event`、`tool`
- 未知 `state` 或坏 JSON：固件打 `[warn]` 到串口，不切换、不死

示例：`{"state":"working","event":"PreToolUse","tool":"Bash"}`

## 常用命令

### 烧固件

```bash
# 编译 + 上传（端口按平台改）
arduino-cli compile -b esp32:esp32:esp32c3:CDCOnBoot=cdc,CPUFreq=160,UploadSpeed=921600 firmware/clawd_mood

# macOS
arduino-cli upload -p /dev/cu.usbmodem101 -b esp32:esp32:esp32c3:CDCOnBoot=cdc,CPUFreq=160,UploadSpeed=921600 firmware/clawd_mood

# Linux (可能需先 sudo usermod -aG dialout $USER 后重登)
arduino-cli upload -p /dev/ttyACM0 -b esp32:esp32:esp32c3:CDCOnBoot=cdc,CPUFreq=160,UploadSpeed=921600 firmware/clawd_mood

# Windows (查设备管理器拿 COM 号)
arduino-cli upload -p COM3 -b esp32:esp32:esp32c3:CDCOnBoot=cdc,CPUFreq=160,UploadSpeed=921600 firmware/clawd_mood
```

板子检测：
- macOS: `ls /dev/cu.usbmodem*`
- Linux: `ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null`
- Windows PowerShell: `Get-CimInstance Win32_SerialPort | Select Name,DeviceID`

首次环境（README §1-3）：
- macOS: `brew install arduino-cli uv`（不再需要 jq）
- Linux: 系统包管理器装 `arduino-cli` + `uv`，加入 `dialout` 组
- Windows: `winget install ArduinoSA.CLI astral-sh.uv`

全平台都要装 ESP32 核心 + Adafruit GFX / ST7789 / ArduinoJson 库。

### 跑 daemon

```bash
./plugin/scripts/daemon.py

# 指定串口（mac/linux 给路径，Windows 给 COM 名）
CLAWD_MOOD_PORT=/dev/cu.usbmodem101 ./plugin/scripts/daemon.py
CLAWD_MOOD_PORT=COM3 uv run plugin/scripts/daemon.py   # Windows PowerShell: $env:CLAWD_MOOD_PORT='COM3'

# 指定 TCP 端口（默认 48756，被占则自动回退；显式指定时冲突会直接报错退出）
CLAWD_MOOD_PORT_TCP=49000 ./plugin/scripts/daemon.py

# 日志不缓冲
PYTHONUNBUFFERED=1 ./plugin/scripts/daemon.py
```

`daemon.py` 头部是 PEP 723 内联依赖，`uv run` 自动建临时 venv 装 pyserial。不需要手动 `pip install`。

### 挂载插件

**Claude Code**：

```bash
claude --plugin-dir /absolute/path/to/clawd-mood/plugin
```

在 Claude Code 里 `/hooks` 确认 12 个事件都挂上（SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / PostToolUseFailure / Notification / Stop / SubagentStart / SubagentStop / PreCompact / PostCompact / SessionEnd）。

**Codex CLI**（≥ 0.133.0）：

```bash
cd /absolute/path/to/clawd-mood
codex plugin marketplace add .          # 注册本地 marketplace（读 .agents/plugins/marketplace.json）
codex plugin add clawd-mood@clawd-mood  # 装插件（拷贝到 ~/.codex/plugins/cache/）
```

任意目录 `codex` 就能用。在 codex 里 `/hooks` 确认 10 个事件都挂上（含 `PermissionRequest` / `PreCompact` / `PostCompact`）。卸载：`codex plugin remove clawd-mood@clawd-mood`。

**Codex 改 plugin 后必须重装**：codex 是把 plugin 内容**拷贝**到 `~/.codex/plugins/cache/clawd-mood/clawd-mood/<version>/`，不是 symlink。修改了源仓库的 `plugin/scripts/hook.py` / `hooks/hooks-codex.json` 后，codex 仍跑 cache 里的旧版本。开发时改完执行：

```bash
codex plugin remove clawd-mood@clawd-mood
codex plugin add clawd-mood@clawd-mood
```

Claude Code 走 `--plugin-dir` 是 live path，无此问题。

### 手动测试（不依赖 Claude Code）

```bash
# 灌 TCP 走完整管道（daemon → 串口 → 屏幕）
# mac/linux:
printf '{"state":"working"}\n' | nc 127.0.0.1 $(cat /tmp/clawd-mood.port)
# Windows PowerShell:
#   $p = Get-Content $env:TEMP\clawd-mood.port
#   $c = New-Object Net.Sockets.TcpClient('127.0.0.1', $p)
#   $s = $c.GetStream(); $b = [Text.Encoding]::UTF8.GetBytes('{"state":"working"}'+"`n")
#   $s.Write($b,0,$b.Length); $c.Close()

# 直接喂 hook.py（模拟 CLI 事件，三平台一致）
echo '{"hook_event_name":"PreToolUse","tool_name":"Bash"}' | ./plugin/scripts/hook.py

# Arduino 串口监视器手输（绕过 daemon）
# 在 IDE 监视器里输：{"state":"idle"} 等
```

JSON 校验：`python3 -m json.tool plugin/hooks/hooks.json`

## 测试策略

**状态机逻辑有单测，硬件/IO 靠手测**：

- **状态机纯逻辑**：`uv run plugin/scripts/test_state_machine.py`（230 用例，无硬件依赖，覆盖事件/通知分类、聚合、优先级、`display`、各场景、陈旧降级、随机 oracle）。requirement-driven：按需求写测试 → 实现 → 独立验证。
- **固件单独**：Arduino 串口监视器手输各状态 JSON，肉眼确认表情（`idle/working/waiting/done/error`）
- **daemon + 固件**：`printf '{"state":"working"}\n' | nc 127.0.0.1 $(cat /tmp/clawd-mood.port)`（mac/linux），Windows PowerShell 见上"手动测试"段
- **端到端**：跑真实任务，观察 idle → working → done → idle；多开会话核对计数/颜色；用 `./plugin/scripts/watch.sh` 看逐会话快照
- **Done 3 秒回 Idle、Sleeping 5 分钟进入**：临时把 `SLEEP_IDLE_MS` 改成 `15000UL` 缩短验证
- **Linux / Windows**：未在本地端到端验证；代码层 review 确认无 macOS-isms。社区验证 welcome

完整测试清单见 `docs/superpowers/plans/2026-06-07-clawd-mood-implementation.md` Task 13 / 17。

## 仓库结构要点

- `firmware/clawd_mood/clawd_mood.ino` —— 单文件 Arduino sketch；7 个 `drawXxx()` 函数 + `tickMoodMachine()` 计时器 + `pollSerial()` 拼行 + `handleLine()` JSON 解析
- `plugin/.claude-plugin/plugin.json` —— Claude Code 插件清单
- `plugin/.codex-plugin/plugin.json` —— Codex CLI 插件清单（指向 `hooks-codex.json`）
- `plugin/hooks/hooks.json` —— Claude Code 12 个事件（含 `PreCompact` / `PostCompact` / `SessionEnd`）全 `async: true`，命令统一指 `uv run "$CLAUDE_PLUGIN_ROOT"/scripts/hook.py`
- `plugin/hooks/hooks-codex.json` —— Codex 10 个事件（含 PermissionRequest / PreCompact / PostCompact），同一个 hook.py
- `plugin/scripts/hook.py` —— event → state 映射 + `Notification` 分类 + `session_id`/`cwd` 透传（Subagent\* 不映射）；TCP 短连接写 daemon（PEP 723，标准库 only，共享给两端）
- `plugin/scripts/daemon.py` —— PEP 723 内联依赖，TCP server ↔ 串口桥；portfile 服务发现；多会话聚合 + `display` 显示策略 + 陈旧降级（默认关）+ 串口热插拔 + 状态快照/逐事件日志
- `plugin/scripts/test_state_machine.py` —— 状态机纯逻辑单测（230 用例：hook 分类 + daemon 聚合/display/陈旧降级，`uv run` 直接跑，无硬件依赖）
- `plugin/scripts/watch.sh` —— 1 秒刷新看 daemon 状态快照（聚合 + 逐会话明细），排障用
- `.agents/plugins/marketplace.json` —— Codex 本地 marketplace 入口
- `AGENTS.md` —— Codex 项目守则，指回本文件
- `docs/superpowers/specs/2026-06-07-clawd-mood-design.md` —— 原始设计说明书（**改架构前先读这个**）
- `docs/superpowers/specs/2026-06-07-codex-support-design.md` —— Codex 支持设计说明书
- `docs/superpowers/specs/2026-06-08-windows-linux-support-design.md` —— Windows + Linux 跨平台设计说明书
- `docs/superpowers/specs/2026-06-17-multi-session-state-machine-design.md` —— 多会话状态机规格（聚合/计数/`?` 语义/优先级，**改聚合规则前先读**）
- `docs/superpowers/plans/2026-06-07-clawd-mood-implementation.md` —— 原始 18 任务实施计划
- `docs/superpowers/plans/2026-06-07-codex-support-implementation.md` —— Codex 支持 8 任务实施计划
- `docs/superpowers/plans/2026-06-08-windows-linux-support-implementation.md` —— 跨平台实施计划

修改设计时同步更新 `docs/superpowers/specs/` 下的文档。
