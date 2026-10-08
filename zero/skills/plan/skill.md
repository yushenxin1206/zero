---
name: plan
description: >
  复杂任务的规划-评审-执行-跟踪工作流：计划落盘在会话数据目录的 plan.md，
  批准前不执行实施动作，执行偏差须回到评审门禁重新确认，进度随时可查。
  当任务复杂（多步骤、跨文件、需求模糊、影响面大、不易回退），或用户要求
  先规划/先出方案再动手时触发。
  关键词：先做个计划、先规划、先出方案、方案评审、按计划执行、调整计划、
  任务进度、进展如何、继续上次的任务
---

# Plan（复杂任务的规划 · 评审 · 执行 · 跟踪）

## 数据布局（Data layout）

- 当前会话的任务计划存放在 **会话数据目录** 下的 `plan.md`。该目录以环境块
  `Session dir` 行为准——即本会话 `messages.jsonl` / `compact.json` /
  `summary.json` 所在目录。它是**会话数据的存储位置，不是工作目录**；
  不要用今天的日期自行拼接路径（会话创建日期与你可见的日期可能不同）。
- 每个会话同一时刻至多一个活跃 `plan.md`。新复杂任务到来而旧计划未结案
  （reviewing / approved / executing）时，先问用户：结案旧计划 / 归档 /
  并入旧计划，不要静默覆盖。

## plan.md 格式（Format）

```markdown
---
task: <一句话任务描述>
status: <draft|reviewing|approved|executing|done|aborted>
updated: <YYYY-MM-DD HH:MM>
---

# Plan: <标题>

## 目标
<目标 + 验收标准：怎样算完成>

## 调研结论
<现状 / 约束 / 已排除的方案，来自只读调研>

## 步骤
1. [pending] <步骤描述>（涉及的文件/命令）
2. [in_progress] <步骤描述>
3. [done] <步骤描述> — <一行结果摘要>
4. [blocked] <步骤描述> — <原因 + 待决事项>
5. [skipped] <步骤描述> — <跳过原因>

## 变更记录
- <MM-DD HH:MM> <谁/为什么改了什么>（append-only，只增不改）
```

状态机（frontmatter `status`）：

    draft → reviewing → approved → executing → done
                                    ↘ aborted（任意阶段可放弃，记录原因）

步骤标记：`pending` → `in_progress` → `done`；`blocked` / `skipped` 为旁路
终态，行内必须附原因。每次改动 plan.md 都同步刷新 frontmatter `updated`。

## 使用时机（When to use）

- 触发：任务复杂（多步骤、跨文件、需求模糊、影响面大、不易回退），
  或用户显式要求规划（"先做个计划""先出方案再动手"）。
- 不触发：简单问答、单步小改动、用户明确要求立即执行的任务。
- 用户问进度：先看会话数据目录是否有 `plan.md`，没有就直接说明无活跃计划，
  不要为此新建计划。

## 工作流（Workflow）

### 1. 调研起草（status: draft → reviewing）

只读调研（read / 只读 shell），把目标、调研结论、步骤写入 `plan.md`
（创建时 `status: draft`），然后在回复中展示计划、给出 `plan.md` 绝对路径，
置 `status: reviewing`，等用户评审。

### 2. 评审门禁（hard gate）

- `status` 不是 `approved` 之前，**禁止任何实施动作**（write / edit /
  删除、变更类 shell 命令）；只允许只读调研和更新 `plan.md` 本身。
- 用户明确同意后才置 `status: approved` 并进入执行；回复含糊（如仅"嗯"）
  时先确认再批准，不要把沉默当批准。

### 3. 逐步执行（status: executing）

- 状态变化**先落盘再继续**：步骤开始前标 `[in_progress]`；完成后立即改
  `[done]` 并附一行结果；阻塞改 `[blocked]` 记原因。
- 每完成一步，在回复中简报（做了什么 / 下一步）。
- 严格按已批准的步骤执行；发现计划与实际不符（偏差、缺步骤、新风险）
  不要自行扩大范围——进入调整协议。

### 4. 收尾（status: done / aborted）

全部步骤 done 后：核对验收标准 → 总结（做了什么 / 结果 / 遗留事项）→
置 `status: done` 落盘。放弃任务则置 `aborted` 并记录原因。

## 调整协议（Adjustment）

任何修改计划的情况（评审期用户意见、执行期发现偏差、用户中途插话）
走同一条路：

1. 更新 `plan.md`（目标 / 调研结论 / 步骤），在"变更记录"追加一行；
2. 执行期的调整把 `status` 改回 `reviewing`；
3. 在回复中展示变更差异（增删改了哪些步骤），重新过评审门禁；
4. 批准后置回 `executing`，从被调整的步骤继续。

## 归档协议（Archive）

归档不依赖删除/改名命令：经用户确认后，先把旧 `plan.md` 内容原样写入
`plan-archive-<YYYYMMDD-HHMMSS>.md`（同目录），再用新计划内容覆盖
`plan.md`。

## 进度查询（Progress）

- 本会话：读 `plan.md`，汇报整体 status、当前步骤、
  done / pending / blocked 计数与下一步。
- 新会话问旧任务：读 `~/.zero/sessions/{channel}/index.json`，按
  `title` / `description` / `last_updated_at` 定位候选会话，再读该会话
  数据目录下的 `plan.md`。定位方式与 session-history skill 的三层检索
  一致；需要更多细节时按该 skill 深入。

## 约束（Constraints）

- **落盘即真相**：对话中的展示只是投影；任何状态变化第一时间写回
  `plan.md`，中断后凭文件恢复，不凭记忆。
- **压缩协同**：上下文被 `<compact>` 压缩后，`plan.md` 是任务状态的唯一
  事实源——先读它，不要凭压缩摘要推断步骤进度。
- **子 agent**：plan 状态更新由主会话负责；委托子 agent 执行步骤时，在
  prompt 中传入 `plan.md` 绝对路径供其只读参考，结果报回后由主会话更新
  状态，不让子 agent 写 `plan.md`。
- **不越权**：`plan.md` 之外的会话数据（messages.jsonl 等）只读，不改动。
