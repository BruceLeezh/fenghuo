// E2E 验证：台账批量导入（Excel/Word/PDF）
const { chromium } = require("playwright-core");
const path = require("path");

const BASE = "http://127.0.0.1:8000";
const OUT = path.resolve(__dirname, "..", "..", "_shots");
const XLSX = path.join(OUT, "_test_import.xlsx");

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
  await p.evaluate(() => {
    const t = [...document.querySelectorAll("button")].find(e => (e.textContent || "").includes("周期监控"));
    if (t) t.click();
  });
  await sleep(800);
  await p.evaluate(() => {
    const t = [...document.querySelectorAll("#pageMonitor .subnav button")]
      .find(e => (e.textContent || "").includes("供应商台账"));
    if (t) t.click();
  });
  await sleep(1600);

  // 打开导入弹层
  R.openImport = await p.evaluate(() => {
    const b = [...document.querySelectorAll("#pane-roster button")].find(x => (x.textContent || "").includes("导入文件"));
    if (b) { b.click(); return true; }
    return false;
  });
  await sleep(500);
  R.mask = await p.evaluate(() => {
    const m = document.getElementById("rosterImportMask");
    return {
      hasFileInput: !!document.getElementById("riFile"),
      importDisabled: document.getElementById("riImport").disabled,
    };
  });

  // 上传 Excel
  await p.setInputFiles("#riFile", XLSX);
  await sleep(2000);

  R.preview = await p.evaluate(() => {
    const box = document.getElementById("riPreview");
    const rows = [...box.querySelectorAll("tbody tr")];
    return {
      previewText: box.textContent.replace(/\s+/g, " ").trim().slice(0, 160),
      rowCount: rows.length,
      importEnabled: !document.getElementById("riImport").disabled,
    };
  });
  await p.screenshot({ path: path.join(OUT, "r1_import_preview.png"), fullPage: false });

  // 确认导入
  R.commit = await p.evaluate(() => {
    const b = document.getElementById("riImport");
    if (b && !b.disabled) { b.click(); return true; }
    return false;
  });
  await sleep(2000);

  R.afterImport = await p.evaluate(() => {
    const area = document.getElementById("rosterArea");
    return {
      maskClosed: !document.getElementById("rosterImportMask"),
      rosterHasHikvision: area.textContent.includes("海康威视"),
      rosterHasHand: area.textContent.includes("汉得信息"),
      rowCount: area.querySelectorAll("table tbody tr, .roster-row").length,
    };
  });
  await p.screenshot({ path: path.join(OUT, "r2_import_after.png"), fullPage: false });

  R.consoleErrors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
