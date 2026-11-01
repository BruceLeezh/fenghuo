const { chromium } = require("playwright-core");
const path = require("path");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
// 截图统一落到 _shots/（与其余 E2E 一致），并按脚本位置解析为绝对路径，
// 避免把机器相关的绝对路径写死在代码里。
const OUT = path.resolve(__dirname, "..", "..", "_shots");

(async () => {
  const b = await chromium.launch({ executablePath: EDGE, headless: true });
  const ctx = await b.newContext({ viewport: { width: 1500, height: 1200 }, deviceScaleFactor: 1.4, acceptDownloads: true });
  const p = await ctx.newPage();
  const errs = [];
  p.on("console", m => { if (m.type() === "error") errs.push(m.text()); });
  p.on("pageerror", e => errs.push("pageerror: " + e.message));

  const t0 = Date.now();
  await p.goto("http://127.0.0.1:8000/#/scan", { waitUntil: "domcontentloaded" });  // 首页改版后：深链直达单点扫描
  await p.fill("#queryInput", "北京科蓝软件系统股份有限公司");
  await p.click("button.btn-primary");
  for (let i = 0; i < 150; i++) {
    await p.waitForTimeout(400);
    if (await p.evaluate(() => {
      const el = document.getElementById("result");
      const m = document.getElementById("loadingMask");
      return el && !el.classList.contains("hidden") && el.innerText.includes("综合安全度") && (!m || m.style.display === "none");
    })) break;
  }
  const elapsed = ((Date.now() - t0) / 1000).toFixed(2);

  // 展开全部维度
  await p.evaluate(() => document.querySelectorAll(".dim-block").forEach(b => b.classList.add("open")));
  await p.waitForTimeout(500);

  const info = await p.evaluate(() => {
    const root = document.getElementById("result");
    const t = root.innerText;
    const q = s => document.querySelectorAll(s).length;
    return {
      rendered: !root.classList.contains("hidden"),
      chars: t.length,
      综合安全度: t.includes("综合安全度"),
      加权明细: t.includes("AHP 加权明细"),
      CR: /CR\s*=\s*0\.0199/.test(t),
      红线: t.includes("红线熔断"),
      风险提示: t.includes("风险提示"),
      六维细则: t.includes("六大维度评分细则"),
      履约表现: t.includes("履约表现"),
      数据源: t.includes("数据源覆盖与交叉验证"),
      行情: t.includes("二级市场行情"),
      AI: t.includes("AI 财务健康度分析"),
      股东: t.includes("主要股东"),
      exportBtn: !!document.querySelector("button[onclick='exportReport()']"),
      dimOpen: q(".dim-block.open"),
      tables: q("table"),
      tableRows: q("tbody tr"),
      srcBadges: q(".src-badge"),
    };
  });

  // 测试导出报告
  let download = "n/a";
  try {
    const [dl] = await Promise.all([
      p.waitForEvent("download", { timeout: 60000 }),
      p.click("button[onclick='exportReport()']"),
    ]);
    const path = await dl.path();
    const fs = require("fs");
    const size = path ? fs.statSync(path).size : 0;
    download = `${dl.suggestedFilename()} (${size} bytes)`;
    await dl.saveAs("_exported_report.html");
  } catch (e) { download = "FAILED: " + e.message.split("\n")[0]; }

  await p.screenshot({ path: path.join(OUT, "_v2_full.png"), fullPage: true });
  await p.evaluate(() => window.scrollTo(0, 0));
  await p.waitForTimeout(300);
  await p.screenshot({ path: path.join(OUT, "_v2_top.png") });

  console.log("=== E2E VERIFY v2 ===");
  console.log("elapsed:", elapsed + "s");
  console.log(JSON.stringify(info, null, 2));
  console.log("download:", download);
  console.log("console errors:", errs.length ? errs.slice(0, 6) : "none");
  await b.close();
})();
