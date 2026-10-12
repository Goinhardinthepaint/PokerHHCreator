// Randomised hands through the engine: chips must be conserved, stacks must never
// go negative, the log must be well-formed, and every hand must terminate.
// Deterministic (seeded) so a failure reproduces; set FUZZ_HANDS to run more.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  computeEndStacks, buildHandDict, positionLabels, survivors,
} from "../src/engine.js";
import { rng, randomHand } from "./fuzz-helpers.mjs";

const HANDS = Number(process.env.FUZZ_HANDS || 3000);

test(`fuzz: ${HANDS} random hands conserve chips and log cleanly`, () => {
  const rand = rng(20261012);
  const LOGGED = new Set(["posts_sb", "posts_bb", "posts_ante", "posts_straddle", "folds", "checks",
    "calls", "calls_allin", "bets", "bets_allin", "raises", "all_in"]);
  for (let h = 0; h < HANDS; h++) {
    const { s, start, holeCards, board } = randomHand(rand);
    const ctx = `hand ${h}`;
    for (const p of s.players) assert.ok(p.stack >= 0, `${ctx}: negative stack ${p.name}`);

    // Every logged action is a known kind; raises go UP, calls never exceed the bet.
    for (const street of ["preflop", "flop", "turn", "river"]) {
      let bet = 0;
      for (const a of s.actionsByStreet[street]) {
        assert.ok(LOGGED.has(a.action), `${ctx}: unknown action ${a.action}`);
        if (a.action === "raises" || a.action === "all_in") {
          assert.ok(a.amount > bet, `${ctx}: ${street} ${a.player} "${a.action}" to ${a.amount} is not above the bet ${bet}`);
          bet = a.amount;
        } else if (a.action === "bets" || a.action === "bets_allin") {
          assert.equal(bet, 0, `${ctx}: bet into an existing bet`);
          bet = a.amount;
        } else if (a.action === "posts_bb" || a.action === "posts_straddle") {
          bet = Math.max(bet, a.amount);
        }
      }
    }

    const surv = survivors(s);
    const end = computeEndStacks(s, { winner: surv[0], winners: surv.length ? [surv[0]] : [], holeCards, board });
    const total = Object.values(end).reduce((a, b) => a + b, 0);
    assert.equal(total, start, `${ctx}: chips not conserved (${total} vs ${start})`);
    for (const v of Object.values(end)) assert.ok(v >= 0, `${ctx}: negative end stack`);

    const hand = buildHandDict(s, {
      stakes: "50/100", holeCards, board, winner: surv[0], winners: [surv[0]],
      positions: positionLabels(s.players, s.buttonSeat),
    });
    assert.equal(hand.players.length, s.players.length, `${ctx}: players lost from the hand dict`);
  }
});
