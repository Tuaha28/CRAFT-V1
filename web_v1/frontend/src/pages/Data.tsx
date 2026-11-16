import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useSession } from "../app/session";
import { Loading, Notice, PageHead } from "../components/ui";

export default function Data() {
  const session = useSession();
  const navigate = useNavigate();
  const datasets = useQuery({ queryKey: ["datasets"], queryFn: api.datasets });
  const [pick, setPick] = useState<{ dataset: number; factory: number } | null>(null);
  const book = useQuery({ queryKey: ["orderbook", pick?.dataset, pick?.factory], queryFn: () => api.orderBook(pick!.dataset, pick!.factory),
    enabled: Boolean(pick), staleTime: Infinity });
  const capture = useMutation({ meta: { success: "Database capture started." }, mutationFn: api.requestCapture, onSuccess: (run) => navigate(`/runs/${run.id}`) });

  if (datasets.isPending) return <Loading />;
  if (datasets.isError) return <Notice tone="error">{datasets.error.message}</Notice>;
  return (
    <>
      <PageHead title="Factory data"
        lead="Saved copies of the factory database. CRAFT plans from a saved copy, so every plan can be repeated.">
        {session.can_request && <button className="btn" onClick={() => capture.mutate()} disabled={capture.isPending}>Capture the database now</button>}
      </PageHead>
      {session.writeback && session.perms.includes("floor.manage_users") && (
        <section className="panel section">
          <h2>Reset demo data</h2>
          <p className="muted small">Approved plans and machine changes are written into demo copies of the factory data; the originals are never changed. To throw the changes away and start again, an administrator runs this on the server (it asks to type RESET first):</p>
          <pre className="log">python manage.py reset_demo_data --load 30 --run</pre>
        </section>)}
      {capture.isError && <Notice tone="error">{(capture.error as Error).message}</Notice>}
      <p className="muted small">A capture reads the database without changing it, checks it and updates the matching data below with every factory. Plans already made keep the data they were made from. It can take half an hour or more.</p>

      {datasets.data.map((d) => (
        <section key={d.id} className="panel section">
          <div className="section-head"><h2>{d.label}</h2>
            <span className="muted small">{d.captured_at ? `Data from ${d.captured_at.slice(6, 8)}/${d.captured_at.slice(4, 6)}/${d.captured_at.slice(0, 4)}` : "Pinned snapshot"}, reference {d.reference_time.slice(0, 10)}</span></div>
          <div className="table-wrap"><table>
            <thead><tr><th>Factory</th><th className="num">Orders</th><th className="num">Lines</th><th className="num">Machines</th><th className="num">Workers</th><th /></tr></thead>
            <tbody>{d.factories.map((f) => (
              <tr key={f.factory_id}><td>Factory {f.factory_id}</td><td className="num">{f.orders}</td>
                <td className="num">{f.lines}</td><td className="num">{f.machines}</td><td className="num">{f.workers}</td>
                <td><button className="btn" style={{ padding: "3px 10px" }} onClick={() => setPick({ dataset: d.id, factory: f.factory_id })}>Show order book</button></td></tr>))}
            </tbody></table></div>
          {pick?.dataset === d.id && (
            book.isPending ? <Loading label="Reading the order book" /> : book.isError ? <Notice tone="error">{book.error.message}</Notice> : (
              <div className="section">
                <h3>Factory {pick.factory}: {book.data.eligible.length} orders that can be planned</h3>
                <p className="muted small">In release order. Orders already in progress on the floor appear with their remaining steps only.</p>
                <div className="table-wrap" style={{ maxHeight: 420, overflow: "auto" }}><table>
                  <thead><tr><th className="num">Position</th><th>Order</th><th>Released</th><th>Due</th><th className="num">Pieces</th><th className="num">Work (h)</th></tr></thead>
                  <tbody>{book.data.eligible.map((o, i) => (
                    <tr key={o.order_id} style={i % 10 === 0 && i ? { borderTop: "2px solid var(--accent-2)" } : undefined}>
                      <td className="num">{i + 1}</td><td>{o.order_id}</td><td className="date">{o.release}</td><td className="date">{o.due}</td><td className="num">{o.pieces ?? "—"}</td>
                      <td className="num">{o.work_hours}</td></tr>))}
                  </tbody></table></div>
              </div>)
          )}
        </section>
      ))}
    </>
  );
}
