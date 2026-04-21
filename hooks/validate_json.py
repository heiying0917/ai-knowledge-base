#!/usr/bin/env python3
"""知识条目 JSON 格式校验脚本。

用法:
    python hooks/validate_json.py <json_file> [json_file2 ...]
    python hooks/validate_json.py "knowledge/articles/*.json"

校验规则:
    1. JSON 解析成功
    2. 必填字段存在且类型正确（dict[str, type] 声明）
    3. ID 格式 {source}-{YYYYMMDD}-{NNN}
    4. status 枚举值: raw / analyzed / published
    5. source_url 符合 https?:// 格式
    6. summary >= 20 字, tags >= 1 个
    7. score（可选）1-10，audience（可选）beginner/intermediate/advanced

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
    "summary": str,
    "tags": list,
    "status": str,
}

VALID_STATUSES: set[str] = {"raw", "analyzed", "published"}
VALID_AUDIENCES: set[str] = {"beginner", "intermediate", "advanced"}

ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9]*-\d{8}-\d{3}$")
URL_PATTERN: re.Pattern[str] = re.compile(r"^https?://.+")

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


def validate_item(item: dict, index: int) -> list[str]:
    """校验单条知识条目。

    Args:
        item: 知识条目字典。
        index: 条目在文件中的序号（从 0 开始），用于定位报错。

    Returns:
        错误信息列表，为空表示通过。
    """
    errors: list[str] = []
    prefix = f"条目#{index}"

    # 必填字段校验
    for field_name, expected_type in REQUIRED_FIELDS.items():
        value = item.get(field_name)
        if value is None:
            errors.append(f"{prefix}: 缺少必填字段 '{field_name}'")
        elif not isinstance(value, expected_type):
            actual = type(value).__name__
            errors.append(
                f"{prefix}: 字段 '{field_name}' 类型错误，期望 {expected_type.__name__}，实际 {actual}"
            )

    # 后续校验依赖字段已存在且类型正确，缺失则跳过
    item_id = item.get("id")
    if isinstance(item_id, str) and not ID_PATTERN.match(item_id):
        errors.append(
            f"{prefix}: ID 格式错误 '{item_id}'，期望 {{source}}-{{YYYYMMDD}}-{{NNN}}"
        )

    status = item.get("status")
    if isinstance(status, str) and status not in VALID_STATUSES:
        errors.append(
            f"{prefix}: status 值无效 '{status}'，可选: {', '.join(sorted(VALID_STATUSES))}"
        )

    source_url = item.get("source_url")
    if isinstance(source_url, str) and not URL_PATTERN.match(source_url):
        errors.append(f"{prefix}: source_url 格式无效 '{source_url}'")

    summary = item.get("summary")
    if isinstance(summary, str) and len(summary) < MIN_SUMMARY_LENGTH:
        errors.append(
            f"{prefix}: summary 长度不足 {len(summary)} 字，最少 {MIN_SUMMARY_LENGTH} 字"
        )

    tags = item.get("tags")
    if isinstance(tags, list) and len(tags) < MIN_TAGS_COUNT:
        errors.append(
            f"{prefix}: tags 数量不足 {len(tags)} 个，最少 {MIN_TAGS_COUNT} 个"
        )

    # 可选字段校验
    score = item.get("score")
    if score is not None:
        if not isinstance(score, (int, float)):
            errors.append(
                f"{prefix}: score 类型错误，期望数字，实际 {type(score).__name__}"
            )
        elif not (SCORE_MIN <= score <= SCORE_MAX):
            errors.append(
                f"{prefix}: score 超出范围 {score}，允许 {SCORE_MIN}-{SCORE_MAX}"
            )

    audience = item.get("audience")
    if audience is not None:
        if not isinstance(audience, str):
            errors.append(
                f"{prefix}: audience 类型错误，期望 str，实际 {type(audience).__name__}"
            )
        elif audience not in VALID_AUDIENCES:
            errors.append(
                f"{prefix}: audience 值无效 '{audience}'，可选: {', '.join(sorted(VALID_AUDIENCES))}"
            )

    return errors


def validate_file(filepath: Path) -> list[str]:
    """校验单个 JSON 文件。

    Args:
        filepath: JSON 文件路径。

    Returns:
        错误信息列表，为空表示全部通过。
    """
    errors: list[str] = []

    try:
        text = filepath.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"[{filepath}] 文件读取失败: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"[{filepath}] JSON 解析失败: {exc}"]

    items = data if isinstance(data, list) else [data]

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(
                f"[{filepath}] 条目#{index}: 非对象类型 {type(item).__name__}"
            )
            continue
        errors.extend(validate_item(item, index))

    return errors


def main() -> None:
    """入口函数。"""
    if len(sys.argv) < 2:
        print(
            "用法: python hooks/validate_json.py <json_file> [json_file2 ...]",
            file=sys.stderr,
        )
        sys.exit(1)

    targets = collect_targets(sys.argv[1:])
    if not targets:
        print("错误: 未找到匹配的文件", file=sys.stderr)
        sys.exit(1)

    total_files = len(targets)
    passed_files = 0
    all_errors: list[str] = []

    for filepath in targets:
        file_errors = validate_file(filepath)
        if file_errors:
            all_errors.append(f"\n❌ {filepath}")
            all_errors.extend(f"  · {e}" for e in file_errors)
        else:
            passed_files += 1

    failed_files = total_files - passed_files

    if all_errors:
        print("\n".join(all_errors))
        print(f"\n{'=' * 50}")
        print(f"校验失败: {failed_files}/{total_files} 个文件未通过")
        sys.exit(1)

    print(f"校验通过: {total_files}/{total_files} 个文件全部合规")
    sys.exit(0)


if __name__ == "__main__":
    main()
