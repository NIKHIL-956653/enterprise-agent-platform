export function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

export function shortHash(h: string): string {
  return h.slice(0, 12);
}

export function fieldErrors(detail: unknown): string[] {
  // FastAPI's 422 detail: [{loc: ["body", "text"], msg: "...", ...}]. Map it to lines a
  // person can act on instead of dumping JSON at them.
  if (!Array.isArray(detail)) return [typeof detail === "string" ? detail : "Invalid input."];
  return detail.map((d) => {
    const row = d as { loc?: unknown[]; msg?: string };
    const field = (row.loc ?? []).filter((p) => p !== "body").join(".");
    return field ? `${field}: ${row.msg ?? "invalid"}` : (row.msg ?? "invalid");
  });
}
