/** Where each floor role starts its day; office roles start on the factory overview ("/"). */
export const FLOOR_HOME: Record<string, string> = {
  supervisor: "/floor/line",
  qc: "/floor/quality",
  maintenance: "/floor/issues",
};

export function homeOf(role: string | null | undefined): string | null {
  return (role && FLOOR_HOME[role]) || null;
}
