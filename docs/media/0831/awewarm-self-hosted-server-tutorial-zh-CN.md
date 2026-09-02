# awewarm 教程：把一台闲置电脑变成订阅激活服务器

![awewarm](../../../logo/hero.png)

前几篇聊过委托是怎么回事：找一台永不关机的机器，替你的笔记本发心跳。那这台机器从哪来？买社区版名额是一条路，但名额有限。其实现成的盒子就够：几块钱一月的 VPS、家里吃灰的 NAS、树莓派，或者旧笔记本插上电塞进柜子。这篇带你从零搭一遍，顺利的话二十分钟搞定。

前提一条：你自己的电脑上装好了 awewarm，而且至少有一条连接在跑（没有就 `awewarm init`，两条命令的事）。

GitHub: [github.com/wehuman01/awewarm](https://github.com/wehuman01/awewarm)

## 先想好：solo 还是 hub？

一台机器只给你自己用，装 awewarm 就够了（solo）。想让家人朋友也来用一台机器，各人管各人的连接，互不干扰，就装 awewarm-hub（hub）。下面每一步都把两种装法并排写出来，挑你那条看就行。以后想换也不用重来，数据目录是通用的，直接迁。

## 第一步：装包

```bash
ssh my-server

pip3 install awewarm        # solo：只给自己用
pip3 install awewarm-hub    # hub：给一群人用
```

## 第二步：起服务

```bash
# solo
awewarm serve               # 监听 127.0.0.1:8790，数据在 ~/.awewarm-server

# hub
awewarm-hub serve           # 同一个数据目录，变成多租户
awewarm-hub invite --name alice   # 发个邀请码（awi_...，一次有效，7 天过期）
```

两个服务都只监听本机，不对外开端口——怎么让别人连上，看第四步。

hub 的管理命令都在 `awewarm-hub` 底下：`invite` 发码，`list` 看用户和邀请码，`revoke` 停用，`restore` 恢复。

## 第三步：让它常驻

别用 nohup。写个 systemd 用户单元（`~/.config/systemd/user/awewarm.service`）：

```ini
[Unit]
Description=awewarm serve
After=network-online.target

[Service]
ExecStart=awewarm serve --data-dir %h/awewarm-server
# hub 模式换成这行：
# ExecStart=awewarm-hub serve --data-dir %h/awewarm-server
Restart=on-failure

[Install]
WantedBy=default.target
```

然后：

```bash
systemctl --user enable --now awewarm
loginctl enable-linger $USER   # 不登录也照常运行，服务器上必须有这条
```

## 第四步（可选）：让笔记本够得着服务器

服务器得让你的笔记本连得上，方式挑一个就行。

**方式 A：家里内网直连。** NAS、树莓派这类跟你电脑同一路由器的盒子，最简单——让服务监听内网，用内网地址连：

```bash
awewarm serve --bind 0.0.0.0          # 服务器上改一下监听地址
awewarm remote connect http://192.168.1.20:8790   # 笔记本上连内网地址
```

走明文 http 连非本机地址，awewarm 会先跟你确认一遍再发 token——这是设计好的提醒，照实确认就行。盒子只在家里 Wi-Fi 里露面的话，到这步就够了。

**方式 B：cloudflared 隧道（VPS 推荐）。** 机器在公网上，别让端口裸奔。隧道白送免费 TLS，源站 IP 还藏在 Cloudflare 后面：

```bash
cloudflared tunnel create awewarm
cloudflared tunnel route dns awewarm warm.example.com
cloudflared tunnel run --url http://127.0.0.1:8790 awewarm
```

笔记本上连 `https://warm.example.com`。cloudflared 自己也该用服务方式常驻，它家文档写得很全。

**方式 C：自己的反向代理。** 已经跑着 nginx 或 caddy 的，把 8790 反代出去、配个证书就行，效果跟隧道一样。

## 关键一步：配对要趁早

solo 有条规矩要记住：**没被认领的服务器，谁先连谁认领**——第一个到达的 token 就是主人。万一别人抢了先，你再连就是响亮的 403。

所以三选一：URL 别外传；起完 `serve` 赶紧连；或者干脆提前把 token 钉死：

```bash
awewarm serve --token awt_你自己的token
```

hub 没这个顾虑：配对得烧一张一次性邀请码，码在你手里，服务器就还是你的。

## 第五步：配对，把连接托付出去

回到你的电脑：

```bash
# solo：本地生成 token 存进 secrets.json，直接认领
awewarm remote connect https://warm.example.com

# hub：烧一张邀请码，换回你的个人 token
awewarm remote connect https://warm.example.com --invite awi_xxx

# 然后委托——两种模式，命令一模一样
awewarm config set glm --remote
```

从这步开始，solo 和 hub 就没有区别了。配对走 https 的话不会弹明文确认——那个提示是给裸 http 连公网地址准备的。

CLI 登录账号（Claude Code / Codex）一样能委托，命令相同。账号凭据管着名下所有订阅，所以会多问一道；脚本里加 `--yes`。服务器上不用装任何 CLI：有就用服务器的，没有就直接发原生 HTTPS 请求到官方后端。

## 第六步：日常怎么用

委托之后还是你熟悉的那些命令：

```bash
awewarm status               # 委托的连接按服务器的真实状态显示
awewarm status --remote      # 只看委托的，带服务器健康行（版本/运行时长/最近 tick）
awewarm run glm              # 手动补一发，服务器上执行，结果回报回来
awewarm config set glm --local   # 反悔了，收回来，本地接着跑
```

改排程不用手动推：`config set` 改完自动同步到服务器。哪天改的时候服务器正好断连，改动留在本地标成待推送，网络一通自己补上去。

## key 存在哪

默认：你的 key 只放在服务器**内存**里，不落盘。服务器重启丢了也没事，你的机器一上线就自动重新认领、重新推送；期间到点的槽位，追赶窗口里照常补发。

真有常年不联网的机器，可以把 key 写到服务器磁盘上（`--persist-key`）。但它默认关着、每一步都要确认，我们也不推荐——明文 key 落了盘，等于信了整台服务器的磁盘。细账看上一篇哲学文。

## 几个常见问题

**服务器重启了。** 什么都不用做，key 自动补推。`status` 里短暂冒出 key missing 的警告，联网就消。

**想换一台服务器。** `config set <id> --local` 一条条收回，`remote disconnect` 忘掉旧的，对着新地址重来一遍。你本地的配置和排程从头到尾没动过。

**隧道断了 / 网络抽风。** `status` 给你看的是标了"过期"的上次快照，不是空白；恢复了自动跟上。

**solo 用着用着想变 hub？** 数据目录直接给 `awewarm-hub serve` 用就行，平滑迁移，反过来也一样。

## 现在就申请

手边实在没有能常开的盒子？社区版 hub（[awewarm.wehuman.top](https://awewarm.wehuman.top)）还在招测试用户：10 个名额，先到先得。

发邮件到 [peng@wehuman.top](mailto:peng@wehuman.top)——你是谁、想保温哪个套餐。bug 请提 [GitHub issues](https://github.com/wehuman01/awewarm/issues)。

## 试用

### 让 agent 装给你

在 Claude Code、Codex 或任何编程 agent 里说一句：

```text
Read https://github.com/wehuman01/awewarm/blob/main/README.ai.md and follow it to install and configure awewarm.
```

### 自己动手

```bash
pip install awewarm

# 本地先跑起来
awewarm init

# 有盒子？二十分钟自建（照上文一步步来）
ssh my-server
pip3 install awewarm && awewarm serve
```

## awewarm 系列文章

- [awewarm：牛来，让你的ai订阅时刻热起来](https://mp.weixin.qq.com/s/HYAzfUPF_PUEfio4nZs1KA)

## Awesome 生态系统

aweshare 是一个不断壮大的 "awesome" 工具家族的一部分 — CLI 优先、本地优先，可由 AI agent 操作。

### CLI 工具

- **[aweskill](https://aweskill.webioinfo.top/)** — CLI 优先的技能包管理器，支持 47+ AI 编程 agent。
- **[aweswitch](https://github.com/Webioinfo01/aweswitch)** — Claude Code、Codex 和 OpenCode 的 agent 配置切换器。
- **[awerouter](https://github.com/mugpeng/awerouter)** — 智能路由器，使用结构信号在 Flash 和 Pro 模型之间分配请求，减少不必要的模型开销。
- **[aweshelf](https://github.com/Webioinfo01/aweshelf)** — 收藏、分类和恢复 AI 编程会话；与 aweswitch 配合保存配置并一键启动。
- **[aweshare](https://github.com/wehuman01/aweshare)** — 通过自建 Hub 共享本地 Ollama/vLLM 后端、国内编程计划或授权的 OpenAI/Anthropic 订阅 — token 的共享经济。
- **[awewarm](https://github.com/wehuman01/awewarm)** — 订阅窗口保温器，保持 AI 编程套餐窗口激活，适用于本地设置和远程 hub 服务器。
- **[awescholar](https://github.com/Webioinfo01/awescholar)** — 可由 AI agent 操作的科学文献发现和整理工具。

### 桌面应用

- **[awedot](https://awedot.wehuman.top/)** — 屏幕边缘的浮动球体跟踪当前 AI 会话：一键收藏，随时恢复，并可与 aweswitch 配合固定 agent 配置（例如使用 GLM 模型重新启动）。

### 项目集合

- **[Awesome AI Meets Biology](https://github.com/Webioinfo01/Awesome-AI-Meets-Biology)** — AI 在生物学、生物信息学和生物医学研究中应用的精选综述。由 awescholar 驱动。
- **[Awesome AI Virtual Tumor](https://github.com/Webioinfo01/Awesome-AI-Virtual-Tumor)** — 用于虚拟肿瘤建模和模拟的最先进 AI 系统精选集合：静态模型、动态模型、agent、基准测试和综述。
