import { fmtUsd, fmtMonth } from "./api.js";

const BONUS_LABEL = { full: "Full bonus", half: "Half bonus", forfeited: "Forfeited (errors)", incomplete: "In progress" };
const BONUS_STYLE = {
  full: { background: "var(--gain-wash)", color: "var(--gain-bright)", border: "1px solid rgba(126,184,138,.4)" },
  half: { background: "var(--gold-wash)", color: "var(--gold-bright)", border: "1px solid var(--gold-dim)" },
  forfeited: { background: "var(--loss-wash)", color: "var(--loss-bright)", border: "1px solid rgba(198,120,120,.4)" },
  incomplete: { background: "transparent", color: "var(--text-dim)", border: "1px solid var(--line-strong)" },
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
      <h1 className="ui-rise" style={st.h1}>Welcome, <em style={{ color: "var(--gold)" }}>{user.username}</em></h1>

      {/* headline numbers */}
      <div style={st.cards}>
        <Stat label="Hands transcribed" value={d.hands.toLocaleString()} />
        <Stat label="Pieces entered" value={d.pieces.toLocaleString()} />
        <Stat label="Total earned" value={fmtUsd(d.total)} accent="#4ade80" />
        <Stat label="Pending payment" value={fmtUsd(d.owed)} accent="#fbbf24" />
      </div>

      {/* Your month */}
      {am ? (
        <div style={{ ...st.panel, border: "1px solid var(--line-strong)" }}>
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
            <span>Month bonus: <strong style={{ color: am.amount ? "var(--gain-bright)" : "var(--text-dim)" }}>{fmtUsd(am.amount)}</strong> of {fmtUsd(am.bonus_amount)}</span>
            <span>Your error rate: <strong style={{ color: am.error_rate < 0.10 ? "var(--gain-bright)" : am.error_rate <= 0.20 ? "var(--gold-bright)" : "var(--loss-bright)" }}>{(am.error_rate * 100).toFixed(1)}%</strong> ({am.error_count} errors)</span>
          </div>
          {!am.is_complete && <div style={st.monthNote}>Complete every stream in {fmtMonth(am.month)} to unlock the bonus (helpers count toward completion).</div>}
        </div>
      ) : (
        <div style={st.panel}><div style={st.empty}>No assigned month yet — an admin assigns your "home month".</div></div>
      )}

      {/* earnings breakdown */}
      <div style={st.panel}>
        <div style={st.panelHead}>Earnings breakdown</div>
        <Row k="Cards income" v={fmtUsd(d.cards_income)} sub={`${d.cards} cards`} />
        <Row k="Actions income" v={fmtUsd(d.actions_income)} sub={`${d.actions} actions`} />
        <Row k="Completion bonuses" v={fmtUsd(d.completion_bonus)} sub={`${d.hands} hands`} />
        <Row k="Stream bonuses" v={fmtUsd(d.stream_bonus)} sub="completed streams" accent="#fbbf24" />
        <Row k="Month bonus" v={fmtUsd(d.month_bonus)} sub={am ? `${fmtMonth(am.month)} · ${BONUS_LABEL[am.status]}` : "no month assigned"} accent="#fbbf24" />
        {d.tutorial_bonus > 0 && <Row k="Tutorial bonus" v={fmtUsd(d.tutorial_bonus)} sub="onboarding reward" accent="#fbbf24" />}
        <div style={st.divider} />
        <Row k="Base" v={fmtUsd(d.base)} />
        <Row k="Total earned" v={fmtUsd(d.total)} accent="#4ade80" bold />
        <Row k="Paid to date" v={fmtUsd(d.paid)} />
        <Row k="Still owed" v={fmtUsd(d.owed)} accent="#fbbf24" bold />
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
                    {clock(h.timestamp_seconds)} ↗
                  </a>
                ) : clock(h.timestamp_seconds)}
                <span style={st.dim}> · {h.stream_id || "—"}</span>
              </span>
              <span style={st.lineMeta}>{fmtUsd(h.earnings)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, accent }) {
  return (
    <div className="ui-card" style={st.statCard}>
      <div style={st.statLabel}>{label}</div>
      <div style={{ ...st.statValue, color: accent || "var(--text-strong)" }}>{value}</div>
    </div>
  );
}
function Row({ k, v, sub, accent, bold }) {
  return (
    <div style={st.row}>
      <span style={st.rowK}>{k}{sub ? <span style={st.dim}> · {sub}</span> : null}</span>
      <span style={{ ...st.rowV, color: accent || "var(--text)", fontWeight: bold ? 800 : 700 }}>{v}</span>
    </div>
  );
}

const st = {
  page: { flex: 1, minHeight: 0, overflowY: "auto", padding: "20px 24px 40px", color: "var(--text)", fontFamily: "var(--font-ui)", maxWidth: 1000, margin: "0 auto", width: "100%", boxSizing: "border-box" },
  h1: { fontSize: 34, fontWeight: 500, fontFamily: "var(--font-display)", margin: "6px 0 18px", color: "var(--text-strong)", letterSpacing: 0.2 },
  cards: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px,1fr))", gap: 12, marginBottom: 16 },
  statCard: { background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 2, padding: "14px 16px" },
  statLabel: { fontSize: 10.5, fontWeight: 700, letterSpacing: 1, textTransform: "uppercase", color: "var(--text-mute)" },
  statValue: { fontSize: 30, fontWeight: 500, fontFamily: "var(--font-display)", marginTop: 4, fontVariantNumeric: "tabular-nums" },
  panel: { background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 2, padding: 16, marginBottom: 16 },
  monthHead: { display: "flex", alignItems: "center", gap: 10, marginBottom: 6, flexWrap: "wrap" },
  yourMonthBadge: { fontSize: 10, fontWeight: 600, letterSpacing: 1, color: "var(--ink)", background: "var(--gold)", borderRadius: 3, padding: "3px 8px" },
  monthTitle: { fontSize: 24, fontWeight: 500, fontFamily: "var(--font-display)", color: "var(--text-strong)" },
  bonusPill: { fontSize: 10.5, fontWeight: 600, borderRadius: 3, padding: "3px 10px" },
  monthSub: { fontSize: 12.5, color: "var(--text-dim)", marginBottom: 8 },
  progressTrack: { height: 10, borderRadius: 3, background: "var(--field)", border: "1px solid var(--line)", overflow: "hidden", marginBottom: 8 },
  progressFill: { height: "100%", background: "linear-gradient(90deg,var(--gold-deep),var(--gold-bright))" },
  monthMeta: { display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8, fontSize: 12.5, color: "var(--text-2)" },
  monthNote: { fontSize: 11.5, color: "var(--text-mute)", marginTop: 8 },
  panelHead: { fontSize: 12, fontWeight: 600, letterSpacing: 1, textTransform: "uppercase", color: "var(--gold)", marginBottom: 10 },
  row: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", fontSize: 13.5 },
  rowK: { color: "var(--text-dim)" },
  rowV: { },
  divider: { height: 1, background: "var(--raised-2)", margin: "8px 0" },
  two: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px,1fr))", gap: 16 },
  lineRow: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "7px 0", borderBottom: "1px solid var(--line)", fontSize: 13, gap: 10 },
  lineTitle: { color: "var(--text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  lineMeta: { display: "flex", alignItems: "center", gap: 8, color: "var(--text-dim)", whiteSpace: "nowrap" },
  doneTag: { fontSize: 10, fontWeight: 600, color: "var(--ink)", background: "var(--gain-deep)", borderRadius: 3, padding: "1px 6px" },
  empty: { fontSize: 12.5, color: "var(--text-mute)", padding: "6px 0" },
  link: { color: "var(--steel)", textDecoration: "none", fontWeight: 600 },
  dim: { color: "var(--text-mute)" },
};
