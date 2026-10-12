import { test } from "node:test";
import assert from "node:assert/strict";
import { bountyName, applyBountyNames, handWinners, advanceStreaks } from "../src/bounty.js";

const hand = {
  players: [{ name: "JT", seat: 1 }, { name: "raver", seat: 2 }, { name: "Nik", seat: 3 }],
  action: {
    preflop: [
      { player: "JT", action: "posts_sb", amount: 50 },
      { player: "raver", action: "posts_bb", amount: 100 },
      { player: "Nik", action: "raises", amount: 300 },
      { player: "JT", action: "calls" },
      { player: "raver", action: "folds" },
    ],
    flop: [{ player: "JT", action: "checks" }, { player: "Nik", action: "bets", amount: 400 }, { player: "JT", action: "calls" }],
  },
  showdown: [{ player: "JT", result: "wins" }, { player: "Nik", result: "loses" }],
  pots: [{ amount: 1000, type: "main", winners: ["JT"] }],
};
const dealt = ["JT", "raver", "Nik"];

test("names: plain at zero, suffixed otherwise, spaces kept", () => {
  assert.equal(bountyName("raver", 0), "raver");
  assert.equal(bountyName("raver", 2), "raver2bounty");
  assert.equal(bountyName("Blank Check Ben", 4), "Blank Check Ben4bounty");
});

test("rename touches every name-bearing field and never mutates the source", () => {
  const out = applyBountyNames(hand, { JT: 3 });
  assert.deepEqual(out.players.map((p) => p.name), ["JT3bounty", "raver", "Nik"]);
  assert.deepEqual(out.action.preflop.map((a) => a.player), ["JT3bounty", "raver", "Nik", "JT3bounty", "raver"]);
  assert.deepEqual(out.showdown.map((s) => s.player), ["JT3bounty", "Nik"]);
  assert.deepEqual(out.pots[0].winners, ["JT3bounty"]);
  assert.equal(hand.players[0].name, "JT");
  assert.equal(applyBountyNames({ ...hand, showdown: [], winner: "raver" }, { raver: 2 }).winner, "raver2bounty");
});

test("streaks: winner +1, everyone else dealt in resets, absent players keep theirs", () => {
  assert.deepEqual(advanceStreaks({ JT: 2, raver: 1 }, dealt, handWinners(hand)), { JT: 3 });
  assert.deepEqual(advanceStreaks({}, dealt, new Set(["Nik"])), { Nik: 1 });
  assert.deepEqual(advanceStreaks({ Away: 5, JT: 1 }, dealt, new Set(["JT"])), { Away: 5, JT: 2 });
});

test("a chop advances both players", () => {
  const chop = { ...hand, showdown: [{ player: "JT", result: "wins" }, { player: "Nik", result: "wins" }] };
  assert.deepEqual(advanceStreaks({ JT: 3 }, dealt, handWinners(chop)), { JT: 4, Nik: 1 });
});

test("multi-run and side-pot winners all count as taking a share", () => {
  const rit = { ...hand, pots: undefined, showdown: [
    { player: "JT", result: "wins", run: 1 }, { player: "Nik", result: "loses", run: 1 },
    { player: "JT", result: "loses", run: 2 }, { player: "Nik", result: "wins", run: 2 },
  ] };
  assert.deepEqual([...handWinners(rit)].sort(), ["JT", "Nik"]);
  const sp = { ...hand, pots: [{ amount: 600, winners: ["JT"] }, { amount: 400, winners: ["Nik"] }] };
  assert.deepEqual([...handWinners(sp)].sort(), ["JT", "Nik"]);
});
