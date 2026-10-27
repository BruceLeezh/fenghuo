/**
 * HTML → PDF（A4，用于初赛提交材料）。
 *
 * 为什么用它：本地没有 wkhtmltopdf / weasyprint，但 E2E 已经在用 Playwright + 本机 Edge，
 * 复用同一条链路可以做到「HTML 里写 @page 打印样式 → 直接出 PDF」，版式与浏览器一致。
 *
 * 用法：
 *   node scripts/html_to_pdf.js <输入.html> [输出.pdf]
 *   不传输出则同名换扩展名。
 */
const { chromium } = require("playwright-core");
const path = require("path");
const fs = require("fs");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

(async () => {
  const input = process.argv[2];
  if (!input) {
    console.error("用法：node scripts/html_to_pdf.js <输入.html> [输出.pdf]");
    process.exit(2);
  }
  const inPath = path.resolve(input);
  if (!fs.existsSync(inPath)) {
    console.error("找不到输入文件：" + inPath);
    process.exit(2);
  }
  const outPath = process.argv[3]
    ? path.resolve(process.argv[3])
    : inPath.replace(/\.html?$/i, ".pdf");

  const executablePath = fs.existsSync(EDGE) ? EDGE : undefined;
  const browser = await chromium.launch({ executablePath, headless: true });
  const page = await browser.newPage();
  await page.goto("file:///" + inPath.replace(/\\/g, "/"), { waitUntil: "load" });
  // 等字体与布局稳定
  await page.waitForTimeout(600);
  await page.pdf({
    path: outPath,
    format: "A4",
    printBackground: true,
    preferCSSPageSize: true,   // 尊重 HTML 里的 @page 尺寸与页边距
  });
  await browser.close();

  const kb = (fs.statSync(outPath).size / 1024).toFixed(1);
  console.log("已生成 " + outPath + "  (" + kb + " KB)");
})();
