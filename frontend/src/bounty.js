// Bounty game notation.
//
// In the bounty game a player's win streak going into a hand is part of their
// PT4 name for that hand: "raver2bounty" means raver won the last two hands in a
// row. PT4 then groups JT / JT1bounty / JT2bounty / … as one player.
//
// Streaks live in the builder as { baseName: n }. Names are only rewritten on the
// hand dict at submit time, never in the roster — otherwise the suffix would
// compound into "raver2bounty3bounty" on the next hand.

export const bountyName = (name, n) => (n > 0 ? `${name}${n}bounty` : name);

// Rewrite every player reference in a buildHandDict() result. These are the only
// name-bearing fields pt4_formatter reads: players, action[street][].player,
// showdown[].player, winner and pots[].winners.
export function applyBountyNames(hand, streaks) {
  const rn = (nm) => (typeof nm === "string" ? bountyName(nm, streaks[nm] || 0) : nm);
  const out = { ...hand };
  out.players = (hand.players || []).map((p) => ({ ...p, name: rn(p.name) }));
  out.action = Object.fromEntries(
    Object.entries(hand.action || {}).map(([street, acts]) => [
      street,
      (acts || []).map((a) => ({ ...a, player: rn(a.player) })),
    ]),
  );
  out.showdown = (hand.showdown || []).map((s) => ({ ...s, player: rn(s.player) }));
  if (hand.winner) out.winner = rn(hand.winner);
  if (hand.pots) out.pots = hand.pots.map((p) => ({ ...p, winners: (p.winners || []).map(rn) }));
  return out;
}

// Everyone who took any part of the pot: an uncontested winner, every "wins"
// entry at showdown (chops, and each run of a multi-run), and side-pot winners.
export function handWinners(hand) {
  const won = new Set();
  if (hand.winner) won.add(hand.winner);
  for (const s of hand.showdown || []) if (s.result === "wins") won.add(s.player);
  for (const p of hand.pots || []) for (const nm of p.winners || []) won.add(nm);
  return won;
}

// Streaks the next hand starts with. A winner goes up one — and so does anyone
// who chops: a chop doesn't pay the bounty out, but the player is still
// competing for the next, bigger one. Everyone else dealt in goes back to zero.
// Players who weren't dealt in keep whatever streak they had.
export function advanceStreaks(streaks, dealtIn, winners) {
  const next = {};
  for (const [nm, n] of Object.entries(streaks)) {
    if (n > 0 && !dealtIn.includes(nm)) next[nm] = n;
  }
  for (const nm of dealtIn) {
    if (winners.has(nm)) next[nm] = (streaks[nm] || 0) + 1;
  }
  return next;
}
