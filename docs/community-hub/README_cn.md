# 社区 Hub —— 没有自己的服务器，也能保温订阅窗口

<a href="./README.md">English</a> · <strong>简体中文</strong>

`https://awewarm.wehuman.top` 是由 awewarm 开发者运营的社区 [awewarm-hub](https://github.com/wehuman01/awewarm-hub) 实例。awewarm 靠在正确的时刻发一条最小请求来保持 AI 编程套餐的订阅窗口开启——通常由你自己的机器按计划触发，机器也就必须醒着。这个 hub 改由一台常开服务器代发：你的笔记本可以合盖、关机、人在远方，订阅窗口照样全天候保温。

采用邀请制配对——邀请码的获取方式见[联系方式](#联系方式)。

## 先读这条 —— 信任规则

hub 用**你的 API key** 发送保温请求，因此 key 的明文会经过服务器内存。使用这个服务，等于信任运维者（项目开发者）和那台机器的 root。不信任的人运营的服务器不是放你 key 的地方——这种情况请自建：在你自己的任意机器上跑 [`awewarm serve`](../../README_cn.md#远程服务器--独享自己的盒子或共享-hub)，或自建一个私有的 awewarm-hub。

把影响面压到最小的两件事，直说：

- 你的 key **永不落服务器磁盘**。只存在于内存中，由你的机器经 TLS 推送；服务器重启后由你的机器自动补推。
- 其余一切——配置、调度、key 的主副本——都留在**你自己的机器**上。随时可以用 `awewarm config set <id> --local` 把任何连接收回本地，没有任何东西被锁在服务端。

## hub 能保温什么

| 连接 | 例子 | hub 能保温？ |
| --- | --- | --- |
| 订阅 endpoint | 任何 OpenAI Chat / OpenAI Responses / Anthropic 兼容 API（base URL + API key） | **能** —— 这正是 hub 的职责 |
| 本机 CLI 账号 | 本机的 `claude` / `codex` 登录 | **不能** —— 登录态在你机器上，awewarm 用后台调度器在本地保温 |

如果你只有本机 CLI 账号，其实不需要 hub：`awewarm init` 一次搞定本地保温。本指南接下来的部分针对第一种。

## 快速开始

交互式步骤（`config add`）请在你自己的终端里完成；其余命令都可以交给你的 AI agent 代跑（见仓库的 [README.ai.md](https://github.com/wehuman01/awewarm/blob/main/README.ai.md)）。

### 1. 安装

需要 Python ≥ 3.9：

```bash
pip3 install awewarm
```

在 Claude Code、Codex 或其他编程 agent 里？让 agent 代劳——对它说：

```text
阅读 https://github.com/wehuman01/awewarm/blob/main/README.ai.md 并按其指引安装和配置 awewarm。
```

这会装好 CLI **和 `awewarm` skill**——之后你直接开口就能操作 awewarm（"下次保温是什么时候？""把 glm 委托给社区 hub"）。本指南里的只读步骤也都可以由 agent 代跑；只有下面交互式的 `config add` 留在你自己的终端里完成。

### 2. 添加第一个连接

`awewarm config add` 会引导你完成：

```bash
awewarm config add
```

- **订阅 endpoint** —— 选择 "Subscription endpoint"，选协议（OpenAI Chat / OpenAI Responses / Anthropic Messages），再输入 API base URL、API key 和模型。它会立刻发一条最小测试请求，坏 key 或错模型当场暴露，而不是等到早上六点。key 存入 `~/.config/awewarm/secrets.json`（权限 600）——绝不出现在 config.json 里，也永不落 hub 的磁盘。
- **本机账号** —— 选择检测到的 `claude` / `codex` 登录（同一条命令也能重新添加之前删掉的账号）。它在本地保温，无法委托。

之后 `awewarm status` 会按 id 列出你的连接（id 由你输入的名字派生——比如 `glm`）；下面各命令里的 `<id>` 指的就是它。

### 3. 与 hub 配对

```bash
awewarm remote connect https://awewarm.wehuman.top --invite awi_...
```

邀请码一次性、有时效（通常 48 小时）。配对时会打印一次你的个人 token 并自动存入 secrets.json——**自己留一份副本**：不重新申请邀请码的话，它是回到你账号的唯一途径。

### 4. 委托订阅连接

```bash
awewarm config set glm --remote     # 换成你的连接 id
awewarm status --remote             # 服务器健康行 + 已委托连接
```

从此由 hub 按计划全天候发保温请求。固定时间按**你机器的时区**执行——时区随推送传递。你的笔记本只需要在修改调度时在线：改动会自动推送，离线期间的改动在下次 `awewarm remote push` 时对账同步。

### 5. 不需要本地调度器 —— 除非你还留着本地连接

全部委托出去后，hub 会替你 tick；`awewarm scheduler install` 用不上（在这样的机器上安装时它还会先询问）。同时还留着本机 `claude` / `codex` 账号要保温？为它装上调度器即可。

## 常见问题

**Token 丢了。** 找运维者——服务器特意保留了可找回的 token。用它重连：

```bash
awewarm remote connect https://awewarm.wehuman.top --token <它>
```

——还是同一个账号、同一批连接。

**服务器重启了 / status 显示 "key missing"。** 设计上就无害：任何本地命令（或 `awewarm remote push`）都会重新认领并补推 key；期间到期的时间点会在补跑窗口内补发，语义和睡醒的笔记本完全一致。

**配额限制。** 每个用户可保留少量委托连接、绑定少量机器（通常一台）。触到上限时 403 报错会写明具体数字——找运维者提高额度，或发一个新邀请码。

**服务哪天没了怎么办？** 这是开发者个人运营的服务，没有可用性承诺。一旦停服，把连接收回本地（`awewarm config set <id> --local`）——配置和 key 从未离开过你的机器，本地调度从服务器停下的地方无缝接续。

**不想用了。**

```bash
awewarm config set glm --local      # 先收回每个已委托的连接
awewarm remote disconnect           # 再忘掉服务器
```

## 联系方式

- **邀请码**：发邮件到 **peng@wehuman.top**——说明你是谁、想保温哪个套餐。
- **awewarm 本身的 bug**：[GitHub issues](https://github.com/wehuman01/awewarm/issues)。
