import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import type { LucideIcon } from "lucide-react";
import { LogOut, Menu, Moon, Sparkles, Sun, X, Activity, AlertTriangle, ClipboardCheck, Boxes, ClipboardList, Database, Home, ListChecks, MonitorPlay, PlusCircle, Shirt, UserCog, Wrench } from "lucide-react";
import { api } from "../lib/api";
import { can, useSession } from "../app/session";
import { useScope } from "../app/scope";
import { homeOf } from "../app/home";
import { setTheme, useTheme } from "../app/theme";
import { Search } from "./Search";
import { Alerts } from "./Alerts";
import { Toaster } from "./Toaster";
import { preload, preloadWhenIdle } from "../app/pages";

// `also`: other pages reached through tabs on this entry's page; the entry stays lit on them.
interface NavItem { to: string; label: string; icon: LucideIcon; end?: boolean; perm?: string[]; also?: string[] }
const PLANNING = ["planning.request_plan", "planning.approve_plan"];
const NAV: { group: string | null; items: NavItem[] }[] = [
  { group: null, items: [{ to: "/", label: "Home", icon: Home, end: true }] },
  { group: "Planning", items: [
    { to: "/plan", label: "New plan", icon: PlusCircle, perm: ["planning.request_plan"] },
    { to: "/runs", label: "Plans", icon: ListChecks, perm: [...PLANNING, "floor.view_floor"], also: ["/compare"] }] },
  { group: "Shop floor", items: [
    { to: "/floor/line", label: "My line", icon: Wrench, perm: ["floor.view_floor"] },
    { to: "/floor/board", label: "Line board", icon: MonitorPlay, perm: ["floor.view_floor"] },
    { to: "/floor/issues", label: "Problems", icon: AlertTriangle, perm: ["floor.view_floor"] },
    { to: "/floor/quality", label: "Quality", icon: ClipboardCheck, perm: ["floor.view_floor"], also: ["/quality"] }] },
  { group: "Factory", items: [
    { to: "/orders", label: "Orders", icon: ClipboardList }, { to: "/styles", label: "Styles", icon: Shirt },
    { to: "/workforce", label: "Resources", icon: Boxes, also: ["/machines", "/materials"] }] },
  { group: "Admin", items: [
    { to: "/users", label: "Users", icon: UserCog, perm: ["floor.manage_users"] },
    { to: "/data", label: "Data", icon: Database, perm: PLANNING },
    { to: "/activity", label: "Activity", icon: Activity, perm: ["planning.approve_plan", "floor.manage_users"] }] },
];

/** The product mark. */
export function Logo() {
  return (
    <svg className="logo" viewBox="0 0 30 30" aria-hidden="true">
      <path d="M15 3l11 6-11 6L4 9z" fill="#99f6e4" /><path d="M4 9l11 6v12L4 21z" fill="#14b8a6" /><path d="M26 9l-11 6v12l11-6z" fill="#2dd4bf" />
    </svg>
  );
}

/** Switches between the dark and the light theme; the choice is remembered per browser. */
export function ThemeToggle({ className = "icon-btn" }: { className?: string }) {
  const theme = useTheme();
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button type="button" className={className} onClick={() => setTheme(next)} aria-label={`Switch to the ${next} theme`} title={`Switch to the ${next} theme`}>
      {theme === "dark" ? <Sun size={18} aria-hidden="true" /> : <Moon size={18} aria-hidden="true" />}
    </button>
  );
}

function ScopePicker() {
  const { datasets, dataset, factoryId, set } = useScope();
  const own = useSession().scope_factory ?? null;
  if (!dataset) return null;
  return (
    <div className="scope">
      <label className="sr-only" htmlFor="scope-d">Factory data</label>
      <select id="scope-d" value={dataset.id} onChange={(e) => {
        const d = datasets.find((x) => x.id === Number(e.target.value))!;
        set(d.id, d.factories.some((f) => f.factory_id === factoryId) ? factoryId! : (own ?? d.factories[0].factory_id));
      }}>
        {datasets.map((d) => <option key={d.id} value={d.id}>{d.label}</option>)}
      </select>
      <label className="sr-only" htmlFor="scope-f">Factory</label>
      <select id="scope-f" value={factoryId ?? ""} disabled={own !== null} onChange={(e) => set(dataset.id, Number(e.target.value))}>
        {dataset.factories.filter((f) => own === null || f.factory_id === own).map((f) => <option key={f.factory_id} value={f.factory_id}>Factory {f.factory_id}</option>)}
      </select>
      <span className="muted small">as of {dataset.reference_time.slice(0, 10)}</span>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const session = useSession();
  const client = useQueryClient();
  const { pathname } = useLocation();
  const [menu, setMenu] = useState(false);  // small screens only: the rail folds behind a button
  useEffect(() => setMenu(false), [pathname]);
  // Fetch the files of the pages this person can open while the browser is idle, so they open at once.
  const allowed = NAV.flatMap((g) => g.items).filter((i) => !i.perm || i.perm.some((p) => can(session, p))).map((i) => i.to).join();
  useEffect(() => { preloadWhenIdle([...allowed.split(","), "@run", "@order"]); }, [allowed]);
  const who = `Signed in as ${session.username}${session.role_label ? `, ${session.role_label.toLowerCase()}` : ""}${session.scope_line ? ` (line ${session.scope_line})` : ""}`;
  const signOut = async () => { await api.logout(); await client.invalidateQueries({ queryKey: ["session"] }); };
  return (
    <div className="shell">
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className={`rail${menu ? " open" : ""}`}>
        <div className="rail-top">
          <NavLink to="/" className="brand" aria-label="CRAFT home"><Logo /><strong>craft</strong></NavLink>
          <button type="button" className="menu-btn" aria-expanded={menu} aria-controls="main-nav" onClick={() => setMenu((m) => !m)}>
            {menu ? <X size={20} aria-hidden="true" /> : <Menu size={20} aria-hidden="true" />}{menu ? "Close" : "Menu"}
          </button>
        </div>
        <nav aria-label="Main" id="main-nav">
          {NAV.map(({ group, items }) => ({ group, items: items.filter((i) => !i.perm || i.perm.some((p) => can(session, p)))
            // Floor roles start on their own page, so the overview gets its own address.
            .map((i) => (i.to === "/" && homeOf(session.role) ? { ...i, to: "/overview", label: "Overview" } : i)) }))
            .filter(({ items }) => items.length).map(({ group, items }) => (
            <div key={group ?? "home"} className="nav-group">
              {group && <span className="nav-title">{group}</span>}
              {items.map(({ to, label, icon: Icon, end, also }) => (
                <NavLink key={to} to={to} end={end} title={label} onPointerEnter={() => preload(to)} onFocus={() => preload(to)}
                  className={({ isActive }) => (isActive || also?.some((p) => pathname.startsWith(p)) ? "active" : undefined)}>
                  <Icon size={20} aria-hidden="true" /><span className="nav-label">{label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <Link to={`${homeOf(session.role) ? "/overview" : "/"}?ask=1`} className="rail-ask">
          <span className="rail-ask-icon" aria-hidden="true"><Sparkles size={16} /></span>
          <span><strong>Ask CRAFT</strong><small>Late orders, stock, machines, quality</small></span>
        </Link>
        <div className="rail-foot">
          <span className="avatar" title={who} aria-hidden="true">{(session.username ?? "?").slice(0, 2).toUpperCase()}</span>
          <span className="rail-name"><strong>{session.username}</strong><small>{session.role_label ?? ""}</small></span>
          <span className="rail-who">{who}. {session.production_activation ? "Production activation on." : "An approved plan goes to the shop-floor pages of this app; the factory database is never changed."}</span>
          <button type="button" className="sign-out" onClick={signOut} title="Sign out"><LogOut size={18} aria-hidden="true" /><span className="nav-label">Sign out</span></button>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar"><ScopePicker /><div className="topbar-tools"><Search /><ThemeToggle /><Alerts /></div></header>
        <main className="main" id="main" tabIndex={-1}><div key={pathname} className="page">{children}</div></main>
      </div>
      <Toaster />
    </div>
  );
}
