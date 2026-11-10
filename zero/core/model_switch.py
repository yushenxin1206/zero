"""
会话级模型切换服务（channel 无关）。

CLI 的 /model、未来 Feishu channel 都通过这里切换会话绑定模型。逻辑全部
落在本模块，channel 只负责「注入 config / 渲染结果」。

切换语义（含上下文安全）：
1. 校验目标模型在 config.models 注册表中；
2. 取会话当前模型（SessionStore 绑定或全局 default）；
3. 按「新模型」的窗口 / 输出预算估当前会话 payload；超出触发阈值则说明
   切到新模型后会话会超窗，需要先压缩；
4. 压缩用「旧模型」执行（新模型窗口装不下全量前缀时，拿新模型压会发生
   上下文溢出），但目标预算按新模型窗口算——这正是本服务复用的
   Compactor.compress_for_target 的 force 场景；
5. 压缩成功（或无需压缩）后，把新模型写回会话绑定（SessionStore.set_model）。

注意：切换时的强制压缩不随 config.compact.enabled 走——它是一次性为了
「切换后不超窗」的正确性措施，与持续的自动压缩开关无关。
"""
import asyncio
import logging
from dataclasses import dataclass

from zero.config import Config
from zero.core.compactor import Compactor
from zero.core.context import Context
from zero.models import LLMCallOptions
from zero.data_paths import DataPaths
from zero.providers.openai import OpenAI
from zero.session.store import SessionStore

logger = logging.getLogger(__name__)


def session_model(
        config: Config, channel: str, session_id: str,
        session_store: SessionStore,
) -> str:
    """会话当前生效的模型注册表键：绑定优先，未绑定回退全局 default。

    channel 无关的只读查询，供各 channel 展示「当前模型」。绑定语义收敛
    在 core，channel 只渲染不重复实现。
    """
    return (session_store.get_model(channel, session_id)
            or config.resolve_runtime("default").llm_call_options.model)


@dataclass
class ModelSwitchResult:
    """一次切换的结果：旧模型 → 新模型，以及是否需要/已执行压缩。"""
    old_model: str
    new_model: str
    compacted: bool   # 是否生产了新的压缩记录（切换前为适配新窗口而压缩）
    window_tokens: int  # 新模型的上下文窗口
    switched: bool    # 是否真的发生了绑定变更（False = 已是同一模型）


async def switch_model(
        config: Config,
        channel: str,
        session_id: str,
        new_model: str,
        *,
        session_store: SessionStore,
        data_paths: DataPaths,
        sandbox_available: bool,
        tools: list[dict] | None = None,
) -> ModelSwitchResult:
    """把会话绑定模型切换到 new_model（config.models 注册表键）。

    切换前先按新模型窗口判定是否需要压缩：需要则先用旧模型按新模型参数
    压缩，再写回绑定。任何校验失败抛 ValueError（由 channel 决定提示文案）。
    """
    new_model = new_model.strip()
    if new_model not in config.models:
        raise ValueError(
            f"模型 {new_model!r} 不在 models 注册表中，"
            f"已知键: {sorted(config.models)}")

    old_model = (await asyncio.to_thread(
        session_store.get_model, channel, session_id)
        or config.resolve_runtime("default").llm_call_options.model)
    if old_model == new_model:
        return ModelSwitchResult(
            old_model=old_model, new_model=new_model, compacted=False,
            window_tokens=config.models[new_model].context_window_tokens,
            switched=False)

    # 新模型的目标运行时（决定切换后的窗口 / 触发阈值 / 输出预算）
    target = config.resolve_runtime("default", model_override=new_model)
    # 旧模型运行时：压缩调用用它执行（规避新窗口装不下前缀导致溢出）
    old_runtime = config.resolve_runtime("default", model_override=old_model)
    old_provider = OpenAI(old_runtime.llm_runtime.provider)
    old_llm_options: LLMCallOptions = old_runtime.llm_call_options.model_copy()

    # 同一进程内会话上下文的 system prompt 复用同一 Context 构建逻辑；
    # 历史/压缩记录/提示词全部在线程池读完再进入压缩调用，事件循环不等盘
    ctx = Context(
        session_id=session_id, channel=channel,
        session_store=session_store, data_paths=data_paths,
        sandbox_available=sandbox_available)
    await ctx.aprepare()
    raw = ctx.raw_history
    rec = ctx.compact_record

    compacted = False
    if raw:
        compact_runtime = config.resolve_runtime("compact")
        compactor = Compactor(
            provider=OpenAI(compact_runtime.llm_runtime.provider),
            session_runtime=target,
            compact_runtime=compact_runtime,
            compact_config=config.compact,
            session_store=session_store,
        )
        record = await compactor.compress_for_target(
            session_id=session_id,
            channel=channel,
            raw=raw,
            rec=rec,
            system_prompt=ctx.system_prompt,
            tools=tools,
            target=target,
            force=True,  # 切换是正确性必需：压完仍可能超也尽力落盘
            provider=old_provider,
            llm_options=old_llm_options,
        )
        compacted = record is not None

    await asyncio.to_thread(
        session_store.set_model, channel, session_id, new_model)
    logger.info(
        "会话模型切换: session=%s %s -> %s%s", session_id, old_model, new_model,
        " (已按新窗口压缩)" if compacted else "")
    return ModelSwitchResult(
        old_model=old_model, new_model=new_model, compacted=compacted,
        window_tokens=config.models[new_model].context_window_tokens,
        switched=True)