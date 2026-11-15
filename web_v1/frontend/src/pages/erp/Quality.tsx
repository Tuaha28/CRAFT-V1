import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { erp } from "../../lib/api";
import { num, prettyLine } from "../../lib/format";
import { useReadyScope } from "../../app/scope";
import { DataTable, Meter } from "../../components/DataTable";
import { Loading, Notice, PageHead, PageTabs, QUALITY_TABS } from "../../components/ui";

const TARGET_DHU = 5; // a common garment-industry ceiling for defects per hundred units

export default function Quality() {
  const ready = useReadyScope();
  const data = useQuery({ queryKey: ["erp-quality", ready], queryFn: () => erp.quality(ready![0], ready![1]), enabled: Boolean(ready) });
  if (!ready || data.isPending) return <Loading />;
  if (data.isError) return <Notice tone="error">{data.error.message}</Notice>;
  const { totals, by_line, by_operation, by_type, worst_orders } = data.data;
  const dhu = Number(totals.dhu);
  const maxType = Math.max(...by_type.map((t) => Number(t.defective)), 1);
  // MySQL decimals arrive as text; the chart needs numbers.
  const ops = by_operation.slice(0, 12).map((o) => ({ operation_name: String(o.operation_name), dhu: Number(o.dhu) }));
  const scale = Math.max(TARGET_DHU * 2, dhu, ...by_line.map((l) => Number(l.dhu))) || 1;
  const pct = (v: number) => `${Math.min(100, (v / scale) * 100)}%`;
  return (
    <>
      <PageHead title="Quality" lead={`Defects per 100 pieces checked (DHU). Below ${TARGET_DHU} is a common target.`} />
      <PageTabs items={QUALITY_TABS} label="Quality" />
      <section className={`panel section hero quality-hero${dhu > TARGET_DHU ? " over" : ""}`} aria-labelledby="dhu-title">
        <div>
          <p className="eyebrow" id="dhu-title">DHU, all lines</p>
          <p className="quality-big"><span className={dhu > TARGET_DHU ? "late-text" : "ontime-text"}>{num(dhu, 1)}</span>
            <small>{dhu > TARGET_DHU ? `above the target of ${TARGET_DHU}` : `within the target of ${TARGET_DHU}`}</small></p>
          <p className="muted small">{Number(totals.defective ?? 0).toLocaleString("en-US")} defective in {Number(totals.inspected ?? 0).toLocaleString("en-US")} pieces checked.</p>
        </div>
        <ul className="dhu-lines" aria-label="DHU by line">
          {by_line.map((l) => (
            <li key={String(l.line)}>
              <span className="dhu-line-top"><strong>{prettyLine(l.line)}</strong><span className={Number(l.dhu) > TARGET_DHU ? "late-text" : undefined}>{num(Number(l.dhu), 1)}</span></span>
              <span className="dhu-track"><i className={Number(l.dhu) > TARGET_DHU ? "over" : ""} style={{ width: pct(Number(l.dhu)) }} /><b style={{ left: pct(TARGET_DHU) }} aria-hidden="true" /></span>
            </li>))}
          <li className="muted small">Marker: target {TARGET_DHU}</li>
        </ul>
      </section>
      <div className="grid-2">
        <section className="panel section"><h2>DHU by operation</h2>
          <p className="muted small">Operations with at least 100 pieces checked, worst first.</p>
          <div className="chart" style={{ height: 360 }}>
            <ResponsiveContainer>
              <BarChart data={ops} layout="vertical" margin={{ left: 40, right: 16 }}>
                <CartesianGrid horizontal={false} stroke="var(--rule)" />
                <XAxis type="number" tick={{ fill: "var(--ink-2)", fontSize: 12 }} />
                <YAxis type="category" dataKey="operation_name" width={130} tick={{ fill: "var(--ink-2)", fontSize: 11 }} />
                <Tooltip contentStyle={{ background: "var(--popover)", border: "1px solid var(--rule-strong)", borderRadius: 12 }} formatter={(v) => [`${v}`, "DHU"]} />
                <Bar dataKey="dhu" radius={[0, 6, 6, 0]}>
                  {ops.map((o) => <Cell key={o.operation_name} fill={o.dhu > TARGET_DHU ? "var(--late)" : "var(--accent)"} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>
        <section className="panel section"><h2>Most common defects</h2>
          <DataTable rows={by_type} columns={[{ key: "defect", label: "Defect" },
            { key: "defective", label: "Pieces", num: true, render: (t) => Number(t.defective).toLocaleString("en-US") },
            { key: "bar", label: "Share", render: (t) => <Meter value={Number(t.defective)} max={maxType} bad /> }]} /></section>
      </div>
      <div className="grid-2">
        <section className="panel section"><h2>By line</h2>
          <DataTable rows={by_line} columns={[{ key: "line", label: "Line", render: (l) => prettyLine(l.line) }, { key: "inspected", label: "Checked", num: true, render: (l) => Number(l.inspected).toLocaleString("en-US") },
            { key: "defective", label: "Defective", num: true, render: (l) => Number(l.defective).toLocaleString("en-US") },
            { key: "dhu", label: "DHU", num: true, render: (l) => <span className={Number(l.dhu) > TARGET_DHU ? "late-text" : undefined}>{num(Number(l.dhu), 2)}</span> },
            { key: "target_eff", label: "Target efficiency", num: true, render: (l) => `${num(Number(l.target_eff), 1)}%` }]} /></section>
        <section className="panel section"><h2>Orders with the most defects</h2>
          <DataTable rows={worst_orders} columns={[
            { key: "po_number", label: "Order", render: (o) => <><Link to={`/orders/${o.production_order_id}`}>Order {String(o.production_order_id)}</Link><br /><span className="muted small">{String(o.po_number)}</span></> },
            { key: "client_name", label: "Client" }, { key: "inspected", label: "Checked", num: true },
            { key: "dhu", label: "DHU", num: true, render: (o) => num(Number(o.dhu), 2) }]} /></section>
      </div>
      <p className="muted small">The factory's quality records carry no usable dates, so this page shows quality by line, operation and defect rather than a trend over time. Checks entered on the floor are under "Checks on the floor".</p>
    </>
  );
}
