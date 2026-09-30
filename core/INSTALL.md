# 读伴 · 高级安装指引

> 这份文档是「快速开始」之外的高级场景：换端口、改绑定地址、在 Linux VPS 上跑、在 Android 的 proot / Termux 容器里跑，以及各种 locale 与字符编码的坑。
>
> 如果你是第一次装，请先看 [README.md](README.md) 的「快速开始」。

---

## 1. 三种部署环境

读伴在任何能跑 Python 3.8+ 的环境都能跑。下面是三种典型场景的差异：

| 环境 | 启动脚本 | 默认端口 | 典型问题 |
| --- | --- | --- | --- |
| **Windows** | 双击 `start-windows.cmd` | 8831 | 控制台编码（chcp 65001 已处理）；一般没坑 |
| **Linux / macOS / VPS** | `bash start.sh` | 8831 | locale 不是 UTF-8 会让 print 中文路径崩；后台跑要 nohup |
| **Android 容器（proot / Termux）** | `bash start.sh` | 8831 | `/sdcard` 是 FUSE 挂载；flock 不可用；locale 几乎必踩 |

下文按顺序覆盖。

---

## 2. Windows

### 2.1 直接装

1. 装 Python 3.8+（[python.org](https://www.python.org/downloads/)），安装时勾上「Add Python to PATH」。
2. 解压项目到任意目录（**别放 `C:\Program Files\`**——那需要管理员权限写 `data/`）。
3. 双击 `start-windows.cmd`，浏览器自动打开。

### 2.2 控制台编码

`start-windows.cmd` 已经在脚本里执行了 `chcp 65001 >nul`（把控制台切到 UTF-8）。如果你看到 `UnicodeEncodeError` 出现在 `data/server.err.log`，说明服务是用其他方式（比如直接双击 `python reader_server.py`）启动的，控制台编码不是 UTF-8。

两种解决方式：

- 用脚本启动（已处理）。
- 或者自己设环境变量 `PYTHONIOENCODING=utf-8` 再启动。

### 2.3 换端口 / 换绑定地址

`start-windows.cmd` 写死的是 8831 与 127.0.0.1。要改就手动用命令行的方式：

```cmd
set READER_PORT=9001
set READER_HOST=0.0.0.0
python reader_server.py
```

> `0.0.0.0` 会让服务监听所有网卡，**意味着同一网络下的其他设备也能访问**。读伴没有任何鉴权，请仅在可信局域网内这样做，或前面套一层反向代理 + 鉴权。

### 2.4 看日志

后台运行时 `pythonw` 不弹窗口，**出错看不到**。需要看输出请直接 `python reader_server.py`（前台模式），或在 `data/` 下找日志文件——但 `start-windows.cmd` 这条路径**不会**写 `server.log` / `server.err.log`，那两份日志只有 `start.sh` 后台模式才会写。

---

## 3. Linux / macOS / VPS

### 3.1 依赖

- Python 3.8+。
- bash（用来跑 `start.sh`）。
- 没有 pip 包，没有系统包依赖。

### 3.2 locale：UTF-8 三件套

**这是 Linux 上最容易踩的坑**。VPS 默认 locale 经常是 `POSIX` 或 `C`，不是 UTF-8。后端 `print` 中文路径时会报 `UnicodeEncodeError: 'ascii' codec can't encode`，而且**后台运行时这个错误看不到**——`nohup ... &` 把 stderr 重定向到 `data/server.err.log` 你才会发现。

`start.sh` 已经在脚本里设好：

```bash
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONIOENCODING=utf-8
```

如果你直接 `python reader_server.py` 跑前台，**请在执行前手动设同样的三个变量**，否则 `print` 中文路径会爆。

> 不设会怎样？后端在启动时会打印 `地址 http://127.0.0.1:8831/`，若同时打印包含中文的 `书库/` 路径，就直接崩；前台模式你看到 traceback；后台模式崩在 stderr 文件里，看起来像「服务起不来」。

### 3.3 启动 / 停止 / 换端口

```bash
# 启动（后台）
bash start.sh

# 停止
bash stop.sh

# 换端口
READER_PORT=8842 bash start.sh

# 监听所有网卡（注意安全，见 2.3）
READER_HOST=0.0.0.0 READER_PORT=8831 bash start.sh
```

`start.sh` 做了这些事：

1. 设 UTF-8 三件套（见 3.2）。
2. `mkdir -p data/ 书库/`（首次跑不会有这俩目录）。
3. 检查 `data/server.pid` 确认没在跑。
4. `nohup python3 reader_server.py` 后台起来，写 `data/server.log` / `data/server.err.log`。
5. 等 15 秒端口就绪，超时报错并保留现场。

### 3.4 看日志

```bash
tail -f data/server.log        # stdout
tail -f data/server.err.log    # stderr（错误在这里）
```

`stop.sh` 优雅退出（SIGTERM），最多等 5 秒再强杀（SIGKILL）。

### 3.5 让服务开机自启（systemd 示例）

新建 `/etc/systemd/system/readermate.service`：

```ini
[Unit]
Description=ReaderMate local txt reader
After=network.target

[Service]
Type=simple
WorkingDirectory=<项目目录>
Environment=LANG=C.UTF-8
Environment=LC_ALL=C.UTF-8
Environment=PYTHONIOENCODING=utf-8
Environment=READER_HOST=127.0.0.1
Environment=READER_PORT=8831
ExecStart=/usr/bin/python3 reader_server.py
Restart=on-failure
User=<你的用户名>

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now readermate.service
sudo systemctl status readermate.service
```

---

## 4. Android 容器（proot / Termux）

### 4.1 推荐环境

- **Termux**（从 F-Droid 安装，**不要用 Google Play 的旧版**）+ proot-distro 装的发行版（Ubuntu / Debian 皆可）。
- 或者直接 Termux 原生 Python 环境（更轻，但 path 布局不同）。

### 4.2 把读伴放进容器

把项目目录放到 `/sdcard/Download/`（或 Termux 能访问的任意目录）。proot 默认 mount 了 `/sdcard`。

Termux 下进入 proot Ubuntu：

```bash
proot-distro login ubuntu
cd /sdcard/Download/<项目目录>
ls -la
```

### 4.3 跑起来

```bash
bash start.sh
# 输出示例：读伴 V1 已起来：http://127.0.0.1:8831/   (PID 12345)
```

Termux 浏览器或系统浏览器访问 `http://127.0.0.1:8831`。

### 4.4 FUSE 挂载的坑（这是 Android 上最值得知道的）

`/sdcard` 在 Android 上是 FUSE（用户态文件系统）挂载，读伴的两个关键决策就是为了这环境做的：

**坑 1：父目录必须先存在**

FUSE 上 `open()` 一个不存在父目录的文件会失败。`start.sh` 已经 `mkdir -p data/ 书库/`。**别试图自己删了 `data/` 让后端重建**——重启服务时是脚本帮你建，不是后端进程的 try/except 自动重建。

**坑 2：`fcntl.flock` 不可用**

读伴**没有用**任何文件锁（没用 `fcntl.flock`、没用 `portalocker`、没用 `fcntl.fcntl`）。并发安全靠：

- 进程内 `threading.Lock`（仅本进程多线程之间）。
- 写文件先写 `<name>.tmp`，再 `os.replace` 原子重命名——`/sdcard` 上 `os.replace` 在同一挂载点内仍是原子 rename。
- 极端情况（写一半断电）会留一个 `.tmp` 文件，下次启动会被同名文件覆盖，无副作用。

**不要尝试往读伴里加文件锁**——`/sdcard` 上 `flock` 会 `OSError: [Errno 38] Function not implemented`。

### 4.5 locale 三件套必须设

见 3.2。`start.sh` 已经设好。如果你绕过 `start.sh` 直接 `python3 reader_server.py &`，**手动设 `LANG=C.UTF-8` `LC_ALL=C.UTF-8` `PYTHONIOENCODING=utf-8`**。

### 4.6 让读伴在手机锁屏后继续可访问

读伴是 HTTP 服务，不是 WebView 插件。**服务进程在 Termux 前台或后台跑着的时候，本机浏览器都能访问**。Termux 被系统回收后台进程时，服务会一起被回收——这是 Android 系统的限制，不是读伴的 bug。

要持续可访问：

- 在系统设置里把 Termux 加进「不受电池优化限制」列表。
- 用 `termux-wake-lock`（Termux:API 包提供）保持唤醒。

---

## 5. 端口与绑定地址

### 5.1 默认端口

| 脚本 | 默认端口 | 为什么 |
| --- | --- | --- |
| `start-windows.cmd` | **8831** | 与 Linux 脚本端口对齐 |
| `start.sh` | **8831** | 避开常见占用（8765 / 8790 / 8800 / 8810） |

### 5.2 改端口

两个环境变量：`READER_HOST`、`READER_PORT`。

- Windows 手动跑：`set READER_PORT=9001` 再 `python reader_server.py`。
- Linux：`READER_PORT=8842 bash start.sh`。
- 直接跑后端：`READER_PORT=8842 python3 reader_server.py`。

### 5.3 改绑定地址

默认 `127.0.0.1`（仅本机可访问）。要让同一局域网的手机 / 平板 / 其他电脑也能访问：

```bash
READER_HOST=0.0.0.0 bash start.sh
```

> **安全提醒**：读伴没有任何鉴权，任何能访问该端口的客户端都能：列出书库、读取任意 txt、删除任意书。**只在可信网络下这么做**，或前面套一层带鉴权的反向代理（caddy / nginx + BasicAuth）。
>
> #### 启用自带 token 鉴权（推荐）
>
> 从 V1.1 起内置了轻量 token 鉴权（`READER_TOKEN` 环境变量）。**只在绑 `0.0.0.0` 时强烈建议打开**——零鉴权就等于同网段谁都能读写你的书和进度。
>
> 启用：
>
> ```bash
> # Linux / macOS
> READER_TOKEN=你的令牌 bash start.sh
> # 或先 export：
> export READER_TOKEN=你的令牌
> bash start.sh
> ```
>
> ```cmd
> :: Windows（cmd）
> set READER_TOKEN=你的令牌
> start-windows.cmd
> ```
>
> ```powershell
> # Windows（PowerShell）
> $env:READER_TOKEN = "你的令牌"
> .\start-windows.cmd
> ```
>
> 访问地址变成：
>
> ```
> http://<地址>:8831/?token=<你的令牌>
> ```
>
> 首次打开后前端会把 token 存进浏览器的 `localStorage`（键名 `rm_token`），同时用 `replaceState` 把 URL 里的 token 抹掉——地址栏和浏览历史都不再保留。**之后所有 /api/* 调用都会带上 `X-Token` 请求头，不用再手动加。**
>
> 工作机制：
>
> - 没设 `READER_TOKEN` → 完全不校验，跟旧版行为一致（向后兼容）。
> - 设了 `READER_TOKEN` → 所有 `/api/*` 必须带 token，接受 `?token=xxx` 或请求头 `X-Token: xxx`；比对用 `hmac.compare_digest` 防时序攻击。
> - token 不对或缺失 → 返回 `401 {"ok": false, "error": "未授权"}`。
> - `GET /`（页面本身）允许无 token 访问——手机第一次打开就能拿到前端页面，之后前端用 localStorage 里的 token 调 API。
> - `GET /api/ping` 任何时候都免鉴权，只回一句"活着"，专给保活脚本／健康检查用——**别拿 `/api/books` 当健康检查**，开了鉴权后会 401，会被误判成"服务没起"。
> - token 也会被首次写入时的关页前自动换到 URL 上，保证最后一笔进度也能存下来。
> - 开了鉴权时，后端会在 `data/带token的入口.html` 自动写一个**跳板页**（内容是自动跳转到带 token 的地址）。**做外壳 / 快捷方式 / 安卓插件的人可以指向这个本地文件**——这样以后换 token 只要重启后端重写它，不用改外壳代码再重新打包。
>   ⚠ **前提**：跳板页**只在开了 `READER_TOKEN` 时才生成**。没开鉴权时这个文件不存在，外壳若默认指它就会白屏。所以外壳的默认入口应当照旧指 `http://<地址>:<端口>/`，**只有确定要开 token 时**才改指跳板页。
>
> 选一个足够长的随机串当令牌，例如：
>
> ```bash
> # Linux / macOS
> openssl rand -hex 32
> # Windows PowerShell
> [guid]::NewGuid().ToString() + [guid]::NewGuid().ToString()
> ```
>
> 如果你启用了 token 又绑了非 `127.0.0.1`，启动时控制台会打印一段醒目横幅，告诉你带 token 的完整访问方式。

---

## 6. 验收：装完怎么确认没装坏

按下面顺序跑一遍。任何一步失败都说明装得有问题——别跳过。

### 6.1 服务在跑

```bash
# 任选一种能查看 LISTEN 的命令
netstat -tlnp 2>/dev/null | grep 8831
# 或
ss -tlnp | grep 8831
```

Windows 上：

```cmd
netstat -ano | findstr "LISTENING" | findstr ":8831"
```

应能看到对应端口 LISTEN。

### 6.2 首页能开

```bash
curl -s http://127.0.0.1:8831/ | head -c 200
```

应该看到 HTML，里面含「读伴」（或项目名）。如果返回的是空或乱码，看 `data/server.err.log`。

### 6.3 API 能应答

```bash
curl -s http://127.0.0.1:8831/api/books
```

应该返回 JSON：

```json
{"ok": true, "books": [], "dir": "...", "updated": "..."}
```

空 `books` 是正常的——书库还没东西。

### 6.4 导入一本自造 txt

```bash
mkdir -p <项目目录>/tmp_验收
cat > <项目目录>/tmp_验收/测试.txt <<'EOF'
第一章 测试

这是测试内容的第一段。读伴验收用。

第二章 测试二

这是测试内容的第二段。
EOF

curl -s -X POST --data-binary @<项目目录>/tmp_验收/测试.txt \
  "http://127.0.0.1:8831/api/import?name=测试.txt"
```

再 `curl -s http://127.0.0.1:8831/api/books`，应该能看到 `测试.txt` 出现在 `books` 数组里。

浏览器那边：刷新页面，`☰ 书库` → 应该看到《测试》。

### 6.5 滚动后 `progress.json` 出现记录

浏览器里打开《测试》，往下滚两屏，等 2 秒。

```bash
cat <项目目录>/data/progress.json
```

应该看到 `books` 下有 `测试.txt` 一项，`offset` 不为 0、`percent` 在 0~1 之间。

### 6.6 重启页面应续读

浏览器里关掉页面（或关整个浏览器），再重新打开 <http://127.0.0.1:8831>：

- 如果 `测试.txt` 是最近读过的，应该自动打开并跳回上次位置。
- 如果不是，**主动打开它**——也应该跳回上次位置。

### 6.7 停止服务

```bash
bash stop.sh                # Linux
# 或 Windows：双击 stop-windows.cmd
```

确认端口释放：

```bash
netstat -tlnp 2>/dev/null | grep 8831 || echo "端口已释放"
```

应输出「端口已释放」。

### 6.8 清理验收产物

```bash
rm -rf <项目目录>/tmp_验收
# 进页面把《测试》删掉（书库侧栏每本右上角的 ×），或：
curl -s -X POST "http://127.0.0.1:8831/api/delete?name=测试.txt"
```

---

## 7. 常见问题

### Q: 服务起来了，但浏览器打不开

1. 看 `data/server.err.log`，是不是启动时崩了。
2. 看地址是不是 `http://` 而不是 `https://`，端口对不对。
3. 看防火墙（Windows Defender / iptables / 云服务商安全组）。

### Q: `data/progress.json` 没出现我刚才读的内容

1. 等 2 秒以上——后端写盘是滚动停下 1.2 秒后才触发。
2. 看浏览器控制台（F12）有没有报错；读伴会在屏幕底部挂一条红条显示错误。
3. 看 `data/server.err.log` 后端有没有收到 `/api/progress` 的请求。

### Q: 中文路径 / 中文书名乱码

1. Linux / VPS 上确认 locale 是 UTF-8（`locale` 命令看输出）。
2. 绕过 `start.sh` 时手动设 `LANG=C.UTF-8` `LC_ALL=C.UTF-8` `PYTHONIOENCODING=utf-8`。

### Q: 后端能起来，但 `data/server.log` 里全是乱码

启动方式绕过了脚本。**用 `start.sh` 或 `start-windows.cmd` 启动**——脚本里已经处理了编码。

### Q: 想换域名 / HTTPS

读伴只提供 HTTP，没有 TLS。**别把它直接暴露在公网**。需要的话前面挂反向代理：

- [Caddy](https://caddyserver.com/)（自动 HTTPS，最省事）
- nginx + certbot

代理后请加 BasicAuth 或其他鉴权（见 5.3 安全提醒）。

---

## 8. iOS / Safari 怎么用

**Safari 里跑不了 Python**，所以 iOS 上没法「本地起后端 + 浏览器打开 127.0.0.1」这一套。要在这个生态里用，只有两条路。

### 路线一（推荐）：后端放在别的机器，iOS 只当客户端

读伴的前端就是一个网页，iOS 只要能访问到后端就行：

- **同一 Wi-Fi**：后端跑在你家里的电脑 / NAS 上，绑 `0.0.0.0`（见 §5.2），iOS 用 Safari 打开 `http://<那台机器的内网 IP>:8831`。
  ⚠ 只在自家局域网里这么用——读伴自身没有鉴权。
- **公网访问**：后端跑在 VPS 上，前面套反向代理 + HTTPS（Caddy 最省事，或者 Cloudflare Tunnel 这类隧道），并**加一层鉴权**（BasicAuth / 访问令牌，见 §7 的「想换域名 / HTTPS」）。**走公网必须设 `READER_TOKEN`**（见 §5.3 启用自带 token 鉴权）——光靠反代鉴权等于把整个书库和读的权限托给代理层，自带 token 是兜底。

这条路的好处：进度照样落在 `data/progress.json` 里，任何能读文件的 AI 都能知道你读到哪了——这正是读伴存在的理由。而且几台设备读写的是**同一份进度**。

### 路线二（不推荐）：做成纯前端离线版

把逻辑全塞进网页、进度存浏览器存储（IndexedDB / localStorage）。确实能离线用，代价是：

- **AI 读不到你的进度了**（浏览器存储不在文件系统里，外部拿不到）
- 换设备、清缓存就丢进度
- 等于把一个「AI 可读的书架」退化成普通的本地阅读器

如果你的目的就是「让 AI 知道我在读什么」，别走这条。

### 小技巧

Safari 打开后可以「添加到主屏幕」，它会以独立窗口打开，用起来跟 App 差不多（本质上还是那个网页）。

---

## 9. 还有什么不行

V1 故意不做的（也**不打算**在 INSTALL 层面绕过）：

- 划线、批注、目录树：功能没实现，不是装的问题。等 V2。
- epub 富格式（图片、表格、公式）：见 README.md 的 `tools/epub2txt.py` 已知局限。

遇到的不是以上列出的问题时，先看 `data/server.err.log`、浏览器底部红条、再看 [README.md](README.md)。