// Shared random-hand generator for the engine fuzz tests and the end-to-end
// (engine -> Python formatter -> PT4 linter) fuzz script.
import {
  initHand, legalActions, applyAction, advanceStreet,
} from "../src/engine.js";

export function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
}
const DECK = [..."23456789TJQKA"].flatMap((r) => [..."shdc"].map((x) => r + x));

// Play one random hand to completion. Returns everything needed to check it.
export function randomHand(rand) {
  const n = 2 + Math.floor(rand() * 8);
  const seats = [...Array(9).keys()].map((i) => i + 1).sort(() => rand() - 0.5).slice(0, n).sort((a, b) => a - b);
  const players = seats.map((seat, i) => ({
    seat, name: `P${seat}`,
    stack: [30, 120, 250, 1000, 4000, 10000, 25000][Math.floor(rand() * 7)] * (1 + i % 2),
  }));
  const buttonSeat = seats[Math.floor(rand() * n)];
  const ante = rand() < 0.3 ? 100 : 0;
  let straddles = [];
  if (rand() < 0.25 && n >= 3) {
    const sorted = [...seats];
    const bbIdx = (sorted.indexOf(buttonSeat) + (n === 2 ? 1 : 2)) % n;
    straddles = [{ seat: sorted[(bbIdx + 1) % n], amount: 200 }];
  }
  let s = initHand({ players, buttonSeat, sb: 50, bb: 100, ante, straddles });
  const start = players.reduce((t, p) => t + p.stack, 0);
  const deck = [...DECK].sort(() => rand() - 0.5);
  const holeCards = Object.fromEntries(seats.map((seat) => [seat, [deck.pop(), deck.pop()]]));
  const board = [deck.pop(), deck.pop(), deck.pop(), deck.pop(), deck.pop()];

  let guard = 0;
  while (!s.handOver) {
    if (++guard > 400) throw new Error("hand did not terminate");
    if (s.actorSeat != null) {
      const L = legalActions(s);
      const r = rand();
      let a;
      if (r < 0.18) a = { type: "fold" };
      else if (r < 0.55) a = L.canCheck ? { type: "check" } : { type: "call" };
      else if (r < 0.85 && (L.canBet || L.canRaise)) {
        const span = Math.max(0, L.maxTo - L.minRaiseTo);
        a = { type: L.canBet ? "bet" : "raise", amount: L.minRaiseTo + Math.floor(rand() * rand() * span) };
      } else a = { type: "allin" };
      s = applyAction(s, a);
    } else if (s.street === "river") break;
    else s = advanceStreet(s);
  }
  return { s, start, holeCards, board, players };
}
