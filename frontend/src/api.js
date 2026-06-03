// Thin fetch wrapper: always sends the session cookie, JSON in/out, throws on
// non-2xx with the server's { error } message attached.
export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    credentials: "include",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON (e.g. file download) */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `Request failed (${res.status})`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const fmtMoney = (n) => "$" + (Number(n) || 0).toFixed(2);

const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];
// "2025-06" → "June 2025"
export function fmtMonth(ym) {
  if (!ym) return "—";
  const [y, m] = String(ym).split("-").map(Number);
  return (MONTH_NAMES[m - 1] || ym) + " " + y;
}
