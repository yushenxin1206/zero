---
name: web-search
description: >
  从网络搜索特定信息。当用户需要查找最新资讯、资料、技术文档、事实核查等在线信息时触发。
  覆盖场景：查找某个主题的最新信息、搜索技术问题的解决方案、获取产品的官方文档、查询某个实体的背景资料。
  关键词：搜索、查询、查一下、搜一下、查找、最新信息、资料、资讯、search、web search、找一下、帮我查
---

# Web Search（网络搜索）

## 使用时机（Usage / When to use）

- 当用户需要从**互联网**获取实时或最新信息时触发。
- 覆盖范围：
  - 搜索某个关键词/主题的最新资料与资讯
  - 查找技术问题的解决方案或官方文档
  - 事实核查 / 获取某个实体（人、公司、产品）的背景资料
  - 查询新闻事件、价格、规格等需要联网的信息
- **不要** 在以下情况使用：
  - 用户只需要基于已有知识回答的通用问题
  - 用户明确要求离线或本地操作

## 执行流程（Workflow / Runtime contract）

1. **解析查询意图**：明确用户的搜索关键词、语言（中文/英文）、期望结果数量（默认 5 条）。
2. **执行搜索**：调用 `search` 工具（基于 DuckDuckGo HTML 接口），传入查询词。
3. **筛选结果**：去除广告、无效链接，保留最相关的条目（标题 + URL + 摘要）。
4. **返回结果**：按相关度列出结果，附来源链接；若信息重要，可简要总结。

## 示例（Examples）

### 示例 1：普通搜索

用户问："帮我查一下 Python 最新的稳定版本"

1. 解析：关键词 `python latest stable version`，语言英文，数量 5。
2. 调用搜索工具获取结果。
3. 筛选出官网（python.org）等权威来源。
4. 返回版本号并附来源链接。

### 示例 2：中文搜索

用户问："搜一下今年新能源汽车销量排名"

1. 解析：关键词 `新能源汽车 销量 排名 2025`，语言中文，数量 5。
2. 调用搜索工具。
3. 保留权威媒体/数据来源。
4. 返回排名摘要与来源链接。

## 搜索工具（Search Tool）

实际搜索通过以下命令执行（Bing 网页搜索，无需 API key）：

```
python <本 skill 目录>/scripts/search.py "QUERY" [结果数量]
```

用**绝对路径**调用：`<本 skill 目录>` 取 `<available_skills>` 中本 skill 的
`<location>` 所在目录，自行拼出 `scripts/search.py` 的绝对路径，不要写相对路径。

> **为什么必须绝对路径**：shell_guard 的内置豁免（`builtin-skill-script`）只认
> 「python 解释器 + 落在包内 `<skill>/scripts/` 下的 .py 绝对路径」。写成相对路径
> `python scripts/search.py ...` 时豁免不生效，每次执行都会触发人工审批。

> **裸调，不要加任何命令前缀**：不要写成 `set PYTHONIOENCODING=utf-8 && python ...`
> 或 `call python ...`——豁免要求命令的**首 token 就是 python 解释器**，加了
> `set` / `chcp` / `call` 等前缀后首 token 变成别的程序，豁免失效会触发人工审批。
> 编码问题已在脚本内用 `setup_stdio()` 解决（强制 UTF-8 输出 + 控制台代码页
> 65001），无需外部设置。

脚本通过携带 `User-Agent` 请求头抓取 Bing 搜索结果页，解析标题、真实 URL 和摘要，并自动解码 Bing 的跳转链接。

## 参考资源（Reference Resources）

| 主题 | 使用时机 | 文件路径 |
|-------|------------|------|
| 搜索执行脚本 | 执行实际搜索时使用 | `scripts/search.py` |
| 概述 | 需要了解本 skill 的整体流程 | `references/overview.md` |

## 补充知识（Supplementary Knowledge）

**无 API key 的搜索方案**

- Bing 网页搜索：`https://www.bing.com/search?q=...`，免费无需密钥，可直接抓取（需携带 `User-Agent`）。
- DuckDuckGo HTML 端点：`https://html.duckduckgo.com/html/?q=...`，免费，但可能触发反爬（本机测试被拦截）。

**增强可靠性**

- 始终携带 `User-Agent` 请求头模拟浏览器。
- 设置合理的超时（如 15 秒）。
- Bing 结果链接是跳转链接（`bing.com/ck/a`），需解码 `u` 参数得到真实 URL（脚本已处理）。

## 注意事项（Notes）

- 始终使用用户提问的语言回复（中文问题用中文，英文问题用英文）。
- 包含原始来源 URL，方便用户核实。
- 若搜索无有效结果，明确告知并建议更换关键词。
- 尊重版权与隐私，不抓取需登录或付费墙后的内容。
