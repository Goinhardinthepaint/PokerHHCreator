// Dump random engine hands as the exact hand dicts the Hand Builder submits, one
// JSON object per line, for tests/test_e2e_fuzz.py to push through the Python
// formatter and the PT4 linter.  Usage: node tests/dump-random-hands.mjs 2000 > hands.jsonl
import { buildHandDict, positionLabels, survivors } from "../src/engine.js";
import { evaluateBest, cmpScore } from "../src/evaluator.js";
import { rng, randomHand } from "./fuzz-helpers.mjs";

const N = Number(process.argv[2] || 1000);
const rand = rng(Number(process.argv[3] || 7));
for (let i = 0; i < N; i++) {
  const { s, holeCards, board } = randomHand(rand);
  const surv = survivors(s);
  // Winner(s) the way the builder gets them: best hand among survivors at
  // showdown (the server's /api/evaluate), else the last player standing.
  let winners = [surv[0]];
  if (surv.length >= 2) {
    let best = null;
    winners = [];
    for (const name of surv) {
      const seat = s.players.find((p) => p.name === name).seat;
      const sc = evaluateBest(holeCards[seat], board);
      const c = best ? cmpScore(sc, best) : 1;
      if (c > 0) { best = sc; winners = [name]; } else if (c === 0) winners.push(name);
    }
  }
  const hand = buildHandDict(s, {
    stakes: "50/100", holeCards, board, winner: winners[0], winners,
    positions: positionLabels(s.players, s.buttonSeat),
  });
  hand.timestamp_start = "00:20:00";
  hand.table_name = `https://youtu.be/FUZZFUZZ${String(i % 1000).padStart(3, "0")}?t=${1200 + i}`;
  hand.video_date = "2026-10-12";
  process.stdout.write(JSON.stringify({ i, hand }) + "\n");
}
