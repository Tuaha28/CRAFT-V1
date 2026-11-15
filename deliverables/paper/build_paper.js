// CRAFT research paper (Word), laid out like the reference heart-failure paper:
// one-column title block, two-column body, wide figure/tables full width.
// Citations are written as {key}; numbers follow order of first use (IEEE).
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, ImageRun, Table, TableRow, TableCell,
  AlignmentType, WidthType, BorderStyle, ShadingType, SectionType, LevelFormat,
  VerticalAlign,
} = require("docx");

const FIG = process.argv[2];
const OUT = process.argv[3];
const FONT = "Times New Roman";

// ---------------------------------------------------------------- references
const REFS = {
  hailemariam: 'S. Hailemariam, "Scheduling problems in the apparel industries: a systematic literature review," Res. J. Text. Apparel, pp. 1–20, 2026, doi: 10.1108/RJTA-09-2025-0213.',
  bao: 'N. Bao, X. Zheng, Y. Fan, A. Simeone, and R. Bao, "Enhancing garment manufacturing efficiency through human-centered scheduling," Prod. Eng., vol. 19, no. 5, pp. 885–898, 2025, doi: 10.1007/s11740-025-01349-8.',
  dauzere: 'S. Dauzère-Pérès, J. Ding, L. Shen, and K. Tamssaouet, "The flexible job shop scheduling problem: A review," Eur. J. Oper. Res., vol. 314, no. 2, pp. 409–432, 2024, doi: 10.1016/j.ejor.2023.05.017.',
  andrade: 'J. Andrade-Pineda, D. Canca, P. Gonzalez-R, M. Calle, and J. Leon-Blanco, "A two-stage constraint programming-based heuristic for the dual-resource flexible job shop problem," Comput. Oper. Res., vol. 194, Art. no. 107608, 2026, doi: 10.1016/j.cor.2026.107608.',
  perroux: 'T. Perroux, T. Arbaoui, K. Hadj-Hamou, L. Merghem-Boulahia, and J. Regazzoni, "Scheduling a dual-resource flexible job-shop with sequence-dependent setup times: an industrial case study in garment manufacturing," Int. J. Prod. Res., pp. 1–23, 2026, doi: 10.1080/00207543.2026.2711058.',
  barak: 'S. Barak, S. Javanmard, and R. Moghdani, "Dual resource constrained flexible job shop scheduling with sequence-dependent setup time," Expert Syst., vol. 41, no. 10, Art. no. e13669, 2024, doi: 10.1111/exsy.13669.',
  huang: 'Z. Huang, S. Guo, J. Zhang, G. Bao, J. Yang, and L. Wang, "A two-stage multi-objective evolutionary algorithm for the dual-resource constrained flexible job shop scheduling problem with variable sublots," Processes, vol. 13, no. 2, Art. no. 487, 2025, doi: 10.3390/pr13020487.',
  liu: 'C. Liu, T. Yuan, W. Song, Z. Lin, Y. Wu, S. Chen, L. Gao, and H. Yin, "Multi-objective flexible job shop scheduling with worker-number decisions using preference-conditioned heterogeneous graph reinforcement learning," Adv. Eng. Inform., vol. 77, Art. no. 105274, 2027, doi: 10.1016/j.aei.2026.105274.',
  naderi: 'B. Naderi, R. Ruiz, and V. Roshanaei, "Mixed-integer programming vs. constraint programming for shop scheduling problems: New results and outlook," INFORMS J. Comput., vol. 35, no. 4, pp. 817–843, 2023, doi: 10.1287/ijoc.2023.1287.',
  laborie: 'P. Laborie, J. Rogerie, P. Shaw, and P. Vilím, "IBM ILOG CP optimizer for scheduling," Constraints, vol. 23, no. 2, pp. 210–250, 2018, doi: 10.1007/s10601-018-9281-x.',
  cpsatlp: 'L. Perron, F. Didier, and S. Gay, "The CP-SAT-LP solver," in Proc. 29th Int. Conf. Principles and Practice of Constraint Programming (CP 2023), LIPIcs, vol. 280, pp. 3:1–3:2, 2023, doi: 10.4230/LIPIcs.CP.2023.3.',
  cpsat: 'L. Perron and F. Didier, "CP-SAT," OR-Tools v9.15, Google. Accessed: Oct. 22, 2026. [Online]. Available: https://developers.google.com/optimization/cp/cp_solver/',
  ortools: 'L. Perron and V. Furnon, "OR-Tools," v9.15, Google. Accessed: Oct. 22, 2026. [Online]. Available: https://developers.google.com/optimization/',
  kasapidis: 'G. Kasapidis, D. Paraskevopoulos, I. Mourtos, and P. Repoussis, "A unified solution framework for flexible job shop scheduling problems with multiple resource constraints," Eur. J. Oper. Res., vol. 320, no. 3, pp. 479–495, 2025, doi: 10.1016/j.ejor.2024.08.010.',
  yunusoglu: 'P. Yunusoglu and S. Topaloglu Yildiz, "Solving the flexible job shop scheduling and lot streaming problem with setup and transport resource constraints," Int. J. Syst. Sci. Oper. Logist., vol. 10, no. 1, Art. no. 2221072, 2023, doi: 10.1080/23302674.2023.2221072.',
  destouet: 'C. Destouet, H. Tlahig, B. Bettayeb, and B. Mazari, "Systematic review and future directions in dynamic flexible job shop scheduling: a decade of research," J. Intell. Manuf., vol. 37, no. 6, pp. 2295–2335, 2026, doi: 10.1007/s10845-025-02645-x.',
  zeitrag: 'Y. Zeiträg and J. Figueira, "A novel machine-learning rolling horizon heuristic for dynamic lot-sizing and job shop scheduling problems," Int. J. Prod. Res., vol. 63, no. 12, pp. 4563–4589, 2025, doi: 10.1080/00207543.2025.2453651.',
  panwalkar: 'S. S. Panwalkar and W. Iskander, "A survey of scheduling rules," Oper. Res., vol. 25, no. 1, pp. 45–61, 1977, doi: 10.1287/opre.25.1.45.',
  pinedo: 'M. L. Pinedo, Scheduling: Theory, Algorithms, and Systems, 6th ed. Cham, Switzerland: Springer, 2022, doi: 10.1007/978-3-031-05921-6.',
  jackson: 'J. R. Jackson, "Scheduling a production line to minimize maximum tardiness," Management Science Research Project, Univ. of California, Los Angeles, Res. Rep. 43, 1955.',
  smith: 'W. E. Smith, "Various optimizers for single-stage production," Nav. Res. Logist. Q., vol. 3, no. 1–2, pp. 59–66, 1956, doi: 10.1002/nav.3800030106.',
  moore: 'J. M. Moore, "An n job, one machine sequencing algorithm for minimizing the number of late jobs," Manage. Sci., vol. 15, no. 1, pp. 102–109, 1968, doi: 10.1287/mnsc.15.1.102.',
  baker: 'K. R. Baker, "Sequencing rules and due-date assignments in a job shop," Manage. Sci., vol. 30, no. 9, pp. 1093–1104, 1984, doi: 10.1287/mnsc.30.9.1093.',
  cheng: 'M. Cheng, N. J. Mukherjee, and S. C. Sarin, "A review of lot streaming," Int. J. Prod. Res., vol. 51, no. 23–24, pp. 7023–7046, 2013, doi: 10.1080/00207543.2013.774506.',
  taillard: 'E. Taillard, "Benchmarks for basic scheduling problems," Eur. J. Oper. Res., vol. 64, no. 2, pp. 278–285, 1993, doi: 10.1016/0377-2217(93)90182-M.',
  fips: 'National Institute of Standards and Technology, "Secure Hash Standard (SHS)," FIPS PUB 180-4, 2015, doi: 10.6028/NIST.FIPS.180-4.',
  ehrgott: 'M. Ehrgott, Multicriteria Optimization, 2nd ed. Berlin, Germany: Springer, 2005, doi: 10.1007/3-540-27659-9.',
  shaw: 'P. Shaw, "Using constraint programming and local search methods to solve vehicle routing problems," in Principles and Practice of Constraint Programming – CP98, Lecture Notes in Computer Science, vol. 1520, pp. 417–431, 1998, doi: 10.1007/3-540-49481-2_30.',
  pisinger: 'D. Pisinger and S. Ropke, "Large neighborhood search," in Handbook of Metaheuristics, 3rd ed., M. Gendreau and J.-Y. Potvin, Eds. Cham, Switzerland: Springer, 2019, pp. 99–127, doi: 10.1007/978-3-319-91086-4_4.',
  wilcoxon: 'F. Wilcoxon, "Individual comparisons by ranking methods," Biometrics Bull., vol. 1, no. 6, pp. 80–83, 1945, doi: 10.2307/3001968.',
};
const order = [];
function cite(text) {
  // "{a,b}" -> "[1], [2]"
  return text.replace(/\{([a-z0-9,]+)\}/g, (_, keys) => keys.split(",").map((k) => {
    if (!REFS[k]) throw new Error("unknown ref " + k);
    if (!order.includes(k)) order.push(k);
    return `[${order.indexOf(k) + 1}]`;
  }).join(", "));
}

// ---------------------------------------------------------------- helpers
const PENDING = "yellow";
// Inline markup: **bold**, *italic*, ==pending (highlighted)==, _<subscript>, ^<superscript>
function runs(text, base = {}) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|\*[^*]+\*|==[^=]+==|_<[^>]+>|\^<[^>]+>)/g;
  let last = 0, m;
  text = cite(text);
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), font: FONT, ...base }));
    const s = m[0];
    if (s.startsWith("**")) out.push(new TextRun({ text: s.slice(2, -2), bold: true, font: FONT, ...base }));
    else if (s.startsWith("==")) out.push(new TextRun({ text: s.slice(2, -2), highlight: PENDING, font: FONT, ...base }));
    else if (s.startsWith("_<")) out.push(new TextRun({ text: s.slice(2, -1), subScript: true, font: FONT, ...base }));
    else if (s.startsWith("^<")) out.push(new TextRun({ text: s.slice(2, -1), superScript: true, font: FONT, ...base }));
    else out.push(new TextRun({ text: s.slice(1, -1), italics: true, font: FONT, ...base }));
    last = m.index + s.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), font: FONT, ...base }));
  return out;
}
const P = (text, o = {}) => new Paragraph({
  children: runs(text, { size: o.size || 20 }),
  alignment: o.align || AlignmentType.JUSTIFIED,
  spacing: { after: o.after ?? 100, line: 240 },
  indent: o.indent === false ? undefined : { firstLine: o.firstLine ?? 284 },
  keepNext: o.keepNext,
});
const H1 = (text) => new Paragraph({ children: [new TextRun({ text, bold: true, font: FONT, size: 24 })], spacing: { before: 200, after: 100 }, keepNext: true });
const H2 = (text) => new Paragraph({ children: [new TextRun({ text, bold: true, font: FONT, size: 21 })], spacing: { before: 140, after: 80 }, keepNext: true });
const EQ = (text, no) => new Paragraph({
  children: [new TextRun({ text: "	", font: FONT }), ...runs(text, { size: 20, italics: true }), new TextRun({ text: `\t(${no})`, font: FONT, size: 20 })],
  tabStops: [{ type: "center", position: 2360 }, { type: "right", position: 4700 }],
  alignment: AlignmentType.LEFT, spacing: { before: 60, after: 100 },
});
const BULLET = (text) => new Paragraph({ children: runs(text, { size: 20 }), numbering: { reference: "bul", level: 0 }, alignment: AlignmentType.JUSTIFIED, spacing: { after: 60 } });
const CAPTION = (text) => new Paragraph({ children: runs(text, { size: 18, bold: true }), alignment: AlignmentType.CENTER, spacing: { before: 80, after: 160 } });
const TCAPTION = (text) => new Paragraph({ children: runs(text, { size: 18, bold: true }), alignment: AlignmentType.CENTER, spacing: { before: 160, after: 80 }, keepNext: true });

const B = { style: BorderStyle.SINGLE, size: 4, color: "000000" };
const NB = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
function table(header, rows, widths, opts = {}) {
  const total = widths.reduce((a, b) => a + b, 0);
  const cell = (t, i, head, shade) => new TableCell({
    width: { size: widths[i], type: WidthType.DXA },
    borders: { top: head ? B : NB, bottom: head ? B : NB, left: NB, right: NB },
    shading: shade ? { fill: shade, type: ShadingType.CLEAR, color: "auto" } : undefined,
    margins: { top: 30, bottom: 30, left: 70, right: 70 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      // head cells are bold; body cells keep **bold** markup (an explicit bold: false would override it)
      children: runs(String(t), head ? { size: opts.size || 17, bold: true } : { size: opts.size || 17 }), keepNext: true,
      alignment: i === 0 || (opts.leftCols || []).includes(i) ? AlignmentType.LEFT : AlignmentType.CENTER,
    })],
  });
  const body = rows.map((r, ri) => new TableRow({ cantSplit: true,
    children: r.map((t, i) => cell(t, i, false, (opts.shadeRow && opts.shadeRow(r, ri)) || (opts.shadeCol === i ? "EEF4E8" : null))),
  }));
  // bottom rule on the last row
  const lastIdx = body.length - 1;
  body[lastIdx] = new TableRow({
    children: rows[lastIdx].map((t, i) => new TableCell({
      width: { size: widths[i], type: WidthType.DXA },
      borders: { top: NB, bottom: B, left: NB, right: NB },
      shading: (opts.shadeRow && opts.shadeRow(rows[lastIdx], lastIdx)) ? { fill: opts.shadeRow(rows[lastIdx], lastIdx), type: ShadingType.CLEAR, color: "auto" }
        : opts.shadeCol === i ? { fill: "EEF4E8", type: ShadingType.CLEAR, color: "auto" } : undefined,
      margins: { top: 30, bottom: 30, left: 70, right: 70 },
      children: [new Paragraph({ children: runs(String(t), { size: opts.size || 17 }), keepNext: false, alignment: i === 0 || (opts.leftCols || []).includes(i) ? AlignmentType.LEFT : AlignmentType.CENTER })],
    })),
  });
  return new Table({
    width: { size: total, type: WidthType.DXA }, columnWidths: widths,
    alignment: AlignmentType.CENTER,
    rows: [new TableRow({ tableHeader: true, children: header.map((t, i) => cell(t, i, true, "E7ECF2")) }), ...body],
  });
}

// Pseudocode box in the reference papers' style: rules above and below, a
// bold title line, numbered lines indented by nesting level (keywords **bold**).
function ALGO(title, lines) {
  const rule = { style: BorderStyle.SINGLE, size: 8, color: "000000" };
  const thin = { style: BorderStyle.SINGLE, size: 4, color: "000000" };
  const para = (children, indent, after = 0) => new Paragraph({ children, keepNext: true, keepLines: true,
    indent: { left: indent, hanging: indent ? 300 : 0 }, spacing: { after, line: 228 } });
  const body = lines.map(([level, text], i) => para([new TextRun({ text: `${i + 1}:  `, font: FONT, size: 16 }),
    ...runs(text, { size: 17 })], 300 + level * 280));
  return new Table({
    width: { size: 4700, type: WidthType.DXA }, columnWidths: [4700],
    borders: { top: NB, bottom: NB, left: NB, right: NB, insideHorizontal: NB, insideVertical: NB },
    rows: [new TableRow({ cantSplit: true, children: [new TableCell({ width: { size: 4700, type: WidthType.DXA },
      borders: { top: rule, bottom: rule, left: NB, right: NB }, margins: { top: 40, bottom: 60, left: 60, right: 60 },
      children: [new Paragraph({ children: runs(title, { size: 18 }), keepNext: true, spacing: { after: 40 },
        border: { bottom: thin } }), ...body] })] })],
  });
}
const SPACER = () => new Paragraph({ children: [], spacing: { after: 80 } });

// A caption and its table inside one unsplittable frame, so Word never
// breaks a table across pages or column-balance breaks.
function framed(parts) {
  return [new Table({
    width: { size: 9864, type: WidthType.DXA }, columnWidths: [9864], alignment: AlignmentType.CENTER,
    borders: { top: NB, bottom: NB, left: NB, right: NB, insideHorizontal: NB, insideVertical: NB },
    rows: [new TableRow({ cantSplit: true, children: [new TableCell({ width: { size: 9864, type: WidthType.DXA },
      borders: { top: NB, bottom: NB, left: NB, right: NB }, margins: { top: 0, bottom: 0, left: 0, right: 0 },
      children: [...parts, new Paragraph({ children: [], spacing: { after: 0 } })] })] })],
  })];
}

// ---------------------------------------------------------------- results (Kaggle logs)
// paper_data.json is built by kaggle_results/stats.py from the notebook logs.
const D = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
const MAIN = ["FIFO", "EDD", "SPT", "EDD-Crew", "CRAFT V3"];
const f1 = (x) => x.toFixed(1), f2 = (x) => x.toFixed(2);
const pct = (a, b) => (100 * (a - b) / a);
const pending = (c) => c.key === "load90_set3" && !c.v31;   // CRAFT row waits for the V3.1 rerun
const done = D.cases.filter((c) => !pending(c));
const C = D.comparisons;
const pv = (p) => (p === null ? "–" : p < 0.001 ? "< 0.001" : p.toFixed(3));
const wtl = (x) => `${x.win}/${x.tie}/${x.loss}`;

const ALL = ["FIFO", "EDD", "SPT", "EDD-Crew", "Savable-First-Crew", "CP-SAT plan", "CRAFT V3"];
const SHORT = { "Savable-First-Crew": "Sav.-First", "CP-SAT plan": "CP-SAT plan" };
const bold = (x, best) => (best ? `**${x}**` : `${x}`);
// Lateness of a plan that leaves an order unfinished cannot be measured: shown as "–*".
const tard = (v) => (v.complete < 10 ? "–*" : f2(v.mean_tard));
const shownIn = (c) => ALL.filter((m) => !(m === "CRAFT V3" && pending(c)));
const cell = (c, m, text, best) => (m === "CRAFT V3" && pending(c) ? "==pending==" : bold(text, best));
// One row per case and one column per method: late orders, mean tardiness, makespan.
const lateRows = D.cases.map((c) => {
  const best = Math.min(...shownIn(c).map((m) => c.methods[m].late));
  return [c.label, c.ops, c.overdue, ...ALL.map((m) => cell(c, m, c.methods[m].late, c.methods[m].late === best))];
});
const tardRows = D.cases.map((c) => {
  const full = shownIn(c).filter((m) => c.methods[m].complete === 10);
  const best = Math.min(...full.map((m) => c.methods[m].mean_tard));
  return [c.label, ...ALL.map((m) => cell(c, m, tard(c.methods[m]), c.methods[m].complete === 10 && Math.abs(c.methods[m].mean_tard - best) < 1e-9)),
    pending(c) ? "==pending==" : c.start.replace("Savable-First-Crew", "Sav.-First")];
});
const mkRows = D.cases.map((c) => {
  const best = Math.min(...shownIn(c).map((m) => c.methods[m].makespan));
  return [c.label, ...ALL.map((m) => cell(c, m, f1(c.methods[m].makespan), Math.abs(c.methods[m].makespan - best) < 1e-9))];
});
// Totals over the evaluated cases: the headline of the results.
const CREW_SET = ["EDD-Crew", "Savable-First-Crew", "CP-SAT plan", "CRAFT V3"];
const tardCases = done.filter((c) => CREW_SET.every((m) => c.methods[m].complete === 10));
const overdueTotal = done.reduce((a, c) => a + c.overdue, 0);
const possible = 10 * done.length - overdueTotal;      // orders that could still be on time
const SUM = Object.fromEntries(ALL.map((m) => {
  const v = done.map((c) => c.methods[m]);
  const full = v.filter((x) => x.complete === 10);
  const late = v.reduce((a, x) => a + x.late, 0);
  return [m, { full: full.length, onTime: 10 * v.length - late, late, avoid: late - overdueTotal,
    tard: tardCases.every((c) => c.methods[m].complete === 10)
      ? tardCases.reduce((a, c) => a + c.methods[m].mean_tard, 0) / tardCases.length : null,
    makespan: v.reduce((a, x) => a + x.makespan, 0) / v.length }];
}));
const sumBest = (k, dir) => (dir < 0 ? Math.min : Math.max)(...ALL.map((m) => SUM[m][k]).filter((x) => x !== null));
const sumRows = ALL.map((m) => {
  const x = SUM[m];
  return [m, bold(`${x.full}/${done.length}`, x.full === sumBest("full", 1)),
    bold(`${x.onTime}/${possible}`, x.onTime === sumBest("onTime", 1)),
    bold(x.avoid, x.avoid === sumBest("avoid", -1)),
    x.tard === null ? "–*" : bold(f2(x.tard), Math.abs(x.tard - sumBest("tard", -1)) < 1e-9),
    bold(f1(x.makespan), Math.abs(x.makespan - sumBest("makespan", -1)) < 1e-9)];
});
const t5rows = ["FIFO", "EDD", "SPT", "EDD-Crew", "Savable-First-Crew", "CP-SAT plan"].map((m) => [m,
  wtl(C[m].late), pv(C[m].late.p), `${wtl(C[m].mean_tard)} (n = ${C[m].mean_tard.n})`, pv(C[m].mean_tard.p),
  f2(C[m].mean_tard.mean_other), f2(C[m].mean_tard.mean_craft)]);
// Kaggle runs of Nov. 2026 (results/sensitivity.py): Set 1 at each load under the paper's
// settings (A, a fourth repeat) and two other settings (B: 4 solver workers, 24 h commit;
// C: as B with 3,600 s improvement).
const SENS = JSON.parse(fs.readFileSync(path.join(path.dirname(process.argv[4]), "sensitivity.json"), "utf8")).cases;
const sens = (label, s) => SENS.find((c) => c.label === label && c.setting === s);
const run4 = (r) => sens(r.label, "A").methods["CRAFT V3"];
const t6rows = D.repeats.map((r) => [r.label, ...r.tard.map(f2), f2(run4(r).tard), [...r.late, run4(r).late].join(" / ")]);
const SENS_LABELS = D.repeats.map((r) => r.label);
const t8rows = SENS_LABELS.flatMap((l) => ["A", "B", "C"].map((s) => {
  const c = sens(l, s), m = c.methods;
  return [s === "A" ? l : "", s, f2(m["CRAFT V3"].tard), m["CRAFT V3"].late, f2(m["EDD-Crew"].tard), m["EDD-Crew"].late,
    f2(m["Savable-First-Crew"].tard), m["Savable-First-Crew"].late, f1(c.minutes)];
}));
const sensDev = SENS_LABELS.flatMap((l) => ["B", "C"].map((s) =>
  -pct(sens(l, "A").methods["CRAFT V3"].tard, sens(l, s).methods["CRAFT V3"].tard)));
const sensMin = (s) => SENS_LABELS.map((l) => sens(l, s).minutes);
const signed = (x) => (x < 0 ? `−${f1(-x)}` : `+${f1(x)}`);
const bestAtEveryLoad = ["A", "B", "C"].filter((s) => SENS_LABELS.every((l) =>
  ["A", "B", "C"].every((o) => sens(l, s).methods["CRAFT V3"].tard <= sens(l, o).methods["CRAFT V3"].tard)));

// Numbers quoted in the text, all derived from the data above.
const nCases = done.length;
const partialRules = D.cases.filter((c) => c.methods.EDD.complete < 10).length;
const ec = C["EDD-Crew"];
const tardGainEC = pct(ec.mean_tard.mean_other, ec.mean_tard.mean_craft);
const lateCraft = done.reduce((a, c) => a + c.methods["CRAFT V3"].late, 0);
const lateEC = done.reduce((a, c) => a + c.methods["EDD-Crew"].late, 0);
const mkLoss = done.filter((c) => c.methods["CRAFT V3"].makespan > c.methods["EDD-Crew"].makespan + 1e-9).length;
const mkWin = done.filter((c) => c.methods["CRAFT V3"].makespan < c.methods["EDD-Crew"].makespan - 1e-9).length;
const eddFull = done.filter((c) => c.methods.EDD.complete === 10);
const eddGain = eddFull.map((c) => pct(c.methods.EDD.mean_tard, c.methods["CRAFT V3"].mean_tard));
const rng = (xs) => `${f1(Math.min(...xs))}–${f1(Math.max(...xs))}`;
const repSpread = Math.max(0.01, Math.ceil(100 * Math.max(...D.repeats.map((r) => {
  const all = [...r.tard, run4(r).tard];
  return Math.max(...all) - Math.min(...all);
}))) / 100);
const hours = D.seconds.map((s) => s / 3600);
const sigLateEC = ec.late.p !== null && ec.late.p < 0.05;
const sigTardEC = ec.mean_tard.p !== null && ec.mean_tard.p < 0.05;
const lateLoss = Object.values(C).some((x) => x.late.loss > 0);
const startGain = done.map((c) => pct(c.methods[c.start].mean_tard, c.methods["CRAFT V3"].mean_tard));
const V3 = SUM["CRAFT V3"], EC = SUM["EDD-Crew"], SF = SUM["Savable-First-Crew"], CP = SUM["CP-SAT plan"], RULES = SUM.EDD;
const onTimeGain = pct(EC.onTime, V3.onTime) * -1;   // % more orders on time than EDD-Crew
const rulesPartial = done.filter((c) => c.methods.EDD.complete < 10).length;
// Cases where CRAFT V3 had higher tardiness than EDD-Crew, and how many more orders it kept on time there.
const tradeCases = tardCases.filter((c) => c.methods["CRAFT V3"].mean_tard > c.methods["EDD-Crew"].mean_tard + 1e-9);
// Cases where no method finished every order (90%-S3), and the makespan evidence.
const noneComplete = done.filter((c) => ALL.every((m) => c.methods[m].complete < 10));
const mkLonger = done.filter((c) => c.methods["CRAFT V3"].makespan > c.methods["EDD-Crew"].makespan + 1e-9);
const mkInherited = mkLonger.filter((c) => c.start === "Savable-First-Crew" && Math.abs(c.methods["CRAFT V3"].makespan - c.methods["Savable-First-Crew"].makespan) < 0.05);
const mkOther = mkLonger.filter((c) => !mkInherited.includes(c));
const tradeSaved = tradeCases.map((c) => c.methods["EDD-Crew"].late - c.methods["CRAFT V3"].late);
const tradeGap = tradeCases.map((c) => c.methods["CRAFT V3"].mean_tard - c.methods["EDD-Crew"].mean_tard);
const sf = C["Savable-First-Crew"], cp = C["CP-SAT plan"];
const maxOverdue = Math.max(...done.map((c) => c.overdue)), minOverdue = Math.min(...done.map((c) => c.overdue));
// Every comparative claim in the text is checked here: the build stops if new data contradicts one.
const claim = (ok, what) => { if (!ok) throw new Error("Paper claim no longer holds: " + what); };
claim(done.every((c) => ALL.every((m) => c.methods[m].late >= c.overdue)), "overdue orders are late under every method");
claim(done.every((c) => c.methods["CRAFT V3"].complete === 10 || noneComplete.includes(c)), "CRAFT completed every order wherever any method did");
claim(mkInherited.every((c) => c.methods["CRAFT V3"].late < c.methods["EDD-Crew"].late), "every inherited longer makespan came with fewer late orders");
claim(mkOther.every((c) => c.methods["CRAFT V3"].makespan < c.methods["Savable-First-Crew"].makespan), "other longer makespans are shorter than the Savable-First start");
claim(V3.onTime > EC.onTime && V3.onTime >= Math.max(...ALL.map((m) => SUM[m].onTime)), "CRAFT has the most orders on time");
claim(V3.avoid < EC.avoid && V3.avoid <= Math.min(...ALL.map((m) => SUM[m].avoid)), "CRAFT has the fewest avoidable late orders");
claim(Object.values(C).every((x) => x.late.loss === 0), "CRAFT never had more late orders than any method");
claim(tradeSaved.every((x) => x > 0), "every tardiness loss to EDD-Crew saved late orders");
claim(sf.late.win === 0 && sf.late.loss === 0 && sf.mean_tard.loss === 0, "CRAFT ties Savable-First on late orders and never has more tardiness");
claim(V3.makespan < SF.makespan, "CRAFT has a shorter makespan than Savable-First");
claim(V3.makespan < Math.min(SUM.FIFO.makespan, SUM.EDD.makespan, SUM.SPT.makespan) && V3.makespan > EC.makespan, "makespan ordering");
claim(cp.late.loss === 0, "CRAFT never has more late orders than its CP-SAT plan");
claim(done.every((c) => c.methods["CRAFT V3"].late === Math.min(...ALL.map((m) => c.methods[m].late))), "CRAFT has the fewest (or tied) late orders in every case");
claim(D.repeats.every((r) => r.late.every((x) => x === run4(r).late)), "the fourth run has the same late orders as runs 1-3");
claim(SENS.every((c) => c.methods["CRAFT V3"].complete === 10 && c.methods["CRAFT V3"].late === sens(c.label, "A").methods["CRAFT V3"].late), "settings B and C keep the late orders of A");
claim(SENS.every((c) => ["EDD-Crew", "Savable-First-Crew"].every((m) => c.methods["CRAFT V3"].tard < c.methods[m].tard && c.methods["CRAFT V3"].late <= c.methods[m].late)), "CRAFT beats both crew rules under every setting");
claim(bestAtEveryLoad.length === 0 && Math.max(...sensDev.map(Math.abs)) < 5, "no setting is best at every load, and settings shift the result by a few percent");
const scope = D.v31_done ? `the ${nCases} test cases` : `the ${nCases} test cases finished so far ==(90%-S3 awaits the V3.1 rerun)==`;

// ---------------------------------------------------------------- content
const titleBlock = [
  new Paragraph({ children: [new TextRun({ text: "CRAFT: A Constraint Programming Framework for Crew-Aware Garment Production Scheduling under Rotating Shifts", bold: true, font: FONT, size: 32 })], alignment: AlignmentType.CENTER, spacing: { after: 200 } }),
  new Paragraph({ children: runs("Muhammad Tuaha, muhammadtuaha094@gmail.com, ==[Department], [University], [City, Country]==", { size: 20 }), alignment: AlignmentType.LEFT, spacing: { after: 40 } }),
  new Paragraph({ children: runs("==[Co-author / Supervisor name], [email], [Department], [University], [City, Country]==", { size: 20 }), alignment: AlignmentType.LEFT, spacing: { after: 200 } }),
  new Paragraph({ children: [new TextRun({ text: "Abstract", bold: true, font: FONT, size: 22 })], spacing: { after: 80 } }),
  P(`Garment sewing floors schedule hundreds of operations across machines and workers whose availability follows rotating shifts and rest days, yet most factories still dispatch work with simple priority rules. This paper proposes CRAFT, a scheduling framework that builds the plan with Constraint Programming (CP-SAT) over rolling time windows and then improves it with CP-SAT large neighborhood search, choosing for each operation the machines, the workers and the crew size. A synthetic factory database was generated from real company records at three load levels (30%, 60% and 90%) and used to compare CRAFT V3 with five rule-based schedulers (FIFO, EDD, SPT and two crew-aware rules) under one independent physical validation. In ${scope}, ${overdueTotal} of the ${10 * nCases} orders were already past their due date when planning started, so at most ${possible} could be delivered on time. CRAFT V3 completed every order in every case where any method did and delivered ${V3.onTime} of these ${possible} on time, against ${EC.onTime} for crew-aware EDD, leaving ${V3.avoid} avoidable late orders against ${EC.avoid}; FIFO, EDD and SPT left an order unfinished in ${rulesPartial} of the ${nCases} cases. Against crew-aware EDD, CRAFT V3 had fewer late orders in ${ec.late.win} cases and never more (Wilcoxon p = ${pv(ec.late.p)}) and ${f1(tardGainEC)}% lower mean tardiness on average, while crew-aware EDD finished all work earlier because CRAFT serves the orders that can still be saved before the hopeless ones; where single-worker EDD finished every order, CRAFT V3 cut mean tardiness by ${rng(eddGain)}%. Repeated runs reproduced the results within ${f2(repSpread)} days. Exact CP, used inside a rolling and validated pipeline, is practical for garment scheduling and delivers more orders on time than the rules factories use today.`, { indent: false }),
  new Paragraph({ children: runs("**Keywords:** Garment Production Scheduling, Dual-Resource Flexible Job Shop, Constraint Programming, CP-SAT, Large Neighborhood Search, Adaptive Crew Size.", { size: 20 }), spacing: { before: 60, after: 200 } }),
];

const body1 = [
  H1("1. Introduction"),
  P("The apparel industry is labour-intensive and order-driven: many small orders with fixed delivery dates pass through a sewing floor where every operation needs both a suitable machine and a skilled operator {hailemariam}. Late delivery costs penalties and buyer confidence, so the daily question for a planner is which order, operation, machine and worker to run next. Recent studies show that the human side of this question (skills, shifts, crew size) has a direct effect on garment efficiency {bao}."),
  P("In scheduling terms this is a dual-resource constrained flexible job shop scheduling problem (DRC-FJSP), where operations are assigned to machines and workers at the same time {dauzere}, {andrade}. In garment plants it becomes harder still: operators work in three rotating shifts with weekly rest days, an operation can be split among a crew of several operators, and the order book changes every week. Exact methods are often dismissed for such problems, and factories fall back on dispatching rules such as First-In-First-Out (FIFO), Earliest Due Date (EDD) and Shortest Processing Time (SPT)."),
  P("This paper presents CRAFT, a scheduling framework that keeps an exact Constraint Programming (CP) model at its core and makes it practical through rolling time windows, warm starts and large neighborhood search. Its contributions are: (i) a CP-SAT model that jointly decides sequence, machine, workers and crew size under rotating shift calendars; (ii) a five-layer scheduler (problem modelling, initial solutions, rolling-window construction, neighbourhood improvement of two starts and selection of the better plan); (iii) a synthetic factory database calibrated from real company records at three load levels; and (iv) a fair evaluation in which every method is checked by the same independent physical validation."),
  P("The rest of the paper is organized as follows. Section 2 reviews the literature. Section 3 describes the materials and methods. Section 4 presents and discusses the results. Section 5 concludes the paper."),
  H1("2. Literature Review"),
  P("The flexible job shop problem and its extensions have been studied for more than three decades, and a recent review classifies the criteria, constraints and solution approaches used {dauzere}. When workers are treated as a second resource the problem becomes DRC-FJSP. Recent work solves it with a two-stage CP-based heuristic {andrade}, with a tri-objective mixed-integer model and invasive weed optimization under sequence-dependent setups {barak}, and with a two-stage multi-objective evolutionary algorithm when orders are split into variable sublots {huang}. The decision of how many workers an operation receives has been studied with graph reinforcement learning {liu}."),
  P("Garment-specific studies are fewer. A 2026 review of 82 apparel scheduling studies reports a shift from deterministic heuristics toward stochastic, bi-objective and human-centred models {hailemariam}. The closest study models a French sewing workshop as a dual-resource flexible job shop with sequence-dependent setups and solves it with MIP, CP and heuristics, reporting that exact methods stop scaling beyond a few hundred operations {perroux}. Human-centred scheduling in garment plants has also been shown to raise efficiency {bao}."),
  P("On the solution side, a large computational study found that CP now solves larger shop scheduling problems than MIP with off-the-shelf solvers {naderi}. CP scheduling uses interval variables and no-overlap constraints {laborie}; the CP-SAT solver of Google OR-Tools combines lazy clause generation, a simplex and a portfolio of search workers {cpsatlp}, {cpsat}. CP-based large neighborhood search (LNS) has given strong results for flexible job shops with multiple resources {kasapidis} and with lot streaming {yunusoglu}. For dynamic order books, rolling horizon decompositions are widely used {destouet}, {zeitrag}."),
  P("The gap addressed here is the combination: an exact CP model with joint machine-worker assignment, adaptive crew size and rotating shift calendars, used inside a rolling and validated pipeline at factory scale, and compared fairly with the rules that garment factories actually use {panwalkar}."),
  H1("3. Materials and Methods"),
  P("This section describes the materials and methods used in the proposed research. The methodology has seven phases, shown in Figure 1 and described one by one below. Phase-I generates the factory data. Phases II to VI are the five layers of the CRAFT V3 scheduler: problem modelling, initial solutions, CP-SAT rolling-window construction, CP-SAT neighbourhood improvement and solution selection. Phase-VII validates every schedule and evaluates it against the baselines. Established techniques are cited where they are used; for the parts designed in this work, their key properties are stated as propositions with proofs."),
];

const fig2Block = [
  new Paragraph({ children: [new ImageRun({ type: "png", data: fs.readFileSync(path.join(path.dirname(FIG), "figure2_gantt.png")),
    transformation: { width: 640, height: 357 }, altText: { title: "Figure 2", description: "Gantt chart of the CRAFT V3 schedule, case 30%-S1", name: "figure2" } })],
    alignment: AlignmentType.CENTER, spacing: { before: 120 } }),
  CAPTION("Figure 2. CRAFT V3 schedule of case 30%-S1, first 14 days: productive work of each worker, coloured by order. Gaps are shift ends and rest days; same-coloured bars on several rows at once are crews."),
];

const figBlock = [
  new Paragraph({ children: [new ImageRun({ type: "png", data: fs.readFileSync(FIG), transformation: { width: 520, height: 724 },
    altText: { title: "Figure 1", description: "Seven-phase methodology of the CRAFT V3 framework", name: "figure1" } })], alignment: AlignmentType.CENTER, spacing: { before: 120 } }),
  CAPTION("Figure 1. Methodology of the proposed CRAFT V3 framework."),
];

const body2 = [
  H2("A. Phase-I: Data Generation"),
  P("**Company records.** Four records of a garment group in the Ad-Dulayl Industrial Park, a Qualified Industrial Zone in Zarqa, Jordan, form the input: an order grid (styles, garment types, quantities, start and ex-factory dates), production plans with standard minute values (SMV) and line efficiencies, a daily output sheet, and a workstation sheet listing the factories, lines and worker capacity per line."),
  P("**Data calibration.** Each distribution the generator needs is taken from these records: garment-type frequencies, order quantities, start dates and due-date lead times, line efficiencies and the SMV of each garment type. The order count of the original book (1,029 order-equivalents) is formed from 139 purchase-order groups and 890 style–colour–month groups. Processing times are scaled so that a typical operation matches the real daily output: the scale factor equals the median real minutes per unit times the mean efficiency, divided by the mean SAM per step."),
  P("**Synthetic factory generator.** Generating instances from controlled parameters is standard practice in scheduling research {taillard}. Lines and their capacities follow the workstation sheet. Each style receives a sewing routing of 6 to 10 steps over five machine types, taken from published operation bulletins, with its SMV split over the steps. Each line has three crews that rotate weekly through three 8-hour shifts (06:00, 14:00, 22:00) and rest one day a week; operators hold one or two skills with an efficiency rating. Machines are allocated to types in proportion to routing demand. The size of the order book is set by the load factor ρ, the ratio of required to available operator time over a 56-day window:"),
  EQ("ρ = Σ (q × SAM / η) / (N × T)", 1),
  P("where q is the order quantity, SAM the standard allowed minutes of an operation, η the mean operator efficiency, N the number of present operators and T the available minutes per operator (one 8-hour shift on 6 of 7 days)."),
  P("**Production history simulation.** So that the data resemble a running factory, the order book is first planned up to the reference time: each order is split into lots of about 20 pieces {cheng}, and every lot step takes the earliest-free machine and qualified worker. The plan is then executed in simulation with processing-time variance and machine breakdowns, which leaves completed, in-progress and waiting work at the reference time."),
  P("**Factory databases.** Three databases are generated with the same resources and seed at ρ = 0.30, 0.60 and 0.90. Each is read through a SELECT-only account into per-factory snapshot files protected by a SHA-256 manifest {fips}, and CRAFT plans only from these verified snapshots."),
];

const body3 = [
  H2("B. Phase-II: Problem Modelling"),
  P("Each order o ∈ O has a release date r_<o>, a due date d_<o> and a route of operations in fixed precedence; Table 1 lists the notation."),
  TCAPTION("Table 1. Notation."),
  table(["Symbol", "Meaning"], [
    ["O, J, M, W", "Orders, operations, machines and workers"],
    ["r_<o>, d_<o>", "Release date and due date of order o"],
    ["q_<j>, SAM_<j>", "Pieces and standard minutes of operation j"],
    ["η_<w>", "Efficiency of worker w"],
    ["C_<j>", "Crew options of j: k_<c> skilled workers W_<c> on k_<c> machines M_<c>"],
    ["R_<j>", "Calendar regimes of j (a crew option and a start range)"],
    ["S_<j>, E_<j>", "Start and end of operation j (integer seconds)"],
    ["x_<jρ>", "1 if operation j runs in regime ρ"],
    ["I^<M>_<jρm>, I^<W>_<jρσw>", "Optional intervals of machine m and of segment σ of worker w"],
    ["F_<m>, F_<w>", "Committed or in-progress work on machine m and worker w"],
    ["b, s, g", "Bundle of 20 pieces, setup of 15 min, changeover of 25 min"],
  ], [1700, 3000], { size: 16, leftCols: [1] }),
  P("**Crew options.** A crew option c ∈ C_<j> assigns operation j to k_<c> workers of the required skill, each on a machine of the required type, with 1 ≤ k_<c> ≤ min(4, ⌊q_<j> / b⌋), so that every worker receives at least one bundle {cheng}. The members work in parallel, so their work rates add up and the productive time is"),
  EQ("P_<jc> = q_<j> SAM_<j> / Σ_<w∈W_c> η_<w>", 2),
  P("Every option starts with the setup s; the productive time then advances only while every member of the crew is on shift, outside rest days and leave."),
  P("**Calendar regimes.** Because work pauses at shift ends, the end of an operation is not its start plus a fixed duration. For each crew option, CRAFT divides the possible start times into regimes ρ ∈ R_<j>: ranges [lo_<ρ>, hi_<ρ>] in which the pattern of shift pauses is the same, so that the end E_<j> = α_<ρ> S_<j> + β_<ρ> and the start and end of every productive segment of every worker are affine functions of S_<j>. Regimes are computed exactly from the crew calendars, and their lower bounds include the release date and the end of work already executed, so the model needs no time discretisation. The CP-SAT model of a set of operations is:"),
  EQ("Σ_<ρ∈R_j> x_<jρ> = 1  ∀ j", 3),
  EQ("x_<jρ> ⇒ lo_<ρ> ≤ S_<j> ≤ hi_<ρ>,  E_<j> = α_<ρ> S_<j> + β_<ρ>", 4),
  EQ("NoOverlap( {I^<M>_<jρm>} ∪ F_<m> )  ∀ m ∈ M", 5),
  EQ("NoOverlap( {I^<W>_<jρσw>} ∪ F_<w> )  ∀ w ∈ W", 6),
  EQ("E_<j> ≤ S_<j′>  for consecutive j, j′ of a route", 7),
  EQ("E_<j> + g ≤ S_<j′>  ∨  E_<j′> + g ≤ S_<j>", 8),
  P("Constraint (3) selects exactly one regime, and with it one crew option, for every operation; (4) links the start and end to that regime. I^<M>_<jρm> is an optional interval over [S_<j>, E_<j>) on each machine of the option, and I^<W>_<jρσw> an optional interval for each productive segment σ of each worker; both are present only when x_<jρ> = 1 {laborie}. Constraints (5) and (6) forbid overlaps on every machine and every worker, including committed work, and (7) enforces the route precedence. Constraint (8) applies to two operations of different orders that can share a machine, with g the changeover time. This is a dual-resource constrained flexible job shop with variable crew size {dauzere}, {andrade}. Schedules are ranked lexicographically {ehrgott}:"),
  EQ("lexmin ( U, L, ΣT, T_<max>, ΣT_<op>, W, K, C_<max> )", 9),
  P("where U is the number of unscheduled operations, L the number of late orders (an unfinished order counts as late), ΣT the total tardiness, T_<max> the maximum tardiness (an unfinished order enters both with an estimated finish: the horizon end plus its unplaced work at the widest crew), ΣT_<op> the tardiness of operations against their operation due dates (Phase-IV), W the number of distinct workers engaged, K the number of assigned crew workers and C_<max> the makespan. Complete delivery is optimised first; workers are saved only afterwards, so extra hands go only where they reduce lateness. CP-SAT solves (9) level by level, fixing each level's optimum before the next. ΣT_<op> guides the windows of Phase-IV; the whole-schedule score f used in Phases III, V and VI omits it and breaks remaining ties by the sum of completion times."),
  P("**Proposition 1.** Under (9), every schedule that places all operations ranks above every schedule that leaves an operation unscheduled. *Proof.* U is the first component of (9). A complete schedule has U = 0 and an incomplete one U ≥ 1, and a lexicographic order is decided by the first component that differs; the remaining components are never compared. ∎ Placing U before L prevents a plan from looking better by dropping work: an unfinished order would otherwise count as only one late order."),
  H2("C. Phase-III: Initial Solutions"),
  P("Algorithm 1 shows how Phases III to VI work together; the phases are described in turn below."),
  ALGO("**Algorithm 1** CRAFT V3", [
    [0, "**Input:** orders, factory data, time budget τ = 1,800 s"],
    [0, "P ← ∅  *(Phase-III)*"],
    [0, "**for** rule ∈ {EDD-Crew, Savable-First-Crew} **do**"],
    [1, "S ← Dispatch(rule); **if** Validate(S) **then** P ← P ∪ {S}"],
    [0, "S_<CP> ← RollingWindows()  *(Phase-IV, Alg. 2)*"],
    [0, "S_<B> ← the best plan of P ∪ {S_<CP>} under f"],
    [0, "Ŝ_<1> ← Improve(S_<CP>, τ)  *(Phase-V, Alg. 3)*"],
    [0, "**if** S_<B> ≠ S_<CP> **then** Ŝ_<2> ← Improve(S_<B>, τ) **else** Ŝ_<2> ← Ŝ_<1>"],
    [0, "Ŝ ← argmin{f(Ŝ_<1>), f(Ŝ_<2>)}  *(Phase-VI)*"],
    [0, "**if** min_<S∈P> f(S) < f(Ŝ) **then** Ŝ ← argmin_<S∈P> f(S)  *(safety floor)*"],
    [0, "**return** Ŝ"],
  ]),
  SPACER(),
  P("Two quick dispatch plans give the scheduler feasible starting points. Both place one operation at a time with the same calendars, eligibility and precedence as the rest of CRAFT {panwalkar}, {pinedo}, and both give each operation the crew of one to four workers that finishes it earliest. **EDD-Crew** takes operations in due-date order {jackson}. **Savable-First-Crew** follows the idea of the Moore–Hodgson rule {moore}, which protects the jobs that can still be on time: an order is classed as savable when its release date plus its remaining work at the widest crew, at one shift per day, falls before its due date; savable orders are dispatched first by due date and the rest afterwards, smallest remaining work first, so that one large late order cannot hold the crews that savable orders need. Every plan is checked by the independent physical validator (Phase-VII) and enters the start pool."),
  H2("D. Phase-IV: CP-SAT Rolling-Window Construction"),
  P("The CP-SAT solver of OR-Tools {ortools}, {cpsat} builds the main plan, with interval variables for operations and no-overlap constraints for every machine and worker {laborie}. First, each operation receives its own due date by working backwards from the order due date {baker}:"),
  EQ("d_<j> = d_<o> − Σ_<l after j> c p̄_<l> / k_<max>", 10),
  P("where the sum runs over the later steps l of the same order, p̄_<l> is the single-worker productive time of step l, k_<max> the widest allowed crew and c = 3 converts productive time into calendar time for a crew that works one 8-hour shift a day. These due dates only rank operations; they never constrain the schedule. Following the rolling horizon principle {zeitrag}, the horizon is then cut into 24-hour windows. Each window takes up to 22 ready operations (released, predecessor finished) in order of d_<j>, plus their next steps as look-ahead, and CP-SAT solves the model (3)–(9) for them, with all committed work fixed, choosing their machines, workers, crew size and start times under staged time limits of 10, 20 and 30 s. Only the operations that start in the first 8 hours are committed; the rest are re-planned in the next window, which starts where the uncommitted plan begins. A window that cannot be solved is retried with fewer operations. When every operation is committed, the result is the CP-SAT plan (Algorithm 2)."),
  ALGO("**Algorithm 2** RollingWindows (Phase-IV)", [
    [0, "compute d_<j> by (10); t ← 0"],
    [0, "**while** an operation is not committed **do**"],
    [1, "H ← ready operations (released, predecessor committed)"],
    [1, "**if** every operation in H is ready after t + 24 h **then** t ← earliest ready time"],
    [1, "X ← up to 22 operations of H ready before t + 24 h, by d_<j>, plus their next steps"],
    [1, "solve (3)–(9) on X with committed work fixed; on failure retry with fewer operations"],
    [1, "commit the operations of X that start before t + 8 h (at least one); validate"],
    [1, "t ← max(t + 8 h, earliest start of the uncommitted plan)"],
    [0, "**return** the committed plan S_<CP>"],
  ]),
  SPACER(),
];

const body4 = [
  H2("E. Phase-V: CP-SAT Neighbourhood Improvement"),
  P("Large neighbourhood search {shaw}, {pisinger} improves the plan. It runs twice for 1,800 s: once from the CP-SAT plan and once from the best-scoring plan of the start pool (a single run when this is the CP-SAT plan). A move frees a neighbourhood of operations and lets CP-SAT re-optimise them with all other operations fixed, using the current schedule as a solution hint {kasapidis}. Unplaced operations are repaired first; then the neighbourhoods follow the late orders, their bottleneck machines and their crews, then a sweep through time slices, then an adaptive mix that favours the move types that recently improved. A neighbourhood holds 20 operations and grows to 40 and 60 when CP-SAT proves that the smaller region holds no gain. After every move the whole schedule is validated again, and the move is accepted only if it is feasible and does not worsen f (Algorithm 3)."),
  ALGO("**Algorithm 3** Improve(S_<0>, τ) (Phase-V)", [
    [0, "S ← S_<0>; weights ω_<k> ← 1 for k ∈ {time, machine, crew, bottleneck}"],
    [0, "**while** time < τ **and** the search has not stalled **do**"],
    [1, "choose N: unplaced work first, then late orders (crew, bottleneck, machine), then a time sweep, then kind k drawn with probability ∝ ω_<k>; |N| = 20"],
    [1, "S′ ← CP-SAT re-optimisation of N with S ∖ N fixed and S as hint"],
    [1, "**if** Validate(S′) **and** f(S′) ≤ f(S) **then** S ← S′"],
    [1, "**if** CP-SAT proves no gain **then** grow N to 40, then 60"],
    [1, "update ω_<k>"],
    [0, "**return** S"],
  ]),
  SPACER(),
  P("**Proposition 2.** Let S_<0> be a feasible start. Phase-V returns a feasible schedule Ŝ with f(Ŝ) ≤ f(S_<0>). *Proof.* By induction on the accepted moves S_<0>, S_<1>, …: S_<0> is feasible, and S_<t+1> is accepted only if the validator finds the whole schedule feasible and f(S_<t+1>) ≤ f(S_<t>). Every iterate is therefore feasible and the sequence f(S_<t>) never increases; Ŝ is the last iterate. ∎"),
  H2("F. Phase-VI: Solution Selection"),
  P("The better of the two improved plans under f becomes the CRAFT V3 schedule. As a safety floor, if any feasible start scores better than the selected plan, that start is returned instead. The output gives, for every operation, its line, machines, workers, crew size, start and end times and shift segments."),
  P("**Proposition 3.** Let P be the start pool together with the CP-SAT plan. The CRAFT V3 schedule is feasible and scores no worse than the best plan in P. *Proof.* The run from the best start begins at S_<B> = argmin_<S∈P> f(S), or coincides with the run from the CP-SAT plan when that plan is S_<B>. By Proposition 2 its result is feasible with f ≤ f(S_<B>). Selection keeps the minimum of the two results and the safety floor can only lower f further, so f(S_<CRAFT>) ≤ f(S_<B>) = min_<S∈P> f(S), and every candidate is feasible. ∎"),
  H2("G. Phase-VII: Validation and Evaluation"),
  P("Every schedule, from CRAFT V3 or a baseline, is replayed by an independent physical validator that checks machine and worker overlaps, crew calendars, rest days and leave, machine type and worker skill, precedence, release dates and the work already in progress; only schedules that pass are evaluated. The baselines use the same physics: FIFO {panwalkar}, EDD {jackson} and SPT {smith} with a single worker per operation, as most factories dispatch, and the crew-aware EDD-Crew and Savable-First-Crew rules of Phase-III. The CP-SAT plan of Phase-IV is reported separately to show what Phase-V adds."),
  P("The measures are: completed orders, late orders (an unfinished order counts as late), late orders that were avoidable (the order was not already overdue when planning started), mean tardiness per order and makespan. Because the lateness of an unfinished order cannot be measured, tardiness is compared only between plans that complete every order. For each baseline, the wins, ties and losses of CRAFT V3 are counted over the cases and the paired differences are tested with the two-sided Wilcoxon signed-rank test {wilcoxon}, ties dropped."),
];

const body4r = [
  H1("4. Results and Discussion"),
  H2("A. Experimental Setup"),
  P("All experiments use Factory 4 (2 lines, 46 machines, 102 workers, 143 worker–skill records) at the reference time 10 Sept. 2026. In each of the three databases the eligible orders are taken in release order, and three disjoint sets of 10 orders form Sets 1–3 (positions 1–10, 11–20 and 21–30). This gives nine cases with 61 to 89 operations each. Each case is run once, and Set 1 at every load twice more, giving 15 runs. CRAFT V3 uses 8 search workers, a 14-day dispatch window and a planning horizon that ends 90 days after the last due date (work not placed by then counts as unfinished); workers are drawn first from the line's own crew and then from the factory pool. CRAFT is implemented in Python 3.13 with the CP-SAT solver of OR-Tools 9.15; all runs used Kaggle cloud notebooks with 4 vCPU (x86-64), one run per notebook session or two in sequence. The time limits are 10, 20 and 30 s for the window stages, 30 s per objective level and 1,800 s for each improvement search. A single-worker EDD plan was also scored as a start in these runs but was never the best start, so it is not part of the method. A month later, Set 1 at every load was run again on the same platform, once with these settings (a fourth repeat) and once with each of two other settings (Section 4-E)." +
    (D.v31_done ? " Case 90%-S3 first showed that a plan leaving one operation unscheduled could outrank a complete plan, which led to Proposition 1 (U ranked first, CRAFT V3.1). The case was rerun with V3.1: in this run the time-limited CP-SAT windows of Phase-IV, like both crew-aware initial solutions, left one operation unplaced, so no complete plan was available and CRAFT V3.1 returned the same plan as before. The original run had found a complete CP-SAT plan with one more late order; the difference between the two runs comes from the time-limited parallel search. Every other run is unaffected, because all its compared plans were complete."
      : " ==Case 90%-S3 is being rerun with U ranked first (CRAFT V3.1) and is left out of the statistics until then.==")),
  H2("B. Overall Results"),
  P(`The order sets come from a running factory, so many of their orders were already late before planning started: ${overdueTotal} of the ${10 * nCases} orders (between ${minOverdue} and ${maxOverdue} of the 10 in each case) were due on or before the reference date. These orders are late under every method, whatever the schedule, so only ${possible} orders could be delivered on time, and a method can only be judged on the remaining ones. Table 2 sums the results over the ${nCases} cases on this basis. CRAFT V3 completed every order in every case except ${noneComplete.map((c) => c.label).join(", ")}, where no method did (each left one operation unplaced within the horizon), and delivered ${V3.onTime} of the ${possible} possible orders on time, against ${EC.onTime} for EDD-Crew and ${CP.onTime} for its own CP-SAT plan before improvement; it left ${V3.avoid} avoidable late orders, against ${EC.avoid} for EDD-Crew. FIFO, EDD and SPT delivered ${RULES.onTime} on time but left an order unfinished in ${rulesPartial} of the ${nCases} cases; the lateness of an unfinished order cannot be measured, so their tardiness is not comparable there (marked –*).`),
  P(`Table 3 gives the late orders case by case. CRAFT V3 had the fewest late orders, or tied for the fewest, in every case. In the ${eddFull.length} cases where single-worker EDD finished every order (${eddFull.map((c) => c.label).join(", ")}), CRAFT V3 cut mean tardiness by ${rng(eddGain)}% and finished all work in less than half the time (Tables 4 and 5). Crews are the main reason: with one worker per operation, work waits for a single operator while others stand idle. Figure 2 shows the first two weeks of the CRAFT V3 schedule of case 30%-S1: crews of up to four workers carry the same operation in parallel, and every bar stops at the end of its worker's shift and resumes in the next one.`),
];

const METHOD_COLS = ALL.map((m) => SHORT[m] || m);
const t1 = [
  TCAPTION(`Table 2. Overall results over ${scope.replace(/==/g, "")}. ${overdueTotal} of the ${10 * nCases} orders were already overdue when planning started, so ${possible} could be on time; avoidable late orders = late orders − overdue orders; mean tardiness over the ${tardCases.length} cases in which the crew-aware methods finished every order (best in bold; –* = orders left unfinished, so mean tardiness cannot be measured).`),
  table(["Method", "Cases with all orders completed", "Orders on time (of possible)", "Avoidable late orders", "Mean tardiness (days)", "Average makespan (days)"],
    sumRows, [2300, 1500, 1600, 1500, 1450, 1500], { size: 17, shadeRow: (r) => (r[0] === "CRAFT V3" ? "EEF4E8" : null) }),
];

const t2 = [
  TCAPTION("Table 3. Late orders per case, including orders that were already overdue when planning started (best per case in bold)."),
  table(["Case", "Ops", "Overdue at start", ...METHOD_COLS], lateRows, [1200, 600, 1000, 900, 900, 900, 1050, 1050, 1150, 1100],
    { size: 16, shadeCol: 9 }),
];

const t3 = [
  TCAPTION("Table 4. Mean tardiness per case (days per order; best per case in bold; –* = an order left unfinished, so tardiness cannot be measured) and the initial solution that CRAFT V3's final plan started from."),
  table(["Case", ...METHOD_COLS, "CRAFT start"], tardRows, [1200, 900, 900, 900, 1050, 1050, 1150, 1100, 1550], { size: 16, shadeCol: 7 }),
];

const t4 = [
  TCAPTION("Table 5. Makespan per case (days from the start of the plan to the last finish; best per case in bold)."),
  table(["Case", ...METHOD_COLS], mkRows, [1300, 1150, 1150, 1150, 1250, 1250, 1300, 1300], { size: 16, shadeCol: 7 }),
];

const body5 = [
  H2("C. CRAFT V3 against Crew-aware Methods"),
  P(`The crew-aware methods use the same crew options as CRAFT, so they are the fair comparison. Against EDD-Crew, CRAFT V3 had fewer late orders in ${ec.late.win} of ${ec.late.n} cases and never more (Table 3), and its mean tardiness was ${f1(tardGainEC)}% lower on average (Table 4). Its tardiness was higher in ${tradeCases.length} cases (${tradeCases.map((c) => c.label).join(", ")}), by ${tradeGap.map(f2).join(", ")} days per order; in each of them it kept ${tradeSaved.join(", ")} more orders on time, which is exactly the trade its objective asks for: delivering an order on time ranks above reducing the lateness of orders that are late anyway.`),
  P(`Savable-First-Crew, the strongest initial solution, matched CRAFT V3 on late orders in every case; CRAFT V3 then lowered its mean tardiness in ${sf.mean_tard.win} cases without raising it in any (${f2(sf.mean_tard.mean_other)} to ${f2(sf.mean_tard.mean_craft)} days) and shortened its average makespan (${f1(SF.makespan)} to ${f1(V3.makespan)} days). CRAFT V3 also had fewer late orders than its own CP-SAT plan in ${cp.late.win} cases and never more. No single start was best: the final plan started from the CP-SAT plan, EDD-Crew or Savable-First-Crew depending on the case (Table 4), which is why CRAFT improves two starts and keeps the better result (Proposition 3). A full run of all seven methods took ${rng(hours)} h per case on 4 vCPU.`),
  H2("D. Makespan"),
  P(`Makespan is where CRAFT V3 is weaker. Its average (${f1(V3.makespan)} days) is shorter than that of FIFO, EDD and SPT (${f1(Math.min(SUM.FIFO.makespan, SUM.EDD.makespan, SUM.SPT.makespan))}–${f1(Math.max(SUM.FIFO.makespan, SUM.EDD.makespan, SUM.SPT.makespan))}) and of Savable-First-Crew (${f1(SF.makespan)}), but longer than that of EDD-Crew (${f1(EC.makespan)}), which finished all work earlier in ${mkLonger.length} of ${nCases} cases (Table 5). The reason lies in what decides the makespan. In ${mkInherited.length} of these ${mkLonger.length} cases the final plan started from Savable-First-Crew and kept exactly its makespan${mkOther.length ? `; in ${mkOther.map((c) => c.label).join(", ")} it shortened it` : ""}. Savable-First-Crew serves the orders that can still be delivered on time first and the rest afterwards, so the last finish is set by a large order that is late anyway. In 60%-S2, for example, the makespan is set by an order of 3,200 pieces that was already overdue when planning started: EDD-Crew finished it on day 131.4 and CRAFT V3 on day 147.1, while CRAFT V3 delivered two more orders on time. Because the makespan is the last level of (9), after the workers and the crew size, the improvement phase moves that order earlier only when this costs no extra worker, which it rarely finds within its time budget. A factory that values finishing all work early can rank total completion time directly after delivery; CRAFT supports this ordering as a setting, but it was not evaluated here.`),
];

const body6 = [
  H2("E. Statistical Analysis and Repeatability"),
  P(`Table 6 gives the paired comparisons. On late orders, CRAFT V3 lost no case to any method: the advantage is significant against EDD-Crew (p = ${pv(ec.late.p)}) and against the CP-SAT plan (p = ${pv(C["CP-SAT plan"].late.p)}), and p = ${pv(C.EDD.late.p)} against single-worker EDD. On mean tardiness, CRAFT V3 is significantly better than Savable-First-Crew (p = ${pv(C["Savable-First-Crew"].mean_tard.p)}) and its CP-SAT plan (p = ${pv(C["CP-SAT plan"].mean_tard.p)}). Against EDD-Crew the tardiness difference (${wtl(ec.mean_tard)}, p = ${pv(ec.mean_tard.p)}) is not significant with ${ec.mean_tard.n} cases, because its three losses are the deliberate trades described above. The repeated runs (Table 7), the fourth one a month after the first three, reproduced the same late orders and the same total tardiness within ${f2(repSpread)} days, so the results are stable despite the time-limited parallel search.`),
  P(`**Sensitivity to the settings.** Table 8 reruns Set 1 at each load under two other settings: B uses 4 solver workers, one per vCPU, and lets each window commit 24 hours instead of 8; C is B with an improvement budget of 3,600 s instead of 1,800 s. Under every setting CRAFT V3 finished all orders, with the same late orders as under the paper's settings (A), no more late orders than either crew-aware rule and lower total tardiness than both. Its total tardiness changed by ${signed(Math.min(...sensDev))}% to ${signed(Math.max(...sensDev))}% against A (a positive value is worse), so the settings shift the result by a few percent at most and no setting is better at every load. Doubling the budget (C) took ${rng(sensMin("C").map((x) => x / 60))} h per case against ${rng(sensMin("A").map((x) => x / 60))} h for A, so the paper's settings are kept.`),
];

const t5 = [
  TCAPTION("Table 6. CRAFT V3 against each method: wins/ties/losses over the cases and two-sided Wilcoxon signed-rank p-values."),
  table(["Baseline", "Late orders W/T/L", "p", "Mean tardiness W/T/L", "p", "Mean tardiness: baseline (d)", "Mean tardiness: CRAFT V3 (d)"],
    t5rows, [1900, 1300, 900, 1800, 900, 1500, 1500], { size: 16 }),
];

const t6 = [
  TCAPTION("Table 7. Repeated runs of CRAFT V3 (Set 1 at each load): total tardiness (days) and late orders. Runs 1–3 in Oct. 2026, run 4 a month later."),
  table(["Case", "Run 1", "Run 2", "Run 3", "Run 4", "Late orders (runs 1 / 2 / 3 / 4)"], t6rows, [1500, 1300, 1300, 1300, 1300, 2600], { size: 16 }),
];

const t8 = [
  TCAPTION("Table 8. Sensitivity to the settings (Set 1 at each load): total tardiness (days), late orders and run time. A: the paper's settings (8 solver workers, 8 h window commit, 1,800 s improvement); B: 4 solver workers, 24 h commit; C: as B with 3,600 s improvement."),
  table(["Case", "Setting", "CRAFT V3 tardiness", "CRAFT V3 late", "EDD-Crew tardiness", "EDD-Crew late", "Sav.-First tardiness", "Sav.-First late", "Run time (min)"],
    t8rows, [1100, 900, 1200, 1000, 1200, 1000, 1200, 1000, 1100], { size: 16 }),
];

const body7 = [
  H1("5. Conclusion"),
  P(`This paper presented CRAFT V3, a constraint programming framework for garment production scheduling with machines, skilled workers, adaptive crews and rotating shifts. A synthetic factory database calibrated from real company records was generated at 30%, 60% and 90% load, and seven methods were compared under one independent physical validation. Many orders of a running factory are already late when a plan is made; on the orders that could still be delivered on time, CRAFT V3 delivered ${V3.onTime} of ${possible} against ${EC.onTime} for crew-aware EDD, completed every order in every case where any method did, had ${sigLateEC ? "significantly " : ""}fewer late orders and ${f1(tardGainEC)}% lower mean tardiness than crew-aware EDD, and cut mean tardiness by ${rng(eddGain)}% against the single-worker rules used in factories wherever those finished their orders. Every schedule it returns is feasible and never worse than its best initial solution (Propositions 2 and 3). The results held in a fourth run a month later and changed by only a few percent under other solver settings. Its limitations are a run time of ${rng(hours)} h per case, a longer makespan than crew-aware EDD because orders that are late anyway are finished last, run-to-run variation of the time-limited search, seen in case 90%-S3, and an evaluation on one factory with sets of 10 orders. CRAFT has been built into a web application in which the production manager reviews and approves each plan before it is used. Future work will evaluate full order books and other factories, run CRAFT cycle by cycle on a weekly rolling plan, shorten its run time, and trial the application on a factory floor.`),
];

// ---------------------------------------------------------------- assemble
const refParas = [H1("References"), ...order.map((k, i) => new Paragraph({
  children: [new TextRun({ text: `[${i + 1}]\t`, font: FONT, size: 17 }), ...runs(REFS[k], { size: 17 })],
  tabStops: [{ type: "left", position: 420 }], indent: { left: 420, hanging: 420 },
  alignment: AlignmentType.LEFT, spacing: { after: 60 },
}))];
// Refs used only inside tables still need numbers before the list is built:
// all content above is created first, so `order` is complete here.

const page = { size: { width: 11906, height: 16838 }, margin: { top: 1134, bottom: 1134, left: 1021, right: 1021 } };
const one = (children, first) => ({ properties: { page, ...(first ? {} : { type: SectionType.CONTINUOUS }), column: { count: 1 } }, children });
const two = (children) => ({ properties: { page, type: SectionType.CONTINUOUS, column: { count: 2, space: 425 } }, children });

const doc = new Document({
  creator: "Muhammad Tuaha",
  title: "CRAFT: A Constraint Programming Framework for Crew-Aware Garment Production Scheduling",
  styles: { default: { document: { run: { font: FONT, size: 20 } } } },
  numbering: { config: [{ reference: "bul", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 360, hanging: 240 } } } }] }] },
  sections: [
    one(titleBlock, true),
    two(body1),
    one(figBlock),
    two([...body2, ...body3, ...body4, ...body4r]),
    one(framed(t1)),
    one(framed(t2)),
    one(fig2Block),
    two(body5.slice(0, 3)),
    one(framed(t3)),
    two(body5.slice(3)),
    one(framed(t4)),
    two(body6),
    one(framed([...t5, ...t6])),
    one(framed(t8)),
    two([...body7, ...refParas]),
  ],
});
Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("written", OUT, buf.length, "refs", order.length); });
