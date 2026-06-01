// 5-card hand evaluator, ported from src/export/hand_evaluator.py.
// Returns a comparable score array (higher = better) so per-pot winners can be
// determined client-side for side-pot distribution. Descriptions are left to
// the backend (the PT4 output is authoritative).

const RANK_ORDER = "23456789TJQKA";
const RANK_VALUE = {};
[...RANK_ORDER].forEach((r, i) => (RANK_VALUE[r] = i));

function parseCard(card) {
  return [RANK_VALUE[card[0].toUpperCase()] ?? 0, card[1].toLowerCase()];
}

// Compare two score arrays lexicographically. >0 if a beats b.
export function cmpScore(a, b) {
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a[i] ?? -1;
    const y = b[i] ?? -1;
    if (x !== y) return x - y;
  }
  return 0;
}

function scoreFive(cards) {
  const parsed = cards.map(parseCard);
  const ranks = parsed.map((p) => p[0]).sort((a, b) => b - a);
  const suits = parsed.map((p) => p[1]);
  const rankSet = new Set(ranks);
  const isFlush = new Set(suits).size === 1;
  const isWheel = rankSet.size === 5 && [12, 0, 1, 2, 3].every((r) => rankSet.has(r));
  const isStraight = (rankSet.size === 5 && ranks[0] - ranks[4] === 4) || isWheel;
  const straightTop = isWheel ? 3 : ranks[0];

  const counts = {};
  ranks.forEach((r) => (counts[r] = (counts[r] || 0) + 1));
  // sort by (count desc, rank desc)
  const entries = Object.keys(counts)
    .map((r) => [Number(r), counts[r]])
    .sort((a, b) => b[1] - a[1] || b[0] - a[0]);
  const sortedRanks = entries.map((e) => e[0]);
  const groups = entries.map((e) => e[1]);

  if (isStraight && isFlush) return [8, straightTop];
  if (groups[0] === 4) return [7, sortedRanks[0], sortedRanks[1]];
  if (groups[0] === 3 && groups[1] === 2) return [6, sortedRanks[0], sortedRanks[1]];
  if (isFlush) return [5, ...ranks];
  if (isStraight) return [4, straightTop];
  if (groups[0] === 3) return [3, sortedRanks[0], ...sortedRanks.slice(1)];
  if (groups[0] === 2 && groups[1] === 2) {
    const hp = Math.max(sortedRanks[0], sortedRanks[1]);
    const lp = Math.min(sortedRanks[0], sortedRanks[1]);
    return [2, hp, lp, sortedRanks[2]];
  }
  if (groups[0] === 2) return [1, sortedRanks[0], ...sortedRanks.slice(1)];
  return [0, ...ranks];
}

function* combinations(arr, k) {
  const n = arr.length;
  if (k > n) return;
  const idx = Array.from({ length: k }, (_, i) => i);
  while (true) {
    yield idx.map((i) => arr[i]);
    let i = k - 1;
    while (i >= 0 && idx[i] === i + n - k) i--;
    if (i < 0) return;
    idx[i]++;
    for (let j = i + 1; j < k; j++) idx[j] = idx[j - 1] + 1;
  }
}

// Best 5-card score from hole + board. Returns null if < 5 cards available.
export function evaluateBest(hole, board) {
  const all = [...(hole || []), ...(board || [])].filter(Boolean);
  if (all.length < 5) return null;
  let best = null;
  for (const combo of combinations(all, 5)) {
    const s = scoreFive(combo);
    if (best === null || cmpScore(s, best) > 0) best = s;
  }
  return best;
}
