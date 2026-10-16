// 配对成功的 Kindle 可能留有旧身份；这一跳只使用服务端设备 cookie。
try {
  const kindleUrl = new URL(location.href);
  if (kindleUrl.searchParams.get("kindle") === "1") {
    localStorage.removeItem("rm_who_token");
    localStorage.removeItem("rm_token");
    localStorage.removeItem("rm_who");
    kindleUrl.searchParams.delete("kindle");
    kindleUrl.searchParams.delete("full");
    history.replaceState(null, "", kindleUrl.pathname + kindleUrl.search + kindleUrl.hash);
  }
} catch (e) {}
