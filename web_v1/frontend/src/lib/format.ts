export const number = (value: number) => new Intl.NumberFormat("en-US").format(value);
export const shortDate = (value: string | Date) => new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short" }).format(new Date(value));
export const dateTime = (value: string | Date) => new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
export const time = (value: string | Date) => new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
export const relative = (value: string | Date) => { const mins = Math.max(1, Math.round((Date.now() - new Date(value).getTime()) / 60_000)); return mins < 60 ? `${mins}m ago` : `${Math.floor(mins / 60)}h ago`; };
export const pct = (value: number) => `${value.toFixed(value % 1 === 0 ? 0 : 1)}%`;
