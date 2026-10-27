// E2E 验证：专属场景 + 服务水平指标 + 外包专项过往记录
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

  const R = {};

  await p.goto(BASE, { waitUntil: "domcontentloaded" });
  await sleep(1600);

  // 进入周期监控中心
  await p.evaluate(() => {
    const t = [...document.querySelectorAll("button")].find(e => (e.textContent || "").includes("周期监控"));
    if (t) t.click();
  });
  await sleep(800);

  const clickTab = async (label) => {
    const ok = await p.evaluate((lb) => {
      const t = [...document.querySelectorAll("#pageMonitor .subnav button")]
        .find(e => (e.textContent || "").includes(lb));
      if (t) { t.click(); return true; }
      return false;
    }, label);
    await sleep(400);
    return ok;
  };

  // ================= 1. 服务水平指标 =================
  R.tabServiceLevel = await clickTab("服务水平");
  await sleep(2600);

  R.serviceLevel = await p.evaluate(() => {
    const el = document.getElementById("slArea");
    if (!el) return { error: "no #slArea" };
    const cats = [...el.querySelectorAll(".sl-cat")];
    const statCards = [...el.querySelectorAll(".stat-card")];
    const tableRows = [...el.querySelectorAll(".sl-table tbody tr")];
    return {
      cats: cats.length,
      metricRows: el.querySelectorAll(".sl-metric").length,
      statCards: statCards.length,
      tableRows: tableRows.length,
      catNames: cats.slice(0, 6).map(c => (c.querySelector(".sl-cat-name") || {}).textContent || ""),
      clauseHints: cats.slice(0, 6).map(c => (c.querySelector(".sl-cat-clause") || {}).textContent || "").map(s => s.slice(0, 16)),
      firstRow: tableRows[0] ? tableRows[0].textContent.replace(/\s+/g, " ").trim().slice(0, 150) : "",
      statText: statCards.map(c => c.textContent.replace(/\s+/g, " ").trim()).join(" | ").slice(0, 220),
    };
  });
  await p.screenshot({ path: path.join(OUT, "s1_servicelevel.png"), fullPage: true });

  // 打开明细
  const detailOpened = await p.evaluate(() => {
    const b = [...document.querySelectorAll("#slArea .sl-table button")].find(x => (x.textContent || "").includes("查看详情"));
    if (b) { b.click(); return true; }
    return false;
  });
  await sleep(1800);
  R.slDetail = await p.evaluate(() => {
    const box = document.getElementById("slDetailBox");
    if (!box) return { error: "no detail box" };
    const cons = [...box.querySelectorAll(".consec-item")];
    return {
      opened: box.innerHTML.length > 200,
      hasDetailCard: !!box.querySelector(".sl-table"),
      cats: box.querySelectorAll(".sl-cat").length,
      metrics: box.querySelectorAll(".sl-metric").length,
      spark: box.querySelectorAll(".spark i").length,
      consecItems: cons.length,
      consecFirst: cons[0] ? cons[0].textContent.replace(/\s+/g, " ").trim().slice(0, 110) : "",
      hasWorkorderBtn: box.innerHTML.includes("生成处置工单"),
      periodRows: box.querySelectorAll("table tbody tr").length,
      conclusion: (() => {
        const m = box.textContent.match(/评价结论：([^\n]{0,80})/);
        return m ? m[1].trim() : "";
      })(),
    };
  });
  await p.screenshot({ path: path.join(OUT, "s2_sl_detail.png"), fullPage: true });

  // ================= 2. 专属场景 =================
  R.tabScenario = await clickTab("专属场景");
  await sleep(3200);

  R.scenario = await p.evaluate(() => {
    const bar = document.getElementById("scenarioBar");
    const el = document.getElementById("scenArea");
    const sel = document.getElementById("scenSupplier");
    if (!el) return { error: "no #scenArea" };
    const picks = [...(bar ? bar.querySelectorAll(".scen-pick") : [])];
    const signs = [...el.querySelectorAll(".sig-item")];
    const wx = [...el.querySelectorAll(".wcmp-row")];
    return {
      pickCards: picks.length,
      pickNames: picks.map(x => (x.querySelector(".sp-name") || {}).textContent || ""),
      supplierOptions: sel ? sel.options.length : 0,
      selectedSupplier: sel ? sel.options[sel.selectedIndex].textContent.slice(0, 30) : "",
      hasShiftBox: !!el.querySelector(".shift-box"),
      shiftText: (() => { const s = el.querySelector(".shift-box"); return s ? s.textContent.replace(/\s+/g, " ").trim().slice(0, 220) : ""; })(),
      weightRows: wx.length,
      signals: signs.length,
      signalTitles: signs.map(s => (s.querySelector(".sig-title") || {}).textContent || "").slice(0, 4),
      headerText: (() => { const m = el.textContent.match(/(核心系统运行维护|业务系统开发测试|客服与集中作业|安全服务外包|规划咨询)场景/); return m ? m[0] : ""; })(),
      hasVerdict: el.textContent.includes("场景化结论"),
    };
  });
  await p.screenshot({ path: path.join(OUT, "s3_scenario.png"), fullPage: true });

  // 切换到开发测试场景，验证结论变化
  const switched = await p.evaluate(() => {
    const picks = [...document.querySelectorAll("#scenarioBar .scen-pick")];
    const t = picks.find(x => (x.textContent || "").includes("开发"));
    if (t) { t.click(); return true; }
    return false;
  });
  await sleep(3600);
  R.scenarioSwitched = await p.evaluate(() => {
    const el = document.getElementById("scenArea");
    const s = el ? el.querySelector(".shift-box") : null;
    return {
      switched: true,
      shiftText: s ? s.textContent.replace(/\s+/g, " ").trim().slice(0, 220) : "",
      headerText: (() => { const m = el.textContent.match(/业务系统开发测试场景/); return m ? m[0] : ""; })(),
      activeCard: (() => { const a = document.querySelector("#scenarioBar .scen-pick.active .sp-name"); return a ? a.textContent : ""; })(),
      signals: el.querySelectorAll(".sig-item").length,
    };
  });
  await p.screenshot({ path: path.join(OUT, "s4_scenario_switched.png"), fullPage: true });

  // ================= 3. 外包专项 =================
  R.tabRoster = await clickTab("供应商台账");
  await sleep(1500);

  const openedSpecial = await p.evaluate(() => {
    const b = [...document.querySelectorAll("#rosterArea button")].find(x => (x.textContent || "").includes("外包专项"));
    if (b) { b.click(); return true; }
    return false;
  });
  R.openedSpecial = openedSpecial;
  await sleep(12000);   // 首次抓取企查查标讯较慢

  R.special = await p.evaluate(() => {
    const box = document.getElementById("spBox");
    if (!box) return { error: "no #spBox" };
    const stats = [...box.querySelectorAll(".sp-stat")];
    const recs = [...box.querySelectorAll(".sp-rec")];
    const sigs = [...box.querySelectorAll(".sig-item")];
    const src = box.querySelector(".sp-src");
    return {
      hasModal: !!document.getElementById("spModal"),
      stats: stats.length,
      statText: stats.map(s => s.textContent.replace(/\s+/g, " ").trim()).slice(0, 6),
      records: recs.length,
      firstRec: recs[0] ? recs[0].textContent.replace(/\s+/g, " ").trim().slice(0, 140) : "",
      signals: sigs.length,
      insight: (() => { const m = box.textContent.match(/专项洞察：([^\n]{0,120})/); return m ? m[1].trim() : ""; })(),
      hasError: box.textContent.includes("外包过往记录未取到"),
      errorText: (() => { const m = box.textContent.match(/外包过往记录未取到\s*([^\n]{0,90})/); return m ? m[1].trim() : ""; })(),
      // 数据来源诚实标注
      srcBadge: src ? (src.classList.contains("offline") ? "offline" : "live") : "none",
      srcText: src ? src.textContent.replace(/\s+/g, " ").trim().slice(0, 160) : "",
    };
  });
  await p.screenshot({ path: path.join(OUT, "s5_special.png"), fullPage: true });

  // ---- 再切一家供应商，验证离线兜底覆盖多家 ----
  const openedSpecial2 = await p.evaluate(() => {
    const bs = [...document.querySelectorAll("#rosterArea button")].filter(x => (x.textContent || "").includes("外包专项"));
    if (bs[2]) { bs[2].click(); return true; }
    return false;
  });
  R.openedSpecial2 = openedSpecial2;
  await sleep(9000);
  R.special2 = await p.evaluate(() => {
    const box = document.getElementById("spBox");
    if (!box) return { error: "no #spBox" };
    const src = box.querySelector(".sp-src");
    return {
      stats: box.querySelectorAll(".sp-stat").length,
      records: box.querySelectorAll(".sp-rec").length,
      signals: box.querySelectorAll(".sig-item").length,
      srcBadge: src ? (src.classList.contains("offline") ? "offline" : "live") : "none",
    };
  });
  await p.screenshot({ path: path.join(OUT, "s6_special_second.png"), fullPage: true });

  R.consoleErrors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
