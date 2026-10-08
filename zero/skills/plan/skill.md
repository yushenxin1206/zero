---
name: plan
description: >
  复杂任务的规划-评审-执行-跟踪工作流：计划状态落盘在会话数据目录的
  plan.json（人类视图 plan.md），批准前不执行实施动作，执行偏差须回到评审
  门禁重新确认，进度随时可查。
  当任务复杂（多步骤、跨文件、需求模糊、影响面大、不易回退），或用户要求
  先规划/先出方案再动手时触发。
  关键词：先做个计划、先规划、先出方案、方案评审、按计划执行、调整计划、
  任务进度、进展如何、继续上次的任务
---

# Plan（复杂任务的规划 · 评审 · 执行 · 跟踪）

## 数据布局（Data layout）

活跃计划存放在 **会话数据目录** 下，该目录以环境块 `Session dir` 行为准——
即本会话 `messages.jsonl` / `compact.json` / `summary.json` 所在目录。它是
**会话数据的存储位置，不是工作目录**；不要用今天的日期自行拼接路径（会话
创建日期与你可见的日期可能不同）。

- `plan.json` —— **源真相**：`PlanState` 的序列化（camelCase）。
- `plan.md` —— **人类可读视图**，由脚本从 `plan.json` 渲染生成。

每个会话同一时刻至多一个活跃计划。新复杂任务到来而旧计划未结案
（reviewing / approved / executing）时，先问用户：结案旧计划 / 归档 /
并入旧计划，不要静默覆盖。

## 唯一写入口：内置脚本（Script）

`plan.json` / `plan.md` **不手工编辑**，一切读写都经本 skill 目录下的脚本
`scripts/plan.py`。它由 shell 调用，作为包内可信脚本免审批。

- 用**绝对路径**调用：`python <本 skill 目录>/scripts/plan.py ...`
  （`<本 skill 目录>` 取 `<available_skills>` 中本 skill 的 location 所在
  目录）。shell_guard 只放行包内 `skills/*/scripts/*.py` 的绝对路径调用，
  相对路径不放行。
- **裸调，不要加任何命令前缀**：不要写成 `set PYTHONIOENCODING=utf-8 && python ...`
  或 `call python ...`——豁免要求命令的**首 token 就是 python 解释器**，加了
  `set` / `chcp` / `call` 等前缀后首 token 变成别的程序，豁免失效会触发人工
  审批。编码问题已在脚本内用 `setup_stdio()` 解决（强制 UTF-8 输出 + 控制台
  代码页 65001），无需外部设置。
- 每个子命令都会校验枚举 → 刷新 `updated_at` → 原子写 `plan.json` → 重渲染
  `plan.md`，并向 stdout 输出一行状态摘要；出错写 stderr 且退出码非 0。

| 子命令 | 作用 |
|--------|------|
| `init --dir D --task T [--goal G] [--findings F] [--force]` | 创建计划（status=draft）；已存在时须显式 `--force` |
| `set --dir D [--task][--goal][--findings][--status S][--note N]` | 更新标量字段 / 整体状态 |
| `add-step --dir D --text TXT` | 追加一个步骤（index 自增） |
| `step --dir D --index N [--status S][--text TXT][--note N]` | 更新某步骤（S ∈ pending/in_progress/done/blocked/skipped） |
| `log --dir D --note N` | 追加一条变更记录 |
| `show --dir D` | 打印当前计划视图 |
| `archive --dir D` | 归档活跃计划为 `plan-archive-<ts>.{json,md}` |

`--dir` 一律传环境块 `Session dir` 的绝对路径。

## 状态模型（State）

整体状态机（`PlanState.status`）：

    draft → reviewing → approved → executing → done
                                    ↘ aborted（任意阶段可放弃，记录原因）

步骤状态（`PlanStep.status`）：`pending` → `in_progress` → `done`；
`blocked` / `skipped` 为旁路终态，须用 `--note` 附原因。

`PlanState` 字段：`task` / `status` / `goal`（目标+验收标准）/ `findings`
（调研结论）/ `steps[]` / `changes[]`（变更记录，append-only）/ `updated_at`。

## 使用时机（When to use）

- 触发：任务复杂（多步骤、跨文件、需求模糊、影响面大、不易回退），
  或用户显式要求规划（"先做个计划""先出方案再动手"）。
- 不触发：简单问答、单步小改动、用户明确要求立即执行的任务。
- 用户问进度：先看会话数据目录是否有 `plan.json`，没有就直接说明无活跃计划，
  不要为此新建计划。

## 工作流（Workflow）

### 1. 调研起草（draft → reviewing）

只读调研（read / 只读 shell），然后落盘：

1. `init --task "..." --goal "..." --findings "..."`（创建为 draft）；
2. 逐个 `add-step --text "..."`；
3. 在回复中展示计划、给出 `plan.json` 绝对路径；
4. `set --status reviewing`，等用户评审。

### 2. 评审门禁（hard gate）

- `status` 不是 `approved` 之前，**禁止任何实施动作**（write / edit /
  删除、变更类 shell 命令）；只允许只读调研和更新计划本身。
- 用户明确同意后才 `set --status approved` 并进入执行；回复含糊（如仅"嗯"）
  时先确认再批准，不要把沉默当批准。

### 3. 逐步执行（executing）

- 状态变化**先落盘再继续**：步骤开始前 `step --index N --status in_progress`；
  完成后 `step --index N --status done --note "<一行结果>"`；阻塞改
  `--status blocked --note "<原因 + 待决事项>"`。
- 每完成一步，在回复中简报（做了什么 / 下一步）。
- 严格按已批准的步骤执行；发现计划与实际不符（偏差、缺步骤、新风险）
  不要自行扩大范围——进入调整协议。

### 4. 收尾（done / aborted）

全部步骤 done 后：核对验收标准 → 总结（做了什么 / 结果 / 遗留事项）→
`set --status done`。放弃任务则 `set --status aborted --note "<原因>"`。

## 调整协议（Adjustment）

任何修改计划的情况（评审期用户意见、执行期发现偏差、用户中途插话）
走同一条路：

1. 用脚本更新计划（`set` / `add-step` / `step`），并用 `log` 追加变更原因；
2. 执行期的调整把 `status` 改回 `reviewing`（`set --status reviewing`）；
3. 在回复中展示变更差异（增删改了哪些步骤），重新过评审门禁；
4. 批准后 `set --status executing`，从被调整的步骤继续。

## 归档协议（Archive）

归档经用户确认后执行 `archive --dir D`：脚本把活跃计划另存为
`plan-archive-<ts>.json / .md` 并移除活跃文件；随后可按需 `init` 新计划。

## 进度查询（Progress）

- 本会话：`show --dir D`（或直接 read `plan.json`），汇报整体 status、
  当前步骤、done / pending / blocked 计数与下一步。
- 新会话问旧任务：读 `~/.zero/sessions/{channel}/index.json`，按
  `title` / `description` / `last_updated_at` 定位候选会话，再读该会话
  数据目录下的 `plan.json`。定位方式与 session-history skill 的三层检索
  一致；需要更多细节时按该 skill 深入。

## 约束（Constraints）

- **落盘即真相**：对话中的展示只是投影；任何状态变化第一时间经脚本写回
  `plan.json`，中断后凭文件恢复，不凭记忆。
- **不手改文件**：绝不直接 write / edit `plan.json` 或 `plan.md`——绕过脚本
  会破坏枚举校验与 json/md 一致性；一切改动走 `scripts/plan.py`。
- **压缩协同**：上下文被 `<compact>` 压缩后，`plan.json` 是任务状态的唯一
  事实源——先读它，不要凭压缩摘要推断步骤进度。
- **子 agent**：plan 状态更新由主会话负责；委托子 agent 执行步骤时，在
  prompt 中传入 `plan.json` 绝对路径供其只读参考，结果报回后由主会话更新
  状态，不让子 agent 写计划。
- **不越权**：计划文件之外的会话数据（messages.jsonl 等）只读，不改动。