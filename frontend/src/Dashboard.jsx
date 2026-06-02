import { fmtMoney } from "./api.js";

// Read-only worker dashboard rendered from the /api/me dashboard payload.
function clock(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const p = (x) => String(x).padStart(2, "0");
  return h > 0 ? `${h}:${p(m)}:${p(ss)}` : `${m}:${p(ss)}`;
}

export default function Dashboard({ user, dashboard: d }) {
  if (!d) return <div style={st.page}>No data.</div>;
  return (
    <div style={st.page}>
      <h1 style={st.h1}>Welcome, {user.username}</h1>

      {/* headline numbers */}
      <div style={st.cards}>
        <Stat label="Hands transcribed" value={d.hands.toLocaleString()} />
        <Stat label="Pieces entered" value={d.pieces.toLocaleString()} />
        <Stat label="Total earned" value={fmtMoney(d.total)} accent="#4ade80" />
        <Stat label="Pending payment" value={fmtMoney(d.owed)} accent="#fbbf24" />
      </div>

      {/* earnings breakdown */}
      <div style={st.panel}>
        <div style={st.panelHead}>Earnings breakdown</div>
        <Row k="Cards income" v={fmtMoney(d.cards_income)} sub={`${d.cards} cards`} />
        <Row k="Actions income" v={fmtMoney(d.actions_income)} sub={`${d.actions} actions`} />
        <Row k="Completion bonuses" v={fmtMoney(d.completion_bonus)} sub={`${d.hands} hands`} />
        <Row k="Stream bonuses" v={fmtMoney(d.stream_bonus)} sub="completed streams" accent="#fbbf24" />
        <div style={st.divider} />
        <Row k="Base" v={fmtMoney(d.base)} />
        <Row k="Total earned" v={fmtMoney(d.total)} accent="#4ade80" bold />
        <Row k="Paid to date" v={fmtMoney(d.paid)} />
        <Row k="Still owed" v={fmtMoney(d.owed)} accent="#fbbf24" bold />
      </div>

      <div style={st.two}>
        {/* streams worked */}
        <div style={st.panel}>
          <div style={st.panelHead}>Streams worked ({d.streams_count})</div>
          {d.streams_worked.length === 0 && <div style={st.empty}>No streams yet.</div>}
          {d.streams_worked.map((s) => (
            <div key={s.stream_id} style={st.lineRow}>
              <span style={st.lineTitle}>{s.title || s.stream_id}</span>
              <span style={st.lineMeta}>
                {s.is_complete ? <span style={st.doneTag}>✓ complete</span> : null}
                <strong>{s.hands}</strong> hand{s.hands === 1 ? "" : "s"}
              </span>
            </div>
          ))}
        </div>

        {/* recent hands */}
        <div style={st.panel}>
          <div style={st.panelHead}>Recent hands</div>
          {d.recent_hands.length === 0 && <div style={st.empty}>No hands yet.</div>}
          {d.recent_hands.map((h) => (
            <div key={h.id} style={st.lineRow}>
              <span style={st.lineTitle}>
                {h.youtube_url ? (
                  <a style={st.link} href={`${h.youtube_url}${h.youtube_url.includes("?") ? "&" : "?"}t=${h.timestamp_seconds || 0}`} target="_blank" rel="noreferrer">
                    ⏱ {clock(h.timestamp_seconds)} ↗
                  </a>
                ) : `⏱ ${clock(h.timestamp_seconds)}`}
                <span style={st.dim}> · {h.stream_id || "—"}</span>
              </span>
              <span style={st.lineMeta}>{fmtMoney(h.earnings)}</span>
            </div>
          ))}
        </div>
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
function Row({ k, v, sub, accent, bold }) {
  return (
    <div style={st.row}>
      <span style={st.rowK}>{k}{sub ? <span style={st.dim}> · {sub}</span> : null}</span>
      <span style={{ ...st.rowV, color: accent || "#e2e8f0", fontWeight: bold ? 800 : 700 }}>{v}</span>
    </div>
  );
}

const st = {
  page: { flex: 1, minHeight: 0, overflowY: "auto", padding: "20px 24px 40px", color: "#e2e8f0", fontFamily: "'Inter','Segoe UI',system-ui,sans-serif", maxWidth: 1000, margin: "0 auto", width: "100%", boxSizing: "border-box" },
  h1: { fontSize: 22, fontWeight: 800, margin: "4px 0 16px" },
  cards: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px,1fr))", gap: 12, marginBottom: 16 },
  statCard: { background: "#0d1320", border: "1px solid #1e293b", borderRadius: 12, padding: "14px 16px" },
  statLabel: { fontSize: 10.5, fontWeight: 700, letterSpacing: 1, textTransform: "uppercase", color: "#64748b" },
  statValue: { fontSize: 24, fontWeight: 800, marginTop: 4 },
  panel: { background: "#0d1320", border: "1px solid #1e293b", borderRadius: 12, padding: 16, marginBottom: 16 },
  panelHead: { fontSize: 12, fontWeight: 800, letterSpacing: 1, textTransform: "uppercase", color: "#f59e0b", marginBottom: 10 },
  row: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", fontSize: 13.5 },
  rowK: { color: "#94a3b8" },
  rowV: { },
  divider: { height: 1, background: "#1e293b", margin: "8px 0" },
  two: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px,1fr))", gap: 16 },
  lineRow: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "7px 0", borderBottom: "1px solid #141e2e", fontSize: 13, gap: 10 },
  lineTitle: { color: "#e2e8f0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  lineMeta: { display: "flex", alignItems: "center", gap: 8, color: "#94a3b8", whiteSpace: "nowrap" },
  doneTag: { fontSize: 10, fontWeight: 800, color: "#0a0e17", background: "#16a34a", borderRadius: 8, padding: "1px 6px" },
  empty: { fontSize: 12.5, color: "#64748b", padding: "6px 0" },
  link: { color: "#7dd3fc", textDecoration: "none", fontWeight: 600 },
  dim: { color: "#64748b" },
};
