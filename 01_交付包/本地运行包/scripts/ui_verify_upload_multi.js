// E2E 验证：涉密文件分析 —— 多文件合并（单次一家 · 可多文件）
const { chromium } = require("playwright-core");
const path = require("path");

const BASE = "http://127.0.0.1:8000";
const OUT = path.resolve(__dirname, "..", "..", "_shots");
const FILES = [
  path.join(OUT, "_t_fin.xlsx"),
  path.join(OUT, "_t_legal.docx"),
  path.join(OUT, "_t_mark.pdf"),
];

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

  // 1. fileInput 支持 multiple + 页面备注文案 + 关联台账下拉已就绪
  R.multipleAttr = await p.evaluate(() => {
    const inp = document.getElementById("fileInput");
    const zone = document.getElementById("dropZone");
    const sel = document.getElementById("uploadRosterSel");
    return {
      multiple: inp ? inp.multiple : null,
      found: !!inp,
      zoneText: (zone ? zone.innerText : "").replace(/\s+/g, " ").trim(),
      rosterOptions: sel ? sel.options.length : 0,   // 应含"不关联"+7家台账
      hasRosterSel: !!sel,
    };
  });

  // 1b. 动态选取「有履约数据」的台账主体 → 履约表现维度应并入
  //     ⚠️ 不写死 S002：台账为 37 家，写死 ID 之后若该主体无履约数据，
  //        linked_roster 恒为 null（server.py 要求 performance.score 非空）。
  R.pickMeta = await p.evaluate(async () => {
    const r = await fetch("/api/servicelevel", { cache: "no-store" });
    const d = await r.json();
    const rows = d.rows || d.suppliers || d.items || [];
    const hit = (rows || []).find(x => x && (x.id || x.supplier_id) && x.score != null);
    const any = hit || (rows || [])[0];
    return any ? { id: any.id || any.supplier_id, name: any.name || "" } : null;
  });
  R.rosterPicked = await p.evaluate((id) => {
    const sel = document.getElementById("uploadRosterSel");
    if (!sel || !id) return null;
    const opt = [...sel.options].find(o => o.value === id);
    if (opt) { sel.value = id; sel.dispatchEvent(new Event("change", { bubbles: true })); }
    return opt ? opt.textContent.trim() : null;
  }, R.pickMeta && R.pickMeta.id);

  // 2. 一次上传 3 个文件（同一家）
  await p.setInputFiles("#fileInput", FILES);
  await sleep(4500);

  // 3. 合并视图：文件贡献清单 + 合并结果 + 六维完整度
  R.mergedView = await p.evaluate((pickId) => {
    const rows = [...document.querySelectorAll(".uf-row")];
    const chips = rows.map(r => [...r.querySelectorAll(".uf-chip")].map(c => c.textContent.trim()));
    const names = rows.map(r => (r.querySelector(".uf-name") || {}).textContent || "");
    const head = (document.querySelector("#result .card.pad") || {}).innerText || "";
    const body = document.getElementById("uploadResultBody");
    const bodyText = body ? body.innerText : "";
    const naDims = [...document.querySelectorAll(".dim-block")].filter(d =>
      d.innerText.includes("无信息") || d.innerText.includes("未录入"));
    return {
      fileRowCount: rows.length,
      fileNames: names,
      fileChips: chips,
      hasMergedBadge: head.includes("已合并为一份档案"),
      supplierNameShown: head.includes("北京华信永创科技有限公司"),
      summaryText: head.includes("3 个文件"),
      linkedBadge: !!pickId && (head.includes("已关联台账 " + pickId) || head.includes("已关联台账" + pickId)),
      hasRedLine: bodyText.includes("被执行") || bodyText.includes("红线"),
      level: bodyText.includes("高危") ? "red" : bodyText.includes("关注") ? "yellow" : "green",
      naDimCount: naDims.length,
      naDimText: naDims.map(d => (d.querySelector(".dim-title") || {}).textContent).filter(Boolean),
      noFabricatedScore: bodyText.includes("不臆造分数") || bodyText.includes("已按比例分摊") || bodyText.includes("无信息"),
    };
  }, R.pickMeta && R.pickMeta.id);

  await p.screenshot({ path: path.join(OUT, "upload_merged.png"), fullPage: false });

  R.consoleErrors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();

  const mv = R.mergedView || {};
  const ok = R.multipleAttr.multiple === true
    && R.multipleAttr.zoneText.includes("单次一家")
    && R.multipleAttr.hasRosterSel
    && R.multipleAttr.rosterOptions >= 8      // "不关联" + 7 家台账
    && mv.fileRowCount === 3
    && mv.hasMergedBadge && mv.supplierNameShown
    // 解析器补齐后：财务 + 工商 + 股东 + 司法 + 舆情 五块数据都能从文件提出
    && mv.fileChips.some(c => c.includes("财务指标"))
    && mv.fileChips.some(c => c.includes("工商信息"))
    && mv.fileChips.some(c => c.includes("股东结构"))
    && mv.fileChips.some(c => c.includes("司法事件"))
    && mv.fileChips.some(c => c.includes("舆情资讯"))
    && mv.fileChips.some(c => c.includes("外包标记"))
    && mv.hasRedLine
    // 关联台账 S002 后：履约表现并入 → 六维完整，不应再有 na 维度
    && mv.linkedBadge
    && mv.naDimCount === 0
    && errors.length === 0;
  console.log(ok ? "\n✅ E2E 全部通过" : "\n❌ 存在未通过项");
  process.exit(ok ? 0 : 1);
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
