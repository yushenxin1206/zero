"""maintenance 任务引擎：自带 cron 循环调度会话复盘与记忆沉淀。

独立于 Scheduler/AgentLoop（复盘写回会话会污染对话，也不触碰
SchedulerStore/tasks.jsonl）。运行状态存 ~/.zero/maintenance/state.json
（各任务最近运行的槽位时间）；启动时若错过了最近的 cron 槽位，则延迟
CATCHUP_DELAY 秒后补跑一次，避免挤占启动关键路径。
"""
import asyncio
import datetime as dt
import json
import logging

from zero.config import Config
from zero.paths import current
from zero.models import AgentRuntimeConfig
from zero.providers.openai import OpenAI
from zero.scheduler import cron

from zero.maintenance.memory import run_memory
from zero.maintenance.session_summary import run_session_summary
from zero.session.store import SessionStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 运行态（路径随 current() 惰性结算，不 import 时冻结）
# ---------------------------------------------------------------------------

# 启动补跑延迟（秒）：错过的最近槽位在启动 60s 后补跑
CATCHUP_DELAY = 60

# 最近槽位回溯上限（分钟）：覆盖一天足够常规 cron
_SLOT_SCAN_MINUTES = 24 * 60


class MaintenanceEngine:
    """维护任务调度引擎：由 main.py 构造并挂为后台任务。"""

    def __init__(self, config: Config, session_store: SessionStore):
        """入参是根配置：两个维护任务各按自己的任务名解析运行时配置
        （模型/provider 可与主对话不同），并各构造独立 provider client。
        维护任务是进程级常量，不跟会话级模型切换走，故只解析一次。"""
        self._maintenance = config.maintenance
        # 会话复盘/记忆沉淀读取的会话存储，由组合根显式传入
        self._session_store = session_store
        summary = config.resolve_runtime('session_summary')
        memory = config.resolve_runtime('memory')
        self._summary_provider = OpenAI(summary.llm_runtime.provider)
        self._summary_runtime: AgentRuntimeConfig = summary
        self._memory_provider = OpenAI(memory.llm_runtime.provider)
        self._memory_runtime: AgentRuntimeConfig = memory
        self._state_lock = asyncio.Lock()

    async def run(self) -> None:
        loops = []
        if self._maintenance.session_summary_enabled:
            loops.append(self._loop("session_summary", self._maintenance.session_summary_cron, self._run_session_summary))
        if self._maintenance.memory_enabled:
            loops.append(self._loop("memory", self._maintenance.memory_cron, self._run_memory))
        if not loops:
            logger.info("maintenance 任务全部关闭")
            return
        logger.info("maintenance 启动: session_summary=%r memory=%r",
                    self._maintenance.session_summary_cron if self._maintenance.session_summary_enabled else None,
                    self._maintenance.memory_cron if self._maintenance.memory_enabled else None)
        await asyncio.gather(*loops)

    async def _run_session_summary(self) -> None:
        await run_session_summary(
            self._summary_provider, self._summary_runtime,
            self._maintenance, self._session_store)

    async def _run_memory(self) -> None:
        await run_memory(
            self._memory_provider, self._memory_runtime,
            self._maintenance, self._session_store)

    async def _loop(self, name: str, cron_expr: str, fn) -> None:
        """单任务调度循环：错过最近槽位 → 补跑；否则睡到下个槽位。"""
        while True:
            now = dt.datetime.now()
            async with self._state_lock:
                state = await asyncio.to_thread(self._load_state)
            last_run = _parse_dt(state.get(name, {}).get("last_run_at"))
            slot = _last_slot(cron_expr, now)
            if slot is not None and (last_run is None or last_run < slot):
                # 从未运行或错过最近槽位 → 延迟补跑一次
                await asyncio.sleep(CATCHUP_DELAY)
                await self._execute(name, fn, slot)
                continue
            ref = now if last_run is None else max(now, last_run)
            try:
                target = cron.next_run(cron_expr, ref)
            except ValueError:
                logger.error("maintenance 任务 %s 的 cron 表达式 %r 无效，该循环终止",
                             name, cron_expr)
                return
            await asyncio.sleep(max((target - dt.datetime.now()).total_seconds(), 0))
            await self._execute(name, fn, target)

    async def _execute(self, name: str, fn, slot: dt.datetime) -> None:
        """执行一次任务并把触发槽位记入状态（槽位而非完成时间，重跑判断更精确）。"""
        try:
            await fn()
        except Exception:  # noqa: BLE001 - 维护任务失败不能杀死循环
            logger.exception("maintenance 任务 %s 执行失败", name)
        async with self._state_lock:
            state = await asyncio.to_thread(self._load_state)
            state.setdefault(name, {})["last_run_at"] = slot.isoformat()
            await asyncio.to_thread(self._save_state, state)

    # ------------------------------------------------------------------
    # State file（读写都在线程池执行，事件循环不参与磁盘等待）
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        state_file = current().maintenance_state
        if not state_file.exists():
            return {}
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            state_file = current().maintenance_state
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("maintenance 状态写入失败（下次运行可能重复补跑）")


def _parse_dt(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _last_slot(cron_expr: str, now: dt.datetime) -> dt.datetime | None:
    """now 之前（含当分钟）最近一次匹配 cron 的时间；找不到返回 None。"""
    cur = now.replace(second=0, microsecond=0)
    for _ in range(_SLOT_SCAN_MINUTES):
        if cron.matches(cron_expr, cur):
            return cur
        cur -= dt.timedelta(minutes=1)
    return None
