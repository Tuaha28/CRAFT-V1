import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import * as Dialog from "@radix-ui/react-dialog";
import { Search as SearchIcon } from "lucide-react";
import { erp } from "../lib/api";
import { useReadyScope } from "../app/scope";

/** Ctrl+K search across orders, people, machines and styles of the chosen factory. */
export function Search() {
  const ready = useReadyScope();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => { const t = setTimeout(() => setDebounced(q.trim()), 250); return () => clearTimeout(t); }, [q]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setOpen(true); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const results = useQuery({ queryKey: ["search", ready, debounced], queryFn: () => erp.search(ready![0], ready![1], debounced),
    enabled: Boolean(ready && debounced.length >= 2) });
  const go = (to: string) => { setOpen(false); setQ(""); navigate(to); };
  const groups = results.data ? [
    { title: "Orders", items: results.data.orders.map((o) => ({ key: `o${o.production_order_id}`, label: `Order ${o.production_order_id}, ${o.client_name}`, hint: `${o.po_number}, due ${o.delivery_deadline}`, to: `/orders/${o.production_order_id}` })) },
    { title: "People", items: results.data.people.map((p) => ({ key: `p${p.employee_id}`, label: `${p.first_name} ${p.last_name}`, hint: `${String(p.role).replace("_", " ")}, #${p.employee_id}`, to: `/workforce?q=${p.employee_id}` })) },
    { title: "Machines", items: results.data.machines.map((m) => ({ key: `m${m.asset_id}`, label: `Machine ${m.asset_id}, ${m.machine_model}`, hint: String(m.current_status).replace("_", " "), to: `/machines?q=${m.asset_id}` })) },
    { title: "Styles", items: results.data.styles.map((s) => ({ key: `s${s.product_id}`, label: `Style ${s.style_code}`, hint: String(s.name), to: `/styles/${s.product_id}` })) },
  ].filter((g) => g.items.length) : [];

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button type="button" className="search-trigger"><SearchIcon size={15} aria-hidden="true" />Search orders, people, machines<kbd>Ctrl K</kbd></button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="overlay" />
        <Dialog.Content className="search-dialog" aria-describedby={undefined}>
          <Dialog.Title className="sr-only">Search</Dialog.Title>
          <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="PO number, client, worker name or id, machine, style" />
          <div className="search-results">
            {debounced.length < 2 ? <p className="muted small">Type at least two characters.</p>
              : results.isPending ? <p className="muted small">Searching…</p>
              : results.isError ? <p className="late-text small">{results.error.message}</p>
              : !groups.length ? <p className="muted small">Nothing matches “{debounced}” in this factory.</p>
              : groups.map((g) => (
                <div key={g.title}><h3>{g.title}</h3>
                  {g.items.map((i) => <button key={i.key} type="button" onClick={() => go(i.to)}><span>{i.label}</span><span className="muted small">{i.hint}</span></button>)}
                </div>))}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
