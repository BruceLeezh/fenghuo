/**
 * 周期监控中心 E2E 验证
 * 覆盖：页面切换 / 组合看板 / 批量扫描 / 台账 / 合规日历 / 监管报送
 */
const { chromium } = require("playwright-core");
const fs = require("fs");
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
  const ctx = await b.newContext({
    viewport: { width: 1500, height: 1300 }, deviceScaleFactor: 1.3, acceptDownloads: true,
  });
  const p = await ctx.newPage();
  const errs = [];
  p.on("console", m => { if (m.type() === "error") errs.push(m.text()); });
  p.on("pageerror", e => errs.push("pageerror: " + e.message));

  await p.goto("http://127.0.0.1:8000/#/monitor", { waitUntil: "domcontentloaded" });  // 首页改版后：深链直达周期监控
  await p.waitForTimeout(1500);

  const out = {};

  // ---------- 1. 已在周期监控（路由深链直达，等价于原「点 #navMonitor」） ----------
  await waitIdle(p, 30000);
  await p.waitForTimeout(1200);
  out.step1_switch = await p.evaluate(() => ({
    monitorVisible: getComputedStyle(document.getElementById("pageMonitor")).display !== "none",
    scanHidden: getComputedStyle(document.getElementById("pageScan")).display === "none",
    navActive: document.getElementById("navMonitor").classList.contains("active"),
    statsCards: document.querySelectorAll("#monStats .stat-card").length,
    statsText: document.getElementById("monStats").innerText.replace(/\n+/g, " | ").slice(0, 260),
  }));
  await p.screenshot({ path: path.join(OUT, "_m1_dashboard.png"), fullPage: true });

  // ---------- 2. 台账 ----------
  await p.evaluate(() => {
    document.querySelectorAll("#pageMonitor .subnav button").forEach(b => {
      if (b.innerText.includes("供应商台账")) b.click();
    });
  });
  await p.waitForTimeout(1200);
  out.step2_roster = await p.evaluate(() => {
    const rows = document.querySelectorAll("#rosterArea table.grid tbody tr");
    return {
      paneActive: document.getElementById("pane-roster").classList.contains("active"),
      rows: rows.length,
      hasAddBtn: !!document.querySelector("button[onclick='openRosterForm(null)']"),
      firstRow: rows[0] ? rows[0].innerText.replace(/\n+/g, " | ").slice(0, 150) : null,
      editBtns: document.querySelectorAll("#rosterArea .mini-btn:not(.danger)").length,
      delBtns: document.querySelectorAll("#rosterArea .mini-btn.danger").length,
    };
  });

  // 打开新增表单并截图
  await p.click("button[onclick='openRosterForm(null)']");
  await p.waitForTimeout(600);
  out.step2b_form = await p.evaluate(() => ({
    formOpen: !!document.getElementById("rosterFormMask"),
    hasCats: (document.getElementById("rfCat") || {}).options ? document.getElementById("rfCat").options.length : 0,
    fields: ["rfName", "rfCat", "rfLines", "rfImp", "rfNon", "rfCross", "rfCs", "rfCe", "rfDd", "rfOn"]
      .filter(id => !!document.getElementById(id)).length,
  }));
  await p.screenshot({ path: path.join(OUT, "_m2_roster_form.png") });
  await p.click("#rfCancel");
  await p.waitForTimeout(400);

  // ---------- 3. 合规日历 ----------
  await p.evaluate(() => {
    document.querySelectorAll("#pageMonitor .subnav button").forEach(b => {
      if (b.innerText.includes("合规日历")) b.click();
    });
  });
  await waitIdle(p, 20000);
  await p.waitForTimeout(1200);
  out.step3_calendar = await p.evaluate(() => {
    const items = document.querySelectorAll("#calArea .todo-item");
    const groups = document.querySelectorAll("#calArea .todo-group");
    const overdue = document.querySelectorAll("#calArea .todo-item.u-overdue").length;
    return {
      paneActive: document.getElementById("pane-calendar").classList.contains("active"),
      todos: items.length,
      groups: groups.length,
      overdue,
      statCards: document.querySelectorAll("#calArea .stat-card").length,
      firstTodo: items[0] ? items[0].innerText.replace(/\n+/g, " | ").slice(0, 200) : null,
    };
  });
  await p.screenshot({ path: path.join(OUT, "_m3_calendar.png"), fullPage: true });

  // ---------- 4. 监管报送 ----------
  await p.evaluate(() => {
    document.querySelectorAll("#pageMonitor .subnav button").forEach(b => {
      if (b.innerText.includes("监管报送")) b.click();
    });
  });
  await waitIdle(p, 20000);
  await p.waitForTimeout(1200);
  out.step4_report = await p.evaluate(() => {
    const rows = document.querySelectorAll("#reportArea table.grid tbody tr");
    return {
      paneActive: document.getElementById("pane-report").classList.contains("active"),
      rows: rows.length,
      needCount: document.querySelectorAll("#reportArea .chip.red").length,
      genBtns: document.querySelectorAll("#reportArea .mini-btn").length,
      firstRow: rows[0] ? rows[0].innerText.replace(/\n+/g, " | ").slice(0, 180) : null,
    };
  });
  await p.screenshot({ path: path.join(OUT, "_m4_report_list.png"), fullPage: true });

  // ---------- 5. 生成监管报告材料（真实拉数） ----------
  const t0 = Date.now();
  await p.evaluate(() => {
    const btns = document.querySelectorAll("#reportArea .mini-btn");
    if (btns.length) btns[0].click();
  });
  let modalOk = false;
  for (let i = 0; i < 300; i++) {
    await p.waitForTimeout(500);
    if (await p.evaluate(() => !!document.getElementById("regRepMask"))) { modalOk = true; break; }
    const candOpen = await p.evaluate(() => !!document.getElementById("candMask"));
    if (candOpen) { out.step5_candPick = true; await p.keyboard.press("Escape"); break; }
  }
  out.step5_regreport = { modalOk, seconds: ((Date.now() - t0) / 1000).toFixed(2) };
  if (modalOk) {
    out.step5_regreport.content = await p.evaluate(() => {
      const m = document.getElementById("regRepMask");
      return {
        text: m.innerText.replace(/\n+/g, " | ").slice(0, 500),
        hasCountdown: !!m.querySelector(".cd-banner"),
        hasPrint: !!document.getElementById("rrPrint"),
        hasDownload: !!document.getElementById("rrDownload"),
      };
    });
    await p.screenshot({ path: path.join(OUT, "_m5_regreport_modal.png") });
    // 测试下载
    try {
      const [dl] = await Promise.all([
        p.waitForEvent("download", { timeout: 30000 }),
        p.click("#rrDownload"),
      ]);
      const path = await dl.path();
      out.step5_regreport.download = `${dl.suggestedFilename()} (${path ? fs.statSync(path).size : 0} bytes)`;
    } catch (e) { out.step5_regreport.download = "FAILED: " + e.message.split("\n")[0]; }
    await p.evaluate(() => document.getElementById("regRepMask").remove());
    await p.waitForTimeout(400);
  }

  // ---------- 6. 批量扫描（最重） ----------
  await p.evaluate(() => {
    document.querySelectorAll("#pageMonitor .subnav button").forEach(b => {
      // 看板曾更名（组合风险看板 → 风险预警看板），用正则兼容两代文案
      if (/看板/.test(b.innerText) || b.innerText.includes("组合")) b.click();
    });
  });
  await p.waitForTimeout(600);
  const ts = Date.now();
  // 显式等按钮可见再点：若看板未切换成功，这里的报错能直接指出原因
  await p.waitForSelector("button[onclick='runScan()']", { state: "visible", timeout: 20000 });
  await p.click("button[onclick='runScan()']");
  await waitIdle(p, 300000);
  await p.waitForTimeout(1500);
  out.step6_scan = {
    seconds: ((Date.now() - ts) / 1000).toFixed(2),
    ...(await p.evaluate(() => {
      const cards = document.querySelectorAll("#boardArea .port-card");
      const red = document.querySelectorAll("#boardArea .port-card.lv-red").length;
      const yellow = document.querySelectorAll("#boardArea .port-card.lv-yellow").length;
      const green = document.querySelectorAll("#boardArea .port-card.lv-green").length;
      return {
        cards: cards.length, red, yellow, green,
        hasAlert: !!document.querySelector("#boardArea .alert-strip"),
        alertText: (document.querySelector("#boardArea .alert-strip") || {}).innerText
          ? document.querySelector("#boardArea .alert-strip").innerText.replace(/\n+/g, " | ").slice(0, 320) : "none",
        firstCard: cards[0] ? cards[0].innerText.replace(/\n+/g, " | ").slice(0, 220) : null,
        dimBars: document.querySelectorAll("#boardArea .pc-dim").length,
      };
    })),
  };
  await p.screenshot({ path: path.join(OUT, "_m6_board.png"), fullPage: true });

  // ---------- 7. 统计卡是否同步更新 ----------
  out.step7_statsAfter = await p.evaluate(() =>
    document.getElementById("monStats").innerText.replace(/\n+/g, " | ").slice(0, 300));

  console.log("=== MONITOR E2E VERIFY ===");
  console.log(JSON.stringify(out, null, 2));
  console.log("console errors:", errs.length ? errs.slice(0, 8) : "none");
  await b.close();
})();
