/**
 * 生成初赛提交用的**展示级截图**（不是功能验证截图）。
 *
 * 与 ui_verify_*.js 的区别：那些是"断言是否工作"，本脚本只负责
 * 「用真实公开数据把系统最该被看到的画面截下来」，输出到
 * `初赛提交包/04_系统截图/`，命名带序号便于按顺序阅读。
 *
 * 前置：服务已在 http://127.0.0.1:8000 运行。
 * 用法：node scripts/make_submission_shots.js
 */
const { chromium } = require("playwright-core");
const path = require("path");
const fs = require("fs");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8000";
const OUT = path.resolve(__dirname, "..", "..", "初赛提交包", "04_系统截图");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: EDGE, headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1560, height: 1180 },
    deviceScaleFactor: 1.5,     // 高清，便于放进 PDF
  });
  const p = await ctx.newPage();
  const errors = [];
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  const shot = async (name, full = true) => {
    await p.screenshot({ path: path.join(OUT, name + ".png"), fullPage: full });
    console.log("  ✓ " + name);
  };

  // 点击「周期监控」下的子标签（前端统一是顶部按钮，用文案匹配）
  const clickTab = async (label) => {
    return p.evaluate((lb) => {
      const bs = [...document.querySelectorAll("button, .tab, [role=tab]")];
      const t = bs.find((b) => b.innerText && b.innerText.trim() === lb)
             || bs.find((b) => b.innerText && b.innerText.includes(lb));
      if (t) { t.click(); return true; }
      return false;
    }, label);
  };

  const waitResult = async (ms = 90000) => {
    for (let i = 0; i < ms / 400; i++) {
      await p.waitForTimeout(400);
      const done = await p.evaluate(() => {
        const mask = document.getElementById("loadingMask");
        const hidden = !mask || mask.style.display === "none" ||
                       getComputedStyle(mask).display === "none" ||
                       mask.classList.contains("hide");
        const r = document.getElementById("result");
        return hidden && r && r.innerHTML.length > 500;
      });
      if (done) return true;
    }
    return false;
  };

  try {
    // ---------- 01 首屏 ----------
    await p.goto(BASE, { waitUntil: "networkidle" });
    await sleep(900);
    await shot("01_首屏_单点扫描入口");

    // ---------- 02 单点扫描结果（真实公开企业） ----------
    await p.fill("#queryInput", "北京科蓝软件系统股份有限公司");
    await p.click("button.btn-primary");
    console.log("  等待单点扫描结果…");
    await waitResult();
    await sleep(1200);
    await shot("02_单点扫描_六维评分总览");

    // ---------- 03 评分规则与监管条款 ----------
    // 修复要点（两个）：
    //  (1) 原实现用 `e.children.length === 0 && /评分细则/` 找叶子节点，真实 DOM 下
    //      文字被包在多层容器里 → children 非 0 → find 返回 undefined → 页面不滚动。
    //  (2) 即使滚动了，`fullPage: true` 会忽略滚动位置、仍然截整页 → 03 与 02 内容
    //      重叠。评分细则位于页面下半部，此处改用 **视口截图**（fullPage=false）
    //      并向下多取几屏，拼成一张聚焦"规则挂接明细"的图。
    await p.evaluate(() => {
      document.querySelectorAll("[id^='dim-']").forEach((el) => el.classList.add("open"));
    });
    await sleep(600);
    const anchorY = await p.evaluate(() => {
      const all = [...document.querySelectorAll("div,section,h2,h3,p,span")];
      const hits = all.filter((e) => /六大维度评分细则/.test(e.innerText || ""));
      if (!hits.length) return null;
      hits.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
      const y = hits[0].getBoundingClientRect().top + window.scrollY - 16;
      window.scrollTo({ top: y, behavior: "instant" });
      return y;
    });
    console.log("  评分细则锚点 y =", anchorY);
    await sleep(800);
    // 视口截图：只截"规则与条款"这一段（约 3 屏）
    await p.screenshot({ path: path.join(OUT, "03_单点扫描_评分规则与监管条款.png") });
    console.log("  ✓ 03_单点扫描_评分规则与监管条款");

    // ---------- 周期监控 ----------
    await p.click("#navMonitor");
    await sleep(700);
    await clickTab("组合风险看板");
    await sleep(1200);
    await shot("04_周期监控_组合风险看板");

    await clickTab("集中度");
    await sleep(1000);
    await shot("05_周期监控_集中度HHI");

    await clickTab("专属场景");
    await sleep(1000);
    await shot("06_周期监控_专属场景权重迁移");

    await clickTab("服务水平");
    await sleep(1000);
    await shot("07_周期监控_服务水平时序");

    await clickTab("处置推演");
    await sleep(1000);
    await shot("08_周期监控_处置推演沙盘");

    await clickTab("合规日历");
    await sleep(900);
    await shot("09_周期监控_合规日历");

    await clickTab("处置工单");
    await sleep(900);
    await shot("10_周期监控_处置工单");

    await clickTab("监管报送");
    await sleep(900);
    await shot("11_周期监控_监管报送");

    await clickTab("供应商台账");
    await sleep(900);
    await shot("12_周期监控_供应商台账");

    // ---------- 涉密文件分析（在"单点风险扫描"页内，需回到该页并滚动到位） ----------
    await p.click("#navScan");
    await sleep(800);
    await p.evaluate(() => {
      const h = [...document.querySelectorAll("h2, h3")].find(
        (e) => /涉密文件分析/.test(e.innerText || ""));
      if (h) h.scrollIntoView({ block: "start" });
      else {
        const z = document.getElementById("dropZone");
        if (z) z.scrollIntoView({ block: "center" });
      }
    });
    await sleep(700);
    await shot("13_涉密文件分析_上传入口");

    // 14：真实上传一份多文件档案，展示"解析明细"（最能说明涉密通道的能力）
    const FIX = [
      path.resolve(__dirname, "..", "..", "_shots", "_t_vertical.xlsx"),
      path.resolve(__dirname, "..", "..", "_shots", "_t_legal.docx"),
    ].filter((f) => fs.existsSync(f));
    if (FIX.length) {
      const input = await p.$("#fileInput");
      if (input) {
        await input.setInputFiles(FIX);
        await sleep(1000);
        // 展开「解析明细」面板
        await p.evaluate(() => {
          document.querySelectorAll("details").forEach((d) => { d.open = true; });
        });
        const btn = await p.$("button:has-text('开始分析')")
                 || await p.$("#uploadBtn") || await p.$("#analyzeBtn");
        if (btn) { await btn.click(); }
        await waitResult(60000);
        await sleep(900);
        await p.evaluate(() => {
          document.querySelectorAll("details").forEach((d) => { d.open = true; });
        });
        await sleep(400);
        await shot("14_涉密文件分析_多文件合并与解析明细");
      }
    }

    console.log("\n控制台错误数：" + errors.length);
    if (errors.length) errors.slice(0, 5).forEach((e) => console.log("  ! " + e));
    console.log("截图目录：" + OUT);
  } finally {
    await browser.close();
  }
})();
