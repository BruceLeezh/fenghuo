// E2E 验证：治理面只读回归（合同条款 / 预警分页与闭环 / 服务水平批量导入防串单 /
//             外包专项快照 / 沙盘旧结果失效 / 工单上下文不串单）
//
// 设计原则：**全程只读**。凡会写数据的动作（提交批量、改预警状态、建工单）要么被
// 前置校验拦住、要么在提交前取消，最后再断言数据未被改动（台账条数/工单条数不变）。
//
// 依赖：python server.py（默认 127.0.0.1:8000）
const { chromium } = require("playwright-core");

const BASE = process.env.FH_BASE || "http://127.0.0.1:8000";

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1560, height: 1200 } });
  const p = await ctx.newPage();
  const errors = [];
  p.on("dialog", async (d) => { try { await d.accept(""); } catch (e) {} });
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  p.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  let pass = 0, fail = 0;
  const check = (k, ok, note) => {
    if (ok) { pass++; console.log(`   OK   ${k}${note ? "  — " + note : ""}`); }
    else { fail++; console.log(`   FAIL ${k}${note ? "  — " + note : ""}`); }
  };

  // 记录写请求（用于证明"防串单"确实没发出提交）
  const writes = [];
  p.on("request", (req) => {
    const u = req.url();
    if (req.method() === "POST" && /\/api\/(servicelevel\/batch|workorders|alerts\/state)/.test(u)) {
      writes.push(u.replace(BASE, ""));
    }
  });

  await p.goto(BASE, { waitUntil: "domcontentloaded" });
  await p.waitForTimeout(1200);

  // ---------- A. 合同条款：未填不得判"齐备" ----------
  const ct = await p.evaluate(async () => {
    const r = await fetch("/api/contract");
    const d = await r.json();
    const rows = d.rows || [];
    return {
      total: d.total,
      byLevel: d.by_level,
      bad: rows.filter(x => (x.unanswered_count || 0) > 0 && x.level === "none").length,
      emptyFull: rows.filter(x => x.has_contract && x.answered_count === 0 && x.level === "none").length,
      fields: rows.length ? ("answered_count" in rows[0] && "unanswered_count" in rows[0]) : false,
      unknownSummaries: rows.filter(x => x.level === "unknown").map(x => x.summary || ""),
      unknownNoText: rows.filter(x => x.level === "unknown" && !(x.summary || "").trim()).length,
    };
  });
  check("A1 合同评价返回 answered/unanswered 计数", ct.fields === true, JSON.stringify(ct.byLevel));
  check("A2 有未填项却判「齐备」的行数 = 0", ct.bad === 0 && ct.emptyFull === 0);
  check("A3 unknown 行都带明确解释（未填写/未登记），无空文案",
        ct.unknownNoText === 0 && ct.unknownSummaries.length > 0,
        (ct.unknownSummaries[0] || "").slice(0, 60));

  // ---------- B. 预警分页 ----------
  await p.evaluate(() => { location.hash = "#/monitor/alerts"; });
  await p.waitForTimeout(1200);
  await p.evaluate(() => loadAlerts(true));
  await p.waitForTimeout(2500);

  const pg1 = await p.evaluate(() => {
    const el = document.getElementById("alertArea");
    const rows = el.querySelectorAll("select.mini-btn").length;      // 每行一个状态下拉
    const txt = el.textContent || "";
    const prev = [...el.querySelectorAll("button")].find(b => b.textContent.includes("上一页"));
    return { rows, hasPager: /第 1\/\d+ 页/.test(txt), prevDisabled: prev ? prev.disabled : null,
             first: (el.querySelector(".card div[style*='font-weight:600']") || {}).textContent || "" };
  });
  check("B1 单页最多 20 条", pg1.rows > 0 && pg1.rows <= 20, `本页 ${pg1.rows} 条`);
  check("B2 分页控件存在且第 1 页「上一页」禁用", pg1.hasPager && pg1.prevDisabled === true);

  const f1 = await p.evaluate(() => (_alertRows[0] || {}).key || "");
  await p.evaluate(() => setAlertPage(1));
  await p.waitForTimeout(600);
  const pg2 = await p.evaluate(() => {
    const el = document.getElementById("alertArea");
    return { rows: el.querySelectorAll("select.mini-btn").length,
             text: (el.textContent.match(/第 \d+\/\d+ 页/) || [""])[0] };
  });
  const f2 = await p.evaluate(() => (_alertRows[20] || {}).key || "");
  check("B3 翻页后行内容变化（页码与数据同步）",
        pg2.text.indexOf("第 2/") >= 0 && f2 && f1 !== f2, `${pg2.text} | ${pg2.rows} 条`);

  const backTo1 = await p.evaluate(() => {
    setAlertFilter("level", "high");     // 换筛选条件必须回到第 1 页
    return _alertPage;
  });
  check("B4 更换筛选条件后回到第 1 页", backTo1 === 0);
  await p.evaluate(() => setAlertFilter("level", ""));
  await p.waitForTimeout(400);

  // 分页后点击索引仍指向正确行（不改状态：只校验 _alertRows 下标可用）
  const idxOk = await p.evaluate(() => {
    const n = document.querySelectorAll("#alertArea select.mini-btn").length;
    if (!n || !_alertRows.length) return false;
    const sel = document.querySelectorAll("#alertArea select.mini-btn")[0];
    const m = (sel.getAttribute("onchange") || "").match(/setAlertState\((\d+)/);
    const i = m ? parseInt(m[1], 10) : -1;
    return i >= 0 && i < _alertRows.length;
  });
  check("B5 行内控件下标落在全量过滤结果范围内（防点错行）", idxOk);

  // ---------- C. 服务水平批量导入：目标变更必须拒绝提交 ----------
  await p.evaluate(() => { location.hash = "#/monitor/servicelevel"; });
  await p.waitForTimeout(800);
  await p.evaluate(() => loadServiceLevel(true));
  await p.waitForTimeout(2500);
  await p.evaluate(() => openSLBatch());
  await p.waitForTimeout(400);

  const cRes = await p.evaluate(async () => {
    const sel = document.getElementById("slbSupplier");
    const opts = [...sel.options].map(o => o.value);
    if (opts.length < 2) return { skipped: true, opts: opts.length };
    sel.value = opts[0];
    document.getElementById("slbText").value =
      "期间\t系统可用率\t故障次数\n2026-01\t99.90\t2\n2026-02\t99.80\t3";
    document.getElementById("slbParse").click();
    await new Promise(r => setTimeout(r, 2500));
    const parsed = document.getElementById("slbHint").textContent || "";
    sel.value = opts[1];                      // 解析后切到另一家
    document.getElementById("slbSubmit").click();
    await new Promise(r => setTimeout(r, 600));
    const hint = document.getElementById("slbHint").textContent || "";
    const disabled = document.getElementById("slbSubmit").disabled;
    return { parsed, hint, disabled, opts: opts.length };
  });
  if (cRes.skipped) {
    check("C1 服务水平批量导入防串单", false, `台账选项不足（${cRes.opts}）`);
  } else {
    check("C1 解析成功", (cRes.parsed || "").indexOf("解析完成") >= 0, (cRes.parsed || "").slice(0, 40));
    check("C2 目标供应商变更后被拒绝提交并提示重新解析",
          (cRes.hint || "").indexOf("已变更") >= 0 && cRes.disabled !== true, cRes.hint);
  }
  await p.evaluate(() => { const m = document.getElementById("slBatchMask"); if (m) m.remove(); });

  // ---------- D. 外包专项：离线快照可读、不 500 ----------
  // S001 有随包离线快照（data/special_records/S001.json）：实时源不可用时必须优雅降级，
  // 而不是 500 或空白。S035 没有快照，用于验证"无数据时给明确原因"。
  const ospOne = async (name, sid) => p.evaluate(async ([nm, id]) => {
    const r = await fetch("/api/outsourcing/special", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: nm, supplier_id: id, refresh: false }),
    });
    const d = await r.json().catch(() => ({}));
    return { status: r.status, ok: d.ok, source: d.source, offline: d.offline,
             error: d.error || "", hasAnalysis: !!d.analysis,
             records: ((d.analysis || {}).record_count || 0) };
  }, [name, sid]);

  const osp = await ospOne("北京科蓝软件系统股份有限公司", "S001");
  check("D1 有离线快照的供应商：200 且给出分析结果", osp.status === 200 && osp.hasAnalysis,
        `source=${osp.source} offline=${osp.offline} records=${osp.records}`);

  const osp2 = await ospOne("上海中软华腾软件系统有限公司", "S035");
  check("D2 无快照且实时源不可用：200 + 明确原因（不是 500/空白）",
        osp2.status === 200 && !osp2.ok && !!osp2.error, (osp2.error || "").slice(0, 60));

  // ---------- E. 沙盘：改决策后旧结果必须清空 ----------
  await p.evaluate(() => { location.hash = "#/monitor/scenario"; });
  await p.waitForTimeout(600);
  await p.evaluate(() => { location.hash = "#/monitor/simulator"; });
  await p.waitForTimeout(1200);
  await p.evaluate(() => loadSimulator());
  await p.waitForTimeout(2500);
  await p.evaluate(() => runSimulate());
  await p.waitForTimeout(6000);
  const sim1 = await p.evaluate(() => ({
    hasResult: (document.getElementById("simResult") || {}).innerHTML?.indexOf("缓释措施") >= 0,
    selects: document.querySelectorAll("select.sim-sel").length,
  }));
  let sim2 = { cleared: false };
  if (sim1.selects > 0) {
    sim2 = await p.evaluate(async () => {
      const sel = document.querySelector("select.sim-sel");
      sel.value = sel.value === "exit" ? "hold" : "exit";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      await new Promise(r => setTimeout(r, 500));
      const t = (document.getElementById("simResult") || {}).textContent || "";
      return { cleared: t.indexOf("决策已变更") >= 0 };
    });
  }
  check("E1 沙盘能产出推演结果", sim1.hasResult === true, `可选动作 ${sim1.selects}`);
  check("E2 决策变更后旧结果被清空并提示重新推演", sim2.cleared === true);

  // ---------- F. 工单：无归属供应商时不得默认落到第一家 ----------
  await p.evaluate(() => openWorkOrderForm({}));
  await p.waitForTimeout(500);
  const wo1 = await p.evaluate(() => {
    const s = document.getElementById("woSupplier");
    return { value: s ? s.value : null, text: s && s.options[0] ? s.options[0].textContent : "" };
  });
  await p.evaluate(() => { const b = document.getElementById("woCancel"); if (b) b.click(); });
  await p.waitForTimeout(300);

  await p.evaluate(() => openWorkOrderForm({ supplier: "北京捷科智诚科技有限公司" }));
  await p.waitForTimeout(500);
  const wo2 = await p.evaluate(() => (document.getElementById("woSupplier") || {}).value || "");
  await p.evaluate(() => { const b = document.getElementById("woCancel"); if (b) b.click(); });

  check("F1 无归属供应商时要求显式选择（不默认第一家）",
        wo1.value === "" && (wo1.text || "").indexOf("请选择") >= 0, `value=${JSON.stringify(wo1.value)}`);
  check("F2 指定供应商时正确预选", wo2 === "北京捷科智诚科技有限公司", wo2);

  // ---------- G. 全程未发生写请求 ----------
  check("G1 全程零写请求（用例设计为只读）", writes.length === 0, writes.join(" ; "));

  const bizErrors = errors.filter(t => t.indexOf("云服务 SDK 加载失败") < 0
                                    && t.indexOf("net::ERR") < 0
                                    && t.indexOf("[fh-cloud]") < 0);
  check("H1 无页面脚本错误", bizErrors.length === 0, bizErrors.slice(0, 2).join(" ; "));

  await browser.close();
  console.log(`\n结果：${fail === 0 ? "✅ 全部通过" : "❌ 存在失败"}   通过 ${pass} / ${pass + fail}`);
  process.exit(fail === 0 ? 0 : 1);
})();
