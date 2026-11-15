import { flexRender, getCoreRowModel, getSortedRowModel, useReactTable, type ColumnDef, type SortingState } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { EmptyBlock } from "./Primitives";

export function DataTable<T>({ data, columns, rowId, onRowClick, emptyTitle = "No records found", emptyText = "Adjust the filters or clear your search to see records." }: {
  data: T[];
  // TanStack cell templates are invariant in TValue; a heterogeneous table necessarily carries per-column value types.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  columns: ColumnDef<T, any>[];
  rowId: (row: T) => string;
  onRowClick?: (row: T) => void;
  emptyTitle?: string;
  emptyText?: string;
}) {
  const [sorting, setSorting] = useState<SortingState>([]); const stableColumns = useMemo(() => columns, [columns]);
  const table = useReactTable({ data, columns: stableColumns, state: { sorting }, onSortingChange: setSorting, getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(), getRowId: rowId });
  if (!data.length) return <EmptyBlock title={emptyTitle} description={emptyText}/>;
  return <div className="table-scroll"><table className="data-table"><thead>{table.getHeaderGroups().map(group => <tr key={group.id}>{group.headers.map(header => <th key={header.id} style={{ width: header.getSize() === 150 ? undefined : header.getSize() }}><button className="th-button" onClick={header.column.getToggleSortingHandler()} disabled={!header.column.getCanSort()}>{flexRender(header.column.columnDef.header, header.getContext())}{header.column.getCanSort() && (header.column.getIsSorted() === "asc" ? <ArrowUp size={12}/> : header.column.getIsSorted() === "desc" ? <ArrowDown size={12}/> : <ArrowUpDown size={12} className="sort-muted"/>)}</button></th>)}</tr>)}</thead><tbody>{table.getRowModel().rows.map(row => <tr key={row.id} onClick={() => onRowClick?.(row.original)} className={onRowClick ? "clickable-row" : ""}>{row.getVisibleCells().map(cell => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody></table></div>;
}
