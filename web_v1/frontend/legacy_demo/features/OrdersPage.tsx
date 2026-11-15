import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createColumnHelper } from "@tanstack/react-table";
import { Clipboard, Flag } from "lucide-react";
import { api } from "../services/api";
import { keys, useOrders } from "../services/queries";
import { useFactory } from "../app/FactoryContext";
import { useToast } from "../app/ToastContext";
import { number, shortDate } from "../lib/format";
import type { Order } from "../types/contracts";
import { DataTable } from "../components/DataTable";
import { Drawer, ConfirmDialog } from "../components/Dialogs";
import { Badge, ErrorBlock, LoadingBlock, PageTitle, SearchInput } from "../components/Primitives";

const col = createColumnHelper<Order>();
export default function OrdersPage() {
  const { factoryId } = useFactory(); const query = useOrders(factoryId); const [search, setSearch] = useState(""); const [risk, setRisk] = useState("all"); const [priority, setPriority] = useState("all"); const [selected, setSelected] = useState<string | null>(null); const toast = useToast(); const client = useQueryClient();
  const list = query.data?.results ?? [];
  const filtered = list.filter(order => (!risk || risk === "all" || order.risk === risk) && (!priority || priority === "all" || order.priority === priority) && `${order.id} ${order.style} ${order.sku} ${order.current_operation}`.toLowerCase().includes(search.toLowerCase()));
  const detail = useQuery({ queryKey: keys.order(selected ?? ""), queryFn: () => api.order(selected!), enabled: !!selected });
  const priorityMutation = useMutation({ mutationFn: (input: { id: string; priority: string }) => api.demoPriority(input.id, input.priority), onSuccess: data => { toast("Demo priority updated", data.notice); void client.invalidateQueries({ queryKey: keys.orders(factoryId) }); void client.invalidateQueries({ queryKey: keys.order(data.order.id) }); }, onError: error => toast("Action not completed", String(error), "error") });
  const columns = useMemo(() => [
    col.accessor("id", { header: "PO / Order", cell: info => <strong className="mono order-id">{info.getValue()}</strong>, size: 116 }),
    col.accessor("factory_name", { header: "Factory", size: 110 }),
    col.accessor("style", { header: "Style / SKU", cell: info => <span>{info.getValue()}<small className="table-subtext">{info.row.original.sku}</small></span>, size: 170 }),
    col.accessor("quantity", { header: "Qty", cell: info => <span className="numeric">{number(info.getValue())}</span>, size: 82 }),
    col.accessor("smv", { header: "SMV", cell: info => <span className="numeric">{info.getValue().toFixed(1)}</span>, size: 72 }),
    col.accessor("release", { header: "Release", cell: info => shortDate(info.getValue()), size: 92 }),
    col.accessor("due", { header: "Ex-factory", cell: info => <span className={new Date(info.getValue()).getTime() < Date.now() ? "date-overdue" : ""}>{shortDate(info.getValue())}</span>, size: 95 }),
    col.accessor("progress", { header: "Progress", cell: info => <div className="progress-cell"><div className="progress-track"><span style={{ width: `${info.getValue()}%` }}/></div><small>{info.getValue()}%</small></div>, size: 125 }),
    col.accessor("current_operation", { header: "Current operation", size: 145 }),
    col.accessor("priority", { header: "Priority", cell: info => <Badge tone={info.getValue() === "Urgent" ? "Delayed" : info.getValue() === "High" ? "At risk" : "neutral"}>{info.getValue()}</Badge>, size: 88 }),
    col.accessor("risk", { header: "Risk", cell: info => <Badge tone={info.getValue()} dot>{info.getValue()}</Badge>, size: 90 }),
  ], []);
  if (query.isLoading) return <LoadingBlock label="Loading order book"/>;
  if (query.isError) return <ErrorBlock message={String(query.error)} retry={() => void query.refetch()}/>;
  const order = detail.data;
  return <><PageTitle eyebrow="CUSTOMER COMMITMENTS" title="Orders" description="Order progress, routing position and ex-factory exposure across the selected scope." actions={<button className="button button-quiet" onClick={() => void query.refetch()}><Clipboard size={14}/> Refresh order book</button>}/>
    <div className="orders-summary-strip"><div><strong>{number(list.length)}</strong><span>visible orders</span></div><div><strong>{list.filter(x => x.risk !== "On track").length}</strong><span>require attention</span></div><div><strong>{list.filter(x => x.priority === "Urgent").length}</strong><span>urgent priority</span></div><span className="orders-demo-note"><i/> Priority controls update demo fixtures only</span></div>
    <div className="toolbar"><SearchInput value={search} onChange={setSearch} placeholder="Search PO, style, SKU, operation…"/><select aria-label="Risk filter" value={risk} onChange={e => setRisk(e.target.value)}><option value="all">All risk states</option><option>On track</option><option>At risk</option><option>Delayed</option></select><select aria-label="Priority filter" value={priority} onChange={e => setPriority(e.target.value)}><option value="all">All priorities</option><option>Urgent</option><option>High</option><option>Normal</option></select><span className="toolbar-spacer"/><span className="section-kicker">{filtered.length} ORDERS</span></div>
    <section className="panel"><DataTable data={filtered} columns={columns} rowId={row => row.id} onRowClick={row => setSelected(row.id)} emptyTitle="No matching orders" emptyText="Clear a filter or search a different PO / SKU."/></section>
    <Drawer open={!!selected} onOpenChange={open => !open && setSelected(null)} title={order ? `${order.id} · ${order.style}` : selected ?? "Order detail"} description={order ? `${order.factory_name} · ${order.line_id} · ${order.sku}` : "Loading order and routing detail"} footer={order && <><ConfirmDialog trigger={<button className="button button-quiet"><Flag size={14}/> Mark urgent</button>} title="Set this order to urgent?" description="This demo action changes the in-memory mock order only. It does not notify production or trigger a real CRAFT repair." confirmLabel="Set urgent in demo" onConfirm={() => priorityMutation.mutate({ id: order.id, priority: "Urgent" })}/><button className="button button-primary" onClick={() => setSelected(null)}>Close</button></>}>{detail.isLoading ? <LoadingBlock/> : detail.isError ? <ErrorBlock message={String(detail.error)} retry={() => void detail.refetch()}/> : order ? <div><div className="order-progress-head"><div><span className="section-kicker">PRODUCTION PROGRESS</span><strong>{order.progress}%</strong></div><Badge tone={order.risk} dot>{order.risk}</Badge></div><div className="order-progress-track"><i style={{ width: `${order.progress}%` }}/></div><div className="detail-grid detail-section"><Detail label="Quantity" value={`${number(order.quantity)} pcs`}/><Detail label="SMV" value={`${order.smv} min`}/><Detail label="Release" value={shortDate(order.release)}/><Detail label="Ex-factory" value={shortDate(order.due)}/><Detail label="Current operation" value={order.current_operation}/><Detail label="Priority" value={order.priority}/></div><div className="detail-section"><h3>Routing sequence</h3><div className="routing-list">{order.routing?.map((operation, index) => <div className="routing-row" key={operation.id}><span className={`routing-step ${operation.status.toLowerCase().replace(" ", "-")}`}>{index + 1}</span><div><strong>{operation.operation}</strong><small>{operation.line_id} · {operation.machine_id}</small></div><Badge tone={operation.status === "Completed" ? "completed" : operation.status === "Running" ? "running" : operation.risk}>{operation.status}</Badge></div>)}</div></div><div className="detail-section"><h3>Schedule context</h3><p className="muted-copy">Routing and order state are fixture data for this demo. Schedule acceptance and physical validation are shown separately in CRAFT Control.</p></div></div> : null}</Drawer>
  </>;
}
function Detail({ label, value }: { label: string; value: string }) { return <div className="detail-field"><label>{label}</label><strong>{value}</strong></div>; }
