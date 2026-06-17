# 多会话状态机设计（2026-06-17，v3）

> 「多会话并发计数 + 状态聚合 + 显示策略」的权威规格。改聚合/显示/事件映射前先读本文。
> 单元测试 `plugin/scripts/test_state_machine.py`（230 用例，requirement-driven：先按需求写测试、再实现、再独立验证）。

## 背景

一个摆件、一块屏，但用户常**同时开多个 CLI 会话**（Claude Code / Codex），彼此独立，各自经历 idle / working / waiting / done / error（**没有 thinking**——见规则 1）。屏幕把 N 个会话聚合成「一张脸 + 一个负载背景色 + 一个底部数字」。

数据流：`hook.py`（事件→状态）→ TCP → `daemon.py`（按 `session_id` 聚合 + 算显示策略）→ 串口 → 固件渲染。

## 三层职责

| 层 | 职责 |
|---|---|
| hook.py | 单事件 → 单状态（含 Notification 分类）；每条带 `session_id` + `cwd` |
| daemon.py | 维护每会话最新状态；聚合 `{state,count}`；算 `color/blink/bottom`；写状态快照 |
| 固件 | **纯渲染**：按 `color` 上背景、`blink` 时闪、底部画 `count`+滚动点、按 `state` 画眼睛 |

## 规则 1：事件 → 每会话状态（hook.py）

**没有 thinking 状态**——一切「在动」统一算 `working`。每会话状态只有 `idle / working / waiting / done / error`。

| 事件 | 状态 | CC | Codex | 备注 |
|---|---|:--:|:--:|---|
| SessionStart | idle | ✓ | ✓ | 启动 / `/clear` / compact 后 |
| UserPromptSubmit | working | ✓ | ✓ | （原 thinking，已并入）|
| PreToolUse / PostToolUse | working | ✓ | ✓ | 多对一收敛 |
| PostToolUseFailure | error | ✓ | — | |
| Stop | done | ✓ | ✓ | 瞬态，固件 3s 回 idle |
| PreCompact | working | ✓ | ✓ | compact 进行中 |
| PostCompact | idle | ✓ | ✓ | compact 完成 |
| PermissionRequest | waiting | — | ✓ | Codex 等批准 |
| Notification | 见规则 2 | ✓ | — | 按 notification_type |
| **SubagentStart / SubagentStop** | **忽略** | ✓ | ✓ | 见下「Subagent 与 recap」|
| SessionEnd | （移除会话）| ✓ | — | 退出计数 |

daemon 对收到的 `"thinking"`（老 hook 残留）归一化为 `working`。

> ⚠️ **Codex 没有** `Notification` / `PostToolUseFailure` / `SessionEnd`。所以 Codex 的 idle 自愈较弱（无 idle_prompt），关窗也不立即移除（只能等 TTL）。

### Subagent 与 recap（本设计的关键坑）

`SubagentStart` / `SubagentStop` **一直在监听**，但**故意不映射成任何状态**：

- **对真 subagent 冗余**：真 subagent 用 `Task` 工具起，其 `PreToolUse(Task)`/`PostToolUse(Task)` 已把会话标成 working。忽略 Subagent* 不丢这份 working。
- **避开 recap 假翻转**：Claude Code 的 **away_summary / recap**（用户离开时自动生成的摘要，`/config` 可关）是用一个**内部 subagent** 生成的，完成时发 `SubagentStop`，**前面没有 Task `PreToolUse`**。若把 SubagentStop 映射成 working，就会在用户**空闲**时把一个 idle 会话翻成 working、且之后无 Stop → 永久卡在 working（实测 `7e1b0bcb`/`0e3a1c3e` 都中招）。忽略 Subagent* 从根上消除此问题，并顺带覆盖 ai-title 等其他内部 subagent 后台活动。

## 规则 2：`Notification` 分类（Claude Code）

| notification_type | 映射 | 含义 |
|---|---|---|
| `permission_prompt` | waiting | 等我确认命令/权限 |
| `elicitation_dialog` | waiting | MCP 弹窗要我填内容 |
| `idle_prompt` | idle | 光标闲置 ~60s（兼作打断后自愈）|
| 其余（auth_success / elicitation_complete / response / 未知）| 忽略 | |

无 `notification_type` 时按 `message` 兜底：含 `permission` / 以 `allow ` 开头 / 含 `needs your` → waiting；含 "waiting for your input" → idle；其余忽略。

## 规则 3：计数（daemon）

```
count = 同时处于 {working, error, waiting} 的会话数
```

- **waiting 计入**（暂停等我确认的真任务）；**error 计入**（工具失败是回合暂态，否则计数抖动）；不含 idle / done。
- 固件在 `count >= 1` 显示「数字 + 滚动点」（`1.`/`1..`/`1...`、`2..`…，点数 1/2/3 每 ~600ms 轮换）；`count == 0` 不显。

## 规则 4：脸优先级（daemon）

```
waiting > error > working > done > idle
```

`summarize(sessions)`：空 → `(idle, 0)`；否则取最高优先级为脸、`{working,error,waiting}` 计数。

## 规则 5：显示策略（`daemon.display`，纯函数）

放在 daemon（不在固件）→ 可单测、改阈值/逻辑大多**不用重烧**。daemon 每次推 `{state, count, color, blink, bottom}`。

- `color`：`count==0`→`green`、`==1`→`orange`、`>=2`→`red`。
- `blink`：`state=="waiting"` → 固件背景按负载色闪（~1.2s/次）。**不再有 `?`**。
- `bottom`：`count==0`→`""`，否则 `str(count)`（固件追加滚动点）。

固件配色 RGB：`green tft.color565(0,150,70)` / `orange tft.color565(218,17,0)`（设备原橙）/ `red tft.color565(255,0,0)`。

## 规则 6：生命周期与「卡死计数」清理

回合结束/打断的信号并不总能到达（Esc 打断**不触发任何 hook**；Codex 无 SessionEnd；崩溃）。多层清理：

1. **`SessionEnd`** → 立即移除（Claude Code 正常关窗）。
2. **idle_prompt（~60s）** → idle（仅 Claude Code）。
3. **TTL 600s** 无任何事件 → 剪除（终极兜底）。
4. **陈旧降级（默认关，`CLAWD_MOOD_STALE_SEC` > 0 开启）**：`working`/`error` 静默超过 N 秒 → 当 idle（退出计数），收到新事件立即恢复；`waiting` 豁免。**默认关**是因为它会把"真在跑但长时间不发事件"的会话误降（误闪 idle）；recap 的根因已在规则 1 修掉，平时不需要它。

> 即时归零做不到（平台无打断事件、回合结束 hook 不保证可靠到达）——已知上限。

固件本地两个转移不依赖上游：`done` 进入 3s 回 `idle`；任意状态 5 分钟无串口消息进 `sleeping`，来消息即唤醒（`sleeping` 仅固件态，daemon 不下发）。

## 规则 7：串口热插拔（daemon）

启动无设备→**headless 起**不退出；运行中写失败/设备消失→标记断开、保留 TCP 与会话表；每 `PRUNE_INTERVAL`(5s) 探测，设备回来即重连并强制重推当前状态。

## 规则 8：可观测性（排障用）

- **状态快照** `<tempdir>/clawd-mood-status.log`（每事件 + 每 5s 覆盖写）：时间 + 聚合(face/count/color/blink) + **逐会话**(session id、状态、age、是否计数/陈旧、项目目录)。`./plugin/scripts/watch.sh` 1 秒刷新实时看。
- **逐事件日志** `<tempdir>/clawd-mood-events.log`（追加）：每条 `[时间] sid event=X state=Y cwd` —— 排查"哪个会话发了什么"（如 recap 的 `SubagentStop`）。

## 验证

- **单元**：`./plugin/scripts/test_state_machine.py`（230 用例：事件/通知分类、聚合、优先级、`display`、场景1-3 + 打断自愈 + 终止保留幸存者、陈旧降级、5 会话随机时间序列对照 oracle）。requirement-driven：sonnet 子代理按需求（不看代码）写测试 → 实现 → 子代理独立验证。
- **集成**（手测，需硬件）：多开 CLI 肉眼核对脸/色/数字；拔插数据线核对恢复；晾窗口触发 recap 核对 idle 不被翻成 working。

## 设计取舍

- **显示策略在 daemon、固件纯渲染**：颜色阈值/闪/底部可单测、改了大多不用重烧（只有 RGB 值、点动画、闪节奏在固件）。
- **删 thinking**：状态更少；`UserPromptSubmit` 即 working。
- **忽略 Subagent***：真 subagent 的 working 由 Task 工具事件表达，且封掉 recap 假翻转（根因修复）。
- **不做合并/去重/限流**：表情按事件到达交错驱动，多端并发会快切，是预期行为。
