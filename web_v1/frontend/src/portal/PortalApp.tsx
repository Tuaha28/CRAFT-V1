import { Fragment, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { BrowserRouter, Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardList, LogOut, MessageCircle, Package, Send, Shirt } from "lucide-react";
import { api, portal, type ApiError, type ChatReply, type PendingAction, type PortalOrder, type PortalRequest, type Row } from "../lib/api";
import { useSession } from "../app/session";
import { Logo, ThemeToggle } from "../components/Shell";
import { Chip, DataTable, Meter } from "../components/DataTable";
import { Loading, Notice, PageHead } from "../components/ui";
import { Toaster } from "../components/Toaster";

const day = (iso: string | null | undefined) =>
  iso ? new Date(`${iso.slice(0, 10)}T00:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "—";

const NAV = [
  { to: "/", label: "My orders", icon: ClipboardList, end: true },
  { to: "/catalogue", label: "Catalogue", icon: Shirt },
  { to: "/assistant", label: "Assistant", icon: MessageCircle },
  { to: "/requests", label: "My requests", icon: Package },
];

function PortalShell({ children }: { children: ReactNode }) {
  const session = useSession();
  const client = useQueryClient();
  const { pathname } = useLocation();
  const signOut = async () => { await api.logout(); await client.invalidateQueries({ queryKey: ["session"] }); };
  const me = useQuery({ queryKey: ["portal-me"], queryFn: portal.me, refetchInterval: 30_000 });
  return (
    <div className="shell portal">
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className="rail">
        <div className="rail-top">
          <NavLink to="/" className="brand" aria-label="Customer portal home"><Logo /><strong>Needle Craft</strong></NavLink>
        </div>
        <nav aria-label="Customer portal" id="main-nav">
          <div className="nav-group">
            {NAV.map(({ to, label, icon: Icon, end }) => (
              <NavLink key={to} to={to} end={end} title={label}
                className={({ isActive }) => (isActive || (to === "/" && pathname.startsWith("/orders")) ? "active" : undefined)}>
                <Icon size={20} aria-hidden="true" /><span className="nav-label">{label}</span>
              </NavLink>))}
          </div>
        </nav>
        <div className="rail-foot">
          <span className="avatar" aria-hidden="true">{(session.customer?.client_name ?? "?").slice(0, 2).toUpperCase()}</span>
          <span className="rail-name"><strong>{session.customer?.client_name}</strong><small>{session.username}</small></span>
          <button type="button" className="sign-out" onClick={signOut} title="Sign out"><LogOut size={18} aria-hidden="true" /><span className="nav-label">Sign out</span></button>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar"><span className="muted small">Customer portal · {session.customer?.client_name}</span>
          {me.data?.simulated && <span className="sim-badge" title="A demonstration: the factory's progress is simulated">Simulated factory clock</span>}
          <div className="topbar-tools"><ThemeToggle /></div></header>
        <main className="main" id="main" tabIndex={-1}><div key={pathname} className="page">{children}</div></main>
      </div>
      <Toaster />
    </div>
  );
}

function ReadyDate({ o }: { o: PortalOrder }) {
  return (
    <span className={o.late ? "late-text" : undefined}>
      {o.stage === "Shipped" ? `Shipped ${day(o.shipped_on)}` : o.estimated_ready_date ? day(o.estimated_ready_date) : "To be confirmed"}
      {o.late && <><br /><small>{o.estimated_ready_date ? "after the due date" : "past the due date"}</small></>}
    </span>
  );
}

function Orders() {
  const navigate = useNavigate();
  const orders = useQuery({ queryKey: ["portal-orders"], queryFn: portal.orders });
  if (orders.isPending) return <Loading label="Reading your orders" />;
  if (orders.isError) return <Notice tone="error">{orders.error.message}</Notice>;
  const rows = orders.data;
  const open = rows.filter((o) => o.stage !== "Shipped");
  const late = open.filter((o) => o.late).length;
  return (
    <>
      <PageHead title="My orders" lead={`${rows.length} order line${rows.length === 1 ? "" : "s"} · ${open.length} in progress${late ? ` · ${late} expected after the due date` : ""}`} />
      <section className="panel section">
        <DataTable rows={rows as unknown as Row[]} empty="No orders yet." onRow={(r) => navigate(`/orders/${r.order_id}`)} columns={[
          { key: "order_id", label: "Order", render: (r) => <><Link to={`/orders/${r.order_id}`}><strong>{String(r.order_id)}</strong></Link><br /><span className="muted small">{String(r.po_number ?? "")}</span></> },
          { key: "product", label: "Style", render: (r) => <>{String(r.product ?? r.style_code)}<br /><span className="muted small">{String(r.style_code)}</span></> },
          { key: "quantity", label: "Pieces", num: true, render: (r) => Number(r.quantity).toLocaleString("en-US") },
          { key: "stage", label: "Where it is", render: (r) => <><Chip value={String(r.stage)} /><Meter value={Number(r.progress_percent)} max={100} /></> },
          { key: "due_date", label: "Due", render: (r) => day(r.due_date as string) },
          { key: "estimated_ready_date", label: "Expected ready", render: (r) => <ReadyDate o={r as unknown as PortalOrder} /> },
        ]} />
        <p className="muted small">Dates are estimates from the factory's production plan and can move. Ask the assistant or send a request if a date matters to you.</p>
      </section>
    </>
  );
}

const STAGES = ["Confirmed", "In production", "All pieces sewn", "Packing", "Shipped"];

function OrderDetail() {
  const { ref = "" } = useParams();
  const order = useQuery({ queryKey: ["portal-order", ref], queryFn: () => portal.order(ref) });
  if (order.isPending) return <Loading />;
  if (order.isError) return <Notice tone="error">{(order.error as ApiError).status === 404 ? "This order is not in your account." : order.error.message}</Notice>;
  const o = order.data;
  const at = STAGES.indexOf(o.stage);
  return (
    <>
      <PageHead title={`Order ${o.order_id}`} lead={<>{o.po_number} · {o.product} ({o.style_code}) · {o.quantity.toLocaleString("en-US")} pieces</>}>
        <Chip value={o.stage} /><Link className="btn" to="/">All orders</Link>
      </PageHead>
      <section className="panel section hero order-hero" aria-labelledby="journey-title">
        <div className="order-hero-main">
          <h2 id="journey-title">Where your order is</h2>
          <ol className="journey">
            {STAGES.map((name, i) => (
              <li key={name} className={i < at || o.stage === "Shipped" ? "done" : i === at ? "now" : ""} aria-current={i === at ? "step" : undefined}>
                <span className="journey-dot" aria-hidden="true">{i < at || o.stage === "Shipped" ? "✓" : i + 1}</span>
                <strong>{name}</strong>
                <small>{name === "In production" && i === at ? `${o.progress_percent}% of pieces through every step` : name === "Shipped" && o.shipped_on ? day(o.shipped_on) : ""}</small>
              </li>))}
          </ol>
        </div>
      </section>
      <div className="plan-layout">
        <section className="panel section">
          <h2>Dates</h2>
          <dl className="facts">
            <dt>Ordered</dt><dd>{day(o.ordered_on)}</dd>
            <dt>Due</dt><dd>{day(o.due_date)}</dd>
            <dt>Expected ready</dt><dd><ReadyDate o={o} /> <span className="muted small">{o.ready_from === "plan" ? "(from the current production plan)" : "(from the order schedule)"}</span></dd>
            <dt>Expected arrival</dt><dd>{day(o.estimated_arrival_date)}</dd>
          </dl>
          {o.customer_note && <p className="muted small">{o.customer_note}</p>}
        </section>
        <aside className="plan-side">
          <section className="panel section">
            <h2>Shipping</h2>
            <dl className="facts">
              <dt>Carrier</dt><dd>{o.carrier ?? "not yet"}</dd>
              <dt>Tracking</dt><dd>{o.tracking_reference ?? "not yet"}</dd>
            </dl>
          </section>
          <section className="panel section">
            <h2>Need a change?</h2>
            <p className="muted small">Ask for more pieces, another colour or a new date. The factory team reviews every request.</p>
            <Link className="btn primary" to={`/requests?order=${o.order_id}`}>Request a change</Link>
          </section>
        </aside>
      </div>
    </>
  );
}

function Catalogue() {
  const products = useQuery({ queryKey: ["portal-products"], queryFn: portal.products });
  if (products.isPending) return <Loading />;
  if (products.isError) return <Notice tone="error">{products.error.message}</Notice>;
  return (
    <>
      <PageHead title="Catalogue" lead="Garment families we make. Prices are indicative base prices per piece, not a quotation." />
      <div className="portal-cards">
        {products.data.map((p) => (
          <section key={p.style_code} className="panel section portal-card">
            <span className="muted small">{p.style_code}</span>
            <h2>{p.name}</h2>
            <dl className="facts">
              <dt>Fabric</dt><dd>{p.fabric}</dd>
              <dt>Weight</dt><dd>{p.gsm} GSM</dd>
              <dt>Sizes</dt><dd>{p.sizes}</dd>
              <dt>Minimum order</dt><dd>{p.moq.toLocaleString("en-US")} pieces per colour</dd>
              <dt>From</dt><dd>USD {p.base_price_usd.toFixed(2)} per piece</dd>
            </dl>
            <Link className="btn" to={`/requests?kind=quote&style=${p.style_code}`}>Ask for a quote</Link>
          </section>))}
      </div>
      <p className="muted small">Indicative values: freight, duties, taxes, branding and special packaging are extra. A binding price comes only with a quotation from the sales team.</p>
    </>
  );
}

// The assistant cites its evidence as [KB-18] or [TOOL-get_order-1]; the page lists the sources by title instead.
const clean = (answer: string) => answer.replace(/\s*\[(?:KB|TOOL)-[\w-]+\]/g, "").trim();

interface Message { role: "you" | "assistant"; text: string; sources?: ChatReply["sources"]; actions?: PendingAction[] }

function ActionCard({ action }: { action: PendingAction }) {
  const client = useQueryClient();
  const [receipt, setReceipt] = useState<string | null>(null);
  const confirm = useMutation({
    mutationFn: () => portal.confirm(action.action_id),
    onSuccess: (r) => { setReceipt(r.request_id); return client.invalidateQueries({ queryKey: ["portal-requests"] }); },
  });
  const details = Object.entries(action.details).filter(([, v]) => v !== null && v !== "");
  return (
    <div className="portal-action">
      <strong>Send this request to the factory team?</strong>
      <dl className="facts">{details.map(([k, v]) => <Fragment key={k}><dt>{k.replace(/_/g, " ")}</dt><dd>{String(v)}</dd></Fragment>)}</dl>
      {receipt ? <p className="ontime-text">Sent as {receipt}. You can follow it under My requests.</p> : (
        <button type="button" className="btn primary" disabled={confirm.isPending} onClick={() => confirm.mutate()}>Yes, send it</button>)}
      {confirm.isError && <Notice tone="error">{confirm.error.message}</Notice>}
    </div>
  );
}

function Assistant() {
  const session = useSession();
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [messages]);
  const ask = useMutation({
    mutationFn: (message: string) => portal.chat(message, sessionId),
    onSuccess: (reply) => {
      setSessionId(reply.session_id);
      setMessages((m) => [...m, { role: "assistant", text: clean(reply.answer), sources: reply.sources, actions: reply.pending_actions }]);
    },
    onError: (error) => setMessages((m) => [...m, { role: "assistant", text: (error as ApiError).status === 503
      ? "The assistant is not available right now. Your orders and requests still work, or send a request and the factory team will answer."
      : (error as Error).message }]),
  });
  const send = (e: FormEvent) => {
    e.preventDefault();
    const message = text.trim();
    if (!message || ask.isPending) return;
    setMessages((m) => [...m, { role: "you", text: message }]);
    setText("");
    ask.mutate(message);
  };
  const starters = ["Where are my orders?", "What is the minimum order for polo shirts?", "How do samples work?"];
  return (
    <>
      <PageHead title="Assistant" lead="Ask about your orders, our garments, samples, quotes and shipping. It answers from the factory's customer handbook and your own order records." />
      <section className="panel section portal-chat">
        <div className="portal-messages" aria-live="polite">
          {!messages.length && (
            <div className="portal-starters">
              <p className="muted">Hello {session.customer?.client_name}. Try:</p>
              {starters.map((s) => <button key={s} type="button" className="btn" onClick={() => setText(s)}>{s}</button>)}
            </div>)}
          {messages.map((m, i) => (
            <div key={i} className={`portal-msg ${m.role}`}>
              <p>{m.text}</p>
              {m.sources && m.sources.length > 0 && <small className="muted">From: {[...new Set(m.sources.map((s) => s.title).filter(Boolean))].slice(0, 3).join(" · ")}</small>}
              {m.actions?.map((a) => <ActionCard key={a.action_id} action={a} />)}
            </div>))}
          {ask.isPending && <div className="portal-msg assistant"><p className="muted">Thinking…</p></div>}
          <div ref={end} />
        </div>
        <form className="portal-ask" onSubmit={send}>
          <label className="sr-only" htmlFor="portal-q">Your question</label>
          <input id="portal-q" value={text} maxLength={4000} onChange={(e) => setText(e.target.value)} placeholder="Type your question" autoComplete="off" />
          <button type="submit" className="btn primary" disabled={!text.trim() || ask.isPending}><Send size={16} aria-hidden="true" /> Send</button>
        </form>
        <p className="muted small">The assistant can make mistakes and never changes an order by itself: changes are sent as requests for the factory team to review.</p>
      </section>
    </>
  );
}

const KIND_HELP: Record<string, string> = {
  order_change: "More pieces, another colour, a new date or address for one of your orders.",
  quote: "A price for a style, quantity, colour and destination.",
  sample: "A sample of a catalogue style.",
  support: "Anything else: documents, packing, a question for the team.",
};

function RequestStatus({ r }: { r: PortalRequest }) {
  return <Chip value={r.status === "PENDING" ? "Waiting for review" : r.status === "ACCEPTED" ? "Accepted" : "Declined"} />;
}

function Requests() {
  const client = useQueryClient();
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  const orders = useQuery({ queryKey: ["portal-orders"], queryFn: portal.orders });
  const products = useQuery({ queryKey: ["portal-products"], queryFn: portal.products });
  const mine = useQuery({ queryKey: ["portal-requests"], queryFn: portal.requests });
  const [kind, setKind] = useState(params.get("kind") ?? (params.get("order") ? "order_change" : "support"));
  const [orderId, setOrderId] = useState(params.get("order") ?? "");
  const [style, setStyle] = useState(params.get("style") ?? "");
  const [quantity, setQuantity] = useState("");
  const [colour, setColour] = useState("");
  const [destination, setDestination] = useState("");
  const [description, setDescription] = useState("");
  const [key] = useState(() => crypto.randomUUID());
  const send = useMutation({
    meta: { success: "Request sent. The factory team will answer here." },
    mutationFn: () => portal.sendRequest({ kind, description, key, order_id: kind === "order_change" ? orderId : undefined,
      style_code: kind === "quote" || kind === "sample" ? style : undefined, quantity: quantity ? Number(quantity) : null,
      colour: colour || undefined, destination: destination || undefined }),
    onSuccess: () => { setDescription(""); return client.invalidateQueries({ queryKey: ["portal-requests"] }); },
  });
  const ready = description.trim().length >= 10 && (kind !== "order_change" || orderId) && (kind !== "sample" || style)
    && (kind !== "quote" || (style && quantity && colour && destination));
  return (
    <>
      <PageHead title="My requests" lead="Send a request to the factory team and follow its answer." />
      <div className="plan-layout">
        <section className="panel section">
          <h2>Your requests</h2>
          {mine.isPending ? <Loading /> : mine.isError ? <Notice tone="error">{mine.error.message}</Notice> : (
            <DataTable rows={mine.data as unknown as Row[]} empty="No requests yet." columns={[
              { key: "request_id", label: "Request", render: (r) => <><strong>{String(r.request_id)}</strong><br /><span className="muted small">{String(r.kind_label)}{r.order_id ? ` · ${r.order_id}` : r.style_code ? ` · ${r.style_code}` : ""}</span></> },
              { key: "description", label: "What you asked", render: (r) => String(r.description) },
              { key: "status", label: "Answer", render: (r) => <><RequestStatus r={r as unknown as PortalRequest} />{r.staff_note ? <><br /><span className="small">{String(r.staff_note)}</span></> : null}</> },
              { key: "created_at", label: "Sent", render: (r) => day(String(r.created_at)) },
            ]} />)}
        </section>
        <aside className="plan-side">
          <section className="panel section">
            <h2>New request</h2>
            <form onSubmit={(e) => { e.preventDefault(); if (ready) send.mutate(); }}>
              <div className="field"><label htmlFor="rq-kind">Type</label>
                <select id="rq-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
                  <option value="order_change">Order change</option><option value="quote">Quotation</option>
                  <option value="sample">Sample</option><option value="support">Support</option>
                </select></div>
              <p className="muted small">{KIND_HELP[kind]}</p>
              {kind === "order_change" && (
                <div className="field"><label htmlFor="rq-order">Order</label>
                  <select id="rq-order" value={orderId} onChange={(e) => setOrderId(e.target.value)}>
                    <option value="">Choose an order</option>
                    {(orders.data ?? []).filter((o) => o.stage !== "Shipped").map((o) => <option key={o.order_id} value={o.order_id}>{o.order_id} · {o.product}</option>)}
                  </select></div>)}
              {(kind === "quote" || kind === "sample") && (
                <div className="field"><label htmlFor="rq-style">Style</label>
                  <select id="rq-style" value={style} onChange={(e) => setStyle(e.target.value)}>
                    <option value="">Choose a style</option>
                    {(products.data ?? []).map((p) => <option key={p.style_code} value={p.style_code}>{p.name} ({p.style_code})</option>)}
                  </select></div>)}
              {(kind === "quote" || kind === "order_change") && (
                <div className="field"><label htmlFor="rq-qty">Pieces{kind === "order_change" ? " (optional)" : ""}</label>
                  <input id="rq-qty" type="number" min={1} max={1000000} value={quantity} onChange={(e) => setQuantity(e.target.value)} /></div>)}
              {kind !== "support" && (
                <div className="field"><label htmlFor="rq-colour">Colour{kind === "quote" ? "" : " (optional)"}</label>
                  <input id="rq-colour" value={colour} maxLength={100} onChange={(e) => setColour(e.target.value)} /></div>)}
              {kind === "quote" && (
                <div className="field"><label htmlFor="rq-dest">Destination</label>
                  <input id="rq-dest" value={destination} maxLength={200} onChange={(e) => setDestination(e.target.value)} placeholder="City, country" /></div>)}
              <div className="field"><label htmlFor="rq-desc">Details</label>
                <textarea id="rq-desc" rows={4} value={description} maxLength={2000} onChange={(e) => setDescription(e.target.value)} placeholder="What do you need, and by when?" /></div>
              <button type="submit" className="btn primary" disabled={!ready || send.isPending}>Send request</button>
              {send.isError && <Notice tone="error">{send.error.message}</Notice>}
            </form>
          </section>
        </aside>
      </div>
    </>
  );
}

export default function PortalApp() {
  return (
    <BrowserRouter>
      <PortalShell>
        <Routes>
          <Route path="/" element={<Orders />} />
          <Route path="/orders/:ref" element={<OrderDetail />} />
          <Route path="/catalogue" element={<Catalogue />} />
          <Route path="/assistant" element={<Assistant />} />
          <Route path="/requests" element={<Requests />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </PortalShell>
    </BrowserRouter>
  );
}
