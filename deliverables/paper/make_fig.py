"""Figure 1 of the CRAFT paper: the methodology as seven phases. Phase-I
generates the data; Phases II-VI are the five layers of the CRAFT V3 scheduler;
Phase-VII validates and evaluates. Reference paper's style: dashed phase frames
with title bars, Start/End ovals, process boxes with captions underneath.
Plain SVG with explicit arrowheads so it renders the same in a browser, Word
and print."""
from pathlib import Path

W = 1000
FONT = "'Times New Roman', Times, serif"
INK = "#1f2a36"
CAP = "#2b3644"
DATA, CRAFT, EVAL = 1, 2, 3
PH = {  # (stroke, title bar, box fill)
    DATA: ("#3e5a72", "#c9d5df", "#e3e9ef"),
    CRAFT: ("#55803f", "#d3e5c6", "#e7f1df"),
    EVAL: ("#4a6fa5", "#cddbef", "#e3ebf7"),
}
X0, X1 = 70, 890          # phase frame
C0, C1 = 92, 868          # content inside a frame
BOX_H, TITLE_H, GAP = 52, 26, 30
out = []


def text(x, y, s, size=13, weight="bold", anchor="middle", fill=INK, style="normal"):
    out.append(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
               f'font-style="{style}" text-anchor="{anchor}" fill="{fill}">{s}</text>')


def arrow(points, color=INK, width=1.8, dashed=False):
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width}"{dash}/>')
    (x1, y1), (x2, y2) = points[-2], points[-1]
    if abs(x1 - x2) < 1e-6:
        s = 1 if y2 > y1 else -1
        head = [(x2, y2), (x2 - 5, y2 - 10 * s), (x2 + 5, y2 - 10 * s)]
    else:
        s = 1 if x2 > x1 else -1
        head = [(x2, y2), (x2 - 10 * s, y2 - 5), (x2 - 10 * s, y2 + 5)]
    out.append(f'<polygon points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in head)}" fill="{color}"/>')


def loop(x_from, x_to, y, label, color):
    """Curved return arrow above the boxes: from the last step back to an earlier one."""
    top = y - 20
    out.append(f'<path d="M{x_from:.1f} {y:.1f} C{x_from:.1f} {top:.1f}, {x_to:.1f} {top:.1f}, {x_to + 8:.1f} {y - 2:.1f}" '
               f'fill="none" stroke="{color}" stroke-width="1.6"/>')
    out.append(f'<polygon points="{x_to:.1f},{y:.1f} {x_to + 2:.1f},{y - 11:.1f} {x_to + 10:.1f},{y - 5:.1f}" fill="{color}"/>')
    text((x_from + x_to) / 2, top + 1, label, size=11.5, weight="normal", style="italic", fill=CAP)


def phase(n, y, h, title):
    stroke, bar, _ = PH[n]
    out.append(f'<rect x="{X0}" y="{y}" width="{X1 - X0}" height="{h}" fill="none" stroke="{stroke}" '
               f'stroke-width="1.8" stroke-dasharray="8 5"/>')
    out.append(f'<rect x="{X0}" y="{y}" width="{X1 - X0}" height="{TITLE_H}" fill="{bar}" stroke="{stroke}" '
               f'stroke-width="1.8" stroke-dasharray="8 5"/>')
    text((X0 + X1) / 2, y + 18, title, size=14.5)


def box(n, x, y, w, rows, h=BOX_H, fill=None, sub=None):
    stroke, _, body = PH[n]
    out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h}" rx="6" fill="{fill or body}" '
               f'stroke="{stroke}" stroke-width="1.5"/>')
    total = len(rows) + (1 if sub else 0)
    first = y + h / 2 + 5 - (total - 1) * 8
    for i, s in enumerate(rows):
        text(x + w / 2, first + i * 16, s, size=13.5)
    if sub:
        text(x + w / 2, first + len(rows) * 16 - 1, sub, size=11, weight="normal", fill=CAP)


def pill(n, x, y, w, s, h=24, italic=False):
    stroke = PH[n][0]
    out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h}" rx="{h / 2}" fill="#ffffff" '
               f'stroke="{stroke}" stroke-width="1.3"/>')
    text(x + w / 2, y + h / 2 + 4.5, s, size=12, weight="bold", style="italic" if italic else "normal")


def caption(x, y, rows):
    for i, s in enumerate(rows):
        text(x, y + i * 14, s, size=11.5, weight="normal", fill=CAP)


def columns(widths):
    """Left x of each column, spread evenly across the content width."""
    gap = (C1 - C0 - sum(widths)) / (len(widths) - 1)
    xs, x = [], C0
    for w in widths:
        xs.append(x)
        x += w + gap
    return xs


def chain(n, xs, widths, y):
    for (xa, wa), xb in zip(zip(xs, widths), xs[1:]):
        arrow([(xa + wa, y + BOX_H / 2), (xb - 2, y + BOX_H / 2)], PH[n][0])


def oval(cx, cy, s):
    out.append(f'<ellipse cx="{cx}" cy="{cy}" rx="44" ry="17" fill="#4b5767" stroke="#2c3440" stroke-width="1.5"/>')
    text(cx, cy + 5, s, size=14, fill="#ffffff")


def standard(n, y, title, items, loop_back=None, note=None):
    """A phase with one row of boxes and two-line captions underneath."""
    lift = 26 if loop_back else 0
    h = TITLE_H + 14 + lift + BOX_H + 44 + (30 if note else 0)
    phase(n, y, h, title)
    widths = [w for _t, w, _c in items]
    xs = columns(widths)
    by = y + TITLE_H + 14 + lift
    for x, (rows, w, cap) in zip(xs, items):
        box(n, x, by, w, rows)
        caption(x + w / 2, by + BOX_H + 17, cap)
    chain(n, xs, widths, by)
    if loop_back:
        a, b, label = loop_back
        loop(xs[a] + widths[a] / 2, xs[b] + widths[b] / 2 - 10, by, label, PH[n][0])
    if note:
        nw = 560
        pill(n, (C0 + C1) / 2 - nw / 2, by + BOX_H + 50, nw, note, italic=True)
    return y + h, xs, widths, by


y = 52
oval((X0 + X1) / 2, 24, "Start")
arrow([((X0 + X1) / 2, 41), ((X0 + X1) / 2, y)], PH[DATA][0])

# Phase-I: data generation
bottom, *_ = standard(DATA, y, "Phase-I (Data Generation)", [
    (["Company", "Records"], 132, ["Order, planning, output", "and workstation sheets"]),
    (["Data", "Calibration"], 132, ["Standard minutes, efficiency,", "order sizes, lead times"]),
    (["Synthetic Factory", "Generator"], 150, ["Lines, routings, rotating", "crews, machines, orders"]),
    (["Production History", "Simulation"], 150, ["Planned and actual execution", "up to the reference date"]),
    (["Factory", "Databases"], 132, ["Three load levels", "(ρ = 30%, 60%, 90%)"]),
])
craft_top = bottom + GAP


def drop(prev_bottom, next_top, n):
    arrow([((X0 + X1) / 2, prev_bottom), ((X0 + X1) / 2, next_top)], PH[n][0])


# Phase-II: problem modelling (CRAFT layer 1)
y = craft_top
drop(bottom, y, CRAFT)
bottom, *_ = standard(CRAFT, y, "Phase-II (Problem Modelling)", [
    (["Orders and", "Precedence"], 160, ["Release dates, due dates", "and operation sequence"]),
    (["Adaptive Crew", "Options"], 160, ["One to four workers", "per operation"]),
    (["Duration", "Model"], 160, ["Processing time shared by", "the crew, plus setup"]),
    (["Shift", "Calendars"], 160, ["Rotating shifts, rest days", "and leave"]),
])

# Phase-III: initial solutions (CRAFT layer 2)
y = bottom + GAP
drop(bottom, y, CRAFT)
h3 = TITLE_H + 14 + 2 * 44 + 8 + 30
phase(CRAFT, y, h3, "Phase-III (Initial Solutions)")
xs = columns([210, 190, 190])
top = y + TITLE_H + 14
box(CRAFT, xs[0], top, 210, ["EDD-Crew Plan"], h=44, sub="earliest due date, fastest crew")
box(CRAFT, xs[0], top + 52, 210, ["Savable-First Plan"], h=44, sub="on-time-possible orders first")
mid = top + 48
box(CRAFT, xs[1], mid - BOX_H / 2, 190, ["Feasibility", "Check"])
caption(xs[1] + 95, mid + BOX_H / 2 + 17, ["Every plan verified against", "shop-floor rules"])
box(CRAFT, xs[2], mid - BOX_H / 2, 190, ["Start", "Pool"])
caption(xs[2] + 95, mid + BOX_H / 2 + 17, ["Feasible starts for the", "improvement phase"])
for yy in (top + 22, top + 74):
    arrow([(xs[0] + 210, yy), (xs[0] + 228, yy), (xs[0] + 228, mid), (xs[1] - 2, mid)], PH[CRAFT][0])
arrow([(xs[1] + 190, mid), (xs[2] - 2, mid)], PH[CRAFT][0])
bottom = y + h3

# Phase-IV: CP-SAT rolling-window construction (CRAFT layer 3)
y = bottom + GAP
drop(bottom, y, CRAFT)
bottom, xs4, ws4, by4 = standard(CRAFT, y, "Phase-IV (CP-SAT Rolling-Window Construction)", [
    (["Operation", "Due Dates"], 130, ["Deadlines propagated", "back from each order"]),
    (["Window", "Selection"], 130, ["Up to 22 ready operations", "within 24 hours"]),
    (["CP-SAT", "Optimization"], 140, ["Machines, workers, crew", "size and start times"]),
    (["Partial", "Commitment"], 130, ["First 8 hours fixed,", "the rest re-planned"]),
    (["CP-SAT", "Plan"], 110, ["Complete feasible", "schedule"]),
], loop_back=(3, 1, "next window"),
   note="Delivery first: late orders → tardiness → workers → makespan")

# Phase-V: CP-SAT neighbourhood improvement (CRAFT layer 4)
y = bottom + GAP
drop(bottom, y, CRAFT)
lift = 26
h5 = TITLE_H + 14 + lift + BOX_H + 44
phase(CRAFT, y, h5, "Phase-V (CP-SAT Neighbourhood Improvement)")
widths = [128, 140, 140, 130, 120]
xs = columns(widths)
by = y + TITLE_H + 14 + lift
pill(CRAFT, xs[0], by - 2, widths[0], "From CP-SAT Plan", h=24)
pill(CRAFT, xs[0], by + BOX_H - 22, widths[0], "From Best Start", h=24)
for yy in (by + 10, by + BOX_H - 10):
    arrow([(xs[0] + widths[0], yy), (xs[0] + widths[0] + 14, yy), (xs[0] + widths[0] + 14, by + BOX_H / 2),
           (xs[1] - 2, by + BOX_H / 2)], PH[CRAFT][0])
caption(xs[0] + widths[0] / 2, by + BOX_H + 17, ["Two searches,", "30 minutes each"])
steps = [(["Neighbourhood", "Selection"], ["Late orders, bottlenecks,", "crews and time slices"]),
         (["CP-SAT", "Re-optimization"], ["Selected operations", "rescheduled, rest fixed"]),
         (["Feasibility", "Check"], ["Whole schedule", "re-verified"]),
         (["Acceptance"], ["Kept only if the", "schedule is no worse"])]
for x, w, (rows, cap) in zip(xs[1:], widths[1:], steps):
    box(CRAFT, x, by, w, rows)
    caption(x + w / 2, by + BOX_H + 17, cap)
chain(CRAFT, xs[1:], widths[1:], by)
loop(xs[4] + widths[4] / 2, xs[1] + widths[1] / 2 - 10, by, "repeat until the time limit", PH[CRAFT][0])
bottom = y + h5

# Phase-VI: solution selection (CRAFT layer 5)
y = bottom + GAP
drop(bottom, y, CRAFT)
bottom, *_ = standard(CRAFT, y, "Phase-VI (Solution Selection)", [
    (["Best Plan", "Selection"], 190, ["The better of the two", "improved plans"]),
    (["Safety", "Floor"], 190, ["Never worse than", "a feasible start"]),
    (["CRAFT V3", "Schedule"], 190, ["Machine, worker, crew and", "timing per operation"]),
])
craft_bottom = bottom

# Bracket marking Phases II-VI as the CRAFT V3 scheduler
bx = X1 + 18
out.append(f'<path d="M{bx - 8} {craft_top} H{bx} V{craft_bottom} H{bx - 8}" fill="none" '
           f'stroke="{PH[CRAFT][0]}" stroke-width="2"/>')
cy = (craft_top + craft_bottom) / 2
out.append(f'<text x="{bx + 22}" y="{cy}" font-family="{FONT}" font-size="15" font-weight="bold" fill="{PH[CRAFT][0]}" '
           f'text-anchor="middle" transform="rotate(90 {bx + 22} {cy})">CRAFT V3 Scheduler</text>')

# Phase-VII: validation and evaluation
y = bottom + GAP
drop(bottom, y, EVAL)
h7 = TITLE_H + 14 + 2 * 44 + 8 + 30
phase(EVAL, y, h7, "Phase-VII (Validation and Evaluation)")
xs = columns([236, 146, 146, 156])
top = y + TITLE_H + 14
box(EVAL, xs[0], top, 236, ["Baseline Schedules"], h=44, sub="FIFO, EDD, SPT, EDD-Crew, Savable-First")
box(EVAL, xs[0], top + 52, 236, ["CRAFT V3 Schedule"], h=44, sub="from Phase-VI")
mid = top + 48
for x, w, rows, cap in ((xs[1], 146, ["Physical", "Validation"], ["Overlaps, calendars,", "skills, precedence"]),
                        (xs[2], 146, ["Performance", "Evaluation"], ["Late orders, tardiness,", "makespan, utilization"]),
                        (xs[3], 156, ["Statistical", "Comparison"], ["Repeated runs, Wilcoxon", "signed-rank test"])):
    box(EVAL, x, mid - BOX_H / 2, w, rows)
    caption(x + w / 2, mid + BOX_H / 2 + 17, cap)
for yy in (top + 22, top + 74):
    arrow([(xs[0] + 236, yy), (xs[0] + 250, yy), (xs[0] + 250, mid), (xs[1] - 2, mid)], PH[EVAL][0])
chain(EVAL, xs[1:], [146, 146, 156], mid - BOX_H / 2)
bottom = y + h7
arrow([((X0 + X1) / 2, bottom), ((X0 + X1) / 2, bottom + 24)], PH[EVAL][0])
oval((X0 + X1) / 2, bottom + 41, "End")

H = int(bottom + 66)
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">'
       f'<rect width="{W}" height="{H}" fill="#ffffff"/>' + "".join(out) + "</svg>")
here = Path(__file__).parent
(here / "figure1_methodology.svg").write_text(svg, encoding="utf-8")
(here / "figure1_methodology.html").write_text(
    '<!doctype html><meta charset="utf-8"><style>html,body{margin:0;background:#fff}</style>' + svg, encoding="utf-8")
print("written", W, H)
