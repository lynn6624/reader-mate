// 读伴自测 · 纯逻辑（离线跑，不碰服务）
// 用法：node selftest.js
const fs = require("fs");
const path = require("path");
const P = path.join(__dirname, "web", "reader.html");
const html = fs.readFileSync(P, "utf8");
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log("× 没抽到 script"); process.exit(1); }
const js = m[1];

// ① 整段 script 语法编译（只编译不执行）
try { new Function(js); console.log("① script 语法 OK，共 " + js.length + " 字符"); }
catch (e) { console.log("① × 语法错误：" + e.message); process.exit(1); }

// ② 抽出纯逻辑区做行为断言
const a = js.indexOf("const CHAP_RE"), b = js.indexOf("/* ---------------- 进度");
if (a < 0 || b < 0) { console.log("× 抽取纯逻辑区失败"); process.exit(1); }
const { splitSegments, segIndexAtOffset, state } = new Function(
  js.slice(a, b) + "\nconst state = { segs: [] };\nreturn { splitSegments, segIndexAtOffset, state };")();

const text = ["第一章 敲门", "", "第一段。龙骨在墙上。", "第二段。风从缝里过。", "",
              "第二章 龙骨", "第三段。龙骨被搬走了。", "  ", "  缩进行也认。"].join("\n");
const segs = splitSegments(text);
let bad = 0;
for (const s of segs) if (text.slice(s.start, s.end) !== s.text) bad++;
console.log("② 段数 " + segs.length + "，偏移校验失败 " + bad + " 处（应为 0）");
console.log("   章节识别：" + segs.map(s => s.chapter || "-").join(" / "));

state.segs = segs;
const off = text.indexOf("第三段");
const i = segIndexAtOffset(off);
console.log("③ 定位「第三段」offset=" + off + " → 段 " + i + " " + JSON.stringify(segs[i]) +
            "（应命中第三段本身，且 start=" + off + "）");
console.log("   段落数一致性：" + (segs.every((s, n) => n === 0 || s.start > segs[n - 1].start) ? "偏移单调递增 OK" : "× 非单调"));

const s2 = splitSegments("啊".repeat(3000) + "\n尾段");
console.log("④ 3000 字单行 → 拆成 " + s2.length + " 段（首段 " + s2[0].start + "-" + s2[0].end + "），末段=" + JSON.stringify(s2[s2.length - 1].text));
