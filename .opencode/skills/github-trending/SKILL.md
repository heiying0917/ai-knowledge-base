---
name: github-trending
description: 当需要采集 GitHub 热门开源项目时使用此技能
allowed-tools:
  - Read
  - Grep
  - Glob
  - WebFetch
---

# GitHub Trending 采集技能

## 使用场景

- 用户要求采集 GitHub 当日/本周/本月热门开源项目
- 编排层（orchestrator）委托采集任务时自动触发
- 定时任务触发日常采集

## 执行步骤

### 1. 搜索热门仓库

通过 GitHub Search API 获取近期高 Star 增长的仓库：

```
GET https://api.github.com/search/repositories?q=created:>2026-04-13&sort=stars&order=desc&per_page=100
```

- 日期参数使用**当天 - 7 天**作为起始时间，确保捕获本周新星
- 每页 100 条，最多请求 3 页（共 300 条候选）
- 如果 API 限流（HTTP 422/403），退而使用 WebFetch 采集 [GitHub Trending 页面](https://github.com/trending?since=weekly)

### 2. 提取信息

对每个仓库提取以下字段：

| 字段 | 来源 | 说明 |
|------|------|------|
| `name` | `full_name` | 仓库全名，格式 `owner/repo` |
| `url` | `html_url` | 仓库页面链接 |
| `summary` | — | 待后续步骤生成 |
| `stars` | `stargazers_count` | 当前 Star 数 |
| `language` | `language` | 主要编程语言 |
| `topics` | `topics[]` | 仓库标签列表 |
| `description` | `description` | 原始描述（用于生成中文摘要） |

### 3. 过滤

**纳入条件**（满足任一即保留）：

- `topics` 包含：`ai`、`llm`、`agent`、`machine-learning`、`deep-learning`、`nlp`、`transformer`、`langchain`、`rag`、`mcp`、`copilot`
- `language` 为 Python / TypeScript / Rust 且 `description` 中含 AI 相关关键词（`gpt`、`claude`、`model`、`neural`、`inference`、`fine-tune`）
- `description` 包含关键词：`LLM`、`large language model`、`AI agent`、`RAG`、`MCP`、`copilot`、`deepseek`、`openai`、`anthropic`

**排除条件**（命中任一即丢弃）：

- 仓库名或 `topics` 包含 `awesome`（Awesome 列表不纳入）
- `description` 为空或 null
- Star 数低于 50

### 4. 去重

- 以仓库 `full_name` 作为唯一键
- 与 `knowledge/raw/` 目录下**最近 7 天**已有的 `github-trending-*.json` 文件进行比对
- 使用 Glob 扫描已有文件，Grep 匹配 `name` 字段，排除已采集的仓库
- 仅保留本次新增的仓库

### 5. 撰写中文摘要

对每个保留的仓库，按以下公式生成 50-150 字的中文摘要：

**公式**：`项目名` + 做了什么（一句话概括核心功能） + 为什么值得关注（技术亮点或解决什么痛点）

示例：

> DeepSeek-R1：开源推理模型，采用强化学习训练，在数学和代码推理任务上接近 o1 水平。值得关注因为它证明了无需大规模监督数据也能获得强推理能力。

要求：

- 用中文撰写，技术术语保留英文原文
- 突出项目的**差异化价值**，避免泛泛而谈
- 如果 `description` 信息不足，通过 WebFetch 访问仓库 README 获取更多上下文

### 6. 排序取 Top 15

按 `stars` 降序排列，取前 15 个仓库。如果过滤后不足 15 个，则全部保留。

### 7. 输出 JSON

将结果输出为 JSON 数组，交由编排层写入 `knowledge/raw/`。

**编排层写入路径**: `knowledge/raw/github-trending-YYYY-MM-DD-HHMM.json`，日期和时间使用当前 UTC 时间。HHMM 为时分（如 `0830`），用于区分同一天多次执行。

**注意**：本技能不执行文件写入，采集结果交由 orchestrator 透传。

## 注意事项

1. **不得伪造数据**：所有项目必须来自真实的 GitHub API 或 Trending 页面，禁止凭空编造
2. **API 限流处理**：GitHub Search API 未认证限制 10 次/分钟，认证后 30 次/分钟。遇到限流时使用指数退避重试，最多 3 次，仍失败则切换到 WebFetch 采集 Trending 页面
3. **编码规范**：输出 JSON 必须是合法的 UTF-8 编码，中文摘要不得包含未转义的特殊字符
4. **幂等性**：同一天多次运行不会产生重复数据，去重步骤保证这一点
5. **禁止覆盖**：已存在的 raw 文件不得覆盖，只能新增
6. **网络超时**：单次请求超时设为 30 秒，总体采集不超过 5 分钟
7. **摘要质量**：中文摘要必须体现项目的**具体技术特点**，避免"这是一个 XXX 工具"式的空洞描述

## 输出格式

```json
[{
    "name": "deepseek-ai/DeepSeek-R1",
    "url": "https://github.com/deepseek-ai/DeepSeek-R1",
    "summary": "DeepSeek 开源推理模型 R1，采用强化学习训练路线，在数学和代码推理任务上表现接近 o1 水平。值得关注因为它证明了无需大规模监督数据也能获得强推理能力，且完全开源可本地部署。",
    "stars": 15000,
    "language": "python",
    "topics": ["llm", "reasoning", "open-source", "deepseek"]
}]
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 仓库全名 `owner/repo` |
| `url` | string | 是 | 仓库 GitHub 链接 |
| `summary` | string | 是 | 中文摘要，50-150 字 |
| `stars` | number | 是 | 当前 Star 数 |
| `language` | string | 否 | 主要编程语言 |
| `topics` | string[] | 否 | 仓库标签列表 |
