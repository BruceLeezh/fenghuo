// E2E 验证：服务水平批量粘贴导入（第25条）
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
  await p.evaluate(() => {
    const t = [...document.querySelectorAll("button")].find(e => (e.textContent || "").includes("周期监控"));
    if (t) t.click();
  });
  await sleep(800);

  // 进入服务水平页
  await p.evaluate(() => {
    const t = [...document.querySelectorAll("#pageMonitor .subnav button")]
      .find(e => (e.textContent || "").includes("服务水平"));
    if (t) t.click();
  });
  await sleep(2200);

  // 打开批量导入
  R.openBatch = await p.evaluate(() => {
    const b = [...document.querySelectorAll("#pane-servicelevel button")].find(x => (x.textContent || "").includes("批量粘贴导入"));
    if (b) { b.click(); return true; }
    return false;
  });
  await sleep(600);

  R.mask = await p.evaluate(() => {
    const m = document.getElementById("slBatchMask");
    if (!m) return { error: "no mask" };
    return {
      hasSupplierSelect: !!document.getElementById("slbSupplier"),
      supplierCount: document.getElementById("slbSupplier").options.length,
      hasTextarea: !!document.getElementById("slbText"),
      parseDisabled: document.getElementById("slbParse").disabled,
      submitDisabled: document.getElementById("slbSubmit").disabled,
    };
  });
  await p.screenshot({ path: path.join(OUT, "b1_batch_mask.png"), fullPage: false });

  // 粘贴 TSV（含 12 列指标，测试别名识别）
  const tsv = [
    "期间\t可用率\t故障解决率\t客户满意度\t需求及时完成率\t缺陷数\t变更率\t人员饱和率\t考核合格率\t漏洞修复率\tRTO达标率\t演练完成率",
    "2026-10\t99.6%\t96%\t4.5\t96%\t2\t7%\t92%\t96%\t96%\t98%\t100%",
    "2026-11\t99.4%\t91%\t4.1\t93%\t5\t9%\t86%\t91%\t91%\t95%\t100%",
  ].join("\n");
  R.fillText = await p.evaluate((txt) => {
    const ta = document.getElementById("slbText");
    ta.value = txt;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    return ta.value.length;
  }, tsv);

  // 解析
  await p.evaluate(() => document.getElementById("slbParse").click());
  await sleep(1400);

  R.preview = await p.evaluate(() => {
    const box = document.getElementById("slbPreview");
    const hint = document.getElementById("slbHint");
    if (!box) return { error: "no preview" };
    const ths = [...box.querySelectorAll("thead th")].map(th => th.textContent.trim());
    const rows = [...box.querySelectorAll("tbody tr")];
    const tags = [...box.querySelectorAll(".sl-grade")].map(t => t.textContent.trim());
    return {
      visible: box.style.display !== "none",
      hint: hint.textContent,
      headerCols: ths.length,
      headerText: ths.join(" | "),
      rowCount: rows.length,
      rowTags: tags,
      firstRowCells: rows[0] ? rows[0].querySelectorAll("td").length : 0,
      hasUnknown: box.textContent.includes("未识别的列名"),
      submitEnabled: !document.getElementById("slbSubmit").disabled,
    };
  });
  await p.screenshot({ path: path.join(OUT, "b2_batch_preview.png"), fullPage: false });

  // 提交
  R.submit = await p.evaluate(() => {
    const b = document.getElementById("slbSubmit");
    if (b && !b.disabled) { b.click(); return true; }
    return false;
  });
  await sleep(2600);
  R.afterSubmit = await p.evaluate(() => {
    const hint = document.getElementById("slbHint");
    const mask = document.getElementById("slBatchMask");
    return {
      hint: hint ? hint.textContent : "",
      maskStillOpen: !!mask,
    };
  });
  await p.screenshot({ path: path.join(OUT, "b3_batch_after.png"), fullPage: true });

  R.consoleErrors = errors;
  console.log(JSON.stringify(R, null, 2));
  await browser.close();
})().catch((e) => { console.error("FAIL", e.message); process.exit(1); });
