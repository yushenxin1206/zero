# Web Search Skill 概览

## 目标
让助手能够从互联网搜索特定信息并返回结构化结果，无需任何 API key。

## 实现方案
- 使用 **DuckDuckGo HTML 端点**（`https://html.duckduckgo.com/html/?q=...`）进行搜索。
- 用 Python 抓取页面并用正则解析结果标题、链接和摘要。
- 携带 `User-Agent` 请求头以提高成功率。

## 使用方式
1. 确认用户搜索意图与语言。
2. 执行 `scripts/search.py`：
   ```
   python search.py "关键词" [结果数量]
   ```
3. 读取输出，向用户展示结果并附上来源 URL。

## 依赖
- Python 3.x（标准库即可，无需第三方包）
- 网络连接
