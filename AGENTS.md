# AGENTS.md — AI 知识库助手

## 1. 项目概述

AI 知识库助手是一个多 Agent 协作系统，自动从 GitHub Trending 和 Hacker News 采集 AI/LLM/Agent 领域的技术动态，经 AI 分析、分类后结构化存储为 JSON，并支持通过 Telegram、飞书等渠道分发推送。目标是让人不掉队地追踪 AI 前沿，把"信息洪流"变成"可检索的知识"。

## 2. 技术栈

| 层级 | 技术选型 | 用途 |
|------|----------|------|
| 运行时 | Python 3.12 | 主语言 |
| Agent 框架 | OpenCode + 国产大模型 (DeepSeek/GLM) | Agent 编排与推理 |
| 工作流引擎 | LangGraph | 多 Agent 状态图编排 |
| Agent 协作 | OpenClaw | 跨 Agent 通信与调度 |
| 数据格式 | JSON (UTF-8) | 知识条目存储 |
| 分发渠道 | Telegram Bot / 飞书 Bot | 消息推送 |

## 3. 编码规范

- **风格**: PEP 8，行宽 120 字符
- **命名**: 变量/函数 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE_CASE`
- **Docstring**: Google 风格，中文编写，所有公开函数/类必须有
- **日志**: 使用 `logging` 模块，**禁止裸 `print()`**，调试输出走 `logger.debug()`
- **类型标注**: 所有函数签名必须加类型标注，使用 Python 3.12+ 语法 (`list[str]` 而非 `List[str]`)
- **错误处理**: 使用自定义异常层级，禁止裸 `except:` 或 `except Exception:`
- **配置管理**: 环境变量通过 `.env` 管理，敏感信息不入库

### Docstring 示例

```python
def fetch_trending(language: str, since: str = "daily") -> list[dict]:
    """获取 GitHub Trending 仓库列表。

    Args:
        language: 编程语言筛选，如 "python"。传空字符串表示全部。
        since: 时间范围，可选 "daily"、"weekly"、"monthly"。

    Returns:
        仓库信息字典列表，每个字典包含 name、url、description、stars 字段。

    Raises:
        FetchError: 请求失败或页面解析异常时抛出。
    """
```

## 4. 项目结构

```
ai-knowledge-base/
├── .opencode/
│   ├── agents/              # Agent 角色定义（YAML/JSON）
│   │   ├── collector.yaml   # 采集 Agent 配置
│   │   ├── analyzer.yaml    # 分析 Agent 配置
│   │   └── organizer.yaml   # 整理 Agent 配置
│   ├── skills/              # Agent 技能定义
│   │   ├── fetch_github.py  # GitHub Trending 采集技能
│   │   ├── fetch_hn.py      # Hacker News 采集技能
│   │   ├── analyze.py       # AI 分析技能
│   │   └── distribute.py    # 多渠道分发技能
│   └── package.json
├── knowledge/
│   ├── raw/                 # 原始采集数据（按日期归档）
│   └── articles/            # 经分析整理后的知识条目 JSON
├── AGENTS.md                # 本文件 — 项目契约
└── .gitignore
```

## 5. 知识条目 JSON 格式

```json
{
  "id": "gh-20260420-deepseek-r1",
  "title": "DeepSeek-R1 开源推理模型发布",
  "source_url": "https://github.com/deepseek-ai/DeepSeek-R1",
  "source_type": "github_trending",
  "summary": "DeepSeek 发布开源推理模型 R1，在数学和代码推理任务上表现接近 o1 水平，采用强化学习训练。",
  "tags": ["llm", "reasoning", "open-source", "deepseek"],
  "category": "model-release",
  "importance": "high",
  "status": "published",
  "language": "zh-CN",
  "collected_at": "2026-04-20T08:30:00Z",
  "analyzed_at": "2026-04-20T08:35:00Z",
  "published_at": "2026-04-20T09:00:00Z",
  "metadata": {
    "stars": 15000,
    "language": "python",
    "hn_score": null
  }
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 唯一标识，格式 `{来源缩写}-{日期}-{简短标识}` |
| `title` | string | 是 | 中文标题，简洁概括 |
| `source_url` | string | 是 | 原始链接 |
| `source_type` | string | 是 | 来源类型: `github_trending` / `hacker_news` |
| `summary` | string | 是 | AI 生成的中文摘要，100-300 字 |
| `tags` | string[] | 是 | 标签，限定在预定义标签集内 |
| `category` | string | 是 | 分类: `model-release` / `paper` / `tool` / `tutorial` / `industry` |
| `importance` | string | 是 | 重要性: `high` / `medium` / `low` |
| `status` | string | 是 | 状态: `raw` → `analyzed` → `published` |
| `language` | string | 是 | 内容语言标识 |
| `collected_at` | string | 是 | ISO 8601 采集时间 |
| `analyzed_at` | string | 否 | ISO 8601 分析完成时间 |
| `published_at` | string | 否 | ISO 8601 发布时间 |
| `metadata` | object | 否 | 来源相关元数据（stars、hn_score 等） |

## 6. Agent 角色概览

| 角色 | Agent 名称 | 职责 | 输入 | 输出 |
|------|-----------|------|------|------|
| 采集 | `collector` | 定时爬取 GitHub Trending / Hacker News，提取 AI 相关条目原始数据 | 定时触发 / 手动指令 | `knowledge/raw/` 下的原始 JSON |
| 分析 | `analyzer` | 对原始数据去重、AI 摘要生成、标签分类、重要性评级 | `knowledge/raw/` 中的原始数据 | 状态为 `analyzed` 的知识条目 |
| 整理 | `organizer` | 将分析后的条目格式化为推送内容，分发到 Telegram / 飞书，归档到 `knowledge/articles/` | 状态为 `analyzed` 的条目 | 推送消息 + 状态为 `published` 的归档文件 |

### 协作流程

```
[定时触发] → collector → knowledge/raw/ → analyzer → organizer → [Telegram/飞书]
                                                                     ↓
                                                          knowledge/articles/
```

## 7. 红线

以下操作**绝对禁止**，任何 Agent 在任何情况下都不得违反：

1. **禁止伪造数据**: 不得凭空编造不存在的项目、论文或新闻条目
2. **禁止覆盖删除**: 不得覆盖或删除 `knowledge/` 下已有的归档文件，只能新增
3. **禁止未经分析直接发布**: 所有条目必须经过 `analyzer` 处理，不允许从 `raw` 直接跳到 `published`
4. **禁止硬编码密钥**: API Key、Bot Token 等敏感信息禁止出现在代码中，统一走环境变量
5. **禁止无限重试**: 网络请求失败最多重试 3 次，必须有指数退避，不得死循环
6. **禁止破坏性文件操作**: Agent 不得执行 `rm -rf`、批量删除、格式化等不可逆操作
7. **禁止绕过状态机**: 条目状态必须按 `raw` → `analyzed` → `published` 顺序流转，不得跳步或回退
