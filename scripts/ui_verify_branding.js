// 排版验收：截取首页 + 滚动后（验证 sticky 导航）+ 宽屏/窄屏
const { chromium } = require("playwright-core");
const path = require("path");
const BASE = "http://127.0.0.1:8000";
const OUT = path.resolve(__dirname, "..", "..", "_shots");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const errors = [];

  // 宽屏
  let ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.5 });
  let p = await ctx.newPage();
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  p.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await p.goto(BASE, { waitUntil: "domcontentloaded" });
  await sleep(1800);
  await p.screenshot({ path: path.join(OUT, "newui_home.png"), fullPage: false });

  const R = await p.evaluate(() => {
    const h1 = document.querySelector(".header h1");
    const nav = document.querySelector(".topnav");
    return {
      title: document.title,
      h1: h1 ? h1.innerText.replace(/\s+/g, " ").trim() : "",
      navSticky: getComputedStyle(nav).position,
      headerBg: getComputedStyle(document.querySelector(".header")).backgroundImage.slice(0, 40),
    };
  });

  // 滚动验证 sticky（当前内容不足一屏，注入占位高度触发真实滚动）
  const scrollInfo = await p.evaluate(() => {
    const c = document.querySelector(".container");
    if (c) c.style.minHeight = "2400px";
    window.scrollTo(0, 900);
    return { scrollHeight: document.body.scrollHeight, innerHeight: window.innerHeight };
  });
  await sleep(500);
  R.scrollInfo = scrollInfo;
  R.scrollY = await p.evaluate(() => window.scrollY);
  await p.screenshot({ path: path.join(OUT, "newui_scrolled.png"), fullPage: false });
  R.navVisibleAfterScroll = await p.evaluate(() => {
    const nav = document.querySelector(".topnav");
    const r = nav.getBoundingClientRect();
    return r.top >= -1 && r.top <= 30;
  });
  await ctx.close();

  // 窄屏
  ctx = await browser.newContext({ viewport: { width: 800, height: 900 }, deviceScaleFactor: 1.4 });
  p = await ctx.newPage();
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await p.goto(BASE, { waitUntil: "domcontentloaded" });
  await sleep(1500);
  await p.screenshot({ path: path.join(OUT, "newui_narrow.png"), fullPage: false });
  await ctx.close();

  R.consoleErrors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
  const ok = R.title.includes("烽火") && R.h1.includes("烽火") && R.navSticky === "sticky"
    && R.navVisibleAfterScroll && errors.length === 0;
  console.log(ok ? "\n✅ 排版验收通过" : "\n❌ 有问题");
  process.exit(ok ? 0 : 1);
})();
