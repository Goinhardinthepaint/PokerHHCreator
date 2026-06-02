import { useState, useEffect } from "react";
import { api, fmtMoney } from "./api.js";

// Admin console: platform stats, every worker with pay balances, per-user
// history + payment recording, stream completion, hand review, and DB export.
export default function Admin() {
  const [overview, setOverview] = useState(null);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState(null);   // selected user detail
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [tab, setTab] = useState("users");       // users | streams

  const loadOverview = () => api("/api/admin/users").then(setOverview).catch((e) => setError(e.message));
  useEffect(() => { loadOverview(); }, []);

  const openUser = async (id) => {
    setLoadingDetail(true);
    try { setDetail(await api(`/api/admin/user/${id}`)); }
    catch (e) { setError(e.message); }
    finally { setLoadingDetail(false); }
  };

  if (error) return <div style={st.page}><div style={st.err}>⚠ {error}</div></div>;
  if (!overview) return <div style={st.page}>Loading…</div>;
  const p = overview.platform;

  return (
    <div style={st.page}>
      <div style={st.headRow}>
        <h1 style={st.h1}>Admin</h1>
        <a style={st.exportBtn} href="/api/admin/export" target="_blank" rel="noreferrer">⬇ Download All Hands (.txt)</a>
      </div>

      {/* Platform stats */}
      <div style={st.cards}>
        <Stat label="Users" value={p.users} />
        <Stat label="Hands" value={p.hands.toLocaleString()} />
        <Stat label="Earnings owed" value={fmtMoney(p.owed)} accent="#fbbf24" />
        <Stat label="Payments made" value={fmtMoney(p.paid)} accent="#4ade80" />
      </div>

      <div style={st.tabs}>
        <button style={{ ...st.tab, ...(tab === "users" ? st.tabOn : {}) }} onClick={() => setTab("users")}>Users</button>
        <button style={{ ...st.tab, ...(tab === "streams" ? st.tabOn : {}) }} onClick={() => setTab("streams")}>Streams</button>
      </div>

      {tab === "users" && (
        <div style={st.panel}>
          <table style={st.table}>
            <thead>
              <tr style={st.thr}>
                <th style={st.th}>User</th><th style={st.th}>Hands</th>
                <th style={st.th}>Earned</th><th style={st.th}>Paid</th>
                <th style={st.th}>Owed</th><th style={st.th}>Last active</th>
              </tr>
            </thead>
            <tbody>
              {overview.users.map((u) => (
                <tr key={u.id} style={st.tr} onClick={() => openUser(u.id)}>
                  <td style={st.td}>{u.username}{u.is_admin ? <span style={st.adminTag}>ADMIN</span> : null}</td>
                  <td style={st.td}>{u.hands}</td>
                  <td style={{ ...st.td, color: "#4ade80" }}>{fmtMoney(u.earnings)}</td>
                  <td style={st.td}>{fmtMoney(u.paid)}</td>
                  <td style={{ ...st.td, color: u.owed > 0 ? "#fbbf24" : "#64748b" }}>{fmtMoney(u.owed)}</td>
                  <td style={{ ...st.td, color: "#64748b" }}>{(u.last_active || "").slice(0, 10)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === "streams" && (
        <div style={st.panel}>
          {overview.streams.length === 0 && <div style={st.empty}>No worked or completed streams yet.</div>}
          {overview.streams.map((s) => (
            <div key={s.id} style={st.streamRow}>
              <div style={{ minWidth: 0 }}>
                <div style={st.streamTitle}>{s.title || s.id}</div>
                <div style={st.streamMeta}>
                  {s.date || "—"} · {s.hands_done} hands ·{" "}
                  {s.contributors.map((c) => `${c.username} (${c.hands})`).join(", ") || "no contributors"}
                </div>
              </div>
              <span style={{ ...st.badge, background: s.is_complete ? "#16a34a" : "#475569" }}>
                {s.is_complete ? "complete" : "in progress"}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* User detail drawer */}
      {(detail || loadingDetail) && (
        <UserDetail
          detail={detail}
          loading={loadingDetail}
          onClose={() => setDetail(null)}
          onPaid={(updated) => { setDetail(updated); loadOverview(); }}
        />
      )}
    </div>
  );
}

function UserDetail({ detail, loading, onClose, onPaid }) {
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState("");
  const [note, setNote] = useState("");
  const [openHand, setOpenHand] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!amount) return;
    setBusy(true); setErr("");
    try {
      const updated = await api("/api/admin/payment", {
        method: "POST",
        body: { user_id: detail.user.id, amount: Number(amount), date, note },
      });
      onPaid(updated);
      setAmount(""); setNote(""); setDate("");
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  return (
    <div style={st.overlay} onClick={onClose}>
      <div style={st.drawer} onClick={(e) => e.stopPropagation()}>
        {loading || !detail ? (
          <div style={{ padding: 20 }}>Loading…</div>
        ) : (
          <>
            <div style={st.drawerHead}>
              <div>
                <div style={st.drawerTitle}>{detail.user.username}</div>
                <div style={st.drawerSub}>{detail.user.email || "no email"}</div>
              </div>
              <button style={st.closeBtn} onClick={onClose}>✕</button>
            </div>
            <div style={st.drawerBody}>
              <div style={st.miniStats}>
                <Mini k="Hands" v={detail.stats.hands} />
                <Mini k="Earned" v={fmtMoney(detail.stats.total)} c="#4ade80" />
                <Mini k="Bonus" v={fmtMoney(detail.stats.stream_bonus)} c="#fbbf24" />
                <Mini k="Owed" v={fmtMoney(detail.stats.owed)} c="#fbbf24" />
              </div>

              {/* Record payment */}
              <div style={st.section}>Record payment</div>
              <div style={st.payForm}>
                <input style={st.input} type="number" step="0.01" placeholder="amount" value={amount} onChange={(e) => setAmount(e.target.value)} />
                <input style={st.input} type="date" value={date} onChange={(e) => setDate(e.target.value)} />
                <input style={{ ...st.input, flex: 2 }} placeholder="note (e.g. PayPal June 1)" value={note} onChange={(e) => setNote(e.target.value)} />
                <button style={st.payBtn} disabled={busy || !amount} onClick={submit}>Record</button>
              </div>
              {err && <div style={st.err}>⚠ {err}</div>}

              {/* Payment history */}
              {detail.payments.length > 0 && (
                <>
                  <div style={st.section}>Payment history</div>
                  {detail.payments.map((pay) => (
                    <div key={pay.id} style={st.payRow}>
                      <span>{fmtMoney(pay.amount)} <span style={st.dim}>· {pay.date}</span></span>
                      <span style={st.dim}>{pay.note}</span>
                    </div>
                  ))}
                </>
              )}

              {/* Hand review */}
              <div style={st.section}>Hands ({detail.hands.length})</div>
              {detail.hands.map((h) => (
                <div key={h.id} style={st.handBlock}>
                  <div style={st.handRow} onClick={() => setOpenHand(openHand === h.id ? null : h.id)}>
                    <span>#{h.id} · {h.stream_id || "—"} · t={h.timestamp_seconds}s</span>
                    <span style={st.dim}>{fmtMoney(h.earnings)} {openHand === h.id ? "▾" : "▸"}</span>
                  </div>
                  {openHand === h.id && (
                    <div style={st.handDetail}>
                      {h.youtube_url && (
                        <a style={st.link} href={`${h.youtube_url}${h.youtube_url.includes("?") ? "&" : "?"}t=${h.timestamp_seconds || 0}`} target="_blank" rel="noreferrer">▶ open at timestamp ↗</a>
                      )}
                      <pre style={st.pre}>{h.pt4_text || "(no PT4 text)"}</pre>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, accent }) {
  return (
    <div style={st.statCard}>
      <div style={st.statLabel}>{label}</div>
      <div style={{ ...st.statValue, color: accent || "#f8fafc" }}>{value}</div>
    </div>
  );
}
function Mini({ k, v, c }) {
  return (
    <div style={st.mini}>
      <div style={st.miniK}>{k}</div>
      <div style={{ ...st.miniV, color: c || "#f8fafc" }}>{v}</div>
    </div>
  );
}

const st = {
  page: { flex: 1, minHeight: 0, overflowY: "auto", padding: "20px 24px 40px", color: "#e2e8f0", fontFamily: "'Inter','Segoe UI',system-ui,sans-serif", maxWidth: 1040, margin: "0 auto", width: "100%", boxSizing: "border-box" },
  headRow: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 },
  h1: { fontSize: 22, fontWeight: 800, margin: 0 },
  exportBtn: { padding: "9px 16px", background: "#16a34a", color: "#06210f", border: "none", borderRadius: 8, fontSize: 13, fontWeight: 800, cursor: "pointer", textDecoration: "none" },
  cards: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px,1fr))", gap: 12, marginBottom: 16 },
  statCard: { background: "#0d1320", border: "1px solid #1e293b", borderRadius: 12, padding: "14px 16px" },
  statLabel: { fontSize: 10.5, fontWeight: 700, letterSpacing: 1, textTransform: "uppercase", color: "#64748b" },
  statValue: { fontSize: 24, fontWeight: 800, marginTop: 4 },
  tabs: { display: "flex", gap: 6, marginBottom: 12 },
  tab: { padding: "8px 16px", background: "#0d1320", border: "1px solid #1e293b", borderRadius: 8, color: "#94a3b8", fontSize: 13, fontWeight: 700, cursor: "pointer" },
  tabOn: { background: "#16243a", color: "#f8fafc", borderColor: "#2b3a52" },
  panel: { background: "#0d1320", border: "1px solid #1e293b", borderRadius: 12, padding: 8 },
  table: { width: "100%", borderCollapse: "collapse" },
  thr: { borderBottom: "1px solid #1e293b" },
  th: { textAlign: "left", fontSize: 10.5, fontWeight: 700, letterSpacing: 0.5, textTransform: "uppercase", color: "#64748b", padding: "8px 10px" },
  tr: { cursor: "pointer", borderBottom: "1px solid #141e2e" },
  td: { padding: "9px 10px", fontSize: 13 },
  adminTag: { marginLeft: 6, fontSize: 9, fontWeight: 800, color: "#0a0e17", background: "#f59e0b", borderRadius: 6, padding: "1px 5px" },
  streamRow: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, padding: "10px", borderBottom: "1px solid #141e2e" },
  streamTitle: { fontSize: 13.5, fontWeight: 700, color: "#e2e8f0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  streamMeta: { fontSize: 11.5, color: "#94a3b8", marginTop: 2 },
  badge: { fontSize: 10, fontWeight: 800, color: "#0a0e17", borderRadius: 8, padding: "3px 9px", whiteSpace: "nowrap" },
  empty: { fontSize: 12.5, color: "#64748b", padding: 12 },

  overlay: { position: "fixed", inset: 0, background: "rgba(3,6,12,.6)", zIndex: 120, display: "flex", justifyContent: "flex-end" },
  drawer: { width: 460, maxWidth: "100%", height: "100%", background: "#0d1320", borderLeft: "1px solid #1e293b", display: "flex", flexDirection: "column", boxShadow: "-12px 0 40px rgba(0,0,0,.5)" },
  drawerHead: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", padding: "16px 18px", borderBottom: "1px solid #1e293b" },
  drawerTitle: { fontSize: 17, fontWeight: 800 },
  drawerSub: { fontSize: 12, color: "#64748b", marginTop: 2 },
  closeBtn: { width: 30, height: 30, borderRadius: 7, background: "#1e293b", border: "1px solid #334155", color: "#cbd5e1", fontSize: 13, cursor: "pointer" },
  drawerBody: { flex: 1, overflowY: "auto", padding: 16 },
  miniStats: { display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 8, marginBottom: 12 },
  mini: { background: "#0e1626", border: "1px solid #1e293b", borderRadius: 8, padding: "8px 6px", textAlign: "center" },
  miniK: { fontSize: 9.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: "#64748b" },
  miniV: { fontSize: 15, fontWeight: 800, marginTop: 2 },
  section: { fontSize: 11, fontWeight: 800, letterSpacing: 1, textTransform: "uppercase", color: "#f59e0b", margin: "14px 0 8px" },
  payForm: { display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" },
  input: { flex: 1, minWidth: 80, padding: "8px 9px", background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 7, color: "#e2e8f0", fontSize: 13, outline: "none", boxSizing: "border-box" },
  payBtn: { padding: "8px 14px", background: "linear-gradient(135deg,#f59e0b,#d97706)", border: "none", borderRadius: 7, color: "#0a0e17", fontSize: 12.5, fontWeight: 800, cursor: "pointer" },
  payRow: { display: "flex", justifyContent: "space-between", fontSize: 13, padding: "6px 0", borderBottom: "1px solid #141e2e" },
  handBlock: { borderBottom: "1px solid #141e2e" },
  handRow: { display: "flex", justifyContent: "space-between", padding: "8px 2px", fontSize: 12.5, cursor: "pointer", color: "#cbd5e1" },
  handDetail: { padding: "4px 2px 10px" },
  pre: { margin: "6px 0 0", padding: 10, background: "#06090f", color: "#a5d6a7", fontSize: 11, lineHeight: 1.5, fontFamily: "'JetBrains Mono',monospace", whiteSpace: "pre-wrap", wordBreak: "break-word", borderRadius: 6, border: "1px solid #1e293b", maxHeight: 260, overflowY: "auto" },
  link: { color: "#7dd3fc", textDecoration: "none", fontSize: 12, fontWeight: 600 },
  dim: { color: "#64748b" },
  err: { marginTop: 8, fontSize: 12.5, color: "#fca5a5", background: "#3a1416", border: "1px solid #7f1d1d", borderRadius: 8, padding: "8px 10px" },
};
