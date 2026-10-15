import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { governance } from "../services/governance";
import { Badge, EmptyBlock, ErrorBlock, LoadingBlock, PageTitle, Panel } from "../components/Primitives";
import { CandidateComparison } from "./CandidateComparison";
import { RunProgress } from "./RunProgress";

export default function GovernancePage() {
  const client = useQueryClient();
  const session = useQuery({ queryKey: ["governance", "session"], queryFn: governance.session, retry: false });
  const authenticated = session.data?.authenticated === true;
  const scopes = useQuery({ queryKey: ["governance", "scopes"], queryFn: governance.scopes, enabled: authenticated });
  const runs = useQuery({ queryKey: ["governance", "runs"], queryFn: governance.runs, enabled: authenticated, refetchInterval: 3000 });
  const candidates = useQuery({ queryKey: ["governance", "candidates"], queryFn: governance.candidates, enabled: authenticated, refetchInterval: 3000 });
  const events = useQuery({ queryKey: ["governance", "events"], queryFn: governance.events, enabled: authenticated, refetchInterval: 3000 });
  const [username, setUsername] = useState(""); const [password, setPassword] = useState("");
  const [scopeId, setScopeId] = useState(""); const [reason, setReason] = useState("");
  const [priorityOrder, setPriorityOrder] = useState("B"); const [priorityTier, setPriorityTier] = useState("URGENT");
  const [targetDue, setTargetDue] = useState(900); const [priorityReason, setPriorityReason] = useState("");
  const [requestKey, setRequestKey] = useState(() => crypto.randomUUID());
  const selectedScope = scopes.data?.results.find(scope => scope.id === Number(scopeId));
  const visibleRuns = selectedScope ? runs.data?.results.filter(run => run.scope === selectedScope.id) : [];
  const visibleCandidates = selectedScope ? candidates.data?.results.filter(candidate => candidate.scope === selectedScope.id) : [];
  const visibleEvents = selectedScope ? events.data?.results.filter(event => event.scope === selectedScope.id) : [];
  const refresh = () => client.invalidateQueries({ queryKey: ["governance"] });
  const login = useMutation({ mutationFn: () => governance.login(username, password), onSuccess: () => { setPassword(""); void refresh(); } });
  const start = useMutation({ mutationFn: () => {
    if (!selectedScope) throw new Error("Select an available TEST scope.");
    return governance.start(selectedScope, requestKey);
  }, onSuccess: () => { setRequestKey(crypto.randomUUID()); void refresh(); } });
  const changePriority = useMutation({ mutationFn: () => {
    if (!selectedScope || selectedScope.environment !== "TEST_PRIORITY") throw new Error("Select the priority TEST scope.");
    return governance.changePriority(selectedScope, priorityOrder, priorityTier, targetDue, priorityReason);
  }, onSuccess: () => { setPriorityReason(""); void refresh(); } });
  const promote = useMutation({ mutationFn: (id: string) => governance.promote(id, reason), onSuccess: () => { setReason(""); void refresh(); } });
  const cancel = useMutation({ mutationFn: (id: string) => governance.cancel(id, reason), onSuccess: () => void refresh() });
  if (session.isLoading) return <LoadingBlock label="Checking isolated integration service"/>;
  return <><PageTitle eyebrow="CONTROLLED INTEGRATION" title="Scheduling governance" description="Actual background CRAFT execution on an isolated fixture or a labeled Factory-1 simulation. No production activation or live-factory admission." actions={<Link className="button button-quiet" to="/craft">Back to demo control</Link>}/>
    <div className="craft-demo-notice"><Badge tone="warning">ISOLATED TEST</Badge><span>Manager approval changes only the application test schedule. All 278 unresolved partial operations across six factories, including 82 in Factory 1, remain blocked from live scheduling.</span></div>
    {session.isError && <ErrorBlock message="Integration service unavailable. Start the opt-in backend on port 8001; existing demo data remains separate." retry={() => void session.refetch()}/>}
    {[login.error, start.error, changePriority.error, promote.error, cancel.error, scopes.error, runs.error, candidates.error, events.error].filter(Boolean).map((error, i) => <ErrorBlock key={i} message={String(error)}/>)}
    {!authenticated && !session.isError && <Panel title="Authorized access"><form className="panel-body" onSubmit={e => { e.preventDefault(); login.mutate(); }}><label>Username <input autoComplete="username" value={username} onChange={e => setUsername(e.target.value)}/></label><label>Password <input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)}/></label><button className="button" disabled={login.isPending || !username || !password}>Sign in</button></form></Panel>}
    {authenticated && <><Panel title="Isolated scheduling scope" subtitle={`Signed in as ${session.data?.username}. Existing active version remains unchanged during solving.`}><div className="panel-body"><label>Factory test scope <select value={scopeId} onChange={e => setScopeId(e.target.value)}><option value="">Select scope</option>{scopes.data?.results.map(s => <option key={s.id} value={s.id}>Factory {s.factory_external_id} · {s.environment}</option>)}</select></label> <button className="button button-violet" disabled={!scopeId || !session.data?.can_request || start.isPending} onClick={() => start.mutate()}>Queue isolated CRAFT proof</button></div></Panel>
    {selectedScope?.environment === "TEST_PRIORITY" && <Panel title="Controlled order priority" subtitle="Two synthetic orders share one machine and skilled worker. The request changes CRAFT's delivery objective, never a physical constraint."><div className="panel-body">
      <div className="craft-demo-notice"><Badge tone="warning">SYNTHETIC TEST</Badge><span>Order A: residual plus final route step, customer target +1,050 s. Order B: one residual step, customer target +3,000 s. Both have 40 completed and 60 unstarted units. A fixed 0–300 s resource hold remains protected.</span></div>
      <p>Current manager priority: <strong>{selectedScope.priority_state.tier ?? "NORMAL"}</strong>{selectedScope.priority_state.selected_order_id ? ` for order ${selectedScope.priority_state.selected_order_id}` : ""}. Revision {selectedScope.priority_state.revision ?? 0}.</p>
      <label>Order <select aria-label="Priority order" value={priorityOrder} onChange={event => setPriorityOrder(event.target.value)}><option value="A">Order A</option><option value="B">Order B</option></select></label>
      <label>Priority <select aria-label="Priority tier" value={priorityTier} onChange={event => setPriorityTier(event.target.value)}><option value="NORMAL">NORMAL</option><option value="HIGH">HIGH</option><option value="URGENT">URGENT</option></select></label>
      <label>Manager target, seconds after TEST reference <input aria-label="Priority target seconds" type="number" min="1" max="3600" value={targetDue} onChange={event => setTargetDue(Number(event.target.value))}/></label>
      <label>Priority reason <input aria-label="Priority reason" value={priorityReason} onChange={event => setPriorityReason(event.target.value)} placeholder="Why this order needs priority"/></label>
      <button className="button button-violet" disabled={!session.data?.can_change_priority || !priorityReason.trim() || changePriority.isPending || targetDue < 1 || targetDue > 3600} onClick={() => changePriority.mutate()}>Record manager priority</button>
      <small>Recording a new priority invalidates earlier unapproved candidates. Queue a new CRAFT run above, then review its measured impact before TEST approval.</small>
    </div></Panel>}
    <Panel title="Background run register" subtitle="CP status and controller state are separate. Refreshes every three seconds.">{visibleRuns?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>Run</th><th>Controller</th><th>CP-SAT</th><th>Progress</th><th>Input fingerprint</th><th>Action</th></tr></thead><tbody>{visibleRuns.map(r => <tr key={r.id}><td className="mono">{r.id.slice(0, 8)}</td><td><Badge>{r.status}</Badge></td><td>{r.cp_status}</td><td><RunProgress run={r}/></td><td className="mono">{r.input_hash.slice(0, 16)}</td><td>{["QUEUED", "PREPARING", "CHECKPOINTED"].includes(r.status) && <button className="button button-sm" disabled={!reason.trim() || !session.data?.can_request || cancel.isPending} onClick={() => cancel.mutate(r.id)}>Cancel admission</button>}</td></tr>)}</tbody></table></div> : <EmptyBlock title="No runs in selected scope" description="Select a scope and queue a controlled proof; the separate worker must be running."/>}</Panel>
    <Panel title="Manager review" subtitle="Replay, physics, scope and quantity checks must pass before a candidate appears."><div className="panel-body"><label>Approval / cancellation reason <input value={reason} onChange={e => setReason(e.target.value)} placeholder="Required audit reason"/></label></div>{visibleCandidates?.map(c => <div className="detail-section" key={c.id}><h3>Candidate {c.id.slice(0, 8)} <Badge>{c.status}</Badge></h3><table className="data-table"><thead><tr><th>Operation</th><th>Start / end (seconds from fixture reference)</th><th>Machines</th><th>Crew</th></tr></thead><tbody>{c.operations.map(o => <tr key={o.operation_key}><td>{o.operation_key}</td><td>{o.start_seconds} → {o.end_seconds}</td><td>{o.resource_configuration.machine_ids.join(", ")}</td><td>{o.resource_configuration.worker_ids.join(", ")}</td></tr>)}</tbody></table><button className="button" disabled={c.status !== "VALIDATED_CANDIDATE" || !session.data?.can_promote || !reason.trim() || promote.isPending} onClick={() => promote.mutate(c.id)}>Approve and promote TEST schedule</button></div>)}</Panel></>}
    {authenticated && visibleCandidates?.map(candidate => <CandidateComparison key={candidate.id} candidate={candidate} canReview={session.data?.can_promote === true}/>)}
    {authenticated && <Panel title="Controlled decision history" subtitle="Application-owned TEST audit events; illustrative dashboard history is separate.">{visibleEvents?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>Action</th><th>Entity</th><th>Actor</th><th>Reason</th></tr></thead><tbody>{visibleEvents.slice(0, 20).map(event => <tr key={event.id}><td>{event.action}</td><td>{event.entity_id}</td><td>{event.actor ?? "worker"}</td><td>{event.reason}</td></tr>)}</tbody></table></div> : <EmptyBlock title="No controlled decisions" description="Priority changes, runs and manager decisions in this scope will appear here."/>}</Panel>}
  </>;
}
