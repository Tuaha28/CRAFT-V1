import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { MODE_LABEL, setLabel } from "../lib/format";
import { useSession } from "../app/session";
import { Loading, Notice, PageHead } from "../components/ui";

const MODE_TEXT = {
  QUICK: "A good plan, sooner. About 1.5 hours for 10 orders.",
  FULL: "CRAFT's best plan. About 1.5 to 3 hours for 10 orders.",
} as const;

export default function NewPlan() {
  const session = useSession();
  const navigate = useNavigate();
  const client = useQueryClient();
  const datasets = useQuery({ queryKey: ["datasets"], queryFn: api.datasets });
  const [datasetId, setDatasetId] = useState<number | null>(null);
  const [factoryId, setFactoryId] = useState<number | null>(null);
  const [orders, setOrders] = useState(10);
  const [offset, setOffset] = useState(0);
  const [mode, setMode] = useState<"QUICK" | "FULL">("QUICK");

  const dataset = datasets.data?.find((d) => d.id === datasetId) ?? null;
  useEffect(() => {
    if (!datasetId && datasets.data?.length) setDatasetId((datasets.data.find((d) => d.load_pct === 30) ?? datasets.data[0]).id);
  }, [datasets.data, datasetId]);
  useEffect(() => {
    if (dataset && !dataset.factories.some((f) => f.factory_id === factoryId)) {
      setFactoryId((dataset.factories.find((f) => f.factory_id === 4) ?? dataset.factories[0])?.factory_id ?? null);
    }
  }, [dataset, factoryId]);

  const book = useQuery({
    queryKey: ["orderbook", datasetId, factoryId], queryFn: () => api.orderBook(datasetId!, factoryId!),
    enabled: Boolean(datasetId && factoryId), staleTime: Infinity,
  });
  const chosen = useMemo(() => book.data?.eligible.slice(offset, offset + orders) ?? [], [book.data, offset, orders]);
  const eligible = book.data?.eligible.length ?? 0;

  const submit = useMutation({
    meta: { success: "Plan requested. You can follow its progress here." },
    mutationFn: () => api.requestPlan({ dataset_id: datasetId!, factory_id: factoryId!, orders, order_offset: offset, mode }),
    onSuccess: async (run) => { await client.invalidateQueries({ queryKey: ["runs"] }); navigate(`/runs/${run.id}`); },
  });
  const onSubmit = (e: FormEvent) => { e.preventDefault(); submit.mutate(); };

  if (datasets.isPending) return <Loading />;
  if (datasets.isError) return <Notice tone="error">{datasets.error.message}</Notice>;
  if (!datasets.data.length) return <Notice>No factory captures were found. Capture the database from Factory data first.</Notice>;

  const totalHours = chosen.reduce((t, o) => t + (o.work_hours || 0), 0);
  const biggest = chosen.reduce<(typeof chosen)[number] | null>((b, o) => (!b || o.work_hours > b.work_hours ? o : b), null);
  const most = Math.max(1, ...chosen.map((o) => o.work_hours || 0));
  const hours = (h: number) => h.toLocaleString("en-US", { maximumFractionDigits: 1 });
  const steps: { title: string; sub: string; done: boolean }[] = [
    { title: "Factory data", sub: dataset ? `${dataset.label}, ${dataset.reference_time.slice(0, 10)}` : "Choose the data", done: Boolean(dataset && factoryId) },
    { title: "Orders", sub: chosen.length ? `${chosen.length} orders chosen` : "Choose the orders", done: chosen.length > 0 },
    { title: "How thorough", sub: MODE_LABEL[mode], done: true },
    { title: "Run and approve", sub: "You review the result", done: false },
  ];
  const current = steps.findIndex((st) => !st.done);

  return (
    <>
      <PageHead title="Plan the next orders" lead="CRAFT plans them across your lines, crews and machines. You approve the result before the floor follows it." />
      {!session.can_request && <Notice>Your account can view plans but not start them. Ask an administrator for the planning permission.</Notice>}
      <ol className="wizard-steps" aria-label="Steps">
        {steps.map((st, i) => (
          <li key={st.title} className={st.done ? "done" : i === current ? "now" : ""} aria-current={i === current ? "step" : undefined}>
            <span className="wizard-dot" aria-hidden="true">{st.done ? "✓" : i + 1}</span>
            <span><strong>{st.title}</strong><small>{st.sub}</small></span>
          </li>))}
      </ol>
      <form className="plan-layout" onSubmit={onSubmit}>
        <section className="panel section" aria-labelledby="orders-title">
          <h2 id="orders-title">Which orders?</h2>
          <p className="muted small">Orders are taken in release order{book.data ? `: ${eligible} can be planned (${book.data.orders_in_capture} in the factory data)` : ""}. Set 1 starts at the first order, set 2 at the 11th, set 3 at the 21st.</p>
          <div className="toolbar">
            <div className="field"><label htmlFor="n">Number of orders</label>
              <input id="n" type="number" min={1} max={30} value={orders} onChange={(e) => setOrders(Math.max(1, Math.min(30, Number(e.target.value) || 1)))} /></div>
            <div className="field"><label htmlFor="off">Start at order</label>
              <input id="off" type="number" min={1} max={Math.max(1, eligible)} value={offset + 1}
                onChange={(e) => setOffset(Math.max(0, (Number(e.target.value) || 1) - 1))} /></div>
            <div className="tabs" role="group" aria-label="Quick sets">
              {[0, 10, 20].map((o, i) => (
                <button key={o} type="button" className={offset === o && orders === 10 ? "on" : ""} onClick={() => { setOffset(o); setOrders(10); }}>Set {i + 1}</button>
              ))}
            </div>
          </div>
          {book.isPending ? <Loading label="Reading the order book (first time for this factory can take a minute)" /> :
            book.isError ? <Notice tone="error">{book.error.message}</Notice> : (
              <div className="table-wrap"><table>
                <thead><tr><th>Order</th><th>Released</th><th>Due</th><th className="num">Pieces</th><th>Work needed</th></tr></thead>
                <tbody>{chosen.map((o) => (
                  <tr key={o.order_id}><td className="nowrap"><strong>Order {o.order_id}</strong></td><td className="date">{o.release}</td><td className="date">{o.due}</td>
                    <td className="num">{o.pieces != null ? o.pieces.toLocaleString("en-US") : "—"}</td>
                    <td><span className="work-cell"><span className="work-bar"><i style={{ width: `${((o.work_hours || 0) / most) * 100}%` }} /></span><span className="nowrap">{hours(o.work_hours || 0)} h</span></span></td></tr>))}
                </tbody></table></div>
            )}
        </section>

        <aside className="plan-side">
          <section className="panel section">
            <h2>Factory data</h2>
            <div className="section" role="radiogroup" aria-label="Factory capture">
              {datasets.data.map((d) => (
                <label key={d.id} className="choice">
                  <input type="radio" name="dataset" checked={d.id === datasetId} onChange={() => setDatasetId(d.id)} />
                  <strong>{d.label}</strong>
                  <span className="muted small">{d.origin === "LIVE_CAPTURE" ? "Captured from the database" : "Snapshot"}, as of {d.reference_time.slice(0, 10)}</span>
                </label>
              ))}
            </div>
            <div className="field"><label htmlFor="factory">Factory</label>
              <select id="factory" value={factoryId ?? ""} onChange={(e) => { setFactoryId(Number(e.target.value)); setOffset(0); }}>
                {dataset?.factories.map((f) => (
                  <option key={f.factory_id} value={f.factory_id}>Factory {f.factory_id}: {f.lines} lines, {f.machines} machines, {f.workers} workers</option>
                ))}
              </select>
            </div>
          </section>
          <section className="panel section">
            <h2>How thorough?</h2>
            <div className="section" role="radiogroup" aria-label="Run mode">
              {(["QUICK", "FULL"] as const).map((m) => (
                <label key={m} className="choice">
                  <input type="radio" name="mode" checked={mode === m} onChange={() => setMode(m)} />
                  <strong>{MODE_LABEL[m]}</strong>
                  <span className="muted small">{MODE_TEXT[m]}</span>
                </label>
              ))}
            </div>
          </section>
          <section className="panel section hero ready" aria-labelledby="ready-title">
            <p className="eyebrow" id="ready-title">Ready to run</p>
            <p className="ready-big">{chosen.length} orders · {hours(totalHours)} h of work</p>
            {biggest && chosen.length > 1 && <p className="muted small">Order {biggest.order_id} alone is {hours(biggest.work_hours)} h. </p>}
            <p className="muted small">{setLabel(offset, chosen.length)}. Plans run one at a time in the background; you can leave this page.</p>
            {submit.isError && <Notice tone="error">{(submit.error as Error).message}</Notice>}
            <button className="btn primary" type="submit" disabled={!session.can_request || !chosen.length || submit.isPending}>
              {submit.isPending ? "Starting…" : `Run CRAFT on ${chosen.length} orders`}
            </button>
          </section>
        </aside>
      </form>
    </>
  );
}
