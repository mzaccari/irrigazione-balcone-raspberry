import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputDir = path.resolve("outputs", "balcony-irrigation-shopping-list");
await fs.mkdir(outputDir, { recursive: true });

const workbook = Workbook.create();

const items = [
  ["Controllo", "Alta", 1, "Raspberry Pi Zero 2 WH", "Zero 2 W with headers / WH, Wi-Fi integrato", "Melopero, The Pi Hut, Welectron", "Da acquistare", "", 25, "", "Versione con header gia saldati: niente saldature"],
  ["Controllo", "Alta", 1, "MicroSD high endurance", "SanDisk High Endurance 32/64 GB", "Amazon, MediaWorld, Unieuro", "Da acquistare", "", 10, "", "Meglio endurance per uso sempre acceso"],
  ["Alimentazione", "Alta", 1, "Convertitore 12V -> 5V", "Buck converter 12V a 5V USB, almeno 3A", "Amazon, negozi elettronica", "Da acquistare", "", 10, "", "Alimenta il Raspberry dalla batteria 12V"],
  ["Pompe", "Alta", 3, "Pompa sommersa 12V", "Pompa 12V DC circa 600 L/h, prevalenza circa 5 m", "Leroy Merlin, ManoMano, Amazon", "Da acquistare", "", 12, "", "Una per zona; verificare corrente assorbita"],
  ["Elettronica", "Alta", 3, "Driver MOSFET con morsetti", "Adafruit MOSFET Driver 5648 o modulo equivalente 3.3V con morsetti", "Robot Italy, Adafruit, DigiKey", "Da acquistare", "", 8, "", "Preferibile al rele; zero saldature se con morsetti"],
  ["Alimentazione", "Alta", 1, "Batteria LiFePO4 12V", "12.8V 20Ah con BMS integrato", "Amazon: ECO-WORTHY, Renogy, Redodo, Kepworth", "Da acquistare", "", 75, "", "20Ah consigliati per margine"],
  ["Solare", "Alta", 1, "Regolatore solare MPPT", "Victron SmartSolar MPPT 75/10 compatibile LiFePO4", "Moory, Toosolar, Amazon", "Da acquistare", "", 70, "", "Piu affidabile dei PWM economici"],
  ["Solare", "Alta", 1, "Pannello solare", "Pannello 12V 50W minimo, 100W consigliato", "FuturaNet, Amazon, Leroy Merlin", "Da acquistare", "", 65, "", "100W se vuoi piu autonomia in giorni nuvolosi"],
  ["Idraulica", "Alta", 1, "Kit irrigazione balcone", "Claber Kit Terrazzo 90772 o Kit Drip 20 vasi 90764", "Claber, Leroy Merlin, Brico, ManoMano", "Da acquistare", "", 55, "", "Da dividere in tre zone separate"],
  ["Idraulica", "Alta", 3, "Valvola di non ritorno", "Valvola per tubo 6/8/10 mm secondo diametro pompa", "Amazon, negozi acquari, Leroy Merlin", "Da acquistare", "", 3, "", "Una dopo ogni pompa"],
  ["Idraulica", "Alta", 3, "Filtro in linea", "Filtro microirrigazione 120/150 mesh", "Amazon, agrarie, Leroy Merlin", "Da acquistare", "", 5, "", "Riduce intasamento gocciolatori"],
  ["Idraulica", "Alta", 1, "Serbatoio acqua", "60-100 L, opaco, con coperchio", "Leroy Merlin, Brico, agrarie", "Da acquistare", "", 35, "", "Meglio in ombra e chiuso"],
  ["Protezione", "Alta", 1, "Scatola elettrica stagna", "Box IP65/IP66 circa 190x140x70 mm o piu grande", "Leroy Merlin, Amazon, elettricista", "Da acquistare", "", 15, "", "Tenere in ombra o schermata dal sole diretto"],
  ["Protezione", "Alta", 1, "Set pressacavi", "Pressacavi IP68 M12/M16 assortiti", "Amazon, Leroy Merlin", "Da acquistare", "", 10, "", "Per ingressi cavi nel box"],
  ["Cablaggio", "Alta", 1, "Morsetti Wago 221", "Set Wago 221 2/3/5 poli", "Amazon, elettricista", "Da acquistare", "", 15, "", "Per evitare saldature"],
  ["Cablaggio", "Alta", 5, "Portafusibili 12V", "Portafusibili auto ATO/ATC impermeabili", "Amazon, autoricambi", "Da acquistare", "", 2, "", "Generale + uno per ogni pompa + scorta"],
  ["Cablaggio", "Alta", 1, "Set fusibili", "Fusibili auto 2A/3A/5A/10A assortiti", "Amazon, autoricambi", "Da acquistare", "", 8, "", "Dimensionare in base alle pompe reali"],
  ["Cablaggio", "Media", 1, "Interruttore generale 12V", "Interruttore DC da quadro o impermeabile", "Amazon, elettricista", "Da acquistare", "", 8, "", "Per manutenzione rapida"],
  ["Sensori", "Alta", 1, "Sensore livello acqua", "Galleggiante serbatoio NO/NC", "Amazon, negozi elettronica", "Da acquistare", "", 7, "", "Blocco anti-marcia a secco"],
  ["Sensori", "Media", 1, "ADC ADS1115", "ADS1115 I2C 16 bit pre-soldered / STEMMA QT", "Adafruit, Amazon, Seeed, Robot Italy", "Da acquistare", "", 12, "", "Serve per sensori analogici umidita"],
  ["Sensori", "Media", 3, "Sensori umidita terreno", "DFRobot SEN0308 waterproof capacitivo o equivalente", "Farnell, DFRobot, Robot Italy", "Da acquistare", "", 12, "", "Uno per zona all'inizio"],
  ["Sensori", "Bassa", 1, "Sensore aria", "BME280 o SHT31 temperatura/umidita", "Amazon, Robot Italy, Melopero", "Da acquistare", "", 8, "", "Utile per modalita estate/inverno"],
  ["Cablaggio", "Media", 1, "Cavo elettrico pompe", "2x0.75 mm2 o 2x1 mm2", "Leroy Merlin, Brico, elettricista", "Da acquistare", "", 20, "", "Usare sezioni maggiori per tratte lunghe"],
  ["Idraulica", "Media", 1, "Tubi e raccordi extra", "Microtubo 4/6 mm, tubo 8/10 mm, T, rubinetti, tappi", "Claber, Leroy Merlin, agrarie", "Da acquistare", "", 25, "", "Serve sempre piu di quanto sembra"],
  ["Cablaggio", "Media", 6, "Connettori impermeabili 2 pin", "Connettori IP67/IP68 a vite o pre-cablati", "Amazon, negozi elettronica", "Da acquistare", "", 3, "", "Per staccare pompe e sensori senza aprire tutto"],
];

const shopping = workbook.worksheets.add("Lista spesa");
shopping.showGridLines = false;

shopping.getRange("A1:K1").merge();
shopping.getRange("A1").values = [["Irrigazione balcone - Lista della spesa"]];
shopping.getRange("A1").format = {
  fill: "#1F4E46",
  font: { bold: true, color: "#FFFFFF", size: 16 },
  horizontalAlignment: "center",
};
shopping.getRange("A2:K2").merge();
shopping.getRange("A2").values = [["Aggiorna la colonna Stato mentre acquisti. I prezzi stimati sono indicativi e modificabili."]];
shopping.getRange("A2").format = {
  fill: "#E8F3EF",
  font: { color: "#1F4E46", italic: true },
  horizontalAlignment: "center",
};

const headers = [["Categoria", "Priorita", "Q.ta", "Componente", "Modello / ricerca consigliata", "Dove trovarlo", "Stato", "Data acquisto", "Prezzo stimato cad.", "Prezzo reale cad.", "Note"]];
shopping.getRange("A4:K4").values = headers;
shopping.getRangeByIndexes(4, 0, items.length, headers[0].length).values = items;

const firstDataRow = 5;
const lastDataRow = firstDataRow + items.length - 1;
shopping.tables.add(`A4:K${lastDataRow}`, true, "ShoppingList");

shopping.getRange("A4:K4").format = {
  fill: "#2D6A5F",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  wrapText: true,
};
shopping.getRange(`A5:K${lastDataRow}`).format = {
  wrapText: true,
  verticalAlignment: "top",
};
shopping.getRange(`C5:C${lastDataRow}`).format.horizontalAlignment = "center";
shopping.getRange(`I5:J${lastDataRow}`).setNumberFormat("€ #,##0.00");
shopping.getRange(`H5:H${lastDataRow}`).setNumberFormat("yyyy-mm-dd");

shopping.getRange("A:K").format.font = { name: "Aptos", size: 10 };
shopping.getRange("A:A").format.columnWidthPx = 115;
shopping.getRange("B:B").format.columnWidthPx = 80;
shopping.getRange("C:C").format.columnWidthPx = 55;
shopping.getRange("D:D").format.columnWidthPx = 190;
shopping.getRange("E:E").format.columnWidthPx = 270;
shopping.getRange("F:F").format.columnWidthPx = 230;
shopping.getRange("G:G").format.columnWidthPx = 115;
shopping.getRange("H:H").format.columnWidthPx = 105;
shopping.getRange("I:J").format.columnWidthPx = 115;
shopping.getRange("K:K").format.columnWidthPx = 300;
shopping.freezePanes.freezeRows(4);

shopping.getRange(`G5:G${lastDataRow}`).dataValidation = {
  rule: { type: "list", values: ["Da acquistare", "Ordinato", "Acquistato", "Non serve"] },
};
shopping.getRange(`B5:B${lastDataRow}`).dataValidation = {
  rule: { type: "list", values: ["Alta", "Media", "Bassa"] },
};

shopping.getRange(`G5:G${lastDataRow}`).conditionalFormats.add("containsText", {
  text: "Acquistato",
  format: { fill: "#D9EAD3", font: { color: "#1B5E20", bold: true } },
});
shopping.getRange(`G5:G${lastDataRow}`).conditionalFormats.add("containsText", {
  text: "Da acquistare",
  format: { fill: "#FCE4D6", font: { color: "#9C2F00", bold: true } },
});
shopping.getRange(`G5:G${lastDataRow}`).conditionalFormats.add("containsText", {
  text: "Ordinato",
  format: { fill: "#FFF2CC", font: { color: "#7F6000", bold: true } },
});

const summary = workbook.worksheets.add("Riepilogo");
summary.showGridLines = false;
summary.getRange("A1:F1").merge();
summary.getRange("A1").values = [["Riepilogo acquisti"]];
summary.getRange("A1").format = {
  fill: "#1F4E46",
  font: { bold: true, color: "#FFFFFF", size: 16 },
  horizontalAlignment: "center",
};
summary.getRange("A3:B8").values = [
  ["Totale righe", ""],
  ["Da acquistare", ""],
  ["Ordinato", ""],
  ["Acquistato", ""],
  ["Non serve", ""],
  ["Budget stimato", ""],
];
summary.getRange("B3:B8").formulas = [
  [`=COUNTA('Lista spesa'!D${firstDataRow}:D${lastDataRow})`],
  [`=COUNTIF('Lista spesa'!G${firstDataRow}:G${lastDataRow},"Da acquistare")`],
  [`=COUNTIF('Lista spesa'!G${firstDataRow}:G${lastDataRow},"Ordinato")`],
  [`=COUNTIF('Lista spesa'!G${firstDataRow}:G${lastDataRow},"Acquistato")`],
  [`=COUNTIF('Lista spesa'!G${firstDataRow}:G${lastDataRow},"Non serve")`],
  [`=SUMPRODUCT('Lista spesa'!C${firstDataRow}:C${lastDataRow},'Lista spesa'!I${firstDataRow}:I${lastDataRow})`],
];
summary.getRange("A3:B8").format = { wrapText: true };
summary.getRange("A3:A8").format = { fill: "#E8F3EF", font: { bold: true, color: "#1F4E46" } };
summary.getRange("B8").setNumberFormat("€ #,##0.00");
summary.getRange("A:A").format.columnWidthPx = 180;
summary.getRange("B:B").format.columnWidthPx = 140;

summary.getRange("D3:F8").values = [
  ["Priorita", "Cosa fare", "Nota"],
  ["Alta", "Comprare prima", "Necessaria per impianto funzionante"],
  ["Media", "Comprare dopo primo test", "Utile per robustezza e automazione"],
  ["Bassa", "Opzionale", "Aggiunge comfort o dati"],
  ["Zero saldature", "Preferire morsetti", "Wago, morsetti a vite, connettori IP67"],
  ["Test iniziale", "Una pompa alla volta", "Misurare ml/min prima di automatizzare"],
];
summary.getRange("D3:F3").format = { fill: "#2D6A5F", font: { bold: true, color: "#FFFFFF" } };
summary.getRange("D4:F8").format = { wrapText: true };
summary.getRange("D:F").format.columnWidthPx = 190;

const blueprint = workbook.worksheets.add("Schema tecnico");
blueprint.showGridLines = false;
blueprint.getRange("A1:H1").merge();
blueprint.getRange("A1").values = [["Schema tecnico rapido"]];
blueprint.getRange("A1").format = {
  fill: "#1F4E46",
  font: { bold: true, color: "#FFFFFF", size: 16 },
  horizontalAlignment: "center",
};
blueprint.getRange("A3:D7").values = [
  ["GPIO", "Pin fisico", "Funzione", "Nota"],
  ["GPIO17", "11", "Pompa 1", "Zona acqua alta: basilico, menta"],
  ["GPIO27", "13", "Pompa 2", "Zona media: lauro/alloro, ornamentali"],
  ["GPIO22", "15", "Pompa 3", "Zona secca: rosmarino, oleandro"],
  ["GPIO23", "16", "Sensore livello acqua", "Blocca pompe se serbatoio vuoto"],
];
blueprint.getRange("A3:D3").format = { fill: "#2D6A5F", font: { bold: true, color: "#FFFFFF" } };
blueprint.getRange("A4:D7").format = { wrapText: true };

blueprint.getRange("F3:H8").values = [
  ["Linea", "Catena idraulica", "Regola"],
  ["Pompa 1", "Pompa -> valvola non ritorno -> filtro -> gocciolatori", "Run breve/frequente"],
  ["Pompa 2", "Pompa -> valvola non ritorno -> filtro -> gocciolatori", "Run medio"],
  ["Pompa 3", "Pompa -> valvola non ritorno -> filtro -> gocciolatori", "Run raro/breve"],
  ["Sicurezza", "Una pompa alla volta", "Pausa 30s tra pompe"],
  ["Taratura", "Misura ml/min in bottiglia graduata", "Poi imposta tempi"],
];
blueprint.getRange("F3:H3").format = { fill: "#2D6A5F", font: { bold: true, color: "#FFFFFF" } };
blueprint.getRange("F4:H8").format = { wrapText: true };
blueprint.getRange("A:H").format.font = { name: "Aptos", size: 10 };
blueprint.getRange("A:A").format.columnWidthPx = 90;
blueprint.getRange("B:B").format.columnWidthPx = 85;
blueprint.getRange("C:C").format.columnWidthPx = 190;
blueprint.getRange("D:D").format.columnWidthPx = 270;
blueprint.getRange("F:F").format.columnWidthPx = 95;
blueprint.getRange("G:G").format.columnWidthPx = 330;
blueprint.getRange("H:H").format.columnWidthPx = 180;

for (const ws of [shopping, summary, blueprint]) {
  ws.getUsedRange().format.verticalAlignment = "top";
}

await workbook.render({ sheetName: "Lista spesa", range: "A1:K20", scale: 1, format: "png" });
await workbook.render({ sheetName: "Riepilogo", range: "A1:F10", scale: 1, format: "png" });
await workbook.render({ sheetName: "Schema tecnico", range: "A1:H9", scale: 1, format: "png" });

const errorScan = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "formula error scan",
});
console.log(errorScan.ndjson);

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
const outPath = path.join(outputDir, "lista_spesa_irrigazione_balcone.xlsx");
await xlsx.save(outPath);
console.log(outPath);
