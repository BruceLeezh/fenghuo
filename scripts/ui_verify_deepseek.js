// E2E 验证：数据源管理中 DeepSeek 显示"已启用" + 连通性自检按钮可用
const { chromium } = require("playwright-core");
const path = require("path");
const BASE = "http://127.0.0.1:8000";
const OUT = path.resolve(__dirname, "..", "..", "_shots");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 1.5 });
  const p = await ctx.newPage();
  p.on("dialog", async (d) => { try { await d.accept(""); } catch (e) {} });
  const errors = [];
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  p.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  await p.goto(BASE, { waitUntil: "domcontentloaded" });
  await sleep(1800);

  // 打开数据源管理
  await p.evaluate(() => openSourceManager());
  await sleep(1500);

  const R = {};
  R.cards = await p.evaluate(() => {
    return [...document.querySelectorAll("#srcMgrBody .src-item")].map(el => {
      const name = (el.querySelector(".src-head b") || {}).textContent || "";
      const status = (el.querySelector(".src-head .tag:last-child") || {}).textContent || "";
      const hasTestBtn = !!([...el.querySelectorAll("button")].find(b => (b.textContent || "").includes("测试连通性")));
      return { name, status, hasTestBtn };
    });
  });
  R.deepseek = R.cards.find(c => c.name.includes("DeepSeek")) || null;
  await p.screenshot({ path: path.join(OUT, "deepseek_enabled.png"), fullPage: false });

  // 点 DeepSeek 的测试连通性
  await p.evaluate(() => testSource("deepseek"));
  await sleep(9000);
  R.testResult = await p.evaluate(() => {
    const box = document.getElementById("st_deepseek");
    return box ? { cls: box.className, txt: (box.textContent || "").trim() } : null;
  });
  await p.screenshot({ path: path.join(OUT, "deepseek_tested.png"), fullPage: false });

  R.consoleErrors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();

  // 环境未配置真实 key 时该用例无法通过（自检必然 401）：明确 SKIP，而不是记成回归。
  // 卡片与按钮仍要存在（否则是真的 UI 缺陷）。
  const txt = (R.testResult && R.testResult.txt) || "";
  const notConfigured = txt.includes("未配置 DEEPSEEK_API_KEY");
  if (notConfigured) {
    const uiOk = !!R.deepseek && R.deepseek.hasTestBtn && errors.length === 0;
    console.log(uiOk ? "⏭ SKIP：环境未配置真实 DEEPSEEK_API_KEY（AI 分析按设计降级）"
                     : "❌ 未配置 key，但 DeepSeek 卡片/自检按钮缺失");
    process.exit(uiOk ? 0 : 1);
  }

  const ok = R.deepseek && R.deepseek.status.includes("已启用") && R.deepseek.hasTestBtn
    && R.testResult && R.testResult.cls.includes("pass")
    && R.testResult.txt.includes("DeepSeek-V4.1-Flash")
    && errors.length === 0;
  console.log(ok ? "\n✅ DeepSeek 启用状态与自检全部通过" : "\n❌ 存在未通过项");
  process.exit(ok ? 0 : 1);
})();
