import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

// Ogni schema e' un SVG a mano in outputs/balcony-irrigation-schematic/.
// Uso: npm run render-schematic [pi|esp32]  (default: tutti)
const DIAGRAMS = {
  pi: "schema_elettrico_irrigazione_balcone",
  esp32: "schema_elettrico_nodo_esp32",
};

const outputDir = path.resolve("outputs", "balcony-irrigation-schematic");
const requested = process.argv.slice(2);
const keys = requested.length ? requested : Object.keys(DIAGRAMS);

const unknown = keys.filter((key) => !DIAGRAMS[key]);
if (unknown.length) {
  console.error(`Schema sconosciuto: ${unknown.join(", ")}. Validi: ${Object.keys(DIAGRAMS).join(", ")}`);
  process.exit(1);
}

const browser = await chromium.launch();

for (const key of keys) {
  const base = DIAGRAMS[key];
  const svgPath = path.join(outputDir, `${base}.svg`);
  const pdfPath = path.join(outputDir, `${base}.pdf`);
  const pngPath = path.join(outputDir, `${base}.png`);

  const svg = await fs.readFile(svgPath, "utf8");
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
  await page.close();

  console.log(pdfPath);
  console.log(pngPath);
}

await browser.close();
