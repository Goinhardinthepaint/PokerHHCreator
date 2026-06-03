// ─────────────────────────────────────────────────────────────────────────────
// Texas Hold'em betting engine — pure functions over a serializable state object.
// Drives the action bar: whose turn, legal actions, applying actions, streets.
// Produces a `hand` dict compatible with the backend /api/format (format_hand).
// ─────────────────────────────────────────────────────────────────────────────

import { evaluateBest, cmpScore } from "./evaluator.js";

export const STREETS = ["preflop", "flop", "turn", "river"];

const clone = (x) => JSON.parse(JSON.stringify(x));

function occupiedSorted(players) {
  return [...players].sort((a, b) => a.seat - b.seat);
}

function ringFrom(players, startSeat) {
  const sorted = occupiedSorted(players);
  const n = sorted.length;
  let idx = sorted.findIndex((p) => p.seat === startSeat);
  if (idx < 0) idx = 0;
  const out = [];
  for (let k = 0; k < n; k++) out.push(sorted[(idx + k) % n]);
  return out;
}

function nextOccupiedSeat(players, afterSeat) {
  const sorted = occupiedSorted(players);
  const n = sorted.length;
  let idx = sorted.findIndex((p) => p.seat === afterSeat);
  if (idx < 0) idx = 0;
  return sorted[(idx + 1) % n].seat;
}

// ── Blind seats + position labels ───────────────────────────────────────────
export function blindSeats(players, buttonSeat) {
  const sorted = occupiedSorted(players);
  const n = sorted.length;
  let bi = sorted.findIndex((p) => p.seat === Number(buttonSeat));
  if (bi < 0) bi = 0;
  const at = (i) => sorted[((i % n) + n) % n];
  if (n === 2) return { btn: at(bi).seat, sb: at(bi).seat, bb: at(bi + 1).seat };
  return { btn: at(bi).seat, sb: at(bi + 1).seat, bb: at(bi + 2).seat };
}

// UTG-style straddles: the `count` seats clockwise after the BB (UTG, UTG+1, …),
// each straddle doubling (2bb, 4bb, 8bb…). Capped so it never reaches the blinds.
export function utgStraddles(players, buttonSeat, bb, count) {
  const sorted = occupiedSorted(players);
  const n = sorted.length;
  if (count <= 0 || n < 3) return [];
  const { bb: bbSeat } = blindSeats(players, buttonSeat);
  const idx = sorted.findIndex((p) => p.seat === bbSeat);
  if (idx < 0) return [];
  const max = Math.min(count, n - 2); // UTG … BTN, never SB/BB
  const out = [];
  for (let k = 1; k <= max; k++) {
    out.push({ seat: sorted[(idx + k) % n].seat, amount: bb * Math.pow(2, k) });
  }
  return out;
}

// Standard middle positions (between BB and BTN) by table size, ordered from
// first-to-act (UTG) to last before the button (CO). 2- and 3-handed have none.
const MIDDLE_BY_COUNT = {
  4: ["UTG"],
  5: ["UTG", "CO"],
  6: ["UTG", "HJ", "CO"],
  7: ["UTG", "LJ", "HJ", "CO"],
  8: ["UTG", "UTG+1", "LJ", "HJ", "CO"],
  9: ["UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO"],
};

// Positions go clockwise from the button: BTN, SB, BB, then UTG…CO.
export function positionLabels(players, buttonSeat) {
  const sorted = occupiedSorted(players);
  const n = sorted.length;
  const map = {};
  if (n === 0) return map;
  let bi = sorted.findIndex((p) => p.seat === Number(buttonSeat));
  if (bi < 0) bi = 0;
  const at = (i) => sorted[((i % n) + n) % n];
  if (n === 1) {
    map[at(bi).seat] = "BTN";
    return map;
  }
  if (n === 2) {
    map[at(bi).seat] = "BTN/SB";
    map[at(bi + 1).seat] = "BB";
    return map;
  }
  map[at(bi).seat] = "BTN";
  map[at(bi + 1).seat] = "SB";
  map[at(bi + 2).seat] = "BB";
  const mids = MIDDLE_BY_COUNT[n] || [];
  for (let k = 0; k < n - 3; k++) {
    map[at(bi + 3 + k).seat] = mids[k] || `UTG+${k}`; // fallback only if n > 9
  }
  return map;
}

// ── Initialise a hand: post blinds/ante/straddle, set first actor ───────────
export function initHand({ players, buttonSeat, sb, bb, ante, straddleSeat, straddleAmount, straddles, buyButton }) {
  const { sb: sbSeat, bb: bbSeat } = blindSeats(players, buttonSeat);
  const ps = occupiedSorted(players).map((p) => ({
    seat: p.seat,
    name: p.name,
    startStack: p.stack,
    stack: p.stack,
    committedStreet: 0,
    committedTotal: 0,
    dead: 0, // dead money (antes) — in the pot but excluded from side-pot layering
    folded: false,
    allIn: false,
    acted: false,
  }));
  const find = (seat) => ps.find((p) => p.seat === seat);
  const preflop = [];
  const post = (p, action, amt, live) => {
    const a = Math.min(amt, p.stack);
    if (a <= 0) return;
    p.stack -= a;
    p.committedTotal += a;
    if (live) p.committedStreet = a; // live blinds count toward the street
    else p.dead += a; // antes are dead money
    preflop.push({ player: p.name, action, amount: a });
  };

  let currentBet = bb;
  let refSeat = bbSeat; // last "blind"/straddle seat — first voluntary actor sits to its left
  let buttonFinal = Number(buttonSeat);

  if (buyButton && find(buyButton.seat)) {
    // A player buys the button (live BB) or the straddle (live 2× BB). All other
    // forced money (SB, BB ante, and for STR the BB too) is combined into one
    // dead ante posted by the buyer. Action starts to the buyer's left and the
    // buyer is the button (acts last).
    const buyer = find(buyButton.seat);
    const isStr = buyButton.type === "str";
    const live = isStr ? 2 * bb : bb;
    const dead = (Number(ante) || 0) + sb + (isStr ? bb : 0);
    post(buyer, "posts_ante", dead, false);
    post(buyer, isStr ? "posts_straddle" : "posts_bb", live, true);
    currentBet = buyer.committedStreet;
    refSeat = buyButton.seat;
    buttonFinal = buyButton.seat;
  } else {
    // Big Blind Ante: the BB posts the entire ante for the table (exact — never
    // split per-player, which caused rounding that skewed the pot by ±1–2).
    if (ante > 0) post(find(bbSeat), "posts_ante", ante, false);
    post(find(sbSeat), "posts_sb", sb, true);
    post(find(bbSeat), "posts_bb", bb, true);

    // Straddles post in order (UTG, then UTG+1, …); each raises the facing bet
    // and pushes first action one seat further. Falls back to single-straddle args.
    const straddleList =
      straddles && straddles.length
        ? straddles
        : straddleSeat && straddleAmount > 0
        ? [{ seat: straddleSeat, amount: straddleAmount }]
        : [];
    for (const str of straddleList) {
      const st = find(str.seat);
      if (!st) continue;
      const before = st.committedStreet;
      post(st, "posts_straddle", str.amount, true);
      if (st.committedStreet > before && st.committedStreet > currentBet) currentBet = st.committedStreet;
      refSeat = str.seat;
    }
  }

  ps.forEach((p) => {
    if (p.stack === 0 && p.committedTotal > 0) p.allIn = true;
  });

  const state = {
    players: ps,
    buttonSeat: buttonFinal,
    sbSeat,
    bbSeat,
    sb,
    bb,
    street: "preflop",
    currentBet,
    lastRaiseSize: bb,
    actionsByStreet: { preflop, flop: [], turn: [], river: [] },
    actorSeat: null,
    handOver: false,
    bettingClosed: false,
    streetComplete: false,
    uncalled: null, // {seat, amount} captured when betting concludes
  };
  return settleActor(state, nextOccupiedSeat(players, refSeat));
}

// ── Who still needs to act on the current street ────────────────────────────
function needsToAct(p, currentBet) {
  return !p.folded && !p.allIn && !(p.acted && p.committedStreet === currentBet);
}

function nextActorSeat(state, lastSeat) {
  const ring = ringFrom(state.players, lastSeat); // [lastSeat, ...]
  for (let i = 1; i <= ring.length; i++) {
    const p = ring[i % ring.length];
    if (needsToAct(p, state.currentBet)) return p.seat;
  }
  return null;
}

// Set the actor to `candidate` if it can act, else the next one; if nobody can
// act, close the street / hand.
function settleActor(state, candidate) {
  // If everyone but one has folded, the hand is over — no option to give out.
  if (state.players.filter((x) => !x.folded).length <= 1) return closeStreet(state);
  const p = state.players.find((x) => x.seat === candidate);
  if (p && needsToAct(p, state.currentBet)) {
    state.actorSeat = candidate;
    return state;
  }
  const ns = nextActorSeat(state, candidate);
  if (ns === null) return closeStreet(state);
  state.actorSeat = ns;
  return state;
}

function closeStreet(state) {
  state.actorSeat = null;
  // The moment betting concludes, snapshot any uncalled bet (the lone top
  // committer of the final street) before later street advances reset commits.
  const captureUncalled = () => {
    const commits = state.players
      .map((p) => ({ seat: p.seat, c: p.committedStreet }))
      .sort((a, b) => b.c - a.c);
    const top = commits[0];
    const second = commits[1] ? commits[1].c : 0;
    if (top && top.c > second) state.uncalled = { seat: top.seat, amount: top.c - second };
  };
  const active = state.players.filter((p) => !p.folded);
  if (active.length <= 1) {
    if (!state.handOver) captureUncalled();
    state.handOver = true;
    state.streetComplete = false;
    return state;
  }
  const canStillAct = active.filter((p) => !p.allIn);
  if (canStillAct.length <= 1) {
    // No further betting possible on any street — just run the board out.
    if (!state.bettingClosed) captureUncalled();
    state.bettingClosed = true;
    state.streetComplete = false;
    return state;
  }
  state.streetComplete = true;
  return state;
}

// ── Legal actions for the current actor ─────────────────────────────────────
export function legalActions(state) {
  if (!state || state.actorSeat == null) return null;
  const p = state.players.find((x) => x.seat === state.actorSeat);
  if (!p) return null;
  const toCall = Math.max(0, state.currentBet - p.committedStreet);
  const maxTo = p.committedStreet + p.stack; // all-in "to" total
  const minRaiseTo =
    state.currentBet > 0
      ? state.currentBet + Math.max(state.lastRaiseSize, state.bb)
      : state.bb;
  return {
    player: p,
    toCall,
    callAmount: Math.min(toCall, p.stack),
    callIsAllin: toCall >= p.stack,
    canCheck: toCall === 0,
    canCall: toCall > 0,
    canBet: state.currentBet === 0 && p.stack > 0,
    canRaise: state.currentBet > 0 && p.stack > toCall,
    minRaiseTo: Math.min(minRaiseTo, maxTo),
    maxTo,
    facing: state.currentBet,
  };
}

// ── Apply an action and advance ─────────────────────────────────────────────
// act = { type: 'fold'|'check'|'call'|'bet'|'raise'|'allin', amount?: number }
// amount for bet/raise is the TARGET total commitment on this street ("to").
export function applyAction(state, act) {
  state = clone(state);
  const p = state.players.find((x) => x.seat === state.actorSeat);
  if (!p) return state;
  const log = state.actionsByStreet[state.street];
  const toCall = Math.max(0, state.currentBet - p.committedStreet);

  if (act.type === "fold") {
    p.folded = true;
    p.acted = true;
    log.push({ player: p.name, action: "folds" });
  } else if (act.type === "check") {
    p.acted = true;
    log.push({ player: p.name, action: "checks" });
  } else if (act.type === "call") {
    const inc = Math.min(toCall, p.stack);
    p.stack -= inc;
    p.committedStreet += inc;
    p.committedTotal += inc;
    p.acted = true;
    if (p.stack === 0) {
      p.allIn = true;
      log.push({ player: p.name, action: "calls_allin", amount: inc });
    } else {
      log.push({ player: p.name, action: "calls" });
    }
  } else {
    // bet / raise / allin — amount is the "to" total for the street
    const wasBet = state.currentBet === 0;
    const maxTo = p.committedStreet + p.stack;
    let to = act.type === "allin" ? maxTo : Math.min(act.amount || 0, maxTo);
    if (to <= p.committedStreet) to = maxTo; // guard against degenerate input
    const inc = to - p.committedStreet;
    p.stack -= inc;
    p.committedTotal += inc;
    p.committedStreet = to;
    p.acted = true;
    if (to > state.currentBet) {
      state.lastRaiseSize = to - state.currentBet;
      state.currentBet = to;
    }
    if (p.stack === 0) {
      p.allIn = true;
      log.push({ player: p.name, action: wasBet ? "bets_allin" : "all_in", amount: wasBet ? inc : to });
    } else {
      log.push({ player: p.name, action: wasBet ? "bets" : "raises", amount: wasBet ? inc : to });
    }
  }

  return settleActor(state, state.actorSeat);
}

// ── Move to the next street (call after the board cards are set) ─────────────
export function advanceStreet(state) {
  state = clone(state);
  const idx = STREETS.indexOf(state.street);
  if (idx >= STREETS.length - 1) return state;
  state.street = STREETS[idx + 1];
  state.currentBet = 0;
  state.lastRaiseSize = state.bb;
  state.streetComplete = false;
  state.players.forEach((p) => {
    p.committedStreet = 0;
    if (!p.folded && !p.allIn) p.acted = false;
  });
  return settleActor(state, nextOccupiedSeat(state.players, state.buttonSeat));
}

export function potTotal(state) {
  if (!state) return 0;
  return state.players.reduce((s, p) => s + p.committedTotal, 0);
}

export function survivors(state) {
  return state.players.filter((p) => !p.folded).map((p) => p.name);
}

// Build layered pots from each player's total contribution. Folded players' chips
// are dead money that still fills the pots they reached. Consecutive layers with
// the same set of eligible (non-folded) players are merged — so a folded player's
// partial contribution doesn't spawn a phantom side pot. Returns [{amount,
// eligible:[seats]}] with index 0 = main pot.
export function computeSidePots(state) {
  // Layer pots from LIVE contributions only (committed minus dead-money antes,
  // minus any uncalled bet). Dead money never creates a side pot — it all drops
  // into the main pot, contested by everyone still in.
  const live = {};
  let totalDead = 0;
  state.players.forEach((p) => {
    live[p.seat] = p.committedTotal - (p.dead || 0);
    totalDead += p.dead || 0;
  });
  if (state.uncalled && live[state.uncalled.seat] != null) {
    live[state.uncalled.seat] -= state.uncalled.amount; // returned, not in any pot
  }
  const nonfolded = new Set(state.players.filter((p) => !p.folded).map((p) => p.seat));
  const seats = Object.keys(live).map(Number);
  const levels = [...new Set(seats.map((s) => live[s]).filter((a) => a > 0))].sort((a, b) => a - b);

  const raw = [];
  let prev = 0;
  for (const lvl of levels) {
    let amount = 0;
    for (const s of seats) if (live[s] >= lvl) amount += lvl - prev;
    const eligible = seats.filter((s) => live[s] >= lvl && nonfolded.has(s)).sort((a, b) => a - b);
    if (amount > 0) raw.push({ amount, eligible });
    prev = lvl;
  }

  // Merge consecutive layers with identical eligibility.
  const merged = [];
  const sameSet = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
  for (const pot of raw) {
    const last = merged[merged.length - 1];
    if (last && sameSet(last.eligible, pot.eligible)) last.amount += pot.amount;
    else merged.push({ amount: pot.amount, eligible: [...pot.eligible] });
  }

  // Dead money (antes) joins the main pot, contested by everyone still in.
  if (totalDead > 0) {
    if (merged.length) merged[0].amount += totalDead;
    else merged.push({ amount: totalDead, eligible: [...nonfolded].sort((a, b) => a - b) });
  }
  return merged;
}

// Resolve side-pot winners by hand strength. Returns null when there is no side
// pot (single pot), the board isn't complete, or an eligible player is missing
// hole cards — callers then fall back to single-winner handling.
// Returns { pots:[{amount,type,winners:[names],winnerSeats:[seats]}], winningsBySeat }.
export function resolveSidePots(state, holeCards, board) {
  const pots = computeSidePots(state);
  if (pots.length < 2) return null;
  const fullBoard = (board || []).filter(Boolean);
  if (fullBoard.length < 5) return null;
  const nameOf = (seat) => (state.players.find((p) => p.seat === seat) || {}).name;

  const out = [];
  const winningsBySeat = {};
  for (let i = 0; i < pots.length; i++) {
    const pot = pots[i];
    let best = null;
    let winnerSeats = [];
    for (const seat of pot.eligible) {
      const hc = (holeCards[seat] || []).filter(Boolean);
      if (hc.length < 2) return null; // can't resolve this pot
      const score = evaluateBest(hc, fullBoard);
      if (!score) return null;
      const c = best ? cmpScore(score, best) : 1;
      if (c > 0) { best = score; winnerSeats = [seat]; }
      else if (c === 0) winnerSeats.push(seat);
    }
    if (!winnerSeats.length) return null;
    const share = Math.floor(pot.amount / winnerSeats.length);
    const rem = pot.amount - share * winnerSeats.length;
    winnerSeats.forEach((seat, j) => {
      winningsBySeat[seat] = (winningsBySeat[seat] || 0) + share + (j === 0 ? rem : 0);
    });
    out.push({ amount: pot.amount, type: i === 0 ? "main" : "side", winners: winnerSeats.map(nameOf), winnerSeats });
  }
  return { pots: out, winningsBySeat };
}

// Ending stacks after the hand resolves: chips not committed + uncalled returned
// + pot awarded to the winner(s). Returns { seat: endingStack }. Matches the
// backend's pot/uncalled accounting so carried-over stacks stay consistent.
export function computeEndStacks(state, { numRuns = 1, runWinners = [], winner, winners, holeCards, board }) {
  const end = {};
  state.players.forEach((p) => { end[p.seat] = p.stack; });
  const seatOf = (nm) => (state.players.find((p) => p.name === nm) || {}).seat;
  const multi = numRuns >= 2;

  let U = 0;
  if (state.uncalled && end[state.uncalled.seat] != null) {
    U = state.uncalled.amount;
    end[state.uncalled.seat] += U;
  }

  // Multiway all-in side pots: distribute each pot to its evaluated winner(s).
  // (Run-it-multiple-times splits the single pot across runs instead.)
  if (!multi) {
    const sp = resolveSidePots(state, holeCards || {}, board || []);
    if (sp) {
      for (const seat of Object.keys(sp.winningsBySeat)) end[seat] += sp.winningsBySeat[seat];
      return end;
    }
  }

  const grossPot = state.players.reduce((s, p) => s + p.committedTotal, 0);
  const truePot = grossPot - U;

  const surv = state.players.filter((p) => !p.folded);
  if (surv.length === 1) {
    end[surv[0].seat] += truePot;
  } else if (surv.length >= 2) {
    if (multi) {
      // Pot split equally across runs; each run's winner collects pot/N (odd
      // chips to the first run). A player who wins multiple runs sums them.
      const share = Math.floor(truePot / numRuns);
      const rem = truePot - share * numRuns;
      for (let i = 0; i < numRuns; i++) {
        const seat = seatOf(runWinners[i] || surv[0].name);
        if (seat != null) end[seat] += share + (i === 0 ? rem : 0);
      }
    } else {
      // Chopped pot: split equally among all tied winners (odd chip to first).
      const ws = winners && winners.length ? winners : [winner || surv[0].name];
      const share = Math.floor(truePot / ws.length);
      const rem = truePot - share * ws.length;
      ws.forEach((nm, i) => {
        const seat = seatOf(nm);
        if (seat != null) end[seat] += share + (i === 0 ? rem : 0);
      });
    }
  }
  return end;
}

// ── Build the hand dict for the backend formatter ───────────────────────────
export function buildHandDict(state, { stakes, holeCards, board, runBoards = [], runWinners = [], numRuns = 1, allInStreetIdx = 0, winner, winners, positions }) {
  const players = occupiedSorted(state.players).map((p) => {
    const cards = (holeCards[p.seat] || []).filter(Boolean);
    const posRaw = positions[p.seat] || "";
    const position = posRaw === "BTN/SB" ? "SB" : ["BTN", "SB", "BB"].includes(posRaw) ? posRaw : "";
    return {
      name: p.name,
      seat: p.seat,
      stack: p.startStack,
      position,
      ...(cards.length === 2 ? { hole_cards: cards } : {}),
    };
  });

  const cleanRun = (arr) => {
    const flop = (arr.slice(0, 3) || []).filter(Boolean);
    return { flop: flop.length === 3 ? flop : [], turn: arr[3] || null, river: arr[4] || null };
  };
  const multi = numRuns >= 2;

  let boardOut;
  if (multi) {
    // Cards dealt BEFORE the all-in are shared across runs (shown once); each run
    // carries only the streets dealt after the all-in (FIRST/SECOND/… markers).
    const runs = (runBoards.length ? runBoards : [board]).slice(0, numRuns);
    const sharedFlop = allInStreetIdx >= 1 ? (runs[0].slice(0, 3) || []).filter(Boolean) : [];
    const sharedTurn = allInStreetIdx >= 2 ? (runs[0][3] || null) : null;
    boardOut = {};
    if (sharedFlop.length === 3) boardOut.flop = sharedFlop;
    if (sharedTurn) boardOut.turn = sharedTurn;
    boardOut.runs = runs.map((rb) => {
      const run = {};
      if (allInStreetIdx < 1) { const f = (rb.slice(0, 3) || []).filter(Boolean); if (f.length === 3) run.flop = f; }
      if (allInStreetIdx < 2 && rb[3]) run.turn = rb[3];
      if (allInStreetIdx < 3 && rb[4]) run.river = rb[4];
      return run;
    });
  } else {
    const r = cleanRun(board);
    boardOut = {};
    if (r.flop.length === 3) boardOut.flop = r.flop;
    if (r.turn) boardOut.turn = r.turn;
    if (r.river) boardOut.river = r.river;
  }

  const surv = survivors(state);
  let showdown = [];
  let winnerField = null;
  if (surv.length >= 2) {
    const seatOf = (nm) => state.players.find((p) => p.name === nm).seat;
    const entries = (run, winSet) =>
      surv.map((nm) => {
        const cards = (holeCards[seatOf(nm)] || []).filter(Boolean);
        return {
          player: nm,
          result: winSet.has(nm) ? "wins" : "loses",
          ...(cards.length === 2 ? { hole_cards: cards } : {}),
          ...(run ? { run } : {}),
        };
      });
    if (multi) {
      showdown = [];
      for (let i = 0; i < numRuns; i++) {
        const w = runWinners[i] || surv[0];
        showdown.push(...entries(i + 1, new Set([w])));
      }
    } else {
      // All tied winners are marked "wins" (chopped pot); else the single winner.
      const winSet = new Set(winners && winners.length ? winners : [winner || surv[0]]);
      showdown = entries(null, winSet);
    }
  } else if (surv.length === 1) {
    winnerField = surv[0];
  }

  const hand = {
    game: { stakes },
    timestamp_start: "00:00:00",
    button_seat: state.buttonSeat,
    players,
    action: state.actionsByStreet,
    board: boardOut,
    showdown,
  };
  if (multi) hand.num_runs = numRuns;
  if (winnerField) hand.winner = winnerField;

  // Multiway all-in side pots: attach explicit pots (amounts + evaluated winners)
  // for the formatter to render as main/side pots. (Not for multi-run, which
  // splits the single pot evenly across runs.)
  if (!multi) {
    const sp = resolveSidePots(state, holeCards, board);
    if (sp) hand.pots = sp.pots.map((p) => ({ amount: p.amount, type: p.type, winners: p.winners }));
  }
  return hand;
}
