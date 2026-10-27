// E2E 验证：数据源管理界面（配置中心 + 自定义数据源）
const { chromium } = require("playwright-core");
const path = require("path");

const BASE = "http://127.0.0.1:8000";
const OUT = path.resolve(__dirname, "..", "..", "_shots");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({
    channel: "msedge", headless: true,
    args: ["--disable-blink-features=AutomationControlled"],
  });
  const ctx = await browser.newContext({ viewport: { width: 1560, height: 1180 }, deviceScaleFactor: 1 });
  const p = await ctx.newPage();
  p.on("dialog", async (d) => { try { await d.accept(""); } catch (e) {} });
  const errors = [];
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  p.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  const R = {};

  await p.goto(BASE, { waitUntil: "domcontentloaded" });
  await sleep(1600);

  // headerMeta 文案（含可扩展槽位数）
  R.headerMeta = await p.evaluate(() => (document.getElementById("headerMeta") || {}).textContent || "");
  R.hasMgrBtn = await p.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find(x => (x.textContent || "").includes("数据源管理"));
    if (b) { b.click(); return true; }
    return false;
  });
  await sleep(1200);

  R.mgr = await p.evaluate(() => {
    const mask = document.getElementById("srcMgrMask");
    if (!mask) return { error: "no #srcMgrMask" };
    const items = [...mask.querySelectorAll(".src-item")];
    const names = items.map(i => (i.querySelector(".src-head b") || {}).textContent || "");
    const switches = [...mask.querySelectorAll(".sw input")].map(s => s.checked);
    const tokenInput = mask.querySelector("#sf_qcc_token");
    const hasCustomBtn = !!mask.querySelector("button[onclick='openCustomForm()']");
    return {
      itemCount: items.length,
      names,
      switchStates: switches,
      qccTokenInput: !!tokenInput,
      qccTokenPlaceholder: tokenInput ? tokenInput.placeholder.slice(0, 30) : "",
      hasCustomBtn,
    };
  });
  await p.screenshot({ path: path.join(OUT, "m1_source_manager.png"), fullPage: false });

  // 新增自定义数据源
  R.openCustom = await p.evaluate(() => {
    const b = [...document.querySelectorAll("#srcMgrMask button")].find(x => (x.textContent || "").includes("新增自定义"));
    if (b) { b.click(); return true; }
    return false;
  });
  await sleep(500);
  await p.evaluate(() => {
    document.getElementById("cfLabel").value = "行内黑名单系统";
    document.getElementById("cfUrl").value = "https://api.example.com/risk?company={name}";
    document.getElementById("cfToken").value = "test-token-123";
    document.getElementById("cfSubmit").click();
  });
  await sleep(1600);

  R.afterAdd = await p.evaluate(() => {
    const mask = document.getElementById("srcMgrMask");
    if (!mask) return { error: "no mask" };
    return {
      customItems: mask.querySelectorAll(".src-item").length,
      hasCustom: mask.textContent.includes("行内黑名单系统"),
    };
  });
  await p.screenshot({ path: path.join(OUT, "m2_custom_added.png"), fullPage: false });

  // 删除该自定义源
  R.removeCustom = await p.evaluate(() => {
    const btn = [...document.querySelectorAll("#srcMgrMask .mini-btn.danger")].find(x => (x.textContent || "").includes("删除"));
    if (btn) { btn.click(); return true; }
    return false;
  });
  await sleep(1400);

  R.afterRemove = await p.evaluate(() => {
    const mask = document.getElementById("srcMgrMask");
    return {
      stillHasCustom: mask ? mask.textContent.includes("行内黑名单系统") : null,
    };
  });

  // 关闭弹层
  await p.evaluate(() => {
    const c = document.querySelector("#srcMgrMask .cand-close");
    if (c) c.click();
  });
  await sleep(400);

  R.consoleErrors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
