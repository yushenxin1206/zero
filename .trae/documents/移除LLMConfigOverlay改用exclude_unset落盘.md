# 移除 LLMConfigOverlay，改用 exclude_unset 落盘

## Context（为什么改）

代码 review 发现 `LLMConfigOverlay` 是**不必要的类拆分**：

1. 它整份**重复抄写** `LLMConfig` 的 10 个字段，是字段列表漂移风险的唯一来源。
2. 它的存在纯粹是为了**绕过 save_config 的全字段 `model_dump_json` 落盘 bug**（一次存盘往返就把 `model_fields_set` 污染成全集，空串/None model 会盖掉基座模型名）。全项目 `exclude_unset` **零次使用**，作者却直接造了个类来赎罪。

修复方向：**让 save_config 用 `exclude_unset=True` 落盘（磁盘只写用户真改过的字段），彻底消除污染源；同时删除 `LLMConfigOverlay`，使 `TaskConfig.llm_config` 直接复用 `LLMConfig`，overlay 合并改为由 `model_fields_set` 驱动。**

目标产物：
- 删除 `LLMConfigOverlay` 类，字段定义单一来源（只剩 `LLMConfig`）。
- 一次存盘往返后不再产生全字段文件，`fields_set` 可信。
- 保留对「历史全字段旧文件」与「空串 model」的健壮性（不因兼容而丢基座模型名）。
- 现有测试语义全部保持（改写实现方式，不改断言目标）。

## 改动清单

### 1. `zero/config.py` — 关键：消除污染源
- [save_config L42](file:///c:/Users/Zhoutao/Desktop/PythonProjects/zero/zero/config.py#L42) 改为：
  `config.model_dump_json(indent=2, by_alias=True, exclude_unset=True)`
- 效果：只落盘 `model_fields_set` 命中的字段。首次用新逻辑 save 旧文件时自动完成一次瘦身迁移。
- 副作用说明：磁盘配置不再展示未设置的默认值 —— 这正是本方案的前提，需在注释标注原因。

### 2. `zero/models.py` — 删类 + 改合并
- **删除** `LLMConfigOverlay`（含 `'' → None` 的 validator，整类删）。
- [TaskConfig.llm_config L296](file:///c:/Users/Zhoutao/Desktop/PythonProjects/zero/zero/models.py#L296) 类型改为 `LLMConfig | None = None`，更新其 docstring（不再有 overlay/None 哨兵语义，改为"字段未设置不覆盖"）。
- **重写** `_task_overlay`（[L347-L353](file:///c:/Users/Zhoutao/Desktop/PythonProjects/zero/zero/models.py#L347-L353)）：改为 **fields_set 驱动 + 空值兜底**合并：

```python
def _task_overlay(task_cfg: TaskConfig | None, base: LLMConfig) -> LLMConfig:
    """把任务级 llm_config 叠加到基座上（未设置的字段不覆盖）。

    以 model_fields_set 判定"是否显式设置"（exclude_unset 落盘下可信），
    再按值过滤 None / 空 model：既能挡住历史上全字段旧文件的 null 字段，
    也能挡掉 model:'' 这种异常值，保证基座模型名永不丢失。
    """
    if task_cfg is None or task_cfg.llm_config is None:
        return base
    ov = task_cfg.llm_config
    updates: dict[str, Any] = {}
    for k in ov.model_fields_set:
        v = getattr(ov, k)
        if v is None or (k == 'model' and v == ''):
            continue
        updates[k] = v
    return base.model_copy(update=updates)
```

  > 说明：新文件下 `fields_set` = 用户真改过的字段，正确合并；历史全字段文件的 null 被 `is None` 过滤、`model:''` 被 model 特判过滤——这条 merge 对两种来源都健壮，仍保持"空 model 不盖基座"的既有保证。
  >
  > `LLMConfig.config_dict`（[L242-L246](file:///c:/Users/Zhoutao/Desktop/PythonProjects/zero/zero/models.py#L242-L246)）处理 `extra_body` 的合并逻辑**不动**，复用在解析产物里。

- **重写** `_compact_output_tokens`（[L418-L428](file:///c:/Users/Zhoutao/Desktop/PythonProjects/zero/zero/models.py#L418-L428)）——这是最容易漏的回归点：

  现在 `TaskConfig.llm_config` 从 overlay（`max_tokens` 默认 `None`）换成 `LLMConfig`（默认 `max_tokens=8192`），原有的 `cc.llm_config.max_tokens is not None` 判断会**恒为真**，把 8192 误当输出上限。必须改为 fields_set 判定：

```python
    cc = tasks.get('compact')
    if (cc is not None and cc.llm_config is not None
            and 'max_tokens' in cc.llm_config.model_fields_set):
        return cc.llm_config.max_tokens
    return DEFAULT_COMPACT_OUTPUT_TOKENS
```

- `resolve_runtime` / `resolve_task_llm`（[L356-L415](file:///c:/Users/Zhoutao/Desktop/PythonProjects/zero/zero/models.py#L356-L415)）调用 `_task_overlay` 处**无需改**（内部实现已替换，签名不变）。`AgentRuntimeConfig` / `ResolvedLLM` 均不引用 overlay，不受影响。

### 3. `tests/test_model_resolution.py` — 改实现、守语义
- [L14 的 `from zero.models import LLMConfigOverlay`](file:///c:/Users/Zhoutao/Desktop/PythonProjects/zero/tests/test_model_resolution.py#L20) 删除。
- [L96-L112](file:///c:/Users/Zhoutao/Desktop/PythonProjects/zero/tests/test_model_resolution.py#L96-L112) 的「full 展开」用例：不能再 `LLMConfigOverlay.model_validate`，改为用 `TaskConfig` 直接构造一张"带全字段 + model:'' / null"的 `llm_config`（即 `LLMConfig` 实例字段被显式填满），断言 `resolve_runtime` 后基座 model 仍保留、null 字段不覆盖。这是对旧用例意图（空串/null 不盖基座）的等价改写。
- [L43-L49、L84-L89 里所有 `LLMConfigOverlay(...)` 构造](file:///c:/Users/Zhoutao/Desktop/PythonProjects/zero/tests/test_model_resolution.py#L43-L49) 换成 `LLMConfig(...)` —— overlay 改为 LLMConfig 后，显式传的字段进 `fields_set`、未传的取 LLMConfig 默认，语义一致。
- [L83-L89 的 compact 输出上限用例](file:///c:/Users/Zhoutao/Desktop/PythonProjects/zero/tests/test_model_resolution.py#L83-L89)：断言目标不变（未写 maxTokens → 2000），但构造方式随之改 `LLMConfig`。**务必跑通此处以验证 `_compact_output_tokens` 的 fields_set 改写正确。**

## 涉及文件
- `zero/config.py`（save_config 一行 + 注释）
- `zero/models.py`（删类、改 TaskConfig 类型、重写 `_task_overlay`、重写 `_compact_output_tokens`）
- `tests/test_model_resolution.py`（删 import、改 overlay 构造、等价改写 full-展开用例）

## 不需改动
- `AgentRuntimeConfig` / `ResolvedLLM` / `resolve_runtime` / `resolve_task_llm` 签名。
- `compactor.py` / `runner.py` / `maintenance/*` / `loop.py` / `main.py` 对解析产物的消费（`LLMConfig` 与 overlay 均带 `provider`+`llm_config`，下游无感）。

## 验证
1. 单元：
   - `cd c:\Users\Zhoutao\Desktop\PythonProjects\zero && python tests/test_model_resolution.py` —— 必须输出 `ALL MODEL RESOLUTION TESTS PASSED`。
   - `python tests/test_compactor.py`（若存在）确认不受影响。
2. 手动回归（一次性迁移验证）：用一份旧的全字段 `config.json` 跑一次 `Config.model_validate` + `resolve_runtime`，确认无全字段污染报错、基座模型名保留；再调 `save_config`，确认新文件只含用户显式字段。
3. 若存在真实 `~/.zero/config.json`：只读检查其 `tasks.*.llmConfig` 形状是否符合预期，不实际改用户的配置文件（验证代码路径即可）。