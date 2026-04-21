#!/usr/bin/env python3
"""知识条目质量评分脚本。

用法:
    python hooks/check_quality.py <json_file> [json_file2 ...]
    python hooks/check_quality.py "knowledge/articles/*.json"

五维度评分（满分 100）:
    - 摘要质量 (25 分): >= 50 字满分，>= 20 字基本分，含技术关键词有奖励
    - 技术深度 (25 分): analysis.score * 2.5 (1-10 → 0-25)
    - 格式规范 (20 分): id、title、source_url、status、时间戳五项各 4 分
    - 标签精度 (15 分): 1-3 个合法标签最佳，有标准标签列表校验
    - 空洞词检测 (15 分): 不含空洞词满分，每出现一个扣分

等级标准:
    A >= 80, B >= 60, C < 60

退出码:
    0 — 无 C 级条目
    1 — 存在 C 级条目
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

VALID_TAGS: set[str] = {
    "llm",
    "agent",
    "tool",
    "framework",
    "open-source",
    "paper",
    "reasoning",
    "multimodal",
    "inference",
    "training",
    "evaluation",
    "safety",
    "rag",
    "mcp",
    "api",
    "code",
    "robotics",
    "openai",
    "deepseek",
    "anthropic",
    "knowledge-distillation",
    "model-release",
    "tutorial",
    "industry",
    "dataset",
    "benchmark",
}

BUZZWORDS_ZH: set[str] = {
    "赋能",
    "抓手",
    "闭环",
    "打通",
    "全链路",
    "底层逻辑",
    "颗粒度",
    "对齐",
    "拉通",
    "沉淀",
    "强大的",
    "革命性的",
}

BUZZWORDS_EN: set[str] = {
    "groundbreaking",
    "revolutionary",
    "game-changing",
    "cutting-edge",
    "innovative",
    "next-generation",
    "state-of-the-art",
    "world-class",
    "disruptive",
    "unprecedented",
    "paradigm-shifting",
}

TECH_KEYWORDS: set[str] = {
    "transformer",
    "attention",
    "moe",
    "diffusion",
    "rlhf",
    "dpo",
    "lora",
    "quantization",
    "distillation",
    "fine-tuning",
    "inference",
    "mcp",
    "rag",
    "agent",
    "llm",
    "gpt",
    "bert",
    "vit",
    "clip",
    "multimodal",
    "tokenizer",
    "embedding",
    "reinforcement learning",
    "supervised",
    "unsupervised",
    "self-supervised",
    "few-shot",
    "zero-shot",
    "chain-of-thought",
    "cot",
    "reasoning",
}


@dataclass
class DimensionScore:
    """单个维度的评分结果。

    Attributes:
        name: 维度名称。
        score: 得分。
        max_score: 满分。
        detail: 扣分/加分说明。
    """

    name: str
    score: float
    max_score: float
    detail: str = ""


@dataclass
class QualityReport:
    """单条知识条目的质量评估报告。

    Attributes:
        item_id: 条目 ID。
        dimensions: 各维度评分。
        total_score: 加权总分。
        grade: 等级 (A/B/C)。
    """

    item_id: str
    dimensions: list[DimensionScore] = field(default_factory=list)
    total_score: float = 0.0
    grade: str = "C"

    def calc_total(self) -> None:
        """计算加权总分和等级。"""
        self.total_score = sum(d.score for d in self.dimensions)
        if self.total_score >= 80:
            self.grade = "A"
        elif self.total_score >= 60:
            self.grade = "B"
        else:
            self.grade = "C"


def score_summary(summary: str) -> DimensionScore:
    """评估摘要质量（满分 25 分）。

    规则:
        - >= 50 字: 20 分
        - >= 20 字: 12 分
        - < 20 字: 4 分
        - 含技术关键词: 每个 +2 分，上限 5 分

    Args:
        summary: 摘要文本。

    Returns:
        摘要质量维度评分。
    """
    detail_parts: list[str] = []
    length = len(summary)

    if length >= 50:
        base = 20
        detail_parts.append(f"长度 {length} 字(>=50, 基础 20 分)")
    elif length >= 20:
        base = 12
        detail_parts.append(f"长度 {length} 字(>=20, 基础 12 分)")
    else:
        base = 4
        detail_parts.append(f"长度 {length} 字(<20, 基础 4 分)")

    summary_lower = summary.lower()
    matched_keywords = [kw for kw in TECH_KEYWORDS if kw in summary_lower]
    bonus = min(len(matched_keywords), 5) * 2
    if bonus > 0:
        detail_parts.append(f"技术关键词 {len(matched_keywords)} 个(+{bonus} 分)")

    score = min(base + bonus, 25)
    return DimensionScore("摘要质量", score, 25, "; ".join(detail_parts))


def score_technical_depth(item: dict) -> DimensionScore:
    """评估技术深度（满分 25 分）。

    规则: analysis.score * 2.5 (1-10 → 0-25)。缺少时 0 分。

    Args:
        item: 知识条目字典。

    Returns:
        技术深度维度评分。
    """
    try:
        raw_score = item.get("analysis", {}).get("score")
        if raw_score is None:
            return DimensionScore("技术深度", 0, 25, "缺少 analysis.score 字段")
        raw_score = float(raw_score)
        raw_score = max(0, min(10, raw_score))
        mapped = round(raw_score * 2.5, 1)
        return DimensionScore(
            "技术深度",
            mapped,
            25,
            f"analysis.score={raw_score:.0f}, 映射 {mapped} 分",
        )
    except (TypeError, ValueError):
        return DimensionScore("技术深度", 0, 25, "analysis.score 解析失败")


def score_format(item: dict) -> DimensionScore:
    """评估格式规范（满分 20 分）。

    五项各 4 分: id、title、source_url、status、时间戳。

    Args:
        item: 知识条目字典。

    Returns:
        格式规范维度评分。
    """
    checks: list[tuple[str, bool]] = []

    checks.append(("id", bool(item.get("id"))))
    checks.append(("title", bool(item.get("title"))))
    checks.append(("source_url", bool(item.get("source_url"))))

    valid_statuses = {"raw", "analyzed", "published", "draft", "review", "archived"}
    status_val = item.get("status")
    checks.append(
        ("status", isinstance(status_val, str) and status_val in valid_statuses)
    )

    ts_fields = ["collected_at", "analyzed_at", "published_at"]
    has_ts = any(
        isinstance(item.get(f), str)
        and re.match(r"^\d{4}-\d{2}-\d{2}T", item.get(f, ""))
        for f in ts_fields
    )
    checks.append(("时间戳", has_ts))

    score = sum(4 for _, ok in checks if ok)
    missing = [name for name, ok in checks if not ok]
    detail = "全部合规" if not missing else f"缺失: {', '.join(missing)}"
    return DimensionScore("格式规范", score, 20, detail)


def score_tags(tags: list[str]) -> DimensionScore:
    """评估标签精度（满分 15 分）。

    规则:
        - 1-3 个标签: 10 分
        - 0 个或 > 5 个: 3 分
        - 其余: 6 分
        - 全部在标准标签列表内: +5 分
        - 部分合法: +3 分

    Args:
        tags: 标签列表。

    Returns:
        标签精度维度评分。
    """
    detail_parts: list[str] = []
    count = len(tags)

    if 1 <= count <= 3:
        base = 10
        detail_parts.append(f"{count} 个标签(基础 10 分)")
    elif count == 0:
        base = 3
        detail_parts.append("无标签(基础 3 分)")
    elif count > 5:
        base = 3
        detail_parts.append(f"{count} 个标签(>5, 基础 3 分)")
    else:
        base = 6
        detail_parts.append(f"{count} 个标签(基础 6 分)")

    valid_count = sum(1 for t in tags if t in VALID_TAGS)
    if valid_count == count and count > 0:
        bonus = 5
        detail_parts.append("全部合法(+5 分)")
    elif valid_count > 0:
        bonus = 3
        detail_parts.append(f"{valid_count}/{count} 合法(+3 分)")
    else:
        bonus = 0
        invalid = [t for t in tags if t not in VALID_TAGS]
        if invalid:
            detail_parts.append(f"非标准标签: {', '.join(invalid[:3])}")

    score = min(base + bonus, 15)
    return DimensionScore("标签精度", score, 15, "; ".join(detail_parts))


def score_buzzwords(summary: str) -> DimensionScore:
    """评估空洞词（满分 15 分）。

    规则: 无空洞词 15 分，每出现一个扣 3 分，最低 0 分。

    Args:
        summary: 摘要文本。

    Returns:
        空洞词检测维度评分。
    """
    found: list[str] = []
    text_lower = summary.lower()

    for word in BUZZWORDS_ZH:
        if word in summary:
            found.append(word)

    for word in BUZZWORDS_EN:
        if word in text_lower:
            found.append(word)

    if not found:
        return DimensionScore("空洞词检测", 15, 15, "未检测到空洞词")

    penalty = min(len(found) * 3, 15)
    score = 15 - penalty
    display = found[:5]
    return DimensionScore(
        "空洞词检测",
        score,
        15,
        f"检测到 {len(found)} 个空洞词: {', '.join(display)}(-{penalty} 分)",
    )


def evaluate_item(item: dict) -> QualityReport:
    """评估单条知识条目的质量。

    Args:
        item: 知识条目字典。

    Returns:
        质量评估报告。
    """
    report = QualityReport(item_id=item.get("id", "<unknown>"))

    summary = item.get("summary", "")
    tags = item.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    report.dimensions = [
        score_summary(summary),
        score_technical_depth(item),
        score_format(item),
        score_tags(tags),
        score_buzzwords(summary),
    ]
    report.calc_total()
    return report


def render_bar(score: float, max_score: float, width: int = 20) -> str:
    """渲染文本进度条。

    Args:
        score: 当前得分。
        max_score: 满分。
        width: 进度条字符宽度。

    Returns:
        进度条字符串。
    """
    ratio = score / max_score if max_score > 0 else 0
    filled = int(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {score:>5.1f}/{max_score}"


def render_report(report: QualityReport) -> None:
    """渲染单条报告到终端。

    Args:
        report: 质量评估报告。
    """
    grade_colors = {"A": "\033[92m", "B": "\033[93m", "C": "\033[91m"}
    reset = "\033[0m"
    color = grade_colors.get(report.grade, "")

    print(
        f"  ┌─ {report.item_id} {color}[{report.grade}]{reset} {report.total_score:.1f}/100"
    )
    for dim in report.dimensions:
        print(f"  │  {dim.name:<8} {render_bar(dim.score, dim.max_score)}")
        if dim.detail:
            print(f"  │           {dim.detail}")
    print(f"  └{'─' * 50}")


def collect_targets(patterns: list[str]) -> list[Path]:
    """从命令行参数收集待检测文件列表。

    支持通配符展开（如 *.json）和直接路径。

    Args:
        patterns: 命令行参数列表。

    Returns:
        去重后的路径列表。
    """
    seen: set[Path] = set()
    result: list[Path] = []

    for pattern in patterns:
        p = Path(pattern)
        parent = p.parent
        name = p.name
        if any(c in name for c in ("*", "?", "[")):
            for matched in sorted(parent.glob(name)):
                resolved = matched.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    result.append(matched)
        else:
            resolved = p.resolve()
            if resolved not in seen:
                seen.add(resolved)
                result.append(p)

    return result


def process_file(filepath: Path) -> list[QualityReport]:
    """处理单个 JSON 文件，返回所有条目的质量报告。

    Args:
        filepath: JSON 文件路径。

    Returns:
        质量报告列表。
    """
    try:
        text = filepath.read_text(encoding="utf-8")
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"  ✗ [{filepath}] JSON 解析失败: {exc}", file=sys.stderr)
        return []
    except OSError as exc:
        print(f"  ✗ [{filepath}] 文件读取失败: {exc}", file=sys.stderr)
        return []

    items = data if isinstance(data, list) else [data]
    reports: list[QualityReport] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        reports.append(evaluate_item(item))

    return reports


def main() -> None:
    """入口函数。"""
    if len(sys.argv) < 2:
        print(
            "用法: python hooks/check_quality.py <json_file> [json_file2 ...]",
            file=sys.stderr,
        )
        sys.exit(1)

    targets = collect_targets(sys.argv[1:])
    if not targets:
        print("错误: 未找到匹配的文件", file=sys.stderr)
        sys.exit(1)

    all_reports: list[QualityReport] = []
    has_c_grade = False

    for filepath in targets:
        reports = process_file(filepath)
        if not reports:
            continue

        print(f"\n📄 {filepath} ({len(reports)} 条)")
        for report in reports:
            render_report(report)
            all_reports.append(report)
            if report.grade == "C":
                has_c_grade = True

    if not all_reports:
        print("未发现可评估的条目", file=sys.stderr)
        sys.exit(1)

    total = len(all_reports)
    a_count = sum(1 for r in all_reports if r.grade == "A")
    b_count = sum(1 for r in all_reports if r.grade == "B")
    c_count = sum(1 for r in all_reports if r.grade == "C")
    avg_score = sum(r.total_score for r in all_reports) / total

    print(f"\n{'=' * 55}")
    print(
        f"总计: {total} 条 | A: {a_count} | B: {b_count} | C: {c_count} | 平均: {avg_score:.1f}"
    )
    print(f"{'=' * 55}")

    sys.exit(1 if has_c_grade else 0)


if __name__ == "__main__":
    main()
