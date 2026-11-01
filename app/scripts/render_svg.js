/**
 * 把 SVG logo 渲染为高分辨率透明 PNG。
 * 用法：node render_svg.js <in.svg> <out.png> <width>
 */
const { chromium } = require("playwright-core");
const path = require("path");
const fs = require("fs");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

(async () => {
  const [inSvg, outPng, wArg] = process.argv.slice(2);
  const width = parseInt(wArg || "2000", 10);
  const svg = fs.readFileSync(inSvg, "utf8");

  const b = await chromium.launch({ executablePath: EDGE, headless: true });
  const ctx = await b.newContext({
    viewport: { width: 100, height: 100 },
    deviceScaleFactor: 1,
  });
  const p = await ctx.newPage();

  // 用 data URL 承载 SVG，透明底
  const b64 = Buffer.from(svg, "utf8").toString("base64");
  await p.setContent(
    `<!doctype html><html><head><style>
      html,body{margin:0;padding:0;background:transparent}
      img{display:block;width:${width}px;height:auto}
    </style></head><body>
      <img id="L" src="data:image/svg+xml;base64,${b64}">
    </body></html>`,
    { waitUntil: "networkidle" }
  );
  await p.waitForTimeout(400);

  const box = await p.evaluate(() => {
    const el = document.getElementById("L");
    const r = el.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height) };
  });

  const el = await p.$("#L");
  await el.screenshot({
    path: outPng,
    omitBackground: true,
  });

  console.log(`rendered ${path.basename(inSvg)} -> ${path.basename(outPng)}  ${box.w}x${box.h}`);
  await b.close();
})();
