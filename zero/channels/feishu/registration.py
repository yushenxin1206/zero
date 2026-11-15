"""扫码连接飞书：设备授权码（device authorization）流程。

用 lark-oapi 自带的 ``aregister_app``：用户在飞书 App 里扫码确认后，平台
直接签发一套应用凭据（client_id / client_secret），省去「去开放平台建应用、
再回来抄密钥」的来回。整个流程在后台任务里跑，不阻塞事件循环。

两条硬约束：

1. **secret 只出现一次**。凭据只在成功回调里存在，绝不写日志、不进异常
   信息、也不推给前端；前端只会看到 appId 与提示文案。
2. **先验令牌，再落盘**。个人版飞书能扫码建应用，但平台会拒绝签发服务端
   令牌。验收不通过就丢弃凭据，绝不把「看起来配好了、实际用不了」的配置
   留给用户。

``addons`` 预填了 IM 收发所需的最小权限与 ``im.message.receive_v1`` 事件，
扫码创建的应用自带这些申请，少一轮事后补权限。
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import lark_oapi as lark
import qrcode
import qrcode.image.svg
from lark_oapi.api.auth.v3 import (
    InternalTenantAccessTokenRequest,
    InternalTenantAccessTokenRequestBody,
)
from lark_oapi.scene.registration import (
    AppAccessDeniedError,
    AppExpiredError,
    RegisterAppError,
    aregister_app,
)

from zero.config import ConfigManager

logger = logging.getLogger(__name__)

# 扫码创建应用时预填的最小权限集：
# - im:message:send_as_bot      回复用户，缺了它连一条消息都发不出去
# - im:message.p2p_msg:readonly 读取用户发来的单聊消息
# - im:message.group_at_msg:readonly 接收群里 @机器人 的消息
# 只给这三项：够收发，又不至于一上来就申请通讯录、群管理等敏感权限。
REGISTRATION_ADDONS: dict[str, Any] = {
    "scopes": {
        "tenant": [
            "im:message:send_as_bot",
            "im:message.p2p_msg:readonly",
            "im:message.group_at_msg:readonly",
        ],
    },
    "events": {
        "items": {
            # 与 FeishuChannel._build_event_handler 注册的处理器一一对应；
            # 少了这个事件，应用建好了也收不到任何消息。
            "tenant": ["im.message.receive_v1"],
        },
    },
}

QR_EXPIRED_TEXT = "二维码已过期，请重新生成"
QR_DENIED_TEXT = "已取消授权：如果你在飞书里点了拒绝，重新扫码即可"
QR_ACCOUNT_TEXT = "当前账号类型不支持调用接口，请换企业租户或手动填写凭据"
QR_NETWORK_TEXT = "扫码失败：无法连接飞书开放平台，请检查网络后重试"

EventCallback = Callable[[dict[str, Any]], None]
ProbeCallback = Callable[[str, str], tuple[bool, str]]
RegisterCallback = Callable[..., Any]


def qr_svg(url: str) -> str:
    """把授权链接画成本地 SVG（二维码在后端生成，不依赖任何 CDN）。

    返回裸 ``<svg>`` 片段：去掉 qrcode 生成的 XML 声明。前端用 innerHTML
    注入时，``<?xml ...?>`` 会被 WKWebView 当成非法节点，导致整块二维码
    渲染成空白（实测过）。
    """
    image = qrcode.make(
        url, image_factory=qrcode.image.svg.SvgPathImage,
        box_size=10, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    markup = buffer.getvalue().decode("utf-8")
    return markup[markup.index("<svg"):]


def probe_tenant_token(app_id: str, app_secret: str) -> tuple[bool, str]:
    """用扫码拿到的凭据换一次 tenant_access_token。

    这是「凭据到底能不能用」的唯一可信判据：接口全部走应用身份，拿不到
    令牌就等于通道不可用，配置写进去也只会在重启后反复连接失败。
    返回值第二项只带错误码与平台文案，绝不包含 secret。
    """
    client = (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )
    request = (
        InternalTenantAccessTokenRequest.builder()
        .request_body(
            InternalTenantAccessTokenRequestBody.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .build()
        )
        .build()
    )
    response = client.auth.v3.tenant_access_token.internal(request)
    if response.success():
        return True, ""
    return False, f"{response.code}: {response.msg}"


def _friendly_error(code: str) -> str:
    """把 SDK 的机器错误码翻译成用户能看懂的中文。"""
    if code == "expired_token":
        return QR_EXPIRED_TEXT
    if code == "access_denied":
        return QR_DENIED_TEXT
    if code == "unsupported_auth_method":
        return "当前飞书环境不支持扫码创建应用，请手动填写 appId 与 appSecret"
    if code == "abort":
        return "已取消扫码"
    if code:
        return f"扫码失败（{code}）：请重试，或在「配置」里手动填写凭据"
    return "扫码失败，请重试"


class QrConnector:
    """一次扫码会话的生命周期：启动、推送二维码、校验凭据、取消。

    同一时刻只保留一个会话；重复启动会先取消旧的，保证「重新生成」既不会
    叠出两条轮询，也不会因为旧会话还在跑而拒绝新二维码。
    """

    def __init__(
        self,
        manager: ConfigManager,
        on_event: EventCallback | None = None,
        *,
        register: RegisterCallback = aregister_app,
        probe: ProbeCallback = probe_tenant_token,
    ):
        self._manager = manager
        self._on_event = on_event
        self._register = register
        self._probe = probe
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # 重新生成时取消旧会话，不能把「已取消」推给正在等新二维码的界面
        self._replacing = False

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._lock:
            old, self._task = self._task, None
            if old is not None and not old.done():
                self._replacing = True
                try:
                    old.cancel()
                    with suppress(asyncio.CancelledError):
                        await old
                finally:
                    self._replacing = False
            self._task = asyncio.create_task(self._run())

    async def cancel(self) -> None:
        """取消当前会话；没有会话时什么也不做。"""
        async with self._lock:
            task, self._task = self._task, None
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _run(self) -> None:
        try:
            # 二维码回调在同一个事件循环里同步执行：推送事件只是排一个
            # 广播任务，不会拖慢 SDK 的轮询节奏。
            #
            # 三个可选参数刻意都不传，含义（见 lark-oapi 官方文档）：
            # - create_only=True 会让确认页只剩「新建应用」，「选择已有应用」
            #   入口被隐藏；不传则保留该入口，用户扫过、建过的应用可以直接
            #   再选中，本次 addons 会追加到它上面。我们要的正是后者。
            # - app_id 非空时流程会绑定到这个已知应用（QR URL 的 clientID
            #   参数），语义是「更新该应用的配置」；我们并不知道用户手里有
            #   哪些应用，所以交给确认页选。
            # - addons 只做增量：合并到平台基础模板，基础权限不会被移除。
            result = await self._register(
                on_qr_code=self._on_qr_code,
                addons=REGISTRATION_ADDONS,
                source="zero-agent",
            )
        except asyncio.CancelledError:
            if not self._replacing:
                self._emit({"phase": "cancelled", "text": "已取消扫码"})
            raise
        except AppExpiredError:
            self._emit({"phase": "failed", "text": QR_EXPIRED_TEXT})
        except AppAccessDeniedError:
            self._emit({"phase": "failed", "text": QR_DENIED_TEXT})
        except RegisterAppError as exc:
            # 只记错误码：SDK 的 description 来自平台，可能带上下文，
            # 但 secret 不在其中；仍然是越少落日志越好。
            logger.warning("飞书扫码流程失败：%s", exc.code)
            self._emit({"phase": "failed", "text": _friendly_error(exc.code)})
        except Exception:
            logger.exception("飞书扫码流程异常")
            self._emit({"phase": "failed", "text": QR_NETWORK_TEXT})
        else:
            await self._accept(result)
        finally:
            self._task = None

    def _on_qr_code(self, info: dict[str, Any]) -> None:
        url = str(info.get("url") or "")
        if not url:
            return
        try:
            expire_in = int(info.get("expire_in") or 0)
        except (TypeError, ValueError):
            expire_in = 0
        self._emit({
            "phase": "waiting",
            "url": url,
            "expireIn": expire_in,
            "svg": qr_svg(url),
        })

    async def _accept(self, result: Any) -> None:
        app_id = str((result or {}).get("client_id") or "")
        app_secret = str((result or {}).get("client_secret") or "")
        if not app_id or not app_secret:
            self._emit({
                "phase": "failed",
                "text": "飞书没有返回完整凭据，请重新扫码",
            })
            return

        try:
            ok, detail = await asyncio.to_thread(
                self._probe, app_id, app_secret)
        except Exception:
            logger.exception("扫码凭据令牌校验异常")
            self._emit({"phase": "failed", "text": QR_NETWORK_TEXT})
            return
        if not ok:
            # 账号类型限制（个人版等）就在这一步暴露；凭据直接丢弃，
            # 不写进配置，避免重启后反复失败。
            logger.warning("扫码得到的应用无法签发访问令牌：%s", detail)
            self._emit({"phase": "failed", "text": QR_ACCOUNT_TEXT})
            return

        try:
            await asyncio.to_thread(
                self._manager.set_feishu_credentials, app_id, app_secret)
        except Exception:
            logger.exception("飞书扫码凭据写入配置失败")
            self._emit({
                "phase": "failed",
                "text": "凭据校验通过，但写入配置失败，请检查数据目录权限",
            })
            return

        self._emit({
            "phase": "success",
            "appId": app_id,
            "text": "扫码成功：凭据已验证并写入配置，重启 zero 后生效",
        })

    def _emit(self, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(payload)
        except Exception:
            logger.exception("飞书扫码事件推送失败")
