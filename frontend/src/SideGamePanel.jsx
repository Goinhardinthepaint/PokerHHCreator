import { useState } from "react";
import { bountyAmount } from "./sideGames.js";

// Bounty / squid settings and live state, shown in the session sidebar while
// either game is on. Shared by both UIs: the host passes its own look in `ui`
// (keys below); anything it leaves out falls back to the classic styling.
const CLASSIC = {
  box: { background: "#0d1320", border: "1px solid #1e293b", borderRadius: 8, padding: "8px 10px", display: "flex", flexDirection: "column", gap: 7 },
  head: { fontSize: 10.5, fontWeight: 800, letterSpacing: 1.5, color: "#fbbf24" },
  row: { display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" },
  label: { fontSize: 10.5, color: "#94a3b8" },
  input: { width: 58, padding: "4px 6px", background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 6, color: "#e2e8f0", fontSize: 12, outline: "none" },
  select: { padding: "4px 6px", background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 6, color: "#e2e8f0", fontSize: 12, outline: "none" },
  hint: { fontSize: 10.5, color: "#64748b", lineHeight: 1.4 },
  value: { fontSize: 11.5, color: "#e2e8f0" },
  accent: { color: "#fbbf24", fontWeight: 700 },
  gain: { color: "#4ade80", fontWeight: 700 },
  loss: { color: "#fca5a5", fontWeight: 700 },
  stepBtn: { width: 20, height: 20, padding: 0, background: "#1e293b", border: "1px solid #334155", borderRadius: 5, color: "#cbd5e1", fontSize: 12, fontWeight: 700, cursor: "pointer", lineHeight: 1 },
  btn: { padding: "4px 10px", background: "#1e293b", border: "1px solid #334155", borderRadius: 6, color: "#cbd5e1", fontSize: 11, cursor: "pointer" },
  away: { fontSize: 9, fontWeight: 800, color: "#0a0e17", background: "#94a3b8", borderRadius: 6, padding: "0 5px" },
};

const num = (v, min = 0) => Math.max(min, Math.round(Number(v) || 0));
const money = (n) => "$" + Number(n || 0).toLocaleString();

export default function SideGamePanel({
  ui = {}, seatedNames = [],
  bountyActive, bountySchedule, setBountySchedule, bountyStreaks,
  squidActive, squidConfig, setSquidConfig, squidRound, squidLeft, squidButtons, onSquidButton, onNewSquidRound,
  lastSideGame,
}) {
  const s = { ...CLASSIC, ...ui };
  const [showLast, setShowLast] = useState(true);
  const others = Math.max(0, seatedNames.length - 1);
  const streakers = Object.entries(bountyStreaks || {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  const holders = Object.entries(squidButtons || {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const held = holders.reduce((a, [, n]) => a + n, 0);
  const withoutButton = seatedNames.filter((nm) => !(squidButtons || {})[nm]).length;

  return (
    <div style={s.box}>
      {bountyActive && (
        <>
          <div style={s.head}>BOUNTY GAME</div>
          <div style={s.row}>
            <span style={s.label}>Pays from</span>
            <input style={s.input} type="number" min={1} value={bountySchedule.start}
              onChange={(e) => setBountySchedule({ ...bountySchedule, start: num(e.target.value, 1) })} />
            <span style={s.label}>wins in a row</span>
          </div>
          <div style={s.row}>
            <span style={s.label}>First bounty $</span>
            <input style={s.input} type="number" min={0} value={bountySchedule.first}
              onChange={(e) => setBountySchedule({ ...bountySchedule, first: num(e.target.value) })} />
            <span style={s.label}>+$</span>
            <input style={s.input} type="number" min={0} value={bountySchedule.step}
              onChange={(e) => setBountySchedule({ ...bountySchedule, step: num(e.target.value) })} />
            <span style={s.label}>per extra win</span>
          </div>
          <div style={s.hint}>
            {[0, 1, 2].map((i) => `${bountySchedule.start + i} in a row ${money(bountyAmount(bountySchedule.start + i, bountySchedule))}`).join(" · ")}
            {" · "}paid by everyone dealt in, every qualifying win. A chop extends the streak but pays nothing.
          </div>
          {streakers.length ? streakers.map(([nm, n]) => {
            const next = bountyAmount(n + 1, bountySchedule);
            return (
              <div key={nm} style={s.value}>
                <span style={s.accent}>{nm}</span> — {n} in a row
                {next > 0 ? <> · next win collects {money(next)} × {others}</> : <> · pays from {bountySchedule.start}</>}
              </div>
            );
          }) : <div style={s.hint}>No streaks yet. Winners' streaks advance automatically after each hand.</div>}
        </>
      )}

      {squidActive && (
        <>
          <div style={{ ...s.head, marginTop: bountyActive ? 6 : 0 }}>SQUID GAME</div>
          <div style={s.row}>
            <input style={s.input} type="number" min={1} value={squidConfig.total}
              onChange={(e) => setSquidConfig({ ...squidConfig, total: num(e.target.value, 1) })} />
            <span style={s.label}>buttons at $</span>
            <input style={s.input} type="number" min={0} value={squidConfig.value}
              onChange={(e) => setSquidConfig({ ...squidConfig, value: num(e.target.value) })} />
            <span style={s.label}>each</span>
          </div>
          <div style={s.row}>
            <select style={s.select} value={squidConfig.mode} onChange={(e) => setSquidConfig({ ...squidConfig, mode: e.target.value })}>
              <option value="progressive">Progressive — win as many as you can</option>
              <option value="one-per-player">One button per player</option>
            </select>
          </div>
          <div style={s.value}>
            Round {squidRound} · <span style={s.accent}>{squidLeft}</span> of {squidConfig.total} buttons left
            {squidLeft > 0 && withoutButton > 0 && held > 0 && (
              <span style={s.hint}> · if it ended now, {withoutButton} would pay {money(squidConfig.value * held)} each</span>
            )}
          </div>
          {holders.map(([nm, n]) => (
            <div key={nm} style={s.row}>
              <button style={s.stepBtn} title="Take a button back" onClick={() => onSquidButton(nm, -1)}>−</button>
              <span style={s.value}><span style={s.accent}>{nm}</span> {n} button{n === 1 ? "" : "s"}</span>
              <button style={s.stepBtn} title="Give a button" disabled={squidLeft <= 0} onClick={() => onSquidButton(nm, 1)}>+</button>
              {!seatedNames.includes(nm) && <span style={s.away} title="Left the table — still paid when the round settles">AWAY</span>}
            </div>
          ))}
          {!holders.length && <div style={s.hint}>No buttons taken yet. The sole winner of each hand takes one; a chop takes none; run it twice counts board one.</div>}
          <div style={s.row}>
            <button style={s.btn} onClick={onNewSquidRound} title="Clear every button and start the next round">Start new round</button>
          </div>
        </>
      )}

      {lastSideGame && (
        <>
          <div style={{ ...s.row, justifyContent: "space-between", marginTop: 4 }}>
            <span style={s.head}>LAST HAND{lastSideGame.hand ? ` (#${lastSideGame.hand})` : ""}</span>
            <button style={s.btn} onClick={() => setShowLast((v) => !v)}>{showLast ? "hide" : "show"}</button>
          </div>
          {showLast && <LastHand s={s} last={lastSideGame} />}
        </>
      )}
    </div>
  );
}

// What last hand's side games paid, per player — the stacks for this hand
// already include it.
function LastHand({ s, last }) {
  const lines = [];
  for (const [game, payouts, toKey] of [["Bounty", last.bounty?.payouts, "winner"], ["Squid", last.squid?.payouts, "holder"]]) {
    if (!payouts || !payouts.length) continue;
    const net = {};
    for (const p of payouts) {
      net[p[toKey]] = (net[p[toKey]] || 0) + p.amount;
      net[p.payer] = (net[p.payer] || 0) - p.amount;
    }
    lines.push(
      <div key={game} style={s.value}>
        {game}:{" "}
        {Object.entries(net).sort((a, b) => b[1] - a[1]).map(([nm, v], i) => (
          <span key={nm}>{i ? ", " : ""}{nm} <span style={v >= 0 ? s.gain : s.loss}>{v >= 0 ? "+" : "−"}{money(Math.abs(v))}</span></span>
        ))}
      </div>,
    );
  }
  if (last.squid?.settled) lines.push(<div key="settled" style={s.hint}>Squid round settled — a new round has started.</div>);
  if (!lines.length) lines.push(<div key="none" style={s.hint}>No side-game money changed hands.</div>);
  return <>{lines}</>;
}
