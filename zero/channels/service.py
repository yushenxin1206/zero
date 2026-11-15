"""
Channel-agnostic services shared by every transport.

Channels are dumb adapters on purpose: they render what these methods
return and translate transport input into these method calls. Session
listing rules ("empty sessions disappear"), title derivation, task
filtering, model switching and turn submission are defined **once** here
instead of being re-implemented per channel.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

import requests

from zero.bus.queue import MessageBus
from zero.config import Config
from zero.core.model_switch import ModelSwitchResult, session_model, switch_model
from zero.permission.envelope import PermissionState, validate_workspace
from zero.models import (
    ApprovalRecord, LLMCallOptions, Message, MessageBody, MessageIn,
    StopRequest)
from zero.providers.base import LLMPayload
from zero.providers.openai import OpenAI
from zero.data_paths import DataPaths
from zero.scheduler.models import ScheduleTask
from zero.scheduler.store import SchedulerStore
from zero.session.models import SessionMeta
from zero.session.store import SessionStore
from zero.skill_catalog import list_skills, parse_skill_meta, read_skill_body
from zero.tools.registry import ToolRegistry


@dataclass(frozen=True)
class SessionView:
    """A session as a channel displays it.

    ``title`` is already resolved: the stored title, or a derived one from
    the first user message. ``title_is_derived`` tells the renderer whether
    to dim it.
    """

    session_id: str
    source_id: str
    title: str
    title_is_derived: bool
    date: str
    last_updated_at: datetime


def _http_error_text(exc: requests.exceptions.HTTPError) -> str:
    """把厂商返回的 HTTP 状态翻成用户能照着改的提示。"""
    response = exc.response
    code = response.status_code if response is not None else 0
    if code in (401, 403):
        return f"认证失败（HTTP {code}）：apiKey 无效或没有该模型权限"
    if code == 404:
        # 最常见的 404 原因：apiBase 只填了基址（…/v1），而它被原样当 POST
        # URL 用，于是请求打到版本前缀上。能拿到实际 URL 时直接把话说明白。
        url = (getattr(response, "url", "") or "").rstrip("/")
        if url and not url.endswith("/chat/completions"):
            return ("接口或模型不存在（HTTP 404）：实际请求的是 "
                    f"{url}，看起来 apiBase 只填了基址——请补全为以 "
                    "/chat/completions 结尾的完整端点")
        return "接口或模型不存在（HTTP 404）：检查 apiBase 与模型名"
    if code == 429:
        return "请求过于频繁（HTTP 429）：稍后重试或检查额度"
    if code >= 500:
        return f"厂商服务异常（HTTP {code}）：稍后重试"
    return f"请求失败（HTTP {code}）"


class ChannelService:
    """Per-channel domain operations: sessions, history, tasks, models, turns.

    ``channel`` doubles as the on-disk bucket name, so each transport keeps
    its own binding file and never overwrites another transport's session.
    """

    def __init__(
        self,
        channel: str,
        *,
        msg_bus: MessageBus,
        session_store: SessionStore,
        scheduler_store: SchedulerStore,
        data_paths: DataPaths,
        permission: PermissionState,
        config: Config | None = None,
        tool_registry: ToolRegistry | None = None,
        source_prefix: str | None = None,
    ):
        self.channel = channel
        self.msg_bus = msg_bus
        self.config = config
        self.tool_registry = tool_registry
        self.source_prefix = source_prefix or channel
        self.source_id = ""
        self.session_id = ""
        # 会话存储由 channel 组合根显式传入；绑定文件与 session 同根
        self.session_store = session_store
        self.scheduler_store = scheduler_store
        self.data_paths = data_paths
        # 切换模型时要重建 Context（含环境段）：沙箱能力在构造时结算为纯事实
        self.sandbox_available = permission.sandbox_available()
        self._binding_path = (
            session_store.root / channel / f".{channel}_current.json")

    # ------------------------------------------------------------------
    # Session binding
    # ------------------------------------------------------------------

    def restore_binding(self) -> bool:
        """Load the persisted binding. Returns False when absent/corrupt."""
        try:
            data = json.loads(self._binding_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        source_id = data.get("source_id")
        session_id = data.get("session_id")
        if not source_id or not session_id:
            return False
        self._bind(str(source_id), str(session_id), persist=False)
        return True

    def bind(self, source_id: str, session_id: str) -> None:
        """Point this channel at an existing session and persist the choice."""
        self._bind(source_id, session_id, persist=True)

    def new_session(self) -> str:
        """Create and bind a fresh session for this channel."""
        source_id = f"{self.source_prefix}-{uuid4().hex[:8]}"
        session_id = self.session_store.create_session(
            self.channel, source_id)
        self._bind(source_id, session_id, persist=True)
        return session_id

    def _bind(self, source_id: str, session_id: str, *, persist: bool) -> None:
        self.source_id = source_id
        self.session_id = session_id
        if not persist:
            return
        self._binding_path.parent.mkdir(parents=True, exist_ok=True)
        self._binding_path.write_text(json.dumps({
            "source_id": source_id,
            "session_id": session_id,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def sessions(self) -> list[SessionView]:
        """List displayable sessions, most recently updated first.

        Sessions created but never used are excluded here — this is the one
        place the "empty sessions don't exist" rule lives.
        """
        views = []
        for entry in self.session_store.list_sessions(self.channel):
            if not self.session_store.has_messages(
                    self.channel, entry.session_id, entry):
                continue
            views.append(self._view(entry))
        return views

    def find(self, session_id: str) -> SessionView | None:
        """Look up one listed session by id (None if missing or empty)."""
        for entry in self.session_store.list_sessions(self.channel):
            if entry.session_id != session_id:
                continue
            if not self.session_store.has_messages(
                    self.channel, entry.session_id, entry):
                return None
            return self._view(entry)
        return None

    def _view(self, entry: SessionMeta) -> SessionView:
        title = entry.title
        derived = not title
        if derived:
            title = self.session_store.make_title(
                self.session_store.first_user_message(
                    self.channel, entry.session_id, entry))
        return SessionView(
            session_id=entry.session_id,
            source_id=entry.source_id,
            title=title,
            title_is_derived=derived,
            date=entry.date,
            last_updated_at=entry.last_updated_at,
        )

    def title(self, session_id: str | None = None) -> str:
        """Stored title for a session ("" when unset)."""
        return self.session_store.get_title(
            self.channel, session_id or self.session_id)

    def rename(self, title: str, session_id: str | None = None) -> bool:
        """Set a session title. Returns False when the session is not indexed."""
        return self.session_store.set_title(
            self.channel, session_id or self.session_id, title)

    # ------------------------------------------------------------------
    # Working directory
    # ------------------------------------------------------------------

    def working_dir(self, session_id: str | None = None) -> str:
        """Current session's working directory ("" when not selected)."""
        return self.session_store.get_working_dir(
            self.channel, session_id or self.session_id)

    def set_working_dir(
            self, path: str, session_id: str | None = None) -> str:
        """Set/clear the session working directory.

        空串 = 清除选择。非空路径必须是已存在的绝对目录，且不能是系统
        根或 Zero 自己的数据目录；校验失败抛 ValueError，由渠道回显。
        """
        text = (path or "").strip()
        canonical = str(validate_workspace(text, self.data_paths)) if text else ""
        ok = self.session_store.set_working_dir(
            self.channel, session_id or self.session_id, canonical)
        if not ok:
            raise ValueError("当前会话不在索引中，无法设置工作目录")
        return canonical

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def history(self, session_id: str | None = None) -> list[Message]:
        """Full persisted history (including tool messages and compaction)."""
        return self.session_store.load_history(
            session_id or self.session_id, self.channel)

    def display_history(self, session_id: str | None = None) -> list[Message]:
        """Conversation messages a user can be shown (no tool traffic).

        Assistant messages that only trigger tools are kept: channels use
        their ``tool_calls`` to rebuild the same tool chips users saw live.
        Tool result bodies are still excluded — live channels never render
        them either.
        """
        return [
            m for m in self.history(session_id)
            if (m.sender == "user" and m.body.content)
            or (m.sender == "assistant"
                and (m.body.content or m.body.tool_calls))
        ]

    def approvals(self, session_id: str | None = None) -> list[ApprovalRecord]:
        """Audit records for approvals that happened in this session."""
        return self.session_store.load_approvals(
            self.channel, session_id or self.session_id)

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------

    def skills(self) -> list[dict[str, Any]]:
        """Package skills with metadata and rendered skill.md body for viewing."""
        return [{
            "name": skill.name,
            "description": skill.description,
            "location": str(skill.location),
            "meta": parse_skill_meta(skill.location),
            "content": read_skill_body(skill.location),
        } for skill in list_skills()]

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    async def test_connection(
            self, config: Config, timeout: float = 20.0) -> dict[str, Any]:
        """用草稿配置发一条最小请求，验证端点 / 密钥 / 模型名是否可用。

        不修改任何状态，也不要求当前进程的 config 已经可用——引导页保存前
        就要能测。只取首个流式片段：拿到任意输出即视为连通。
        """
        runtime = config.resolve_runtime("default")
        provider = OpenAI(runtime.llm_runtime.provider)
        payload = LLMPayload(
            messages=[{"role": "user", "content": "ping"}],
            llm_call_options=LLMCallOptions(
                model=runtime.llm_call_options.model,
                max_tokens=1,
                temperature=0,
                stream=True,
            ),
        )
        started = time.monotonic()
        stream = provider.stream_chunks(payload)
        try:
            await asyncio.wait_for(stream.__anext__(), timeout)
        except StopAsyncIteration:
            pass
        except (asyncio.TimeoutError, TimeoutError):
            return {"ok": False, "error": f"连接超时：{int(timeout)} 秒内无响应"}
        except requests.exceptions.HTTPError as exc:
            return {"ok": False, "error": _http_error_text(exc)}
        except requests.exceptions.Timeout:
            return {"ok": False, "error": "请求超时：检查 apiBase 与网络"}
        except requests.exceptions.ConnectionError:
            return {"ok": False, "error": "无法连接到 apiBase：检查地址、端口与网络"}
        except Exception as exc:  # noqa: BLE001 - 测试失败要回显而不是中断连接
            return {"ok": False, "error": f"连接测试失败：{type(exc).__name__}: {exc}"}
        finally:
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001 - 已被超时取消时关闭可能失败
                pass
        return {
            "ok": True,
            "latencyMs": int((time.monotonic() - started) * 1000),
        }

    # ------------------------------------------------------------------
    # Scheduled tasks
    # ------------------------------------------------------------------

    def tasks(self, *, all_sessions: bool = False) -> list[ScheduleTask]:
        """This channel's tasks for the current session (or all sessions).

        Newest next-run first: recurring tasks use the advanced
        ``trigger_at`` as their next execution time; tasks without a
        trigger are listed last.
        """
        tasks = [
            t for t in self.scheduler_store.load_all()
            if t.channel == self.channel
            and (all_sessions or t.session_id == self.session_id)
        ]
        tasks.sort(
            key=lambda t: (
                t.trigger_at is not None,
                t.trigger_at or datetime.min,
            ),
            reverse=True,
        )
        return tasks

    def task(self, task_id: str) -> ScheduleTask | None:
        """Look up a single scheduled task by id."""
        return self.scheduler_store.get(task_id)

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    def models(self) -> list[str]:
        """Registered model keys (empty without config)."""
        return sorted(self.config.models) if self.config is not None else []

    def current_model(self) -> str:
        """The model this session resolves to ("" without config, "?" on error)."""
        if self.config is None:
            return ""
        try:
            return session_model(
                self.config, self.channel, self.session_id,
                self.session_store)
        except ValueError:
            return "?"

    async def switch_model(self, name: str) -> ModelSwitchResult:
        """Switch the session-bound model (may compact first)."""
        if self.config is None:
            raise ValueError("未注入配置，无法切换模型")
        tools = (
            self.tool_registry.schemas()
            if self.tool_registry is not None else None)
        return await switch_model(
            config=self.config, channel=self.channel,
            session_id=self.session_id, new_model=name,
            session_store=self.session_store, data_paths=self.data_paths,
            sandbox_available=self.sandbox_available, tools=tools)

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def make_input(self, text: str) -> MessageIn:
        """Build the inbound message for one user turn in the current session."""
        return MessageIn(
            channel=self.channel,
            session_id=self.session_id,
            body=MessageBody(content=text),
            channel_message_id=str(uuid4()),
            channel_sender_id=self.source_id,
            channel_session_id=self.source_id,
        )

    async def submit(self, text: str) -> MessageIn:
        """Enqueue a user turn and return the message that identifies it.

        Raises ``BusQueueFull`` when the bus is saturated: the caller must
        surface the rejection instead of pretending the turn was accepted.
        """
        message = self.make_input(text)
        self.msg_bus.in_queue.enqueue_nowait(message)
        return message

    async def request_stop(self, turn_id: str) -> None:
        """Ask the core to stop one turn (bypasses the serial in-queue)."""
        await self.msg_bus.stop_queue.enqueue(StopRequest(
            channel=self.channel, session_id=self.session_id, turn_id=turn_id))
