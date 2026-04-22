#!/usr/bin/env python3
"""知识条目 JSON 格式校验脚本。

用法:
    python hooks/validate_json.py <json_file> [json_file2 ...]
    python hooks/validate_json.py "knowledge/articles/*.json"
    echo '<json>' | python hooks/validate_json.py --stdin

校验规则:
    1. JSON 解析成功
    2. 必填字段存在且类型正确
    3. ID 格式 {source}-{YYYYMMDD}-{NNN}
    4. category 枚举值
    5. importance 枚举值
    6. status 枚举值
    7. source_url 符合 https?:// 格式
    8. summary >= 20 字, tags >= 1 个

退出码:
    0 — 全部通过
    1 — 存在校验失败
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REQUIRED_FIELDS: dict[str, type] = {
    "id": str,
    "title": str,
    "source_url": str,
    "source_type": str,
    "summary": str,
    "tags": list,
    "category": str,
    "importance": str,
    "status": str,
    "language": str,
    "collected_at": str,
}

VALID_CATEGORIES: set[str] = {"model-release", "paper", "tool", "tutorial", "industry"}
VALID_IMPORTANCE: set[str] = {"high", "medium", "low"}
VALID_STATUSES: set[str] = {"raw", "analyzed", "published"}
VALID_AUDIENCES: set[str] = {"beginner", "intermediate", "advanced"}
VALID_SOURCE_TYPES: set[str] = {"github_trending", "hacker_news"}

ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9]*-\d{8}-\d{3}$")
URL_PATTERN: re.Pattern[str] = re.compile(r"^https?://.+")
ISO8601_PATTERN: re.Pattern[str] = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

MIN_SUMMARY_LENGTH: int = 20
MIN_TAGS_COUNT: int = 1
SCORE_MIN: int = 1
SCORE_MAX: int = 10


def collect_targets(patterns: list[str]) -> list[Path]:
    """从命令行参数收集待校验文件列表。

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


def _fmt_valid_set(valid_set: set[str]) -> str:
    return ", ".join(f"'{v}'" for v in sorted(valid_set))


def validate_item(item: dict, index: int) -> list[str]:
    """校验单条知识条目，返回详细的错误信息和修复建议。

    Args:
        item: 知识条目字典。
        index: 条目在文件中的序号（从 0 开始），用于定位报错。

    Returns:
        错误信息列表，为空表示通过。每条错误包含问题描述和修复建议。
    """
    errors: list[str] = []
    p = f"条目#{index}"

    for field_name, expected_type in REQUIRED_FIELDS.items():
        value = item.get(field_name)
        if value is None:
            errors.append(f"{p}: 缺少必填字段 '{field_name}'，请添加该字段")
        elif not isinstance(value, expected_type):
            errors.append(
                f"{p}: 字段 '{field_name}' 类型错误，期望 {expected_type.__name__}，"
                f"实际 {type(value).__name__}，当前值: {value!r}"
            )

    item_id = item.get("id")
    if item_id is None:
        pass
    elif not isinstance(item_id, str):
        pass
    elif not ID_PATTERN.match(item_id):
        errors.append(
            f"{p}: 字段 'id' 格式错误 '{item_id}'，"
            f"要求格式: {{source}}-{{YYYYMMDD}}-{{NNN}}，"
            f"例如: 'github-20260421-001'、'hn-20260421-003'"
        )

    source_type = item.get("source_type")
    if isinstance(source_type, str) and source_type not in VALID_SOURCE_TYPES:
        errors.append(
            f"{p}: 字段 'source_type' 值无效 '{source_type}'，"
            f"可选值: {_fmt_valid_set(VALID_SOURCE_TYPES)}"
        )

    category = item.get("category")
    if isinstance(category, str) and category not in VALID_CATEGORIES:
        errors.append(
            f"{p}: 字段 'category' 值无效 '{category}'，"
            f"可选值: {_fmt_valid_set(VALID_CATEGORIES)}"
        )

    importance = item.get("importance")
    if isinstance(importance, str) and importance not in VALID_IMPORTANCE:
        errors.append(
            f"{p}: 字段 'importance' 值无效 '{importance}'，"
            f"可选值: {_fmt_valid_set(VALID_IMPORTANCE)}"
        )

    status = item.get("status")
    if isinstance(status, str) and status not in VALID_STATUSES:
        errors.append(
            f"{p}: 字段 'status' 值无效 '{status}'，"
            f"可选值: {_fmt_valid_set(VALID_STATUSES)}"
        )

    source_url = item.get("source_url")
    if isinstance(source_url, str) and not URL_PATTERN.match(source_url):
        errors.append(
            f"{p}: 字段 'source_url' 格式无效 '{source_url}'，"
            f"要求以 http:// 或 https:// 开头的完整 URL，"
            f"例如: 'https://github.com/user/repo'"
        )

    summary = item.get("summary")
    if isinstance(summary, str) and len(summary) < MIN_SUMMARY_LENGTH:
        errors.append(
            f"{p}: 字段 'summary' 长度不足（当前 {len(summary)} 字），"
            f"要求最少 {MIN_SUMMARY_LENGTH} 字的中文摘要，"
            f"请补充内容使其达到要求长度"
        )

    tags = item.get("tags")
    if isinstance(tags, list) and len(tags) < MIN_TAGS_COUNT:
        errors.append(
            f"{p}: 字段 'tags' 数量不足（当前 {len(tags)} 个），"
            f"要求最少 {MIN_TAGS_COUNT} 个标签，"
            f"例如: ['llm', 'open-source', 'reasoning']"
        )

    score = item.get("score")
    if score is not None:
        if not isinstance(score, (int, float)):
            errors.append(
                f"{p}: 字段 'score' 类型错误，期望数字，实际 {type(score).__name__}，当前值: {score!r}"
            )
        elif not (SCORE_MIN <= score <= SCORE_MAX):
            errors.append(
                f"{p}: 字段 'score' 超出范围（当前 {score}），允许 {SCORE_MIN}-{SCORE_MAX}"
            )

    audience = item.get("audience")
    if audience is not None:
        if not isinstance(audience, str):
            errors.append(
                f"{p}: 字段 'audience' 类型错误，期望 str，实际 {type(audience).__name__}，当前值: {audience!r}"
            )
        elif audience not in VALID_AUDIENCES:
            errors.append(
                f"{p}: 字段 'audience' 值无效 '{audience}'，"
                f"可选值: {_fmt_valid_set(VALID_AUDIENCES)}"
            )

    collected_at = item.get("collected_at")
    if isinstance(collected_at, str) and not ISO8601_PATTERN.match(collected_at):
        errors.append(
            f"{p}: 字段 'collected_at' 格式无效 '{collected_at}'，"
            f"要求 ISO 8601 格式，例如: '2026-04-21T08:30:00Z'"
        )

    return errors


def validate_file(filepath: Path) -> list[str]:
    """校验单个 JSON 文件。

    Args:
        filepath: JSON 文件路径。

    Returns:
        错误信息列表，为空表示全部通过。
    """
    try:
        text = filepath.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"[{filepath}] 文件读取失败: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"[{filepath}] JSON 解析失败: {exc}"]

    items = data if isinstance(data, list) else [data]
    errors: list[str] = []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(
                f"[{filepath}] 条目#{index}: 非对象类型 {type(item).__name__}，期望 dict"
            )
            continue
        errors.extend(validate_item(item, index))

    return errors


def validate_content(text: str, label: str = "<stdin>") -> list[str]:
    """校验 JSON 文本内容（不依赖文件路径）。

    Args:
        text: JSON 文本。
        label: 报错时显示的来源标识。

    Returns:
        错误信息列表，为空表示通过。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"[{label}] JSON 解析失败: {exc}"]

    items = data if isinstance(data, list) else [data]
    errors: list[str] = []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(
                f"[{label}] 条目#{index}: 非对象类型 {type(item).__name__}，期望 dict"
            )
            continue
        errors.extend(validate_item(item, index))

    return errors


def format_errors(errors: list[str]) -> str:
    """将错误列表格式化为带修复参考的输出。

    Args:
        errors: 校验错误列表。

    Returns:
        格式化后的错误信息字符串。
    """
    lines = [f"  · {e}" for e in errors]
    lines.append("")
    lines.append("=" * 50)
    lines.append("校验失败，请根据上述错误信息修复 JSON 内容。")
    lines.append("")
    lines.append("正确格式参考:")
    lines.append(
        json.dumps(
            {
                "id": "github-20260421-001",
                "title": "示例标题",
                "source_url": "https://github.com/user/repo",
                "source_type": "github_trending",
                "summary": "这是一段示例摘要内容，长度需要超过二十个字才能通过校验。",
                "tags": ["llm", "open-source"],
                "category": "tool",
                "importance": "medium",
                "status": "analyzed",
                "language": "zh-CN",
                "collected_at": "2026-04-21T08:30:00Z",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return "\n".join(lines)


def main() -> None:
    """入口函数。"""
    if len(sys.argv) < 2:
        print(
            "用法: python hooks/validate_json.py <json_file> [json_file2 ...]\n"
            "      echo '<json>' | python hooks/validate_json.py --stdin",
            file=sys.stderr,
        )
        sys.exit(1)

    if sys.argv[1] == "--stdin":
        text = sys.stdin.read()
        errors = validate_content(text)
        if errors:
            print(format_errors(errors))
            sys.exit(1)
        print("校验通过: 内容合规")
        sys.exit(0)

    targets = collect_targets(sys.argv[1:])
    if not targets:
        print("错误: 未找到匹配的文件", file=sys.stderr)
        sys.exit(1)

    total_files = len(targets)
    all_errors: list[str] = []

    for filepath in targets:
        file_errors = validate_file(filepath)
        if file_errors:
            all_errors.append(f"\n❌ {filepath}")
            all_errors.extend(f"  · {e}" for e in file_errors)

    if all_errors:
        all_errors.append("")
        all_errors.append("=" * 50)
        all_errors.append(
            f"校验失败: {total_files - (len(all_errors) - len(targets))}/{total_files} 个文件未通过"
        )
        print("\n".join(all_errors))
        sys.exit(1)

    print(f"校验通过: {total_files}/{total_files} 个文件全部合规")
    sys.exit(0)


if __name__ == "__main__":
    main()
