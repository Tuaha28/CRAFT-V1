import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { erp, type ApiError } from "../../lib/api";
import { num, qty as quantity } from "../../lib/format";
import { useReadyScope } from "../../app/scope";
import { Chip, DataTable, Meter } from "../../components/DataTable";
import { Loading, Notice, PageHead } from "../../components/ui";
import { PriorityChip, PriorityControl, usePriority } from "../../components/Priority";
import { useSession } from "../../app/session";

type Stage = { name: string; sub: string; done: boolean };

/** Where the order is, from the factory records: PO status, production status, step progress and the shipment. */
function journey(o: Record<string, unknown>, delivery: Record<string, unknown> | null, done: number, ordered: number): Stage[] {
  const po = String(o.po_status ?? ""), prod = String(o.production_status ?? ""), ship = String(delivery?.delivery_status ?? "");
  const shipped = po === "Shipped" || ship === "In_Transit" || Boolean(delivery?.actual_shipment_date);
  const sewn = shipped || prod === "Completed" || (ordered > 0 && done >= ordered);
  const started = sewn || prod === "Processing" || po === "In_Production" || done > 0;
  const packed = shipped;
  return [
    { name: "Confirmed", sub: o.order_date ? `Ordered ${String(o.order_date).slice(0, 10)}` : "PO received", done: true },
    { name: "In production", sub: started ? "Started on the lines" : "Not started", done: started },
    { name: "All pieces sewn", sub: `${done.toLocaleString("en-US")} of ${ordered.toLocaleString("en-US")} through every step`, done: sewn },
    { name: "Packing", sub: ship === "Packing" ? "Being packed now" : packed ? "Packed" : "Not yet", done: packed },
    { name: "Shipped", sub: delivery?.actual_shipment_date ? `Shipped ${String(delivery.actual_shipment_date).slice(0, 10)}` : shipped ? "Shipped" : "Not yet", done: shipped },
  ];
}

function Ring({ value, max }: { value: number; max: number }) {
  const r = 62, c = 2 * Math.PI * r, share = max > 0 ? Math.min(1, value / max) : 0;
  return (
    <div className="ring-big" role="img" aria-label={`${value} of ${max} pieces through every step`}>
      <svg viewBox="0 0 160 160" aria-hidden="true">
        <circle cx="80" cy="80" r={r} fill="none" className="ring-track" strokeWidth="14" />
        <circle cx="80" cy="80" r={r} fill="none" stroke="var(--accent)" strokeWidth="14" strokeLinecap="round"
          strokeDasharray={`${share * c} ${c}`} transform="rotate(-90 80 80)" />
      </svg>
      <span><strong>{Math.round(share * 100)}%</strong><small>{value.toLocaleString("en-US")} / {max.toLocaleString("en-US")}</small></span>
    </div>
  );
}

export default function OrderPage() {
  const { id = "" } = useParams();
  const ready = useReadyScope();
  const data = useQuery({ queryKey: ["erp-order", ready, id], queryFn: () => erp.order(ready![0], ready![1], Number(id)), enabled: Boolean(ready) });
  const session = useSession();
  const planner = session.can_request;
  const priority = usePriority(ready?.[0], ready?.[1], planner);
  if (!ready || data.isPending) return <Loading />;
  if (data.isError) return <Notice tone="error">{(data.error as ApiError).status === 404 ? "This order is not in the chosen factory." : data.error.message}</Notice>;
  const { order: o, routing, materials, delivery } = data.data;
  const ordered = Number(o.target_quantity);
  const done = routing.length ? Math.min(...routing.map((r) => Number(r.pieces_done))) : 0;
  const inspected = routing.reduce((s, r) => s + Number(r.inspected), 0);
  const defective = routing.reduce((s, r) => s + Number(r.defective), 0);
  const dhu = inspected ? (100 * defective) / inspected : null;
  const stages = journey(o, delivery && Object.keys(delivery).length ? delivery : null, done, ordered);
  const now = stages.findIndex((st) => !st.done);
  return (
    <>
      <PageHead title={`Order ${o.production_order_id}`} lead={<>{o.po_number} · {o.client_name}, style <Link to={`/styles/${o.product_id}`}>{o.style_code}</Link> ({o.category}) · {ordered.toLocaleString("en-US")} pieces, due {String(o.delivery_deadline)}</>}>
        <PriorityChip on={priority.includes(String(o.production_order_id))} /><Chip value={o.po_status} /><Link className="btn" to="/orders">All orders</Link>
      </PageHead>

      <section className="panel section hero order-hero" aria-labelledby="journey-title">
        <div className="order-hero-main">
          <h2 id="journey-title">Where this order is</h2>
          <ol className="journey">
            {stages.map((st, i) => (
              <li key={st.name} className={st.done ? "done" : i === now ? "now" : ""} aria-current={i === now ? "step" : undefined}>
                <span className="journey-dot" aria-hidden="true">{st.done ? "✓" : i + 1}</span>
                <strong>{st.name}</strong><small>{st.sub}</small>
              </li>))}
          </ol>
        </div>
        <Ring value={done} max={ordered} />
      </section>

      <div className="plan-layout">
        <section className="panel section">
          <h2>Progress by step</h2>
          <DataTable rows={routing} columns={[
            { key: "step", label: "Step", num: true }, { key: "operation_name", label: "Operation" },
            { key: "pieces_done", label: "Pieces done", num: true, render: (r) => Number(r.pieces_done).toLocaleString("en-US") },
            { key: "progress", label: "Progress", render: (r) => <Meter value={Number(r.pieces_done)} max={ordered} /> },
            { key: "defective", label: "Defects", num: true },
          ]} />
        </section>
        <aside className="plan-side">
          {planner && <PriorityControl d={ready[0]} f={ready[1]} orderId={String(o.production_order_id)} canChange={planner} />}
          <section className="panel section">
            <h2>Quality</h2>
            <p><span className={`figure${dhu != null && dhu > 5 ? " late-text" : ""}`}>{dhu != null ? num(dhu, 1) : "—"}</span> <span className="muted">DHU</span></p>
            <p className="muted small">{inspected ? `${defective.toLocaleString("en-US")} defects found in ${inspected.toLocaleString("en-US")} pieces checked (defects per hundred units).` : "No quality checks recorded yet."}</p>
          </section>
          <section className="panel section">
            <h2>Shipping</h2>
            {delivery && Object.keys(delivery).length ? (
              <dl className="facts">
                <dt>Status</dt><dd><Chip value={delivery.delivery_status} /></dd>
                <dt>Carrier</dt><dd>{delivery.carrier_name ?? "—"}</dd>
                <dt>Tracking</dt><dd>{delivery.tracking_number ?? "—"}</dd>
                <dt>Shipped</dt><dd>{delivery.actual_shipment_date ?? "not yet"}</dd>
                <dt>Expected arrival</dt><dd>{String(delivery.estimated_arrival_date ?? "—").slice(0, 10)}</dd>
              </dl>) : <p className="muted">No shipment record.</p>}
          </section>
          <section className="panel section">
            <h2>Materials for this order</h2>
            <DataTable rows={materials} columns={[
              { key: "name", label: "Material" },
              { key: "required", label: "Needed incl. wastage", num: true, render: (r) => quantity(r.required, r.unit) },
            ]} />
          </section>
        </aside>
      </div>
    </>
  );
}
