import { prettyLine } from "../../lib/format";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { erp } from "../../lib/api";
import { useReadyScope } from "../../app/scope";
import { Chip, DataTable, Meter, Pager, useListParams } from "../../components/DataTable";
import { Loading, Measure, Notice, PageHead, PageTabs, RESOURCE_TABS } from "../../components/ui";

const SIZE = 50;
const STATUS = ["Active", "Idle", "Under_Maintenance", "Breakdown"];

export default function Machines() {
  const ready = useReadyScope();
  const { get, set, page } = useListParams();
  const params = { q: get("q"), status: get("status"), line: get("line"), page, size: SIZE };
  // Every machine, unfiltered, for the floor map (same query the home page uses).
  const all = useQuery({ queryKey: ["erp-machines", ready, "all"], enabled: Boolean(ready),
    queryFn: () => erp.machines(ready![0], ready![1], { size: 200 }) });
  const data = useQuery({ queryKey: ["erp-machines", ready, params], queryFn: () => erp.machines(ready![0], ready![1], params),
    enabled: Boolean(ready), placeholderData: keepPreviousData });
  if (!ready || data.isPending) return <Loading />;
  if (data.isError) return <Notice tone="error">{data.error.message}</Notice>;
  const { items, by_line, by_type, total } = data.data;
  const sum = (k: string) => by_line.reduce((s, l) => s + Number(l[k] ?? 0), 0);
  const machines = sum("machines");
  return (
    <>
      <PageHead title="Machines" lead="Sewing machines of this factory, their current state and what operations each can run." />
      <PageTabs items={RESOURCE_TABS} label="Resources" />
      <div className="measures">
        <Measure value={machines} label="machines" />
        <Measure value={machines ? `${Math.round((100 * sum("active")) / machines)}%` : "—"} label="available (active)" tone="ontime" />
        <Measure value={sum("maintenance")} label="under maintenance" />
        <Measure value={sum("breakdown")} label="broken down" tone={sum("breakdown") ? "late" : undefined} />
      </div>
      {all.data && <FloorMap machines={all.data.items} />}
      <details className="panel section more">
        <summary>Show machines by line and by type</summary>
        <div className="grid-2">
        <section className="section"><h2>By line</h2>
          <DataTable rows={by_line} columns={[{ key: "line", label: "Line", render: (r) => prettyLine(r.line) }, { key: "machines", label: "Machines", num: true },
            { key: "active", label: "Active", num: true }, { key: "maintenance", label: "Maintenance", num: true },
            { key: "breakdown", label: "Breakdown", num: true }, { key: "idle", label: "Idle", num: true },
            { key: "bar", label: "Availability", render: (l) => <Meter value={Number(l.active)} max={Number(l.machines)} /> }]} /></section>
        <section className="section"><h2>By machine type</h2>
          <DataTable rows={by_type} columns={[{ key: "operation_type", label: "Can run" }, { key: "machines", label: "Machines", num: true },
            { key: "active", label: "Active", num: true }]} /></section>
        </div>
      </details>
      <section className="panel section">
        <h2>Machine register</h2>
        <div className="toolbar">
          <div className="field"><label htmlFor="mq">Search</label>
            <input id="mq" defaultValue={get("q")} placeholder="Machine number, model or serial" onChange={(e) => set({ q: e.target.value })} /></div>
          <div className="field"><label htmlFor="ms">State</label>
            <select id="ms" value={get("status")} onChange={(e) => set({ status: e.target.value })}>
              <option value="">All</option>{STATUS.map((s) => <option key={s} value={s}>{s.replace("_", " ")}</option>)}</select></div>
          <div className="field"><label htmlFor="ml">Line</label>
            <select id="ml" value={get("line")} onChange={(e) => set({ line: e.target.value })}>
              <option value="">All lines</option>{by_line.map((l) => <option key={String(l.line)} value={String(l.line ?? "")}>{prettyLine(l.line)}</option>)}</select></div>
        </div>
        <DataTable rows={items} empty="No machines match." columns={[
          { key: "asset_id", label: "No.", num: true }, { key: "machine_model", label: "Model" }, { key: "line", label: "Line", render: (r) => prettyLine(r.line) },
          { key: "current_status", label: "State", render: (m) => <Chip value={m.current_status} /> },
          { key: "last_reason", label: "Last change", render: (m) => m.current_status === "Active"
            ? <span className="muted">Running</span> : String(m.last_reason ?? "—") },
          { key: "capabilities", label: "Can run" }]} />
        <Pager page={page} size={SIZE} total={total} onPage={(p) => set({ page: String(p) })} />
      </section>
    </>
  );
}

type MachineRow = Record<string, unknown>;

/** Every machine as a tile, grouped by line; machines that are not running stand out. */
function FloorMap({ machines }: { machines: MachineRow[] }) {
  const lines = new Map<string, MachineRow[]>();
  for (const m of machines) {
    const key = String(m.line ?? "");
    lines.set(key, [...(lines.get(key) ?? []), m]);
  }
  const tone = (s: unknown) => (s === "Active" ? "run" : s === "Breakdown" ? "down" : s === "Idle" ? "idle" : "fix");
  return (
    <section className="panel section hero" aria-labelledby="map-title">
      <div className="section-head"><h2 id="map-title">Floor map</h2>
        <ul className="map-key" aria-label="Key"><li><i className="k-run" />Running</li><li><i className="k-fix" />Maintenance</li>
          <li><i className="k-down" />Broken down</li><li><i className="k-idle" />Idle</li></ul></div>
      <div className="machine-lines">
        {[...lines.entries()].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true })).map(([line, list]) => (
          <div key={line} className="machine-line">
            <h3>{prettyLine(line)} <span className="muted small">{list.length} machines</span></h3>
            <div className="machine-tiles">
              {list.map((m) => (
                <span key={String(m.asset_id)} className={`mtile ${tone(m.current_status)}`}
                  title={`Machine ${m.asset_id}: ${String(m.current_status).replace("_", " ").toLowerCase()}${m.capabilities ? `, ${m.capabilities}` : ""}`}>
                  {String(m.asset_id)}
                </span>))}
            </div>
          </div>))}
      </div>
    </section>
  );
}
