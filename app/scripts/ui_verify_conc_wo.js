/**
 * 集中度风险 + 处置工单 两个新标签页的 E2E 验证
 */
const { chromium } = require("playwright-core");
const path = require("path");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
// 截图统一落到 _shots/（与其余 E2E 一致），不写死机器相关绝对路径
const OUT = path.resolve(__dirname, "..", "..", "_shots");

const waitIdle = async (p, ms = 120000) => {
  for (let i = 0; i < ms / 400; i++) {
    await p.waitForTimeout(400);
    const busy = await p.evaluate(() => {
      const m = document.getElementById("loadingMask");
      return !!m && m.style.display !== "none";
    });
    if (!busy) return true;
  }
  return false;
};

(async () => {
  const b = await chromium.launch({ executablePath: EDGE, headless: true });
  const ctx = await b.newContext({ viewport: { width: 1500, height: 1300 }, deviceScaleFactor: 1.3 });
  const p = await ctx.newPage();
  const errs = [];
  p.on("console", m => { if (m.type() === "error") errs.push(m.text()); });
  p.on("pageerror", e => errs.push("pageerror: " + e.message));

  // 捕获 alert / confirm / prompt，避免卡住
  p.on("dialog", async d => {
    if (d.type() === "prompt") await d.accept("E2E自动填写：已按监管要求推动整改");
    else await d.accept();
  });

  await p.goto("http://127.0.0.1:8000/#/monitor/concentration", { waitUntil: "domcontentloaded" });  // 首页改版后：深链直达集中度 pane
  await waitIdle(p, 40000);
  await p.waitForTimeout(1500);

  const out = {};

  // ---------- A. 集中度风险 ----------
  await p.evaluate(() => {
    document.querySelectorAll("#pageMonitor .subnav button").forEach(b => {
      if (b.innerText.includes("集中度风险")) b.click();
    });
  });
  await waitIdle(p, 30000);
  await p.waitForTimeout(1500);

  out.concentration = await p.evaluate(() => {
    const area = document.getElementById("conArea");
    const t = area.innerText;
    return {
      paneActive: document.getElementById("pane-concentration").classList.contains("active"),
      statCards: area.querySelectorAll(".stat-card").length,
      有HHI: t.includes("HHI") && t.includes("赫芬达尔"),
      HHI值: (t.match(/(\d+\.?\d*)\s*\n\s*HHI/) || [])[1] || null,
      有单供应商占比: t.includes("单供应商占比"),
      有集团合并: t.includes("集团 / 关联方合并"),
      有业务条线: t.includes("业务条线单点依赖"),
      有预警: t.includes("集中度风险预警"),
      供应商条数: area.querySelectorAll(".conc-share-row").length,
      阈值线数: area.querySelectorAll(".conc-thresh").length,
      业务线行数: area.querySelectorAll("table.grid tbody tr").length,
      未维护金额告警: t.includes("未维护外包金额"),
    };
  });
  await p.screenshot({ path: path.join(OUT, "_n1_concentration.png"), fullPage: true });

  // ---------- B. 处置工单 ----------
  await p.evaluate(() => {
    document.querySelectorAll("#pageMonitor .subnav button").forEach(b => {
      if (b.innerText.includes("处置工单")) b.click();
    });
  });
  await waitIdle(p, 30000);
  await p.waitForTimeout(1200);

  out.workorder_empty = await p.evaluate(() => {
    const area = document.getElementById("woArea");
    return {
      paneActive: document.getElementById("pane-workorder").classList.contains("active"),
      statCards: area.querySelectorAll(".stat-card").length,
      isEmpty: area.innerText.includes("暂无处置工单"),
      hasAddBtn: !!document.querySelector("button[onclick='openWorkOrderForm()']"),
    };
  });

  // ---- 新建工单（通过台账的「建工单」快捷方式，走真实评分） ----
  await p.evaluate(() => {
    document.querySelectorAll("#pageMonitor .subnav button").forEach(b => {
      if (b.innerText.includes("供应商台账")) b.click();
    });
  });
  await p.waitForTimeout(1000);

  const quickBtns = await p.$$("#rosterArea .mini-btn");
  let clicked = false;
  for (const btn of quickBtns) {
    const txt = await btn.innerText();
    if (txt.trim() === "建工单") { await btn.click(); clicked = true; break; }
  }
  out.clicked_quick = clicked;

  // 等待评分 + 表单
  let formOpen = false;
  for (let i = 0; i < 400; i++) {
    await p.waitForTimeout(500);
    if (await p.evaluate(() => !!document.getElementById("woFormMask"))) { formOpen = true; break; }
  }
  out.workorder_form = { formOpen };
  if (formOpen) {
    await p.waitForTimeout(800);
    out.workorder_form.fields = await p.evaluate(() => ({
      supplier: (document.getElementById("woSupplier") || {}).value || "",
      type: (document.getElementById("woType") || {}).value || "",
      trigger: ((document.getElementById("woTrigger") || {}).value || "").slice(0, 120),
      measuresLen: ((document.getElementById("woMeasures") || {}).value || "").length,
      owner: !!document.getElementById("woOwner"),
      clause: (document.getElementById("woClause") || {}).value || "",
      suggestBtn: !!document.getElementById("woSuggest"),
    }));
    await p.screenshot({ path: path.join(OUT, "_n2_wo_form.png") });

    // 填责任人并保存
    await p.fill("#woOwner", "张风控");
    await p.click("#woSave");
    await waitIdle(p, 30000);
    await p.waitForTimeout(1200);
  }

  out.workorder_created = await p.evaluate(() => {
    const area = document.getElementById("woArea");
    const cards = area.querySelectorAll(".wo-card");
    return {
      cards: cards.length,
      firstCard: cards[0] ? cards[0].innerText.replace(/\n+/g, " | ").slice(0, 340) : null,
      flowNodes: area.querySelectorAll(".flow-node").length,
      hasFlow: area.innerText.includes("待处理") && area.innerText.includes("已闭环"),
    };
  });
  await p.screenshot({ path: path.join(OUT, "_n3_wo_created.png"), fullPage: true });

  // ---- 状态流转：待处理 → 处置中 ----
  if (out.workorder_created.cards > 0) {
    await p.evaluate(() => {
      const btn = [...document.querySelectorAll("#woArea .mini-btn")].find(b => b.innerText.includes("处置中"));
      if (btn) btn.click();
    });
    await waitIdle(p, 30000);
    await p.waitForTimeout(1200);
    out.workorder_advanced = await p.evaluate(() => {
      const area = document.getElementById("woArea");
      const c = area.querySelector(".wo-card");
      return {
        cardClass: c ? c.className : "",
        statusText: c ? (c.innerText.match(/待处理|处置中|待验证|已升级|已闭环/) || [])[0] : null,
        hasOverdue: area.innerText.includes("超期"),
      };
    });

    // ---- 查看留痕 ----
    await p.evaluate(() => {
      const btn = [...document.querySelectorAll("#woArea .mini-btn")].find(b => b.innerText.includes("查看留痕"));
      if (btn) btn.click();
    });
    await p.waitForTimeout(800);
    out.workorder_timeline = await p.evaluate(() => {
      const tl = document.querySelector("#woArea .timeline");
      return {
        visible: tl ? getComputedStyle(tl.parentElement).display !== "none" : false,
        items: document.querySelectorAll("#woArea .tl-item").length,
        text: tl ? tl.innerText.replace(/\n+/g, " | ").slice(0, 260) : null,
      };
    });
    await p.screenshot({ path: path.join(OUT, "_n4_wo_timeline.png"), fullPage: true });
  }

  // ---------- C. 总览卡同步 ----------
  out.monStats = await p.evaluate(() =>
    document.getElementById("monStats").innerText.replace(/\n+/g, " | ").slice(0, 420));

  // ---------- D. 接口层：验证工单状态机 ----------
  out.api = {};
  try {
    const r1 = await p.evaluate(async () => {
      const r = await fetch("/api/workorders");
      return await r.json();
    });
    out.api.listCount = (r1.items || []).length;
    if (out.api.listCount) {
      const id = r1.items[0].id;
      // 非法跳步：处置中 → 已闭环 应被拒
      const bad = await p.evaluate(async (wid) => {
        const r = await fetch("/api/workorders/" + encodeURIComponent(wid) + "/advance", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ to_status: "已闭环", note: "跳步测试", by: "E2E" })
        });
        return { status: r.status, body: await r.json() };
      }, id);
      out.api.illegalAdvance = { status: bad.status, detail: (bad.body.detail || "").slice(0, 90) };
    }
  } catch (e) { out.api.err = e.message; }

  console.log("=== 集中度 + 工单 E2E ===");
  console.log(JSON.stringify(out, null, 2));
  console.log("console errors:", errs.length ? errs.slice(0, 8) : "none");
  await b.close();
})();
