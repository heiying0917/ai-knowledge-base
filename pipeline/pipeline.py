#!/usr/bin/env python3
"""四步知识库自动化流水线。

Step 1 采集（Collect） — 从 GitHub Search API 和 RSS 源采集 AI 相关内容
Step 2 分析（Analyze） — 调用 LLM 对每条内容进行摘要/评分/标签分析
Step 3 整理（Organize） — 去重 + 格式标准化 + 校验
Step 4 保存（Save）   — 将文章保存为独立 JSON 文件到 knowledge/articles/

用法:
    python pipeline/pipeline.py --sources github,rss --limit 20
    python pipeline/pipeline.py --sources github --limit 5
    python pipeline/pipeline.py --sources rss --limit 10
    python pipeline/pipeline.py --sources github --limit 5 --dry-run
    python pipeline/pipeline.py --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

try:
    from pipeline.model_client import chat_with_retry, create_provider
except ImportError:
    from model_client import chat_with_retry, create_provider

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
RAW_DIR = KNOWLEDGE_DIR / "raw"
ARTICLES_DIR = KNOWLEDGE_DIR / "articles"

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_AI_QUERIES: list[str] = [
    "topic:ai",
    "topic:llm",
    "topic:machine-learning",
    "topic:deep-learning",
    "topic:nlp",
]


def _build_github_queries() -> list[str]:
    """构建带时间窗口的 GitHub 搜索查询列表。

    为每个 topic 添加 created/pushed 日期过滤器，
    确保每次采集都能发现新项目而非老面孔。

    Returns:
        带日期参数的查询字符串列表。
    """
    since_created = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
        "%Y-%m-%d"
    )
    since_pushed = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    queries: list[str] = []
    for topic_query in GITHUB_AI_QUERIES:
        queries.append(f"{topic_query} created:>{since_created}")
        queries.append(f"{topic_query} pushed:>{since_pushed}")
    return queries


RSS_SOURCES_PATH = PROJECT_ROOT / "pipeline" / "rss_sources.yaml"

REQUEST_TIMEOUT = 30.0
COLLECT_MAX_RETRIES = 3


def load_rss_sources(config_path: Path | None = None) -> list[dict[str, str]]:
    """从 YAML 配置文件加载启用的 RSS 数据源。

    Args:
        config_path: YAML 配置文件路径，为 None 时使用默认路径。

    Returns:
        启用的 RSS 源列表，每项包含 name、url、category 字段。

    Raises:
        FileNotFoundError: 配置文件不存在时抛出。
        ValueError: yaml 模块未安装时抛出。
    """
    path = config_path or RSS_SOURCES_PATH

    if not path.exists():
        logger.warning("RSS 配置文件不存在: %s", path)
        return []

    if yaml is None:
        raise ValueError("需要 pyyaml 库来加载 RSS 配置: pip install pyyaml")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    sources = data.get("sources", []) if isinstance(data, dict) else []

    enabled = [
        {
            "name": s["name"],
            "url": s["url"],
            "category": s.get("category", ""),
        }
        for s in sources
        if s.get("enabled") and s.get("name") and s.get("url")
    ]

    logger.debug("加载 RSS 配置: %d/%d 个源已启用", len(enabled), len(sources))
    return enabled


@dataclass
class CollectResult:
    """单次采集的结构化结果。

    Attributes:
        items: 成功采集的数据条目列表。
        source: 来源标识，如 "github" 或 "rss:hackernews"。
        success: 请求本身是否成功（即使返回 0 条数据也算 True）。
        error: 失败原因描述，成功时为 None。
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""
    success: bool = True
    error: str | None = None


VALID_CATEGORIES: set[str] = {"model-release", "paper", "tool", "tutorial", "industry"}
VALID_IMPORTANCE: set[str] = {"high", "medium", "low"}
VALID_SOURCE_TYPES: set[str] = {"github_trending", "hacker_news", "rss"}

AI_KEYWORDS: set[str] = {
    "ai",
    "artificial intelligence",
    "llm",
    "large language model",
    "gpt",
    "agent",
    "rag",
    "transformer",
    "deep learning",
    "machine learning",
    "nlp",
    "natural language",
    "diffusion",
    "multimodal",
    "inference",
    "fine-tun",
    "openai",
    "deepseek",
    "anthropic",
    "claude",
    "gemini",
    "reasoning",
    "reinforcement",
    "embedding",
    "tokenizer",
    "mcp",
    "model context protocol",
    "copilot",
}


def _is_ai_related(text: str) -> bool:
    """判断文本是否与 AI 相关。

    Args:
        text: 待检测文本，统一转小写匹配。

    Returns:
        命中任意关键词返回 True。非字符串输入返回 False。
    """
    if not isinstance(text, str):
        return False
    lower = text.lower()
    return any(kw in lower for kw in AI_KEYWORDS)


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_seq(source: str, date_part: str) -> int:
    """扫描 articles/ 和 raw/ 中同来源同日期已有文件，返回下一个可用编号。

    支持两种文件格式:
        - 批量文件: {source}-YYYY-MM-DD-HHMM.json（内部 JSON 数组）
        - 单条文件: {source}-{YYYYMMDD}-{NNN}.json

    Args:
        source: 来源缩写，如 github、rss。
        date_part: 日期字符串 YYYYMMDD。

    Returns:
        下一个可用编号（从 1 开始）。
    """
    max_seq = 0
    id_pattern = re.compile(rf"^{re.escape(source)}-{date_part}-(\d{{3}})")

    for directory in (ARTICLES_DIR, RAW_DIR):
        if not directory.exists():
            continue
        for fp in directory.glob(f"{source}-*.json"):
            for item_id in _extract_ids_from_file(fp):
                m = id_pattern.match(item_id)
                if m:
                    max_seq = max(max_seq, int(m.group(1)))
            m = id_pattern.match(fp.stem)
            if m:
                max_seq = max(max_seq, int(m.group(1)))

    return max_seq + 1


def _extract_ids_from_file(filepath: Path) -> list[str]:
    """从 JSON 文件中提取所有条目的 id 字段。

    Args:
        filepath: JSON 文件路径。

    Returns:
        id 字符串列表。
    """
    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = data if isinstance(data, list) else [data]
    return [
        item.get("id", "")
        for item in items
        if isinstance(item, dict) and item.get("id")
    ]


def _generate_id(source: str, _identifier: str = "", *, seq: int | None = None) -> str:
    """生成知识条目 ID。

    格式: {source}-{YYYYMMDD}-{NNN}，如 github-20260422-001。

    Args:
        source: 来源缩写，如 github、rss。
        _identifier: 未使用，保留接口兼容。
        seq: 指定编号，为 None 时自动从文件系统扫描获取下一个。

    Returns:
        符合格式要求的 ID 字符串。
    """
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    if seq is None:
        seq = _next_seq(source, date_part)
    return f"{source}-{date_part}-{seq:03d}"


# ---------------------------------------------------------------------------
# Step 1: 采集（Collect）
# ---------------------------------------------------------------------------


def _http_get_with_retry(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_retries: int = COLLECT_MAX_RETRIES,
    follow_redirects: bool = False,
) -> httpx.Response | None:
    """带重试的 HTTP GET 请求。

    仅对 429、5xx 和超时进行重试，4xx（非 429）直接抛出。
    指数退避：1s → 2s → 4s。

    Args:
        url: 请求 URL。
        params: 查询参数。
        headers: 请求头。
        max_retries: 最大重试次数。
        follow_redirects: 是否跟随重定向。

    Returns:
        成功的 Response 对象，重试耗尽返回 None。

    Raises:
        httpx.HTTPStatusError: 遇到非可重试的 HTTP 错误时抛出。
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT, follow_redirects=follow_redirects
            ) as client:
                resp = client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                return resp
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            status = exc.response.status_code
            if status == 429 or status >= 500:
                if attempt < max_retries:
                    wait = 2**attempt
                    logger.warning(
                        "HTTP GET 失败 (%d) %s，第 %d/%d 次重试，等待 %ds...",
                        status,
                        url[:80],
                        attempt + 1,
                        max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = 2**attempt
                logger.warning(
                    "HTTP GET 网络错误 %s，第 %d/%d 次重试，等待 %ds...",
                    url[:80],
                    attempt + 1,
                    max_retries,
                    wait,
                )
                time.sleep(wait)
                continue
            raise

    if last_exc is not None:
        logger.error("HTTP GET 重试耗尽: %s | %s", url[:80], last_exc)
    return None


def collect_github(limit: int = 20) -> CollectResult:
    """从 GitHub Search API 采集 AI 相关热门仓库。

    使用多组精准查询词轮换搜索，避免单一宽泛查询导致零结果。
    无 Token 时注意 GitHub Search API 限制为 10 次/分钟。

    Args:
        limit: 最大采集条数。

    Returns:
        CollectResult 实例，包含采集到的条目和请求状态。
    """
    queries = _build_github_queries()
    logger.info("采集 GitHub: 多查询轮换 (limit=%d, %d 组查询)", limit, len(queries))

    seen_urls: set[str] = set()
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    has_failure = False

    per_query = max(3, limit // len(queries) + 1)

    for query in queries:
        if len(items) >= limit:
            break

        remaining = limit - len(items)
        per_page = min(per_query, remaining, 30)

        logger.debug("GitHub 查询: q=%s, per_page=%d", query, per_page)

        try:
            resp = _http_get_with_retry(
                GITHUB_SEARCH_URL,
                params={
                    "q": query,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": per_page,
                },
                headers={
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "ai-knowledge-base/1.0",
                },
            )
        except httpx.HTTPStatusError as exc:
            has_failure = True
            errors.append(f"HTTP {exc.response.status_code} (q={query})")
            logger.warning("GitHub 查询失败: q=%s | %s", query, exc)
            continue
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            has_failure = True
            errors.append(f"{type(exc).__name__} (q={query})")
            logger.warning("GitHub 查询失败: q=%s | %s", query, exc)
            continue

        if resp is None:
            has_failure = True
            errors.append(f"重试耗尽 (q={query})")
            continue

        repo_list = resp.json().get("items", [])
        logger.debug("GitHub 查询 q=%s 返回 %d 条", query, len(repo_list))

        for repo in repo_list:
            if len(items) >= limit:
                break

            url = repo.get("html_url", "")
            if url in seen_urls:
                continue

            desc = repo.get("description") or ""
            topics = " ".join(repo.get("topics", []))

            if not _is_ai_related(f"{desc} {repo.get('name', '')} {topics}"):
                logger.debug("跳过非 AI 相关: %s", repo.get("full_name"))
                continue

            seen_urls.add(url)
            items.append(
                {
                    "source_type": "github_trending",
                    "source_url": url,
                    "title": repo.get("full_name", ""),
                    "description": desc,
                    "metadata": {
                        "stars": repo.get("stargazers_count", 0),
                        "language": repo.get("language"),
                        "topics": repo.get("topics", []),
                        "created_at": repo.get("created_at", ""),
                        "updated_at": repo.get("updated_at", ""),
                    },
                    "collected_at": _now_iso(),
                }
            )

    logger.info("GitHub 采集完成: %d 条 AI 相关内容", len(items))
    return CollectResult(
        items=items,
        source="github",
        success=not has_failure,
        error="; ".join(errors) if errors else None,
    )


def _parse_rss_feed(
    feed_name: str, url: str, limit: int, category: str = ""
) -> CollectResult:
    """解析单个 RSS 源并提取条目。

    使用 xml.etree.ElementTree 解析 RSS XML，
    支持 RSS 2.0 格式的 <item> 元素提取。
    请求失败时自动重试（429/5xx/超时），最多 3 次。

    Args:
        feed_name: RSS 源名称标识。
        url: RSS 订阅地址。
        limit: 最大提取条数。
        category: 数据源分类标签。

    Returns:
        CollectResult 实例，包含解析到的条目和请求状态。
    """
    source_label = f"rss:{feed_name}"

    try:
        resp = _http_get_with_retry(
            url,
            headers={"User-Agent": "ai-knowledge-base/1.0"},
            follow_redirects=True,
        )
    except httpx.HTTPStatusError as exc:
        error_msg = f"RSS [{feed_name}] HTTP {exc.response.status_code}"
        logger.error("%s: %s", error_msg, exc)
        return CollectResult(source=source_label, success=False, error=error_msg)
    except httpx.TimeoutException:
        error_msg = f"RSS [{feed_name}] 请求超时"
        logger.error(error_msg)
        return CollectResult(source=source_label, success=False, error=error_msg)
    except httpx.TransportError:
        error_msg = f"RSS [{feed_name}] 网络连接失败"
        logger.error(error_msg)
        return CollectResult(source=source_label, success=False, error=error_msg)

    if resp is None:
        error_msg = f"RSS [{feed_name}] 重试耗尽"
        return CollectResult(source=source_label, success=False, error=error_msg)

    try:
        root = ElementTree.fromstring(resp.text)
    except ElementTree.ParseError as exc:
        error_msg = f"RSS [{feed_name}] XML 解析失败"
        logger.error("%s: %s", error_msg, exc)
        return CollectResult(source=source_label, success=False, error=error_msg)

    items: list[dict[str, Any]] = []

    atom_ns = "http://www.w3.org/2005/Atom"
    entry_tags = ("item", f"{{{atom_ns}}}entry")

    for item_elem in root.iter():
        if len(items) >= limit:
            break

        raw_tag = item_elem.tag
        if not (raw_tag == "item" or raw_tag == f"{{{atom_ns}}}entry"):
            continue

        title = ""
        link = ""
        description = ""

        for child in item_elem:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "title":
                title = child.text or ""
            elif tag == "link":
                if child.text:
                    link = child.text
                elif child.get("href"):
                    link = child.get("href", "")
            elif tag in ("description", "summary", "content"):
                if not description:
                    description = child.text or ""

        clean_desc = re.sub(r"<[^>]+>", "", description).strip()

        if not _is_ai_related(f"{title} {clean_desc}"):
            logger.debug("跳过非 AI 相关 RSS 条目: %s", title[:50])
            continue

        items.append(
            {
                "source_type": "rss",
                "source_url": link,
                "title": title,
                "description": clean_desc[:500],
                "metadata": {
                    "feed_name": feed_name,
                    "category": category,
                },
                "collected_at": _now_iso(),
            }
        )

    logger.info("RSS [%s] 提取 %d 条 AI 相关内容", feed_name, len(items))
    return CollectResult(items=items, source=source_label)


def collect_rss(limit: int = 20) -> CollectResult:
    """从 RSS 源采集 AI 相关内容。

    从 rss_sources.yaml 加载 enabled 的数据源并逐个采集。
    部分源失败不影响其他源的采集。

    Args:
        limit: 最大采集条数。

    Returns:
        CollectResult 实例，合并所有源的结果。部分失败时 success=False。
    """
    feeds = load_rss_sources()
    if not feeds:
        logger.warning("无可用 RSS 源（检查 rss_sources.yaml 配置）")
        return CollectResult(source="rss", success=False, error="无可用 RSS 源")

    logger.info("采集 RSS: %d 个已启用订阅源 (limit=%d)", len(feeds), limit)
    all_items: list[dict[str, Any]] = []
    has_failure = False
    errors: list[str] = []
    remaining = limit

    for feed in feeds:
        if remaining <= 0:
            break
        result = _parse_rss_feed(
            feed["name"], feed["url"], remaining, category=feed.get("category", "")
        )
        all_items.extend(result.items)
        remaining -= len(result.items)
        if not result.success:
            has_failure = True
            if result.error:
                errors.append(result.error)

    logger.info("RSS 采集完成: 共 %d 条", len(all_items))
    return CollectResult(
        items=all_items,
        source="rss",
        success=not has_failure,
        error="; ".join(errors) if errors else None,
    )


def step_collect(sources: list[str], limit: int) -> list[dict[str, Any]]:
    """执行采集步骤。

    根据指定的来源列表，分别调用对应的采集函数。
    部分来源失败不影响其他来源的采集结果。

    Args:
        sources: 来源列表，每项为 "github" 或 "rss"。
        limit: 每个来源的最大采集条数。

    Returns:
        合并后的原始数据列表。
    """
    logger.info("=" * 50)
    logger.info("Step 1: 采集 (Collect)")
    logger.info("来源: %s | 每源上限: %d", ", ".join(sources), limit)
    logger.info("=" * 50)

    all_items: list[dict[str, Any]] = []
    results: list[CollectResult] = []

    for source in sources:
        if source == "github":
            result = collect_github(limit)
        elif source == "rss":
            result = collect_rss(limit)
        else:
            logger.warning("未知来源: %s，跳过", source)
            continue
        results.append(result)
        all_items.extend(result.items)

    for r in results:
        status = "成功" if r.success else "失败"
        logger.info(
            "  %-15s %s: %d 条%s",
            r.source,
            status,
            len(r.items),
            f" ({r.error})" if r.error else "",
        )

    if not all_items:
        any_success = any(r.success for r in results)
        if not any_success and results:
            logger.error("所有来源请求均失败，流水线终止")
        else:
            logger.warning("采集结果为空（所有条目均未命中 AI 关键词），流水线终止")

    logger.info("采集总计: %d 条原始数据", len(all_items))
    return all_items


# ---------------------------------------------------------------------------
# Step 2: 分析（Analyze）
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM_PROMPT = """你是一个 AI/LLM 领域的技术分析专家。请对给定的技术内容进行分析，严格按以下 JSON 格式返回，不要包含任何其他文字：

{
  "title": "中文标题，简洁概括（不超过50字）",
  "summary": "中文摘要，≤50字，公式：项目名 + 做了什么 + 核心差异点",
  "tags": ["tag1", "tag2", "tag3"],
  "category": "model-release/paper/tool/tutorial/industry 之一",
  "importance": "high/medium/low 之一",
  "score": 7,
  "highlights": [
    "用具体指标或方法名称描述的第一个技术亮点",
    "用具体指标或方法名称描述的第二个技术亮点"
  ],
  "score_reason": "简短说明为什么是这个分数而不是更高或更低"
}

分类说明:
- model-release: 新模型发布或重大更新
- paper: 学术论文或研究报告
- tool: 开发工具、框架、库
- tutorial: 教程、指南、最佳实践
- industry: 行业动态、产品发布、商业新闻

评分说明 (score 1-10):
- 8-10: 重大突破或极高频关注
- 5-7: 有价值的进展或工具
- 1-4: 常规更新或小众内容

标签从以下集合中选取（可添加新标签但优先使用标准标签）:
llm, agent, tool, framework, open-source, paper, reasoning, multimodal,
inference, training, evaluation, safety, rag, mcp, api, code, robotics,
openai, deepseek, anthropic, knowledge-distillation, model-release,
tutorial, industry, dataset, benchmark

摘要要求:
- ≤50字硬上限，宁短勿长
- 公式：项目名 + 做了什么 + 核心差异点
- 技术术语保留英文

亮点要求:
- 2-3条，必须引用具体指标、方法名称或技术路线
- 禁止空洞描述如"技术先进"、"性能优秀"

评分理由要求:
- 说明为什么是这个分数而不是更高或更低"""


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    """从 LLM 回复中提取 JSON 对象。

    支持直接 JSON、markdown 代码块包裹、以及混合文本中的 JSON。

    Args:
        raw: LLM 返回的原始文本。

    Returns:
        解析后的字典，解析失败返回 None。
    """
    text = raw.strip()

    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if json_match:
        text = json_match.group(1).strip()

    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        text = brace_match.group(0)

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    return None


def step_analyze(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """执行分析步骤。

    对每条原始数据调用 LLM 生成中文摘要、标签、分类和评分。
    LLM 调用失败时保留原始 description 作为摘要，标记为低质量。

    Args:
        raw_items: 采集步骤产出的原始数据列表。

    Returns:
        分析后的知识条目列表，每条包含 LLM 生成的分析字段。
    """
    logger.info("=" * 50)
    logger.info("Step 2: 分析 (Analyze)")
    logger.info("待分析: %d 条", len(raw_items))
    logger.info("=" * 50)

    provider = None
    try:
        provider = create_provider()
        logger.info(
            "LLM 提供商: %s, 模型: %s", provider.provider_name, provider.default_model
        )
    except ValueError as exc:
        logger.warning("LLM 初始化失败: %s，将跳过 AI 分析", exc)

    analyzed: list[dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    source_seq: dict[str, int] = {}

    for i, item in enumerate(raw_items):
        source = item.get("source_type", "unknown")
        title = item.get("title", "")
        desc = item.get("description", "")
        url = item.get("source_url", "")
        collected = item.get("collected_at", _now_iso())

        logger.info("[%d/%d] 分析: %s", i + 1, len(raw_items), title[:60])

        analysis_result = _analyze_single(provider, title, desc, url)

        source_prefix = "github" if source == "github_trending" else "rss"
        source_seq[source_prefix] = source_seq.get(source_prefix, 0) + 1
        entry_id = _generate_id(source_prefix, seq=source_seq[source_prefix])

        entry = {
            "id": entry_id,
            "title": analysis_result.get("title", title) if analysis_result else title,
            "source_url": url,
            "source_type": source,
            "summary": analysis_result.get("summary", desc)
            if analysis_result
            else desc,
            "tags": analysis_result.get("tags", []) if analysis_result else [],
            "category": analysis_result.get("category", "tool")
            if analysis_result
            else "tool",
            "importance": analysis_result.get("importance", "low")
            if analysis_result
            else "low",
            "status": "analyzed" if analysis_result else "raw",
            "language": "zh-CN",
            "collected_at": collected,
            "analyzed_at": _now_iso() if analysis_result else None,
            "metadata": item.get("metadata", {}),
        }

        if analysis_result:
            entry["analysis"] = {
                "highlights": analysis_result.get("highlights", []),
                "score": analysis_result.get("score", 0),
                "score_reason": analysis_result.get("score_reason", ""),
            }
            success_count += 1
        else:
            fail_count += 1

        analyzed.append(entry)

    logger.info("分析完成: 成功 %d, 跳过 %d", success_count, fail_count)
    return analyzed


def _analyze_single(
    provider: Any,
    title: str,
    description: str,
    url: str,
) -> dict[str, Any] | None:
    """对单条内容调用 LLM 进行分析。

    Args:
        provider: LLM 提供商实例，为 None 时跳过分析。
        title: 内容标题。
        description: 内容描述。
        url: 来源链接。

    Returns:
        LLM 返回的分析结果字典，失败返回 None。
    """
    if provider is None:
        return None

    content_text = f"标题: {title}\n链接: {url}\n描述: {description}"
    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": content_text},
    ]

    try:
        resp = chat_with_retry(
            messages, provider=provider, temperature=0.3, max_tokens=600
        )
        result = _parse_llm_json(resp.content)
        if result:
            logger.debug("LLM 分析成功: %s → %s", title[:30], result.get("category"))
            return result
        logger.warning("LLM 返回内容无法解析为 JSON: %s", resp.content[:100])
        return None
    except Exception as exc:
        logger.warning("LLM 分析失败: %s | %s", title[:40], exc)
        return None


# ---------------------------------------------------------------------------
# Step 3: 整理（Organize）
# ---------------------------------------------------------------------------


def step_organize(analyzed_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """执行整理步骤。

    包括 URL 去重、字段校验、分类/重要性标准化。

    Args:
        analyzed_items: 分析步骤产出的知识条目列表。

    Returns:
        去重和标准化后的知识条目列表。
    """
    logger.info("=" * 50)
    logger.info("Step 3: 整理 (Organize)")
    logger.info("待整理: %d 条", len(analyzed_items))
    logger.info("=" * 50)

    seen_urls: set[str] = set()
    unique: list[dict[str, Any]] = []
    dup_count = 0

    for item in analyzed_items:
        url = item.get("source_url", "")
        if url in seen_urls:
            dup_count += 1
            logger.debug("去重跳过: %s", url)
            continue
        seen_urls.add(url)
        unique.append(item)

    for item in unique:
        _normalize_item(item)

    valid: list[dict[str, Any]] = []
    for item in unique:
        errors = _validate_item(item)
        if errors:
            logger.warning("条目 %s 校验问题: %s", item.get("id"), "; ".join(errors))
        valid.append(item)

    logger.info(
        "整理完成: 输入 %d → 去重后 %d（移除 %d 重复）→ 输出 %d 条",
        len(analyzed_items),
        len(unique),
        dup_count,
        len(valid),
    )
    return valid


def _normalize_item(item: dict[str, Any]) -> None:
    """标准化单条知识条目的字段。

    就地修正分类、重要性、标签等枚举字段为合法值。

    Args:
        item: 知识条目字典，会被就地修改。
    """
    cat = item.get("category", "")
    if cat not in VALID_CATEGORIES:
        logger.debug("修正非法 category '%s' → 'tool'", cat)
        item["category"] = "tool"

    imp = item.get("importance", "")
    if imp not in VALID_IMPORTANCE:
        logger.debug("修正非法 importance '%s' → 'medium'", imp)
        item["importance"] = "medium"

    tags = item.get("tags", [])
    if not isinstance(tags, list):
        item["tags"] = []
    else:
        item["tags"] = [str(t) for t in tags if isinstance(t, (str, int, float))]

    if not item.get("summary"):
        item["summary"] = item.get("description", "")[:300]

    if not item.get("title"):
        item["title"] = "未命名条目"


def _validate_item(item: dict[str, Any]) -> list[str]:
    """校验单条知识条目的必填字段和格式。

    Args:
        item: 知识条目字典。

    Returns:
        错误信息列表，为空表示通过。
    """
    errors: list[str] = []

    required_fields = ["id", "title", "source_url", "source_type", "summary", "status"]
    for field_name in required_fields:
        if not item.get(field_name):
            errors.append(f"缺少必填字段 '{field_name}'")

    source_type = item.get("source_type", "")
    if source_type and source_type not in VALID_SOURCE_TYPES:
        errors.append(f"非法 source_type: {source_type}")

    summary = item.get("summary", "")
    if isinstance(summary, str) and len(summary) < 20:
        errors.append(f"摘要过短: {len(summary)} 字")

    return errors


# ---------------------------------------------------------------------------
# Step 4: 保存（Save）
# ---------------------------------------------------------------------------


def step_save(items: list[dict[str, Any]], dry_run: bool = False) -> list[Path]:
    """执行保存步骤。

    按来源分组批量写入 articles/，文件名格式 {source}-YYYY-MM-DD-HHMM.json。
    同时将全量数据备份到 raw/，文件名格式 {source}-YYYYMMDD-HHMM.json。
    dry_run 模式下只打印预览，不写磁盘。

    Args:
        items: 整理后的知识条目列表。
        dry_run: 为 True 时只预览不实际保存。

    Returns:
        保存的文件路径列表（dry_run 时为空列表）。
    """
    logger.info("=" * 50)
    logger.info("Step 4: 保存 (Save)%s", " [DRY RUN]" if dry_run else "")
    logger.info("待保存: %d 条", len(items))
    logger.info("=" * 50)

    if dry_run:
        for item in items:
            logger.info(
                "[DRY RUN] %s | %s | %s | %s",
                item.get("id"),
                item.get("importance"),
                item.get("category"),
                item.get("title", "")[:50],
            )
        return []

    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    now = datetime.now(timezone.utc)
    hhmm = now.strftime("%H%M")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        source_type = item.get("source_type", "unknown")
        source_prefix = "github" if source_type == "github_trending" else "rss"
        grouped.setdefault(source_prefix, []).append(item)

    for source_prefix, group_items in grouped.items():
        date_str = now.strftime("%Y-%m-%d")
        filename = f"{source_prefix}-{date_str}-{hhmm}.json"
        filepath = ARTICLES_DIR / filename

        if filepath.exists():
            logger.warning("文件已存在，跳过: %s", filepath.name)
            continue

        try:
            filepath.write_text(
                json.dumps(group_items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            saved_paths.append(filepath)
            logger.info("已保存: %s (%d 条)", filepath.name, len(group_items))
        except OSError as exc:
            logger.error("写入失败: %s | %s", filepath.name, exc)

        date_compact = now.strftime("%Y%m%d")
        raw_filename = f"{source_prefix}-{date_compact}-{hhmm}.json"
        raw_path = RAW_DIR / raw_filename
        try:
            raw_path.write_text(
                json.dumps(group_items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("原始数据备份: %s", raw_path.name)
        except OSError as exc:
            logger.error("原始数据备份失败: %s", exc)

    logger.info("保存完成: %d 条文章 → %s", len(items), ARTICLES_DIR)
    return saved_paths


# ---------------------------------------------------------------------------
# 流水线编排
# ---------------------------------------------------------------------------


def run_pipeline(
    sources: list[str] | None = None,
    limit: int = 20,
    dry_run: bool = False,
    verbose: bool = False,
) -> list[Path]:
    """执行完整的四步流水线。

    Args:
        sources: 采集来源列表，每项为 "github" 或 "rss"。
            为 None 或空列表时默认为 ["github"]。
        limit: 每个来源的最大采集条数。
        dry_run: 为 True 时只运行采集/分析/整理，不保存文件。
        verbose: 为 True 时输出详细日志。

    Returns:
        保存的文件路径列表。
    """
    if not sources:
        sources = ["github"]

    start_time = time.monotonic()

    raw_items = step_collect(sources, limit)
    if not raw_items:
        logger.warning("采集结果为空，流水线终止")
        return []

    analyzed_items = step_analyze(raw_items)

    organized_items = step_organize(analyzed_items)

    saved = step_save(organized_items, dry_run=dry_run)

    elapsed = time.monotonic() - start_time
    logger.info("=" * 50)
    logger.info("流水线执行完成")
    logger.info(
        "采集 %d → 分析 %d → 整理 %d → 保存 %d | 耗时 %.1fs",
        len(raw_items),
        len(analyzed_items),
        len(organized_items),
        len(saved),
        elapsed,
    )
    logger.info("=" * 50)

    return saved


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    Returns:
        配置好的 ArgumentParser 实例。
    """
    parser = argparse.ArgumentParser(
        description="AI 知识库四步自动化流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python pipeline/pipeline.py --sources github,rss --limit 20\n"
            "  python pipeline/pipeline.py --sources github --limit 5\n"
            "  python pipeline/pipeline.py --dry-run --verbose\n"
        ),
    )
    parser.add_argument(
        "--sources",
        type=str,
        default="github",
        help="采集来源，逗号分隔 (github, rss)，默认: github",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="每个来源的最大采集条数，默认: 20",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干跑模式：只运行流水线不保存文件",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="输出详细日志 (DEBUG 级别)",
    )
    return parser


def main() -> None:
    """CLI 入口函数。"""
    parser = build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    valid_sources = {"github", "rss"}
    invalid = set(sources) - valid_sources
    if invalid:
        logger.error(
            "不支持的来源: %s（可选: %s）", ", ".join(invalid), ", ".join(valid_sources)
        )
        sys.exit(1)

    logger.info(
        "启动流水线 | 来源: %s | 限制: %d | dry_run: %s",
        args.sources,
        args.limit,
        args.dry_run,
    )

    saved = run_pipeline(
        sources=sources,
        limit=args.limit,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    if saved:
        print(f"\n完成! 保存 {len(saved)} 篇文章到 {ARTICLES_DIR}/")
    elif args.dry_run:
        print("\n(dry-run 模式，未保存文件)")
    else:
        print("\n未保存任何文件")


if __name__ == "__main__":
    main()
