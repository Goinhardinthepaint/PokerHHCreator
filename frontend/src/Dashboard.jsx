import { fmtMoney, fmtMonth } from "./api.js";

const BONUS_LABEL = { full: "Full bonus", half: "Half bonus", forfeited: "Forfeited (errors)", incomplete: "In progress" };
const BONUS_STYLE = {
  full: { background: "#16a34a", color: "#06210f" },
  half: { background: "#f59e0b", color: "#0a0e17" },
  forfeited: { background: "#7f1d1d", color: "#fecaca" },
  incomplete: { background: "#1e293b", color: "#cbd5e1" },
};

// Read-only worker dashboard rendered from the /api/me dashboard payload.
function clock(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const p = (x) => String(x).padStart(2, "0");
  return h > 0 ? `${h}:${p(m)}:${p(ss)}` : `${m}:${p(ss)}`;
}

export default function Dashboard({ user, dashboard: d }) {
  if (!d) return <div style={st.page}>No data.</div>;
  const am = d.assigned_month;
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

      {/* Your month */}
      {am ? (
        <div style={{ ...st.panel, border: "1px solid #2b3a52" }}>
          <div style={st.monthHead}>
            <span style={st.yourMonthBadge}>YOUR MONTH</span>
            <span style={st.monthTitle}>{fmtMonth(am.month)}</span>
            <span style={{ ...st.bonusPill, ...BONUS_STYLE[am.status] }}>{BONUS_LABEL[am.status]}</span>
          </div>
          <div style={st.monthSub}>
            {am.hands_done.toLocaleString()}/{am.hands_estimated.toLocaleString()} hands ({am.pct}%) ·
            {" "}{am.complete_streams}/{am.total_streams} streams complete · deadline {am.deadline || "—"}
          </div>
          <div style={st.progressTrack}><div style={{ ...st.progressFill, width: `${Math.min(100, am.pct)}%` }} /></div>
          <div style={st.monthMeta}>
            <span>Month bonus: <strong style={{ color: am.amount ? "#4ade80" : "#94a3b8" }}>{fmtMoney(am.amount)}</strong> of {fmtMoney(am.bonus_amount)}</span>
            <span>Your error rate: <strong style={{ color: am.error_rate < 0.10 ? "#4ade80" : am.error_rate <= 0.20 ? "#fbbf24" : "#fca5a5" }}>{(am.error_rate * 100).toFixed(1)}%</strong> ({am.error_count} errors)</span>
          </div>
          {!am.is_complete && <div style={st.monthNote}>Complete every stream in {fmtMonth(am.month)} to unlock the bonus (helpers count toward completion).</div>}
        </div>
      ) : (
        <div style={st.panel}><div style={st.empty}>No assigned month yet — an admin assigns your "home month".</div></div>
      )}

      {/* earnings breakdown */}
      <div style={st.panel}>
        <div style={st.panelHead}>Earnings breakdown</div>
        <Row k="Cards income" v={fmtMoney(d.cards_income)} sub={`${d.cards} cards`} />
        <Row k="Actions income" v={fmtMoney(d.actions_income)} sub={`${d.actions} actions`} />
        <Row k="Completion bonuses" v={fmtMoney(d.completion_bonus)} sub={`${d.hands} hands`} />
        <Row k="Stream bonuses" v={fmtMoney(d.stream_bonus)} sub="completed streams" accent="#fbbf24" />
        <Row k="Month bonus" v={fmtMoney(d.month_bonus)} sub={am ? `${fmtMonth(am.month)} · ${BONUS_LABEL[am.status]}` : "no month assigned"} accent="#fbbf24" />
        {d.tutorial_bonus > 0 && <Row k="Tutorial bonus" v={fmtMoney(d.tutorial_bonus)} sub="onboarding reward" accent="#fbbf24" />}
        <div style={st.divider} />
        <Row k="Base" v={fmtMoney(d.base)} />
        <Row k="Total earned" v={fmtMoney(d.total)} accent="#4ade80" bold />
        <Row k="Paid to date" v={fmtMoney(d.paid)} />
        <Row k="Still owed" v={fmtMoney(d.owed)} accent="#fbbf24" bold />
      </div>

      {/* own vs other-month hands */}
      <div style={st.cards}>
        <Stat label={am ? `Hands in ${fmtMonth(am.month)}` : "Hands (your month)"} value={(d.own_month_hands?.count ?? 0).toLocaleString()} />
        <Stat label="Hands on other months" value={(d.other_month_hands?.count ?? 0).toLocaleString()} accent="#7dd3fc" />
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
  monthHead: { display: "flex", alignItems: "center", gap: 10, marginBottom: 6, flexWrap: "wrap" },
  yourMonthBadge: { fontSize: 10, fontWeight: 800, letterSpacing: 1, color: "#0a0e17", background: "#f59e0b", borderRadius: 6, padding: "3px 8px" },
  monthTitle: { fontSize: 18, fontWeight: 800, color: "#f8fafc" },
  bonusPill: { fontSize: 10.5, fontWeight: 800, borderRadius: 8, padding: "3px 10px" },
  monthSub: { fontSize: 12.5, color: "#94a3b8", marginBottom: 8 },
  progressTrack: { height: 10, borderRadius: 6, background: "#0a0f1a", border: "1px solid #1e293b", overflow: "hidden", marginBottom: 8 },
  progressFill: { height: "100%", background: "linear-gradient(90deg,#16a34a,#4ade80)" },
  monthMeta: { display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8, fontSize: 12.5, color: "#cbd5e1" },
  monthNote: { fontSize: 11.5, color: "#64748b", marginTop: 8 },
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
