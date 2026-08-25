# awewarm Community Hub 安全加固

> 日期：2026-08-25  
> 对象：`https://awewarm.wehuman.top` 及 `awewarm-hub 0.6.0`  
> 威胁模型：攻击者知道公开域名，但没有邀请码、租户 token、用户 API key 或服务器权限。

## 结论

当前没有发现匿名攻击者能够绕过认证、读取租户状态、取得 API key，或触发真实保温请求。
邀请码和租户 token 的随机强度足够，租户工作区与撤销链路也有回归覆盖。

眼下最现实的风险不是“猜中邀请码”，而是明文 HTTP 误用和匿名请求造成的可用性攻击。
Cloudflare Tunnel 隐藏源站、减少了公网攻击面，但不能代替应用入口的限流和 HTTPS 强制策略。

## 一、HTTP 未强制升级到 HTTPS

### 已确认行为

访问 `http://awewarm.wehuman.top/healthz` 会直接返回 HTTP 200，没有跳转到 HTTPS；HTTPS
响应也没有 `Strict-Transport-Security`。

### 攻击示例

官方 awewarm 客户端会拒绝公网明文地址，但第三方脚本、旧客户端或手写请求仍可能误配：

```text
http://awewarm.wehuman.top
Authorization: Bearer awt_...
```

在不可信 Wi-Fi、公司代理或存在本地中间人的网络中，租户 token 会在客户端到 Cloudflare
边缘这一跳以明文传输。攻击者取得 token 后，虽然不能读取已有 API key，仍可管理该租户的
连接、覆盖连接配置、触发运行并消耗套餐额度。

### 待办

- [ ] Cloudflare 开启 HTTP → HTTPS 永久跳转。
- [ ] HTTPS 响应添加 HSTS；先使用较短 `max-age` 验证，不启用 `preload`，不默认覆盖其他子域名。
- [ ] 保留客户端对公网 `http://` 的拒绝，并增加对应回归测试，避免未来放宽。

### 验收

```bash
curl -I http://awewarm.wehuman.top/healthz
curl -I https://awewarm.wehuman.top/healthz
```

第一条必须跳到 HTTPS；第二条必须包含 HSTS。

## 二、公开 `/v1/join` 缺少匿名限流

### 现状

- `/v1/join` 必须公开，供用户用一次性邀请码配对。
- 邀请码使用 `secrets.token_urlsafe(16)`，约 128 bit 随机性，穷举不可行。
- 现有 60 次/分钟限制位于租户 token 认证之后，只限制已经加入的租户。
- HTTP 服务使用 `ThreadingHTTPServer`，每个请求占一个线程；连接有 30 秒空闲超时，请求体有
  256 KiB 上限，但匿名并发本身没有入口配额。

### 攻击示例

攻击者不需要有效邀请码，可以持续并发提交：

```http
POST /v1/join
Content-Type: application/json

{"invite":"awi_invalid"}
```

攻击者拿不到租户身份，但可能制造大量线程、争抢 registry 锁，使正常配对、状态查询或
cloudflared 到源站的请求变慢，最终表现为 502/504。Cloudflare 的通用 DDoS 防护不能保证会
识别这种针对单个 API 的低成本请求。

### 待办

- [ ] Cloudflare 对精确路径 `/v1/join` 设置按来源 IP 的限流；初始值建议 5 次/分钟，超限直接
  Block 10 分钟。CLI 无法处理交互式 Challenge，不使用 Managed Challenge。
- [ ] 应用层增加认证前、按来源 IP 的 join 限流；只有在源站保持 Tunnel-only 时才信任
  `CF-Connecting-IP`，否则退回 socket 地址且不得直接信任客户端伪造的头。
- [ ] 保留一个较高的全局保险上限，防止分布式来源绕过单 IP 限制；不得让单一匿名来源占满
  所有合法用户共享的低额度桶。
- [ ] 为并发无效 join、限流恢复、有效邀请码不被无效请求永久阻塞增加测试。

### 验收

- 单个来源超过阈值后收到 429 或 Cloudflare 429/403。
- 其他来源持有效邀请码仍可正常加入。
- 限流窗口结束后自动恢复，不需要重启服务。
- 压力测试期间线程数、内存和 registry 锁等待保持有界，tick 不被饿死。

## 三、服务器失陷后的秘密边界

这不是“只知道域名”的匿名漏洞，但需要作为运维边界持续检查：

- 用户 API key 默认只在服务进程 RAM 中；保持 `persistKeys` 关闭。
- `tenants.json` 会明文保存邀请码和租户 token，以支持操作员找回；任何能读取数据目录或备份的
  人都能消费未使用邀请码或冒充租户。
- 0600 只约束普通用户，不能抵御 root、容器逃逸、错误公开的卷或未加密备份。

### 待办

- [ ] 生产环境确认 `persistKeys` 为 off，并核对不存在租户 `keys.json`。
- [ ] 数据目录、快照和备份使用最小权限；不得进入公开对象存储、镜像层或诊断包。
- [ ] 建立 token 泄漏处置步骤：撤销对应邀请码、发新码、用户重新配对并推送配置。
- [ ] 定期核对运行中的版本、监听地址与 Tunnel 配置；服务保持绑定 `127.0.0.1:8790`。

## 四、已验证的正向安全属性

- 无 token 请求 `/v1/state` 返回 401。
- hub 模式下 `/v1/claim` 被拒绝，不存在抢占单租户服务器的 claim 流程。
- 无效邀请码不会产生租户；邀请码单次使用、过期和撤销链路存在测试。
- 未处理异常对外统一为通用 500，不回传 Python traceback。
- 部署笔记所指服务器的公网 `8790` 定点连接超时；当前测试出口未发现绕过 Cloudflare 的入口。
- `awewarm-hub` 测试：147 项通过。

## 非目标

- 不把邀请码当密码认证之外的第二套账号系统。
- 不用 CAPTCHA 破坏 CLI 配对流程。
- 不宣称 Cloudflare 能防住所有应用层拒绝服务；边缘与应用层各自保留一道限流。
- 本文不覆盖取得有效 token 后的恶意租户行为，那需要单独的授权与滥用测试。
