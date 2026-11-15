import { Link, useNavigate, useParams } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { erp, type ApiError } from "../../lib/api";
import { num } from "../../lib/format";
import { useReadyScope } from "../../app/scope";
import { DataTable, Meter, Pager, useListParams } from "../../components/DataTable";
import { LineBalance } from "../../components/LineBalance";
import { Loading, Measure, Notice, PageHead } from "../../components/ui";

const SIZE = 50;

export default function Styles() {
  const ready = useReadyScope();
  const navigate = useNavigate();
  const { get, set, page } = useListParams();
  const params = { q: get("q"), category: get("category"), page, size: SIZE };
  const data = useQuery({ queryKey: ["erp-styles", ready, params], queryFn: () => erp.styles(ready![0], ready![1], params),
    enabled: Boolean(ready), placeholderData: keepPreviousData });
  return (
    <>
      <PageHead title="Styles" lead="Garment styles and how they are sewn. Styles with orders in this factory come first." />
      <section className="panel section">
        <div className="toolbar">
          <div className="field"><label htmlFor="sq">Search</label>
            <input id="sq" defaultValue={get("q")} placeholder="Style code or name" onChange={(e) => set({ q: e.target.value })} /></div>
          <div className="field"><label htmlFor="sc">Category</label>
            <select id="sc" value={get("category")} onChange={(e) => set({ category: e.target.value })}>
              <option value="">All categories</option>
              {data.data?.categories.map((c) => <option key={String(c.category)} value={String(c.category)}>{c.category} ({c.n})</option>)}
            </select></div>
        </div>
        {!ready || data.isPending ? <Loading /> : data.isError ? <Notice tone="error">{data.error.message}</Notice> : (
          <>
            <DataTable rows={data.data.items} onRow={(r) => navigate(`/styles/${r.product_id}`)} columns={[
              { key: "style_code", label: "Style" }, { key: "name", label: "Name" }, { key: "category", label: "Category" },
              { key: "steps", label: "Sewing steps", num: true },
              { key: "sam", label: "SAM per piece (min)", num: true, render: (r) => num(Number(r.sam), 2) },
              { key: "orders", label: "Orders here", num: true },
            ]} />
            <Pager page={page} size={SIZE} total={data.data.total} onPage={(p) => set({ page: String(p) })} />
          </>)}
      </section>
    </>
  );
}

export function StylePage() {
  const { id = "" } = useParams();
  const ready = useReadyScope();
  const data = useQuery({ queryKey: ["erp-style", ready, id], queryFn: () => erp.style(ready![0], ready![1], Number(id)), enabled: Boolean(ready) });
  if (!ready || data.isPending) return <Loading />;
  if (data.isError) return <Notice tone="error">{(data.error as ApiError).status === 404 ? "Style not found." : data.error.message}</Notice>;
  const { product, variants, routing, bom } = data.data;
  const sam = routing.reduce((s, r) => s + Number(r.sam), 0);
  const maxSam = Math.max(...routing.map((r) => Number(r.sam)), 0);
  return (
    <>
      <PageHead title={`Style ${product.style_code}`} lead={`${product.name} (${product.category})`}><Link className="btn" to="/styles">All styles</Link></PageHead>
      <div className="measures">
        <Measure value={routing.length} label="sewing steps" />
        <Measure value={num(sam, 2)} label="standard minutes per piece" />
        <Measure value={variants.length} label="sizes and colours" />
      </div>
      <section className="panel section">
          <h2>Operation bulletin</h2>
          <p className="muted small">The longest step limits how fast one line can sew this style.</p>
          <DataTable rows={routing} columns={[
            { key: "step", label: "Step", num: true }, { key: "operation_name", label: "Operation" },
            { key: "required_machine_type", label: "Machine type" },
            { key: "sam", label: "SAM (min)", num: true, render: (r) => num(Number(r.sam), 2) },
            { key: "share", label: "", render: (r) => <Meter value={Number(r.sam)} max={maxSam} bad={Number(r.sam) === maxSam} /> },
          ]} />
      </section>
      <LineBalance routing={routing} />
      <details className="panel section more">
        <summary>Show materials and sizes</summary>
        <div className="grid-2">
          <section className="section"><h2>Bill of materials</h2>
            <DataTable rows={bom} columns={[{ key: "name", label: "Material" },
              { key: "per_piece", label: "Per piece", num: true, render: (r) => `${num(Number(r.per_piece), 3)} ${r.unit}` },
              { key: "wastage_pct", label: "Wastage", num: true, render: (r) => `${num(Number(r.wastage_pct), 0)}%` }]} /></section>
          <section className="section"><h2>Sizes and colours</h2>
            <DataTable rows={variants} columns={[{ key: "size", label: "Size" }, { key: "color", label: "Colour" }, { key: "sku", label: "SKU" }]} /></section>
        </div>
      </details>
    </>
  );
}
