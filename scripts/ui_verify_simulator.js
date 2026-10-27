// E2E 验证：处置策略推演沙盘（第27/31/33条）
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

  // ================= 1. 处置推演 - 基线 =================
  R.tabSimulator = await clickTab("处置推演");
  await sleep(2600);

  R.baseline = await p.evaluate(() => {
    const el = document.getElementById("simArea");
    if (!el) return { error: "no #simArea" };
    const stats = [...el.querySelectorAll(".stat-card")];
    const rows = [...el.querySelectorAll(".sl-table tbody tr")];
    const sels = [...el.querySelectorAll("select.sim-sel")];
    const tags = [...el.querySelectorAll(".tag")];
    return {
      statCards: stats.length,
      statText: stats.map(c => c.textContent.replace(/\s+/g, " ").trim()).join(" | ").slice(0, 220),
      matrixRows: rows.length,
      actionSelects: sels.length,
      firstSelectValue: sels[0] ? sels[0].value : "",
      lineChips: tags.length,
      hasLoadError: el.textContent.includes("加载失败"),
    };
  });
  await p.screenshot({ path: path.join(OUT, "t1_sim_baseline.png"), fullPage: true });

  // ================= 2. 载入建议 =================
  R.suggestClicked = await p.evaluate(() => {
    const b = document.getElementById("simSuggest");
    if (b) { b.click(); return true; }
    return false;
  });
  await sleep(2200);

  R.suggest = await p.evaluate(() => {
    const el = document.getElementById("simArea");
    const sels = [...el.querySelectorAll("select.sim-sel")];
    const res = document.getElementById("simResult");
    return {
      selects: sels.map(s => s.value),
      nonHold: sels.filter(s => s.value !== "hold").map(s => s.value),
      resultHtml: res ? res.innerHTML.length : 0,
      hasSuggestionStrip: res ? res.textContent.includes("建议的初始处置组合") : false,
      suggestionText: res ? res.textContent.replace(/\s+/g, " ").trim().slice(0, 300) : "",
    };
  });
  await p.screenshot({ path: path.join(OUT, "t2_sim_suggest.png"), fullPage: true });

  // ================= 3. 执行推演 =================
  R.runClicked = await p.evaluate(() => {
    const b = document.getElementById("simRun");
    if (b) { b.click(); return true; }
    return false;
  });
  await sleep(2600);

  R.result = await p.evaluate(() => {
    const el = document.getElementById("simResult");
    if (!el) return { error: "no #simResult" };
    const stats = [...el.querySelectorAll(".stat-card")];
    const shareRows = [...el.querySelectorAll(".conc-share-row")];
    const mitigRows = [...el.querySelectorAll("table.sl-table tbody tr")];
    return {
      resultHtmlLen: el.innerHTML.length,
      statCards: stats.length,
      statText: stats.map(c => c.textContent.replace(/\s+/g, " ").trim()).join(" | ").slice(0, 240),
      shareRows: shareRows.length,
      hasExitTag: el.textContent.includes("已退出"),
      hasLineRisk: el.textContent.includes("业务连续性风险"),
      hasNewConc: el.textContent.includes("二次集中度风险"),
      mitigationRows: mitigRows.length,
      mitigationOk: el.textContent.includes("全部达标") || el.textContent.includes("缓释措施达标"),
      verdict: (() => {
        const m = el.textContent.match(/[⛔⚠✓—]\s*(.{0,30})/);
        return m ? m[1].trim().slice(0, 40) : "";
      })(),
    };
  });
  await p.screenshot({ path: path.join(OUT, "t3_sim_result.png"), fullPage: true });

  // ================= 4. 手动改为"退出 S001"，验证断档/集中度警示 =================
  const forced = await p.evaluate(() => {
    const sels = [...document.querySelectorAll("select.sim-sel")];
    let changed = false;
    sels.forEach(s => { s.value = "exit"; s.dispatchEvent(new Event("change")); changed = true; });
    return changed;
  });
  await sleep(500);
  R.forceExit = await p.evaluate(() => {
    const b = document.getElementById("simRun");
    if (b) b.click();
    return true;
  });
  await sleep(2600);
  R.exitResult = await p.evaluate(() => {
    const el = document.getElementById("simResult");
    if (!el) return { error: "no #simResult" };
    return {
      hasLineRisk: el.textContent.includes("业务连续性风险"),
      hasNewConc: el.textContent.includes("二次集中度风险"),
      verdictText: (() => { const m = el.textContent.match(/[⛔⚠✓—]\s*(.{0,40})/); return m ? m[1].trim() : ""; })(),
      mitigationFail: el.textContent.includes("未达标"),
      shareRows: el.querySelectorAll(".conc-share-row").length,
    };
  });
  await p.screenshot({ path: path.join(OUT, "t4_sim_exit_all.png"), fullPage: true });

  R.consoleErrors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
