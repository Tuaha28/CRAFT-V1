import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { floor, type AppUser, type RoleInfo } from "../lib/api";
import { when } from "../lib/format";
import { useReadyScope } from "../app/scope";
import { useSession } from "../app/session";
import { lineName, useFloorLines } from "../components/Floor";
import { Loading, Notice, PageHead } from "../components/ui";

/** Shown once: the administrator passes it on, the server keeps only a hash. */
function OneTimePassword({ username, password, onClose }: { username: string; password: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="answer" role="status">
      <strong>Password for {username}</strong>
      <code className="secret">{password}</code>
      <span className="small">Give it to {username} now. It is shown only once; if it is lost, issue a new one.</span>
      <div style={{ display: "flex", gap: 8 }}>
        <button type="button" className="btn" onClick={() => { navigator.clipboard?.writeText(password).then(() => setCopied(true)).catch(() => undefined); }}>
          {copied ? "Copied" : "Copy"}</button>
        <button type="button" className="btn" onClick={onClose}>Done</button>
      </div>
    </div>
  );
}

function RoleFields({ roles, role, setRole, factory, setFactory, line, setLine, factoryId, lines, id }: {
  roles: RoleInfo[]; role: string; setRole: (r: string) => void; factory: boolean; setFactory: (b: boolean) => void;
  line: number | null; setLine: (l: number | null) => void; factoryId: number; lines: { line_id: number; line_number: string }[]; id: string;
}) {
  const needsLine = roles.find((r) => r.key === role)?.line_required ?? false;
  return (
    <>
      <div className="field"><label htmlFor={`${id}-role`}>Role</label>
        <select id={`${id}-role`} value={role} onChange={(e) => { setRole(e.target.value); if (roles.find((r) => r.key === e.target.value)?.line_required) setFactory(true); }}>
          {roles.map((r) => <option key={r.key} value={r.key}>{r.label}</option>)}</select></div>
      <div className="field"><label htmlFor={`${id}-factory`}>Works for</label>
        <select id={`${id}-factory`} value={factory ? "one" : "all"} disabled={needsLine} onChange={(e) => { setFactory(e.target.value === "one"); if (e.target.value === "all") setLine(null); }}>
          <option value="all">All factories</option><option value="one">Factory {factoryId}</option></select></div>
      <div className="field"><label htmlFor={`${id}-line`}>Line</label>
        <select id={`${id}-line`} value={line ?? ""} disabled={!factory} required={needsLine} onChange={(e) => setLine(e.target.value ? Number(e.target.value) : null)}>
          <option value="">{needsLine ? "Choose a line" : "All lines"}</option>
          {lines.map((l) => <option key={l.line_id} value={l.line_id}>{lineName(lines, l.line_id)}</option>)}</select>
        {needsLine && <span className="hint">A line supervisor sees and enters data for one line only.</span>}</div>
    </>
  );
}

function EditUser({ user, roles, factoryId, datasetId, lines, self, onClose }: {
  user: AppUser; roles: RoleInfo[]; factoryId: number; datasetId: number; lines: { line_id: number; line_number: string }[]; self: boolean; onClose: () => void;
}) {
  const client = useQueryClient();
  const [role, setRole] = useState(user.role ?? "planner");
  const [factory, setFactory] = useState(user.factory_id !== null);
  const [line, setLine] = useState<number | null>(user.line_id);
  const [active, setActive] = useState(user.active);
  const [email, setEmail] = useState(user.email ?? "");
  const save = useMutation({
    meta: { success: "User updated." },
    mutationFn: () => floor.changeUser(user.id, { role, factory_id: factory ? factoryId : null, line_id: factory ? line : null, dataset_id: datasetId, active, email }),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ["floor-users"] }); onClose(); },
  });
  return (
    <form className="form-grid" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <RoleFields id={`u${user.id}`} roles={roles} role={role} setRole={setRole} factory={factory} setFactory={setFactory} line={line} setLine={setLine} factoryId={factoryId} lines={lines} />
      <EmailField id={`u${user.id}`} value={email} onChange={setEmail} />
      <label className="check"><input type="checkbox" checked={active} disabled={self} onChange={(e) => setActive(e.target.checked)} /> Can sign in</label>
      <div className="form-actions"><button className="btn primary" disabled={save.isPending}>Save</button><button type="button" className="btn" onClick={onClose}>Cancel</button></div>
      {save.isError && <Notice tone="error">{save.error.message}</Notice>}
    </form>
  );
}

/** Email address, used only for CRAFT's plan emails to managers. */
function EmailField({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="field"><label htmlFor={`${id}-email`}>Email</label>
      <input id={`${id}-email`} type="email" value={value} onChange={(e) => onChange(e.target.value.trim())} autoComplete="off" placeholder="name@factory.com" />
      <span className="hint">Managers get an email when CRAFT has a new plan for them to review.</span></div>
  );
}

export default function Users() {
  const session = useSession();
  const ready = useReadyScope();
  const client = useQueryClient();
  const users = useQuery({ queryKey: ["floor-users"], queryFn: floor.users });
  const lines = useFloorLines(ready?.[0], ready?.[1]);
  const [username, setUsername] = useState("");
  const [role, setRole] = useState("supervisor");
  const [factory, setFactory] = useState(true);
  const [line, setLine] = useState<number | null>(null);
  const [email, setEmail] = useState("");
  const [shown, setShown] = useState<{ username: string; password: string } | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);
  const create = useMutation({
    meta: { success: "User created." },
    mutationFn: () => floor.createUser({ username, role, factory_id: factory ? ready![1] : null, line_id: factory ? line : null, dataset_id: ready![0], email }),
    onSuccess: async (u) => { setShown({ username: u.username, password: u.password }); setUsername(""); setLine(null); setEmail(""); setAdding(false); await client.invalidateQueries({ queryKey: ["floor-users"] }); },
  });
  const reset = useMutation({
    meta: { success: "Password reset." },
    mutationFn: (id: number) => floor.resetPassword(id),
    onSuccess: (r) => setShown(r),
  });
  if (!ready || users.isPending || lines.isPending) return <Loading label="Loading users" />;
  if (users.isError) return <Notice tone="error">{users.error.message}</Notice>;
  const roles = users.data.roles;
  const factoryLines = lines.data?.lines ?? [];
  return (
    <>
      <PageHead title="Users and roles" lead="Who can sign in and what each person may do.">
        <button type="button" className="btn primary" onClick={() => setAdding(!adding)}>Add a user</button>
      </PageHead>
      {shown && <OneTimePassword {...shown} onClose={() => setShown(null)} />}
      {reset.isError && <Notice tone="error">{reset.error.message}</Notice>}
      {adding && <section className="panel section"><h2>Add a user</h2>
        <form className="form-grid" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
          <div className="field"><label htmlFor="nu-name">User name</label>
            <input id="nu-name" value={username} onChange={(e) => setUsername(e.target.value.trim())} pattern="[A-Za-z0-9_.\-]{3,40}" required autoComplete="off" />
            <span className="hint">3 to 40 letters, digits, dots, dashes or underscores.</span></div>
          <EmailField id="nu" value={email} onChange={setEmail} />
          <RoleFields id="nu" roles={roles} role={role} setRole={setRole} factory={factory} setFactory={setFactory} line={line} setLine={setLine} factoryId={ready[1]} lines={factoryLines} />
          <div className="form-actions"><button className="btn primary" disabled={create.isPending}>{create.isPending ? "Adding…" : "Add user"}</button></div>
          {create.isError && <Notice tone="error">{create.error.message}</Notice>}
        </form>
      </section>}
      <section className="panel">
        <div className="table-wrap"><table>
          <thead><tr><th>User</th><th>Role</th><th>Works for</th><th>Last sign-in</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{users.data.users.map((u) => (
            editing === u.id ? (
              <tr key={u.id}><td colSpan={6}><strong>{u.username}</strong>
                <EditUser user={u} roles={roles} factoryId={ready[1]} datasetId={ready[0]} lines={factoryLines} self={u.username === session.username} onClose={() => setEditing(null)} /></td></tr>
            ) : (
              <tr key={u.id}>
                <td><strong>{u.username}</strong>{u.username === session.username && <span className="muted small"> (you)</span>}
                  {u.email ? <><br /><span className="muted small">{u.email}</span></>
                    : (u.role === "manager" || u.role === "admin") && <><br /><span className="warn-text small">No email address: this person won't get CRAFT's plan emails</span></>}</td>
                <td>{u.role_label ?? <span className="muted">No role</span>}</td>
                <td>{u.factory_id ? `Factory ${u.factory_id}` : "All factories"}{u.line_id ? `, ${lineName(factoryLines, u.line_id)}` : ""}</td>
                <td className="small">{u.last_login ? when(u.last_login) : "Never"}</td>
                <td><span className={`chip ${u.active ? "good" : ""}`}>{u.active ? "Active" : "Deactivated"}</span></td>
                <td className="row-actions">
                  <button type="button" className="btn" onClick={() => setEditing(u.id)}>Change</button>
                  <button type="button" className="btn" disabled={reset.isPending} onClick={() => reset.mutate(u.id)}>New password</button>
                </td>
              </tr>)))}</tbody>
        </table></div>
      </section>
    </>
  );
}
