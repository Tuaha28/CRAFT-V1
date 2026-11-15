import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { erp } from "../../lib/api";
import { num, prettyLine } from "../../lib/format";
import { useReadyScope } from "../../app/scope";
import { Chip, DataTable, Meter, Pager, useListParams } from "../../components/DataTable";
import { Loading, Measure, Notice, PageHead, PageTabs, RESOURCE_TABS } from "../../components/ui";

const SIZE = 50;

export default function Workforce() {
  const ready = useReadyScope();
  const { get, set, page } = useListParams();
  const params = { q: get("q"), role: get("role"), page, size: SIZE };
  const data = useQuery({ queryKey: ["erp-workforce", ready, params], queryFn: () => erp.workforce(ready![0], ready![1], params),
    enabled: Boolean(ready), placeholderData: keepPreviousData });
  if (!ready || data.isPending) return <Loading />;
  if (data.isError) return <Notice tone="error">{data.error.message}</Notice>;
  const { items, skills, crews, roles, total, reference_date } = data.data;
  const maxOps = Math.max(...skills.map((s) => Number(s.operators)), 1);
  return (
    <>
      <PageHead title="Workforce" lead={`Operators, crews and skills of this factory. Shifts and leave are for ${reference_date}.`} />
      <PageTabs items={RESOURCE_TABS} label="Resources" />
      <div className="measures">
        {roles.map((r) => <Measure key={String(r.role)} value={Number(r.n)} label={String(r.role).replace("_", " ").toLowerCase() + "s"} />)}
      </div>
      <details className="panel section more">
        <summary>Show crews and skills</summary>
        <div className="grid-2">
        <section className="section">
          <h2>Crews and shift rotation</h2>
          <p className="muted small">Crews rotate morning, evening and night shifts weekly and rest one day a week.</p>
          <DataTable rows={crews} columns={[
            { key: "batch_name", label: "Crew" }, { key: "line", label: "Line", render: (r) => prettyLine(r.line) }, { key: "members", label: "People", num: true },
            { key: "shift_today", label: "Shift today", render: (c) => c.resting_today ? <Chip value="Rest day" /> : c.shift_today },
            { key: "rest_day", label: "Rest day" }, { key: "primary_skill", label: "Main skill" }]} />
        </section>
        <section className="section">
          <h2>Skills</h2>
          <p className="muted small">Operators qualified for each operation and their mean efficiency rating.</p>
          <DataTable rows={skills} columns={[
            { key: "operation_type", label: "Operation" },
            { key: "operators", label: "Operators", num: true },
            { key: "bar", label: "", render: (s) => <Meter value={Number(s.operators)} max={maxOps} /> },
            { key: "mean_efficiency", label: "Mean efficiency", num: true, render: (s) => `${num(Number(s.mean_efficiency), 1)}%` }]} />
        </section>
        </div>
      </details>
      <section className="panel section">
        <h2>People</h2>
        <div className="toolbar">
          <div className="field"><label htmlFor="wq">Search</label>
            <input id="wq" defaultValue={get("q")} placeholder="Name or employee number" onChange={(e) => set({ q: e.target.value })} /></div>
          <div className="field"><label htmlFor="wr">Role</label>
            <select id="wr" value={get("role")} onChange={(e) => set({ role: e.target.value })}>
              <option value="">Everyone</option>
              {roles.map((r) => <option key={String(r.role)} value={String(r.role)}>{String(r.role).replace("_", " ")}</option>)}</select></div>
        </div>
        <DataTable rows={items} empty="Nobody matches." columns={[
          { key: "employee_id", label: "No.", num: true },
          { key: "name", label: "Name", render: (p) => `${p.first_name} ${p.last_name}` },
          { key: "role", label: "Role", render: (p) => String(p.role).replace("_", " ") },
          { key: "line", label: "Line", render: (r) => prettyLine(r.line) },
          { key: "shift", label: `Shift on ${reference_date}`, render: (p) => p.is_on_leave ? <Chip value="On leave" />
            : p.resting_today ? <Chip value="Rest day" />
            : p.start_time ? `${p.shift_name ? `${p.shift_name} ` : ""}${String(p.start_time).slice(0, 5)}–${String(p.end_time).slice(0, 5)}` : "—" },
          { key: "efficiency", label: "Mean efficiency", num: true, render: (p) => p.efficiency == null ? "—" : `${num(Number(p.efficiency), 1)}%` }]} />
        <Pager page={page} size={SIZE} total={total} onPage={(p) => set({ page: String(p) })} />
      </section>
    </>
  );
}
