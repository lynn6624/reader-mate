"use strict";
// 读伴 ReaderMate — ToolPkg 壳
// 把一个 compose_dsl 的 WebView 页面挂进 Operit 主侧边栏，指向本地读伴服务。
var ROUTE = "toolpkg:com.zeroone.readermate:ui:reader";
function registerToolPkg() {
  ToolPkg.registerUiRoute({
    id: "reader",
    route: ROUTE,
    runtime: "compose_dsl",
    screen: "dist/ui/reader.ui.js",
    params: {},
    title: { zh: "读伴", en: "ReaderMate" }
  });
  ToolPkg.registerNavigationEntry({
    id: "reader_sidebar",
    route: ROUTE,
    surface: "main_sidebar_plugins",
    title: { zh: "读伴", en: "ReaderMate" },
    icon: "auto_stories",
    order: 102
  });
  return true;
}

exports.registerToolPkg = registerToolPkg;
