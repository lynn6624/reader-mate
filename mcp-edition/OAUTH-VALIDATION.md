# MCP OAuth 阶段 1 验证记录

日期：2026-11-02（北京时间）。基线 `f38ce92`，分支 `codex/mcp-oauth`，PR #11。全部使用本机临时数据；未读取、修改或连接生产身份/书库。

## 本机已执行

Windows、Python 3.14.2，最终 OAuth/共读/成员/建议测试使用 MCP 1.30.0、Authlib 1.9.2、Flask 3.1.3。初轮曾使用本机原有 MCP 1.26.0；发现版本差异后已按仓库精确版本重跑相关测试，以下结果以最终重跑为准。

| 检查 | 结果与证据范围 |
| --- | --- |
| `python core/selftest-oauth.py` | 13 组协议/CLI/ASGI/并发/重定向测试全部通过；另有实际子进程 HTTP + 官方 MCP SDK 流程通过 |
| 发现与协议 | 匿名 401、资源/issuer 绑定、S256、DCR、回调/state/iss、错误 client/resource/scope、重复参数均覆盖 |
| 邀请与浏览器事务 | 邀请过期/无效/已用、CSRF、跨 Cookie、取消、两个浏览器并发抢同一邀请码、同事务双击均覆盖；未做真实浏览器验收 |
| Token 生命周期 | 授权码交换/过期/重放/并发，刷新轮换/过期检查/扩大 scope 拒绝/并发/重放撤销家族，独立授权与服务重载均覆盖 |
| 实际 HTTP/MCP | 完整 DCR → 邀请验证 → 身份确认 → 授权码交换 → 15 工具发现；使用临时读者 `004` 和 `op004` 验证精确区分 |
| 阅读连续性 | 旧 URL 身份先写进度；OAuth 读回、续写、创建划线和回复；旧 URL 再读到新进度；自报 who/viewer_id 不改身份 |
| 撤销与兼容 | 撤销后拒绝；其他授权和旧 001 Header/004 URL 仍可用；新 Token 不能经旧路径或 Web 登录；新主机拒绝旧路径；原身份停用后拒绝 |
| 保密与失败 | 数据库/日志不含测试邀请码、Access/Refresh、原 Key；页面不回显邀请；缺失/损坏库失败关闭；错误 Host/Origin 和不安全外部传输拒绝 |
| 内部转发 | 实际回环 302 服务 + 捕获服务：GET/POST 均不跟随跳转，捕获服务没有收到原身份凭据；环境 HTTP_PROXY 不接管 OAuth 内部请求 |
| `python core/selftest-cloud.py` | 共读、UTF-16、进度 CAS、所有权、旧路径及模拟 TTS 回归通过 |
| `python core/selftest-members.py --no-browser` | 身份存储/多进程并发、通道隔离、旧 004、撤销/重启及无历史改写通过；浏览器明确跳过 |
| `python core/selftest-proposals.py --no-browser` | 建议定位/审批、失败恢复、HTTP/MCP 身份及权限、共享草稿、CAS 和重启回归通过；浏览器明确跳过 |
| `python core/selftest-publication-integration.py` | B5 多进程发布/回滚、进度及批注保全、旧版本拒绝、清理与备份 CLI 回归通过（该测试仅标准库 Web/HTTP，不使用 MCP/Authlib/Flask） |
| 功能关闭且未安装 OAuth 可选包 | 去掉测试 PYTHONPATH 后，原共读/MCP 回归通过；未强制旧接入安装 Authlib/Flask |
| 语法 | 6 个相关 Python 文件按 Python 3.11 语法解析通过；不等同于 Python 3.11 实际运行 |

原始本机测试输出保存在工作区 `.test-temp/oauth-*-results.log`，B5 证据位于 `.test-temp/oauth-stage1-publication-checks/`；不提交日志、随机测试状态、身份或数据库。可通过源码自测重新生成脱敏结果。

## CI 与后续验收

新增 `.github/workflows/reader-mate-oauth.yml`，在 Ubuntu/Python 3.11 安装精确直接依赖并运行上述五组自测，不使用部署凭据、生产 SSH 或其他 secrets。

实现提交 `34dc97d0b1b6677ff279192f43d2cc60cae0a3d0` 的 [Linux/Python 3.11 CI 运行 41448065951](https://github.com/lynn6624/reader-mate/actions/runs/41448065951) 已完成且成功，核对了依赖安装及五个测试步骤均为 success。该结果验证实际 Linux/Python 3.11 运行，不验证生产配额或浏览器；后续提交状态以 PR 检查为准。

| 尚待阶段 2 / 3 验证 | 当前状态 |
| --- | --- |
| 正式有效证书、独立测试域名、外部 HTTPS 与代理路由 | 未执行 |
| 真实浏览器 Secure Cookie、确认页、回调布局及行为 | 未执行；当前 HTTP 自测模拟 TLS 终止代理并手动传测试 Cookie |
| 实际 ChatGPT 添加无秘密 URL、自动发现和登录 | 未执行 |
| 实际客户端刷新/断线恢复、撤销已连接授权 | 未执行；本地协议/MCP 行为已覆盖 |
| Linux 256 MiB slice 下峰值、节流与 OOM | 未执行；不以数据库上限或代码大小代替内存测试 |
| 完整测试环境代理/服务回滚演练 | 未执行；功能关闭和旧接入已本机回归 |
| 生产 004 的精确身份映射及真实进度/建议连续性 | 未执行；临时 004/op004 不代表已读取生产映射 |
| 生产部署、旧凭据迁移/撤销 | 未执行，仍需用户明确确认 |

本轮交付为阶段 1 可审查实现和本地验证，不是正式接入完成。配置、操作、容量边界及回滚见 [OAUTH.md](OAUTH.md)，外部客户端步骤见 [OAUTH-PLAN.md](OAUTH-PLAN.md)。
