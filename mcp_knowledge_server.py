"""AI 知识库 MCP Server。

提供基于 stdio 的 JSON-RPC 2.0 MCP 服务，支持搜索和查询本地知识库文章。
无第三方依赖，仅使用 Python 标准库。

使用方式：
    python mcp_knowledge_server.py

协议：JSON-RPC 2.0 over stdio
MCP 方法：initialize, tools/list, tools/call
"""

import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_knowledge_server")

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge" / "articles"

SERVER_NAME = "ai-knowledge-base"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

TOOLS_SPEC = [
    {
        "name": "search_articles",
        "description": "按关键词搜索文章标题和摘要，返回匹配的知识条目列表。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，在标题和摘要中匹配（不区分大小写）",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回结果数量上限，默认 5",
                    "default": 5,
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "get_article",
        "description": "按文章 ID 获取完整内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {
                    "type": "string",
                    "description": "文章的唯一标识，如 github-20260422-openclaw-openclaw",
                },
            },
            "required": ["article_id"],
        },
    },
    {
        "name": "knowledge_stats",
        "description": "返回知识库统计信息，包括文章总数、来源分布和热门标签。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _load_all_articles() -> list[dict[str, Any]]:
    """加载 knowledge/articles/ 下所有 JSON 文件。

    Returns:
        文章字典列表，解析失败的文件会被跳过并记录警告。
    """
    articles: list[dict[str, Any]] = []
    if not KNOWLEDGE_DIR.is_dir():
        logger.warning("知识库目录不存在: %s", KNOWLEDGE_DIR)
        return articles

    for json_file in sorted(KNOWLEDGE_DIR.glob("*.json")):
        try:
            with open(json_file, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    articles.append(data)
                elif isinstance(data, list):
                    articles.extend(item for item in data if isinstance(item, dict))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("跳过无效文件 %s: %s", json_file.name, exc)

    logger.debug("共加载 %d 篇文章", len(articles))
    return articles


def _search_articles(keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    """按关键词搜索文章标题和摘要。

    Args:
        keyword: 搜索关键词，不区分大小写。
        limit: 返回结果数量上限。

    Returns:
        匹配的文章字典列表，每项包含 id、title、source_type、tags、importance。
    """
    articles = _load_all_articles()
    keyword_lower = keyword.lower()
    results: list[dict[str, Any]] = []

    for article in articles:
        title = article.get("title", "").lower()
        summary = article.get("summary", "").lower()
        if keyword_lower in title or keyword_lower in summary:
            results.append(
                {
                    "id": article.get("id", ""),
                    "title": article.get("title", ""),
                    "source_type": article.get("source_type", ""),
                    "tags": article.get("tags", []),
                    "importance": article.get("importance", ""),
                    "summary": article.get("summary", ""),
                }
            )
            if len(results) >= limit:
                break

    return results


def _get_article(article_id: str) -> dict[str, Any] | None:
    """按 ID 获取文章完整内容。

    Args:
        article_id: 文章唯一标识。

    Returns:
        文章完整字典，未找到时返回 None。
    """
    articles = _load_all_articles()
    for article in articles:
        if article.get("id") == article_id:
            return article
    return None


def _knowledge_stats() -> dict[str, Any]:
    """返回知识库统计信息。

    Returns:
        包含 total、by_source、top_tags 的统计字典。
    """
    articles = _load_all_articles()
    source_counter: Counter[str] = Counter()
    tag_counter: Counter[str] = Counter()

    for article in articles:
        source = article.get("source_type", "unknown")
        source_counter[source] += 1
        for tag in article.get("tags", []):
            tag_counter[tag] += 1

    return {
        "total": len(articles),
        "by_source": dict(source_counter),
        "top_tags": tag_counter.most_common(20),
    }


def _handle_initialize(params: dict[str, Any] | None) -> dict[str, Any]:
    """处理 MCP initialize 请求。"""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        },
    }


def _handle_tools_list() -> dict[str, Any]:
    """处理 MCP tools/list 请求。"""
    return {"tools": TOOLS_SPEC}


def _handle_tools_call(name: str, arguments: dict[str, Any]) -> Any:
    """处理 MCP tools/call 请求。

    Args:
        name: 工具名称。
        arguments: 工具参数字典。

    Returns:
        工具执行结果。

    Raises:
        ValueError: 工具名称未知时抛出。
    """
    if name == "search_articles":
        keyword = arguments.get("keyword", "")
        limit = arguments.get("limit", 5)
        return _search_articles(keyword, limit)

    if name == "get_article":
        article_id = arguments.get("article_id", "")
        result = _get_article(article_id)
        if result is None:
            return {"error": f"未找到文章: {article_id}"}
        return result

    if name == "knowledge_stats":
        return _knowledge_stats()

    raise ValueError(f"未知工具: {name}")


def _make_response(request_id: Any, result: Any) -> dict[str, Any]:
    """构造 JSON-RPC 成功响应。"""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _make_error_response(
    request_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """构造 JSON-RPC 错误响应。"""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _process_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """处理单个 JSON-RPC 请求。

    Args:
        request: 解析后的 JSON-RPC 请求字典。

    Returns:
        JSON-RPC 响应字典，通知类请求返回 None。
    """
    request_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "initialize":
        return _make_response(request_id, _handle_initialize(params))

    if method == "initialized":
        return None

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _make_response(request_id, _handle_tools_list())

    if method == "tools/call":
        tool_name = params.get("name", "") if isinstance(params, dict) else ""
        tool_args = params.get("arguments", {}) if isinstance(params, dict) else {}
        try:
            result = _handle_tools_call(tool_name, tool_args)
            return _make_response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False, indent=2),
                        }
                    ],
                    "isError": False,
                },
            )
        except ValueError as exc:
            return _make_error_response(request_id, -32602, str(exc))
        except Exception as exc:
            logger.exception("工具调用异常")
            return _make_error_response(request_id, -32603, f"内部错误: {exc}")

    return _make_error_response(request_id, -32601, f"未知方法: {method}")


def _write_response(response: dict[str, Any]) -> None:
    """将 JSON-RPC 响应写入 stdout（换行分隔的 JSON）。"""
    body = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(body + "\n")
    sys.stdout.flush()


def _read_request() -> dict[str, Any] | None:
    """从 stdin 读取 JSON-RPC 请求。

    支持两种模式：
    1. 带 Content-Length 头的消息（MCP 标准模式）
    2. 每行一个 JSON 对象（简易模式）

    Returns:
        解析后的请求字典，EOF 时返回 None。
    """
    line = sys.stdin.readline()
    if not line:
        return None

    line = line.strip()
    if not line:
        return None

    if line.lower().startswith("content-length:"):
        content_length = int(line.split(":", 1)[1].strip())
        sys.stdin.readline()
        body = sys.stdin.read(content_length)
        return json.loads(body)

    return json.loads(line)


def main() -> None:
    """启动 MCP Server，循环读取 stdin 并处理请求。"""
    logger.info(
        "MCP Server 启动: %s v%s, 知识库路径: %s",
        SERVER_NAME,
        SERVER_VERSION,
        KNOWLEDGE_DIR,
    )

    while True:
        try:
            request = _read_request()
        except json.JSONDecodeError as exc:
            logger.warning("JSON 解析失败: %s", exc)
            continue
        except EOFError:
            logger.info("输入流关闭，退出")
            break

        if request is None:
            logger.info("输入流关闭，退出")
            break

        logger.debug("收到请求: %s", request.get("method"))

        response = _process_request(request)
        if response is not None:
            _write_response(response)


if __name__ == "__main__":
    main()
