import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { portal, type PortalRequest, type Row } from "../lib/api";
import { can, useSession } from "../app/session";
import { useScope } from "../app/scope";
import { Chip, DataTable } from "../components/DataTable";
import { Loading, Notice, PageHead } from "../components/ui";

function Decide({ r }: { r: PortalRequest }) {
  const client = useQueryClient();
  const [note, setNote] = useState("");
  const decide = useMutation({
    meta: { success: (v: unknown) => `Request ${(v as { decision: string }).decision === "ACCEPTED" ? "accepted" : "declined"}; the customer sees your answer.` },
    mutationFn: (v: { decision: "ACCEPTED" | "DECLINED" }) => portal.decide(r.id, v.decision, note),
    onSuccess: () => client.invalidateQueries({ queryKey: ["staff-requests"] }),
  });
  if (r.status !== "PENDING") return <><Chip value={r.status === "ACCEPTED" ? "Accepted" : "Declined"} />{r.staff_note && <><br /><span className="small">{r.staff_note}</span></>}<br /><span className="muted small">{r.decided_by}</span></>;
  return (
    <div className="decide-inline">
      <label className="sr-only" htmlFor={`note-${r.id}`}>Answer to the customer</label>
      <input id={`note-${r.id}`} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} placeholder="Answer to the customer" />
      <button type="button" className="btn primary" disabled={!note.trim() || decide.isPending} onClick={() => decide.mutate({ decision: "ACCEPTED" })}>Accept</button>
      <button type="button" className="btn" disabled={!note.trim() || decide.isPending} onClick={() => decide.mutate({ decision: "DECLINED" })}>Decline</button>
      {decide.isError && <Notice tone="error">{decide.error.message}</Notice>}
    </div>
  );
}

function RequestInbox() {
  const requests = useQuery({ queryKey: ["staff-requests"], queryFn: portal.staffRequests });
  if (requests.isPending) return <Loading />;
  if (requests.isError) return <Notice tone="error">{requests.error.message}</Notice>;
  const waiting = requests.data.filter((r) => r.status === "PENDING").length;
  return (
    <section className="panel section">
      <div className="section-head"><h2>Customer requests</h2><span className="muted small">{waiting} waiting for an answer</span></div>
      <DataTable rows={requests.data as unknown as Row[]} empty="No customer requests yet." columns={[
        { key: "request_id", label: "Request", render: (r) => <><strong>{String(r.request_id)}</strong><br /><span className="muted small">{String(r.kind_label)}</span></> },
        { key: "client_name", label: "Customer", render: (r) => <>{String(r.client_name)}<br /><span className="muted small">{String(r.customer)}</span></> },
        { key: "description", label: "Request", render: (r) => <>{String(r.description)}<br /><span className="muted small">{[r.order_id, r.style_code, r.quantity && `${r.quantity} pcs`, r.colour, r.destination].filter(Boolean).join(" · ")}</span></> },
        { key: "status", label: "Answer", render: (r) => <Decide r={r as unknown as PortalRequest} /> },
      ]} />
      <p className="muted small">Accepting records your answer for the customer; it does not change the plan or the factory data. Plan changes still go through a new plan and its approval.</p>
    </section>
  );
}

function Accounts() {
  const client = useQueryClient();
  const { datasets } = useScope();
  const accounts = useQuery({ queryKey: ["customer-accounts"], queryFn: portal.accounts });
  const [datasetId, setDatasetId] = useState<number | null>(datasets[0]?.id ?? null);
  const [q, setQ] = useState("");
  const [clientName, setClientName] = useState("");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [created, setCreated] = useState<{ username: string; password: string } | null>(null);
  const found = useQuery({ queryKey: ["clients", datasetId, q], queryFn: () => portal.clients(datasetId!, q), enabled: Boolean(datasetId && q.trim().length >= 2) });
  const create = useMutation({
    mutationFn: () => portal.createAccount({ username, client_name: clientName, dataset_id: datasetId!, email }),
    onSuccess: (a) => { setCreated({ username: a.username, password: a.password }); setUsername(""); setEmail(""); setClientName(""); setQ("");
      return client.invalidateQueries({ queryKey: ["customer-accounts"] }); },
  });
  return (
    <div className="plan-layout">
      <section className="panel section">
        <h2>Customer accounts</h2>
        {accounts.isPending ? <Loading /> : accounts.isError ? <Notice tone="error">{accounts.error.message}</Notice> : (
          <DataTable rows={accounts.data as unknown as Row[]} empty="No customer accounts yet." columns={[
            { key: "client_name", label: "Customer" }, { key: "username", label: "Sign-in" },
            { key: "data", label: "Factory data" }, { key: "active", label: "Status", render: (r) => <Chip value={r.active ? "Active" : "Disabled"} /> },
          ]} />)}
        <p className="muted small">A customer sees only the orders of their own company, the catalogue, the assistant and their requests.</p>
      </section>
      <aside className="plan-side">
        <section className="panel section">
          <h2>Add a customer</h2>
          {created && <Notice>Give {created.username} this password once: <strong className="mono">{created.password}</strong>. It is not shown again.</Notice>}
          <form onSubmit={(e) => { e.preventDefault(); if (clientName && username && datasetId) create.mutate(); }}>
            <div className="field"><label htmlFor="ca-data">Factory data</label>
              <select id="ca-data" value={datasetId ?? ""} onChange={(e) => { setDatasetId(Number(e.target.value)); setClientName(""); }}>
                {datasets.map((d) => <option key={d.id} value={d.id}>{d.label}</option>)}
              </select></div>
            <div className="field"><label htmlFor="ca-q">Company</label>
              <input id="ca-q" value={q} onChange={(e) => { setQ(e.target.value); setClientName(""); }} placeholder="Type 2+ letters of the company name" /></div>
            {found.data && !clientName && (
              <ul className="pick-list">{found.data.map((c) => (
                <li key={c.client_name}><button type="button" className="link-btn" onClick={() => { setClientName(c.client_name); setQ(c.client_name); }}>{c.client_name} <span className="muted small">({c.orders} order{c.orders === 1 ? "" : "s"})</span></button></li>))}
                {!found.data.length && <li className="muted small">No company with that name.</li>}
              </ul>)}
            <div className="field"><label htmlFor="ca-user">Sign-in name</label>
              <input id="ca-user" value={username} onChange={(e) => setUsername(e.target.value)} pattern="[A-Za-z0-9_.\-]{3,40}" placeholder="e.g. williams_buyer" /></div>
            <div className="field"><label htmlFor="ca-email">Email (optional)</label>
              <input id="ca-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></div>
            <button type="submit" className="btn primary" disabled={!clientName || !username || create.isPending}>Create account</button>
            {create.isError && <Notice tone="error">{create.error.message}</Notice>}
          </form>
        </section>
      </aside>
    </div>
  );
}

export default function Customers() {
  const session = useSession();
  return (
    <>
      <PageHead title="Customers" lead="Requests from the customer portal, and the companies that can sign in to it." />
      {can(session, "portal.handle_request") && <RequestInbox />}
      {can(session, "floor.manage_users") && <Accounts />}
    </>
  );
}
