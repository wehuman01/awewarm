# awewarm-hub 拆分计划

> 目标：把 hub（多租户服务器）从 `awewarm` 中剥离为独立包 `awewarm-hub`，两者均发布到公开 PyPI。
> 本计划产生自 2026-08-22 的架构讨论；同日更新：hub 由"闭源专有许可"改为**与 awewarm 同样开源（MPL-2.0），公开仓发布在 wehuman01 组织下**，两个包在文档与元数据层面互相引用（pip 依赖方向不变：hub → awewarm，避免循环依赖）。

## 一、既定决策

| # | 决策 | 理由 |
|---|---|---|
| 1 | 两个独立包、独立命令 | hub 操作员与 hub 用户是不同角色，物理隔离最清晰 |
| 2 | 命令名 `awewarm-hub`，`awewarm-hub serve` 隐含 hub 模式（取代 `awewarm serve --hub`） | 操作员的世界完整地在一个命令里 |
| 3 | `awewarm-hub` **依赖**开源 `awewarm`（pip 库依赖，非复制代码） | 引擎（WarmServer + schedule + transport ≈1200-1500 行）定义线协议语义，依赖保证协议永远与开源客户端对齐，避免复制副本静默漂移 |
| 4 | ~~闭源 = 法律闭源（专有许可证）~~ → **开源 MPL-2.0，与 awewarm 完全一致** | 2026-08-22 变更：与 awewarm 一样开源，同样的许可证，同等的再分发/修改/商用自由 |
| 5 | ~~开发仓私有（GitHub private）~~ → **公开仓 `wehuman01/awewarm-hub`**，发版 wheel 发到公开 PyPI（Trusted Publishing） | 2026-08-22 变更：源码公开可浏览，与 awewarm 并列在同一组织下 |
| 6 | 开源仓的 solo `awewarm serve` 保留不动 | 单人委托场景服务器两边是同一人，无角色要隔离 |
| 7 | `_scheduler_install` 加 confirm 门（与本拆分互补的脚枪修复） | `pip install awewarm-hub` 会连带装上开源 `awewarm` CLI，服务器盒子上 `scheduler install` 误触问题仍需此门 |

## 二、架构

```
┌─ awewarm（开源，MPL-2.0，公开 PyPI）────────────┐
│ 客户端 CLI：init/discover/status/run/config/    │
│            scheduler/remote/self-update         │
│ solo 服务器：awewarm serve（单租户 WarmServer） │
│ 引擎：WarmServer + schedule.py + transport.py   │
│       + config.py 状态部分 + locking.py         │
│ HTTP 基础设施：_Handler（重构为可继承）          │
└───────────────────┬─────────────────────────────┘
                    │ pip 依赖 awewarm>=0.6,<0.7
┌───────────────────┴─────────────────────────────┐
│ awewarm-hub（开源，MPL-2.0，公开仓 wehuman01 + 公开 PyPI）│
│ Hub + Tenant：邀请台账/配额/revoke/restore/认证 │
│ 管理命令：invite / list users / list invites /  │
│           revoke / restore / config             │
│ awewarm-hub serve（hub 模式 HTTP + tick）       │
│ self-update（复用 get_pypi_latest，查自身包名） │
└────────────────────────────────────────────────┘
```

线协议不变：客户端 `remote connect --invite awi_...` 完全不受影响，hub 用户无感知。

## 三、Phase 0 — 提交在手改动（开源仓）

dev 分支上有一批未提交的 hub 改动（server.py、cli.py、test_hub.py、README、README_cn、CHANGELOG）。
**先原样提交**，保证拆分 diff 干净、历史可追溯。

- 验证：`git status` 干净；`python -m pytest` 全绿。

## 四、Phase 1 — 开源仓重构（product/tools/awewarm，dev 分支）

### 1a. `_Handler` 可继承重构（先行，独立提交）

- 把 server.py 的 `_Handler` 拆出可复用内核：单租户路由（healthz/claim/connections/keys/state/run/release）
  与"引擎对象注入"解耦，hub 分支（/v1/join、租户认证、配额、release 语义）留出覆盖点。
- `run()` 拆出 solo 版；hub 版启动逻辑后续搬走。
- 开源仓内单租户行为零变化，现有测试保持绿。
- 在 server.py 模块 docstring 标注：`WarmServer`/`_Handler` 是 awewarm-hub 依赖的半公开扩展面，改动需考虑兼容。

### 1b. 移除 hub 代码 + 墓碑

- server.py：删除 `Tenant`、`Hub`（约 336-803 行，~450 行）及 hub 模式启动分支。
- cli.py：删除 `hub` 命令组（invite/list/revoke/restore/config）及 `_resolve_server_data_dir` 中仅服务 hub 的部分；
  `serve` 移除 `--hub/--max-tenants/--max-conns-per-tenant` 选项。
- 墓碑（复用 cli.py:517 `_moved` 机制）：
  - `awewarm serve --hub` → 报错 "moved to: awewarm-hub serve"
  - `awewarm hub <any>` → 报错 "moved to: awewarm-hub <any>"
- tests/test_hub.py（770 行）及 test_cli.py 中 hub 相关用例迁出（进 awewarm-hub 仓）。

### 1c. `_scheduler_install` confirm 门

- 安装前检查本地配置：`enabled 且 location != "remote"` 的连接数为 0 时，提示
  "无可调度的本地连接（全部已委托/尚无连接），服务器端 serve 会自行 tick"并确认；
  确认必须发生在 `--wake` 的 sudo 提示之前。
- 非交互场景（脚本）按项目惯例处理（直接装并打印提示，或要求 flag——实现时与现有 confirm 风格对齐）。

### 1d. 文档与版本

- README / README_cn：hub 章节替换为一段指向 `awewarm-hub` 的说明（开源 MPL-2.0，PyPI 可装，链接 wehuman01 仓）。
- docs/CHANGELOG.md 记录 breaking change；版本 bump 到 **0.6.0**。
- 按既定 dev→main 流程发布开源仓。

- Phase 1 验证：`pytest` 全绿；`awewarm serve --hub` 与 `aweharm hub invite` 输出墓碑指引；
  solo `serve` + `remote connect`（单租户）端到端可用。

## 五、Phase 2 — 开源仓 awewarm-hub（product/awewarm/awewarm-hub，独立 git 仓）

> 目录已落位为 `product/awewarm/awewarm-hub`（与开源仓 `product/awewarm/awewarm` 并列）。

### 2a. 仓库骨架

- `pyproject.toml`：
  - `name = "awewarm-hub"`，初始版本 `0.1.0`
  - `license = "MPL-2.0"` + 与 awewarm 相同的 MPL-2.0 LICENSE 文本
  - `dependencies = ["awewarm>=0.6,<0.7"]`（引擎稳定后再放宽）
  - 入口 `awewarm-hub = "awewarm_hub.cli:main"`
- 目录：`src/awewarm_hub/`（`cli.py`、`engine.py`、`handler.py`）、`tests/`。

### 2b. 代码搬入

- `engine.py`：`Hub`、`Tenant` 原样迁入（从 Phase 1 之前的 server.py 复制），改 import 为
  `from awewarm.server import WarmServer, ApiError` 等开源侧保留的扩展面。
- `handler.py`：继承开源 `_Handler` 内核，实现 `/v1/join`、租户 Bearer 认证、配额检查、hub 语义的 release。
- `cli.py`：`serve`（隐含 hub，含 --data-dir/--bind/--port/--max-tenants/--max-conns-per-tenant/--tick-seconds）、
  `invite`、`list users`、`list invites`、`revoke`、`restore`、`config`、`self-update`。
  命令行为与原 `awewarm hub ...` 一致，仅前缀变化。
- tests：test_hub.py 770 行迁入，调用路径改为新入口。

### 2c. 数据目录兼容

- 沿用 `~/.awewarm-server`（或 `--data-dir`），tenants.json 格式初始不变——
  现有 hub 服务器换装 `awewarm-hub` 后原目录直接可用，操作员零迁移。

- Phase 2 验证：pytest 全绿；`awewarm-hub serve` + 客户端 `remote connect --invite` 端到端 pairing、
  tick、revoke/restore 全流程通过；用现有生产数据目录（备份后）冒烟。

## 六、Phase 3 — 发布流水线

- GitHub 公开仓 `wehuman01/awewarm-hub`；`.github/workflows/publish.yml`：打 tag → build →
  Trusted Publishing（OIDC）上传 PyPI。纯 Python wheel，无平台矩阵。
- PyPI 注册/占位 `awewarm-hub` 包名；确认与开源包的依赖解析正常（pip install awewarm-hub 自动带上 awewarm）。
- `self-update` 复用 `get_pypi_latest`，包名参数化为自身。
- 服务器（awewarm.wehuman.top）切换：装 `awewarm-hub`，serve 进程换新入口，systemd/守护方式不变。

## 七、风险与备注

1. **MPL-2.0 一致性**（2026-08-22 变更后）：两个包同为 MPL-2.0（文件级弱 copyleft），无闭源下游问题；
   hub 通过 pip 依赖引用 awewarm，不复制 MPL 文件内容。若日后接受外部贡献，MPL 文件级义务照常适用。
3. **引擎 API 稳定性**：开源仓 0.x 期间 `awewarm.server` 内部结构可能变；
   `awewarm-hub` 用紧版本区间（>=0.6,<0.7）锁住，开源仓每次 engine 相关改动需同步评估 hub 兼容性
   （可在开源仓 CHANGELOG 中标注 "engine surface changed"）。
4. **协议握手**：`/healthz` 已返回 version；客户端对旧 hub 服务器的兼容提示依赖它，拆分时保持该行为不变。
5. **技能文档**：`~/.agents/skills/awewarm/SKILL.md` 中 hub 命令表需同步更新/拆分（仓库外事项，单独跟进）。

## 八、待定项

- [ ] PyPI `awewarm-hub` 名字可用性确认（发布前查一次）

## 九、执行顺序总览

```
Phase 0  提交在手改动（开源仓 dev）
Phase 1a _Handler 可继承重构（独立提交，测试保持绿）
Phase 1b 移除 hub + 墓碑 + 测试迁出
Phase 1c scheduler install confirm 门
Phase 1d 文档 + CHANGELOG + 0.6.0 发布（dev→main）
Phase 2a 仓库骨架（product/awewarm/awewarm-hub，git init）
Phase 2b Hub/Tenant + 管理命令 + 测试迁入
Phase 2c 数据目录兼容冒烟
Phase 3  公开 GitHub 仓（wehuman01）+ CI 发版 + PyPI 占名 + 生产服务器切换
```
