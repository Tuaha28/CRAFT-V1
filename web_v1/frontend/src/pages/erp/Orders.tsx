import { useState } from "react";
import { Link } from "react-router-dom";
import { keepPreviousData, useQueries, useQuery } from "@tanstack/react-query";
import { erp, type Row } from "../../lib/api";
import { num } from "../../lib/format";
import { useReadyScope } from "../../app/scope";
import { Chip, DataTable, Meter, Pager, useListParams } from "../../components/DataTable";
import { CountUp } from "../../components/CountUp";
import { RingCard } from "../../components/RingCard";
import { Loading, Notice, PageHead } from "../../components/ui";
import { PriorityChip, usePriority } from "../../components/Priority";
import { useSession } from "../../app/session";

const STATUSES = [["", "All"], ["OVERDUE", "Overdue"], ["Confirmed", "Not started"], ["In_Production", "In production"], ["Shipped", "Shipped"]] as const;
const SIZE = 50;

const overdue = (r: Row) => r.po_status !== "Shipped" && Number(r.days_to_deadline) < 0;

function DaysLeft({ row }: { row: Row }) {
  const d = Number(row.days_to_deadline);
  if (row.po_status === "Shipped") return <span className="muted">shipped</span>;
  return d < 0 ? <span className="late-text">{-d} days overdue</span> : <span>{d === 0 ? "due today" : `${d} days left`}</span>;
}

/** The order picked in the table: what it is, where it stands, and the way to its full page. */
function OrderCard({ row }: { row: Row }) {
  const ready = useReadyScope();
  const id = Number(row.production_order_id);
  const detail = useQuery({ queryKey: ["erp-order", ready, String(id)], queryFn: () => erp.order(ready![0], ready![1], id), enabled: Boolean(ready) });
  const qty = Number(row.target_quantity);
  const routing = detail.data?.routing ?? [];
  const done = routing.length ? Math.min(...routing.map((r) => Number(r.pieces_done))) : 0;
  const inspected = routing.reduce((s, r) => s + Number(r.inspected), 0);
  const defective = routing.reduce((s, r) => s + Number(r.defective), 0);
  return (
    <aside className="panel section order-card" aria-label={`Order ${row.production_order_id}`}>
      <div key={id} className="order-card-body">
        <div className="section-head"><div><h2>Order {String(row.production_order_id)}</h2><span className="muted small">{String(row.po_number)}</span></div><Chip value={overdue(row) ? "Overdue" : row.po_status} /></div>
        <p className="muted">{String(row.client_name)}, style <Link to={`/styles/${row.product_id}`}>{String(row.style_code)}</Link> ({String(row.category)})</p>
        <dl className="facts">
          <dt>Pieces</dt><dd>{qty.toLocaleString()}</dd>
          <dt>Due</dt><dd>{String(row.delivery_deadline)}, <DaysLeft row={row} /></dd>
          <dt>Shipping</dt><dd>{row.delivery_status ? <Chip value={row.delivery_status} /> : "No shipment yet"}</dd>
        </dl>
        {detail.isPending ? <Loading label="Reading progress" /> : detail.isError ? <Notice tone="error">{detail.error.message}</Notice> : (
          <div className="order-progress">
            <span className="small">Pieces through every step: <strong>{done.toLocaleString()} of {qty.toLocaleString()}</strong></span>
            <Meter value={done} max={qty} />
            <span className="small muted">{inspected ? `DHU ${num((100 * defective) / inspected, 1)} on ${inspected.toLocaleString()} pieces checked` : "No quality checks yet"}</span>
          </div>)}
        <Link className="btn primary" to={`/orders/${id}`}>Open order</Link>
      </div>
    </aside>
  );
}

export default function Orders() {
  const ready = useReadyScope();
  const { get, set, page } = useListParams({ sort: "deadline" });
  const [picked, setPicked] = useState<number | null>(null);
  const status = get("status");
  const params = { q: get("q"), status, sort: get("sort"), page, size: SIZE };
  const priority = usePriority(ready?.[0], ready?.[1], useSession().can_request);
  const data = useQuery({ queryKey: ["erp-orders", ready, params], queryFn: () => erp.orders(ready![0], ready![1], params),
    enabled: Boolean(ready), placeholderData: keepPreviousData });
  // One cheap count per status (a page of one row; only the total is read).
  const counts = useQueries({ queries: STATUSES.map(([s]) => ({
    queryKey: ["erp-orders-count", ready, s], queryFn: () => erp.orders(ready![0], ready![1], { status: s, size: 1 }), enabled: Boolean(ready) })) });
  const count = (s: string) => counts[STATUSES.findIndex(([k]) => k === s)]?.data?.total;
  const all = count("") ?? 0;
  const shown = (s: string) => count(s) == null ? "—" : <CountUp value={count(s)!} />;
  const filter = (s: string) => set({ status: status === s ? "" : s });

  const items = data.data?.items ?? [];
  const current = items.find((r) => Number(r.production_order_id) === picked) ?? items[0];

  return (
    <>
      <PageHead title="Orders" lead="Purchase orders for this factory, nearest due date first." />
      <div className="ring-row">
        <RingCard label="Overdue" value={shown("OVERDUE")} note={count("") == null ? "counting…" : `of ${all} orders`} share={(count("OVERDUE") ?? 0) / Math.max(1, all)}
          tone={count("OVERDUE") == null ? undefined : count("OVERDUE") ? "late" : "ok"} onClick={() => filter("OVERDUE")} pressed={status === "OVERDUE"} />
        <RingCard label="Not started" value={shown("Confirmed")} note="confirmed, waiting" share={(count("Confirmed") ?? 0) / Math.max(1, all)}
          onClick={() => filter("Confirmed")} pressed={status === "Confirmed"} />
        <RingCard label="In production" value={shown("In_Production")} note="on the lines" share={(count("In_Production") ?? 0) / Math.max(1, all)}
          onClick={() => filter("In_Production")} pressed={status === "In_Production"} />
        <RingCard label="Shipped" value={shown("Shipped")} note={count("") == null ? "counting…" : `of ${all} orders`} share={(count("Shipped") ?? 0) / Math.max(1, all)}
          tone={count("Shipped") == null ? undefined : "ok"} onClick={() => filter("Shipped")} pressed={status === "Shipped"} />
      </div>
      <section className="panel section">
        <div className="toolbar">
          <div className="tabs" role="tablist" aria-label="Order status">
            {STATUSES.map(([s, label]) => (
              <button key={s} type="button" role="tab" aria-selected={status === s} className={status === s ? "on" : ""} onClick={() => set({ status: s })}>
                {label}{count(s) != null && <span className="tab-count">{count(s)}</span>}</button>))}
          </div>
          <div className="field"><label htmlFor="oq">Search</label>
            <input id="oq" defaultValue={get("q")} placeholder="Order, PO, client or style" onChange={(e) => set({ q: e.target.value })} /></div>
          <div className="field"><label htmlFor="osort">Sort by</label>
            <select id="osort" value={get("sort")} onChange={(e) => set({ sort: e.target.value })}>
              <option value="deadline">Due date</option><option value="ordered">Order date</option>
              <option value="client">Client</option><option value="quantity">Pieces</option><option value="po">PO number</option></select></div>
        </div>
        {!ready || data.isPending ? <Loading /> : data.isError ? <Notice tone="error">{data.error.message}</Notice> : (
          <div className="orders-layout">
            <div>
              <DataTable rows={items} empty="No orders match." onRow={(r) => setPicked(Number(r.production_order_id))}
                selected={(r) => r === current}
                columns={[
                  { key: "po_number", label: "Order", render: (r) => <><strong>Order {String(r.production_order_id)}</strong> <PriorityChip on={priority.includes(String(r.production_order_id))} /><br /><span className="muted small">{String(r.po_number)} · {String(r.client_name)}</span></> },
                  { key: "style_code", label: "Style", render: (r) => <>{String(r.style_code)}<br /><span className="muted small">{String(r.category)}</span></> },
                  { key: "target_quantity", label: "Pieces", num: true, render: (r) => Number(r.target_quantity).toLocaleString() },
                  { key: "delivery_deadline", label: "Due", render: (r) => <><span className="nowrap">{String(r.delivery_deadline)}</span><br /><span className="small"><DaysLeft row={r} /></span></> },
                  { key: "po_status", label: "Status", render: (r) => <Chip value={overdue(r) ? "Overdue" : r.po_status} /> },
                ]} />
              <Pager page={page} size={SIZE} total={data.data.total} onPage={(p) => set({ page: String(p) })} />
            </div>
            {current && <OrderCard row={current} />}
          </div>)}
      </section>
    </>
  );
}
