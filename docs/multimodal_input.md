# 多模态输入（图片 + 文字）设计方案

状态：已搁置（2026-11-16 暂缓，暂不实施）
范围：Web（pywebview 桌面窗口）、CLI 两个交互渠道；飞书通道列为后续阶段
约束：只走 OpenAI-compatible `/chat/completions`；不改动现有业务代码，本文只给方案

---

## 0. 结论摘要

| 议题 | 推荐做法 | 一句话理由 |
| --- | --- | --- |
| 传输 | 图片二进制不进 `messages.jsonl`，只存元信息；文件落在会话目录 `attachments/` | 历史文件保持纯文本、append-only，不因图片膨胀 |
| 模型侧格式 | `content` 数组 + `image_url.url = data:image/...;base64,...` | 公开 URL 不可得（本地桌面应用），OpenAI-compatible 只认 URL 或 data URL |
| 输入端 | Web：粘贴 / 拖拽 / 文件选择三入口；CLI：`/image <绝对路径>` | 覆盖真实使用习惯，且都能复用同一套入库逻辑 |
| 发送时机 | 先上传拿 `attachment.id`，`send` 只带 id 列表 | 上传即校验、即时预览；避免把几 MB base64 塞进一条 `send` 帧 |
| 视觉能力 | `models.*.supportsVision` 显式声明，默认 `false`（fail-closed） | 不支持时报可执行的错误，而不是静默丢图或等厂商 400 |
| 图片处理 | 引入 Pillow：去 EXIF、等比缩到 ≤1568px、重编码 JPEG q85、生成缩略图 | 降 token、降带宽、降隐私风险；原图直接发会让每轮请求达到几 MB |
| 历史重放 | 历史只回元信息 + 缩略图；点开时按需拉全图 | 控制 WS 帧大小与首屏内存 |
| 旧图 | 只保留最近 N 轮（默认 3）+ 最多 8 张进模型 payload，更早的替换为文字占位；UI 仍可见 | 图片按 prompt token 计费，且每轮重发，必须有明确的成本闸门 |
| 压缩配合 | 压缩输入一律剥图（文字占位），压缩记录天然只保留文字结论 | 压缩模型可能不具备视觉能力，且摘要不需要图 |
| 权限 | 走路径的来源用 `PermissionState.check_readable` 拦凭证目录；粘贴字节视为用户主动输入；不新增工具、不进审批 | 用户选图是用户意图，不该被当成模型主动读盘 |

最小可用版本（MVP）：Web 粘贴 + 拖拽 + 选择文件，单图/多图上传，发送给标记了 `supportsVision` 的模型，历史可回显。
后续增强：CLI、缩略图、旧图淘汰策略、飞书图片消息、原生拖拽事件。

---

## 1. 现状盘点（方案的事实基础）

### 1.1 消息模型

- [`MessageBody`](../zero/models.py#L74-L81) 已经有 `attachments: list | None` 字段，但**无类型、全仓无读写**，是现成的落点。
- [`Message.to_openai()`](../zero/models.py#L144-L164) 对 user 消息只产出 `{"role": "user", "content": str}`，没有多段 content 的组装点。
- 历史落盘是 [`SessionStore.append()`](../zero/session/store.py#L509-L518) 的逐行 `model_dump_json()`（snake_case），轮次用 `turn_begin/turn_end` 包住；未闭合轮次在加载时整轮丢弃。

### 1.2 Web 链路

- 前端是单文件 [`index.html`](../zero/channels/web/static/index.html)，**没有任何拖拽 / 粘贴 / 文件读取逻辑**；发送只有一条 [`submit()`](../zero/channels/web/static/index.html#L3940-L3959)，发 `{"type":"send","text":...}`。
- [`WebChannel`](../zero/channels/web/server.py) 是纯传输入口：`_dispatch` 按 `type` 分发，`_on_send` 明确 `if not text: return`（[这里](../zero/channels/web/server.py#L681-L705)），所以**纯图片消息现在会被直接丢弃**。
- WebSocket 服务端 `max_size=8 * 1024 * 1024`（[这里](../zero/channels/web/server.py#L155-L165)）；base64 膨胀 4/3，当前上限只够 ~6MB 原始图。
- 窗口通过 `webview.create_window(html=html, js_api=api)` 加载，`base_uri` 为空；JS 侧只剩 `bootstrap / set_theme / pick_folder / open_target / restart_app`（[shell.py](../zero/channels/web/shell.py)）。pywebview 6.2.1 **没有原生文件拖拽事件**（`Window.events` 只有 closed/loaded/shown 等），拖拽必须在 WebView 内用 HTML5 DnD 处理。
- `ChannelService.submit()` / `make_input()` 只接受 `text`（[service.py](../zero/channels/service.py#L441-L460)）。

### 1.3 CLI 链路

- [`CliInput.read()`](../zero/channels/cli/input.py#L67-L96) 每次读一行文本；`_chat_loop` 把 `text` 直接 `make_input(text)` 入队（[channel.py](../zero/channels/cli/channel.py#L143-L174)），斜杠命令走 `CliCommands.dispatch`。

### 1.4 Provider

- [`OpenAI.stream_chunks()`](../zero/providers/openai.py) 把 `LLMPayload.messages` 原样当 JSON body POST；**协议层不需要任何改动**，只要 messages 里出现 content 数组即可。
- [`LLMPayload`](../zero/providers/base.py#L31-L44) 只是容器。

### 1.5 上下文与压缩

- [`Context.messages`](../zero/core/context.py#L353-L369) 每次迭代从 turn 级快照合成 `[压缩消息] + 尾部`，同步属性、无 IO。
- [`estimate_tokens` / `estimate_message`](../zero/core/compactor.py#L39-L53) 只按文本估算；图片不参与会导致压缩永远不触发（超窗直接 400）。
- 压缩输入由 [`build_compact_messages`](../zero/core/compactor.py#L162-L181) 用 `to_openai()` 组装。

### 1.6 历史展示

- [`ChannelService.display_history()`](../zero/channels/service.py#L294-L307) 过滤条件是 `sender == "user" and m.body.content`，**纯图片消息 content 为空会被整条过滤掉**。
- Web 历史由 [`_history_sync()`](../zero/channels/web/server.py#L216-L257) 序列化，字段固定为 id/role/content/compact/turnId/at/toolCalls/usage/fileChanges。

### 1.7 权限

- 读方向是减法：除 [`ungrantable_read()`](../zero/permission/envelope.py#L200-L213)（`~/.ssh`、`~/.aws`、keychain、`~/.zero/config.json` 等）外都可读。
- 审批链只服务**工具调用**（`before_tool_call`）；渠道侧的用户输入不经 hook。图片上传属于用户输入，不需要审批，但**路径来源仍应过 `check_readable`**，否则等于给任意本地进程加了一条「读任意文件并以 base64 回传」的旁路。

---

## 2. 数据形态

### 2.1 消息里的表达

新增一个带类型的附件模型，并替换现有裸 `list`：

```python
# zero/models.py
ImageKind = Literal["image"]
ATTACHMENT_IMAGE: ImageKind = "image"


class Attachment(Base):
    """一条用户输入里的图片附件（只存元信息，二进制在会话目录）。

    刻意不存绝对路径：路径由 session_dir + id + ext 派生，避免 JSONL
    可移植性问题和「手改历史指向包络外文件」的读取越权。
    """
    id: str                       # uuid4 hex，同时是文件名
    mime: str                     # image/png | image/jpeg | image/webp
    bytes: int                    # 入库后的字节数（已压缩）
    width: int
    height: int
    name: str = ""                # 原始文件名 / "剪贴板图片"，仅展示用
    kind: ImageKind = ATTACHMENT_IMAGE   # 预留：后续可能加文件/音频


class MessageBody(Base):
    content: str | None = None
    attachments: list[Attachment] | None = None   # ← 替换现有的裸 list
    ...
```

一条「图片 + 文字」的用户消息在历史里就是：

```json
{"sender":"user","body":{"content":"这个报错怎么修",
  "attachments":[{"id":"9f3c...","mime":"image/png","bytes":182734,
                  "width":1568,"height":902,"name":"截图 2026-11-15.png"}]}}
```

纯图片消息 `content` 允许为 `None` 或 `""`，**但 `attachments` 非空**，两类过滤点必须同时看两个字段：

- `display_history()`：`(m.sender == "user" and (m.body.content or m.body.attachments))`
- `_on_send()`：`if not text and not attachments: return`
- `auto_title()` / `first_user_message()`：文字为空时用首张图片的 `name`（仍为空则 `[图片]`）

### 2.2 磁盘布局

```
~/.zero/sessions/{channel}/{date}/{session_id}/
    messages.jsonl            # 只多一个 attachments 字段，文件体积几乎不变
    attachments/
        9f3c....jpg           # 送模型的规范图（≤1568px，JPEG q85）
        9f3c....thumb.jpg     # 320px 缩略图，只给历史列表用
```

- 原图**不留存**：入库即重编码覆盖，避免「原图和规范图各一份」带来的空间翻倍与隐私残留（EXIF/GPS 直出给第三方）。
- 文件名只由 `id` + mime 映射出的扩展名组成，不接受用户提供的名字参与路径，杜绝 `../` 与非法字符。
- `SessionStore.delete_session()` 已经是 `shutil.rmtree(session_dir)`，附件随会话一起消失，无需额外逻辑。

### 2.3 模型 payload 的组装点

只在 `to_openai()` 一处定义「多段 content 长什么样」，其它路径（压缩、子 agent）复用：

```python
def to_openai(self, images: dict[str, str] | None = None) -> dict | None:
    if self.sender == "user":
        parts: list[dict] = []
        if self.body.content:
            parts.append({"type": "text", "text": self.body.content})
        for att in self.body.attachments or []:
            url = (images or {}).get(att.id)
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                # 没被加载进本次 payload（旧图淘汰 / 压缩输入 / 模型无视觉）
                parts.append({"type": "text",
                              "text": f"[图片已省略：{att.name or att.id}]"})
        if all(p["type"] == "text" for p in parts):
            # 纯文本保持字符串形态：兼容所有厂商，也保住前缀缓存
            return {"role": "user",
                    "content": "\n".join(p["text"] for p in parts) or None}
        return {"role": "user", "content": parts}
    ...
```

关键点：

1. **纯文本消息绝不变形**（仍是 `content: "..."`），零兼容风险、前缀缓存不失效。
2. `images` 由调用方注入，`to_openai` 本身不做任何文件 IO——否则同步属性里会踩「async 里做同步 IO」的规约。
3. 缺图时**降级为文字占位**而不是报错：历史里被淘汰/被剥掉图的消息仍能正常参与上下文。

### 2.4 重启与重放

`load_history()` 原样返回带 `attachments` 的消息，不需要额外逻辑；文件缺失（用户手动删了 `attachments/`）时 `to_openai` 自动落到占位、UI 侧显示「图片已丢失」。**没有迁移、没有版本号**：新字段可空，旧 JSONL 直接可读。

---

## 3. 输入端

### 3.1 统一入口：AttachmentStore

新增 `zero/session/attachments.py`，由组合根创建并注入（与 `SessionStore` / `PermissionState` 同风格，不做模块级单例）：

```python
class AttachmentStore:
    def __init__(self, session_store: SessionStore):
        self.sessions = session_store
        self._encoded: OrderedDict[tuple[str, int, int], str] = OrderedDict()  # LRU

    def ingest_bytes(self, channel, session_id, data: bytes,
                     name: str = "") -> Attachment: ...
    def ingest_path(self, channel, session_id, path: Path,
                    name: str = "") -> Attachment: ...
    def data_url(self, channel, session_id, att: Attachment) -> str: ...
    def thumb_url(self, channel, session_id, att: Attachment) -> str: ...
    def gc(self, channel, session_id, keep: set[str]) -> int: ...
```

`ingest_*` 统一做：大小上限 → magic bytes 嗅探（不信任扩展名与前端声明的 mime）→ Pillow 解码（`MAX_IMAGE_PIXELS` 防解压炸弹）→ 等比缩放/重编码 → 写盘（`save_or_create_file` 原子写）→ 返回 `Attachment`。

失败一律抛 `ValueError`，文案要能直接照做（例如「图片超过 10MB 上限，请压缩后重试」），不静默丢弃。

### 3.2 Web

三个入口最终都收敛到两种上传方式：

| 入口 | 前端拿到什么 | 上传方式 | 说明 |
| --- | --- | --- | --- |
| 粘贴（⌘V） | `ClipboardEvent.clipboardData.files` | `{"type":"image","data":base64,"mime":...,"name":...}` | 剪贴板没有路径，只能传字节 |
| 拖拽 | `DragEvent.dataTransfer.files` | 同上 | 必须 `dragover`/`drop` 都 `preventDefault()`，否则 WebView 会试图导航到文件 |
| 选择文件 | `js_api.pick_images()` 返回绝对路径 | `{"type":"image","path":"/abs/x.png"}` | 走原生对话框，**不经过 base64**，最省事也最快 |

`pick_images()` 用 `webview.FileDialog.OPEN` + `file_types=('图片 (*.png;*.jpg;*.jpeg;*.webp)',)`、`allow_multiple=True`，与现有 [`pick_folder()`](../zero/channels/web/shell.py#L105-L122) 同一写法。

**安全细节（重要）**：`{"type":"image","path":...}` 若允许任意路径，等于给 WS 客户端开放了「按需读本地文件」。因此 `pick_images()` 把返回的路径登记进 `WebChannel` 的短时白名单（`dict[path, deadline]`，2 分钟、一次性），`image` op 只接受白名单内路径 + `check_readable` 通过。CLI 的 `/image` 由用户亲手输入路径，只做 `check_readable`。

协议新增（沿用现有 `type` 分发风格）：

```text
client → server
  {"type":"image","data":"<base64>","mime":"image/png","name":"clipboard.png"}
  {"type":"image","path":"/abs/x.png"}                  # 仅限 picker 白名单
  {"type":"attachment","id":"9f3c...","variant":"thumb"} # 历史按需取图
  {"type":"send","text":"...","attachments":["9f3c..."]} # 扩展既有 send

server → client
  {"type":"image","ok":true,"attachment":{"id":...,"mime":...,"bytes":...,
      "width":...,"height":...,"name":...},"thumb":"data:image/jpeg;base64,..."}
  {"type":"image","ok":false,"error":"图片超过 10MB 上限"}
  {"type":"attachment","id":"9f3c...","data":"data:image/jpeg;base64,..."}
  history 的每条消息增加 "attachments":[{...元信息...}]
```

`max_size` 从 8MB 提到 24MB（给 10MB 原图的 base64 + JSON 余量）。这是传输层上限，不是业务上限。

前端改造点（都在 `index.html` 内，保持单文件）：

- composer 内新增待发送区：缩略图 + 删除按钮 + 重试态；`state.pending = [{id, thumb, name, bytes}]`。
- `submit()`：`if (!text && !state.pending.length) return;`，发送后清空待发送区。
- 用户气泡渲染：`addUser(text, at, attachments)`，图片按网格排列，点击用 WS 拉全图（或直接浏览器新窗口打开 data URL）。
- 历史渲染：`applyHistory` 里对带附件的消息并发（限 3）请求 `variant:"thumb"`，前端按 id 缓存；点缩略图再取 `full`。
- 拖拽 / 粘贴监听挂在 composer 与 `#messages` 上；非图片文件直接提示「暂只支持图片」。
- 配置表单：models 映射行补一个 `supportsVision` 复选框（`cfgSection` 的 models 分支，[index.html:5061](../zero/channels/web/static/index.html#L5061) 附近），否则用户没法在界面上声明视觉模型。

### 3.3 CLI

新增斜杠命令（复用 `CliCommands` 的命令表）：

```
/image <绝对路径> [<绝对路径> ...]   把图片加入下一条消息
/image clear                        清空待发送图片
```

- 立即入库（而不是发送时才读），这样路径错误、格式不支持、超限都在输入时就暴露，不会等用户敲完一大段文字才失败。
- 底部工具栏显示 `· 图片 ×2`；发送后清空。
- 输入行本身不做「看起来像图片路径就自动附件」的魔法——静默改变语义比多打一个命令更糟。
- 纯图片消息：CLI 里需要允许空正文提交（`_chat_loop` 现在的 `if not text: continue` 要改为「正文为空但有待发送图片时仍可提交」）。

### 3.4 飞书（后续）

`message_type == "image"` 时 `msg.content` 是 `{"image_key": "..."}`；需要用 `im.v1.message_resource.get` 下载到字节后走同一个 `ingest_bytes`。下载发生在无人值守路径，但触发者是用户本人，不需要审批。建议放到第三阶段，与「渠道图片下行」一起做。

---

## 4. 供应商侧

### 4.1 请求格式

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "这个报错怎么修"},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}}
  ]
}
```

取舍：

| 方案 | 结论 | 理由 |
| --- | --- | --- |
| 公网 URL | 不可行 | 本地桌面应用没有可公网访问的图床；上传到第三方等于默认外泄 |
| data URL（base64 内联） | **推荐** | OpenAI-compatible 的通用子集，几乎所有支持视觉的厂商都吃这一种 |
| 独立 `base64` 字段 | 不可行 | OpenAI 协议没有这个字段；只有部分厂商（如本地 Ollama）自有扩展，不通用 |
| 直接把本地路径交给厂商 | 不可行 | 厂商读不到你的本机磁盘 |

`detail` 字段（`"low"|"high"|"auto"`）刻意**不传**：OpenAI 支持，但部分兼容网关会因未知字段 400。需要时用 `llm_call_options.extra_body` 让用户自行开启。

### 4.2 能力声明

```python
class ModelSpec(Base):
    provider: str = ''
    context_window_tokens: int = 0
    # 是否支持图片输入。默认 False = fail-closed：没声明就当不支持，
    # 发图时直接报可执行的错误，而不是等厂商返回 400 或静默丢图。
    supports_vision: bool = False
```

配置文件里是 `models.<name>.supportsVision`（`Config` 走 camelCase alias）。

校验时机：

1. **提交时**（`ChannelService.submit`）：`attachments` 非空且当前会话模型 `supports_vision == False` → 抛 `ValueError("当前模型 X 未声明支持图片输入：请在 config.json 的 models.X.supportsVision 置为 true，或切换到视觉模型")`。错误在入队前抛出，前端保留草稿与待发送图片。
2. **轮次开始时**（defense in depth）：`AgentLoop` 读到的 `MessageIn` 带附件但运行时模型不支持 → 直接把图片降级为占位并 `StreamEvent(error)`；避免历史里旧图 + 中途切模型导致整轮 400。

### 4.3 历史图片与模型切换

`Context.messages` 组装时若当前模型 `supports_vision == False`，`images` 传 `None`，所有图自动落成 `[图片已省略：...]` 文字。这样切到文本模型后老会话仍可继续聊，不会因为历史里有一张图而永久报错。

### 4.4 验证矩阵（必须实测）

| 厂商 / 模型 | data URL | 备注 |
| --- | --- | --- |
| dashscope（qwen-vl-max / qwen3-vl） | 待实测 | 兼容模式对 data URL 的支持要确认；有的版本要求 `image_url` 为 OSS URL |
| zhipu（glm-4v / glm-4.5v） | 待实测 | 一般支持 base64 |
| deepseek（deepseek-chat） | 不支持 | 无视觉能力，`supportsVision` 保持 false |
| 自定义网关（openrouter / one-api） | 视上游而定 | 由用户自行声明能力 |

这一项会直接决定 MVP 能不能只靠 data URL；如果某个目标厂商只吃 URL，退路是「本地起一个带 token 的 127.0.0.1 HTTP 端点，把 URL 指过去」——但那要求厂商能访问你的 localhost，实际只对本地推理服务有意义，不作为主线。

---

## 5. 上下文与成本

### 5.1 图片的 token 估算

图片按 prompt token 计费，且**每一轮都要重发**，必须进入现有压缩判定，否则压缩永远不触发、直到超窗 400。

```python
def estimate_image_tokens(width: int, height: int) -> int:
    """OpenAI vision(detail=high) 口径的保守估算：85 + 170 × 512px 切片数。

    切片前会把长边缩到 2048、短边缩到 768；我们在入库时已经压到 1568，
    所以直接按 512 网格数切片是上界（最多 4×4=16 片 → 2805 tokens）。
    """
    tiles = math.ceil(width / 512) * math.ceil(height / 512)
    return 85 + 170 * max(tiles, 1)
```

`estimate_message()` 追加 `sum(estimate_image_tokens(a.width, a.height) for a in m.body.attachments or [])`。只读元信息，不碰文件。

参考量级（1568px 封顶后）：

| 图片 | 切片 | 估算 tokens |
| --- | --- | --- |
| 1024×768 截图 | 2×2 | 765 |
| 1568×882 截图 | 4×2 | 1445 |
| 1568×1568 方图 | 4×4 | 2805 |

一张图 ≈ 1–3k tokens，10 张图 ≈ 一个中等长度的代码文件。所以「最多 8 张 + 只保留最近 3 轮」不是拍脑袋，是把单轮图片成本压到 ~10–20k 的量级。

### 5.2 压缩（图片必须一起处理）

- `build_compact_messages()` 调 `m.to_openai()` **不传 images** → 自动落成 `[图片已省略]`。压缩模型不需要视觉，也不需要为图付 token。
- 压缩记录本身是文字，压缩后旧图的 token 成本归零；但**文件仍在磁盘、UI 仍能看**，这符合「压缩只影响模型上下文」的既有语义。
- `find_tail_boundary()` 用 `estimate_message()`，加了图片估算后边界自然把「图片轮」算得更重，行为正确，无需改动。
- 边界安全性：图片只出现在 user 消息上，不会破坏「尾部首条不能是 tool」的不变量。

### 5.3 旧图淘汰（aging）

新增配置（`config.json` 顶层 `images`，与 `compact` 同级）：

```python
class ImageConfig(Base):
    enabled: bool = True
    max_bytes: int = 10 * 1024 * 1024     # 单张原图上限
    max_per_message: int = 8              # 单条消息图片数上限
    max_dimension: int = 1568             # 入库后的长边上限
    keep_recent_turns: int = 3            # 只把最近 N 轮的图片送进 payload
    max_in_payload: int = 8               # payload 里最多几张图（从新到旧）
    thumb_dimension: int = 320
```

生效位置在 `Context.messages` 组装前的一次纯内存扫描：从历史末尾往前数 `keep_recent_turns` 个 user 消息，只给这些消息里的图加载 data URL；再按 `max_in_payload` 从新到旧截断。**不改磁盘、不改 `Message` 对象**，只决定传给 `to_openai` 的 `images` 映射。

为什么不用「只靠压缩」：压缩阈值是窗口的 75%，在 128k 窗口下意味着图片成本可以累积到 ~96k tokens 才动手，中间每一轮都在烧钱。Aging 是成本闸门，压缩是窗口闸门，两者职责不同。

### 5.4 base64 的加载时机与缓存

`Context.messages` 是同步属性、每次迭代都会调用，**绝不能在里面对文件做 base64**。做法：

- `Context.aprepare()`（已经在线程池里跑）在加载历史之后，按 aging 规则算出需要哪些附件 id，逐个读文件 + base64，放进 `self._images: dict[str, str]`。
- 同一个 turn 内每次迭代复用同一批字符串 → 请求体字节完全一致 → 厂商侧前缀缓存不会因为「同一张图编码结果不同」而失效。
- 跨 turn 的复用交给 `AttachmentStore` 的 LRU（键 = `(路径, size, mtime_ns)`），默认 16 条，避免内存随图片数无界增长。

### 5.5 每个 turn 的请求体大小

最坏情况：8 张 1568px JPEG（每张 ~200–400KB）→ base64 ~2.5–4MB → **每次 LLM 迭代都序列化并发送一遍**。20 次迭代就是 80MB 上行。这是必须接受的成本，但要在实现时注意：

- 入库压缩比（q85）比「原图直发」小一个数量级，这是引入 Pillow 的核心理由。
- `requests` 的 `json=` 每次都重新 `dumps`，属可接受开销；如果实测明显卡顿，再考虑 `data=` 预编码 + 手工拼 body。

---

## 6. 权限与安全

| 风险 | 处理 |
| --- | --- |
| 通过 `image(path)` 读任意本地文件 | 只接受 `pick_images()` 白名单内的路径（一次性、2 分钟）；并过 `check_readable()` 挡掉 `~/.ssh`、`~/.aws`、keychain、`~/.zero/config.json` 等 |
| 粘贴/拖拽的字节绕过读权限 | 字节来自用户当前剪贴板/拖拽动作，属于用户主动输入，不构成模型越权；且没有路径可越 |
| 图片携带 EXIF/GPS 泄露给厂商 | 入库时重编码（Pillow 默认不写 EXIF），原图不留存 |
| 图片本身含敏感信息（证件、聊天截图） | 不做 OCR、不做自动上传；UI 上明确「图片会发送给当前模型厂商」（待发送区一行提示）；是否加水印/打码属于后续增强，MVP 不做 |
| 解压炸弹 / 超大像素 | `Image.MAX_IMAGE_PIXELS` + 尺寸/字节双上限，解码异常按 `ValueError` 拒绝 |
| 伪造 mime（`.png` 实为 HTML/SVG） | 只认 magic bytes + Pillow 实际解码出的格式，白名单 `PNG/JPEG/WEBP`；SVG 一律拒绝（可执行脚本 + 需要光栅化） |
| 落盘越权 | 附件目录固定在 `session_dir/attachments/`，文件名只由 `id` 生成；会话目录本来就在写包络内 |
| 图片残留 | 删会话即 `rmtree`；未发送的孤儿文件见 §7.2 |
| 历史回放拉图 | `attachment` op 只允许当前会话内、且 id 能在该会话的 attachments 目录里找到的条目 |

**不引入审批**：上传是渠道侧的用户动作，不是工具调用。让模型「自己去找图」才会走 `read` 工具并受守卫约束——这一版刻意不做「模型主动读图」的能力。

---

## 7. 清理、体积与具体改动点

### 7.1 体积

| 内容 | 量级 |
| --- | --- |
| `messages.jsonl` 每条消息新增 | ~200 字节（元信息），可忽略 |
| 单张规范图 | ~100–500KB（1568px JPEG q85，取决于内容） |
| 单张缩略图 | ~10–20KB |
| 一条 8 图消息 | ~1–4MB |

结论：会话目录会随图片线性增长，但**历史文本仍是纯文本**，会话列表、索引、压缩、摘要、维护任务全部不受影响。这是把二进制放独立文件而不是塞 JSONL 的最大收益。

### 7.2 孤儿清理

上传成功但用户没发送（关窗口、删草稿）会留下无用文件。策略：

- 入库时若 `attachments/` 文件数超过 `2 × max_per_message`，触发一次 `gc()`：读取本会话 `messages.jsonl` 的附件 id 集合，删除「不被引用且 mtime 早于 1 小时」的文件。
- 绑定/打开会话时也触发一次（成本是读一遍该会话 JSONL，与 `load_history` 同量级）。
- 不做全局定时清理任务：附件天然按会话分桶，单会话 gc 足够，且不会误删别的会话。

### 7.3 改动点清单

| 文件 | 改动 |
| --- | --- |
| `zero/models.py` | 新增 `Attachment`；`MessageBody.attachments` 换类型；`to_openai(images=...)` 组装 content 数组；`ModelSpec.supports_vision`；新增 `ImageConfig` 并挂到 `Config.images` |
| `zero/session/attachments.py`（新） | `AttachmentStore`：嗅探/解码/缩放/缩略图/原子写/`data_url` LRU/`gc` |
| `zero/session/store.py` | 新增 `attachments_dir()` 路径派生；`delete_session` 无需改（rmtree 已覆盖） |
| `zero/channels/service.py` | `make_input(text, attachments=None)`；`submit()` 校验视觉能力；`attach_bytes/attach_path` 代理；`display_history` 过滤条件补 attachments |
| `zero/channels/web/server.py` | 新增 `image` / `attachment` 两个 op；`send` 支持 `attachments`；`_on_send` 允许纯图；`_history_sync` 回传附件元信息；`max_size=24MB`；`pick_images` 白名单 |
| `zero/channels/web/shell.py` | `_BootstrapApi.pick_images()` |
| `zero/channels/web/static/index.html` | 待发送区 UI、粘贴/拖拽/选图、缩略图渲染、点击取全图、`submit()` 带 attachments、配置表单 `supportsVision` |
| `zero/channels/cli/commands.py` / `channel.py` | `/image` 命令、待发送状态、纯图提交 |
| `zero/core/context.py` | `aprepare()` 里加载并缓存 data URL；`messages` 传入 images；aging 规则；system prompt 增加一句图片说明 |
| `zero/core/compactor.py` | `estimate_image_tokens`；`estimate_message` 计入图片；压缩输入天然剥图（`to_openai()` 不传 images） |
| `zero/core/loop.py` | 轮次开始时的视觉能力兜底（historical/entry 双重防御） |
| `zero/main.py` | 组合根创建 `AttachmentStore` 并注入 `CoreRuntime` → 各 channel |
| `pyproject.toml` | 新增 `pillow>=11` |
| `tests/` | 见下 |

### 7.4 测试

- `tests/test_attachments.py`：magic bytes 伪装、超字节、超尺寸、解压炸弹、SVG 拒绝、路径由 id 派生、gc 只删未引用且过期文件。
- `tests/test_multimodal_message.py`：纯文本仍是字符串；带图是 parts；缺图落占位；`estimate_message` 计入图片。
- `tests/test_context_images.py`：aging 只加载最近 N 轮且不超过 `max_in_payload`；无视觉模型时全剥；`aprepare` 不在事件循环里做 IO（复用 `test_async_io.py` 的思路）。
- `tests/test_compactor.py`（扩展）：压缩输入里不出现 `image_url`。
- `tests/test_web_history_usage.py` 或新文件：`image` op 的三种来源、白名单过期、`attachment` 越权 id、纯图 `send`。
- `tests/test_cli_channels.py`（扩展）：`/image` 入库失败提示、纯图提交。

---

## 8. 分阶段落地

### 阶段 0：可行性验证（半天，不改仓库业务代码）

1. 在现有 pywebview 窗口里跑一段临时 JS，验证 macOS WKWebView 下 `paste` 能否拿到 `clipboardData.files`、`drop` 能否拿到 `dataTransfer.files`（pywebview 6.2.1 没有原生 drop 事件）。
2. 用 `requests` 直接打目标厂商的 `/chat/completions`，确认 data URL 被接受、`usage.prompt_tokens` 是否包含图片 token。
3. 确认 pywebview 打包（`.app`）里 Pillow 能正常 import（`packaging/` 相关流程）。

这三项任一不成立，方案都要调整（见 §9）。

### 阶段 1：MVP（Web 单渠道）

- 新增 `Attachment` / `AttachmentStore` / `ImageConfig` / `ModelSpec.supportsVision`。
- Web：粘贴 + 拖拽 + 选择文件，多图，待发送区，历史缩略图 + 点击取全图。
- `to_openai` 多段 content + 缺图占位；`estimate_message` 计图。
- 提交时的视觉能力校验与纯图消息支持。
- 不做：CLI、aging、飞书、旧图淘汰。

验收：截图粘贴 → 发送给视觉模型 → 回答正确；重启后历史仍显示图片且能点开；同一会话切到文本模型后老会话可继续聊。

### 阶段 2：成本与体验

- CLI `/image`。
- Aging（`keep_recent_turns` / `max_in_payload`）与 data URL LRU。
- 缩略图 + 大图分离（MVP 若已做则此处只补 IntersectionObserver 懒加载）。
- 压缩链路的图片剥离与 token 估算校准（用真实 `usage.prompt_tokens` 回归估算偏差）。

### 阶段 3：扩展

- 飞书图片消息（下载 `image_key` → `ingest_bytes`）。
- 原生拖拽反馈（悬停高亮、整窗 drop zone）。
- 图片标注/裁剪（发图前框选区域），以及在图片上叠加文字说明。
- 多模态下行（模型生成图片）不在本方案范围。

---

## 9. 不确定项（必须实测，不能靠推断）

| # | 不确定点 | 影响 | 验证方式 |
| --- | --- | --- | --- |
| 1 | pywebview 6.2.1 / macOS WKWebView 的粘贴事件是否带 `clipboardData.files` | 决定「粘贴」入口是否可用 | 临时 JS 打日志 |
| 2 | HTML5 drag&drop 在 pywebview 中是否被默认行为吞掉（导航/打开文件） | 决定拖拽入口是否可用 | 手动拖一张图，观察窗口是否跳转 |
| 3 | 目标厂商对 data URL 的支持（尤其 dashscope 兼容模式） | 决定是否需要退化为 URL 方案 | 直接 curl / requests |
| 4 | `usage.prompt_tokens` 中图片 token 的实际计入与 §5.1 估算的偏差 | 压缩触发点是否准确 | 用 1/2/4 张图对比实测 |
| 5 | 5MB base64 帧在 websockets 15 下的实际耗时与内存峰值 | 是否需要分片上传或降低上限 | 压测一次上传 |
| 6 | pywebview 打包产物里 Pillow 的二进制依赖是否完整 | 打包后能否用图 | 构建 `.app` 后实测 |
| 7 | macOS 从「照片」拖出的 HEIC 是否常见 | 是否需要 `pillow-heif` | 实际拖一张 HEIC 观察报错 |
| 8 | 会话目录里大量图片对 `delete_session` / 备份的影响 | 删除耗时 | 造 100 张图测一次 |

已确认的事实（不需要再验）：pywebview 6.2.1 无原生文件拖放事件；`create_file_dialog` 支持 `file_types` 与多选；WS 当前 `max_size` 为 8MB；`MessageBody.attachments` 已存在但全仓未使用。

---

## 10. 备选方案与取舍

| 备选 | 为什么不选 |
| --- | --- |
| base64 直接写进 `messages.jsonl` | 每次追加/重写/加载都要搬 MB 级字符串；`load_history` 在每轮起点跑，会直接拖慢首 token 时间；也破坏「历史是纯文本可 grep」的现状 |
| 本地 HTTP 图床（`<img src="http://127.0.0.1:port/...">`） | 需要再开一个带鉴权的 HTTP 服务，攻击面 +1；只在「厂商能访问 localhost」或「前端懒加载性能不足」时才值得，列为后续优化 |
| 把图片当文件路径交给模型，让模型用 `read` 工具看 | `read` 返回的是文本（`errors="replace"`），模型看不到像素；若给 `read` 加多模态返回，则读图变成工具调用、要走审批，体验和成本都更差 |
| 不做服务端重编码，原图直发 | Retina 截图 2880px 会被切成 6×4=24 片（~4k tokens）且请求体 4MB；还保留 EXIF/GPS。收益远小于引入 Pillow 的成本 |
| 图片只在当轮生效，不进历史 | 无法回答「刚才那张图里第二行是什么」这类追问；与现有历史语义不一致 |
| 给每个厂商写视觉适配层 | 项目明确只走 OpenAI-compatible；多一套适配层等于自建 SDK，收益不成立 |
