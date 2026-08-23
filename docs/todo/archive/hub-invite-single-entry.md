# awewarm-hub 邀请码单一入口收敛

> 目标：hub 的撤销/恢复收敛到邀请码单一入口，授权状态单一事实源，设备上限分层管理。
> 本计划产生自 2026-08-23 的设计讨论，决策已确认。改动落在 awewarm-hub 仓（`product/awewarm/awewarm-hub`）。

## 一、既定决策

| # | 决策 | 理由 |
|---|---|---|
| 1 | 撤销/恢复只保留邀请码一个入口：`revoke <awi_>` / `restore <awi_>`，删除 `revoke t_...` 寻址 | 码用掉后 `usedBy → 租户` 的关联是永久的，码是这份授权终身的名字；撤 pending 码与撤已用码本是同一语义（"这份授权死了"）。现有双入口是同一个状态转移的两份地址、两条代码路径各自双写标志位 |
| 2 | 授权状态单一事实源：只存在邀请码上（`revokedAt`），租户记录删除 `suspendedAt` 镜像 | 挂起从授权推导（token → 租户 → 授权），不存镜像就没有同步逻辑；现在"t_ 路径清机器、awi_ 路径不清"的漂移就是双写的症状 |
| 3 | 撤销不动机器配对：revoke/restore 是纯授权动作，完全无损可逆 | 换机器/重装的正规出路是操作员发新码（新租户、新 token、新机器配额），系统里始终只有一个门；旧设计 revoke 暗藏破坏性副作用（清设备绑定），restore 后才暴露 |
| 4 | `max_machines` 分层：全局默认 + per-invite 覆盖 | 机器上限本质是授权的属性，长在授权上；全局值只是铸造时的默认值 |
| 5 | 不加"码上的值不得超过全局值"的天花板规则 | 操作员是唯一信任角色，天花板防不了任何对手，只防自己手滑——没有对手的安检门 |
| 6 | 旋钮归属准则：因人而异的管到码上，hub 容量留全局 | 机器数因人而异（有人双机）→ per-invite；`max_tenants`、`max_conns_per_tenant` 是容量表达 → 全局 |

## 二、目标模型

- 邀请码是唯一入口、唯一授权事实源：pending → used（关联租户）→ revoked / expired，一张台账讲完所有授权历史。
- `revoke awi_...`：pending 就杀码；已用则关联租户失去访问权（token 401、tick 跳过、释放容量槽）。
- `restore awi_...`：反向，过容量检查。
- 认证链：Bearer token → 租户 → 它的授权；授权 revoked → 401（提示 restore）；机器上限读码上 `machines` 值执行。
- 租户记录只留身份/工作区/用量/机器列表，不存任何授权状态副本；调整在线用户的机器上限 = 改那行授权记录，一个地方。

CLI 面收敛为：`invite [--machines N]` / `list invites` / `list users` / `revoke <awi_>` / `restore <awi_>`。

非目标（出现真实需求再拆）：暂停 ≠ 撤销的区分、给老租户补发新码、一码多用、per-invite 容量旋钮。

## 三、任务

### 3a. engine 收敛

- `revoke` / `restore` 只按码寻址，合并为单条代码路径；`revoke t_...` 改为报错并指引
  "用邀请码操作：awewarm-hub list invites --reveal"。
- 租户记录删除 `suspendedAt`；`auth` / `tick` / `_require_capacity` / `summarize` 的挂起判断
  改为从授权推导（`_invite_of` 的 usedBy 扫描已存在，这个规模下开销为零）。
- 撤销路径不再 `pop machines`；机器列表只在 auth 首次绑定时增长。
- `mint_invite` 增加 `machines` 参数：铸造时盖进授权记录，缺省取全局默认
  （读 serve 启动时戳进台账的 `serve.maxMachines`）；`list invites` 展示每码的 machines 值。
- 认证的机器上限检查读授权记录上的 `machines`；403 文案的出路从 "revoke + restore"
  改为"找操作员要新邀请码"。
- `list users` 展示推导的挂起状态与来源码（已有 `invite` 列，保持）。

### 3b. 数据迁移

- 检查生产台账是否存在无明文 `code` 的老邀请行（"codes were kept on disk" 之前铸造的）——
  无法用码寻址，有则删除让用户重新配对。
- 存量 `suspendedAt` 迁移：读老字段，映射为对应授权的 `revokedAt`，然后删字段；
  一次性启动迁移或 load 时惰性迁移，实现时与现有 registry 版本机制对齐。
- serve 与 CLI 跨进程兼容：serve 在认证/事务前 refresh 的机制保持不变，
  撤销/恢复立刻生效的语义不变。

### 3c. 测试与文档

- test_hub 用例改写：双入口用例合并为单入口；撤销/恢复前后机器配对不变；
  per-invite machines 覆盖与全局默认各自生效；无 code 老行的迁移。
- README / README_cn hub 段落、CHANGELOG 更新（单入口是 breaking change，版本 bump）。

### 验证

`pytest` 全绿；端到端：`revoke awi_` 后 token 401、tick 跳过、容量释放；
`restore` 后同一台设备无损续上；旧租户挂着时同机器用新码可加入并正常工作；
`invite --machines 3` 允许第三台设备、默认码第二台被拒。

## 四、待定项

- [x] 生产台账老邀请行检查（3b 前置）→ 落定：不需要人工前置检查。v1→v2 迁移自动删除无明文
  `code` 的邀请行**及其产出的租户**（其 token 随之失效，否则将成为永远无法撤销的租户）；
  工作区保留在磁盘上，受影响用户发新码重新配对。升级后用 `awewarm-hub list users` 核对即可。
- [x] 客户端机器超限提示 → 落定：`remote.py` 的 `RemoteError` 逐字透传服务端文案
  （机器超限 403、撤销后 401 均直读），无需客户端改动。

## 五、落地记录（2026-08-23）

实现落在 awewarm-hub v0.6.0（breaking，CLI 单入口 + registry v2 一次性迁移）。
评审补充的三处细节均已吸收：

1. 无码老行删除时连租户一起删（token 失效、容量释放）；迁移在 `Hub.__init__` 内、
   跨进程事务锁下执行一次，幂等。
2. 挂起推导的改动面覆盖 `auth`/`tick`/`_require_capacity`/`summarize`/`list_invites`
   以及全量操作员文案（401/403、status、serve help）。
3. "改那行授权记录"的落定：调整在线用户机器上限 = 手改 `tenants.json` 中其邀请码行的
   `machines` 值（运行中的 serve 经 refresh 采纳磁盘改动），或发新码；README/SKILL 已写明。
   重装换机的正规出路 = 发新码（撤销不再清机器配对，撤销/恢复完全无损可逆）。
