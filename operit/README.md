# 读伴 ReaderMate · Operit 侧边栏壳

把「读伴」网页阅读器挂进 **Operit** 主侧边栏的一层 ToolPkg 壳。壳本身不含阅读逻辑，只做两件事：

1. 注册一个 `compose_dsl` UI 路由，里面塞一个 `WebView`，指向本地读伴服务（默认 `http://127.0.0.1:8831`）；
2. 注册一个导航入口，让它出现在 Operit 侧边栏。

**内核**（`reader_server.py` + `web/reader.html`）不在这里 —— 见仓库 `core/`。同一份内核 Windows / Linux / Android 通用；这里只有 Operit 特供的那层壳。

---

## 依赖

- 一个在跑的读伴内核（监听 `127.0.0.1:8831`）
- Operit App

## 文件

| 文件 | 作用 |
|---|---|
| `manifest.json` | 包声明（`toolpkg_id=com.zeroone.readermate`，`main=dist/main.js`） |
| `dist/main.js` | 注册 UI 路由 + 侧边栏入口 |
| `dist/ui/reader.ui.js` | 屏幕本体：渲染 WebView + 旁路保活 |
| `ensure.sh` | 保活脚本（幂等）：探活 `/api/ping`，不在就拉起内核 |

## 安装

1. 把本目录整个放进 Operit 的开发包目录，例如
   `/sdcard/Download/Operit/dev_package/com.zeroone.readermate/`（**目录名要与 `toolpkg_id` 一致**）。
2. **把 `manifest.json` 装进 Operit（俗称「烧录」）** —— 二选一：

   **A · AI 烧（推荐，最稳）**：若 Operit 侧装了 `operit_editor` 这个包，直接让它调用：

   ```
   operit_editor:debug_install_toolpkg
     source_path = /sdcard/Download/Operit/dev_package/com.zeroone.readermate/manifest.json
     wait_ms = 20000
   ```

   返回成功即完成注册，**全程不需要任何界面操作**。本项目开发时一直用这条，没出过岔。

   **B · 人工导入**：若你的 Operit「包管理 → 插件」里存在「导入本地包 / 安装工具包」这类入口，可把本目录打成一个 `.toolpkg`（就是 **zip 改名**，内含 `manifest.json` + `dist/`），再从该入口选它安装。

   ⚠️ 并非所有版本都暴露这个入口；**若翻不到，就回到 A** —— A 不依赖任何界面。

3. 打开 Operit 左侧栏，点「读伴」。

> 装完左侧栏没出现「读伴」时：先**重启 Operit**；若仍无，检查 Operit 版本是否支持 ToolPkg 的 `main_sidebar_plugins`（API 1.0.0 起）。

## 两处按需改（你的路径/端口不一定一样）

- `dist/ui/reader.ui.js` 顶部 `READER_URL` —— 内核服务地址。
- `dist/ui/reader.ui.js` 顶部 `ENSURE_CMD` —— 保活脚本路径（默认 `/sdcard/Download/Operit/reader-mate/ensure.sh`）。

改完要**重新烧录**才生效。

## 踩坑（复现即省两小时）

1. `registerUiRoute` 的 `screen` 必须是**路径字符串**（`"dist/ui/reader.ui.js"`），不能传函数对象。
2. `toolpkg_id` 用反向域名格式，开发目录名要与 id 一致。
3. `compose_dsl` **没有 `useEffect`**（只有 `useState / useMutable / useRef / useMemo`）。
4. 渲染体里**绝不能 `await` 工具调用** —— 会把屏幕钉死在「启动中」。副作用走旁路 fire-and-forget。

## 保活说明

Android 没有 systemd，后台进程会被系统回收，服务偶尔会掉。`ensure.sh` 只保证「打开侧边栏时服务在」；要更稳可以再跑一个轮询的看门狗（每 N 秒探活，掉了就拉起）。

---

MIT License.
