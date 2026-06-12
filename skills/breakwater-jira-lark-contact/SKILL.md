---
name: breakwater-jira-lark-contact
description: 读取 Jira issue 的 reporter/assignee，并在 Lark/Feishu 中定位、拉群和真实 at 对应用户时使用。
---

# Breakwater Jira Lark Contact

用于把 Jira issue 上的人映射到 Lark/Feishu 用户，并在目标群内同步。常用对象是 assignee（经办人），也可以按需读取 reporter 或 creator。

## 读取 Jira

使用 jira-issue skill 进行所有 jira 操作。

## Lark/Feishu 定位和同步

搜索目标群：

```bash
lark-cli im +chat-search --query "群名" --as user --format json --page-size 20
```

查询群成员：

```bash
lark-cli im chat.members get --as user \
  --params '{"chat_id":"oc_xxx","member_id_type":"open_id","page_size":100}' \
  --format json
```

搜索飞书用户：

```bash
lark-cli contact +search-user --query "姓名或 username" --as user --format json --page-size 10
```

如果不在群里，先拉入群：

```bash
lark-cli im chat.members create --as user \
  --params '{"chat_id":"oc_xxx","member_id_type":"open_id","succeed_type":1}' \
  --data '{"id_list":["ou_xxx"]}' \
  --format json
```

发送消息时必须使用真实 open_id at：

```bash
lark-cli im +messages-send --as bot --chat-id oc_xxx \
  --content '{"text":"<at user_id=\"ou_xxx\">Name</at> OPS-1234 needs your update."}' \
  --msg-type text \
  --idempotency-key breakwater-OPS-1234-contact
```

最后读回最近消息，确认 `mentions[].id` 是目标用户 open_id：

```bash
lark-cli im +chat-messages-list --as user --chat-id oc_xxx --page-size 5 --format json
```
