async function refreshKindleSessions(){
  const box = $("#kindleSessions");
  if (!box || $("#kindlePairing").hidden) return;
  const response = await fetchAuth("/api/kindle/sessions");
  const data = await response.json();
  box.replaceChildren();
  if (!response.ok) { box.textContent = data.error || "设备列表暂不可用"; return; }
  if (!data.sessions.length) { box.textContent = "尚无已配对设备"; return; }
  data.sessions.forEach((device, index) => {
    const row = document.createElement("div"); row.className = "device";
    const label = document.createElement("span");
    label.textContent = "设备 " + (index + 1) + " · " + new Date(device.created * 1000).toLocaleDateString();
    const revoke = document.createElement("button");
    revoke.className = "btn"; revoke.textContent = "撤销";
    revoke.addEventListener("click", async () => {
      if (!confirm("撤销这台 Kindle 的登录？")) return;
      const result = await fetchAuth("/api/kindle/sessions/" + encodeURIComponent(device.session_id), {method:"DELETE"});
      if (!result.ok) { toast("撤销失败"); return; }
      refreshKindleSessions();
    });
    row.append(label, revoke); box.append(row);
  });
}
$("#kindleCreate").addEventListener("click", async () => {
  const button = $("#kindleCreate"), link = $("#kindlePairUrl"), note = $("#kindlePairNote");
  button.disabled = true;
  try {
    const response = await fetchAuth("/api/kindle/pairings", {method:"POST"});
    const data = await response.json();
    if (!response.ok) { note.textContent = data.error || "生成失败"; return; }
    link.href = data.url; link.textContent = data.url; link.hidden = false;
    note.textContent = "10 分钟内在 Kindle 浏览器打开此链接，并在设备上确认。链接仅可用一次。";
  } catch (e) { note.textContent = "网络暂不可用，请稍后重试"; }
  finally { button.disabled = false; }
});
