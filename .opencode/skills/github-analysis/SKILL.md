---
name: github-analysis
description: 当需要对 GitHub Trending 采集的开源项目进行深度分析总结时使用此技能
allowed-tools:
  - Read
  - Grep
  - Glob
  - WebFetch
---

# GitHub 开源项目深度分析技能

## 使用场景

- 编排层（orchestrator）委托分析任务时自动触发
- 对 `knowledge/raw/` 中 github-trending 来源的原始采集数据进行深度分析
- 将 `raw` 状态的条目转化为 `analyzed` 状态

## 评分标准

| 分数 | 含义 | 判断依据 |
|------|------|----------|
| 9-10 | 改变格局 | 开创全新范式、颠覆现有方案、行业级影响力 |
| 7-8 | 直接有帮助 | 可立即应用于生产、解决明确痛点、显著提升效率 |
| 5-6 | 值得了解 | 有参考价值、拓宽视野、未来可能有用 |
| 1-4 | 可略过 | 信息增量小、同质化严重、与 AI/LLM/Agent 关联度低 |

**评分约束**：单次分析中 9-10 分的项目不超过 2 个。

## 执行步骤

### 1. 读取最新采集文件

- 使用 Glob 扫描 `knowledge/raw/` 目录，找到最近的采集文件
- 仅关注文件名格式为 `github-trending-YYYY-MM-DD-*.json` 的文件
- 优先处理当天文件，若无则处理最近一天的文件
- 使用 Read 读取文件内容，解析 JSON

### 2. 逐条深度分析

对每条原始数据执行以下分析：

#### 2.1 摘要精炼

- 将原始摘要压缩至 **≤50 字**的中文摘要
- 保留核心技术要点，去除修饰性描述
- 公式：`项目名` + 做了什么 + 核心差异点

示例：

> DeepSeek-R1：开源推理模型，强化学习训练，推理能力接近 o1，证明无需大规模监督数据也可获得强推理。

#### 2.2 技术亮点提取

- 提取 **2-3 个**技术亮点
- **用事实说话**：引用具体指标、方法名称、技术路线，避免空洞描述
- 评估维度侧重：Star 增速、技术路线创新性、社区活跃度、可复现性
- 如果原始数据信息不足，通过 WebFetch 访问仓库 README 获取更多上下文

亮点示例：

- "采用 GRPO 强化学习算法，无需 SFT 即可实现推理能力提升"
- "在 MATH 基准上达到 79.8%，接近 o1 的 85.5%"

非亮点示例（禁止）：

- "技术先进"
- "性能优秀"

#### 2.3 评分

- 根据评分标准给出 **1-10 分**及评分理由
- 评分理由必须具体，说明为什么是这个分数而不是更高或更低
- 严格执行约束：**9-10 分不超过 2 个**

#### 2.4 标签建议

- 从预定义标签集中选取标签：`llm`、`reasoning`、`open-source`、`agent`、`rag`、`mcp`、`tool`、`framework`、`model-release`、`paper`、`tutorial`、`fine-tune`、`inference`、`training`、`dataset`、`evaluation`、`multimodal`、`code`、`safety`、`embedding`
- 如现有标签无法准确描述，可新增标签但需标注为自定义标签
- 每条分配 **3-5 个**标签

### 3. 输出分析结果 JSON

将分析结果以数组形式返回，每条包含分析后的完整知识条目。

**注意**：本技能不执行文件写入，分析结果交由 orchestrator 透传写入 `knowledge/articles/`。编排层写入路径: `knowledge/articles/github-trending-YYYY-MM-DD-HHMM.json`。

## 注意事项

1. **不得伪造数据**：所有分析必须基于原始采集数据，禁止凭空编造技术亮点或指标
2. **事实核查**：引用的指标、方法名称必须与原始来源一致，不确定的标注"据官方描述"
3. **评分纪律**：严格控制高分比例，大多数项目应在 5-7 分区间
4. **摘要精炼**：50 字是硬上限，宁短勿长，宁具体勿笼统
5. **信息补充**：仅当原始数据信息不足以完成分析时，才通过 WebFetch 补充信息，且补充内容必须标注来源
6. **禁止跳过分析**：每条原始数据都必须经过完整分析流程，不得因"不够重要"而跳过

## 输出格式

```json
[
  {
    "id": "gh-20260420-deepseek-r1",
    "title": "DeepSeek-R1 开源推理模型发布",
    "source_url": "https://github.com/deepseek-ai/DeepSeek-R1",
    "source_type": "github_trending",
    "summary": "开源推理模型，强化学习训练，推理能力接近 o1。",
    "tags": ["llm", "reasoning", "open-source", "deepseek"],
    "category": "model-release",
    "importance": "high",
    "status": "analyzed",
    "language": "zh-CN",
    "collected_at": "2026-04-20T08:30:00Z",
    "analyzed_at": "2026-04-20T08:35:00Z",
    "metadata": {
      "stars": 15000,
      "language": "python"
    },
    "collected_by": "collector",
    "raw_written_by": "orchestrator",
    "analyzed_by": "analyzer",
    "analysis": {
      "highlights": [
        "采用 GRPO 强化学习算法，无需 SFT 即可实现推理能力提升",
        "在 MATH 基准上达到 79.8%，接近 o1 的 85.5%"
      ],
      "score": 9,
      "score_reason": "开源社区首个在推理能力上接近闭源顶级模型的成果，且训练方法可复现，对行业格局有实质影响"
    }
  }
]
```

### 字段说明

#### 知识条目字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 唯一标识，格式 `gh-{日期}-{简短标识}`，继承自原始采集数据 |
| `title` | string | 是 | 中文标题，简洁概括项目核心，继承自原始采集数据 |
| `source_url` | string | 是 | 项目原始链接，继承自原始采集数据 |
| `source_type` | string | 是 | 来源类型，固定为 `github_trending`，继承自原始采集数据 |
| `summary` | string | 是 | AI 精炼的中文摘要，≤50 字，由 analyzer 生成替换原始摘要 |
| `tags` | string[] | 是 | 标签，3-5 个，从预定义标签集中选取，由 analyzer 重新分配 |
| `category` | string | 是 | 分类: `model-release` / `paper` / `tool` / `tutorial` / `industry`，由 analyzer 判定 |
| `importance` | string | 是 | 重要性: `high`(score ≥ 7) / `medium`(5 ≤ score ≤ 6) / `low`(score ≤ 4)，由 analyzer 根据评分映射 |
| `status` | string | 是 | 状态，由原始的 `raw` 变更为 `analyzed` |
| `language` | string | 是 | 内容语言标识，固定为 `zh-CN`，继承自原始采集数据 |
| `collected_at` | string | 是 | ISO 8601 采集时间，继承自原始采集数据 |
| `analyzed_at` | string | 是 | ISO 8601 分析完成时间，由 analyzer 生成 |
| `metadata` | object | 否 | 来源相关元数据（stars、language、forks 等），继承自原始采集数据 |
| `collected_by` | string | 是 | 采集者标识，固定为 `collector`，继承自原始采集数据 |
| `raw_written_by` | string | 是 | raw 文件写入者，固定为 `orchestrator`，继承自原始采集数据 |
| `analyzed_by` | string | 是 | 分析者标识，固定为 `analyzer`，由 analyzer 填写 |
| `analysis` | object | 是 | 分析详情，由 analyzer 生成，详见下方 |

#### analysis 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `analysis.highlights` | string[] | 是 | 技术亮点，2-3 条，必须引用具体指标或方法名称，禁止空洞描述 |
| `analysis.score` | number | 是 | 评分，1-10 整数，单次分析中 9-10 分不超过 2 个 |
| `analysis.score_reason` | string | 是 | 评分理由，说明为什么是这个分数而不是更高或更低 |

#### 字段来源溯源

| 字段 | 来源 | 说明 |
|------|------|------|
| `id` / `title` / `source_url` / `source_type` / `collected_at` / `metadata` / `collected_by` / `raw_written_by` / `language` | 原始采集数据 | 直接继承，analyzer 不修改 |
| `summary` | analyzer | 基于原始摘要精炼，≤50 字 |
| `tags` | analyzer | 基于分析结果重新分配，3-5 个 |
| `category` | analyzer | 根据项目性质判定分类 |
| `importance` | analyzer | 根据 score 映射: high/medium/low |
| `status` | analyzer | 从 `raw` 变更为 `analyzed` |
| `analyzed_at` | analyzer | 分析完成时的时间戳 |
| `analyzed_by` | analyzer | 固定值 `analyzer` |
| `analysis` | analyzer | 分析详情（highlights、score、score_reason） |