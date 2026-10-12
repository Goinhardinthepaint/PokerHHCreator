// Betting-engine tests. Run: npm test (node --test tests/)
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  initHand, legalActions, applyAction, advanceStreet, computeSidePots, computeEndStacks,
  buildHandDict, positionLabels, blindSeats, potTotal, survivors,
} from "../src/engine.js";

const P = (seat, name, stack = 10000) => ({ seat, name, stack });
const deal = (players, extra = {}) =>
  initHand({ players, buttonSeat: players[0].seat, sb: 50, bb: 100, ante: 0, straddles: [], ...extra });
const act = (s, type, amount) => applyAction(s, { type, amount });
const actor = (s) => s.players.find((p) => p.seat === s.actorSeat)?.name;
const lastLog = (s) => s.actionsByStreet[s.street].at(-1);
const chips = (s) => s.players.reduce((t, p) => t + p.stack + p.committedTotal, 0);

test("heads-up: button posts the small blind and acts first preflop, BB first postflop", () => {
  const ps = [P(1, "A"), P(2, "B")];
  assert.deepEqual(blindSeats(ps, 1), { btn: 1, sb: 1, bb: 2 });
  let s = deal(ps);
  assert.equal(actor(s), "A");
  s = act(s, "call");
  assert.equal(actor(s), "B");
  s = act(s, "check");
  assert.equal(s.streetComplete, true);
  s = advanceStreet(s);
  assert.equal(actor(s), "B");
});

test("positions by table size", () => {
  const six = [1, 2, 3, 4, 5, 6].map((n) => P(n, "P" + n));
  assert.deepEqual(positionLabels(six, 1), { 1: "BTN", 2: "SB", 3: "BB", 4: "UTG", 5: "HJ", 6: "CO" });
  assert.deepEqual(positionLabels([P(1, "A"), P(2, "B")], 2), { 2: "BTN/SB", 1: "BB" });
});

test("fold to the big blind returns the SB-sized uncalled bet", () => {
  let s = deal([P(1, "A"), P(2, "B"), P(3, "C")]);
  assert.equal(actor(s), "A"); // 3-handed: BTN is first to act preflop
  s = act(s, "fold");
  s = act(s, "fold");
  assert.equal(s.handOver, true);
  assert.deepEqual(s.uncalled, { seat: 3, amount: 50 });
  const end = computeEndStacks(s, {});
  assert.deepEqual(end, { 1: 10000, 2: 9950, 3: 10050 });
});

test("all-in for LESS than the call is logged as a call, not a raise", () => {
  // B can't cover A's raise: clicking ALL-IN must record "calls … and is all-in".
  let s = deal([P(1, "A", 10000), P(2, "B", 300), P(3, "C", 10000)]);
  s = act(s, "raise", 1000); // A
  s = act(s, "allin");       // B (SB) has 250 behind after posting 50
  const log = lastLog(s);
  assert.equal(log.player, "B");
  assert.equal(log.action, "calls_allin");
  assert.equal(log.amount, 250);
  assert.equal(s.currentBet, 1000, "a short all-in must not change the bet to call");
  assert.equal(actor(s), "C");
});

test("all-in for MORE than the call is a raise all-in", () => {
  let s = deal([P(1, "A", 10000), P(2, "B", 3000), P(3, "C", 10000)]);
  s = act(s, "raise", 1000);
  s = act(s, "allin");
  assert.deepEqual(lastLog(s), { player: "B", action: "all_in", amount: 3000 });
  assert.equal(s.currentBet, 3000);
});

test("all-in exactly matching the call is a call all-in", () => {
  // B has 1000 behind after the SB; the call costs 950 and leaves 50, so ALL-IN raises to 1050.
  let s = deal([P(1, "A", 10000), P(2, "B", 1050), P(3, "C", 10000)]);
  s = act(s, "raise", 1000);
  s = act(s, "allin");
  assert.deepEqual(lastLog(s), { player: "B", action: "all_in", amount: 1050 });
  let t = deal([P(1, "A", 10000), P(2, "B", 1000), P(3, "C", 10000)]);
  t = act(t, "raise", 1000);
  t = act(t, "allin"); // B: 950 behind exactly covers the 950 call
  assert.deepEqual(lastLog(t), { player: "B", action: "calls_allin", amount: 950 });
});

test("straddles post in order and action starts left of the last straddler", () => {
  const ps = [1, 2, 3, 4, 5].map((n) => P(n, "P" + n));
  const s = deal(ps, { straddles: [{ seat: 4, amount: 200 }] });
  assert.deepEqual(s.actionsByStreet.preflop.map((a) => [a.player, a.action, a.amount]), [
    ["P2", "posts_sb", 50], ["P3", "posts_bb", 100], ["P4", "posts_straddle", 200],
  ]);
  assert.equal(s.currentBet, 200);
  assert.equal(actor(s), "P5");
});

test("a blind that also straddles keeps both posts in its street total", () => {
  const ps = [1, 2, 3, 4].map((n) => P(n, "P" + n));
  const s = deal(ps, { straddles: [{ seat: 3, amount: 200 }] }); // seat 3 is the BB
  const bbP = s.players.find((p) => p.seat === 3);
  assert.equal(bbP.committedTotal, 300);
  assert.equal(bbP.committedStreet, 300, "street total must match chips put in");
  assert.equal(chips(s), 40000);
});

test("BB ante is dead money in the main pot", () => {
  let s = deal([P(1, "A"), P(2, "B"), P(3, "C")], { ante: 100 });
  assert.equal(s.actionsByStreet.preflop[0].action, "posts_ante");
  assert.equal(potTotal(s), 250);
  s = act(s, "fold"); s = act(s, "fold");
  const end = computeEndStacks(s, {});
  // C posted 100 ante + 100 BB, wins 50 SB + own 200 back; uncalled 50 vs SB.
  assert.equal(end[3], 10050);
  assert.equal(Object.values(end).reduce((a, b) => a + b, 0), 30000);
});

test("three-way all-in builds a main pot and a side pot", () => {
  let s = deal([P(1, "A", 1000), P(2, "B", 5000), P(3, "C", 5000)]);
  s = act(s, "allin");       // A all-in 1000
  s = act(s, "allin");       // B all-in 5000
  s = act(s, "call");        // C calls 5000
  assert.equal(s.bettingClosed, true);
  const pots = computeSidePots(s);
  assert.deepEqual(pots, [{ amount: 3000, eligible: [1, 2, 3] }, { amount: 8000, eligible: [2, 3] }]);
});

test("side pots pay the right winners and conserve chips", () => {
  let s = deal([P(1, "A", 1000), P(2, "B", 5000), P(3, "C", 5000)]);
  s = act(s, "allin"); s = act(s, "allin"); s = act(s, "call");
  for (let i = 0; i < 3; i++) s = advanceStreet(s);
  const holeCards = { 1: ["As", "Ah"], 2: ["Ks", "Kh"], 3: ["Qs", "Qh"] };
  const board = ["2c", "7d", "9h", "Jc", "3s"];
  const end = computeEndStacks(s, { holeCards, board });
  assert.deepEqual(end, { 1: 3000, 2: 8000, 3: 0 });
  const hand = buildHandDict(s, { stakes: "50/100", holeCards, board, positions: positionLabels(s.players, 1) });
  assert.deepEqual(hand.pots.map((p) => p.winners), [["A"], ["B"]]);
});

test("chopped pot splits evenly with the odd chip to the first winner", () => {
  let s = deal([P(1, "A"), P(2, "B")]);
  s = act(s, "raise", 301); s = act(s, "call");
  for (let i = 0; i < 3; i++) { if (s.streetComplete || s.bettingClosed) s = advanceStreet(s); while (s.actorSeat != null) s = act(s, "check"); }
  const end = computeEndStacks(s, { winners: ["A", "B"] });
  assert.equal(end[1] + end[2], 20000);
  assert.equal(Math.abs(end[1] - end[2]) <= 1, true);
});

test("survivors and pot after a fold", () => {
  let s = deal([P(1, "A"), P(2, "B"), P(3, "C")]);
  s = act(s, "raise", 300); s = act(s, "fold");
  assert.deepEqual(survivors(s), ["A", "C"]);
  assert.equal(potTotal(s), 450); // A 300 + B's folded 50 + C's 100
});

test("legal actions expose the call amount and all-in-ness", () => {
  let s = deal([P(1, "A", 10000), P(2, "B", 400), P(3, "C", 10000)]);
  s = act(s, "raise", 1000);
  const L = legalActions(s);
  assert.equal(L.player.name, "B");
  assert.equal(L.callAmount, 350);
  assert.equal(L.callIsAllin, true);
  assert.equal(L.canRaise, false, "can't raise when the stack doesn't cover the call");
});
