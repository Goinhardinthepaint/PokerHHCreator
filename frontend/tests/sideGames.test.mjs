// Side-game logic, checked against the worked examples in docs/SIDE_GAME_HH_FORMAT.md.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  displayName, applySideGameNames, mainPotWinners, bountyAmount, bountyLevels, settleBounty,
  settleSquid, sideGameDeltas, sideGamePayload, DEFAULT_BOUNTY,
} from "../src/sideGames.js";

const seats = (...names) => names.map((name, i) => ({ seat: i + 1, name }));
const SCHED = { start: 3, first: 300, step: 100 };

test("display names: suffixes for counts entering the hand, bounty first", () => {
  assert.equal(displayName("JT"), "JT");
  assert.equal(displayName("JT", 3), "JT3bounty");
  assert.equal(displayName("raver", 0, 2), "raver2squid");
  assert.equal(displayName("JT", 3, 1), "JT3bounty1squid");
  assert.equal(displayName("Adam22", 0, 1), "Adam221squid"); // why parsers must not regex-strip
});

test("names are rewritten everywhere and the source dict is untouched", () => {
  const hand = {
    players: [{ name: "JT" }, { name: "raver" }],
    action: { preflop: [{ player: "JT", action: "raises", amount: 300 }, { player: "raver", action: "folds" }] },
    showdown: [], winner: "JT", pots: [{ winners: ["JT"] }],
  };
  const out = applySideGameNames(hand, { JT: 2 }, { JT: 1, raver: 3 });
  assert.deepEqual(out.players.map((p) => p.name), ["JT2bounty1squid", "raver3squid"]);
  assert.deepEqual(out.action.preflop.map((a) => a.player), ["JT2bounty1squid", "raver3squid"]);
  assert.equal(out.winner, "JT2bounty1squid");
  assert.deepEqual(out.pots[0].winners, ["JT2bounty1squid"]);
  assert.equal(hand.players[0].name, "JT");
});

test("who won the hand: uncontested, showdown, chop, side pots, run it twice", () => {
  assert.deepEqual(mainPotWinners({ winner: "A" }), ["A"]);
  assert.deepEqual(mainPotWinners({ showdown: [{ player: "A", result: "wins" }, { player: "B", result: "loses" }] }), ["A"]);
  assert.deepEqual(mainPotWinners({ showdown: [{ player: "A", result: "wins" }, { player: "B", result: "wins" }] }), ["A", "B"]);
  // A side-pot-only winner did not win the hand.
  assert.deepEqual(mainPotWinners({ showdown: [], pots: [{ winners: ["A"] }, { winners: ["B"] }] }), ["A"]);
  // Run it twice: only board one counts.
  assert.deepEqual(mainPotWinners({ num_runs: 2, showdown: [
    { player: "A", result: "wins", run: 1 }, { player: "B", result: "loses", run: 1 },
    { player: "A", result: "loses", run: 2 }, { player: "B", result: "wins", run: 2 },
  ] }), ["A"]);
});

test("bounty schedule: 3 pays $300, 4 pays $400, then +$100 a win", () => {
  assert.deepEqual([2, 3, 4, 5, 6, 7].map((s) => bountyAmount(s, SCHED)), [0, 300, 400, 500, 600, 700]);
  assert.deepEqual(bountyLevels(SCHED), { 3: 300, 4: 400, 5: 500 });
  assert.deepEqual(DEFAULT_BOUNTY, SCHED);
});

test("bounty example A: JT wins his 3rd straight, $300 from each of 3 others", () => {
  const r = settleBounty({ dealtIn: seats("JT", "raver", "Nik", "Britney"), streaks: { JT: 2 }, schedule: SCHED, winners: ["JT"] });
  assert.deepEqual(r.rows.map((x) => [x.base, x.before, x.after, x.result]), [
    ["JT", 2, 3, "won"], ["raver", 0, 0, "lost"], ["Nik", 0, 0, "lost"], ["Britney", 0, 0, "lost"],
  ]);
  assert.deepEqual(r.payouts.map((p) => [p.payer, p.winner, p.amount, p.streak]), [
    ["raver", "JT", 300, 3], ["Nik", "JT", 300, 3], ["Britney", "JT", 300, 3],
  ]);
  assert.deepEqual(r.nextStreaks, { JT: 3 });
  assert.deepEqual(sideGameDeltas(r.payouts), { JT: 900, raver: -300, Nik: -300, Britney: -300 });
});

test("bounty stacks: four straight wins = $700 from each opponent", () => {
  const dealtIn = seats("JT", "raver");
  let streaks = {}, paid = 0;
  for (let i = 0; i < 4; i++) {
    const r = settleBounty({ dealtIn, streaks, schedule: SCHED, winners: ["JT"] });
    paid += r.payouts.filter((p) => p.payer === "raver").reduce((a, p) => a + p.amount, 0);
    streaks = r.nextStreaks;
  }
  assert.equal(paid, 700);
});

test("bounty example B: a chop advances both choppers and pays nobody", () => {
  const r = settleBounty({ dealtIn: seats("JT", "raver", "Nik", "Britney"), streaks: { JT: 3 }, schedule: SCHED, winners: ["JT", "raver"] });
  assert.deepEqual(r.rows.map((x) => [x.base, x.before, x.after, x.result]), [
    ["JT", 3, 4, "chopped"], ["raver", 0, 1, "chopped"], ["Nik", 0, 0, "lost"], ["Britney", 0, 0, "lost"],
  ]);
  assert.equal(r.payouts.length, 0);
  assert.deepEqual(r.nextStreaks, { JT: 4, raver: 1 });
});

test("bounty: a player not dealt in keeps their streak", () => {
  const r = settleBounty({ dealtIn: seats("A", "B"), streaks: { Away: 5, A: 1 }, schedule: SCHED, winners: ["B"] });
  assert.deepEqual(r.nextStreaks, { Away: 5, B: 1 });
});

const EIGHT = seats("JT", "raver", "Nik", "Britney", "Rampage", "Mariano", "Sashimi", "Adam22");
const CFG = { total: 7, value: 200, mode: "progressive" };

test("squid example C: the last of 7 buttons settles the round", () => {
  const r = settleSquid({ dealtIn: EIGHT, buttons: { JT: 2, raver: 1, Nik: 1, Britney: 1, Rampage: 1 }, config: CFG, round: 1, left: 1, winners: ["Mariano"] });
  assert.equal(r.settled, true);
  assert.deepEqual([r.leftBefore, r.leftAfter], [1, 0]);
  assert.deepEqual(r.rows.find((x) => x.base === "Mariano"), { seat: 6, base: "Mariano", before: 0, after: 1, won: true });
  const byPayer = {}, byHolder = {};
  for (const p of r.payouts) {
    byPayer[p.payer] = (byPayer[p.payer] || 0) + p.amount;
    byHolder[p.holder] = (byHolder[p.holder] || 0) + p.amount;
  }
  assert.deepEqual(byPayer, { Sashimi: 1400, Adam22: 1400 });
  assert.deepEqual(byHolder, { JT: 800, raver: 400, Nik: 400, Britney: 400, Rampage: 400, Mariano: 400 });
  assert.deepEqual(r.next, { buttons: {}, round: 2, left: 7 });
});

test("squid example E: a holder who left is listed Away and still paid", () => {
  const seated = EIGHT.filter((p) => p.name !== "Rampage");
  const r = settleSquid({ dealtIn: seated, buttons: { JT: 2, raver: 1, Nik: 1, Britney: 1, Rampage: 1 }, config: CFG, left: 1, winners: ["Mariano"] });
  assert.deepEqual(r.rows.at(-1), { seat: null, base: "Rampage", before: 1, after: 1, won: false });
  const toRampage = r.payouts.filter((p) => p.holder === "Rampage");
  assert.deepEqual(toRampage.map((p) => [p.payer, p.holderSeat, p.amount]), [["Sashimi", null, 200], ["Adam22", null, 200]]);
  assert.equal(r.rows.reduce((a, x) => a + x.after, 0), 7);
});

test("squid example F: a tie gives no button to anyone", () => {
  const r = settleSquid({ dealtIn: EIGHT, buttons: { JT: 2, Nik: 1 }, config: CFG, left: 4, winners: ["raver", "Nik"] });
  assert.equal(r.leftAfter, 4);
  assert.equal(r.rows.some((x) => x.won), false);
});

test("squid example D and one-per-player mode", () => {
  const d = settleSquid({ dealtIn: EIGHT, buttons: { JT: 1, raver: 1, Nik: 1, Britney: 1 }, config: CFG, left: 3, winners: ["JT"] });
  assert.deepEqual([d.leftAfter, d.next.buttons.JT], [2, 2]);
  const one = settleSquid({ dealtIn: EIGHT, buttons: { JT: 1 }, config: { ...CFG, mode: "one-per-player" }, left: 6, winners: ["JT"] });
  assert.deepEqual([one.leftAfter, one.next.buttons.JT, one.rows[0].won], [6, 1, false]);
});

test("payload carries snake_case fields for the server", () => {
  const b = settleBounty({ dealtIn: seats("JT", "raver"), streaks: { JT: 2 }, schedule: SCHED, winners: ["JT"] });
  const s = settleSquid({ dealtIn: seats("JT", "raver"), buttons: {}, config: CFG, left: 7, winners: ["JT"] });
  const p = sideGamePayload({ bounty: b, bountySchedule: SCHED, squid: s, squidConfig: CFG, squidRound: 1 });
  assert.deepEqual(p.bounty.levels, { 3: 300, 4: 400, 5: 500 });
  assert.deepEqual(p.bounty.payouts[0], { payer_seat: 2, payer: "raver", winner_seat: 1, winner: "JT", amount: 300, streak: 3 });
  assert.deepEqual([p.squid.left_before, p.squid.left_after, p.squid.mode], [7, 6, "progressive"]);
});
