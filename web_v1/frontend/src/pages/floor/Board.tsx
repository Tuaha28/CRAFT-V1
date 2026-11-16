import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { floor, type Board as BoardData } from "../../lib/api";
import { num } from "../../lib/format";
import { useReadyScope } from "../../app/scope";
import { DayNav, lineName, StateChip, useFloorLines } from "../../components/Floor";
import { Loading, Notice, PageHead } from "../../components/ui";

type BoardLine = BoardData["lines"][number];

function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => { const t = window.setInterval(() => setNow(new Date()), 30_000); return () => window.clearInterval(t); }, []);
  return now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** One line as the floor TV shows it: pieces today, how far behind the plan, and each hour. */
function LineTile({ l, name, day }: { l: BoardLine; name: string; day: number }) {
  const share = (v: number) => `${Math.min(100, (v / Math.max(1, l.target_day)) * 100)}%`;
  const gap = l.target_so_far - l.actual_so_far;
  if (!l.target_day && !l.actual_day) {
    return (
      <section className="panel tv-line idle" aria-label={name}>
        <header><h2><Link to={`/floor/line?line=${l.line_id}&day=${day}`}>{name}</Link></h2><StateChip state={l.state} /></header>
        <p className="muted">No work planned on this line today.</p>
        <footer className="small muted"><span className={l.open_issues ? "late-text" : undefined}>{l.open_issues} open problem{l.open_issues === 1 ? "" : "s"}</span></footer>
      </section>);
  }
  return (
    <section className={`panel tv-line ${l.state.toLowerCase()}`} aria-label={name}>
      <header>
        <h2><Link to={`/floor/line?line=${l.line_id}&day=${day}`}>{name}</Link></h2>
        <StateChip state={l.state} />
      </header>
      <p className="muted">Pieces sewn today</p>
      <p className="tv-big"><span>{l.actual_day}</span><small>/ {l.target_day}</small></p>
      <div className="tv-track" role="img" aria-label={`${l.actual_day} recorded, ${l.target_so_far} planned by now, ${l.target_day} planned today`}>
        <i className="tv-plan" style={{ width: share(l.target_so_far) }} />
        <i className="tv-done" style={{ width: share(l.actual_day) }} />
      </div>
      <p className="tv-gap">
        <span className="warn-text">Plan by now: {l.target_so_far}</span>
        <span className={gap > 0 ? "late-text" : "ontime-text"}>{gap > 0 ? `Behind by ${gap}` : gap < 0 ? `Ahead by ${-gap}` : "On plan"}</span>
      </p>
      <div className="tv-hours">
        {l.hours.map((h) => {
          const past = h.hour < l.hours_passed;
          const behind = past && h.target > 0 && h.actual < 0.85 * h.target;
          return (
            <div key={h.hour} className={`tv-hour${behind ? " behind" : ""}`}>
              <small>{String(h.hour).padStart(2, "0")}:00</small>
              <strong>{past || h.actual ? h.actual : "—"}</strong>
              <small>of {Math.round(h.target)}</small>
            </div>);
        })}
      </div>
      <footer className="small muted">
        <span>{l.operations} operation{l.operations === 1 ? "" : "s"} today</span>
        <span className={l.open_issues ? "late-text" : undefined}>{l.open_issues} open problem{l.open_issues === 1 ? "" : "s"}</span>
        <span>DHU {l.dhu === null ? "not checked" : num(l.dhu, 1)}</span>
      </footer>
    </section>
  );
}

/** Every line on one day: recorded against planned output, refreshed every 30 seconds.
 *  "Show on a screen" puts the board full screen for a TV on the floor. */
export default function Board() {
  const ready = useReadyScope();
  const clock = useClock();
  const [day, setDay] = useState<number | undefined>(undefined);
  const lines = useFloorLines(ready?.[0], ready?.[1]);
  const board = useQuery({ queryKey: ["floor-board", ready, day], queryFn: () => floor.board(ready![0], ready![1], day),
    enabled: Boolean(ready), refetchInterval: 30_000 });
  const issues = useQuery({ queryKey: ["floor-issues", ready, true], queryFn: () => floor.issues(ready![0], ready![1], true),
    enabled: Boolean(ready), refetchInterval: 30_000 });
  const box = useRef<HTMLDivElement>(null);
  const fullscreen = () => { box.current?.requestFullscreen?.().catch(() => undefined); };

  if (!ready || board.isPending) return <Loading label="Loading the line board" />;
  if (board.isError) return <Notice tone="error">{board.error.message}</Notice>;
  const b = board.data;
  if (!b.plan) {
    return (<><PageHead title="Line board" />
      <div className="panel empty"><p>No plan has been approved for this factory yet, so there is nothing to track.</p></div></>);
  }
  const down = (issues.data?.issues ?? []).filter((i) => i.kind === "MACHINE_DOWN" && i.status !== "RESOLVED");
  // A line with planned work but no output entered is not "on plan": nobody knows yet.
  const problem = b.open_issues > 0 || b.lines.some((l) => l.state === "BEHIND");
  const unknown = !problem && b.lines.some((l) => l.state === "NO_DATA");
  const attention = problem || unknown;
  const andonText = problem ? "Attention" : unknown ? "Output not entered" : "All on plan";
  return (
    <>
      <PageHead title="Line board" lead="Every line against the plan. Updates on its own.">
        <button type="button" className="btn" onClick={fullscreen}>Show on a screen</button>
      </PageHead>
      <DayNav days={b.days ?? []} day={b.day ?? 0} today={b.today ?? -1} date={b.date ?? ""} onChange={setDay} />
      <div className="board tv" ref={box}>
        <div className="tv-head">
          <strong>Factory {ready[1]}</strong>
          <span className="muted">{b.date ? new Date(`${b.date}T00:00:00`).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }) : ""}</span>
          <span className="tv-spacer" />
          <span className={`andon ${attention ? "attention" : "ok"}`} role="status"><i aria-hidden="true" />{andonText}</span>
          <span className="tv-clock">{clock}</span>
        </div>
        {down.length > 0 && (
          <div className="tv-down">
            {down.map((i) => (
              <div key={i.id} className="tv-down-card">
                <small>Machine down{i.status === "IN_PROGRESS" ? " · being repaired" : ""}</small>
                <strong>Machine {i.machine_id ?? "?"}</strong>
                <span>{lineName(lines.data?.lines, i.line_id)}{i.note ? ` · ${i.note}` : ""}</span>
              </div>))}
          </div>)}
        <div className="board-lines">
          {b.lines.map((l) => <LineTile key={l.line_id} l={l} name={lineName(lines.data?.lines, l.line_id)} day={b.day ?? 0} />)}
          {!b.lines.length && <p className="muted">No line of yours has work in this plan.</p>}
        </div>
      </div>
    </>
  );
}
