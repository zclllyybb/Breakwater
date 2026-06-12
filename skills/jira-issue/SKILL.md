---
name: jira-issue
description: 当需要访问 Jira 内容时使用，包括创建、评论、搜索 issue 以及获取具体 issue 内容。
---

# Jira Issue

## Overview

使用当前 skill 目录内脚本访问 Jira REST API v2。命令只依赖 skill 内部结构：`pyproject.toml` 与 `scripts/`。

```bash
uv run --project . python3 scripts/<script>.py ...
```

以下命令都假设当前目录就是 `jira-issue` 这个 skill 目录。默认配置来源按以下优先级生效：

- 命令行参数
- 当前 shell 环境变量
- 当前 workspace 下的 `.env`

`JIRA_URL` 必须通过命令行、环境变量或 `.env` 显式配置。

`--dry-run` 只打印 payload，不会发请求，也不要求 `JIRA_URL` / `JIRA_TOKEN`。

## Quick Start

1. 确认系统里已经有 `uv`；如果没有，让用户自行按官方文档安装。
2. 如需默认凭据，可写入当前 workspace 下的 `.env`；否则直接传 CLI 参数或使用当前 shell 环境变量。
3. Issue 描述和评论内容统一走 `--description-file` / `--comment-file`；传 stdin 时使用 `-`。
4. 先执行 `--dry-run` 预览 payload。
5. 确认无误后去掉 `--dry-run` 正式提交，并记录返回的 `key` / `self` 或 `comment_id` / `self`。

## Prerequisites

- 需要 `uv`
- 如果 `uv` 不在 PATH，提示用户自行安装；本 skill 不提供安装流程

## Create Issue

```bash
uv run --project . python3 scripts/jira_create_issue.py \
  --project-key <PROJECT_KEY> \
  --summary "..." \
  --description-file /path/to/description.md \
  --dry-run
```

常用参数：
- `--auth`：`bearer` 或 `basic`（默认 bearer）
- `--project-key`：也可用 `JIRA_PROJECT`
- `--issue-type`：也可用 `JIRA_ISSUE_TYPE`，默认 Bug
- `--description-file`：从文件读取描述；传 `-` 时从 stdin 读取
- `--label`/`--component`/`--assignee`/`--priority`
- `--affects-version`/`--fix-version`：对应 Affects Version / Fix Version（可重复）
- `--epic`：Epic issue key，创建成功后会将 issue 挂载到该 epic
- `--print-payload`/`--dry-run`

输出：`key` 与 `self`（若返回）。

经验示例：
```bash
uv run --project . python3 scripts/jira_create_issue.py \
  --project-key OPS \
  --issue-type Bug \
  --summary "Regression job failed with an out-of-memory error" \
  --description-file /path/to/description.md \
  --priority Highest \
  --assignee alice \
  --label storage \
  --label ingestion \
  --affects-version 1.2.3 \
  --fix-version 1.2.4
```

挂载到 Epic 的示例：
```bash
uv run --project . python3 scripts/jira_create_issue.py \
  --project-key OPS \
  --issue-type Bug \
  --summary "Planner produces an incorrect execution plan" \
  --description-file /path/to/description.md \
  --label planner \
  --epic OPS-1000 \
  --dry-run
```
创建成功后会输出 `epic_linked=OPS-1000` 表示已挂载到 epic。

stdin 示例：
```bash
uv run --project . python3 scripts/jira_create_issue.py \
  --project-key QA \
  --issue-type 任务 \
  --summary "..." \
  --description-file - <<'EOF'
第一行

第二行
EOF
```

## Comment Issue

```bash
uv run --project . python3 scripts/jira_comment_issue.py \
  --issue-key OPS-12345 \
  --comment-file /path/to/comment.md \
  --dry-run
```

常用参数：

- `--issue-key`：目标 Issue key
- `--comment-file`：评论内容文件；传 `-` 时从 stdin 读取
- `--print-payload` / `--dry-run`：预览请求

输出：`comment_id` 与 `self`（若返回）。

## Search Issues

使用 JQL 检索 Jira Issue（`POST /rest/api/2/search`）。搜索是只读操作，无需 `--dry-run`，直接执行即可。推荐使用 `--jql` 原始 JQL 方式，灵活度最高。

```bash
uv run --project . python3 scripts/jira_search_issue.py \
  --jql 'project = OPS AND labels = "planner"' \
  --max-results 10
```

### JQL 语法速查（已验证）

JQL 格式：`field operator value [AND/OR field operator value ...] [ORDER BY field ASC/DESC]`

**字符串匹配**（`~` 为包含，`!~` 为不包含）：

| 字段 | 说明 | 示例 |
|------|------|------|
| `summary` | 摘要 | `summary ~ "Planner"` |
| `description` | 描述 | `description ~ "内存超限"` |
| `text` | 全文（摘要+描述+评论） | `text ~ "coredump"` |
| `comment` | 评论 | `comment ~ "修复"` |
| `environment` | 环境 | `environment ~ "UAT"` |

**精确匹配字段**（`=` 为等于，`!=` 为不等于，`IN` / `NOT IN` 为多值）：

| 字段 | 说明 | 示例 |
|------|------|------|
| `project` | 项目 Key | `project = OPS` |
| `labels` | 标签（单值用 `=`，多值用 `IN`） | `labels = "planner"` / `labels IN ("ingestion", "storage")` |
| `assignee` | 经办人 | `assignee = alice` |
| `reporter` | 报告人 | `reporter = alice` |
| `issuetype` | 类型 | `issuetype = Bug` |
| `priority` | 优先级 | `priority = High` |
| `component` | 组件 | `component = document`（需用项目实际组件名） |
| `affectedVersion` | 影响版本 | `affectedVersion = "3.1.0"`（`=` 精确匹配，`~` 不适用） |
| `fixVersion` | 修复版本 | `fixVersion = "3.1.4"` 或 `fixVersion >= "3.1.0"` |
| `"Epic Link"` | Epic 链接 | `"Epic Link" = OPS-1000` |
| `status` | 状态 | `status = "In Review"` |
| `issuekey` | Issue Key | `issuekey >= OPS-1000` |

**日期筛选**：

| 语法 | 说明 |
|------|------|
| `created >= -7d` | 最近 7 天创建 |
| `updated >= -1w` | 最近 1 周更新 |
| `created >= "2026-04-01"` | 指定日期后创建 |
| `created >= startOfWeek()` | 本周起 |
| `resolved >= -30d` | 最近 30 天解决 |

**其他操作符**：`IS` / `IS NOT`（空值判断）、`WAS` / `WAS NOT`（历史状态）、`CHANGED`（字段变更）。

> **注意**：`status` 和 `issuetype` 的 JQL 值必须用**英文名称**或**数字 ID** 而非中文翻译名（如 `Bug` 而非 `故障`，`"In Review"` 而非 `处理中`）。可用 `GET /rest/api/2/status` 和 `GET /rest/api/2/issuetype` 查看有效值。

### 搜索方式

#### 原始 JQL 搜索（推荐）

传入 `--jql` 直接使用 JQL，最灵活：

```bash
# 按 label + 关键词 + 近期创建
uv run --project . python3 scripts/jira_search_issue.py \
  --jql 'project = OPS AND labels = "planner" AND summary ~ "Planner" AND created >= -7d' \
  --max-results 10

# 按多个 label + 优先级 + 时间排序
uv run --project . python3 scripts/jira_search_issue.py \
  --jql 'project = OPS AND labels IN ("ingestion", "storage") AND priority = High ORDER BY created DESC' \
  --max-results 20

# 按经办人 + 状态
uv run --project . python3 scripts/jira_search_issue.py \
  --jql 'project = OPS AND assignee = alice AND status = "In Review"'
```

#### 参数组合搜索

通过多个筛选参数自动拼接 JQL，适合简单查询：

```bash
# 按多个 label + assignee 搜索
uv run --project . python3 scripts/jira_search_issue.py \
  --project-key OPS \
  --label storage \
  --label ingestion \
  --assignee alice \
  --max-results 10

# 全文关键词搜索
uv run --project . python3 scripts/jira_search_issue.py \
  --project-key OPS \
  --keyword "内存" \
  --keyword-field text \
  --max-results 5
```

> 参数组合搜索只是 JQL 生成的快捷方式，复杂查询建议直接用 `--jql`。

### 常用参数

| 参数 | 说明 |
|------|------|
| `--jql` | 原始 JQL（推荐，覆盖所有其他筛选参数） |
| `--project-key` | 项目 Key，默认 `JIRA_PROJECT` |
| `--keyword` | 搜索关键词 |
| `--keyword-field` | 关键词作用域：`summary` / `description` / `text` / `comment` / `environment`（默认 `text`） |
| `--label` | 按 label 筛选（可重复） |
| `--assignee` | 按经办人筛选（Jira 用户名） |
| `--status` | 按状态筛选（英文名或 ID，如 `"In Review"` 或 `3`） |
| `--issue-type` | 按类型筛选（英文名，如 `Bug`） |
| `--priority` | 按优先级筛选（如 `High`） |
| `--component` | 按组件筛选 |
| `--affects-version` / `--fix-version` | 按版本筛选 |
| `--epic` | 按 Epic Key 筛选 |
| `--reporter` | 按报告人筛选 |
| `--max-results` | 返回条数上限（默认 50） |
| `--fields` | 自定义返回字段（默认已包含 key,summary,status,issuetype,priority,assignee,labels,created,comment,attachment；`*` 为全部） |
| `--output-json` | 输出完整 JSON |
| `--output-file` | 将完整 JSON 写入文件，stdout 只输出 total/returned/path/bytes/keys 等小摘要 |
| `--show-comments` | 在紧凑输出中显示完整评论内容和作者时间 |
| `--show-attachments` | 在紧凑输出中显示附件文件名、大小和下载 URL |
| `--show-description` | 在紧凑输出中显示 Issue 描述 |

### 输出格式

**紧凑格式（默认）**：每行一个 issue 基本字段，评论数和附件数单独一行，用 `--show-comments` / `--show-attachments` / `--show-description` 查看详细内容：

```
total=8
key=OPS-1234 | summary=...NPE and break retry semantics | status=In Review | issuetype=Bug | ...
  comments=2
  attachments=1
```

**带评论和附件详情**（`--show-comments --show-attachments`）：

```
key=OPS-1234 | summary=... | status=In Review | ...
  [comment] Alice @ 2026-04-24T15:54:11.105+0800
    !screenshot-1.png|thumbnail!
  [comment] Bob @ 2026-04-24T16:46:36.154+0800
    https://github.com/octo-org/example-repo/pull/100
  attachments=1
    screenshot-1.png (779614 bytes, image/png) -> https://jira.example.com/secure/attachment/100/screenshot-1.png
```

**JSON 格式**（`--output-json`）：输出完整 API 响应 JSON。结果可能很大时使用 `--output-file xxx`，再用 `jq` / `rg` / 小脚本按需提取片段，不要把完整文件直接 `cat` 回终端。

### 典型调查流程

从 bug 现象（报错信息、堆栈、函数名）出发定位相关 Issue：

```bash
# Step 1: 用堆栈中的函数名全文搜索
uv run --project . python3 scripts/jira_search_issue.py \
  --jql 'project = OPS AND text ~ "planner exception"' \
  --show-comments --show-description --max-results 10

# Step 2: 对找到的 issue 查看完整评论和附件
uv run --project . python3 scripts/jira_search_issue.py \
  --jql 'project = OPS AND issuekey = OPS-1234' \
  --show-comments --show-attachments --show-description

# Step 3: 从评论中提取 PR/分支信息，通过 gh 等操作查看 PR 标签，追溯到修复版本
```

> 评论中常包含关键信息：**修复 PR 链接**、**cherry-pick 分支**、**复现条件**、**根本原因分析**。附件（screenshot、日志）可通过 `content` 字段的 URL 查看。

## Config

发送请求时需要：

- `JIRA_TOKEN`

可选默认值：

- `JIRA_URL`：Jira base URL，例如 `https://jira.example.com`
- `JIRA_USER`：仅 `--auth basic` 时需要
- `JIRA_PROJECT`
- `JIRA_ISSUE_TYPE`

## Common Mistakes

- `--assignee` 使用 Jira 用户名，不是邮箱，例如 `alice`
- 多行内容不要试图走内联参数；统一用 `--description-file` / `--comment-file`
- 需要动态内容时，用 `--description-file -` 或 `--comment-file -` 配合 heredoc
- 这些命令默认在 skill 目录内执行；如果不在该目录，先切到 skill 目录再运行
- 搜索时 `status` 和 `issuetype` 在 JQL 中必须用**英文名或 ID**（如 `Bug` 而非 `故障`，`"In Review"` 而非 `处理中`）。中文名只在 UI 显示层生效，JQL 查询无效。
