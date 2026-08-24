# 配置文件格式：保持 JSON，不做 TOML 迁移

> 2026-08-24 讨论：有人建议把配置从 JSON 换成 TOML（理由：可写注释）。
> 结论：**不换**。本项目所有 JSON 文件都是程序自己写的（CLI 修改、temp+rename 原子整体重写），
> 用户手写的注释在下一次程序写入时照样丢失，TOML 的主要卖点兑现不了。

## 为什么不换

- hub registry（`~/.awewarm-server/`）是状态库不是配置：租户配对、机器列表、用量都在里面，
  引擎随时整体重写（awewarm-hub `engine.py`），人不手编，注释无处安放。
- 客户端 `~/.awewarm/config.json`：CLI 优先设计，所有改动走命令（`awewarm config` 等），
  `_write_json`（awewarm `config.py`）原子重写整个文件，手编注释必然被抹掉。
- `requires-python >= 3.9`，标准库 `tomllib` 要 3.11 且只读不写——写 TOML 必须引入第三方依赖
  （tomlkit/toml），还要为换格式平添注释保留的往返写逻辑。
- 存量用户机器上已有 `config.json` / registry，切换格式需要迁移代码，长期背着维护。

## 待办

- [ ] awewarm：文档与报错提示写明"配置文件由命令管理，请勿手编；手工调整参考相关文档"
      —— 现状是手写 `#` 注释会让 `json.loads` 直接 die，先把这个坑用一句话堵上
- [ ] awewarm-hub：README 补一句 registry 由 `awewarm-hub` 命令管理，勿手编

## 重新评估的触发条件

出现真实的"用户手编配置 + 留注释"需求时再考虑 TOML，且届时一并接受：
tomlkit 往返写（保留注释）+ 一次性静默迁移存量文件。
只为"能写注释"单独换格式，是把 cosmetic 好处当架构问题，不值得。
