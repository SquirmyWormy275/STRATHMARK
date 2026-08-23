import crypto from "node:crypto";
import fs from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const moduleRoot = process.env.STRATHMARK_ARTIFACT_NODE_MODULES;
if (!moduleRoot) {
  throw new Error("STRATHMARK_ARTIFACT_NODE_MODULES must name the bundled node_modules directory");
}
const require = createRequire(import.meta.url);
const artifactEntry = require.resolve("@oai/artifact-tool", { paths: [moduleRoot] });
const zipEntry = require.resolve("jszip", { paths: [moduleRoot] });
const { SpreadsheetFile, Workbook } = await import(pathToFileURL(artifactEntry).href);
const { default: JSZip } = await import(pathToFileURL(zipEntry).href);

const scriptPath = fileURLToPath(import.meta.url);
const repo = path.resolve(process.env.STRATHMARK_REPO_ROOT || path.join(path.dirname(scriptPath), "..", ".."));
const mode = process.argv[2] || "--verify";
const workbookPath = path.join(repo, "benchmarks", "v3", "formula_golden.xlsx");
const receiptPath = path.join(repo, "benchmarks", "v3", "formula_engine_verification.json");
const workDir = path.resolve(process.env.STRATHMARK_ARTIFACT_WORKDIR || process.cwd());
const previewDir = path.join(workDir, "formula-golden-renders");
await fs.mkdir(previewDir, { recursive: true });

const sha256 = (value) => crypto.createHash("sha256").update(value).digest("hex");
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
const digestValue = (value) => sha256(Buffer.from(canonical(value), "utf8"));

const NAVY = "#17324D";
const TEAL = "#147D73";
const BLUE = "#EAF2F8";
const GREEN = "#E8F5EE";
const GOLD = "#FFF4D6";
const BORDER = "#B8C4CE";
const WHITE = "#FFFFFF";
const INPUT = "#1F5AA6";

function title(sheet, range, text) {
  sheet.getRange(range).merge();
  sheet.getRange(range).values = [[text]];
  sheet.getRange(range).format = {
    fill: NAVY,
    font: { bold: true, color: WHITE, size: 16 },
    rowHeight: 30,
    verticalAlignment: "center",
  };
}
function header(range) {
  range.format = {
    fill: TEAL,
    font: { bold: true, color: WHITE },
    borders: { preset: "all", style: "thin", color: BORDER },
    wrapText: true,
  };
}
function inputStyle(range) {
  range.format = {
    fill: GOLD,
    font: { color: INPUT },
    borders: { preset: "all", style: "thin", color: BORDER },
  };
}
function formulaStyle(range) {
  range.format = {
    fill: GREEN,
    font: { color: "#166534" },
    borders: { preset: "all", style: "thin", color: BORDER },
  };
}

function buildWorkbook() {
  const wb = Workbook.create();
  const assumptions = wb.worksheets.add("Assumptions");
  const inputs = wb.worksheets.add("Inputs");
  const governor = wb.worksheets.add("Governor Projection");
  const transforms = wb.worksheets.add("Transforms");
  const irls = wb.worksheets.add("IRLS Trace");
  const distribution = wb.worksheets.add("Distribution");
  const sheets = [assumptions, inputs, governor, transforms, irls, distribution];
  for (const sheet of sheets) sheet.showGridLines = false;

  title(assumptions, "A1:Q1", "STRATHMARK V3 Formula Assessor — Frozen Parameter and Prior Bundle");
  assumptions.getRange("A2:Q2").merge();
  assumptions.getRange("A2:Q2").values = [["Gold cells are published assumptions or sealed facts. Green cells are formulas. Prior selection is exact target context, then declared discipline, then frozen population fallback."]];
  assumptions.getRange("A2:Q2").format = { fill: GOLD, wrapText: true, rowHeight: 34 };
  assumptions.getRange("A4:B4").values = [["Parameter", "Value"]];
  header(assumptions.getRange("A4:B4"));
  assumptions.getRange("A5:B22").values = [
    ["Huber c", 1.5], ["Max IRLS iterations", 20], ["IRLS tolerance", 0.0000000001],
    ["MAD consistency", 1.4826], ["Minimum robust scale", 0.05], ["Exact context", 1],
    ["Same discipline", 0.6], ["Cross discipline", 0.25], ["Diameter decay", 2],
    ["Recency half-life days", 730], ["Issued official", 1], ["Verified historical", 0.85],
    ["Active tournament", 1], ["Other authoritative", 0.9], ["Legacy", 0.75],
    ["Density exponent", 0.35], ["Dense effective N", 5], ["Time quantum ms", 1],
  ];
  inputStyle(assumptions.getRange("A5:B22"));
  assumptions.getRange("D4:G4").values = [["Event", "Scale", "Size exponent", "Discipline"]];
  header(assumptions.getRange("D4:G4"));
  assumptions.getRange("D5:G7").values = [
    ["single_buck", 1.3, 1.8, "sawing"],
    ["standing_block", 1.08, 2.05, "axe"],
    ["underhand", 1, 2, "axe"],
  ];
  inputStyle(assumptions.getRange("D5:G7"));
  assumptions.getRange("D10:E10").values = [["Conversion variance", "Value"]];
  header(assumptions.getRange("D10:E10"));
  assumptions.getRange("D11:E14").values = [
    ["Same discipline", 0.01], ["Cross discipline", 0.04],
    ["Size coefficient", 0.15], ["Material coefficient", 0.25],
  ];
  inputStyle(assumptions.getRange("D11:E14"));
  assumptions.getRange("D17:E17").values = [["Positive support", "Value"]];
  header(assumptions.getRange("D17:E17"));
  assumptions.getRange("D18:E21").values = [
    ["Minimum log sigma", 0.05], ["Maximum log sigma", 1.5],
    ["Minimum time ms", 1], ["Maximum time ms", 600000],
  ];
  inputStyle(assumptions.getRange("D18:E21"));
  assumptions.getRange("D23:E23").values = [["Manifest lock", "Published value"]];
  header(assumptions.getRange("D23:E23"));
  assumptions.getRange("D24:E24").values = [["SHA-256", "2c58a9527c77a33e0b813fe938db44c6298ac0ea8b543a199b720d97baaf1354"]];
  inputStyle(assumptions.getRange("D24:E24"));
  assumptions.getRange("D26:E26").values = [["Quantile probability", "Frozen Z score"]];
  header(assumptions.getRange("D26:E26"));
  assumptions.getRange("D27:E31").values = [[0.05, -1.645], [0.25, -0.674], [0.5, 0], [0.75, 0.674], [0.95, 1.645]];
  inputStyle(assumptions.getRange("D27:E31"));

  assumptions.getRange("H4:Q4").values = [["Lookup key", "Tier", "Event", "Diameter mm", "Material", "Discipline", "Median seconds", "Log variance", "Pseudo strength", "Lineage SHA-256"]];
  header(assumptions.getRange("H4:Q4"));
  assumptions.getRange("H5:Q10").values = [
    ["standing_block|300|eucalyptus", "exact_context", "standing_block", 300, "eucalyptus", "axe", 52, 0.1764, 3, "b15434b2c74b2db5ffaa698c623ecb122b760b41f8d928ad1a3b02fa494cf1f1"],
    ["underhand|300|eucalyptus", "exact_context", "underhand", 300, "eucalyptus", "axe", 45, 0.16, 3, "be67bb87329d79a404244a99832bebae1275271faca40713ed7e784e68727d4d"],
    ["underhand|350|pine", "exact_context", "underhand", 350, "pine", "axe", 58, 0.3025, 3, "c100f311d763ccb7b49ccff80159b7244b2f891e044126c73ad972d4ee75a52e"],
    ["axe", "discipline", "", null, "", "axe", 49, 0.25, 3, "e435c4d1cd8a5dceb3a9a3af9ec9ff58b6316b5e4ca03500272e2aa016a3dfc2"],
    ["sawing", "discipline", "", null, "", "sawing", 65, 0.3025, 3, "c2b4b42da3cd0cd980cf135b8924aadf1671e839175b8125d168153e6082b507"],
    ["population", "population", "", null, "", "", 55, 0.4225, 3, "0ac3345d97864c3b6db118ef06107316b3fe73751e0a290cfeb9e19e4413aee7"],
  ];
  inputStyle(assumptions.getRange("H5:Q10"));
  assumptions.getRange("H13:I13").values = [["Selected prior field", "Formula-derived value"]];
  header(assumptions.getRange("H13:I13"));
  assumptions.getRange("H14:H20").values = [["Target context key"], ["Target discipline"], ["Source row"], ["Tier"], ["Median seconds"], ["Log variance / pseudo"], ["Lineage"]];
  assumptions.getRange("I14").formulas = [["='Inputs'!$B$4&\"|\"&'Inputs'!$B$5&\"|\"&'Inputs'!$B$6"]];
  assumptions.getRange("I15").formulas = [["=VLOOKUP('Inputs'!$B$4,$D$5:$G$7,4,FALSE)"]];
  assumptions.getRange("I16").formulas = [["=IF(I14=$H$5,ROW($H$5),IF(I14=$H$6,ROW($H$6),IF(I14=$H$7,ROW($H$7),IF(I15=$H$8,ROW($H$8),IF(I15=$H$9,ROW($H$9),ROW($H$10))))))"]];
  assumptions.getRange("I17").formulas = [["=INDEX($I$5:$I$10,I16-4)"]];
  assumptions.getRange("I18").formulas = [["=INDEX($N$5:$N$10,I16-4)"]];
  assumptions.getRange("I19").formulas = [["=INDEX($O$5:$O$10,I16-4)&\" / \"&INDEX($P$5:$P$10,I16-4)"]];
  assumptions.getRange("I20").formulas = [["=INDEX($Q$5:$Q$10,I16-4)"]];
  formulaStyle(assumptions.getRange("H14:I20"));
  assumptions.freezePanes.freezeRows(4);
  assumptions.getRange("A:Q").format.columnWidth = 18;
  assumptions.getRange("A:A").format.columnWidth = 28;
  assumptions.getRange("E:E").format.columnWidth = 68;
  assumptions.getRange("H:H").format.columnWidth = 32;
  assumptions.getRange("Q:Q").format.columnWidth = 68;

  title(inputs, "A1:P1", "Golden Replay — Visible Target, P-256-Sealed Governor Facts, and Raw Evidence");
  inputs.getRange("A3:B3").values = [["Target", "Value"]];
  header(inputs.getRange("A3:B3"));
  inputs.getRange("A4:B7").values = [["Event", "underhand"], ["Diameter mm", 300], ["Material", "eucalyptus"], ["Density kg/m3", 720]];
  inputStyle(inputs.getRange("A4:B7"));
  inputs.getRange("D3:E3").values = [["Governor receipt field", "Sealed value"]];
  header(inputs.getRange("D3:E3"));
  inputs.getRange("D4:E10").values = [
    ["Cutoff UTC", new Date("2026-08-29T12:00:00.000Z")],
    ["Active tournament", "show-a"], ["Other authoritative", "authority-a"],
    ["Legacy tournament", "legacy-a"], ["Historical cutoff", "history:2026-08-01"],
    ["Tournament epoch", "epoch:9a37db8ae45d0118017df8ea5cc087f85b528d7d2b4983588fac7e8671b7c849"],
    ["Receipt SHA-256", "924f418092da5b4c9507aa98f80a67114203387add5bbeb1c212a224aa74ab20"],
  ];
  inputStyle(inputs.getRange("D4:E10"));
  inputs.getRange("E4").format.numberFormat = "yyyy-mm-dd hh:mm";
  inputs.getRange("G3:H3").values = [["Signed governor seal", "Bound value"]];
  header(inputs.getRange("G3:H3"));
  inputs.getRange("G4:H10").values = [
    ["Signer key ID", "integrity-key:formula-test"],
    ["Signed manifest body SHA-256", "152e1ad207952255eacc67b8278507829d2cdad23fe04a0fbc2f7a885f7fccb3"],
    ["EvidencePacket SHA-256", "18e7c2b3e7ed1e945d9b89edfd591a7cfa8efcb19d69af4f82a064459af97735"],
    ["Epoch content SHA-256", "9a37db8ae45d0118017df8ea5cc087f85b528d7d2b4983588fac7e8671b7c849"],
    ["Epoch maximum sequence", 3],
    ["Epoch member-set SHA-256", "0ee19d6bb565873f5dd5be473563dc6cfd5a440ae6c97a6ae43f525a505b5093"],
    ["Seal algorithm", "ecdsa-p256-sha256"],
  ];
  inputStyle(inputs.getRange("G4:H10"));
  inputs.getRange("A12:P12").values = [["Seq", "Raw ms", "Event", "Diameter mm", "Material", "Density", "Occurred UTC", "Origin", "Tournament", "Result status", "Evidence ID", "Cutoff key", "Sealed max seq", "Source SHA-256", "Result key", "Result revision"]];
  header(inputs.getRange("A12:P12"));
  inputs.getRange("A13:P15").values = [
    [1, 38200, "underhand", 300, "eucalyptus", 720, new Date("2026-08-29T12:00:00.000Z"), "live_issued_race", "show-a", "completion", "evidence:result-1", "history:2026-08-01", 3, "0000000000000000000000000000000000000000000000000000000000000001", "result:ba68c257dab6ddf1c979b1aca34628c64ea8d165cd711001143ec01f31a58ae9", 1],
    [2, 40100, "underhand", 275, "eucalyptus", 720, new Date("2025-08-29T12:00:00.000Z"), "historical_import", "authority-a", "completion", "evidence:result-2", "history:2026-08-01", 3, "0000000000000000000000000000000000000000000000000000000000000002", "result:c12f99cd4cbecd683bdb3d6c0898f0307d9aceb5a0f21f6a12f2579dec5f2259", 1],
    [3, 36500, "standing_block", 300, "eucalyptus", 720, new Date("2024-08-29T12:00:00.000Z"), "live_issued_race", "legacy-a", "completion", "evidence:result-3", "history:2026-08-01", 3, "0000000000000000000000000000000000000000000000000000000000000003", "result:8397fc31ed467308cb9eeb8cd575c9479e35e0bb64888754643244037a6b6001", 1],
  ];
  inputStyle(inputs.getRange("A13:P15"));
  inputs.getRange("G13:G15").format.numberFormat = "yyyy-mm-dd hh:mm";
  inputs.freezePanes.freezeRows(12);
  inputs.getRange("A:P").format.columnWidth = 17;
  inputs.getRange("C:C").format.columnWidth = 22;
  inputs.getRange("E:E").format.columnWidth = 24;
  inputs.getRange("G:G").format.columnWidth = 22;
  inputs.getRange("H:H").format.columnWidth = 29;
  inputs.getRange("I:I").format.columnWidth = 22;
  inputs.getRange("K:L").format.columnWidth = 24;
  inputs.getRange("N:N").format.columnWidth = 68;
  inputs.getRange("H:H").format.columnWidth = 68;
  inputs.getRange("O:O").format.columnWidth = 72;

  title(governor, "A1:Q1", "Signed Evidence Governor Projection — Caller Labels Are Not Accepted");
  governor.getRange("A2:Q2").merge();
  governor.getRange("A2:Q2").values = [["The externally trusted P-256 seal binds the exact packet, epoch identity/content/member set, cutoff, tournament authority, raw result, and admission provenance before Formula facts are derived."]];
  governor.getRange("A2:Q2").format = { fill: BLUE, wrapText: true, rowHeight: 34 };
  governor.getRange("A4:Q4").values = [["Seq", "Evidence ID", "Age days", "Quality", "Tournament relevance", "Numeric eligible", "Source SHA-256", "Governor receipt SHA-256", "Admission reason", "Result key", "Revision", "Source seq", "Raw time ms", "Authority kind", "Epoch member bound", "Packet SHA-256", "Signed body SHA-256"]];
  header(governor.getRange("A4:Q4"));
  for (let row = 5; row <= 7; row += 1) {
    const src = row + 8;
    governor.getRange(`A${row}:B${row}`).formulas = [[`='Inputs'!A${src}`, `='Inputs'!K${src}`]];
    governor.getRange(`C${row}`).formulas = [[`='Inputs'!$E$4-'Inputs'!G${src}`]];
    governor.getRange(`D${row}`).formulas = [[`=IF('Inputs'!H${src}="live_issued_race","issued_official","verified_historical")`]];
    governor.getRange(`E${row}`).formulas = [[`=IF('Inputs'!I${src}='Inputs'!$E$5,"active",IF('Inputs'!I${src}='Inputs'!$E$6,"other_authoritative",IF('Inputs'!I${src}='Inputs'!$E$7,"legacy","REVIEW_BLOCKED")))`]];
    governor.getRange(`F${row}:H${row}`).formulas = [[`=IF('Inputs'!J${src}="completion",1,0)`, `='Inputs'!N${src}`, "='Inputs'!$E$10"]];
    governor.getRange(`I${row}`).formulas = [[`=IF(F${row}=0,"status_ineligible",IF('Inputs'!H${src}="historical_import","historical_cutover","eligible_completion"))`]];
    governor.getRange(`J${row}:M${row}`).formulas = [[`='Inputs'!O${src}`, `='Inputs'!P${src}`, `='Inputs'!A${src}`, `='Inputs'!B${src}`]];
    governor.getRange(`N${row}`).formulas = [[`=IF('Inputs'!H${src}="historical_import","historical_cutover","live_issued_field")`]];
    governor.getRange(`O${row}`).formulas = [[`=AND(J${row}='Inputs'!O${src},K${row}='Inputs'!P${src},L${row}='Inputs'!A${src},F${row}=1,'Inputs'!M${src}='Inputs'!$H$8)`]];
    governor.getRange(`P${row}:Q${row}`).formulas = [["='Inputs'!$H$6", "='Inputs'!$H$5"]];
  }
  formulaStyle(governor.getRange("A5:Q7"));
  governor.getRange("C5:C7").format.numberFormat = "0.000000";
  governor.freezePanes.freezeRows(4);
  governor.getRange("A:Q").format.columnWidth = 22;
  governor.getRange("G:J").format.columnWidth = 68;
  governor.getRange("P:Q").format.columnWidth = 68;

  title(transforms, "A1:S1", "Target-Context Log-Seconds Transformation and Frozen Weight Ladder");
  transforms.getRange("A3:S3").values = [["Seq", "Raw ms", "Event factor", "Size factor", "Material factor", "Transformed seconds", "Log seconds", "Relation", "Context", "Diameter similarity", "Recency", "Quality", "Tournament", "Conversion variance", "Weight", "Supported", "Source diameter", "Target diameter", "Robust residual contribution"]];
  header(transforms.getRange("A3:S3"));
  for (let row = 4; row <= 6; row += 1) {
    const src = row + 9;
    const gov = row + 1;
    transforms.getRange(`A${row}:B${row}`).formulas = [[`='Inputs'!A${src}`, `='Inputs'!B${src}`]];
    transforms.getRange(`C${row}`).formulas = [[`=IF('Inputs'!C${src}='Inputs'!$B$4,1,VLOOKUP('Inputs'!$B$4,'Assumptions'!$D$5:$G$7,2,FALSE)/VLOOKUP('Inputs'!C${src},'Assumptions'!$D$5:$G$7,2,FALSE))`]];
    transforms.getRange(`D${row}`).formulas = [[`=('Inputs'!$B$5/'Inputs'!D${src})^VLOOKUP('Inputs'!$B$4,'Assumptions'!$D$5:$G$7,3,FALSE)`]];
    transforms.getRange(`E${row}`).formulas = [[`=IF('Inputs'!E${src}='Inputs'!$B$6,1,('Inputs'!$B$7/'Inputs'!F${src})^'Assumptions'!$B$20)`]];
    transforms.getRange(`F${row}`).formulas = [[`=B${row}/1000*C${row}*D${row}*E${row}`]];
    transforms.getRange(`G${row}`).formulas = [[`=LN(F${row})`]];
    transforms.getRange(`H${row}`).formulas = [[`=IF('Inputs'!C${src}='Inputs'!$B$4,"exact_event",IF(VLOOKUP('Inputs'!C${src},'Assumptions'!$D$5:$G$7,4,FALSE)=VLOOKUP('Inputs'!$B$4,'Assumptions'!$D$5:$G$7,4,FALSE),"same_discipline","cross_discipline"))`]];
    transforms.getRange(`I${row}`).formulas = [[`=IF(AND(H${row}="exact_event",'Inputs'!D${src}='Inputs'!$B$5,'Inputs'!E${src}='Inputs'!$B$6),'Assumptions'!$B$10,IF(OR(H${row}="exact_event",H${row}="same_discipline"),'Assumptions'!$B$11,'Assumptions'!$B$12))`]];
    transforms.getRange(`J${row}`).formulas = [[`=EXP(-'Assumptions'!$B$13*ABS(LN('Inputs'!D${src}/'Inputs'!$B$5)))`]];
    transforms.getRange(`K${row}`).formulas = [[`=2^(-'Governor Projection'!C${gov}/'Assumptions'!$B$14)`]];
    transforms.getRange(`L${row}`).formulas = [[`=IF('Governor Projection'!D${gov}="issued_official",'Assumptions'!$B$15,'Assumptions'!$B$16)`]];
    transforms.getRange(`M${row}`).formulas = [[`=IF('Governor Projection'!E${gov}="active",'Assumptions'!$B$17,IF('Governor Projection'!E${gov}="other_authoritative",'Assumptions'!$B$18,'Assumptions'!$B$19))`]];
    transforms.getRange(`N${row}`).formulas = [[`=IF(H${row}="same_discipline",'Assumptions'!$E$11,IF(H${row}="cross_discipline",'Assumptions'!$E$12,0))+('Assumptions'!$E$13*ABS(LN('Inputs'!D${src}/'Inputs'!$B$5)))^2+('Assumptions'!$E$14*ABS(LN(E${row})))^2`]];
    transforms.getRange(`O${row}`).formulas = [[`=I${row}*J${row}*K${row}*L${row}*M${row}/(1+N${row})`]];
    transforms.getRange(`P${row}`).formulas = [[`='Governor Projection'!F${gov}`]];
    transforms.getRange(`Q${row}:R${row}`).formulas = [[`='Inputs'!D${src}`, "='Inputs'!$B$5"]];
    transforms.getRange(`S${row}`).formulas = [[`=O${row}*MIN(ABS(G${row}-'Distribution'!$B$4),'Assumptions'!$B$5*'IRLS Trace'!$T$13)^2`]];
  }
  formulaStyle(transforms.getRange("A4:S6"));
  transforms.getRange("C4:O6").format.numberFormat = "0.0000000000";
  transforms.freezePanes.freezeRows(3);
  transforms.getRange("A:S").format.columnWidth = 17;
  transforms.getRange("H:H").format.columnWidth = 20;

  title(irls, "A1:W1", "Weighted Median/MAD Initialization and Every Huber IRLS Iteration");
  irls.getRange("A3:C3").values = [["Candidate", "Log seconds", "Base weight"]];
  header(irls.getRange("A3:C3"));
  irls.getRange("A4:A9").values = [["prior-1"], ["prior-2"], ["prior-3"], ["evidence-1"], ["evidence-2"], ["evidence-3"]];
  for (let row = 4; row <= 6; row += 1) irls.getRange(`B${row}:C${row}`).formulas = [["=LN('Assumptions'!$I$18)", "='Assumptions'!$P$6/'Assumptions'!$P$6"]];
  for (let row = 7; row <= 9; row += 1) irls.getRange(`B${row}:C${row}`).formulas = [[`='Transforms'!G${row - 3}`, `='Transforms'!O${row - 3}`]];
  formulaStyle(irls.getRange("A4:C9"));
  irls.getRange("S3:U3").values = [["Sorted value", "Sorted weight", "Cumulative weight"]];
  header(irls.getRange("S3:U3"));
  for (let row = 4; row <= 9; row += 1) {
    irls.getRange(`S${row}`).formulas = [[`=SMALL($B$4:$B$9,ROW()-3)`]];
    irls.getRange(`T${row}`).formulas = [[`=SUMIF($B$4:$B$9,S${row},$C$4:$C$9)/COUNTIF($B$4:$B$9,S${row})`]];
    irls.getRange(`U${row}`).formulas = [[`=SUM($T$4:T${row})`]];
  }
  irls.getRange("S11:T13").values = [["Initialization", "Calculated value"], ["Weighted median", null], ["Weighted MAD / scale", null]];
  header(irls.getRange("S11:T11"));
  irls.getRange("T12").formulas = [["=MINIFS($S$4:$S$9,$U$4:$U$9,\">=\"&SUM($C$4:$C$9)/2)"]];
  irls.getRange("T13").formulas = [["=MAX('Assumptions'!$B$9,'Assumptions'!$B$8*MINIFS($S$15:$S$20,$U$15:$U$20,\">=\"&SUM($C$4:$C$9)/2))"]];
  irls.getRange("S14:W14").values = [["Sorted absolute residual", "Weight", "Cumulative", "Raw absolute residual", "Raw weight"]];
  header(irls.getRange("S14:W14"));
  for (let row = 15; row <= 20; row += 1) {
    const sourceRow = row - 11;
    irls.getRange(`V${row}:W${row}`).formulas = [[`=ABS($B$${sourceRow}-$T$12)`, `=$C$${sourceRow}`]];
    irls.getRange(`S${row}`).formulas = [[`=SMALL($V$15:$V$20,ROW()-14)`]];
    irls.getRange(`T${row}`).formulas = [[`=SUMIF($V$15:$V$20,S${row},$W$15:$W$20)/COUNTIF($V$15:$V$20,S${row})`]];
    irls.getRange(`U${row}`).formulas = [[`=SUM($T$15:T${row})`]];
  }
  irls.getRange("A23:N23").values = [["Iteration", "Start center", "Scale", "Prior 1 eff", "Prior 2 eff", "Prior 3 eff", "Evidence 1 eff", "Evidence 2 eff", "Evidence 3 eff", "Total eff", "End center", "Delta", "Active", "Active iteration"]];
  header(irls.getRange("A23:N23"));
  for (let row = 24; row <= 43; row += 1) {
    const iteration = row - 23;
    irls.getRange(`A${row}`).values = [[iteration]];
    irls.getRange(`B${row}`).formulas = [[iteration === 1 ? "=$T$12" : `=K${row - 1}`]];
    irls.getRange(`C${row}`).formulas = [["=$T$13"]];
    for (let col = 4; col <= 9; col += 1) {
      const sourceRow = col;
      const letter = String.fromCharCode(64 + col);
      irls.getRange(`${letter}${row}`).formulas = [[`=$C$${sourceRow}*IF(ABS(($B$${sourceRow}-$B${row})/$C${row})<='Assumptions'!$B$5,1,'Assumptions'!$B$5/ABS(($B$${sourceRow}-$B${row})/$C${row}))`]];
    }
    irls.getRange(`J${row}`).formulas = [[`=SUM(D${row}:I${row})`]];
    irls.getRange(`K${row}`).formulas = [[`=(D${row}*$B$4+E${row}*$B$5+F${row}*$B$6+G${row}*$B$7+H${row}*$B$8+I${row}*$B$9)/J${row}`]];
    irls.getRange(`L${row}`).formulas = [[`=ABS(K${row}-B${row})`]];
    irls.getRange(`M${row}`).formulas = [[iteration === 1 ? "=1" : `=IF(L${row - 1}>'Assumptions'!$B$7,1,0)`]];
    irls.getRange(`N${row}`).formulas = [[`=IF(M${row}=1,A${row},0)`]];
  }
  formulaStyle(irls.getRange("A24:N43"));
  irls.getRange("B4:W43").format.numberFormat = "0.0000000000";
  irls.freezePanes.freezeRows(23);
  irls.getRange("A:W").format.columnWidth = 16;
  irls.getRange("V:W").format.columnWidth = 18;
  irls.getRange("A:A").format.columnWidth = 18;

  title(distribution, "A1:H1", "Lognormal Predictive Distribution — Independently Formula-Driven");
  distribution.getRange("A3:B3").values = [["Component", "Calculated value"]];
  header(distribution.getRange("A3:B3"));
  distribution.getRange("A4:A13").values = [["Final log center"], ["Personal weight"], ["Effective sample size"], ["Robust residual variance"], ["Weighted conversion variance"], ["Prior variance"], ["Scarcity inflation"], ["Predictive log sigma"], ["Median ms"], ["Central 90% half-width ms"]];
  distribution.getRange("B4").formulas = [["=INDEX('IRLS Trace'!$K$24:$K$43,MATCH(MAX('IRLS Trace'!$N$24:$N$43),'IRLS Trace'!$N$24:$N$43,0))"]];
  distribution.getRange("B5").formulas = [["=SUM('Transforms'!O4:O6)"]];
  distribution.getRange("B6").formulas = [["=B5^2/SUMSQ('Transforms'!O4:O6)"]];
  distribution.getRange("B7").formulas = [["=SUM('Transforms'!S4:S6)/B5"]];
  distribution.getRange("B8").formulas = [["=SUMPRODUCT('Transforms'!O4:O6,'Transforms'!N4:N6)/B5"]];
  distribution.getRange("B9").formulas = [["=INDEX('Assumptions'!$P$5:$P$10,'Assumptions'!$I$16-4)/(INDEX('Assumptions'!$P$5:$P$10,'Assumptions'!$I$16-4)+B5)*INDEX('Assumptions'!$O$5:$O$10,'Assumptions'!$I$16-4)"]];
  distribution.getRange("B10").formulas = [["=1+1/MAX(B6,0.25)"]];
  distribution.getRange("B11").formulas = [["=MAX('Assumptions'!$E$18,MIN('Assumptions'!$E$19,SQRT((B7+B8+B9)*B10)))"]];
  distribution.getRange("B12").formulas = [["=ROUND(EXP(B4)*1000,0)"]];
  distribution.getRange("B13").formulas = [["=(G8-G4)/2"]];
  formulaStyle(distribution.getRange("A4:B13"));
  distribution.getRange("D3:H3").values = [["Probability", "Z score", "Unbounded ms", "Bounded positive ms", "Formula dependency"]];
  header(distribution.getRange("D3:H3"));
  for (let row = 4; row <= 8; row += 1) {
    const source = row + 23;
    distribution.getRange(`D${row}:E${row}`).formulas = [[`='Assumptions'!D${source}`, `='Assumptions'!E${source}`]];
    distribution.getRange(`F${row}`).formulas = [[`=ROUND(EXP($B$4+E${row}*$B$11)*1000,0)`]];
    distribution.getRange(`G${row}`).formulas = [[`=MAX('Assumptions'!$E$20,MIN('Assumptions'!$E$21,F${row}))`]];
    distribution.getRange(`H${row}`).formulas = [[`="Inputs->Governor->Transforms->IRLS("&MAX('IRLS Trace'!$N$24:$N$43)&")->Distribution"`]];
  }
  formulaStyle(distribution.getRange("D4:H8"));
  distribution.getRange("D11:E11").values = [["Canonical audit projection", "Formula-derived value"]];
  header(distribution.getRange("D11:E11"));
  distribution.getRange("D12:D16").values = [["Input row 1"], ["Input row 2"], ["Input row 3"], ["Governor receipt"], ["Prior lineage"]];
  for (let row = 12; row <= 14; row += 1) {
    const src = row + 1;
    const gov = row - 7;
    distribution.getRange(`E${row}`).formulas = [[`='Inputs'!A${src}&"|"&'Inputs'!B${src}&"|"&'Inputs'!C${src}&"|"&'Inputs'!D${src}&"|"&'Governor Projection'!C${gov}&"|"&'Governor Projection'!D${gov}&"|"&'Governor Projection'!E${gov}`]];
  }
  distribution.getRange("E15").formulas = [["='Inputs'!$E$10"]];
  distribution.getRange("E16").formulas = [["='Assumptions'!$I$20"]];
  for (let row = 12; row <= 16; row += 1) distribution.getRange(`E${row}:H${row}`).merge();
  formulaStyle(distribution.getRange("D12:H16"));
  distribution.getRange("E12:H16").format.wrapText = true;
  distribution.freezePanes.freezeRows(3);
  distribution.getRange("A:H").format.columnWidth = 22;
  distribution.getRange("A:A").format.columnWidth = 30;
  distribution.getRange("H:H").format.columnWidth = 55;

  for (const sheet of sheets) sheet.getRange("A1:W50").format.font.name = "Aptos";
  return { wb, sheets: [
    ["Assumptions", "A1:Q31"], ["Inputs", "A1:P15"],
    ["Governor Projection", "A1:Q7"], ["Transforms", "A1:S6"],
    ["IRLS Trace", "A1:W43"], ["Distribution", "A1:H16"],
  ], inputs, distribution };
}

async function normalizeXlsx(rawBytes) {
  const archive = await JSZip.loadAsync(rawBytes);
  const relPath = "xl/_rels/workbook.xml.rels";
  let relationships = await archive.file(relPath).async("string");
  const ids = new Map();
  relationships = relationships.replace(/Target="([^"]+)" Id="([^"]+)"/g, (matched, target, oldId) => {
    const sheetMatch = target.match(/sheet(\d+)\.xml/);
    const stable = sheetMatch ? `R_sheet${sheetMatch[1]}` : target.includes("styles.xml") ? "R_styles" : target.includes("theme1.xml") ? "R_theme" : "R_shared_strings";
    ids.set(oldId, stable);
    return `Target="${target}" Id="${stable}"`;
  });
  archive.file(relPath, relationships);
  let workbookXml = await archive.file("xl/workbook.xml").async("string");
  for (const [oldId, stable] of ids) workbookXml = workbookXml.replaceAll(oldId, stable);
  archive.file("xl/workbook.xml", workbookXml);
  let rootRelationships = await archive.file("_rels/.rels").async("string");
  rootRelationships = rootRelationships.replace(/Id="[^"]+"/, 'Id="R_root_workbook"');
  archive.file("_rels/.rels", rootRelationships);
  const fixedDate = new Date("2000-01-01T00:00:00.000Z");
  for (const entry of Object.values(archive.files)) entry.date = fixedDate;
  return Buffer.from(await archive.generateAsync({ type: "uint8array", compression: "DEFLATE", compressionOptions: { level: 9 }, platform: "DOS" }));
}

async function formulaGraphDigest(bytes) {
  const archive = await JSZip.loadAsync(bytes);
  const graph = [];
  const sheets = Object.keys(archive.files).filter((name) => /^xl\/worksheets\/sheet\d+\.xml$/.test(name)).sort();
  for (const name of sheets) {
    const xml = await archive.file(name).async("string");
    const cells = [...xml.matchAll(/<(?:x:)?c\b[^>]*\br="([^"]+)"[^>]*>[\s\S]*?<\/(?:x:)?c>/g)];
    for (const cell of cells) {
      const formula = cell[0].match(/<(?:x:)?f[^>]*>([\s\S]*?)<\/(?:x:)?f>/);
      if (formula) graph.push([name, cell[1], formula[1]]);
    }
  }
  return digestValue(graph);
}

async function toolVersion() {
  const packagePath = path.join(moduleRoot, "@oai", "artifact-tool", "package.json");
  return JSON.parse(await fs.readFile(packagePath, "utf8")).version;
}

async function exerciseEngine() {
  const { wb, sheets, inputs, distribution } = buildWorkbook();
  const inspections = {};
  const renderHashes = {};
  for (const [name, range] of sheets) {
    const inspected = await wb.inspect({ kind: "table", range: `${name}!${range}`, include: "values,formulas", tableMaxRows: 50, tableMaxCols: 23, maxChars: 40000 });
    inspections[name] = sha256(Buffer.from(inspected.ndjson, "utf8"));
    const preview = await wb.render({ sheetName: name, range, scale: 1.25, format: "png" });
    const previewBytes = Buffer.from(await preview.arrayBuffer());
    renderHashes[name] = sha256(previewBytes);
    await fs.writeFile(path.join(previewDir, `${name.replaceAll(" ", "_")}.png`), previewBytes);
  }
  const errorReport = await wb.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!", options: { useRegex: true, maxResults: 300 }, summary: "formula error scan" });
  if (/"matchCount":\s*[1-9]/.test(errorReport.ndjson)) throw new Error(`workbook formula errors: ${errorReport.ndjson}`);
  await wb.inspect({ kind: "table", range: "Distribution!B4:B13", include: "values,formulas", tableMaxRows: 20, tableMaxCols: 2, maxChars: 4000 });
  const baseline = {
    log_center: distribution.getRange("B4").values[0][0],
    log_scale: distribution.getRange("B11").values[0][0],
    center_ms: distribution.getRange("B12").values[0][0],
    uncertainty_ms: distribution.getRange("B13").values[0][0],
  };
  inputs.getRange("B13").values = [[43200]];
  for (const [sheetName, range] of [
    ["Assumptions", "I14:I20"], ["Governor Projection", "A5:I7"],
    ["Transforms", "A4:S6"], ["IRLS Trace", "B4:C9"],
    ["IRLS Trace", "S4:U9"], ["IRLS Trace", "T12:T13"],
    ["IRLS Trace", "S15:W20"], ["IRLS Trace", "B24:N43"],
    ["Distribution", "B4:B13"], ["Distribution", "D4:H8"],
    ["Distribution", "E12:E16"],
  ]) {
    const rangeObject = wb.worksheets.getItem(sheetName).getRange(range);
    rangeObject.formulas = rangeObject.formulas;
  }
  await wb.inspect({ kind: "table", range: "Distribution!B4:B13", include: "values,formulas", tableMaxRows: 20, tableMaxCols: 2, maxChars: 4000 });
  const mutated = {
    center_ms: distribution.getRange("B12").values[0][0],
    uncertainty_ms: distribution.getRange("B13").values[0][0],
  };
  inputs.getRange("B13").values = [[38200]];
  for (const [sheetName, range] of [
    ["Assumptions", "I14:I20"], ["Governor Projection", "A5:I7"],
    ["Transforms", "A4:S6"], ["IRLS Trace", "B4:C9"],
    ["IRLS Trace", "S4:U9"], ["IRLS Trace", "T12:T13"],
    ["IRLS Trace", "S15:W20"], ["IRLS Trace", "B24:N43"],
    ["Distribution", "B4:B13"], ["Distribution", "D4:H8"],
    ["Distribution", "E12:E16"],
  ]) {
    const rangeObject = wb.worksheets.getItem(sheetName).getRange(range);
    rangeObject.formulas = rangeObject.formulas;
  }
  await wb.inspect({ kind: "table", range: "Distribution!B4:B13", include: "values,formulas", tableMaxRows: 20, tableMaxCols: 2, maxChars: 4000 });
  const restored = {
    log_center: distribution.getRange("B4").values[0][0],
    log_scale: distribution.getRange("B11").values[0][0],
    center_ms: distribution.getRange("B12").values[0][0],
    uncertainty_ms: distribution.getRange("B13").values[0][0],
  };
  if (canonical(baseline) !== canonical(restored) || baseline.center_ms === mutated.center_ms) {
    const selected = wb.worksheets.getItem("Assumptions").getRange("I14:I20").values;
    const transformed = wb.worksheets.getItem("Transforms").getRange("F4:O6").values;
    throw new Error(`artifact-tool mutation/recalculation/restore proof failed: ${canonical({ baseline, mutated, restored, selected, transformed })}`);
  }
  const output = await SpreadsheetFile.exportXlsx(wb);
  const rawPath = path.join(workDir, "formula_golden.raw.xlsx");
  await output.save(rawPath);
  const bytes = await normalizeXlsx(await fs.readFile(rawPath));
  await fs.unlink(rawPath);
  const inputProjection = {
    target: ["underhand", 300, "eucalyptus", 720],
    cutoff: "2026-08-29T12:00:00.000Z",
    raw_ms: [38200, 40100, 36500],
    occurred_at_utc: ["2026-08-29T12:00:00.000Z", "2025-08-29T12:00:00.000Z", "2024-08-29T12:00:00.000Z"],
    sources: ["live_issued_race", "historical_import", "live_issued_race"],
    tournaments: ["show-a", "authority-a", "legacy-a"],
    evidence_packet_digest: "18e7c2b3e7ed1e945d9b89edfd591a7cfa8efcb19d69af4f82a064459af97735",
    epoch_content_digest: "9a37db8ae45d0118017df8ea5cc087f85b528d7d2b4983588fac7e8671b7c849",
    signed_manifest_body_digest: "152e1ad207952255eacc67b8278507829d2cdad23fe04a0fbc2f7a885f7fccb3",
    signer_key_id: "integrity-key:formula-test",
  };
  return {
    bytes, workbook_sha256: sha256(bytes), formula_graph_sha256: await formulaGraphDigest(bytes),
    baseline_inputs_sha256: digestValue(inputProjection), baseline, mutated, restored,
    mutation: { cell: "Inputs!B13", before: 38200, after: 43200, restored: 38200 },
    inspections, renderHashes, formula_error_count: 0,
  };
}

const result = await exerciseEngine();
const builderSha = sha256(await fs.readFile(scriptPath));
const receiptContent = {
  schema_version: "strathmark-v3-formula-engine-verification-v1",
  artifact_tool_version: await toolVersion(),
  node_version: process.version,
  builder_sha256: builderSha,
  workbook_sha256: result.workbook_sha256,
  formula_graph_sha256: result.formula_graph_sha256,
  baseline_inputs_sha256: result.baseline_inputs_sha256,
  governor_binding_sha256: digestValue({
    evidence_packet_digest: "18e7c2b3e7ed1e945d9b89edfd591a7cfa8efcb19d69af4f82a064459af97735",
    epoch_content_digest: "9a37db8ae45d0118017df8ea5cc087f85b528d7d2b4983588fac7e8671b7c849",
    signed_manifest_body_digest: "152e1ad207952255eacc67b8278507829d2cdad23fe04a0fbc2f7a885f7fccb3",
    signer_key_id: "integrity-key:formula-test",
  }),
  manifest_digest: "2c58a9527c77a33e0b813fe938db44c6298ac0ea8b543a199b720d97baaf1354",
  baseline_outputs: result.baseline,
  mutation_outputs: result.mutated,
  restored_outputs: result.restored,
  mutation: result.mutation,
  formula_error_count: result.formula_error_count,
  inspection_sha256: result.inspections,
  render_sha256: result.renderHashes,
};
const receipt = { ...receiptContent, receipt_digest: digestValue(receiptContent) };

if (mode === "--build") {
  await fs.writeFile(workbookPath, result.bytes);
  await fs.writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
  console.log(JSON.stringify({ status: "built", workbook: workbookPath, receipt: receiptPath, ...receipt }, null, 2));
} else if (mode === "--verify") {
  const expectedReceipt = JSON.parse(await fs.readFile(receiptPath, "utf8"));
  const expectedBytes = await fs.readFile(workbookPath);
  if (canonical(expectedReceipt) !== canonical(receipt)) throw new Error("engine verification receipt mismatch");
  if (!expectedBytes.equals(result.bytes)) throw new Error("canonical workbook bytes differ from independent rebuild");
  console.log(JSON.stringify({ status: "verified", ...receipt }, null, 2));
} else {
  throw new Error(`unsupported mode ${mode}`);
}
