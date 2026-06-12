---
name: breakwater-reply
description: Reply to a Breakwater Lark request by recording a slot-bound response through the local Breakwater CLI.
---

# Breakwater Reply

Use this skill when a task prompt contains a `slot_id`.

You must not send Lark/Feishu messages or Jira comments directly. Breakwater owns the delivery path.
After analysis is complete, record the user-facing reply through the CLI:

```bash
BREAKWATER_DB=/absolute/path/to/breakwater.db uv run breakwater reply <slot_id> --message "your reply"
```

For multi-line replies, write the content to a workspace file and pass it:

```bash
BREAKWATER_DB=/absolute/path/to/breakwater.db uv run breakwater reply <slot_id> --message-file .breakwater/reply-<slot_id>.txt
```

Rules:

- Use exactly the `slot_id` from the prompt.
- Do not invent a new slot.
- Do not call Lark or Jira write APIs.
- Call `breakwater reply` before the task ends.
- After the command succeeds, keep the final assistant message short.

Jira access is available through the `jira-issue` skill. Breakwater also exposes a convenience wrapper:

```bash
uv run breakwater jira-search --jql 'project = OPS ORDER BY updated DESC' --max-results 5
```
