import { useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { Activity, AlertTriangle, BarChart3, Bell, CalendarDays, ChevronDown, ClipboardList, Factory, Gauge, Menu, PackageSearch, ShieldCheck, Users, Workflow } from "lucide-react";
import { useFactory } from "../app/FactoryContext";
import { useFactories } from "../services/queries";
import { rows } from "../services/api";
import { ErrorBlock } from "./Primitives";

const navigation = [
  { label: "Dashboard", path: "/dashboard", icon: Gauge, section: "CONTROL ROOM" },
  { label: "Production Schedule", path: "/schedule", icon: CalendarDays },
  { label: "Orders", path: "/orders", icon: PackageSearch },
  { label: "Resources", path: "/resources", icon: Users, section: "FACTORY STATE" },
  { label: "Disruptions", path: "/disruptions", icon: AlertTriangle, attention: true },
  { label: "CRAFT Control", path: "/craft", icon: Workflow, section: "SCHEDULING" },
  { label: "Integration Test", path: "/craft/governance", icon: ShieldCheck },
  { label: "Analytics", path: "/analytics", icon: BarChart3, section: "GOVERNANCE" },
  { label: "Run History", path: "/history", icon: ClipboardList },
];
const titles: Record<string, string> = { "/dashboard": "Dashboard", "/schedule": "Production Schedule", "/orders": "Orders", "/resources": "Resources", "/disruptions": "Disruption Center", "/craft": "CRAFT Control", "/analytics": "Analytics", "/history": "Run History" };

export function AppShell({ children }: { children: React.ReactNode }) {
  const location = useLocation(); const navigate = useNavigate(); const { factoryId, setFactoryId } = useFactory(); const factoriesQuery = useFactories(); const factories = rows(factoriesQuery.data); const [collapsed, setCollapsed] = useState(false);
  return <div className={`app-frame ${collapsed ? "nav-collapsed" : ""}`}>
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Factory size={20}/></span><span className="brand-word">CRAFT<small>PRODUCTION CONTROL</small></span><button className="collapse-toggle" aria-label="Toggle navigation" onClick={() => setCollapsed(!collapsed)}><Menu size={17}/></button></div>
      <div className="site-switch"><span className="site-dot"/><div><strong>Garment operations</strong><span>Network control</span></div><ChevronDown size={15}/></div>
      <nav className="main-nav" aria-label="Main navigation">{navigation.map(item => <div key={item.path}>{item.section && <div className="nav-section">{item.section}</div>}<NavLink to={item.path} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`} title={collapsed ? item.label : undefined}><item.icon size={18}/><span>{item.label}</span>{item.attention && <i className="nav-alert"/>}</NavLink></div>)}</nav>
      <div className="sidebar-footer"><div className="system-state"><span className="pulse-dot"/><div><strong>Demo environment</strong><span>Static fixture source</span></div></div><div className="sidebar-footnote"><ShieldCheck size={14}/> Demo schedule context</div></div>
    </aside>
    <div className="main-column">
      <header className="topbar"><div className="topbar-left"><div className="mobile-brand"><Factory size={18}/> CRAFT</div><label className="factory-select"><span>FACTORY</span>{factoriesQuery.isError ? <ErrorBlock message="Factory list unavailable" retry={() => void factoriesQuery.refetch()}/> : <select aria-label="Select factory" value={factoryId ?? "all"} onChange={event => setFactoryId(event.target.value === "all" ? undefined : Number(event.target.value))}><option value="all">All factories</option>{factories.map(factory => <option value={factory.id} key={factory.id}>{factory.name} · {factory.region.split(" · ")[1]}</option>)}</select>}</label><div className="topbar-context"><span className="context-light"/> Shift A <span className="context-divider">·</span> 06:00–14:00 <span className="context-divider">·</span> 10:20 UTC fixture</div></div><div className="topbar-right"><span className="demo-badge"><i/> DEMO / MOCK DATA</span><button className="topbar-icon" aria-label="Open attention queue" onClick={() => navigate("/disruptions")}><Bell size={18}/><i/></button><div className="user-chip"><span className="avatar">SR</span><span><strong>S. Rahman</strong><small>Production manager</small></span><ChevronDown size={14}/></div></div></header><nav className="mobile-nav" aria-label="Mobile navigation">{navigation.map(item => <NavLink key={item.path} to={item.path} className={({ isActive }) => `mobile-nav-link ${isActive ? "active" : ""}`}><item.icon size={16}/><span>{item.label === "Production Schedule" ? "Schedule" : item.label === "Disruptions" ? "Events" : item.label === "CRAFT Control" ? "CRAFT" : item.label === "Run History" ? "History" : item.label}</span></NavLink>)}</nav>
      <main className="content-area"><div className="content-heading"><div><div className="breadcrumb"><span>Operations</span><span> / </span><strong>{titles[location.pathname] ?? "Operations"}</strong></div></div><div className="heading-live"><Activity size={14}/> DEMO FACTORY STATE <span>Updated 10:20:14</span></div></div>{children}</main>
    </div>
  </div>;
}
