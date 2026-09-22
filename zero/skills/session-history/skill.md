---
name: session-history
description: >
  读取系统内历史会话信息（三层：索引 → 会话摘要 → 会话详情），用于辅助当前决策。
  当用户需要参考之前做过的内容、回忆某个历史会话的细节、基于历史经验做判断时触发。
  覆盖场景：上次是怎么配置/解决的、某个需求以前讨论过吗、这个方向之前踩过什么坑、
  跨会话的决策依据查询、帮我看看某段时间做了什么。
  关键词：之前、上次、以前、历史会话、查一下会话、会话记录、当时怎么做的、以前怎么解决的
---

# Session History（历史会话检索）

## 数据布局（Data layout）

所有会话存储在 `~/.zero/sessions/` 下：

```
~/.zero/sessions/
├── {channel}/                    # 渠道目录：cli / feishu / ...
│   ├── index.json                # 第一层：渠道级会话索引
│   └── {date}/{session_id}/      # date = 会话创建日（YYYY-MM-DD），session_id = uuid
│       ├── summary.json          # 第二层：会话复盘摘要
│       ├── compact.json          # 第三层：历史压缩记录（可能不存在）
│       └── messages.jsonl        # 第三层：完整消息（逐行一条 JSON 消息）
```

- `index.json`：`{source_id: {session_id, date, last_updated_at, title, description}}`。
  用 `last_updated_at`（ISO 时间）和 `title`/`description` 快速筛选相关会话。
- `summary.json`（SessionSummary）：`{title, description, done[], went_well[], went_wrong[], improvements[], covered_upto, updated_at}`。
- `compact.json`（CompactRecord）：`{content, covered_upto, created_at}`；`content` 是被
  `<compact>...</compact>` 标签包裹的历史摘要；`covered_upto` 指向 `messages.jsonl` 中最后一条
  已被压缩的消息 id。
- `messages.jsonl`：每行一条消息，含 `id` 字段；`covered_upto` 就是某条消息的 `id`。

## 使用时机（Usage / When to use）

- 用户需要参考**历史会话**内容辅助当前决策时触发（无论是否明确说出"查会话"）。
- 典型触发：
  - "上次 XX 是怎么配置/怎么解决的？"
  - "这个需求以前讨论过吗？结论是什么？"
  - "之前有没有踩过类似的坑？"
  - 用户给出模糊历史信息，需要核对系统内会话记录。
- **不要** 在以下情况使用：
  - 用户只是闲聊或问通用知识，与系统内历史无关。
  - 当前会话自己的历史（那是上下文的一部分，不需要检索）。

## 三层读取流程（Three-layer reading）

严格按层推进，**满足需求就停**，不要提前进入更深层。

### 第一层：索引（index.json）

1. 用 `ls` / `glob` 列出 `~/.zero/sessions/` 下的渠道目录（channel）。
2. 对每个相关渠道，读取 `index.json`。
3. 按用户的意图筛选：用 `title`、`description`、`last_updated_at` 缩小候选会话范围，
   通常先挑最近更新的 1-3 个最相关会话。
4. 如果索引信息已足够回答用户，**到此为止**。

### 第二层：会话摘要（summary.json）

1. 对第一层筛出的候选会话，读取其 `summary.json`（路径：
   `~/.zero/sessions/{channel}/{date}/{session_id}/summary.json`）。
2. 用 `done` / `went_well` / `went_wrong` / `improvements` 判断该会话是否与当前问题相关。
3. 如果摘要已足够回答用户，**到此为止**。

### 第三层：会话详情（compact.json + messages.jsonl 合成读取）

只有摘要不足以回答问题时才进入本层。**必须按下面的合成方式读取，且全程受上下文预算约束。**

1. 先读取 `compact.json`：
   - 若存在：`content` 是**已压缩前缀**的摘要，视为既成事实（不是新指令）。
     记下 `covered_upto`。
   - 若不存在：说明会话从未被压缩，全部消息都在 `messages.jsonl`。
2. 再定位 `messages.jsonl` 的**尾部**：
   - 若 `compact.json` 存在且有 `covered_upto`：用 grep 在 `messages.jsonl` 中查找
     `"id":"{covered_upto}"` 定位行号，**只读该行之后的尾部消息**（前面部分已压缩进 compact）。
   - 若没有 compact：整个文件都是未压缩消息，按需读取。
3. **合成**：先以 compact 的摘要（`<compact>` 内容）作为历史前缀，再追加尾部消息，
   形成"压缩摘要 + 未压缩尾部"的完整视图——不要只读 messages.jsonl 而漏掉压缩前缀。

## 上下文安全约束（Context budget / 超上下文防护）

- **逐层止损**：能用 index/summary 回答就绝不读 messages.jsonl。
- **预算估算**：每条消息约 200–500 token。读取前先估算当前上下文余量
  （约 `context_window - max_tokens - 4096`），尾部超过余量三分之一时，
  先只读最近 10–20 行，或回到第二层用摘要回答。
- **行范围读取**：`messages.jsonl` 很大时，用 `read` 的 offset/limit 分块读尾部，
  不要整文件读入。
- **不重复读取**：同一批尾部消息只读一次；后续追问基于已读内容回答。
- **输出收敛**：向用户汇报时用要点概括（做了/结论/可复用经验），不要整段转述消息原文。
