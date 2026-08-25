# awewarm 社区版开测：10 个名额，先到先得

![awewarm](../../../logo/hero2.webp)

awewarm 的原理是挑准时机发一个极小的请求，把 AI 编程套餐的订阅窗口一直续着。本地跑的时候，到点了它甚至能把睡着的笔记本唤醒——这已经够用了。它搞不定的只有一种情况：机器关机。晚上关机、装包里托运、人出差电脑留家里，这些时候排程就断了。社区版 hub 补上的就是这最后一环：换一台 7×24 在线的服务器来发这些请求，你电脑关机都不怕，订阅窗口照样全天候热着。

今天开始，`https://awewarm.wehuman.top` 这个社区版 hub 招第一批测试用户：**10 个名额，先到先得。**

## 怎么申请

发封邮件就行：

- **收件人**：[peng@wehuman.top](mailto:peng@wehuman.top)
- **写清楚**：你是谁，想给哪个套餐保温（GLM token 套餐、豆包，或者任何 OpenAI Chat / OpenAI Responses / Anthropic 兼容的订阅接口，base URL + API key 就算）

申请就这些。我会回你一个一次性邀请码（`awi_...`）。10 个名额发完即止——后来的邮件自动进下一轮的等候名单。

## 测试用户能拿到什么

- **全天候保温。** 固定时间点按你电脑的时区跑，推送的时候跟着带过去。你只有改排程时才需要把电脑联网——改完自动重新推送。
- **还是你熟悉的那个 awewarm。** 五分钟搞定：`awewarm config add` 加连接，`awewarm remote connect` 配对，`awewarm config set <id> --remote` 委托出去。之后就交给 hub 帮你跳。
- **随时能反悔。** 你的配置、排程、密钥的原件从头到尾都在你自己机器上。`awewarm config set <id> --local` 一条命令随时收回，本地排程接着服务器停的地方继续跑，一点不耽误。

完全没用过 awewarm？让你家 agent 装一下：

> "Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it to install and configure awewarm."

## 我会尽量保护

hub 发保温请求用的是**你自己的 API key**，也就是说密钥明文会过一遍服务器的内存。用这个服务，等于你信任运营者（我，项目作者本人）和这台机器的 root。爆炸半径能控制住，靠的是这几点：

- 你的 key **永远不落服务器的盘**。只在内存里，由你的机器走 TLS 推上去，服务器重启后会自动重新推。
- 其他一切——配置、排程、密钥原件——都**留在你机器上**。没有任何东西被锁死。

这个取舍你不接受也行：自己跑一个，`awewarm serve` 起在任何你控制的机器上，或者搭个私有的 [awewarm-hub](https://github.com/wehuman01/awewarm-hub)。

## hub 能做什么

| 连接类型 | hub 能保温吗？ |
|---|---|
| 订阅接口（OpenAI Chat / OpenAI Responses / Anthropic 兼容，base URL + API key） | **能**——这就是 hub 的本职 |
| 本地 CLI 账号（`claude` / `codex` 登录态） | 不能——登录态在你机器上，awewarm 用本地后台排程给它保温 |

只有本地 CLI 账号？那你用不上 hub——`awewarm init` 一条命令本地保温就配好了。

## 你可能想问的

**有没有可用性保证？** 没有，hub 是我个人在运营。哪天它停了，`awewarm config set <id> --local` 把连接收回来就行——你的配置和 key 从没离开过你的机器。当然如果服务停了，我也会通知大家。

**服务器重启了 / status 显示 "key missing"。** 不用慌，就是这么设计的：你下一条本地命令会自动重新认领服务器、重推密钥；期间到点的槽位会在追赶窗口里补发，跟笔记本睡醒补跑是一个逻辑。

**token 丢了。** 服务器专门留了找回机制，说一声就行，然后 `awewarm remote connect https://awewarm.wehuman.top --token <it>` 重连——账号、连接都还在。

**有数量限制吗？** 每个 tester 能委托的连接数、能配对的机器数都有个小上限（一般就是一台）。撞上限了？找我说一声。

**不想用了。** `awewarm config set <id> --local` 把每个委托的连接收回来，再 `awewarm remote disconnect` 忘掉服务器。干干净净，不留东西。

## 现在就申请

10 个名额，先到先得。

发邮件到 [peng@wehuman.top](mailto:peng@wehuman.top)——你是谁、想保温哪个套餐。bug 请提 [GitHub issues](https://github.com/wehuman01/awewarm/issues)。

## awewarm 系列文章

- [awewarm：牛来，让你的ai订阅时刻热起来](https://mp.weixin.qq.com/s/HYAzfUPF_PUEfio4nZs1KA)



## More from mugpeng

awewarm 是 aweteam 生态的一部分：

- **[aweskill](https://aweskill.webioinfo.top/)** — CLI 优先的技能包管理器，支持 47+ AI 编程 agent
- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — Claude Code、Codex、OpenCode 的 agent 配置切换器
- **[awerouter](https://github.com/mugpeng/awerouter)** — 智能路由器，用结构信号把请求分给 Flash 或 Pro 模型
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — AI 编程会话管理器，按 profile 恢复现场
- **[aweshare](https://github.com/wehuman01/aweshare)** — 本地优先的 AI 能力中继：共享你的 GPU 和 API key，但 key 不出门
- **[awewarm](https://github.com/mugpeng/awewarm)** — 订阅窗口保温器，让 AI 编程套餐的窗口 predictable 地开着
