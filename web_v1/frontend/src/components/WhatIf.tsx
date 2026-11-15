import { useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, erp, type RunDetail } from "../lib/api";
import { MODE_LABEL } from "../lib/format";
import { useSession } from "../app/session";
import { Notice } from "./ui";

type Tab = "disruption" | "promise";

/** What-if questions on a finished plan. Both answers are new CRAFT runs on a
 *  scenario copy of the factory data; the original plan and data never change. */
export function WhatIf({ run }: { run: RunDetail }) {
  const session = useSession();
  const [tab, setTab] = useState<Tab>("disruption");
  if (!session.can_request || !run.digest) return null;
  return (
    <section className="panel section">
      <div className="section-head">
        <h2>What if…</h2>
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === "disruption"} className={tab === "disruption" ? "on" : ""} onClick={() => setTab("disruption")}>A machine or worker is unavailable</button>
          <button role="tab" aria-selected={tab === "promise"} className={tab === "promise" ? "on" : ""} onClick={() => setTab("promise")}>Can we accept a new order?</button>
        </div>
      </div>
      {tab === "disruption" ? <Disruption run={run} /> : <PromiseForm run={run} />}
    </section>
  );
}

function ModePicker({ mode, setMode }: { mode: "QUICK" | "FULL"; setMode: (m: "QUICK" | "FULL") => void }) {
  return (
    <div className="field"><label htmlFor="wi-mode">Run</label>
      <select id="wi-mode" value={mode} onChange={(e) => setMode(e.target.value as "QUICK" | "FULL")}>
        <option value="QUICK">{MODE_LABEL.QUICK}</option><option value="FULL">{MODE_LABEL.FULL}</option>
      </select></div>
  );
}

function Disruption({ run }: { run: RunDetail }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [machines, setMachines] = useState<Set<string>>(new Set());
  const [workers, setWorkers] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<"QUICK" | "FULL">("QUICK");
  const used = useMemo(() => {
    const m = new Map<string, number>(), w = new Map<string, number>();
    for (const row of run.digest?.gantt.CRAFT_V3 ?? []) {
      row.machines.forEach((x) => m.set(x, (m.get(x) ?? 0) + 1));
      row.workers.forEach((x) => w.set(x, (w.get(x) ?? 0) + 1));
    }
    const sort = (map: Map<string, number>) => [...map.entries()].sort((a, b) => b[1] - a[1]);
    return { machines: sort(m), workers: sort(w) };
  }, [run.digest]);
  const submit = useMutation({
    meta: { success: "What-if plan requested." },
    mutationFn: () => api.disruption(run.id, { machines_down: [...machines].map(Number), workers_absent: [...workers].map(Number), mode }),
    onSuccess: async (r) => { await client.invalidateQueries({ queryKey: ["runs"] }); navigate(`/runs/${r.id}`); },
  });
  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, id: string) => {
    const next = new Set(set); if (next.has(id)) next.delete(id); else next.add(id); setter(next);
  };
  return (
    <form className="section" onSubmit={(e: FormEvent) => { e.preventDefault(); submit.mutate(); }}>
      <p className="muted small">Mark what is unavailable. CRAFT re-plans the same orders without it, so you can compare the two plans. The machine or worker is treated as unavailable for the whole plan.</p>
      <div className="grid-2">
        <fieldset className="pick"><legend>Machines in this plan <span className="muted small">(operations)</span></legend>
          {used.machines.map(([id, n]) => (
            <label key={id} className={machines.has(id) ? "on" : ""}><input type="checkbox" checked={machines.has(id)} onChange={() => toggle(machines, setMachines, id)} />
              Machine {id} <span className="muted">({n})</span></label>))}
        </fieldset>
        <fieldset className="pick"><legend>Workers in this plan <span className="muted small">(operations)</span></legend>
          {used.workers.map(([id, n]) => (
            <label key={id} className={workers.has(id) ? "on" : ""}><input type="checkbox" checked={workers.has(id)} onChange={() => toggle(workers, setWorkers, id)} />
              Worker {id} <span className="muted">({n})</span></label>))}
        </fieldset>
      </div>
      <div className="toolbar">
        <ModePicker mode={mode} setMode={setMode} />
        <button className="btn primary" type="submit" disabled={submit.isPending || (!machines.size && !workers.size)}>
          {submit.isPending ? "Starting…" : `Re-plan without ${machines.size + workers.size || "them"}`}
        </button>
      </div>
      {submit.isError && <Notice tone="error">{(submit.error as Error).message}</Notice>}
    </form>
  );
}

function PromiseForm({ run }: { run: RunDetail }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const reference = run.digest!.reference_time.slice(0, 10);
  const [q, setQ] = useState("");
  const [productId, setProductId] = useState<number | null>(null);
  const [quantity, setQuantity] = useState(300);
  const [release, setRelease] = useState(reference);
  const [due, setDue] = useState(() => new Date(new Date(reference).getTime() + 21 * 86_400_000).toISOString().slice(0, 10));
  const [mode, setMode] = useState<"QUICK" | "FULL">("QUICK");
  const styles = useQuery({ queryKey: ["erp-styles", run.dataset?.id, run.factory_id, "promise", q],
    queryFn: () => erp.styles(run.dataset!.id, run.factory_id!, { q, size: 30 }), enabled: Boolean(run.dataset) });
  const submit = useMutation({
    meta: { success: "Delivery check requested." },
    mutationFn: () => api.promise(run.id, { product_id: productId!, quantity, release, due, mode }),
    onSuccess: async (r) => { await client.invalidateQueries({ queryKey: ["runs"] }); navigate(`/runs/${r.id}`); },
  });
  return (
    <form className="section" onSubmit={(e: FormEvent) => { e.preventDefault(); submit.mutate(); }}>
      <p className="muted small">CRAFT plans the new order together with this plan's {run.digest!.orders} orders and tells you when it can be delivered, and what it does to the others.</p>
      <div className="grid-2">
        <div className="field"><label htmlFor="pr-q">Style</label>
          <input id="pr-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search style code or name" />
          <select aria-label="Choose a style" size={5} value={productId ?? ""} onChange={(e) => setProductId(Number(e.target.value))}>
            {(styles.data?.items ?? []).map((s) => <option key={String(s.product_id)} value={String(s.product_id)}>{s.style_code}: {s.category}, {s.steps} steps, {Number(s.sam).toFixed(1)} min</option>)}
          </select></div>
        <div className="section">
          <div className="field"><label htmlFor="pr-qty">Pieces</label>
            <input id="pr-qty" type="number" min={1} max={20000} value={quantity} onChange={(e) => setQuantity(Number(e.target.value) || 1)} /></div>
          <div className="grid-2">
            <div className="field"><label htmlFor="pr-rel">Fabric ready (release)</label><input id="pr-rel" type="date" value={release} min={reference} onChange={(e) => setRelease(e.target.value)} /></div>
            <div className="field"><label htmlFor="pr-due">Buyer wants it by</label><input id="pr-due" type="date" value={due} min={release} onChange={(e) => setDue(e.target.value)} /></div>
          </div>
          <ModePicker mode={mode} setMode={setMode} />
        </div>
      </div>
      <div className="toolbar">
        <button className="btn primary" type="submit" disabled={submit.isPending || !productId}>{submit.isPending ? "Starting…" : "Check the delivery date"}</button>
      </div>
      {submit.isError && <Notice tone="error">{(submit.error as Error).message}</Notice>}
    </form>
  );
}
