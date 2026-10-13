// A long random "session" with the bounty, squid AND 7-2 games running, as the
// builder would submit it: streaks and buttons carry from hand to hand, players
// come and go (so squid holders end up Away), rounds settle and restart.
// Usage: node tests/dump-side-game-hands.mjs 2000 [seed] > hands.jsonl
import { buildHandDict, positionLabels, survivors } from "../src/engine.js";
import { evaluateBest, cmpScore } from "../src/evaluator.js";
import {
  applySideGameNames, mainPotWinners, settleBounty, settleSquid, settleSevenTwo, sideGamePayload, DEFAULT_BOUNTY,
} from "../src/sideGames.js";
import { rng, randomHand } from "./fuzz-helpers.mjs";

const N = Number(process.argv[2] || 1000);
const rand = rng(Number(process.argv[3] || 5));
const schedule = { ...DEFAULT_BOUNTY };
let streaks = {};
let buttons = {};
let round = 1;
let config = { total: 7, value: 200, mode: "progressive" };
let left = config.total;

for (let i = 0; i < N; i++) {
  if (i === Math.floor(N / 2)) { // second half: the rare one-per-player variant, fresh round
    config = { total: 5, value: 150, mode: "one-per-player" };
    buttons = {}; round += 1; left = config.total;
  }
  const { s, holeCards, board } = randomHand(rand);
  const surv = survivors(s);
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
  hand.table_name = `https://youtu.be/SIDEGAME${String(i % 1000).padStart(3, "0")}?t=${60 + i}`;
  hand.video_date = "2026-10-12";

  const dealtIn = [...s.players].sort((a, b) => a.seat - b.seat).map((p) => ({ seat: p.seat, name: p.name }));
  const won = mainPotWinners(hand);
  const bounty = settleBounty({ dealtIn, streaks, schedule, winners: won });
  const squid = settleSquid({ dealtIn, buttons, config, round, left, winners: won });
  const out = applySideGameNames(hand, streaks, buttons);
  const sevenTwo = settleSevenTwo({ dealtIn, holeCards, value: 250, winners: won });
  out.side_game = sideGamePayload({ bounty, bountySchedule: schedule, squid, squidConfig: config, squidRound: round, sevenTwo, sevenTwoValue: 250 });
  process.stdout.write(JSON.stringify({ i, hand: out }) + "\n");

  streaks = bounty.nextStreaks;
  ({ buttons, round, left } = squid.next);
}
