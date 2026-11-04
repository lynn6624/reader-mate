# Token 页 OAuth：新版隔离验收

日期：2026-11-04。固定源码 c7de420230efe6aaeb640310e3ad1e3f892ac91d，逐 Git 对象验证 82 个文件。当前生产未部署；独立域名旧方案已失效。报告位于 checks/oauth-management-20261104/。

## 同一入口与业务流程

在独立合成环境中，真实 Caddy 仅监听回环 HTTPS：OAuth 发现、授权、交换和 MCP 路由到 MCP 进程，原页面及 /api/oauth/* 路由到 Token 管理端。HTTP 客户端验证临时 CA，使用固定测试 Host，未关闭证书验证、未安装系统 CA。该测试不是公网 CA 或生产新路由验收。

三身份 004、op004、op001 都经管理 API 获取邀请码，走 S256 和明确刷新同意，获得仅 reader:mcp 的凭证；官方 SDK 各执行两轮工具发现、书单、大书窗口读取及进度保存。书籍为 2,000,009 UTF-16 单位的合成大书。同期管理员连续查询邀请及授权，单页不超过 50 条，第二页可查询。随后刷新，再从管理端撤销每条本轮合成授权，访问和刷新均拒绝。原身份文件字节不变。

## 共享预算

| 测试 | 解释器 Web / MCP / Token | 耗时 | 50ms 采样最大 memory.current | memory.high 增量 | max / oom / oom_kill |
| --- | --- | --- | --- | --- | --- |
| 首轮 | 3.12 / 3.12 / 3.12 | 7.935s | 204,800,000 字节（195.31MiB） | 240 | 0 / 0 / 0 |
| 生产同款组合复测 | 3.12 / 3.12 / 3.10 | 8.339s | 204,587,008 字节（195.11MiB） | 154 | 0 / 0 / 0 |

两轮三个业务进程合计 MemoryMax=256MiB、MemoryHigh=192MiB，与生产业务分组范围一致。Caddy 代理有独立 96/64MiB 限额；测试控制器在预算外。此结果不代表整台 VPS 内存或不限并发的容量保证。

旧内核无 memory.peak，报告该项为 null。采样可能漏掉瞬时峰值；memory.high 非零表示实际发生软限额压力，不能声称无节流。复测重新启动三业务进程，保留首轮合成数据、邀请/授权和撤销记录，未清库，也不把两轮差异单独归因于 Python 版本。

## Tunnel、保密与收尾

使用实际 cloudflared CLI validate/rule 验证脱敏片段。另将新规则加入当前生产配置的私有候选副本，验证 8 个 OAuth 路径指向 MCP、7 项原有路由结果不变；新规则显式指定 Token 域名的 Host。未覆盖生产配置，未启动新公网隧道，未改 DNS。

四份测试服务日志均扫描原合成身份 Key、路径 Key、管理口令、OAuth Token/邀请码格式和私钥，未发现完整凭证。两轮结束后关闭全部新测试服务与代理，保留私有审计。生产 MCP/Web/Token 源码、身份文件、Caddy/Tunnel 配置摘要，以及五个生产服务 MainPID 均不变。

## 复现与上线边界

控制器 core/selftest-oauth-portal-resources.py 在预算外运行，要求由 oauth_testbed 创建的合成 fixture、私有测试管理员凭据、已就绪的共享 HTTPS 分流和三进程 cgroup。参数为 --fixture、--cgroup、--endpoint、--ca、--credentials、--report；--rounds 默认 2。私有凭据文件 marker 必须为 reader-mate-oauth-portal-test-v1，含仅用于本 fixture 的 password。禁止传入生产口令或生产书库。

新版管理接口及桌面/手机 Chrome 的 Linux CI 已单独验证，详见 PR。生产发布需按实际 Token 域名路径分流，重启 MCP、Token 与 reader 专用 Tunnel，并复验公网证书、原 Header/URL、原页面、生产 004 进度与建议连续性；这些不是此次合成测试已经完成的项目。生产变更及任何现有效凭证撤销仍须明确确认。
