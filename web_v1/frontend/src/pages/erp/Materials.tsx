import { useQuery } from "@tanstack/react-query";
import { erp } from "../../lib/api";
import { num, qty } from "../../lib/format";
import { useReadyScope } from "../../app/scope";
import { Chip, DataTable } from "../../components/DataTable";
import { Loading, Measure, Notice, PageHead, PageTabs, RESOURCE_TABS } from "../../components/ui";

export default function Materials() {
  const ready = useReadyScope();
  const data = useQuery({ queryKey: ["erp-materials", ready], queryFn: () => erp.materials(ready![0], ready![1]), enabled: Boolean(ready) });
  if (!ready || data.isPending) return <Loading />;
  if (data.isError) return <Notice tone="error">{data.error.message}</Notice>;
  const { materials, suppliers, stock } = data.data;
  const short = materials.filter((m) => Number(m.shortage) > 0);
  return (
    <>
      <PageHead title="Materials" lead="Stock against what this factory's open orders still need. Stock is shared by the whole company." />
      <PageTabs items={RESOURCE_TABS} label="Resources" />
      {short.length > 0 && (
        <section className="panel hero shortage" aria-label="Shortage">
          <p className="eyebrow">Short for open orders</p>
          {short.map((m) => (
            <div key={m.name} className="shortage-row">
              <strong>{m.name}</strong>
              <span className="figure late-text">{qty(m.shortage, m.unit)}</span>
              <span className="muted">needed {qty(m.required_open_orders, m.unit)}, in stock {qty(m.in_stock, m.unit)}</span>
            </div>
          ))}
        </section>
      )}
      <div className="measures">
        <Measure value={materials.length} label="materials" />
        <Measure value={short.length} label="short for open orders" tone={short.length ? "late" : "ontime"} />
        <Measure value={suppliers.length} label="suppliers" />
      </div>
      <section className="panel section">
        <h2>Stock against open orders</h2>
        <DataTable rows={materials} columns={[
          { key: "name", label: "Material" }, { key: "material_type", label: "Type" },
          { key: "in_stock", label: "In stock", num: true, render: (r) => qty(r.in_stock, r.unit) },
          { key: "required_open_orders", label: "Needed by open orders", num: true, render: (r) => qty(r.required_open_orders, r.unit) },
          { key: "shortage", label: "Status", render: (r) => Number(r.shortage) > 0
            ? <Chip value={`Short by ${qty(r.shortage, r.unit)}`} /> : <Chip value="Covered" /> },
        ]} />
      </section>
      <details className="panel section more">
        <summary>Show suppliers and warehouse stock</summary>
        <div className="grid-2">
        <section className="section"><h2>Suppliers</h2>
          <DataTable rows={suppliers} columns={[{ key: "name", label: "Supplier" },
            { key: "lead_time_days", label: "Lead time (days)", num: true },
            { key: "reliability_score", label: "Reliability", num: true, render: (r) => `${num(Number(r.reliability_score), 1)}%` },
            { key: "stock_lines", label: "Stock lines", num: true }]} /></section>
        <section className="section"><h2>Warehouse stock</h2>
          <div style={{ maxHeight: 420, overflow: "auto" }}>
            <DataTable rows={stock} columns={[{ key: "warehouse_location", label: "Location" }, { key: "material", label: "Material" },
              { key: "current_quantity", label: "Quantity", num: true, render: (r) => qty(r.current_quantity, r.unit) },
              { key: "supplier", label: "Supplier" }]} /></div></section>
        </div>
      </details>
    </>
  );
}
