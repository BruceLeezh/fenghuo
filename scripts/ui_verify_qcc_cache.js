// E2E：数据源管理里的「企查查结果缓存」卡片（省积分可视化）
const { chromium } = require("playwright-core");
const path = require("path");
const BASE = "http://127.0.0.1:8000";
const OUT = path.resolve(__dirname, "..", "..", "_shots");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 1100 }, deviceScaleFactor: 1.5 });
  const p = await ctx.newPage();
  p.on("dialog", async (d) => { try { await d.accept(""); } catch (e) {} });
  const errors = [];
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  p.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  await p.goto(BASE, { waitUntil: "domcontentloaded" });
  await sleep(1800);
  await p.evaluate(() => openSourceManager());
  await sleep(2000);

  const R = {};
  R.cacheCard = await p.evaluate(() => {
    const el = [...document.querySelectorAll("#srcMgrBody .src-item")]
      .find(x => (x.querySelector("b") || {}).textContent === "企查查结果缓存");
    if (!el) return null;
    return {
      text: el.innerText.replace(/\s+/g, " ").trim().slice(0, 300),
      buttons: [...el.querySelectorAll("button")].map(b => b.textContent.trim()),
    };
  });

  // 点「清理过期缓存」验证交互
  await p.evaluate(() => purgeQccCache());
  await sleep(2500);
  R.purgeResult = await p.evaluate(() => {
    const el = [...document.querySelectorAll("#srcMgrBody .src-item")]
      .find(x => (x.querySelector("b") || {}).textContent === "企查查结果缓存");
    return el ? el.innerText.replace(/\s+/g, " ").trim().slice(0, 200) : null;
  });
  await p.screenshot({ path: path.join(OUT, "qcc_cache_card.png"), fullPage: false });

  R.consoleErrors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();

  const ok = R.cacheCard && R.cacheCard.buttons.some(b => b.includes("清理过期缓存"))
    && R.cacheCard.buttons.some(b => b.includes("清空全部"))
    && R.cacheCard.text.includes("省积分") && errors.length === 0;
  console.log(ok ? "\n✅ 缓存管理卡片通过" : "\n❌ 存在问题");
  process.exit(ok ? 0 : 1);
})();
