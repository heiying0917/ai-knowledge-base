#!/usr/bin/env python3
"""一次性迁移脚本：将 Pipeline 生成的单条文件合并为批量格式。

处理内容:
    1. articles/ 下的单条文件 (github-20260422-xxx.json) → 批量文件 (github-2026-04-22-0830.json)
    2. raw/ 下的 pipeline-YYYYMMDD-HHMM.json → {source}-YYYYMMDD-HHMM.json
    3. ID 格式从 {source}-{date}-{slug} 修正为 {source}-{date}-{NNN}
    4. analysis 字段补充 highlights 和 score_reason
    5. 删除不应存在的 published_at 字段（articles 阶段 status 应为 analyzed）

使用:
    python scripts/migrate_data.py --dry-run
    python scripts/migrate_data.py

注意: 运行后检查输出，确认无误后删除本脚本。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = PROJECT_ROOT / "knowledge" / "articles"
RAW_DIR = PROJECT_ROOT / "knowledge" / "raw"

OLD_ID_PATTERN = re.compile(r"^[a-z]+-\d{8}-.+")
VALID_BATCH_PATTERN = re.compile(r"^[a-z]+-\d{4}-\d{2}-\d{2}-\d{4}$")


def is_single_item_file(filepath: Path) -> bool:
    """判断文件是否为 Pipeline 生成的单条条目文件。

    Args:
        filepath: 文件路径。

    Returns:
        单条条目文件返回 True。
    """
    if not filepath.suffix == ".json":
        return False
    stem = filepath.stem
    if OLD_ID_PATTERN.match(stem):
        return True
    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
        return isinstance(data, dict) and "id" in data
    except (json.JSONDecodeError, OSError):
        return False


def is_pipeline_raw(filepath: Path) -> bool:
    """判断是否为 Pipeline 生成的 raw 文件。

    Args:
        filepath: 文件路径。

    Returns:
        Pipeline raw 文件返回 True。
    """
    return filepath.stem.startswith("pipeline-")


def fix_entry(entry: dict, seq: int) -> dict:
    """修正单条知识条目的格式。

    Args:
        entry: 原始条目。
        seq: 新编号。

    Returns:
        修正后的条目。
    """
    source_type = entry.get("source_type", "unknown")
    source_prefix = "github" if source_type == "github_trending" else "rss"

    collected_at = entry.get("collected_at", "")
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", collected_at)
    date_part = date_match.group(1).replace("-", "") if date_match else "20260422"

    new_id = f"{source_prefix}-{date_part}-{seq:03d}"
    entry["id"] = new_id

    analysis = entry.get("analysis", {})
    if isinstance(analysis, dict):
        if "highlights" not in analysis:
            analysis["highlights"] = []
        if "score_reason" not in analysis:
            score = analysis.get("score", 0)
            analysis["score_reason"] = f"迁移自旧格式，原始评分 {score}"
        entry["analysis"] = analysis

    if "published_at" in entry:
        del entry["published_at"]

    return entry


def migrate_articles(dry_run: bool) -> None:
    """迁移 articles/ 下的单条文件为批量文件。

    Args:
        dry_run: 为 True 时只打印不执行。
    """
    if not ARTICLES_DIR.exists():
        print("articles/ 目录不存在，跳过")
        return

    single_files: list[Path] = []
    for fp in sorted(ARTICLES_DIR.glob("*.json")):
        if is_single_item_file(fp):
            single_files.append(fp)

    if not single_files:
        print("articles/ 无需迁移的单条文件")
        return

    print(f"发现 {len(single_files)} 个单条文件需要迁移")

    groups: dict[str, list[dict]] = defaultdict(list)
    file_map: dict[str, list[Path]] = defaultdict(list)

    for fp in single_files:
        try:
            entry = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  跳过（解析失败）: {fp.name} — {exc}")
            continue

        source_type = entry.get("source_type", "unknown")
        source_prefix = "github" if source_type == "github_trending" else "rss"
        collected_at = entry.get("collected_at", "")
        date_match = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):\d{2}", collected_at)
        if date_match:
            group_key = f"{source_prefix}-{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}-{date_match.group(4)}00"
        else:
            group_key = f"{source_prefix}-2026-04-22-0000"

        groups[group_key].append(entry)
        file_map[group_key].append(fp)

    for group_key, entries in sorted(groups.items()):
        for i, entry in enumerate(entries, 1):
            fix_entry(entry, i)

        output_path = ARTICLES_DIR / f"{group_key}.json"
        action = "将创建" if dry_run else "创建"
        print(f"  {action}: {output_path.name} ({len(entries)} 条)")

        if not dry_run:
            output_path.write_text(
                json.dumps(entries, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for old_fp in file_map[group_key]:
                old_fp.unlink()
                print(f"    删除: {old_fp.name}")


def migrate_raw(dry_run: bool) -> None:
    """迁移 raw/ 下的 pipeline-*.json 文件。

    Args:
        dry_run: 为 True 时只打印不执行。
    """
    if not RAW_DIR.exists():
        print("raw/ 目录不存在，跳过")
        return

    pipeline_files = [fp for fp in RAW_DIR.glob("*.json") if is_pipeline_raw(fp)]
    if not pipeline_files:
        print("raw/ 无需迁移的 pipeline 文件")
        return

    print(f"发现 {len(pipeline_files)} 个 pipeline-*.json 文件需要迁移")

    for fp in pipeline_files:
        timestamp_match = re.match(r"pipeline-(\d{8}-\d{4})", fp.stem)
        if not timestamp_match:
            print(f"  跳过（文件名不匹配）: {fp.name}")
            continue

        try:
            items = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(items, dict):
                items = [items]
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  跳过（解析失败）: {fp.name} — {exc}")
            continue

        if not items:
            print(f"  跳过（空文件）: {fp.name}")
            continue

        source_types = set()
        for item in items:
            st = item.get("source_type", "unknown")
            source_types.add("github" if st == "github_trending" else "rss")

        ts = timestamp_match.group(1)
        for source_prefix in sorted(source_types):
            source_items = [
                item
                for item in items
                if ("github" if item.get("source_type") == "github_trending" else "rss")
                == source_prefix
            ]
            new_name = f"{source_prefix}-{ts}.json"
            new_path = RAW_DIR / new_name
            action = "将重命名为" if dry_run else "重命名为"
            print(f"  {action}: {fp.name} → {new_name} ({len(source_items)} 条)")

            if not dry_run:
                new_path.write_text(
                    json.dumps(source_items, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        if not dry_run:
            fp.unlink()
            print(f"    删除原始文件: {fp.name}")


def main() -> None:
    """入口函数。"""
    parser = argparse.ArgumentParser(description="迁移 Pipeline 旧格式数据为批量格式")
    parser.add_argument("--dry-run", action="store_true", help="只预览不执行")
    args = parser.parse_args()

    if args.dry_run:
        print("=== DRY RUN 模式 ===\n")

    print("=== 迁移 articles/ ===")
    migrate_articles(args.dry_run)

    print("\n=== 迁移 raw/ ===")
    migrate_raw(args.dry_run)

    if not args.dry_run:
        print("\n迁移完成。请运行 hooks/validate_json.py 验证迁移结果。")
    else:
        print("\n预览完成。去掉 --dry-run 参数执行实际迁移。")


if __name__ == "__main__":
    main()
