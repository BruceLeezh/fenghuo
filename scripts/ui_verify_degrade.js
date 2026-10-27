/**
 * 单点扫描页：数据源降级警示 + 多候选选择器 验证
 */
const { chromium } = require("playwright-core");
const path = require("path");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
// 截图统一落到 _shots/（与其余 E2E 一致），不写死机器相关绝对路径
const OUT = path.resolve(__dirname, "..", "..", "_shots");

(async () => {
  const b = await chromium.launch({ executablePath: EDGE, headless: true });
  const ctx = await b.newContext({ viewport: { width: 1500, height: 1300 }, deviceScaleFactor: 1.3 });
  const p = await ctx.newPage();
  const errs = [];
  p.on("console", m => { if (m.type() === "error") errs.push(m.text()); });
  p.on("pageerror", e => errs.push("pageerror: " + e.message));

  await p.goto("http://127.0.0.1:8000/#/scan", { waitUntil: "domcontentloaded" });  // 首页改版后：深链直达单点扫描
  await p.waitForTimeout(1500);

  const out = {};

  // ---- 1. 单点查询，检查降级警示 ----
  // 注意：输入会触发候选下拉，下拉挂在 body 上会盖住按钮。
  // 真实用户点击时下拉会先被 mousedown 关闭；脚本用 fill 不触发 mousedown，
  // 故这里显式等待候选消失后再点，模拟真实操作顺序。
  await p.fill("#queryInput", "北京科蓝软件系统股份有限公司");
  await p.waitForTimeout(900);
  await p.waitForSelector("#suggestDD", { state: "detached" }).catch(() => {});
  await p.evaluate(() => { const d = document.getElementById("suggestDD"); if (d) d.remove(); });
  await p.click("#queryBtn");
  for (let i = 0; i < 200; i++) {
    await p.waitForTimeout(400);
    const done = await p.evaluate(() => {
      const el = document.getElementById("result");
      const m = document.getElementById("loadingMask");
      return el && !el.classList.contains("hidden") && el.innerText.includes("综合安全度") && (!m || m.style.display === "none");
    });
    if (done) break;
  }
  await p.waitForTimeout(800);

  out.query = await p.evaluate(() => {
    const t = document.getElementById("result").innerText;
    return {
      降级警示: t.includes("数据源覆盖不完整"),
      权威源缺失说明: t.includes("企查查 MCP（工商/司法/财务/股东）"),
      积分提示: t.includes("积分余额不足"),
      可信度下降: t.includes("结论可信度下降"),
      未参与卡: document.querySelectorAll("#result .src-card.off").length,
      得分: (t.match(/(\d+\.\d+)\s*综合安全度/) || [])[1] || (t.match(/综合安全度[^\d]*(\d+\.\d+)/) || [])[1],
      红线: t.includes("红线熔断"),
    };
  });
  await p.screenshot({ path: path.join(OUT, "_s1_degraded.png"), fullPage: true });

  // ---- 2. 健康公司对比（本地库命中） ----
  await p.fill("#queryInput", "恒生电子");
  await p.waitForTimeout(900);
  await p.evaluate(() => { const d = document.getElementById("suggestDD"); if (d) d.remove(); });
  await p.click("#queryBtn");
  for (let i = 0; i < 200; i++) {
    await p.waitForTimeout(400);
    const done = await p.evaluate(() => {
      const m = document.getElementById("loadingMask");
      return (!m || m.style.display === "none") && document.getElementById("result").innerText.includes("综合安全度");
    });
    if (done) break;
  }
  await p.waitForTimeout(800);
  out.query2 = await p.evaluate(() => {
    const t = document.getElementById("result").innerText;
    return {
      供应商: (t.match(/^(.+?)\n/) || [])[1],
      降级警示: t.includes("数据源覆盖不完整"),
      维度块: document.querySelectorAll(".dim-block").length,
    };
  });

  console.log("=== SINGLE SCAN VERIFY ===");
  console.log(JSON.stringify(out, null, 2));
  console.log("console errors:", errs.length ? errs.slice(0, 6) : "none");
  await b.close();
})();
