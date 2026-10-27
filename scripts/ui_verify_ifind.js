// E2E 验证：同花顺 iFinD 真实接入（数据源管理卡片状态 + 文案 + 聚合链路）
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

  // 打开数据源管理
  R.opened = await p.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find(x => (x.textContent || "").includes("数据源管理"));
    if (b) { b.click(); return true; }
    return false;
  });
  await sleep(1400);

  R.mgr = await p.evaluate(() => {
    const mask = document.getElementById("srcMgrMask");
    if (!mask) return { error: "no #srcMgrMask" };
    const items = [...mask.querySelectorAll(".src-item")];
    const rows = items.map((it) => {
      const name = (it.querySelector(".src-head b") || {}).textContent || "";
      const badge = (it.querySelector(".src-status") || it.querySelector(".badge") || {}).textContent || "";
      const sw = it.querySelector(".sw input");
      const fields = [...it.querySelectorAll(".src-fields label")].map(l => (l.textContent || "").trim());
      return { name: name.trim(), status: badge.trim(), on: sw ? sw.checked : null, fields };
    });
    return {
      intro: (mask.querySelector("div[style*='line-height']") || {}).textContent || "",
      rows,
    };
  });

  // iFinD 行详情
  R.ifind = (R.mgr.rows || []).find(r => r.name.includes("iFinD")) || null;
  R.wind = (R.mgr.rows || []).find(r => r.name.includes("Wind")) || null;

  // 文案断言：不应再出现"待接入槽位（同花顺"
  R.introHasIfindPlaceholder = /待接入槽位（同花顺/.test(R.mgr.intro || "");
  R.introMentionsIfindCapability = /iFinD 提供实时行情/.test(R.mgr.intro || "");

  await p.screenshot({ path: path.join(OUT, "ifind_mgr.png"), fullPage: false });

  // 关弹层
  await p.evaluate(() => {
    const c = document.querySelector("#srcMgrMask .cand-close");
    if (c) c.click();
  });
  await sleep(400);

  // 单点查询：验证 iFinD 参与聚合（行情来源徽章）
  R.query = await p.evaluate(async () => {
    try {
      const r = await fetch("/api/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "北京科蓝软件系统股份有限公司", source: "auto" }),
      });
      const d = await r.json();
      const res = d.result || {};
      return {
        total: res.total_score,
        level: res.level,
        sources: (res.sources || []).map(s => s.key || s.label),
        marketSource: (res.market || {}).source,
        marketPrice: (res.market || {}).price,
        reputation: res.dimensions && res.dimensions.reputation ? res.dimensions.reputation.score : null,
        reputationNa: res.dimensions && res.dimensions.reputation ? res.dimensions.reputation.na : null,
        newsCount: (res.news || []).length,
      };
    } catch (e) {
      return { error: String(e) };
    }
  });

  await sleep(600);
  await p.screenshot({ path: path.join(OUT, "ifind_result.png"), fullPage: false });

  R.consoleErrors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
