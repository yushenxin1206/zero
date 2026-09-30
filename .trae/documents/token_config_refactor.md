# token 预算参数配置化（AgentRuntimeSpec 新增 4 字段）

## Context

当前项目的 token 预算散落为硬编码常量或间接取值，无法按任务/环境配置：

- 压缩输出上限 = compact 任务 `llm_call_options.max_tokens`（默认 8192，与"压缩记录约 2000 token"的实际诉求脱节）
- 会话复盘上限 = `maintenance/session_summary.py` 常量 `SESSION_SUMMARY_MAX_TOKENS = 2000`
- 记忆沉淀上限 = `maintenance/memory.py` 常量 `MEMORY_MAX_TOKENS = 8000`
- reasoning 思考余量 = `core/compactor.py` 与 `maintenance/llm.py` 各一份 `REASONING_TOKEN_HEADROOM = 4096`（重复定义）

目标：把 4 个值提升为 `AgentRuntimeSpec` 字段（带默认值），消费方一律从解析后的 `AgentRuntimeConfig` 读取；**压缩输出上限不再从 `llm_call_options.max_tokens` 取**。这样默认值保持现状（2000/2000/8000/4096），但每个任务可在 `config.json` 的 `agentRuntimes` 里按 `task 显式 > default 显式 > 模型默认` 链路覆盖（`_merge_runtime` 对标量字段已天然支持，**config.py 无需改动**）。

新增字段（snake_case，JSON 别名自动 camelCase）：
| 字段 | 默认值 | JSON 键 | 消费方 |
|---|---|---|---|
| `context_compact_max_tokens` | 2000 | `contextCompactMaxTokens` | compact 任务（压缩输出预算） |
| `session_summary_max_tokens` | 2000 | `sessionSummaryMaxTokens` | session_summary 任务 |
| `memory_max_tokens` | 8000 | `memoryMaxTokens` | memory 任务 |
| `reasoning_token_headroom` | 4096 | `reasoningTokenHeadroom` | 各任务（各自读取自己的 runtime） |

## 已确认的决策

1. **回退语义：完全取代**。压缩输出上限只读 `context_compact_max_tokens`；即使 compact 任务在 config.json 显式写了 `llmCallOptions.maxTokens`，也不再参与压缩输出预算（`maxTokens` 仍保留其调用选项语义，如 temperature/model，仅不决定输出上限）。
2. **trigger 计算里的 4096 一并字段化**。compactor 触发阈值 `window - max_tokens - 4096` 的 `4096` 改为 `rc.reasoning_token_headroom`。
3. **命名风格**：字段 snake_case（`context_compact_max_tokens` 等），JSON 键走模型 `to_camel` 别名自动 camelCase（`contextCompactMaxTokens` 等）。
4. **触发阈值里的 `rc.llm_call_options.max_tokens` 保留不动**。它是主对话自己的输出预算（本轮回复最多写多少），回答"当前 payload（含在途回复）是否超窗"；而 `context_compact_max_tokens` 回答"压缩后摘要+尾部装不装得下"，两者独立。若用压缩上限（2000）预留回复空间会低估实际输出（可达 8192），导致触发过迟、窗口溢出，故必须保留。

## 改动点

### 1. zero/models.py（唯一模型改动点）

`AgentRuntimeSpec`（L293-298）新增 4 个字段及注释：

```python
# 压缩记录输出上限（不再取 compact 任务 llm_call_options.max_tokens）
context_compact_max_tokens: int = 2000
# 会话复盘输出上限
session_summary_max_tokens: int = 2000
# 记忆沉淀输出上限
memory_max_tokens: int = 8000
# 单次 LLM 调用为 reasoning 思考预留的 completion 预算余量（思考与正文共享预算）
reasoning_token_headroom: int = 4096
```

- L296 注释中的 `-4096` 同步改为 `-reasoning_token_headroom`
- L343-345 docstring "压缩记录的输出上限不再单独留字段" 改为 "压缩记录输出上限取 compact 任务解析结果的 `context_compact_max_tokens`（不再读 `llm_call_options.max_tokens`）"
- `AgentRuntimeConfig` 继承 Spec，无需额外改动；`_merge_runtime`（config.py）标量字段自动走优先级链，config.py 不动

### 2. zero/core/compactor.py

- 删除 L146-148 `REASONING_TOKEN_HEADROOM = 4096` 常量及注释
- `__init__`（L190-196）：除 `self.compact_llm_options` 外，新增 `self.compact_config = compact_config`（保持 `compact_llm_options` 用于取 model/temperature）
- L213-216 触发阈值：`- 4096` 改为 `- rc.reasoning_token_headroom`（主对话思考余量）
- L226 压缩后预估：`self.compact_llm_options.max_tokens` 改为 `self.compact_config.context_compact_max_tokens`
- L277-315 `_call_compact`：`budget = self.compact_config.context_compact_max_tokens + self.compact_config.reasoning_token_headroom`，docstring 同步（输出预算 = context_compact_max_tokens + 该任务的 reasoning_token_headroom）

### 3. zero/maintenance/llm.py

- 删除 L15 `REASONING_TOKEN_HEADROOM` 常量
- `complete_text` 签名改为 `(provider, model, messages, max_tokens, headroom, label)`，`budget = max_tokens + headroom`。helper 只收数值、不收 pydantic 对象，保持零依赖

### 4. zero/maintenance/session_summary.py

- 删除 L22 `SESSION_SUMMARY_MAX_TOKENS` 常量
- `run_session_summary`（L53）：删掉单独取 model（L56），把 `runtime` 传入 `_summarize_session`
- `_summarize_session` 签名改为 `(provider, runtime, channel, session_id)`，`model = runtime.llm_call_options.model`，L122-126 传 `runtime.session_summary_max_tokens` 与 `runtime.reasoning_token_headroom`

### 5. zero/maintenance/memory.py

- 删除 L22 `MEMORY_MAX_TOKENS` 常量
- L73-76 `complete_text` 调用传 `runtime.memory_max_tokens` 与 `runtime.reasoning_token_headroom`

### 6. 测试更新

- **tests/test_model_resolution.py**
  - L23-24：`COMPACT_OUTPUT_FALLBACK = LLMCallOptions().max_tokens` 改为断言新字段默认值（2000/2000/8000/4096）
  - L129-144 压缩输出上限块：改为断言 `rc4` 的 `context_compact_max_tokens == 2000`（与 compact `llm_call_options.max_tokens` 无关）；`rc5` 中 compact 显式 `max_tokens=5000` 不再影响压缩输出上限；L142-143 `not hasattr(rc5, "context_compress_output_tokens")` 保留
  - 新增优先级链用例：default 显式 `contextCompactMaxTokens=3000` + compact 显式 `1500` → 解析结果 1500（task > default）
- **tests/test_compactor.py**（L189-200 `make_compactor`）：签名加 `context_compact_max=500, headroom=4096`；`cc` 构造改为 `AgentRuntimeConfig(context_compact_max_tokens=context_compact_max, reasoning_token_headroom=headroom, llm_call_options=LLMCallOptions(model="mock-model"))`（不再依赖 `cc.llm_call_options.max_tokens`）；`rc` 加 `reasoning_token_headroom=headroom`；L261 断言 `max_tokens == 500 + 4096` 语义不变；L196-197、L341 相关注释同步；L306-317 no-gain 用例在默认 headroom 下数值不变
- **tests/test_context_cache.py**（L133-140 `make_compactor`）：同上模式更新

## 不做的事

- 不改 `config.py`（`_merge_runtime` 已支持标量合并）、不改 `runner.py`/`engine.py`（构造处无变化）
- 主对话触发阈值中的 `rc.llm_call_options.max_tokens`（主对话输出预算）保持不变——它不属于"压缩输出上限"
- 不更新 docs/（grep 确认 docs 未引用这些常量）

## 验证

1. 跑三个受影响测试：
   - `.venv\Scripts\python.exe tests\test_model_resolution.py`
   - `.venv\Scripts\python.exe tests\test_compactor.py`
   - `.venv\Scripts\python.exe tests\test_context_cache.py`
2. 全量回归：`.venv\Scripts\python.exe -m pytest tests/ -q`（如 pytest 可用），否则逐个运行 tests/ 下的脚本式测试
3. 冒烟：`python -m zero.config` 生成 config.json 模板，确认 `agentRuntimes.default` 下带出 4 个新字段；再确认旧配置（无新键）加载不报错（extra='forbid' 只拦未知键，缺省字段走默认值）
