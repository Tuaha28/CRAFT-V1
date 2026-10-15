import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Gantt, type IApi } from "@svar-ui/react-gantt";
import { Link } from "react-router-dom";
import { governance, type Candidate, type ScheduleRow } from "../services/governance";
import { Drawer } from "../components/Dialogs";
import { Badge, EmptyBlock, ErrorBlock, LoadingBlock, PageTitle, Panel } from "../components/Primitives";
import "./controlledSchedule.css";

type TimelineTask = {
  id: string; text: string; start: Date; end: Date; progress: number;
  type: "task" | "summary"; parent?: string; css?: string; open?: boolean; status?: string;
};
type Selection = { row: ScheduleRow; version: "candidate" | "active-before" };
const columns = [
  { id: "text", header: "Line / operation", width: 210, template: (task: TimelineTask) => task.text },
  { id: "status", header: "Schedule state", width: 125, template: (task: TimelineTask) => task.status ?? "" },
];

function validRow(row: ScheduleRow) {
  return Number.isFinite(row.start_seconds) && Number.isFinite(row.end_seconds) &&
    row.end_seconds > row.start_seconds && Number.isInteger(row.resource_configuration.line_id);
}

export default function ControlledSchedule() {
  const session = useQuery({ queryKey: ["governance", "session"], queryFn: governance.session, retry: false });
  const candidates = useQuery({ queryKey: ["governance", "candidates"], queryFn: governance.candidates,
    enabled: session.data?.authenticated === true, refetchInterval: 5000 });
  const [selectedId, setSelectedId] = useState("");
  const [selected, setSelected] = useState<Selection | null>(null);
  const [zoom, setZoom] = useState<"hour" | "day">("day");
  const versions = useMemo(() => (candidates.data?.results ?? []).filter(
    candidate => ["ACTIVE", "VALIDATED_CANDIDATE"].includes(candidate.status)), [candidates.data]);
  const current: Candidate | undefined = versions.find(candidate => candidate.id === selectedId) ??
    versions.find(candidate => candidate.status === "ACTIVE") ?? versions[0];
  const sourceLabel = current?.evidence?.source === "SIMULATED_FACTORY1_PARTIAL_CHAIN"
    ? "FACTORY-1 SIMULATED CONTINUATION" : current?.evidence?.source === "SYNTHETIC_PRIORITY_FIXTURE"
      ? "TWO-ORDER SYNTHETIC PRIORITY TEST" : "CONTROLLED SYNTHETIC FIXTURE";
  const comparison = useQuery({ queryKey: ["governance", "comparison", current?.id],
    queryFn: () => governance.compare(current!.id), enabled: !!current?.id });
  const timeline = useMemo(() => {
    const value = comparison.data;
    if (!current || !value?.reference_time || value.validation?.candidate_validated !== true) return { tasks: [] as TimelineTask[], selectedRows: new Map<string, Selection>(), invalid: 0 };
    const origin = new Date(value.reference_time).getTime();
    if (!Number.isFinite(origin)) return { tasks: [] as TimelineTask[], selectedRows: new Map<string, Selection>(), invalid: current.operations.length };
    const items: TimelineTask[] = [];
    const byId = new Map<string, Selection>();
    const lines = new Map<number, TimelineTask[]>();
    let invalid = 0;
    const add = (row: ScheduleRow, version: Selection["version"]) => {
      if (!validRow(row)) { invalid += 1; return; }
      const line = row.resource_configuration.line_id;
      const id = `${version}-${row.operation_key}`;
      const task: TimelineTask = {
        id, text: row.operation_key, start: new Date(origin + row.start_seconds * 1000),
        end: new Date(origin + row.end_seconds * 1000), progress: 0, type: "task",
        parent: `line-${line}`, css: version === "active-before" ? "task-active-before" : "task-validated-candidate",
        status: version === "active-before" ? "ACTIVE BEFORE" : current.status === "ACTIVE" ? "APPROVED TEST" : "CANDIDATE",
      };
      lines.set(line, [...(lines.get(line) ?? []), task]);
      byId.set(id, { row, version });
    };
    for (const change of value.changes) {
      if (change.before && change.changed && value.active_version !== current.id) add(change.before, "active-before");
    }
    for (const row of current.operations) add(row, "candidate");
    for (const [line, tasks] of [...lines.entries()].sort((a, b) => a[0] - b[0])) {
      items.push({ id: `line-${line}`, text: `Line ${line}`, type: "summary", open: true,
        start: new Date(Math.min(...tasks.map(task => task.start.getTime()))),
        end: new Date(Math.max(...tasks.map(task => task.end.getTime()))), progress: 0, status: "TEST SCOPE" });
      items.push(...tasks);
    }
    return { tasks: items, selectedRows: byId, invalid };
  }, [comparison.data, current]);
  const start = timeline.tasks.length ? new Date(Math.min(...timeline.tasks.map(task => task.start.getTime())) - 3600000) : undefined;
  const end = timeline.tasks.length ? new Date(Math.max(...timeline.tasks.map(task => task.end.getTime())) + 3600000) : undefined;
  const onInit = (api: IApi) => {
    api.on("select-task", ({ id }) => setSelected(timeline.selectedRows.get(String(id)) ?? null));
    api.intercept("update-task", () => false);
    api.intercept("add-task", () => false);
    api.intercept("delete-task", () => false);
  };
  if (session.isLoading) return <LoadingBlock label="Checking controlled scheduling access" />;
  if (session.isError) return <ErrorBlock message="Controlled scheduling API is unavailable. Start the opt-in Django integration backend." retry={() => void session.refetch()} />;
  if (!session.data?.authenticated) return <><PageTitle eyebrow="CONTROLLED TEST" title="CRAFT schedule" description="Sign in to view application-owned validated schedule versions."/><EmptyBlock title="Scheduling access required" description="Sign in through Scheduling governance; demo schedule data remains separate."/><Link className="button" to="/governance">Open scheduling governance</Link></>;
  if (candidates.isLoading) return <LoadingBlock label="Loading validated TEST schedules" />;
  if (candidates.isError) return <ErrorBlock message={String(candidates.error)} retry={() => void candidates.refetch()} />;
  return <><PageTitle eyebrow="CONTROLLED TEST · CRAFT OUTPUT" title="Production schedule" description="Application-owned validated candidate and approved TEST versions. These are not observed production execution." actions={<Link className="button button-quiet" to="/governance">Manager review</Link>}/>
    <div className="craft-demo-notice"><Badge tone="warning">TEST ONLY</Badge><span>{sourceLabel}. Real Factory-1 production admission remains blocked. No simulated candidate is a live factory schedule. Shared header factory and shift indicators are illustrative; this scope is selected through governance.</span></div>
    {!versions.length ? <EmptyBlock title="No validated TEST schedule" description="Request a controlled CRAFT run in Scheduling governance. Failed or unvalidated runs never appear here."/> : <>
      <div className="toolbar schedule-toolbar"><label htmlFor="controlled-version">Schedule version</label><select id="controlled-version" value={current?.id ?? ""} onChange={event => { setSelectedId(event.target.value); setSelected(null); }}>{versions.map(version => <option key={version.id} value={version.id}>{version.id.slice(0, 8)} · {version.status === "ACTIVE" ? "APPROVED TEST" : "VALIDATED CANDIDATE"}</option>)}</select><span className="toolbar-spacer"/><button className="button button-quiet button-sm" onClick={() => setZoom(zoom === "hour" ? "day" : "hour")}>Zoom: {zoom}</button></div>
      {comparison.isLoading && <LoadingBlock label="Loading version and validation evidence"/>}
      {comparison.isError && <ErrorBlock message={String(comparison.error)} retry={() => void comparison.refetch()}/>}
      {comparison.data && <Panel title={current?.status === "ACTIVE" ? "Approved TEST schedule" : "Validated candidate — awaiting manager decision"} subtitle={`Fixture reference ${comparison.data.reference_time ?? "unavailable"} · ${comparison.data.changed_operation_count} changed operations versus current TEST schedule`}>
        {comparison.data.validation?.candidate_validated !== true && <div className="schedule-callout"><strong>Stored validation evidence is incomplete or invalid. Operation bars are withheld.</strong></div>}
        {timeline.invalid > 0 && <div className="schedule-callout"><strong>{timeline.invalid} operation rows have invalid time or line data and were withheld from the timeline.</strong></div>}
        {timeline.tasks.length && start && end ? <div className="gantt-stage"><div className="gantt-host" data-testid="controlled-schedule-gantt"><Gantt key={`${current?.id}-${zoom}`} tasks={timeline.tasks} columns={columns} scales={zoom === "hour" ? [{ unit: "day", step: 1, format: "%d %M" }, { unit: "hour", step: 1, format: (date: Date) => date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) }] : [{ unit: "week", step: 1, format: "Week %W" }, { unit: "day", step: 1, format: "%D %d" }]} readonly init={onInit} durationUnit="hour" lengthUnit="minute" cellWidth={80} cellHeight={34} gridWidth={335} start={start} end={end}/></div></div> : <EmptyBlock title="No renderable operation bars" description="The selected version has no validated operation interval for this timeline."/>}
        <footer className="schedule-footer"><span>Solid bars: selected validated TEST version. Outlined bars: changed assignments in the prior active TEST version.</span><span>Fixture-local wall clock; not live telemetry.</span></footer>
      </Panel>}
    </>}
    <Drawer open={!!selected} onOpenChange={open => !open && setSelected(null)} title={selected?.row.operation_key ?? "Operation"} description={selected?.version === "active-before" ? "Prior approved TEST assignment" : "Selected validated TEST assignment"}>{selected && <div className="detail-grid">{[
      ["Schedule state", selected.version === "active-before" ? "Prior active TEST" : current?.status === "ACTIVE" ? "Approved TEST" : "Validated candidate"],
      ["Operation", selected.row.operation_key], ["Line", String(selected.row.resource_configuration.line_id)],
      ["Machine", selected.row.resource_configuration.machine_ids.join(", ")],
      ["Worker crew", selected.row.resource_configuration.worker_ids.join(", ")],
      ["Start offset", `${selected.row.start_seconds} seconds after fixture reference`],
      ["End offset", `${selected.row.end_seconds} seconds after fixture reference`],
    ].map(([label, value]) => <div className="detail-field" key={label}><label>{label}</label><strong>{value}</strong></div>)}</div>}</Drawer>
  </>;
}
