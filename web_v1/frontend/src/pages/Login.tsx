import { useState, type FormEvent, type KeyboardEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CalendarClock, CheckCircle2, Eye, EyeOff, Factory, Loader2, Lock, ShieldCheck, User } from "lucide-react";
import { api } from "../lib/api";
import { Logo, ThemeToggle } from "../components/Shell";
import { Notice } from "../components/ui";

/**
 * A picture of a plan for the sign-in page. Nobody is signed in yet, so it shows no factory data:
 * the rows and bars are illustration only.
 */
const PREVIEW = [
  { line: "Line 1", bars: [[2, 26, 0], [30, 22, 1], [56, 30, 2]] },
  { line: "Line 2", bars: [[6, 18, 3], [27, 34, 0], [64, 20, 4]] },
  { line: "Line 3", bars: [[0, 30, 2], [34, 16, 4], [53, 33, 1]] },
  { line: "Line 4", bars: [[10, 24, 1], [38, 28, 3], [70, 18, 0]] },
] as const;

const POINTS = [
  { icon: CalendarClock, title: "Plans that keep promises", text: "Every order, step, line and crew placed against its due date." },
  { icon: CheckCircle2, title: "Approve with confidence", text: "See who is late, by how much, and why, before the floor starts." },
  { icon: Factory, title: "Run the floor from one place", text: "Line boards, problems and quality checks follow the approved plan." },
];

function PlanPreview() {
  return (
    <div className="lp-preview" aria-hidden="true">
      <div className="lp-preview-head">
        <span className="lp-dots"><i /><i /><i /></span>
        <span>Week plan</span>
        <span className="lp-pill"><i />On track</span>
      </div>
      <div className="lp-rows">
        {PREVIEW.map((row, r) => (
          <div key={row.line} className="lp-row">
            <span className="lp-line">{row.line}</span>
            <span className="lp-track">
              {row.bars.map(([left, width, hue], b) => (
                <i key={b} className={`lp-bar h${hue}`} style={{ left: `${left}%`, width: `${width}%`, animationDelay: `${300 + (r * 3 + b) * 90}ms` }} />
              ))}
            </span>
          </div>
        ))}
        <span className="lp-sweep-box"><span className="lp-now-run"><span className="lp-now"><b>Now</b></span></span></span>
      </div>
      <div className="lp-preview-foot">
        <span><b>12</b> orders</span><span><b>4</b> lines</span><span><b>0</b> late</span>
      </div>
    </div>
  );
}

export function Login() {
  const client = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [caps, setCaps] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError(null);
    try {
      await api.login(username, password);
      await client.invalidateQueries({ queryKey: ["session"] });
    } catch (e) {
      setError(e instanceof Error && e.message === "INVALID_CREDENTIALS" ? "That username and password do not match." : (e as Error).message);
    } finally { setBusy(false); }
  }
  const watchCaps = (e: KeyboardEvent<HTMLInputElement>) => setCaps(e.getModifierState?.("CapsLock") ?? false);

  return (
    <div className="login">
      <div className="lp-glow" aria-hidden="true"><i /><i /><i /></div>
      <header className="lp-top">
        <span className="lp-brand"><Logo /><strong>craft</strong></span>
        <ThemeToggle />
      </header>

      <section className="lp-story">
        <span className="lp-kicker">Production planning for garment factories</span>
        <h1>Every order, line and machine, <span className="lp-accent">planned to the hour.</span></h1>
        <ul className="lp-points">
          {POINTS.map(({ icon: Icon, title, text }, i) => (
            <li key={title} style={{ animationDelay: `${120 + i * 90}ms` }}>
              <span className="lp-icon"><Icon size={18} aria-hidden="true" /></span>
              <span><strong>{title}</strong><small>{text}</small></span>
            </li>
          ))}
        </ul>
        <PlanPreview />
      </section>

      <main className="lp-card-wrap">
        <form className="lp-card" onSubmit={submit} aria-labelledby="signin-title">
          <span className="lp-card-logo"><Logo /></span>
          <h2 id="signin-title">Welcome back</h2>
          <p className="muted">Sign in with the account your administrator gave you.</p>

          <div className="field"><label htmlFor="u">Username</label>
            <span className="lp-input"><User size={17} aria-hidden="true" />
              <input id="u" autoComplete="username" autoCapitalize="none" spellCheck={false} value={username}
                onChange={(e) => setUsername(e.target.value)} required autoFocus /></span></div>

          <div className="field"><label htmlFor="p">Password</label>
            <span className="lp-input"><Lock size={17} aria-hidden="true" />
              <input id="p" type={show ? "text" : "password"} autoComplete="current-password" value={password}
                onChange={(e) => setPassword(e.target.value)} onKeyUp={watchCaps} onKeyDown={watchCaps} required />
              <button type="button" className="lp-eye" onClick={() => setShow((s) => !s)} aria-label={show ? "Hide what you typed" : "Show what you typed"} aria-pressed={show}>
                {show ? <EyeOff size={17} aria-hidden="true" /> : <Eye size={17} aria-hidden="true" />}
              </button></span>
            {caps && <span className="lp-caps" role="status">Caps Lock is on</span>}
          </div>

          {error && <Notice tone="error">{error}</Notice>}
          <button className="btn primary lp-submit" type="submit" disabled={busy}>
            {busy ? <><Loader2 size={18} className="spin" aria-hidden="true" />Signing in…</> : <>Sign in<ArrowRight size={18} aria-hidden="true" /></>}
          </button>
          <p className="lp-secure"><ShieldCheck size={15} aria-hidden="true" />Your factory database is only read, never changed.</p>
        </form>
      </main>
    </div>
  );
}
