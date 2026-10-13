import { useState } from "react";
import { bountyAmount, SIDE_GAMES, squidGameKey } from "./sideGames.js";

// The side-game popup, shared by both UIs. Side Game opens a menu of the games
// (Squid, Progressive Squid, Bounty); picking one asks for that game's reward
// structure, then shows who holds what while it runs. The host passes its own
// look in `ui`; anything it leaves out falls back to the classic styling.

const CLASSIC = {
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,.6)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 60, padding: 16 },
  box: { width: 460, maxWidth: "94vw", maxHeight: "88vh", overflowY: "auto", boxSizing: "border-box", background: "#0e1626", border: "1px solid #334155", borderRadius: 14, padding: 22, boxShadow: "0 30px 80px rgba(0,0,0,.6)", display: "flex", flexDirection: "column", gap: 12, color: "#e2e8f0" },
  title: { fontSize: 17, fontWeight: 800, color: "#fbbf24", letterSpacing: 0.4 },
  sub: { fontSize: 12.5, color: "#94a3b8", lineHeight: 1.5 },
  head: { fontSize: 10.5, fontWeight: 800, letterSpacing: 1.5, color: "#fbbf24", marginTop: 4 },
  card: { display: "flex", flexDirection: "column", gap: 4, textAlign: "left", padding: "11px 13px", background: "#0d1320", border: "1px solid #1e293b", borderRadius: 10, color: "#e2e8f0", cursor: "pointer" },
  cardOn: { border: "1px solid #fbbf24" },
  cardTitle: { display: "flex", alignItems: "center", gap: 8, fontSize: 14, fontWeight: 800 },
  cardText: { fontSize: 12, color: "#94a3b8", lineHeight: 1.45 },
  badge: { fontSize: 9, fontWeight: 800, letterSpacing: 1, color: "#0a0e17", background: "#fbbf24", borderRadius: 6, padding: "1px 6px" },
  field: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", fontSize: 13 },
  label: { fontSize: 12.5, color: "#cbd5e1" },
  input: { width: 76, padding: "6px 8px", background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 7, color: "#e2e8f0", fontSize: 13, outline: "none" },
  preview: { fontSize: 12, color: "#cbd5e1", lineHeight: 1.55, background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 8, padding: "8px 10px" },
  hint: { fontSize: 11.5, color: "#64748b", lineHeight: 1.45 },
  warn: { fontSize: 11.5, color: "#fbbf24", lineHeight: 1.45 },
  row: { display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" },
  value: { fontSize: 12.5, color: "#e2e8f0" },
  accent: { color: "#fbbf24", fontWeight: 700 },
  gain: { color: "#4ade80", fontWeight: 700 },
  loss: { color: "#fca5a5", fontWeight: 700 },
  stepBtn: { width: 22, height: 22, padding: 0, background: "#1e293b", border: "1px solid #334155", borderRadius: 5, color: "#cbd5e1", fontSize: 13, fontWeight: 700, cursor: "pointer", lineHeight: 1 },
  away: { fontSize: 9, fontWeight: 800, color: "#0a0e17", background: "#94a3b8", borderRadius: 6, padding: "0 5px" },
  actions: { display: "flex", gap: 8, flexWrap: "wrap", marginTop: 4 },
  primary: { flex: 1, minWidth: 150, padding: "10px 14px", background: "linear-gradient(135deg,#fbbf24,#d97706)", border: "none", borderRadius: 9, color: "#0a0e17", fontSize: 13, fontWeight: 800, cursor: "pointer" },
  btn: { padding: "10px 14px", background: "#1e293b", border: "1px solid #334155", borderRadius: 9, color: "#cbd5e1", fontSize: 13, fontWeight: 700, cursor: "pointer" },
  danger: { padding: "10px 14px", background: "transparent", border: "1px solid #7f1d1d", borderRadius: 9, color: "#fca5a5", fontSize: 13, fontWeight: 700, cursor: "pointer" },
  link: { background: "none", border: "none", padding: 0, color: "#94a3b8", fontSize: 12, textDecoration: "underline", cursor: "pointer" },
  footer: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginTop: 4 },
};

const num = (v, min = 0) => Math.max(min, Math.round(Number(v) || 0));
const money = (n) => "$" + Number(n || 0).toLocaleString();

export default function SideGameDialog({
  ui = {}, view, setView, onClose, seatedNames = [],
  bountyActive, bountySchedule, bountyStreaks,
  squidActive, squidConfig, squidRound, squidLeft, squidButtons,
  lastSideGame, onStart, onSave, onEnd, onSquidButton, onNewSquidRound, onCountOnly,
}) {
  const s = { ...CLASSIC, ...ui };
  const isOn = (k) => (k === "bounty" ? !!bountyActive : !!squidActive && squidGameKey(squidConfig) === k);
  return (
    <div className="ui-fade" style={s.overlay} onClick={onClose}>
      <div className="ui-pop" style={s.box} onClick={(e) => e.stopPropagation()}>
        {view === "menu" || !SIDE_GAMES[view] ? (
          <>
            <div style={s.title}>Side game</div>
            <div style={s.sub}>Which game is being played? Next you&apos;ll set its buttons or bounties.</div>
            {Object.entries(SIDE_GAMES).map(([k, g]) => (
              <button key={k} style={{ ...s.card, ...(isOn(k) ? s.cardOn : {}) }} onClick={() => setView(k)}>
                <span style={s.cardTitle}>{g.label}{isOn(k) && <span style={s.badge}>ON</span>}</span>
                <span style={s.cardText}>{g.blurb}</span>
              </button>
            ))}
            <div style={s.footer}>
              {onCountOnly
                ? <button style={s.link} onClick={onCountOnly} title="Count hands of some other side game quickly, with no hand histories">Other game: just count hands</button>
                : <span />}
              <button style={s.btn} onClick={onClose}>Close</button>
            </div>
          </>
        ) : (
          <GameView
            key={view} s={s} game={view} isOn={isOn} setView={setView} onClose={onClose}
            seatedNames={seatedNames}
            bountyActive={bountyActive} bountySchedule={bountySchedule} bountyStreaks={bountyStreaks}
            squidActive={squidActive} squidConfig={squidConfig} squidRound={squidRound} squidLeft={squidLeft} squidButtons={squidButtons}
            lastSideGame={lastSideGame} onStart={onStart} onSave={onSave} onEnd={onEnd}
            onSquidButton={onSquidButton} onNewSquidRound={onNewSquidRound}
          />
        )}
      </div>
    </div>
  );
}

function GameView({
  s, game, isOn, setView, onClose, seatedNames,
  bountyActive, bountySchedule, bountyStreaks,
  squidActive, squidConfig, squidRound, squidLeft, squidButtons,
  lastSideGame, onStart, onSave, onEnd, onSquidButton, onNewSquidRound,
}) {
  const g = SIDE_GAMES[game];
  const isBounty = game === "bounty";
  const familyOn = isBounty ? !!bountyActive : !!squidActive; // this game, or the other squid game
  const thisOn = isOn(game);
  const seated = seatedNames.length;
  // Start from the structure last used (a new squid game defaults to one button
  // fewer than the players seated).
  const [form, setForm] = useState(() =>
    isBounty
      ? { start: bountySchedule.start, first: bountySchedule.first, step: bountySchedule.step }
      : { total: squidActive || seated < 2 ? squidConfig.total : seated - 1, value: squidConfig.value });
  const set = (k, min) => (e) => setForm((f) => ({ ...f, [k]: num(e.target.value, min) }));

  const others = Math.max(0, seated - 1);
  const holders = Object.entries(squidButtons || {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const held = holders.reduce((a, [, n]) => a + n, 0);
  const withoutButton = seatedNames.filter((nm) => !(squidButtons || {})[nm]).length;
  const streakers = Object.entries(bountyStreaks || {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);

  const end = () => {
    const what = isBounty ? "Every streak is cleared." : "Every button held is cleared.";
    if (window.confirm(`End the ${g.label}? ${what}`)) onEnd(game);
  };

  return (
    <>
      <div style={s.title}>{g.label}{thisOn && <> <span style={s.badge}>ON</span></>}</div>
      <div style={s.sub}>{g.blurb}</div>

      <div style={s.head}>{isBounty ? "BOUNTY STRUCTURE" : "BUTTONS"}</div>
      {isBounty ? (
        <>
          <div style={s.field}>
            <span style={s.label}>Bounty starts at</span>
            <input style={s.input} type="number" min={1} value={form.start} onChange={set("start", 1)} />
            <span style={s.label}>wins in a row</span>
          </div>
          <div style={s.field}>
            <span style={s.label}>First bounty $</span>
            <input style={s.input} type="number" min={0} value={form.first} onChange={set("first", 0)} />
            <span style={s.label}>then +$</span>
            <input style={s.input} type="number" min={0} value={form.step} onChange={set("step", 0)} />
            <span style={s.label}>for each further win</span>
          </div>
          <div style={s.preview}>
            {[0, 1, 2].map((i) => (
              <div key={i}>
                Win #{form.start + i} in a row: <span style={s.accent}>{money(bountyAmount(form.start + i, form))}</span> from each player dealt in
              </div>
            ))}
            <div style={s.hint}>Each win pays on its own, so a {form.start + 1}-hand streak collects {money(bountyAmount(form.start, form))} + {money(bountyAmount(form.start + 1, form))} from each player.</div>
          </div>
        </>
      ) : (
        <>
          <div style={s.field}>
            <input style={s.input} type="number" min={1} value={form.total} onChange={set("total", 1)} />
            <span style={s.label}>buttons, worth $</span>
            <input style={s.input} type="number" min={0} value={form.value} onChange={set("value", 0)} />
            <span style={s.label}>each</span>
          </div>
          <div style={s.preview}>
            When the last of the {form.total} buttons is won, every player dealt in without a button pays each holder{" "}
            <span style={s.accent}>{money(form.value)}</span> per button they hold. Holders who have left the table still get paid.
            <div style={s.hint}>
              {game === "squid"
                ? "One button each: a winner who already has one takes nothing."
                : "A player can win as many buttons as they can."}
              {" "}Run it twice: only the first board counts. A chop takes no button.
            </div>
          </div>
          {game === "squid" && seated >= 2 && form.total >= seated && (
            <div style={s.warn}>With {seated} players and one button each, {form.total} buttons would leave nobody to pay. Usually it&apos;s one fewer button than players.</div>
          )}
        </>
      )}

      <div style={s.actions}>
        {!familyOn && (
          <button style={s.primary} onClick={() => onStart(game, form)}>Start {g.label}</button>
        )}
        {familyOn && thisOn && (
          <button style={s.primary} onClick={() => onSave(game, form)}>Save changes</button>
        )}
        {familyOn && !thisOn && (
          <button style={s.primary} onClick={() => onSave(game, form)} title="Keeps every button already won">Switch to {g.label}</button>
        )}
        <button style={s.btn} onClick={() => setView("menu")}>Back</button>
        {thisOn && <button style={s.danger} onClick={end}>End game</button>}
        {!thisOn && <button style={s.btn} onClick={onClose}>Cancel</button>}
      </div>

      {thisOn && !isBounty && (
        <>
          <div style={s.head}>ROUND {squidRound}</div>
          <div style={s.value}>
            <span style={s.accent}>{squidLeft}</span> of {squidConfig.total} buttons left
            {squidLeft > 0 && withoutButton > 0 && held > 0 && (
              <span style={s.hint}> · if it ended now, {withoutButton} would pay {money(squidConfig.value * held)} each</span>
            )}
          </div>
          {holders.map(([nm, n]) => (
            <div key={nm} style={s.row}>
              <button style={s.stepBtn} title="Take a button back" onClick={() => onSquidButton(nm, -1)}>−</button>
              <span style={s.value}><span style={s.accent}>{nm}</span> {n} button{n === 1 ? "" : "s"}</span>
              <button style={s.stepBtn} title="Give a button" disabled={squidLeft <= 0} onClick={() => onSquidButton(nm, 1)}>+</button>
              {!seatedNames.includes(nm) && <span style={s.away} title="Left the table; still paid when the round settles">AWAY</span>}
            </div>
          ))}
          {!holders.length && <div style={s.hint}>No buttons won yet this round. Fix a mistake with the ± on a seat.</div>}
          <div style={s.row}>
            <button style={s.btn} onClick={onNewSquidRound} title="Clear every button and start the next round">Start new round</button>
          </div>
        </>
      )}

      {thisOn && isBounty && (
        <>
          <div style={s.head}>STREAKS</div>
          {streakers.length ? streakers.map(([nm, n]) => {
            const next = bountyAmount(n + 1, bountySchedule);
            return (
              <div key={nm} style={s.value}>
                <span style={s.accent}>{nm}</span>: {n} in a row
                {next > 0 ? <> · next win collects {money(next)} × {others}</> : <> · pays from {bountySchedule.start}</>}
              </div>
            );
          }) : <div style={s.hint}>No streaks yet. They advance after each saved hand; fix a mistake with the ± on a seat.</div>}
        </>
      )}

      {thisOn && lastSideGame && <LastHand s={s} last={lastSideGame} />}
    </>
  );
}

// What last hand's side games paid, per player. This hand's stacks already
// include it.
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
  if (last.squid?.settled) lines.push(<div key="settled" style={s.hint}>Squid round settled; a new round has started.</div>);
  if (!lines.length) lines.push(<div key="none" style={s.hint}>No side-game money changed hands.</div>);
  return (
    <>
      <div style={s.head}>LAST HAND{last.hand ? ` (#${last.hand})` : ""}</div>
      {lines}
    </>
  );
}

// Side games run on their own, but a hand that moves side-game money waits for
// a yes: a bounty being paid, or the last squid button going (the round
// settles). Yes saves the hand and pays; No goes back unsaved so the streaks
// or buttons can be fixed, and completing the hand asks again.
export function SidePayConfirm({ ui = {}, confirm, onYes, onNo }) {
  const s = { ...CLASSIC, ...ui };
  const { bounty, squid, squidValue, squidTotal } = confirm;
  const winners = bounty ? [...new Set(bounty.payouts.map((p) => p.winner))] : [];
  const taker = squid?.rows.find((r) => r.won);
  const holders = squid ? squid.rows.filter((r) => r.after > 0) : [];
  return (
    <div className="ui-fade" style={s.overlay}>
      <div className="ui-pop" style={s.box} role="dialog" aria-label="Confirm side-game payout">
        {bounty && winners.map((w) => {
          const mine = bounty.payouts.filter((p) => p.winner === w);
          return (
            <div key={w} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <div style={s.title}>Did {w} just win the bounty game?</div>
              <div style={s.sub}>
                {w} has now won <span style={s.accent}>{mine[0].streak} in a row</span>, so each player dealt in pays{" "}
                <span style={s.accent}>{money(mine[0].amount)}</span>.
              </div>
              <NetLine s={s} payouts={mine} toKey="winner" />
            </div>
          );
        })}
        {squid && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={s.title}>Has squid ended, and are all squids correct?</div>
            <div style={s.sub}>
              {taker ? <>The last of the {squidTotal} buttons just went to <span style={s.accent}>{taker.base}</span>. </> : null}
              Everyone dealt in without a button pays each holder <span style={s.accent}>{money(squidValue)}</span> per button.
            </div>
            <div style={s.head}>BUTTONS</div>
            {holders.map((h) => (
              <div key={h.base} style={s.value}>
                <span style={s.accent}>{h.base}</span> {h.after} button{h.after === 1 ? "" : "s"}
                {h.seat == null && <> <span style={s.away}>AWAY</span></>}
              </div>
            ))}
            <div style={s.head}>PAYS OUT</div>
            <NetLine s={s} payouts={squid.payouts} toKey="holder" />
          </div>
        )}
        <div style={s.actions}>
          <button style={s.primary} onClick={onYes}>Yes, pay it out</button>
          <button style={s.btn} onClick={onNo}>No, let me fix it</button>
        </div>
        <div style={s.hint}>Yes saves the hand and pays it out (recording a payout earns you $0.25). No saves nothing: fix the streaks or buttons with the ± on the seats or the side-game chip in the top bar, then complete the hand again.</div>
      </div>
    </div>
  );
}

// Each player's net from a list of payouts: "JT +$800, raver −$400, Nik −$400".
function NetLine({ s, payouts, toKey }) {
  const n = {};
  for (const p of payouts) {
    n[p[toKey]] = (n[p[toKey]] || 0) + p.amount;
    n[p.payer] = (n[p.payer] || 0) - p.amount;
  }
  return (
    <div style={s.value}>
      {Object.entries(n).sort((a, b) => b[1] - a[1]).map(([nm, v], i) => (
        <span key={nm}>{i ? ", " : ""}{nm} <span style={v >= 0 ? s.gain : s.loss}>{v >= 0 ? "+" : "−"}{money(Math.abs(v))}</span></span>
      ))}
    </div>
  );
}
