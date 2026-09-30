/// <reference path="../../../types/index.d.ts" />
// 读伴 ReaderMate — UI 屏（v2 · 不阻塞版）
// 教训：compose_dsl 没有 useEffect；渲染体里 await 工具调用＝拿命堵屏。
// 所以这里只做两件事：① 立刻渲染 WebView；② 旁路 fire-and-forget 发一次保活命令。
var READER_URL = "http://127.0.0.1:8831";
var ENSURE_CMD = "bash '/sdcard/Download/Operit/reader-mate/ensure.sh'";
function Screen(ctx) {
  // 保活只发一次：用 useRef 挡重入，不等 promise，不碰 state。
  var kicked = ctx.useRef("ensureKicked", false);
  if (!kicked.current) {
    kicked.current = true;
    try {
      ctx.callTool("super_admin:terminal", { command: ENSURE_CMD, timeoutMs: 25000 });
    } catch (e) {}
  }
  var ctrl = ctx.createWebViewController("reader_mate");
  return ctx.UI.WebView({
    url: READER_URL,
    controller: ctrl,
    javaScriptEnabled: true,
    domStorageEnabled: true,
    databaseEnabled: true,
    allowFileAccess: true,
    allowContentAccess: true,
    supportZoom: false,
    builtInZoomControls: false,
    useWideViewPort: true,
    loadWithOverviewMode: true,
    mixedContentMode: "alwaysAllow",
    cacheMode: "default"
  });
}
exports.default = Screen;
