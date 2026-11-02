# MCP OAuth 阶段 2：隔离验证记录

日期：2026-11-02 至 11-03（北京时间）。分支 codex/mcp-oauth，PR #11，基线 f38ce92。此轮新增测试副本及临时服务；未部署到生产，未修改生产身份、阅读数据、代理配置或现有凭证。实际 ChatGPT 验收仍在进行，不能据此开始阶段 3 迁移。

## 范围与证据

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 本机协议及实际 HTTP/MCP | 发现、S256、邀请、CSRF、授权码、资源/scope、15 工具、隔离、刷新/重放/撤销通过；新增 Origin 和跨域回调 CSP 回归 | core/selftest-oauth.py |
| 无 offline_access 的刷新同意 | 16 组协议回归及真实 Chrome 通过；仅 reader:mcp、页面同意、自然过期和轮换重连，旧授权保持原资格 | checks/oauth-stage2-20261102/browser-refresh-consent.json |
| 桌面与手机真实 Chrome | 1280/390 像素视口，Secure/HttpOnly/Lax Cookie、确认页、state/iss、工具、进度、批注及撤销通过；追加跨域回调验证 | core/selftest-oauth-browser.py |
| 公网 HTTPS | 临时独立 HTTPS 主机、公开 CA 证书；Chrome 和 httpx 均验证证书，无忽略错误选项；浏览器授权、MCP、刷新重连与撤销通过 | checks/oauth-stage2-20261102/browser-public-https.json |
| 自然过期 | 本机临时 CA 的 30 秒 Access Token，自然等待后 401，再刷新并重连成功 | checks/oauth-stage2-20261102/browser-local.json |
| Linux 资源预算 | 三业务进程合计硬限额 256 MiB、软限额 192 MiB，三读者并发、两轮大书读写，通过，无 OOM/硬限额事件 | checks/oauth-stage2-20261102/resource-result.json |
| 服务回滚 | 仅关闭并恢复测试 OAuth；旧 op001 Header、004 URL 均可发现 15 工具并读取；撤销状态跨重启保留，测试业务数据不变 | checks/oauth-stage2-20261102/rollback-result.json |
| 生产与日志边界 | 已选生产源码/身份/代理配置摘要和三个生产 MainPID 不变，生产及旧测试服务存活；扫描六份新测试日志未发现完整测试凭证 | checks/oauth-stage2-20261102/audit-summary.json |
| 实际 ChatGPT | 修复后已完成授权码交换，绑定合成 004；用户反馈每个工具均已调用且正常。自动刷新、撤销后的实际客户端行为继续验收 | checks/oauth-stage2-20261102/chatgpt-tools.json |
| 生产映射和迁移 | 未执行；合成 004/op004 的测试不能证明生产映射 | 阶段 3 需单独确认 |

JSON 与截图只包含合成数据，不提交活跃测试域名、邀请码、私有导出、数据库或原始日志。截图使用本机临时 CA 场景，不能当作公网证书证据。公网证书流程使用的是临时服务域名；正式域名、长期证书/路由和可用性仍需生产部署评审。

## 本轮修复

1. Chrome 在 no-referrer 策略下提交表单时可能发送 Origin: null。只对 POST /oauth/authorize 允许该值，仍独立验证 Cookie、事务、有效期与 CSRF；其他来源和端点继续拒绝。
2. 长 client_id 使手机页面溢出。加入静态响应式样式，以 CSP SHA-256 精确允许，不启用 unsafe-inline；真实浏览器断言页面宽度不超过视口。
3. 原浏览器用同域测试回调，漏掉真实客户端跨域跳转。Chrome 会把表单后的 302 也纳入 form-action 校验。授权页现在只额外允许本事务已经与注册记录精确匹配的回调 origin；拒绝未注册回调及可注入 CSP 的主机字符，并增加双域名浏览器回归。[浏览器差异说明](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/form-action)。
4. OAuth 阅读和进度保存复用既有 Web 正文窗口接口，使用 text_base/total_units 保持绝对 UTF-16 位置、正文末尾与代理对边界。旧 Header/URL 的请求路径保持原行为。协议自测增加大书非零窗口、EOF、代理对及进度回归。
5. OAuth 内部 Web 请求按 MCP 进程串行进入，客户端取消时仍等工作线程实际完成才释放配额，降低并发冷索引占用；仅 OAuth 内部超时调整为 60 秒。回环限定、禁止重定向和环境代理的防泄漏检查保留。

首次 ChatGPT 授权因跨域回调被阻止，授权码未兑换。CSP 修复后，2026-11-03 00:37（北京时间）新授权已兑换 Access Token，服务器记录稳定 ID 为 004；00:40 用户反馈每个工具都已调用且正常。此项工具调用证据来自用户操作反馈，服务器未启用逐工具请求追踪以免引入凭证日志；分别记录两类证据，不能把注册回调本身当作工具通过。

本次 ChatGPT 注册声明支持 refresh_token，但授权只请求 reader:mcp。原策略要求显式 offline_access，因而该次授权没有刷新 Token。现调整为：授权页对声明支持刷新的客户端明确展示离线刷新及最长有效期，确认后持久保存 refresh_consented，再由 Authlib 签发和轮换刷新 Token。业务 scope 保持原值，不自动添加 offline_access；现有无刷新同意的授权不升级，旧 offline_access 授权保留原同意。客户端须重新授权以取得新策略下的刷新凭证。

此策略依据 [MCP 2026-07-28 Refresh Tokens](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#refresh-tokens)：客户端可选添加 offline_access，是否签发刷新 Token 由 AS 决定。仍检查注册 grant_type、同意、client/resource/scope、最长寿命和原身份状态，轮换与重放撤销规则不变。新增自动化覆盖无 offline_access 的确认、自然过期、刷新、scope 不扩大、重放、仅授权码客户端及旧授权兼容；真实 ChatGPT 自动刷新须重新授权后单独验收。

## 资源结果的解释

测试环境通过现有 Workbench CLI 访问，使用新的私有目录、虚拟环境、SQLite、合成书库与短期 systemd 服务。测试服务限制写入新目录，并禁止访问生产和旧测试数据目录。未修改生产 Caddy、现有隧道、DNS 或正式服务。

预算内仅有 Web、MCP（含 OAuth AS）、Token 管理端三个进程，与原生产业务进程分组一致。控制器、测试启动器、回环代理和独立临时隧道不在该预算内；代理/隧道分别有独立限额。结果不是整台 VPS 的峰值保证。

- 书籍：2,000,009 UTF-16 单位，250,000 行重复短句；合成身份 004、op004、op001 同时访问，各两轮。
- 耗时 6.844 秒；memory.current 每 50 毫秒采样，最大 203,616,256 字节，约 194.18 MiB。
- memory.high 增加 23；memory.max、oom、oom_kill 均增加 0。软限额发生过节流，不能声称没有内存压力。
- 旧内核无 memory.peak，报告该项为 null；采样可能漏掉瞬时峰值，不用采样值冒充内核精确峰值。
- 早期额外包含启动器/代理的更严分组出现超时；另一次输入偏移 20 切在 UTF-16 代理对中，修正为有效偏移 25。失败摘要保留在 resource-attempts.json，最终结果使用明确的三进程范围。

## 复现自动化

在开发用虚拟环境、mcp-edition 目录执行：

~~~sh
python -m pip install -r core/requirements-oauth-e2e.txt
python -m playwright install --with-deps chromium
python core/selftest-oauth.py
python core/selftest-oauth-browser.py --artifacts /tmp/reader-oauth-browser
~~~

浏览器自测自己创建合成环境和一天有效的临时 CA。httpx 显式信任该 CA，Chromium 只固定该次公钥，不关闭全局证书验证。可通过 CHROME_TEST 指向已有 Chrome。CI 在 Ubuntu/Python 3.11 跑协议及原业务回归，再运行独立 Chromium 任务并上传合成截图和 JSON；这项自动化不连接生产或 ChatGPT。

公网模式由测试管理员在新副本生成短期邀请，使用私有 JSON 文件交给测试客户端：

~~~sh
python core/selftest-oauth-browser.py --public-fixture /private/oauth-client.json --artifacts /private/public-browser
~~~

私有文件格式见脚本：marker 为 reader-mate-oauth-public-client-v1，含 issuer/resource/access_ttl 及 browser_invites（004、op004）。只允许临时 trycloudflare.com 测试主机。它不含底层长期读者 Key。公网模式产出的 client_secret_digests 仅供管理员扫描远端日志，归档前删除；公开截图也不能包含活跃测试主机。

Linux cgroup 测试要求预先启动合成服务。创建入口：

~~~sh
python core/oauth_testbed.py create --root /isolated/new-fixture --issuer https://oauth.example.test --large --access-ttl 900
~~~

该命令拒绝现有目录，随机生成合成身份及钥匙，输出回环端口。在独立机器上将 Web、MCP、Token 管理端按部署示例的环境启动到新的共享 cgroup；从 fixture.json 读取随机端口、路径及 issuer，Web 沿用 MALLOC_ARENA_MAX=1 和 MALLOC_TRIM_THRESHOLD_=131072。禁止连接生产 Web API。新 cgroup 设置 memory.max=268435456、memory.high=201326592，三个真实 PID 写入私有 fixture/pids.json：

~~~json
{"launcher": null, "children": [101, 102, 103], "budget_scope": "synthetic Web + MCP OAuth + token portal only; controller/proxy/tunnel excluded"}
~~~

PID 必须换成实际值；控制器会校验归属和限额，不能虚填。若直接使用 oauth_testbed.py serve，启动器也在同一 cgroup 时应如实包含并报告，其结果不等于三进程范围。控制器在预算外执行：

~~~sh
python core/selftest-oauth-resources.py --fixture /isolated/new-fixture --cgroup /sys/fs/cgroup/oauth-example.slice --report /private/resource-result.json
~~~

自测验证 fixture 标记及 cgroup，不创建/停止服务，不修改限额。每次冷启动验证应仅重启新测试服务；不要用生产重启、生产凭据或清空 OAuth 数据库来得到通过结果。

## ChatGPT 人工验收与回滚

1. 保留所有旧连接，新增无秘密的测试 HTTPS /mcp，选择 OAuth。私有测试邀请只在授权页填写，确认合成稳定 ID 004；该 ID 与 op004 不同。
2. 授权后实际调用 get_calling_guide、list_books、read_book(oauth-stage2.txt)，核对 viewer_id；保存并读回进度。仅能看到工具列表不算读写通过。
3. 使用同一连接重试实际工具调用以验证恢复。撤销仅此次新测试授权后，旧 Token 必须被拒绝、刷新失败，再从客户端重新授权。真实客户端的刷新/撤销行为需单独记录。
4. 回滚演练只修改新测试 MCP 的功能开关并重启该实例：关闭时裸新 /mcp 返回 404，旧 Header/URL 正常；恢复后匿名 401，已撤销授权仍拒绝；核对测试数据和生产摘要。
5. 结束隔离实验时停止明确命名的新测试服务、代理及临时隧道，保留私有 OAuth 数据库/审计到检查完成。测试单位带运行时上限且未设开机启动；清理前确认路径和服务名，不能覆盖现有生产/测试环境。

生产部署、正式 004 到底绑定 004 还是 op004、正式数据连续性、长期 HTTPS 路由及旧 URL 凭据撤销仍需阶段 3/4 明确确认。本轮不做这些动作。

真实客户端刷新验收期间，仅新测试 MCP 的运行时 drop-in 将新签发 Access Token 暂设 60 秒；既有 Token 的到期时间不改。用户重新授权并等待至少 90 秒后实际调用 list_books，由客户端续期；服务端只查刷新凭证消费/新签发计数，不读取或代用客户端 Token。测试完成后移除该测试单元的 oauth-refresh-test.conf，恢复合成 fixture 的 access_ttl=900，daemon-reload 后仅重启此测试 MCP；保留数据库、撤销状态与业务数据。该操作不涉及生产单元。
