---
name: breakwater-develop
description: 处理 Breakwater 中用户明确希望 Codex 修改代码、实现修复或在配置工作区执行本地开发任务的请求。
---

# Breakwater Develop

只有当 Breakwater prompt 和用户请求都指向明确的开发任务时，才使用这个 skill。例如：实现功能、修复 bug、修改测试，或做一个范围清晰的小型重构。

## 前置条件

- 找到正确的目标代码库
- 非常清楚地知道用户希望基于哪个分支开发

如果这些条件不满足，通过正常的 Breakwater reply 路径说明缺少的信息或风险，然后退出当前 skill，不再进行开发。

## 具体操作

你必须使用新的 branch 和 worktree，除非用户明确给出其他指令。建立 worktree 后按项目指令（如有）准备好环境；
你必须在新的开发分支上首先拉取远端最新内容，防止 push 以后冲突；
你必须在开发和测试、在代码库中操作前，理解相关目录的 AGENTS.md 和明确相关的 skill 并按要求操作；
你必须调用 create_goal 工具，通过 goal 模式定义并完成整个开发任务。在 goal 中，你要做的事情如下：

首先：如果有 badcase，首先尝试在开发前复现老的 badcase。然后进行此循环——

1. 按照用户要求进行开发，代码必须符合项目要求、代码习惯并达到最高标准；
2. 编译集群，如果并发数可以调整，采用不低于 -j90 的并发。如果环境提供了标准脚本，总是使用标准脚本而非裸的构建命令，除非脚本不可用；
3. 添加并完成相关的测试，确认通过且有限的相关测试（如有）未被破坏；
4. 使用 subagent 来 review 代码，subagent 需要根据实际接口进行设置——fork_turns 必须为 none，fork_context 必须为 false。如果代码库中存在 code-review 相关 skill，则必须遵守此 skill，否则尝试使用当前环境提供的 review 能力。如果都没有，你自行定义 review 要求，但必须覆盖性能、正确性、可维护性，不得遗留任何可能的问题，所有问题都必须深入代码给出明确的结论；
5. 如果 subagent 结论中提出了高优问题，你来鉴别问题是否合理，不解决此问题是否无法按高标准完成本分支的开发目的。如果是，回到第一步针对 review 意见进行迭代；
6. 如果上一问题为否，或 subagent 没有提出显著问题，则完成开发，继续以下步骤。

开发完成后，除非用户明确要求只在本地更改，否则你必须将分支发布，或者按用户要求直接发起 PR。

此外，如果用户明确要求开发过程中做或者不做某事，可以调整上述步骤，但 coding、测试、review 的整体原则必须被覆盖。

## 疑问

对于飞书或本地 Breakwater slot，完成后必须使用 prompt 中给出的 Breakwater reply CLI 记录面向用户的结果：

```bash
BREAKWATER_DB=/absolute/path/to/breakwater.db uv run breakwater reply <slot_id> --message "summary"
```
