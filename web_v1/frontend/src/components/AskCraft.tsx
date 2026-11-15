import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowUp, Sparkles, X } from "lucide-react";
import { api, erp, type AskRoute } from "../lib/api";
import { ask, SUGGESTIONS, type Answer, type AskContext, type Source } from "../lib/ask";
import { useReadyScope } from "../app/scope";
import { lineName, useFloorLines } from "./Floor";

/** pending: waiting for the AI reading; route: the AI reading; fellBack: the AI was on but failed, so the built-in reading answered. */
interface Turn { id: number; question: string; pending?: boolean; route?: AskRoute; fellBack?: boolean }

/** One answer: headline, figures, a short explanation, a list, a link. */
function AnswerCard({ answer, failed, onOrder }: { answer: Answer; failed: boolean; onOrder: (id: string) => void }) {
  if (answer.missing) {
    return <div className="ask-answer"><p className="muted">{failed ? "This data could not be read with your account." : answer.title}</p></div>;
  }
  return (
    <div className="ask-answer">
      <strong className="ask-title">{answer.title}</strong>
      {answer.facts && <dl className="ask-facts">{answer.facts.map((f) => (
        <div key={f.label} className={f.tone ?? ""}><dt>{f.label}</dt><dd>{f.value}</dd></div>))}</dl>}
      {answer.text && <p>{answer.text}</p>}
      {answer.items && answer.items.length > 0 && <ul className="ask-items">{answer.items.map((item, i) => (
        <li key={i} className={item.tone ?? ""}>
          {item.order ? <button type="button" onClick={() => onOrder(item.order!)}><span>{item.text}</span>{item.sub && <small>{item.sub}</small>}</button>
            : <><span>{item.text}</span>{item.sub && <small>{item.sub}</small>}</>}
        </li>))}</ul>}
      {answer.link && <Link className="ask-link" to={answer.link.to}>{answer.link.label}</Link>}
    </div>
  );
}

/**
 * Ask CRAFT: a side panel that answers questions about the current plan and the
 * factory database. Answers are worked out in the browser from data the app
 * already reads (see lib/ask.ts); nothing is sent to any outside service.
 */
export function AskCraft({ open, onClose, onOrder }: { open: boolean; onClose: () => void; onOrder: (id: string) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const ready = useReadyScope();
  const on = Boolean(ready) && open;
  const plan = useQuery({ queryKey: ["current-plan", ready], queryFn: () => erp.currentPlan(ready![0], ready![1]), enabled: on });
  const summary = useQuery({ queryKey: ["erp-summary", ready], queryFn: () => erp.summary(ready![0], ready![1]), enabled: on });
  const materials = useQuery({ queryKey: ["erp-materials", ready], queryFn: () => erp.materials(ready![0], ready![1]), enabled: on });
  const quality = useQuery({ queryKey: ["erp-quality", ready], queryFn: () => erp.quality(ready![0], ready![1]), enabled: on });
  const machines = useQuery({ queryKey: ["erp-machines", ready, "all"], queryFn: () => erp.machines(ready![0], ready![1], { size: 200 }), enabled: on });
  const lines = useFloorLines(ready?.[0], ready?.[1]);
  const ai = useQuery({ queryKey: ["ask-status"], queryFn: api.askStatus, enabled: open, staleTime: 60_000 });
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");

  useEffect(() => {
    const d = dialog.current;
    if (!d) return;
    if (open && !d.open) d.showModal?.();
    if (!open && d.open) d.close();
  }, [open]);
  useEffect(() => { list.current?.scrollTo?.({ top: list.current.scrollHeight, behavior: "smooth" }); }, [turns]);

  const ctx: AskContext = useMemo(() => ({
    digest: plan.data?.digest, runId: plan.data?.run?.id, lineLabel: (line: number) => lineName(lines.data?.lines, line),
    summary: summary.data, materials: materials.data, quality: quality.data, machines: machines.data?.items,
  }), [plan.data, lines.data, summary.data, materials.data, quality.data, machines.data]);
  const failed: Record<Source, boolean> = { plan: plan.isError, summary: summary.isError, materials: materials.isError,
    quality: quality.isError, machines: machines.isError };

  function send(question: string) {
    const q = question.trim();
    if (!q) return;
    const id = Date.now() + Math.random();
    const useAi = Boolean(ai.data?.ai);
    setTurns((t) => [...t.slice(-19), { id, question: q, pending: useAi }]);
    setText("");
    if (!useAi) return;
    const settle = (patch: Partial<Turn>) => setTurns((all) => all.map((t) => (t.id === id ? { ...t, pending: false, ...patch } : t)));
    api.askRoute(q).then((r) => settle({ route: r.route }), () => settle({ fellBack: true }));
  }
  const submit = (e: FormEvent) => { e.preventDefault(); send(text); };

  return (
    <dialog ref={dialog} className="drawer ask-drawer" onClose={onClose} onClick={(e) => { if (e.target === dialog.current) onClose(); }}
      aria-labelledby="ask-title">
      <div className="ask-body">
        <div className="section-head">
          <h2 id="ask-title"><Sparkles size={18} aria-hidden="true" /> Ask CRAFT</h2>
          <button type="button" className="btn icon" onClick={onClose} aria-label="Close Ask CRAFT"><X size={18} /></button>
        </div>
        <div className="ask-log" ref={list} aria-live="polite">
          {!turns.length && <p className="muted">Questions about this plan and the factory, answered from their data. Try one of these:</p>}
          {!turns.length && ai.data && <p className="ask-note">{ai.data.ai
            ? "An AI model reads your question to pick the answer. Only the question is sent; every figure comes from the factory's own data."
            : "Answers come from the app's built-in reader. Nothing is sent outside the factory."}</p>}
          {turns.map((t) => {
            if (t.pending) return (
              <div key={t.id} className="ask-turn"><p className="ask-q">{t.question}</p>
                <div className="ask-answer ask-wait" role="status"><span className="ask-dots" aria-hidden="true"><i /><i /><i /></span>Reading your question…</div></div>
            );
            const answer = ask(t.question, ctx, t.route);
            return (
              <div key={t.id} className="ask-turn">
                <p className="ask-q">{t.question}</p>
                <AnswerCard answer={answer} failed={Boolean(answer.missing && failed[answer.missing])} onOrder={onOrder} />
                {t.fellBack && <small className="ask-note">The AI reader is busy or unavailable, so the built-in reader answered.</small>}
              </div>
            );
          })}
        </div>
        <div className="ask-chips" role="group" aria-label="Suggested questions">
          {SUGGESTIONS.map((s) => <button key={s} type="button" className="ask-chip" onClick={() => send(s)}>{s}</button>)}
        </div>
        <form className="ask-form" onSubmit={submit}>
          <label htmlFor="ask-input" className="sr-only">Your question</label>
          <input id="ask-input" value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask about an order, a line, stock…" autoComplete="off" />
          <button type="submit" className="btn primary icon" aria-label="Ask" disabled={!text.trim()}><ArrowUp size={18} /></button>
        </form>
      </div>
    </dialog>
  );
}
