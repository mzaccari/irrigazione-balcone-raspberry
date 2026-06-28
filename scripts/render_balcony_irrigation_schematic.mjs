import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const svgPath = path.resolve("outputs", "balcony-irrigation-schematic", "schema_elettrico_irrigazione_balcone.svg");
const outputDir = path.dirname(svgPath);
const pdfPath = path.join(outputDir, "schema_elettrico_irrigazione_balcone.pdf");
const pngPath = path.join(outputDir, "schema_elettrico_irrigazione_balcone.png");

const svg = await fs.readFile(svgPath, "utf8");
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 2 });

await page.setContent(`<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <style>
      @page { size: A3 landscape; margin: 10mm; }
      html, body { margin: 0; padding: 0; background: white; }
      body { display: flex; align-items: center; justify-content: center; }
      svg { width: 100vw; height: auto; max-height: 100vh; }
    </style>
  </head>
  <body>${svg}</body>
</html>`);

await page.pdf({
  path: pdfPath,
  format: "A3",
  landscape: true,
  printBackground: true,
  margin: { top: "10mm", bottom: "10mm", left: "10mm", right: "10mm" },
});

await page.screenshot({ path: pngPath, fullPage: true });
await browser.close();

console.log(pdfPath);
console.log(pngPath);
