// E2E 验证：WorkBuddy 云摘要的安全边界（2026-10-24 新增云摘要能力后的守卫回归）
//
// 背景：云摘要（fhFillCloudSummary）只允许服务【公开查询】通道；
//      涉密通道（/api/upload）的评分明细来自涉密文件，绝不允许发往任何第三方云 LLM。
//      （击穿点曾在 showResult：channel 参数未被 AI 摘要分支消费，已修复。）
//
// 断言组：
//  A  涉密通道 → AI 区显示「不调用任何模型」固定说明；云 SDK 即使加载成功也绝不收到提示词
//  B  公开通道（后端无 LLM 占位文本）→ 云摘要卡片正常生成；提示词不含公司名（脱敏口径）
//  C  公开通道（后端已有 DeepSeek 叙述）→ 直接展示后端文本，不触发云调用
//  D  fhBuildPrompt 单测式自检：na 维度表述为「依据不足」，公司名不外发
//
// 运行前置：python server.py（默认 127.0.0.1:8000；可用 FH_BASE 覆盖）
// 依赖：playwright-core（NODE_PATH 指向含 playwright-core 的 node_modules）
const { chromium } = require("playwright-core");
const path = require("path");

const BASE = process.env.FH_BASE || "http://127.0.0.1:8000";   // 与 run_all_checks 的 BASE_URL 一致
const OUT = path.resolve(__dirname, "..", "..", "_shots");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- 公共 mock 数据（形状对齐 server.py::_to_result） ----
function dim(label, score, rules) {
  return { label, score, deducted: 100 - score, weight: 0.167, raw_weight: 0.167,
           kind: "base", na: false, verified: null, note: "", rules: rules || [], checks: [] };
}
const DIMS = {
  performance: dim("履约表现", 88),
  legal: dim("法律合规", 70),
  operating: dim("经营管理", 82),
  financial: dim("财务状况", 45, [{ name: "资产负债率过高", clause: "第X条", deduct: 20,
                                    metric: "资产负债率", threshold: "70%", actual: "76.87%", hint: "", basis: "" }]),
  reputation: dim("舆情", 90),
  concentration: dim("关联集中度", 85),
};
function result(name, aiText) {
  return {
    supplier: name, important_outsourcing: false, non_resident: false, cross_border: false,
    total_score: 72.5, level: "yellow", action: "约谈", red_lines: [], constraints: [],
    coverage: { used: 6, available: Object.keys(DIMS), missing: [], base_available: 5,
                group: "six_dim", comparable: true },
    baseline_score: null, performance_origin: "simulated", weights: {}, raw_weights: {},
    cr: 0.0199, dim_labels: {}, dim_kind: {}, na_dimensions: [], performance: null,
    dimensions: JSON.parse(JSON.stringify(DIMS)), legal_events: [], news: [],
    financials: null, market: null, shareholders: [], registration: null,
    ai_analysis: aiText, risk_alerts: [], sources: [], verified: {}, failed: [],
  };
}

const CLASSIFIED_NAME = "涉密测试供应商甲";
const PUBLIC_NAME = "公开测试公司乙";
const LLM_PLACEHOLDER = "（LLM 未接入：未配置 DEEPSEEK_API_KEY，无法调用 LLM）";

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1560, height: 1180 } });
  const p = await ctx.newPage();
  const errors = [];
  p.on("dialog", async (d) => { try { await d.accept(""); } catch (e) {} });
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  p.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  // 云 SDK 桩：不真出网，只记录调用（提示词、配置），任何通道触发了云调用都会留痕
  const SDK_STUB = `
    window.__fhCloudCalls = { prompts: [], cfgs: [] };
    window.WorkBuddyCloud = {
      createWorkBuddyCloud: function (cfg) {
        window.__fhCloudCalls.cfgs.push(JSON.parse(JSON.stringify(cfg || {})));
        return {
          llm: {
            models: { list: async () => [{ id: "deepseek-v4.1-flash", disabled: false }] },
            chat: { completions: { create: async function* (opts) {
              window.__fhCloudCalls.prompts.push(JSON.parse(JSON.stringify(opts)));
              yield { choices: [{ delta: { content: "云摘要桩文本：该供应商财务维度偏弱。" } }] };
            } } },
          },
        };
      },
    };`;

  // 外部请求一律拦截（本测试不允许任何真实出网；出网点全部由桩替代）
  const externalHits = [];
  await p.route("**/*", (route) => {
    const u = new URL(route.request().url());
    if (u.hostname === "127.0.0.1" || u.hostname === "localhost") return route.continue();
    externalHits.push({ url: route.request().url(), type: route.request().resourceType() });
    if (/cdn\.jsdelivr\.net/.test(u.hostname)) {
      return route.fulfill({ contentType: "application/javascript", body: SDK_STUB });
    }
    return route.abort();
  });

  let pass = 0, fail = 0;
  const R = {};
  const check = (k, ok, note) => {
    if (ok) { pass++; console.log(`   OK   ${k}${note ? "  — " + note : ""}`); }
    else { fail++; console.log(`   FAIL ${k}${note ? "  — " + note : ""}`); R[k] = false; }
  };

  await p.goto(BASE, { waitUntil: "domcontentloaded" });
  await sleep(1500);
  check("S0 页面加载且云 SDK 桩就位", await p.evaluate(() => !!window.WorkBuddyCloud));

  // ---------- A. 涉密通道 ----------
  const beforeCalls = () => p.evaluate(() => window.__fhCloudCalls.prompts.length);
  const callsBeforeA = await beforeCalls();

  await p.route("**/api/upload", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      channel: "classified", count: 1, ok_count: 1,
      supplier_name: CLASSIFIED_NAME, linked_roster: null,
      merged: result(CLASSIFIED_NAME, null),
      files: [{ filename: "财务报表.pdf", ok: true, fields: ["财务指标"] }],
    }),
  }));
  await p.setInputFiles("#fileInput", {
    name: "财务报表.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 test"),
  });
  await sleep(1800);

  const aiBoxesA = await p.$$eval(".ai-box", (els) => els.map((e) => e.textContent || ""));
  check("A1 涉密结果页出现 AI 区固定说明（不调用任何模型）",
        aiBoxesA.some((t) => t.indexOf("不调用任何模型") >= 0), aiBoxesA.join(" | ").slice(0, 80));
  check("A2 涉密结果页没有云摘要占位（正在生成…）",
        !aiBoxesA.some((t) => t.indexOf("正在生成") >= 0 || t.indexOf("云服务") >= 0));
  check("A3 云 SDK 未收到任何提示词（涉密数据零外发）",
        (await beforeCalls()) === callsBeforeA,
        `prompts=${await beforeCalls()}`);
  check("A4 无真实外部请求（除被桩替代的 SDK 脚本加载）",
        externalHits.every((h) => h.type === "script"), JSON.stringify(externalHits));
  await p.screenshot({ path: path.join(OUT, "_cloud_guard_A_classified.png"), fullPage: false });

  // ---------- B. 公开通道（后端 LLM 占位文本 → 走云摘要） ----------
  await p.unroute("**/api/upload");
  // 首页改版层是全屏覆盖层，须走路由 hash 进入应用（switchPage 不收起首页）
  await p.evaluate(() => { location.hash = "#/scan"; });
  await sleep(1200);
  await p.route("**/api/query", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ channel: "public", result: result(PUBLIC_NAME, LLM_PLACEHOLDER) }),
  }));
  await p.fill("#queryInput", PUBLIC_NAME);
  await p.evaluate(() => doQuery());
  await sleep(1800);

  const cloudBox = await p.$("[data-fh-ai-body]");
  const cloudText = cloudBox ? (await cloudBox.textContent()) : "";
  check("B1 公开通道云摘要卡片生成", !!cloudBox && cloudText.indexOf("云摘要桩文本") >= 0,
        (cloudText || "").slice(0, 60));
  const promptB = await p.evaluate(() => {
    const ps = (window.__fhCloudCalls && window.__fhCloudCalls.prompts) || [];
    return ps.length ? ps[ps.length - 1].messages.map((m) => m.content).join("\n") : "";
  });
  check("B2 云提示词不含公司名（脱敏口径）", promptB.indexOf(PUBLIC_NAME) < 0);
  check("B3 云提示词含维度事实", promptB.indexOf("财务状况") >= 0 && promptB.indexOf("45 分") >= 0);
  await p.screenshot({ path: path.join(OUT, "_cloud_guard_B_public_cloud.png"), fullPage: false });

  // ---------- C. 公开通道（后端已有 DeepSeek 叙述 → 不触发云调用） ----------
  const callsBeforeC = await beforeCalls();
  await p.unroute("**/api/query");
  await p.evaluate(() => { location.hash = "#/scan"; });
  await sleep(1200);
  await p.route("**/api/query", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ channel: "public", result: result(PUBLIC_NAME, "后端 DeepSeek 分析文本：财务结构需关注。") }),
  }));
  await p.fill("#queryInput", PUBLIC_NAME);
  await p.evaluate(() => doQuery());
  await sleep(1500);
  const aiBoxesC = await p.$$eval(".ai-box", (els) => els.map((e) => e.textContent || ""));
  check("C1 后端有叙述时直接展示 DeepSeek 卡片",
        aiBoxesC.some((t) => t.indexOf("后端 DeepSeek 分析文本") >= 0));
  check("C2 后端有叙述时不新增云调用", (await beforeCalls()) === callsBeforeC);

  // ---------- D. fhBuildPrompt 直接自检 ----------
  const pd = await p.evaluate(() => {
    const r = { supplier: "绝密供应商丙", total_score: null, level: "yellow",
                dimensions: { financial: { na: false, score: 40, rules: [{ name: "R1", hit: true }] } },
                coverage: { available: ["financial"], missing: ["legal", "operating"] }, red_lines: ["被执行人"] };
    return fhBuildPrompt(r);
  });
  check("D1 na/缺失维度表述为依据不足", pd.indexOf("依据不足") >= 0);
  check("D2 公司名不进入提示词", pd.indexOf("绝密供应商丙") < 0 && pd.indexOf("目标供应商") >= 0);
  check("D3 红线事实保留", pd.indexOf("被执行人") >= 0);

  const bizErrors = errors.filter((t) => t.indexOf("云服务 SDK 加载失败") < 0
                                    && t.indexOf("[fh-cloud]") < 0);
  check("E1 无页面错误", bizErrors.length === 0, bizErrors.slice(0, 2).join(" ; "));

  await browser.close();
  console.log(`\n结果：${fail === 0 ? "✅ 全部通过" : "❌ 存在失败"}   通过 ${pass} / ${pass + fail}`);
  process.exit(fail === 0 ? 0 : 1);
})();
