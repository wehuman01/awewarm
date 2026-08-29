# awewarm 更新：codex登录账号也能委托给服务器了

![awewarm](../../../logo/hero.png)

前几天社区版开测那篇文章里，有一张表我写得很干脆：订阅接口（base URL + API key）hub 能保温；本地 CLI 账号（`claude` / `codex` 登录态）——**不能**。理由也直白：登录态在你机器上，服务器拿不到。

这版更新就是来推翻这句话的。Claude Code 和 Codex 的登录账号现在也能委托了：`awewarm config set <id> --remote` 一条命令，本地机器读出自己的登录凭据（macOS 钥匙串里的 Claude 凭据、`~/.codex/auth.json`），像 API key 一样推到服务器上，让服务器拿着凭据替你跑 CLI。说了算的始终是你本机的登录——每次推送和后台同步（最多半小时一轮）都会重新读一遍，凭据指纹对不上就自动重推，`status` 里也能看到当前指纹。

信任模型没变：凭据只存在服务器内存里，不落盘。有一处新讲究——登录凭据是整个账号共用的，名下所有订阅都挂在它下面，所以委托时会多问一道，确认框里写明目标服务器的 URL。脚本里加 `--yes` 就能跳过。

## 服务器上没装 CLI？也行

委托原本要求服务器装着对应的 CLI，这个前提这次也松掉了。目标机器上没有 `claude` / `codex`？照样能推。服务器会把这条连接记成原生模式，到点直接发原生 HTTPS 请求——codex 用它的 OAuth token 构造 ChatGPT 后端的 SSE 请求，claude 带着 accessToken 发 POST。推送时会先离线校验一遍凭据，注定跑不通的委托当场就被拦下，错误信息直接告诉你怎么修：在本机 `claude /login` 或 `codex login`，然后重新 push。之后那台机器装上了 CLI，再推一次就切回 CLI 模式。

## 换新电脑：一条备份，一条恢复

配对是绑机器的，以前换台电脑就得重新配对，还得找运营者放行。现在有专门的迁移命令：

```bash
awewarm config backup        # 生成备份，打印路径
# 把备份文件安全地带到新机器（它含明文密钥，传输自己加密）
awewarm config restore awewarm-backup.tar.gz
```

备份就是一个 tar.gz，里面打包了 config、secrets、state 和 machine-id。在新机器上 restore 之后，hub 把它认作原来那台机器——不用新的配对名额，不用运营者介入，排程和密钥原样接上。新机器上已经有同名文件时，restore 拒绝覆盖（加 `--force` 才动手）；压缩包里混进不认识的东西，也会拒收。

## 笔记本常年不在线？key 可以选择存在服务器上

默认的做法是密钥只放在服务器内存里，你的机器一上线就自动重推。代价是：服务器重启时你恰好关机，这台机器名下的保温就得等你回来。机器要是偶尔才联网一次，现在可以主动选择把 key 存到服务器盘上（`keys.json`，明文、0600），重启也不丢：

```bash
awewarm config set glm-sub --persist-key on
```

这个选项默认关闭，我们也不推荐开——明文 key 落到服务器盘上，信任范围就从"服务器内存"扩大到"整台机器的磁盘"了。所以每一步都设了确认：开启时默认选"否"，关闭时会同时告诉你两个后果——服务器立刻删 key；之后重启再碰上你离线，保温又会挂起。hub 那头还得运营者先放行（`awewarm-hub config --persist-keys on`），没放行的 hub 收到持久化推送直接 403。`status` 会标明 key 现在住在哪边，收回连接时磁盘副本一并清掉。

## 你说，awewarm 跑

| 你说 | awewarm 跑 |
|---|---|
| "我的 Claude Code 登录也要在服务器上保温。" | `awewarm config set <id> --remote` |
| "我刚在本机重新登录了。" | `awewarm remote push`（或者等着——后台同步会自动重推） |
| "服务器上不想装 CLI。" | 直接 push，走原生 HTTPS 模式 |
| "我要换新电脑了。" | `awewarm config backup` → 新机器上 `awewarm config restore` |
| "我这台机器几周才开一次机。" | `awewarm config set <id> --persist-key on` |

一句话总结：委托不再挑连接的类型——从 API key 到 CLI 登录账号，整台机器的保温都能交给服务器。

## 试用

### 让 agent 装给你

在 Claude Code、Codex 或任何编程 agent 里说一句：

```text
Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it to install and configure awewarm.
```

### 自己动手

```bash
pip install awewarm

# CLI 登录账号一样能委托
awewarm config set claude-main --remote

# 换机迁移
awewarm config backup

# 笔记本常年不在线？可选
awewarm config set glm-sub --persist-key on
```

## 现在就申请

不想自己搭服务器？社区版 hub（[awewarm.wehuman.top](https://awewarm.wehuman.top)）还在招测试用户：10 个名额，先到先得。

发邮件到 [peng@wehuman.top](mailto:peng@wehuman.top)——你是谁、想保温哪个套餐。bug 请提 [GitHub issues](https://github.com/wehuman01/awewarm/issues)。

## awewarm 系列文章

- [awewarm：牛来，让你的ai订阅时刻热起来](https://mp.weixin.qq.com/s/HYAzfUPF_PUEfio4nZs1KA)

## More from mugpeng

awewarm 是 aweteam 生态的一部分：

- **[aweskill](https://aweskill.webioinfo.top/)** — CLI 优先的技能包管理器，支持 47+ AI 编程 agent
- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — Claude Code、Codex、OpenCode 的 agent 配置切换器
- **[awerouter](https://github.com/mugpeng/awerouter)** — 智能路由器，用结构信号把请求分给 Flash 或 Pro 模型，减少不必要的模型开销
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — 收藏、分类、恢复 AI 编程会话，还能搭配aweswitch 实现保存配置，一键启动
- **[aweshare](https://github.com/wehuman01/aweshare)** — 通过自建 Hub 共享本地 Ollama/vLLM 或已授权的 OpenAI/Anthropic 后端，实现token 的共享经济
- **[awewarm](https://github.com/wehuman01/awewarm)** — 订阅窗口保持器，让 AI 编程套餐的窗口持续激活，无论是本地设置，还是通过远程连接的服务器
