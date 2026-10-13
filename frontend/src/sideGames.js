// Bounty, squid and 7-2 side games — the pure logic, shared by both UIs.
//
// Rules (confirmed by Cole, 2026-10-12) and the hand-history notation they're
// written in: docs/SIDE_GAME_HH_FORMAT.md. Cole's database parser is built from
// that spec, so the names and the SIDE GAME block produced here must match it.
//
// Who "won the hand", for every game: the winner(s) of the MAIN pot on the FIRST
// board. Run it twice → only board one counts. Several main-pot winners → a chop.
// A player who wins only a side pot did not win the hand.

export const DEFAULT_BOUNTY = { start: 3, first: 300, step: 100 };
export const DEFAULT_SQUID = { total: 7, value: 200, mode: "progressive" };
export const DEFAULT_SEVEN_TWO = { value: 200 };
// What the worker earns for recording a side-game payout: a bounty paid, a
// squid round settling, or a 7-2 paid (the server pays it, from the hand's
// SIDE GAME data).
export const SIDE_PAYOUT_BONUS = 0.25;

// ── Display names ────────────────────────────────────────────────────────────
// State ENTERING the hand rides in the name: JT3bounty, raver2squid, JT3bounty1squid.
export const displayName = (base, streak = 0, buttons = 0) =>
  base + (streak > 0 ? `${streak}bounty` : "") + (buttons > 0 ? `${buttons}squid` : "");

// Rewrite every player reference in a buildHandDict() result. These are the only
// name-bearing fields pt4_formatter reads. Returns a new dict — the roster keeps
// base names, so a suffix can never compound ("raver2bounty3bounty").
export function applySideGameNames(hand, streaks = {}, buttons = {}) {
  const rn = (nm) => (typeof nm === "string" ? displayName(nm, streaks[nm] || 0, buttons[nm] || 0) : nm);
  const out = { ...hand };
  out.players = (hand.players || []).map((p) => ({ ...p, name: rn(p.name) }));
  out.action = Object.fromEntries(
    Object.entries(hand.action || {}).map(([street, acts]) => [street, (acts || []).map((a) => ({ ...a, player: rn(a.player) }))]),
  );
  out.showdown = (hand.showdown || []).map((s) => ({ ...s, player: rn(s.player) }));
  if (hand.winner) out.winner = rn(hand.winner);
  if (hand.pots) out.pots = hand.pots.map((p) => ({ ...p, winners: (p.winners || []).map(rn) }));
  return out;
}

// ── Who won the hand ─────────────────────────────────────────────────────────
export function mainPotWinners(hand) {
  if (hand.winner) return [hand.winner]; // everyone else folded
  const sd = hand.showdown || [];
  if ((hand.num_runs || 0) >= 2 || sd.some((s) => s.run)) {
    return sd.filter((s) => (s.run || 1) === 1 && s.result === "wins").map((s) => s.player);
  }
  if (hand.pots && hand.pots.length) return [...(hand.pots[0].winners || [])];
  return sd.filter((s) => s.result === "wins").map((s) => s.player);
}

// ── Bounty ───────────────────────────────────────────────────────────────────
// The bounty for reaching a streak: `first` at `start` wins in a row, `step`
// more for each win after that. Bounties stack: every qualifying win pays.
export function bountyAmount(streak, sched = DEFAULT_BOUNTY) {
  if (streak < sched.start) return 0;
  return sched.first + (streak - sched.start) * sched.step;
}

// The first few levels, for the schedule line: {3: 300, 4: 400, 5: 500}.
export function bountyLevels(sched = DEFAULT_BOUNTY, count = 3) {
  const out = {};
  for (let i = 0; i < count; i++) out[sched.start + i] = bountyAmount(sched.start + i, sched);
  return out;
}

// Settle one hand. dealtIn: [{seat, name}] for everyone dealt in, by seat.
// A win or a chop extends the streak; only an outright win pays, and every other
// player dealt in pays it. Players not dealt in keep their streak untouched.
export function settleBounty({ dealtIn, streaks = {}, schedule = DEFAULT_BOUNTY, winners }) {
  const outright = winners.length === 1;
  const rows = dealtIn.map((p) => {
    const before = streaks[p.name] || 0;
    const result = winners.includes(p.name) ? (outright ? "won" : "chopped") : "lost";
    return { seat: p.seat, base: p.name, before, after: result === "lost" ? 0 : before + 1, result };
  });
  const payouts = [];
  for (const w of rows) {
    if (w.result !== "won") continue;
    const amount = bountyAmount(w.after, schedule);
    if (amount <= 0) continue;
    for (const o of rows) {
      if (o !== w) payouts.push({ payerSeat: o.seat, payer: o.base, winnerSeat: w.seat, winner: w.base, amount, streak: w.after });
    }
  }
  const nextStreaks = {};
  const dealtNames = new Set(dealtIn.map((p) => p.name));
  for (const [nm, n] of Object.entries(streaks)) if (n > 0 && !dealtNames.has(nm)) nextStreaks[nm] = n;
  for (const r of rows) if (r.after > 0) nextStreaks[r.base] = r.after;
  return { rows, payouts, nextStreaks };
}

// ── Squid ────────────────────────────────────────────────────────────────────
// Settle one hand. buttons: {name: count} for every holder, seated or not.
// The sole main-pot winner takes a button while any remain (one-per-player: only
// if they hold none). A tie takes nothing. When the last button goes, every
// player dealt in who holds none pays every holder value × buttons held —
// including holders who've left the table. Then a new round starts.
export function settleSquid({ dealtIn, buttons = {}, config = DEFAULT_SQUID, round = 1, left, winners }) {
  const held = (nm) => buttons[nm] || 0;
  const leftBefore = left ?? config.total;
  const sole = winners.length === 1 ? winners[0] : null;
  const taker = sole && leftBefore > 0 && (config.mode === "progressive" || held(sole) === 0) ? sole : null;
  const after = { ...buttons };
  if (taker) after[taker] = held(taker) + 1;
  const leftAfter = leftBefore - (taker ? 1 : 0);

  const dealtNames = new Set(dealtIn.map((p) => p.name));
  const seated = dealtIn.map((p) => ({ seat: p.seat, base: p.name, before: held(p.name), after: after[p.name] || 0, won: p.name === taker }));
  const away = Object.keys(buttons)
    .filter((nm) => !dealtNames.has(nm) && held(nm) > 0)
    .sort((a, b) => a.localeCompare(b))
    .map((nm) => ({ seat: null, base: nm, before: held(nm), after: held(nm), won: false }));
  const rows = [...seated, ...away];

  const settled = leftBefore > 0 && leftAfter === 0;
  const payouts = [];
  if (settled) {
    const payers = seated.filter((r) => r.after === 0);
    const holders = rows.filter((r) => r.after > 0);
    for (const p of payers) {
      for (const h of holders) {
        payouts.push({ payerSeat: p.seat, payer: p.base, holderSeat: h.seat, holder: h.base, amount: config.value * h.after, buttons: h.after });
      }
    }
  }
  const nextButtons = {};
  if (!settled) for (const [nm, n] of Object.entries(after)) if (n > 0) nextButtons[nm] = n;
  return {
    rows, leftBefore, leftAfter, settled, payouts,
    next: settled ? { buttons: {}, round: round + 1, left: config.total } : { buttons: nextButtons, round, left: leftAfter },
  };
}

// ── Money moving between hands ───────────────────────────────────────────────
// Net chips per player from this hand's side-game payouts. The next hand's
// starting stacks include them.
export function sideGameDeltas(...payoutLists) {
  const d = {};
  for (const list of payoutLists) {
    for (const p of list || []) {
      const to = p.winner ?? p.holder;
      d[p.payer] = (d[p.payer] || 0) - p.amount;
      d[to] = (d[to] || 0) + p.amount;
    }
  }
  return d;
}

// ── What the server renders into the *** SIDE GAME *** block ─────────────────
// ── 7-2 ──────────────────────────────────────────────────────────────────────
// A hole-card pair is "7-2" if its ranks are a 7 and a 2, suited or not.
export function is72(cards) {
  if (!cards || cards.length < 2 || !cards[0] || !cards[1]) return false;
  const ranks = [cards[0][0], cards[1][0]];
  return ranks.includes("7") && ranks.includes("2");
}

// Settle one hand. holeCards: {seat: [c1, c2]}. Returns null when nobody dealt
// in holds 7-2 (the hand then carries no 7-2 lines). The sole winner of the
// hand holding 7-2 collects `value` from every other player dealt in; a chop
// pays nothing.
export function settleSevenTwo({ dealtIn, holeCards = {}, value = DEFAULT_SEVEN_TWO.value, winners }) {
  const holders = dealtIn.filter((p) => is72(holeCards[p.seat]));
  if (!holders.length) return null;
  const outright = winners.length === 1;
  const rows = holders.map((p) => ({
    seat: p.seat, base: p.name, cards: holeCards[p.seat].slice(0, 2),
    result: winners.includes(p.name) ? (outright ? "won" : "chopped") : "lost",
  }));
  const payouts = [];
  for (const w of rows) {
    if (w.result !== "won" || !(value > 0)) continue;
    for (const o of dealtIn) {
      if (o.seat !== w.seat) payouts.push({ payerSeat: o.seat, payer: o.name, winnerSeat: w.seat, winner: w.base, amount: value });
    }
  }
  return { rows, payouts };
}

export function sideGamePayload({ bounty, bountySchedule, squid, squidConfig, squidRound, sevenTwo, sevenTwoValue }) {
  const out = {};
  if (bounty) {
    out.bounty = {
      levels: bountyLevels(bountySchedule),
      step: bountySchedule.step,
      rows: bounty.rows,
      payouts: bounty.payouts.map((p) => ({ payer_seat: p.payerSeat, payer: p.payer, winner_seat: p.winnerSeat, winner: p.winner, amount: p.amount, streak: p.streak })),
    };
  }
  if (squid) {
    out.squid = {
      round: squidRound, total: squidConfig.total, value: squidConfig.value, mode: squidConfig.mode,
      left_before: squid.leftBefore, left_after: squid.leftAfter,
      rows: squid.rows, settled: squid.settled,
      payouts: squid.payouts.map((p) => ({ payer_seat: p.payerSeat, payer: p.payer, holder_seat: p.holderSeat, holder: p.holder, amount: p.amount, buttons: p.buttons })),
    };
  }
  if (sevenTwo) {
    out.seven_two = {
      value: sevenTwoValue,
      rows: sevenTwo.rows,
      payouts: sevenTwo.payouts.map((p) => ({ payer_seat: p.payerSeat, payer: p.payer, winner_seat: p.winnerSeat, winner: p.winner, amount: p.amount })),
    };
  }
  return out;
}

// The side games the builder offers (the Side Game popup lists them in this
// order). The two squid games share one state; `mode` tells them apart.
export const SIDE_GAMES = {
  squid: {
    label: "Squid Game", short: "Squid", mode: "one-per-player",
    blurb: "One button per player. The sole winner of a hand takes a button if they don't have one yet. When the last button goes, everyone without one pays each holder.",
  },
  progressive: {
    label: "Progressive Squid Game", short: "Progressive Squid", mode: "progressive",
    blurb: "Players can win several buttons. The sole winner of a hand takes a button. When the last goes, everyone without one pays each holder for every button they hold.",
  },
  bounty: {
    label: "Bounty Game", short: "Bounty",
    blurb: "Win enough hands in a row and everyone dealt in pays you, more for each further win. A chop extends the streak but pays nothing.",
  },
  sevenTwo: {
    label: "7-2 Game", short: "7-2",
    blurb: "Win a hand holding 7-2 (any suits) and every other player dealt in pays you. A chop pays nothing.",
  },
};

// Which squid game a squid config is.
export const squidGameKey = (cfg) => (cfg?.mode === "one-per-player" ? "squid" : "progressive");
