import { useState, useMemo, useEffect, useRef } from "react";
import {
  initHand,
  legalActions,
  applyAction,
  advanceStreet,
  buildHandDict,
  computeEndStacks,
  utgStraddles,
  positionLabels,
  potTotal,
  survivors,
  STREETS,
} from "./engine.js";

// ── Card constants ──────────────────────────────────────────────────────────
const RANKS = ["A", "K", "Q", "J", "T", "9", "8", "7", "6", "5", "4", "3", "2"];
const SUITS = [
  { v: "s", symbol: "♠", color: "#1e293b" },
  { v: "h", symbol: "♥", color: "#dc2626" },
  { v: "d", symbol: "♦", color: "#2563eb" },
  { v: "c", symbol: "♣", color: "#16a34a" },
];
const SUIT = Object.fromEntries(SUITS.map((s) => [s.v, s]));
const STREET_LABEL = { preflop: "Preflop", flop: "Flop", turn: "Turn", river: "River" };
const BADGE_COLOR = {
  BTN: "#f59e0b", "BTN/SB": "#f59e0b", SB: "#38bdf8", BB: "#a78bfa",
  CO: "#22c55e", HJ: "#22c55e", LJ: "#22c55e", STR: "#7c3aed",
};

const fmtChips = (n) => "$" + (n ?? 0).toLocaleString();

// Parse a YouTube link into a normalized url + start time (seconds).
// Supports watch?v=, youtu.be/, embed/, and t=/start= (e.g. 2000, 2000s, 1h2m3s).
function parseTimestamp(s) {
  if (/^\d+$/.test(s)) return parseInt(s, 10);
  let sec = 0;
  const h = s.match(/(\d+)h/);
  const m = s.match(/(\d+)m/);
  const ss = s.match(/(\d+)s/);
  if (h) sec += parseInt(h[1], 10) * 3600;
  if (m) sec += parseInt(m[1], 10) * 60;
  if (ss) sec += parseInt(ss[1], 10);
  return sec;
}
function parseYouTube(link) {
  if (!link || !link.trim()) return { url: "", startSec: 0, id: "" };
  const id =
    (link.match(/[?&]v=([A-Za-z0-9_-]{11})/) ||
      link.match(/youtu\.be\/([A-Za-z0-9_-]{11})/) ||
      link.match(/embed\/([A-Za-z0-9_-]{11})/) || [])[1] || "";
  const t = link.match(/[?&](?:t|start)=([0-9hms]+)/);
  const startSec = t ? parseTimestamp(t[1]) : 0;
  // Canonical, clickable link that keeps the timestamp so it jumps to the hand.
  const base = id ? `https://youtu.be/${id}` : link.trim();
  const url = id && startSec > 0 ? `${base}?t=${startSec}` : base;
  return { url, startSec, id };
}
function secsToHMS(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return [h, m, sec].map((x) => String(x).padStart(2, "0")).join(":");
}

// ── Playing card ────────────────────────────────────────────────────────────
function Card({ card, onClick, size = "md", faceDownIfEmpty }) {
  const dims = { sm: { w: 30, h: 42, f: 15 }, md: { w: 44, h: 62, f: 20 }, lg: { w: 56, h: 78, f: 26 } }[size];
  const base = {
    width: dims.w, height: dims.h, borderRadius: 6, display: "flex",
    flexDirection: "column", alignItems: "center", justifyContent: "center",
    cursor: onClick ? "pointer" : "default", userSelect: "none", flexShrink: 0,
    fontWeight: 800, fontSize: dims.f, lineHeight: 1,
  };
  if (!card) {
    if (faceDownIfEmpty)
      return (
        <div onClick={onClick} style={{ ...base, background: "repeating-linear-gradient(45deg,#7f1d1d,#7f1d1d 4px,#991b1b 4px,#991b1b 8px)", border: "2px solid #450a0a" }} />
      );
    return (
      <div onClick={onClick} style={{ ...base, background: "#0b1220", border: "1.5px dashed #334155", color: "#475569", fontSize: dims.f - 4 }}>＋</div>
    );
  }
  const rank = card[0] === "T" ? "10" : card[0];
  const sd = SUIT[card[1]] || SUITS[0];
  return (
    <div onClick={onClick} style={{ ...base, background: "#f8fafc", border: "1px solid #cbd5e1", color: sd.color, boxShadow: "0 2px 6px rgba(0,0,0,.4)", gap: 1 }}>
      <span>{rank}</span>
      <span style={{ fontSize: dims.f + 2 }}>{sd.symbol}</span>
    </div>
  );
}

// ── Card picker popover (rank grid + suit grid) ─────────────────────────────
function CardPicker({ used, onPick, onClose, title }) {
  return (
    <div style={styles.pickerOverlay} onClick={onClose}>
      <div style={styles.pickerBox} onClick={(e) => e.stopPropagation()}>
        <div style={styles.pickerTitle}>{title || "Choose card"}</div>
        {SUITS.map((s) => (
          <div key={s.v} style={{ display: "flex", gap: 4, marginBottom: 6 }}>
            {RANKS.map((r) => {
              const c = `${r}${s.v}`;
              const taken = used.has(c);
              return (
                <button
                  key={c}
                  disabled={taken}
                  onClick={() => onPick(c)}
                  style={{
                    ...styles.pickerCard,
                    color: s.color,
                    opacity: taken ? 0.22 : 1,
                    cursor: taken ? "not-allowed" : "pointer",
                  }}
                >
                  {r === "T" ? "10" : r}
                  {s.symbol}
                </button>
              );
            })}
          </div>
        ))}
        <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8 }}>
          <button style={styles.pickerClear} onClick={() => onPick("")}>Clear</button>
          <button style={styles.pickerClear} onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}

// ── Player seat ─────────────────────────────────────────────────────────────
function Seat({ p, empty, pos, badge, isButton, isActor, folded, committed, cards, desc, isWinner, bought, phase, sitting, sitValue, onSit, onSitChange, onSitCommit, onNameClick, onCardClick, onStackClick, editingStack, stackValue, onStackChange, onStackCommit }) {
  const posStyle = { ...styles.seat, left: `${pos.left}%`, top: `${pos.top}%` };

  if (empty) {
    return (
      <div style={posStyle}>
        <div style={styles.emptySeat}>
          <div style={styles.emptySeatNum}>SEAT {p.seat}</div>
          {sitting ? (
            <input
              autoFocus
              style={styles.sitInput}
              value={sitValue}
              placeholder="name…"
              onChange={(e) => onSitChange(e.target.value)}
              onBlur={onSitCommit}
              onKeyDown={(e) => e.key === "Enter" && onSitCommit()}
            />
          ) : (
            <button style={styles.sitBtn} onClick={onSit}>+ Sit</button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div style={{ ...posStyle, opacity: folded ? 0.4 : 1, filter: folded ? "grayscale(0.7)" : "none" }}>
      {/* bet chip toward center */}
      {committed > 0 && (
        <div style={styles.betChip}>{fmtChips(committed)}</div>
      )}
      <div style={{ ...styles.seatBox, boxShadow: isActor ? "0 0 0 2px #f59e0b, 0 0 22px 4px rgba(245,158,11,.55)" : isWinner ? "0 0 0 2px #22c55e, 0 0 20px 3px rgba(34,197,94,.5)" : "0 4px 14px rgba(0,0,0,.5)", borderColor: isActor ? "#f59e0b" : isWinner ? "#22c55e" : "#1e293b" }}>
        {badge && (
          <div style={{ ...styles.badge, background: BADGE_COLOR[badge] || "#475569" }}>{badge}</div>
        )}
        {isButton && <div style={styles.dealerBtn}>D</div>}
        <div style={styles.cardsRow}>
          <Card card={cards[0]} size="sm" faceDownIfEmpty={phase !== "setup"} onClick={() => onCardClick(0)} />
          <Card card={cards[1]} size="sm" faceDownIfEmpty={phase !== "setup"} onClick={() => onCardClick(1)} />
        </div>
        <div style={{ ...styles.seatName, cursor: onNameClick ? "pointer" : "default" }} onClick={onNameClick} title={onNameClick ? "Buy the button / straddle" : undefined}>{p.name}</div>
        {bought && <div style={styles.boughtTag}>BOUGHT {bought === "str" ? "STR" : "BTN"}</div>}
        {editingStack ? (
          <input
            autoFocus
            style={styles.stackInput}
            type="number"
            value={stackValue}
            onChange={(e) => onStackChange(e.target.value)}
            onBlur={onStackCommit}
            onKeyDown={(e) => e.key === "Enter" && onStackCommit()}
          />
        ) : (
          <div style={styles.seatStack} onClick={onStackClick}>{fmtChips(p.stack)}</div>
        )}
        {desc && (
          <div style={{ ...styles.handDesc, color: isWinner ? "#4ade80" : "#94a3b8" }}>
            {isWinner ? "🏆 " : ""}{desc}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Auto-persistence ─────────────────────────────────────────────────────────
// The entire session (roster, settings, accumulated hands, in-progress hand) is
// mirrored to localStorage on every change and restored at load, so reloads,
// code updates and refreshes never lose data. Read once at module load.
const STORAGE_KEY = "pokerTable.session.v2";
const DEFAULT_ROSTER = [
  { seat: 1, name: "Ivey", stack: 10000 },
  { seat: 2, name: "Negreanu", stack: 10000 },
  { seat: 3, name: "Dwan", stack: 10000 },
  { seat: 4, name: "Selbst", stack: 10000 },
  { seat: 5, name: "Hellmuth", stack: 10000 },
  { seat: 6, name: "Brunson", stack: 10000 },
  { seat: 7, name: "Antonius", stack: 10000 },
  { seat: 8, name: "Polk", stack: 10000 },
  { seat: 9, name: "Galfond", stack: 10000 },
];
function loadSession() {
  try {
    const raw = typeof localStorage !== "undefined" && localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}
const SAVED = loadSession();
const pick = (key, fallback) => (SAVED[key] !== undefined ? SAVED[key] : fallback);

// ── Main app ────────────────────────────────────────────────────────────────
export default function App() {
  // Session (persists across hands) — initialised from any saved session.
  const [stakes, setStakes] = useState(() => pick("stakes", "50/100"));
  const [ante, setAnte] = useState(() => pick("ante", 100));
  const [buttonSeat, setButtonSeat] = useState(() => pick("buttonSeat", 1));
  const [straddleCount, setStraddleCount] = useState(() => pick("straddleCount", 0)); // # of UTG-style straddles
  const [buyButton, setBuyButton] = useState(() => pick("buyButton", null)); // {seat, type:'btn'|'str'}
  const [buyMenuSeat, setBuyMenuSeat] = useState(null); // seat whose buy-the-button menu is open
  const [roster, setRoster] = useState(() => pick("roster", DEFAULT_ROSTER));

  // Hand state
  const [phase, setPhase] = useState(() => pick("phase", "setup")); // setup | holecards | betting | complete
  const [eng, setEng] = useState(() => pick("eng", null));
  const [engHistory, setEngHistory] = useState(() => pick("engHistory", [])); // prior engine states for undo
  const [holeCards, setHoleCards] = useState(() => pick("holeCards", {})); // seat -> [c1,c2]
  const [board, setBoard] = useState(() => pick("board", ["", "", "", "", ""]));
  const [board2, setBoard2] = useState(() => pick("board2", ["", "", "", "", ""]));
  const [rit, setRit] = useState(() => pick("rit", false));
  const [winner, setWinner] = useState(() => pick("winner", ""));
  const [winner2, setWinner2] = useState(() => pick("winner2", ""));
  const [handNumber, setHandNumber] = useState(() => pick("handNumber", 1));
  const [youtubeLink, setYoutubeLink] = useState(() => pick("youtubeLink", "")); // per-hand timestamped link

  // UI state
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [editor, setEditor] = useState(null); // {kind:'hole'|'board'|'board2', seat?, idx}
  const [editingStack, setEditingStack] = useState(null); // seat
  const [stackDraft, setStackDraft] = useState("");
  const [sitSeat, setSitSeat] = useState(null); // empty seat being filled
  const [sitDraft, setSitDraft] = useState("");
  const [betEdit, setBetEdit] = useState(null); // {key, value}
  const [preview, setPreview] = useState(() => pick("preview", ""));
  const [error, setError] = useState("");
  const [sessionHands, setSessionHands] = useState(() => pick("sessionHands", [])); // [{n, text}] accumulated PT4 hands
  const [configDraft, setConfigDraft] = useState(""); // save/restore session JSON
  const [showHistory, setShowHistory] = useState(false); // session-history modal
  const [copiedAll, setCopiedAll] = useState(false); // transient "Copied!" feedback
  const [flash, setFlash] = useState(""); // transient green "Hand #N saved" toast

  const named = useMemo(() => roster.filter((p) => p.name.trim()), [roster]);
  // Session/roster are editable until betting actually starts — so the button
  // and stacks can still be changed after dealing or resetting a hand.
  const locked = phase === "betting" || phase === "complete";
  const positions = useMemo(() => positionLabels(named, buttonSeat), [named, buttonSeat]);
  const { sb, bb } = useMemo(() => {
    const [s, b] = stakes.split("/").map(Number);
    return { sb: s || 50, bb: b || 100 };
  }, [stakes]);
  // UTG-style straddles (UTG, UTG+1, …) computed from the count + button.
  const straddleList = useMemo(
    () => utgStraddles(named, buttonSeat, bb, straddleCount),
    [named, buttonSeat, bb, straddleCount]
  );

  // Every roster row is a physical seat at the table; a blank name = an empty
  // seat that still occupies its spot. Returns all seats sorted by seat number,
  // each annotated with occupancy and the live stack (engine stack mid-hand).
  const tableSeats = useMemo(() => {
    return [...roster]
      .sort((a, b) => a.seat - b.seat)
      .map((p) => {
        const empty = !p.name.trim();
        const enginePlayer = eng?.players.find((e) => e.seat === p.seat);
        // Show the live engine stack only once betting has started; before that
        // (setup / hole-card phase) reflect the editable roster stack.
        return {
          seat: p.seat,
          name: p.name.trim(),
          empty,
          stack: locked && enginePlayer ? enginePlayer.stack : p.stack,
        };
      });
  }, [roster, eng, locked]);

  const legal = eng ? legalActions(eng) : null;

  // Raise-input value, defaulting to the min raise for the current actor without
  // an effect: it tracks edits via a key tied to (street, actor, facing bet).
  const betKey = legal ? `${eng.street}:${eng.actorSeat}:${eng.currentBet}` : null;
  const betTo = betEdit && betEdit.key === betKey ? betEdit.value : legal ? legal.minRaiseTo : 0;
  const setBetTo = (v) => setBetEdit({ key: betKey, value: Number(v) });

  // All cards currently in use (to disable in the picker)
  const usedCards = useMemo(() => {
    const set = new Set();
    Object.values(holeCards).forEach((arr) => arr.forEach((c) => c && set.add(c)));
    board.forEach((c) => c && set.add(c));
    if (rit) board2.forEach((c) => c && set.add(c));
    return set;
  }, [holeCards, board, board2, rit]);

  // ── Derived flow flags ────────────────────────────────────────────────────
  const inBetting = phase === "betting" && eng;
  const hasActor = inBetting && eng.actorSeat != null;
  const needBoard = inBetting && eng.actorSeat == null && !eng.handOver && eng.street !== "river" && (eng.streetComplete || eng.bettingClosed);
  const nextStreet = needBoard ? STREETS[STREETS.indexOf(eng.street) + 1] : null;
  const canComplete = inBetting && eng.actorSeat == null && (eng.handOver || eng.street === "river");
  const surv = eng ? survivors(eng) : [];

  // When every surviving player has hole cards AND the board is complete, the
  // showdown can be evaluated authoritatively — the winner is no longer a guess.
  const showdownEval = useMemo(() => {
    if (!eng || rit) return null;
    const survList = eng.players.filter((p) => !p.folded);
    if (survList.length < 2) return null;
    const fullBoard = board.filter(Boolean);
    if (fullBoard.length < 5) return null;
    const evalPlayers = [];
    for (const p of survList) {
      const hc = (holeCards[p.seat] || []).filter(Boolean);
      if (hc.length < 2) return null;
      evalPlayers.push({ name: p.name, hole_cards: hc });
    }
    return { players: evalPlayers, board: fullBoard, key: JSON.stringify({ p: evalPlayers, b: fullBoard }) };
  }, [eng, holeCards, board, rit]);

  const [evalResult, setEvalResult] = useState(() => pick("evalResult", null)); // {key, winner, winners, descriptions}
  const lastEvalKey = useRef(null);
  useEffect(() => {
    if (!showdownEval) { lastEvalKey.current = null; return; }
    if (lastEvalKey.current === showdownEval.key) return;
    lastEvalKey.current = showdownEval.key;
    let cancelled = false;
    fetch("/api/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ players: showdownEval.players, board: showdownEval.board }),
    })
      .then((r) => r.json())
      .then((data) => {
        if (cancelled || !data.descriptions) return;
        setEvalResult({ key: showdownEval.key, winner: data.winner, winners: data.winners || (data.winner ? [data.winner] : []), descriptions: data.descriptions });
        if (data.winner) setWinner(data.winner); // evaluator decides — overrides any pick
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [showdownEval]);

  // Fresh evaluation matching the current cards/board (else null).
  const autoEval = showdownEval && evalResult && evalResult.key === showdownEval.key ? evalResult : null;

  // ── Auto-persist the full session on every change ─────────────────────────
  useEffect(() => {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          stakes, ante, buttonSeat, straddleCount, buyButton, roster,
          phase, eng, engHistory, holeCards, board, board2, rit,
          winner, winner2, handNumber, youtubeLink, sessionHands, preview, evalResult,
        })
      );
    } catch {
      /* localStorage full or unavailable — ignore */
    }
  }, [stakes, ante, buttonSeat, straddleCount, buyButton, roster, phase, eng, engHistory, holeCards, board, board2, rit, winner, winner2, handNumber, youtubeLink, sessionHands, preview, evalResult]);

  // Board cards required to deal the next street
  function boardReadyFor(street) {
    if (street === "flop") return board[0] && board[1] && board[2];
    if (street === "turn") return board[3];
    if (street === "river") return board[4];
    return true;
  }

  // ── Roster editing ────────────────────────────────────────────────────────
  const updateRoster = (seat, patch) =>
    setRoster((r) => r.map((p) => (p.seat === seat ? { ...p, ...patch } : p)));
  const addSeat = () => {
    const used = new Set(roster.map((p) => p.seat));
    let s = 1;
    while (used.has(s) && s <= 9) s++;
    if (s > 9) return;
    setRoster((r) => [...r, { seat: s, name: "", stack: 10000 }]); // empty seat at the table
  };
  const removeSeat = (seat) => setRoster((r) => r.filter((p) => p.seat !== seat));
  // Stand a player up: keep the physical seat at the table, just empty it.
  const emptySeat = (seat) => updateRoster(seat, { name: "" });

  // ── Card editing ──────────────────────────────────────────────────────────
  // The picker steps through a group of slots: hole cards [0,1], the flop
  // [0,1,2], turn [3], river [4]. After a pick it advances to the next still-
  // empty slot in the group and only closes once the group is filled.
  const openHole = (seat, idx) => setEditor({ kind: "hole", seat, group: [0, 1], cur: idx });
  const openBoard = (idx) => setEditor({ kind: "board", group: idx <= 2 ? [0, 1, 2] : [idx], cur: idx });
  const openBoard2 = (idx) => setEditor({ kind: "board2", group: idx <= 2 ? [0, 1, 2] : [idx], cur: idx });
  const pickCard = (c) => {
    if (!editor) return;
    const { kind, seat, group, cur } = editor;
    const base = kind === "hole" ? (holeCards[seat] ? [...holeCards[seat]] : ["", ""]) : kind === "board" ? [...board] : [...board2];
    base[cur] = c;
    if (kind === "hole") setHoleCards((hc) => ({ ...hc, [seat]: base }));
    else if (kind === "board") setBoard(base);
    else setBoard2(base);
    // Advance to the next still-empty slot in the group (forward, wrapping);
    // never re-open the slot just edited.
    const startPos = group.indexOf(cur);
    let next = null;
    for (let k = 1; k < group.length; k++) {
      const slot = group[(startPos + k) % group.length];
      if (!base[slot]) { next = slot; break; }
    }
    setEditor(next != null ? { kind, seat, group, cur: next } : null);
  };

  // ── Hand lifecycle ────────────────────────────────────────────────────────
  // Deal a fresh engine state for the current roster/button and clear the
  // hand-specific entry (cards, board, winner, preview). Keeps the YouTube link.
  function freshDeal() {
    setEng(
      initHand({ players: named, buttonSeat, sb, bb, ante: Number(ante) || 0, straddles: buyButton ? [] : straddleList, buyButton })
    );
    setEngHistory([]);
    setHoleCards({});
    setBoard(["", "", "", "", ""]);
    setBoard2(["", "", "", "", ""]);
    setRit(false);
    setWinner("");
    setWinner2("");
    setPreview("");
    setError("");
    setPhase("holecards");
  }

  function dealHand() {
    setError("");
    if (named.length < 2) {
      setError("Add at least 2 players in the sidebar.");
      setSidebarOpen(true);
      return;
    }
    if (!named.some((p) => p.seat === Number(buttonSeat))) {
      setError("Button seat must be an occupied seat.");
      return;
    }
    freshDeal();
  }

  // Restart the current hand: re-deal but stay in the editable hole-card phase so
  // the button and stacks can still be changed before betting begins.
  function resetHand() {
    if (named.length < 2) return;
    freshDeal();
  }

  // Lock in the table and begin betting. Re-inits the engine from the current
  // roster/button/stacks so any edits made during the hole-card phase apply.
  function startBetting() {
    setError("");
    if (named.length < 2) {
      setError("Add at least 2 players in the sidebar.");
      return;
    }
    if (!named.some((p) => p.seat === Number(buttonSeat))) {
      setError("Button seat must be an occupied seat.");
      setSidebarOpen(true);
      return;
    }
    setEng(
      initHand({ players: named, buttonSeat, sb, bb, ante: Number(ante) || 0, straddles: buyButton ? [] : straddleList, buyButton })
    );
    setEngHistory([]);
    setPhase("betting");
  }

  // Each engine transition snapshots the prior state so it can be undone.
  const act = (type, amount) => {
    setEngHistory((h) => [...h, eng]);
    setEng((s) => applyAction(s, { type, amount }));
  };
  const dealNextStreet = () => {
    setEngHistory((h) => [...h, eng]);
    setEng((s) => advanceStreet(s));
  };
  function undo() {
    if (!engHistory.length) return;
    setEng(engHistory[engHistory.length - 1]);
    setEngHistory((h) => h.slice(0, -1));
    if (phase === "complete") setPhase("betting");
  }

  // One click: validate the YouTube link, auto-generate the PT4 text, save it to
  // the session, flash confirmation, and advance to the next hand.
  async function completeHand() {
    setError("");
    // 1. YouTube link validation
    const link = youtubeLink.trim();
    if (!link) return setError("Add a timestamped YouTube link before completing.");
    if (!/youtube\.com|youtu\.be/i.test(link)) return setError("Enter a valid YouTube link (youtube.com or youtu.be).");
    if (!/[?&](?:t|start)=/.test(link)) return setError("This link has no timestamp. Add ?t=SECONDS to the URL.");
    // 2. Winner must be determinable — for a multiway showdown that means cards in.
    if (surv.length >= 2 && !autoEval) {
      return setError("Enter hole cards for every player still in so the winner can be scored.");
    }

    try {
      const effWinner = autoEval ? autoEval.winner : winner || surv[0];
      const effWinners = autoEval ? autoEval.winners : surv.length === 1 ? [surv[0]] : [winner || surv[0]];
      const hand = buildHandDict(eng, { stakes, holeCards, board, board2, rit: false, winner: effWinner, winner2, winners: effWinners, positions: buyButton ? {} : positions });
      if (buyButton) hand.buy_button_seat = buyButton.seat;
      const { url, startSec } = parseYouTube(link);
      if (startSec > 0) hand.timestamp_start = secsToHMS(startSec);
      if (url) hand.table_name = url; // timestamped link → PT4 table name

      const resp = await fetch("/api/format", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hand, hand_index: handNumber - 1, stream_url: url, start_sec: startSec, end_sec: startSec }),
      });
      const data = await resp.json();
      if (!resp.ok) return setError(data.error || `Server error ${resp.status}`);

      const n = handNumber;
      setSessionHands((hs) => [...hs.filter((h) => h.n !== n), { n, text: data.text }].sort((a, b) => a.n - b.n));
      setFlash(`Hand #${n} saved`);
      setTimeout(() => setFlash(""), 1800);
      nextHand(); // carries over stacks, rotates button, clears, increments hand #
    } catch (e) {
      setError(`Could not reach server: ${e.message}. Is server.py running on :8000?`);
    }
  }

  // Serialize the session's starting conditions so they can be restored after a
  // disconnect (table, stakes, ante, button, straddle, roster, hand #).
  function captureConfig() {
    const json = JSON.stringify(
      {
        stakes,
        ante: Number(ante),
        buttonSeat: Number(buttonSeat),
        straddleCount,
        handNumber,
        roster,
      },
      null,
      0
    );
    setConfigDraft(json);
    if (navigator.clipboard) navigator.clipboard.writeText(json).catch(() => {});
  }

  function restoreConfig() {
    try {
      const c = JSON.parse(configDraft);
      if (c.stakes) setStakes(c.stakes);
      if (c.ante != null) setAnte(c.ante);
      if (c.straddleCount != null) setStraddleCount(c.straddleCount);
      if (Array.isArray(c.roster)) setRoster(c.roster);
      if (c.buttonSeat != null) setButtonSeat(Number(c.buttonSeat));
      if (c.handNumber != null) setHandNumber(c.handNumber);
      setEng(null);
      setPhase("setup");
      setError("");
    } catch {
      setError("Couldn't parse that session JSON.");
    }
  }

  // Deliberately wipe the saved session and start fresh (guarded by a confirm).
  function newSession() {
    if (!window.confirm("Start a NEW session? This permanently clears the saved roster, stacks, and all accumulated hands.")) return;
    try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
    setStakes("50/100");
    setAnte(100);
    setButtonSeat(1);
    setStraddleCount(0);
    setBuyButton(null);
    setRoster(DEFAULT_ROSTER);
    setEng(null);
    setEngHistory([]);
    setHoleCards({});
    setBoard(["", "", "", "", ""]);
    setBoard2(["", "", "", "", ""]);
    setRit(false);
    setWinner("");
    setWinner2("");
    setHandNumber(1);
    setYoutubeLink("");
    setSessionHands([]);
    setPreview("");
    setEvalResult(null);
    setConfigDraft("");
    setError("");
    setPhase("setup");
  }

  function nextHand() {
    // Carry over ending stacks when the hand reached a result; players who bust
    // (≤ 0 chips) are stood up but their seat stays at the table.
    let newRoster = roster;
    if (eng) {
      const survList = eng.players.filter((p) => !p.folded);
      const resolved = survList.length === 1 || phase === "complete";
      if (resolved) {
        const ends = computeEndStacks(eng, { rit, winner: autoEval ? autoEval.winner : winner, winner2, winners: autoEval ? autoEval.winners : winner ? [winner] : [], holeCards, board });
        newRoster = roster.map((p) => {
          if (ends[p.seat] == null) return p;
          const s = ends[p.seat];
          return s <= 0 ? { ...p, name: "", stack: 0 } : { ...p, stack: s };
        });
      }
    }
    setRoster(newRoster);

    // Rotate the button to the next occupied seat of the (post-bust) roster.
    const occ = newRoster.filter((p) => p.name.trim()).sort((a, b) => a.seat - b.seat);
    if (occ.length) {
      const i = occ.findIndex((p) => p.seat === Number(buttonSeat));
      const nextSeat =
        i >= 0 ? occ[(i + 1) % occ.length].seat : (occ.find((p) => p.seat > Number(buttonSeat)) || occ[0]).seat;
      setButtonSeat(nextSeat);
    }

    setEng(null);
    setEngHistory([]);
    setHoleCards({});
    setBoard(["", "", "", "", ""]);
    setBoard2(["", "", "", "", ""]);
    setRit(false);
    setWinner("");
    setWinner2("");
    setPreview("");
    setError("");
    setYoutubeLink("");
    setBuyButton(null);
    setBuyMenuSeat(null);
    setPhase("setup");
    setHandNumber((h) => h + 1);
  }

  // Buy-the-button: the buyer becomes the button and posts the lone live blind.
  function buyTheButton(seat, type) {
    setBuyButton({ seat, type });
    setButtonSeat(seat);
    setStraddleCount(0); // a bought straddle/button replaces normal straddles
    setBuyMenuSeat(null);
  }

  // All session hands as one PT4 text block (each hand separated by 2 blank lines).
  const sessionText = sessionHands.map((h) => h.text.trimEnd()).join("\n\n\n") + (sessionHands.length ? "\n" : "");

  function downloadSession() {
    if (!sessionHands.length) return;
    const blob = new Blob([sessionText], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `session_${sessionHands.length}hands.txt`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function copyAllHands() {
    if (!sessionHands.length || !navigator.clipboard) return;
    navigator.clipboard.writeText(sessionText).then(
      () => { setCopiedAll(true); setTimeout(() => setCopiedAll(false), 1500); },
      () => {}
    );
  }

  // ── Layout positions ──────────────────────────────────────────────────────
  // Seats sit around a rounded-rectangle (racetrack) table, leaving a gap at the
  // bottom-centre for the dealer. A superellipse warps the angular sweep onto a
  // rectangular outline. Seat index 0 (lowest seat #) lands just left of the
  // dealer; the last seat lands just to its right.
  function ellipsePos(i, n) {
    const GAP = 30; // degrees reserved for the dealer cutout (centre of bottom edge)
    const arc = 360 - GAP;
    const startDeg = 90 + GAP / 2; // just left of the dealer (bottom edge)
    const t = n === 1 ? 0.5 : i / (n - 1);
    const ang = ((startDeg + t * arc) * Math.PI) / 180;
    const p = 0.6; // <1 → squares the ellipse into a rounded rectangle
    const c = Math.cos(ang);
    const s = Math.sin(ang);
    const sx = Math.sign(c) * Math.pow(Math.abs(c), p);
    const sy = Math.sign(s) * Math.pow(Math.abs(s), p);
    // Wide spread horizontally (47) vs vertically (40) for the widescreen oval.
    return { left: 50 + 47 * sx, top: 50 + 40 * sy };
  }

  const actorName = hasActor ? eng.players.find((p) => p.seat === eng.actorSeat).name : "";
  const seatOptions = named.map((p) => p.seat).sort((a, b) => a - b);

  // ── Render ──────────────────────────────────────────────────────────────
  return (
    <div style={styles.app}>
      {/* Sidebar */}
      <aside style={{ ...styles.sidebar, width: sidebarOpen ? 280 : 0, padding: sidebarOpen ? "20px 16px" : 0 }}>
        {sidebarOpen && (
          <div style={styles.sidebarInner}>
            <div style={styles.sideHead}>SESSION</div>
            <div style={styles.row}>
              <div style={styles.col}>
                <label style={styles.label}>Stakes</label>
                <input style={styles.input} value={stakes} onChange={(e) => setStakes(e.target.value)} disabled={locked} />
              </div>
              <div style={styles.col}>
                <label style={styles.label}>BB Ante</label>
                <input style={styles.input} type="number" value={ante} onChange={(e) => setAnte(e.target.value)} disabled={locked} />
              </div>
            </div>

            <div style={styles.row}>
              <div style={styles.col}>
                <label style={styles.label}>Button Seat</label>
                <select style={styles.input} value={buttonSeat} onChange={(e) => setButtonSeat(Number(e.target.value))} disabled={locked}>
                  {seatOptions.map((s) => (<option key={s} value={s}>Seat {s}</option>))}
                </select>
              </div>
            </div>

            <label style={{ ...styles.checkRow, marginTop: 4 }}>
              <input type="checkbox" checked={straddleCount > 0} disabled={locked} onChange={(e) => setStraddleCount(e.target.checked ? 1 : 0)} />
              <span>UTG straddle (2× BB)</span>
            </label>
            {straddleCount > 0 && (
              <div style={styles.straddleBox}>
                <div style={styles.straddleRow}>
                  <span style={styles.label}>Straddlers</span>
                  <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <button style={styles.stepBtn} disabled={locked || straddleCount <= 1} onClick={() => setStraddleCount((c) => Math.max(1, c - 1))}>−</button>
                    <span style={{ minWidth: 16, textAlign: "center", fontWeight: 700 }}>{straddleCount}</span>
                    <button style={styles.stepBtn} disabled={locked} onClick={() => setStraddleCount((c) => c + 1)}>+</button>
                  </div>
                </div>
                <div style={styles.straddlePreview}>
                  {straddleList.length
                    ? straddleList.map((st, i) => {
                        const nm = named.find((p) => p.seat === st.seat)?.name || `Seat ${st.seat}`;
                        return `${i === 0 ? "UTG" : `UTG+${i}`} ${nm}: $${st.amount}`;
                      }).join("  ·  ")
                    : "not enough players to straddle"}
                </div>
              </div>
            )}

            <div style={{ ...styles.sideHead, marginTop: 16 }}>ROSTER <span style={styles.sideHint}>· blank name = empty seat</span></div>
            {[...roster].sort((a, b) => a.seat - b.seat).map((p) => (
              <div key={p.seat} style={styles.rosterRow}>
                <span style={styles.seatTag}>{p.seat}</span>
                <input style={{ ...styles.input, flex: 1 }} value={p.name} placeholder="(empty)" onChange={(e) => updateRoster(p.seat, { name: e.target.value })} disabled={locked} />
                <input style={{ ...styles.input, width: 74 }} type="number" value={p.stack} onChange={(e) => updateRoster(p.seat, { stack: Number(e.target.value) })} disabled={locked} />
                {!locked && p.name.trim() && <button style={styles.miniX} title="Stand up (keep seat)" onClick={() => emptySeat(p.seat)}>⏏</button>}
                {!locked && <button style={styles.miniX} title="Remove seat from table" onClick={() => removeSeat(p.seat)}>✕</button>}
              </div>
            ))}
            {!locked && roster.length < 9 && (
              <button style={styles.addBtn} onClick={addSeat}>+ Add seat</button>
            )}
            {locked && <div style={styles.lockNote}>Session locked during betting — Reset Hand to edit.</div>}

            <div style={{ ...styles.sideHead, marginTop: 16 }}>SAVE / RESTORE <span style={styles.sideHint}>· back up the table</span></div>
            <div style={styles.autosaveNote}>✓ Auto-saved — survives reloads &amp; refreshes</div>
            <textarea
              style={styles.configBox}
              value={configDraft}
              onChange={(e) => setConfigDraft(e.target.value)}
              placeholder="Click Capture to copy this session's setup, or paste a saved setup here and click Restore."
              spellCheck={false}
            />
            <div style={{ display: "flex", gap: 6 }}>
              <button style={{ ...styles.addBtn, flex: 1, marginTop: 0 }} onClick={captureConfig}>⧉ Capture + copy</button>
              <button style={{ ...styles.addBtn, flex: 1, marginTop: 0 }} onClick={restoreConfig} disabled={!configDraft.trim()}>⤴ Restore</button>
            </div>
            <button style={{ ...styles.addBtn, marginTop: 6, color: "#fca5a5", borderColor: "#7f1d1d" }} onClick={newSession}>⟲ New session (clear saved)</button>
          </div>
        )}
      </aside>

      <button style={{ ...styles.sidebarToggle, left: sidebarOpen ? 280 : 0 }} onClick={() => setSidebarOpen((o) => !o)}>
        {sidebarOpen ? "‹" : "›"}
      </button>

      {/* Main area */}
      <main style={styles.main}>
        <div style={styles.topBar}>
          <div style={styles.logo}><span style={styles.chip}>♠</span> POKER TABLE</div>
          <div style={styles.ytWrap}>
            <span style={styles.ytIcon}>📺</span>
            <input
              style={styles.ytInput}
              value={youtubeLink}
              onChange={(e) => setYoutubeLink(e.target.value)}
              placeholder="Paste timestamped YouTube link for this hand…"
            />
            {(() => {
              const { url, id, startSec } = parseYouTube(youtubeLink);
              if (!url) return null;
              return (
                <a href={url} target="_blank" rel="noreferrer" style={styles.ytParsed} title={url}>
                  {(id || "link")}{startSec > 0 ? ` @ ${secsToHMS(startSec)}` : ""} ↗
                </a>
              );
            })()}
          </div>
          <div style={styles.topMeta}>
            <span style={styles.statTracker}>
              Hands: <strong style={{ color: "#f8fafc" }}>{sessionHands.length}</strong>
              {"  ·  "}Earnings: <strong style={{ color: "#4ade80" }}>${(sessionHands.length * 0.4).toFixed(2)}</strong>
            </span>
            <span>{stakes} {Number(ante) > 0 ? `· $${ante} ante` : ""}</span>
            {phase !== "setup" && (
              <>
                <button style={styles.topBtn} title="Restart this hand (same players & stacks)" onClick={resetHand}>↺ Reset Hand</button>
                <button style={styles.topBtn} title="Next hand (carry over stacks, rotate button)" onClick={nextHand}>Next Hand ↻</button>
              </>
            )}
            {sessionHands.length > 0 && (
              <button style={styles.sessionBtn} title="View, copy or download every hand this session" onClick={() => setShowHistory(true)}>
                📋 View All Hands ({sessionHands.length})
              </button>
            )}
            <span style={styles.handTag}>Hand #{handNumber}</span>
          </div>
        </div>

        {/* Table */}
        <div style={styles.tableWrap}>
          <div style={styles.rail}>
            <div style={styles.felt}>
              <div style={styles.bettingLine} />
              <div style={styles.feltBrand}>♠<div style={styles.feltBrandText}>POKER ROOM</div></div>

              {/* Pot */}
              <div style={styles.potArea}>
                <div style={styles.potLabel}>POT</div>
                <div style={styles.potValue}>{fmtChips(potTotal(eng))}</div>
              </div>

              {/* Community cards */}
              <div style={styles.boardArea}>
                {rit && <div style={styles.runLabel}>RUN 1</div>}
                <div style={styles.boardRow}>
                  {[0, 1, 2, 3, 4].map((i) => (
                    <Card key={i} card={board[i]} size="lg" faceDownIfEmpty={false} onClick={() => openBoard(i)} />
                  ))}
                </div>
                {rit && (
                  <>
                    <div style={styles.runLabel}>RUN 2</div>
                    <div style={styles.boardRow}>
                      {[0, 1, 2, 3, 4].map((i) => (
                        <Card key={i} card={board2[i]} size="lg" faceDownIfEmpty={false} onClick={() => openBoard2(i)} />
                      ))}
                    </div>
                  </>
                )}
              </div>
            </div>

            {/* Dealer cutout: concave notch + chip tray */}
            <div style={styles.dealerCut} />
            <div style={styles.chipTray}>
              {["#dc2626", "#2563eb", "#16a34a", "#0a0e17", "#f59e0b"].map((c, i) => (
                <div key={i} style={{ ...styles.chipWell }}>
                  <div style={{ ...styles.chipStack, background: c, borderColor: c === "#0a0e17" ? "#475569" : "#ffffff55" }} />
                </div>
              ))}
            </div>
            <div style={styles.dealerLabel}>DEALER</div>
          </div>

          {/* Seats — every physical seat, including empty ones */}
          {tableSeats.map((p, i) => {
            const enginePlayer = eng?.players.find((e) => e.seat === p.seat);
            return (
              <Seat
                key={p.seat}
                p={p}
                empty={p.empty}
                pos={ellipsePos(i, tableSeats.length)}
                badge={buyButton ? (p.seat === buyButton.seat ? "BTN" : "") : straddleList.some((st) => st.seat === p.seat) ? "STR" : positions[p.seat]}
                isButton={Number(buttonSeat) === p.seat}
                isActor={hasActor && eng.actorSeat === p.seat}
                folded={enginePlayer?.folded}
                committed={enginePlayer?.committedStreet || 0}
                cards={holeCards[p.seat] || ["", ""]}
                desc={autoEval?.descriptions?.[p.name]}
                isWinner={autoEval && autoEval.winners.includes(p.name)}
                bought={buyButton && buyButton.seat === p.seat ? buyButton.type : null}
                onNameClick={!locked && !p.empty ? () => setBuyMenuSeat(p.seat) : undefined}
                phase={phase}
                sitting={sitSeat === p.seat}
                sitValue={sitDraft}
                onSit={() => {
                  if (locked) return; // can't sit down once betting starts
                  setSitSeat(p.seat);
                  setSitDraft("");
                }}
                onSitChange={setSitDraft}
                onSitCommit={() => {
                  if (sitDraft.trim()) updateRoster(p.seat, { name: sitDraft.trim() });
                  setSitSeat(null);
                }}
                onCardClick={(idx) => openHole(p.seat, idx)}
                onStackClick={() => {
                  if (locked) return; // stacks are engine-controlled once betting starts
                  setEditingStack(p.seat);
                  setStackDraft(String(p.stack));
                }}
                editingStack={editingStack === p.seat}
                stackValue={stackDraft}
                onStackChange={setStackDraft}
                onStackCommit={() => {
                  updateRoster(p.seat, { stack: Number(stackDraft) || 0 });
                  setEditingStack(null);
                }}
              />
            );
          })}
        </div>

        {/* Action bar */}
        <div style={styles.actionBar}>
          {(phase === "betting" || phase === "complete") && engHistory.length > 0 && (
            <button style={styles.undoBtn} title="Undo last action" onClick={undo}>↶ Undo</button>
          )}
          {error && <div style={styles.errorInline}>⚠ {error}</div>}

          {phase === "setup" && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>{named.length} players seated · button on Seat {buttonSeat}</div>
              <button style={styles.dealBtn} onClick={dealHand}>♠ DEAL HAND</button>
            </div>
          )}

          {phase === "holecards" && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>Set hole cards (optional) and adjust the button / stacks if needed, then start.</div>
              <button style={styles.dealBtn} onClick={startBetting}>START BETTING ▶</button>
            </div>
          )}

          {inBetting && hasActor && legal && (
            <div style={styles.actionRow}>
              <div style={styles.actorTag}>
                <div style={styles.actorName}>{actorName} to act</div>
                <div style={styles.actorSub}>{STREET_LABEL[eng.street]} · facing {fmtChips(legal.facing)} · stack {fmtChips(legal.player.stack)}</div>
              </div>
              <button style={{ ...styles.actBtn, ...styles.foldBtn }} onClick={() => act("fold")}>FOLD</button>
              {legal.canCheck ? (
                <button style={{ ...styles.actBtn, ...styles.checkBtn }} onClick={() => act("check")}>CHECK</button>
              ) : (
                <button style={{ ...styles.actBtn, ...styles.checkBtn }} onClick={() => act("call")}>
                  CALL {fmtChips(legal.callAmount)}{legal.callIsAllin ? " (ALL-IN)" : ""}
                </button>
              )}

              {(legal.canBet || legal.canRaise) && (
                <div style={styles.raiseGroup}>
                  <div style={styles.sizeBtns}>
                    {legal.canBet ? (
                      <>
                        <button style={styles.sizeBtn} onClick={() => setBetTo(Math.max(legal.minRaiseTo, Math.round(potTotal(eng) * 0.5)))}>½ pot</button>
                        <button style={styles.sizeBtn} onClick={() => setBetTo(Math.max(legal.minRaiseTo, Math.round(potTotal(eng) * 0.75)))}>¾ pot</button>
                        <button style={styles.sizeBtn} onClick={() => setBetTo(Math.max(legal.minRaiseTo, potTotal(eng)))}>pot</button>
                      </>
                    ) : (
                      <>
                        <button style={styles.sizeBtn} onClick={() => setBetTo(legal.minRaiseTo)}>min</button>
                        <button style={styles.sizeBtn} onClick={() => setBetTo(Math.min(legal.maxTo, legal.facing + potTotal(eng)))}>pot</button>
                      </>
                    )}
                  </div>
                  <input
                    style={styles.betInput}
                    type="number"
                    value={betTo}
                    min={legal.minRaiseTo}
                    max={legal.maxTo}
                    onChange={(e) => setBetTo(Number(e.target.value))}
                  />
                  <input
                    style={styles.slider}
                    type="range"
                    min={legal.minRaiseTo}
                    max={legal.maxTo}
                    value={Math.min(Math.max(betTo, legal.minRaiseTo), legal.maxTo)}
                    onChange={(e) => setBetTo(Number(e.target.value))}
                  />
                  <button
                    style={{ ...styles.actBtn, ...styles.raiseBtn }}
                    onClick={() => {
                      const to = Math.min(Math.max(betTo, legal.minRaiseTo), legal.maxTo);
                      act(legal.canBet ? "bet" : "raise", to);
                    }}
                  >
                    {legal.canBet ? "BET" : "RAISE TO"} {fmtChips(Math.min(Math.max(betTo, legal.minRaiseTo), legal.maxTo))}
                  </button>
                </div>
              )}
              <button style={{ ...styles.actBtn, ...styles.allinBtn }} onClick={() => act("allin")}>ALL-IN</button>
            </div>
          )}

          {needBoard && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>
                {eng.bettingClosed ? "All-in — set the " : "Betting complete — set the "}
                <strong>{STREET_LABEL[nextStreet]}</strong> on the table, then deal.
              </div>
              <button
                style={{ ...styles.dealBtn, opacity: boardReadyFor(nextStreet) ? 1 : 0.4 }}
                disabled={!boardReadyFor(nextStreet)}
                onClick={dealNextStreet}
              >
                DEAL {STREET_LABEL[nextStreet].toUpperCase()} ▶
              </button>
            </div>
          )}

          {canComplete && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>
                {eng.handOver ? `${surv[0]} wins uncontested.` : `Showdown — ${surv.join(", ")}`}
              </div>
              <button style={styles.completeBtn} onClick={completeHand}>✓ COMPLETE HAND</button>
            </div>
          )}
        </div>
      </main>

      {/* Card picker */}
      {editor && (
        <CardPicker
          used={usedCards}
          onPick={pickCard}
          onClose={() => setEditor(null)}
          title={
            editor.kind === "hole"
              ? `Hole card ${editor.group.indexOf(editor.cur) + 1} of ${editor.group.length}`
              : editor.group.length > 1
              ? `Flop card ${editor.group.indexOf(editor.cur) + 1} of ${editor.group.length}`
              : "Choose card"
          }
        />
      )}

      {/* Buy-the-button menu */}
      {buyMenuSeat != null && (() => {
        const p = named.find((x) => x.seat === buyMenuSeat);
        const isBuyer = buyButton && buyButton.seat === buyMenuSeat;
        return (
          <div style={styles.pickerOverlay} onClick={() => setBuyMenuSeat(null)}>
            <div style={styles.buyBox} onClick={(e) => e.stopPropagation()}>
              <div style={styles.pickerTitle}>{p ? p.name : `Seat ${buyMenuSeat}`} — buy the button</div>
              <div style={styles.buyNote}>
                Posts a single live blind + the other blinds/ante as dead money; action starts to their left.
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button style={{ ...styles.buyBtn, background: buyButton?.type === "btn" && isBuyer ? "#16a34a" : "#0e7490" }} onClick={() => buyTheButton(buyMenuSeat, "btn")}>
                  Buy the BTN<div style={styles.buySub}>BB live · SB+ante dead</div>
                </button>
                <button style={{ ...styles.buyBtn, background: buyButton?.type === "str" && isBuyer ? "#16a34a" : "#7c3aed" }} onClick={() => buyTheButton(buyMenuSeat, "str")}>
                  Buy the STR<div style={styles.buySub}>2× BB live · SB+BB+ante dead</div>
                </button>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}>
                {isBuyer ? (
                  <button style={styles.pickerClear} onClick={() => { setBuyButton(null); setBuyMenuSeat(null); }}>Clear</button>
                ) : <span />}
                <button style={styles.pickerClear} onClick={() => setBuyMenuSeat(null)}>Close</button>
              </div>
            </div>
          </div>
        );
      })()}

      {/* Transient "Hand #N saved" confirmation */}
      {flash && <div style={styles.flashToast}>✓ {flash}</div>}

      {/* Session history — full-screen view of every hand this session */}
      {showHistory && (
        <div style={styles.historyOverlay}>
          <div style={styles.historyHead}>
            <span style={styles.historyTitle}>Session History — {sessionHands.length} hand{sessionHands.length === 1 ? "" : "s"}</span>
            <div style={{ display: "flex", gap: 10 }}>
              <button style={styles.histBtn} disabled={!sessionHands.length} onClick={copyAllHands}>{copiedAll ? "✓ Copied!" : "⧉ Copy All"}</button>
              <button style={{ ...styles.histBtn, background: "#22c55e", color: "#06210f", borderColor: "#22c55e" }} disabled={!sessionHands.length} onClick={downloadSession}>↓ Download All</button>
              <button style={styles.histBtn} onClick={() => setShowHistory(false)}>✕ Close</button>
            </div>
          </div>
          <pre style={styles.historyText}>{sessionText || "No hands generated yet — generate a hand to start the session."}</pre>
        </div>
      )}
    </div>
  );
}

// ── Styles ──────────────────────────────────────────────────────────────────
const FELT = "radial-gradient(ellipse at center, #1b6b43 0%, #0f4d2f 70%, #0a3a23 100%)";
const styles = {
  app: { display: "flex", minHeight: "100vh", background: "#070b12", color: "#e2e8f0", fontFamily: "'Inter','Segoe UI',system-ui,sans-serif" },
  sidebar: { background: "#0d1320", borderRight: "1px solid #1e293b", overflow: "hidden", transition: "width .18s ease", flexShrink: 0, boxSizing: "border-box" },
  sidebarInner: { display: "flex", flexDirection: "column", gap: 10 },
  sideHead: { fontSize: 11, fontWeight: 800, letterSpacing: 2, color: "#f59e0b" },
  sideHint: { fontSize: 9, fontWeight: 500, letterSpacing: 0.5, color: "#475569" },
  sidebarToggle: { position: "fixed", top: 14, zIndex: 30, width: 22, height: 44, background: "#1e293b", color: "#cbd5e1", border: "1px solid #334155", borderRadius: "0 8px 8px 0", cursor: "pointer", transition: "left .18s ease" },
  row: { display: "flex", gap: 8 },
  col: { flex: 1, display: "flex", flexDirection: "column", gap: 4 },
  label: { fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: 1, color: "#64748b" },
  input: { padding: "7px 9px", background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 7, color: "#e2e8f0", fontSize: 13, fontFamily: "inherit", outline: "none", boxSizing: "border-box", width: "100%" },
  rosterRow: { display: "flex", gap: 6, alignItems: "center" },
  seatTag: { width: 20, textAlign: "center", fontSize: 11, color: "#64748b", fontWeight: 700 },
  miniX: { width: 24, height: 24, background: "#1e293b", border: "none", borderRadius: 5, color: "#94a3b8", cursor: "pointer", fontSize: 11 },
  addBtn: { marginTop: 4, padding: "7px", background: "#1e293b", border: "1px solid #334155", borderRadius: 7, color: "#cbd5e1", fontSize: 12, cursor: "pointer" },
  lockNote: { marginTop: 8, fontSize: 11, color: "#64748b", fontStyle: "italic" },
  configBox: { width: "100%", height: 64, boxSizing: "border-box", padding: "7px 9px", background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 7, color: "#94a3b8", fontSize: 10.5, fontFamily: "'JetBrains Mono',monospace", outline: "none", resize: "vertical", marginBottom: 6 },
  autosaveNote: { fontSize: 10, color: "#4ade80", marginBottom: 6 },
  straddleBox: { background: "#0d1320", border: "1px solid #1e293b", borderRadius: 8, padding: "8px 10px", display: "flex", flexDirection: "column", gap: 6 },
  straddleRow: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  stepBtn: { width: 24, height: 24, background: "#1e293b", border: "1px solid #334155", borderRadius: 6, color: "#cbd5e1", fontSize: 14, fontWeight: 700, cursor: "pointer", lineHeight: 1 },
  straddlePreview: { fontSize: 10.5, color: "#fbbf24", lineHeight: 1.4 },

  main: { flex: 1, display: "flex", flexDirection: "column", minWidth: 0 },
  topBar: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 22px 12px 34px", borderBottom: "1px solid #141e2e" },
  logo: { display: "flex", alignItems: "center", gap: 10, fontWeight: 800, letterSpacing: 2, fontSize: 16 },
  chip: { display: "inline-flex", width: 28, height: 28, borderRadius: "50%", background: "linear-gradient(135deg,#f59e0b,#d97706)", color: "#0a0e17", alignItems: "center", justifyContent: "center", fontSize: 16 },
  ytWrap: { display: "flex", alignItems: "center", gap: 8, flex: 1, maxWidth: 460, margin: "0 18px", background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 8, padding: "5px 10px" },
  ytIcon: { fontSize: 14, opacity: 0.8 },
  ytInput: { flex: 1, background: "transparent", border: "none", outline: "none", color: "#e2e8f0", fontSize: 12.5, fontFamily: "inherit", minWidth: 0 },
  ytParsed: { fontSize: 10.5, color: "#4ade80", whiteSpace: "nowrap", fontWeight: 600, textDecoration: "none", cursor: "pointer" },
  topMeta: { display: "flex", gap: 12, alignItems: "center", fontSize: 13, color: "#94a3b8" },
  topBtn: { padding: "6px 12px", background: "#16243a", border: "1px solid #2b3a52", borderRadius: 7, color: "#cbd5e1", fontSize: 12, fontWeight: 700, cursor: "pointer", whiteSpace: "nowrap" },
  sessionBtn: { padding: "6px 12px", background: "#0e7490", border: "1px solid #155e75", borderRadius: 7, color: "#e0f2fe", fontSize: 12, fontWeight: 700, cursor: "pointer", whiteSpace: "nowrap" },
  statTracker: { background: "#0d1320", border: "1px solid #1e293b", borderRadius: 8, padding: "6px 12px", fontSize: 13, color: "#94a3b8", whiteSpace: "nowrap" },
  flashToast: { position: "fixed", top: 64, left: "50%", transform: "translateX(-50%)", zIndex: 80, background: "#16a34a", color: "#f0fdf4", fontWeight: 800, fontSize: 15, letterSpacing: 0.5, padding: "12px 28px", borderRadius: 10, boxShadow: "0 8px 30px rgba(34,197,94,.5)" },
  historyOverlay: { position: "fixed", inset: 0, background: "#070b12", zIndex: 60, display: "flex", flexDirection: "column" },
  historyHead: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 22px", borderBottom: "1px solid #1e293b", flexShrink: 0 },
  historyTitle: { fontSize: 16, fontWeight: 800, letterSpacing: 0.5, color: "#f8fafc" },
  histBtn: { padding: "9px 16px", background: "#16243a", border: "1px solid #2b3a52", borderRadius: 8, color: "#e2e8f0", fontSize: 13, fontWeight: 700, cursor: "pointer", whiteSpace: "nowrap" },
  historyText: { flex: 1, margin: 0, padding: "18px 24px", overflowY: "auto", background: "#06090f", color: "#a5d6a7", fontSize: 12.5, lineHeight: 1.55, fontFamily: "'JetBrains Mono',monospace", whiteSpace: "pre-wrap", wordBreak: "break-word" },
  handTag: { background: "#f59e0b22", color: "#f59e0b", padding: "4px 10px", borderRadius: 20, fontWeight: 700, fontSize: 12 },

  tableWrap: { position: "relative", flex: 1, margin: "12px 16px 6px", minHeight: 420 },
  rail: {
    // Wide horizontal oval filling the available width (widescreen, ~2:1+).
    position: "absolute", left: "0.5%", top: "5%", width: "99%", height: "90%", borderRadius: 9999,
    background: "linear-gradient(160deg,#6b4423 0%,#4a2f18 45%,#2e1d0e 100%)",
    boxShadow: "0 26px 64px rgba(0,0,0,.65), inset 0 3px 8px rgba(255,255,255,.10), inset 0 -8px 24px rgba(0,0,0,.55)",
    border: "1px solid #160d05",
  },
  felt: {
    position: "absolute", inset: 26, borderRadius: 9999, background: FELT,
    boxShadow: "inset 0 0 80px rgba(0,0,0,.6), inset 0 0 0 3px rgba(0,0,0,.35)", overflow: "hidden",
  },
  bettingLine: { position: "absolute", inset: "20% 14%", borderRadius: 9999, border: "2px solid rgba(255,255,255,.10)" },
  feltBrand: { position: "absolute", left: "50%", top: "50%", transform: "translate(-50%,-50%)", fontSize: 64, color: "rgba(255,255,255,.05)", textAlign: "center", pointerEvents: "none", fontWeight: 800, lineHeight: 1 },
  feltBrandText: { fontSize: 13, letterSpacing: 6, marginTop: 2 },
  dealerCut: {
    position: "absolute", left: "50%", bottom: "-1%", transform: "translateX(-50%)",
    width: "30%", maxWidth: 330, height: "21%", minHeight: 66,
    background: "#070b12", borderRadius: "130px 130px 0 0",
    border: "6px solid #2a1c10", borderBottom: "none",
    boxShadow: "inset 0 10px 22px rgba(0,0,0,.7)", zIndex: 3,
  },
  chipTray: {
    position: "absolute", left: "50%", bottom: "16%", transform: "translateX(-50%)",
    display: "flex", gap: 5, padding: "5px 8px", borderRadius: 9, zIndex: 5,
    background: "linear-gradient(#1c1308,#0d0a05)", border: "1px solid #3a2a16",
    boxShadow: "0 5px 12px rgba(0,0,0,.6)",
  },
  chipWell: { width: 20, height: 26, borderRadius: 4, background: "#080604", display: "flex", alignItems: "flex-end", justifyContent: "center", padding: 2, boxShadow: "inset 0 2px 5px rgba(0,0,0,.85)" },
  chipStack: { width: 18, height: 18, borderRadius: "50%", border: "2px solid", boxShadow: "0 -3px 0 rgba(255,255,255,.18), 0 -6px 0 rgba(0,0,0,.25)" },
  dealerLabel: { position: "absolute", left: "50%", bottom: "5.5%", transform: "translateX(-50%)", fontSize: 9, letterSpacing: 3, color: "#8aa0b8", fontWeight: 800, zIndex: 5 },
  potArea: { position: "absolute", left: "50%", top: "30%", transform: "translate(-50%,-50%)", textAlign: "center" },
  potLabel: { fontSize: 10, letterSpacing: 2, color: "#cbe8d5", opacity: 0.7, fontWeight: 700 },
  potValue: { fontSize: 22, fontWeight: 800, color: "#fde68a", textShadow: "0 2px 6px rgba(0,0,0,.5)" },
  boardArea: { position: "absolute", left: "50%", top: "52%", transform: "translate(-50%,-50%)", display: "flex", flexDirection: "column", alignItems: "center", gap: 4 },
  runLabel: { fontSize: 9, letterSpacing: 2, color: "#cbe8d5", opacity: 0.65, fontWeight: 700 },
  boardRow: { display: "flex", gap: 12 },

  seat: { position: "absolute", transform: "translate(-50%,-50%)", zIndex: 5 },
  emptySeat: { width: 96, padding: "10px 8px", textAlign: "center", borderRadius: 12, border: "2px dashed #2b3a52", background: "rgba(10,15,26,.5)", display: "flex", flexDirection: "column", alignItems: "center", gap: 6 },
  emptySeatNum: { fontSize: 10, fontWeight: 700, letterSpacing: 1.5, color: "#475569" },
  sitBtn: { padding: "5px 12px", background: "#16243a", border: "1px solid #2b3a52", borderRadius: 7, color: "#7dd3fc", fontSize: 11, fontWeight: 700, cursor: "pointer" },
  sitInput: { width: 80, padding: "4px 6px", background: "#0a0f1a", border: "1px solid #334155", borderRadius: 6, color: "#e2e8f0", fontSize: 12, textAlign: "center", outline: "none" },
  seatBox: { position: "relative", width: 124, background: "#0e1626", border: "1px solid #1e293b", borderRadius: 12, padding: "8px 8px 6px", textAlign: "center", transition: "box-shadow .15s" },
  cardsRow: { display: "flex", gap: 4, justifyContent: "center", marginBottom: 5 },
  seatName: { fontSize: 13, fontWeight: 700, color: "#f1f5f9", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" },
  seatStack: { fontSize: 13, fontWeight: 700, color: "#4ade80", cursor: "pointer", marginTop: 1 },
  handDesc: { fontSize: 10, fontWeight: 600, marginTop: 3, lineHeight: 1.2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" },
  boughtTag: { marginTop: 3, fontSize: 9, fontWeight: 800, letterSpacing: 0.5, color: "#0a0e17", background: "#fbbf24", borderRadius: 8, padding: "1px 6px", display: "inline-block" },
  buyBox: { background: "#0e1626", border: "1px solid #334155", borderRadius: 12, padding: 18, maxWidth: 420, boxShadow: "0 20px 60px rgba(0,0,0,.6)" },
  buyNote: { fontSize: 11, color: "#94a3b8", marginBottom: 12, lineHeight: 1.4 },
  buyBtn: { flex: 1, padding: "12px 10px", border: "none", borderRadius: 9, color: "#fff", fontSize: 14, fontWeight: 800, cursor: "pointer", letterSpacing: 0.5 },
  buySub: { fontSize: 9.5, fontWeight: 500, opacity: 0.85, marginTop: 3, letterSpacing: 0 },
  stackInput: { width: 80, marginTop: 2, padding: "2px 6px", background: "#0a0f1a", border: "1px solid #334155", borderRadius: 6, color: "#4ade80", fontSize: 13, textAlign: "center", outline: "none" },
  badge: { position: "absolute", top: -9, left: -9, fontSize: 9, fontWeight: 800, color: "#0a0e17", padding: "2px 6px", borderRadius: 10, letterSpacing: 0.5 },
  dealerBtn: { position: "absolute", bottom: -8, right: -8, width: 24, height: 24, borderRadius: "50%", background: "radial-gradient(circle at 35% 30%,#ffffff,#cbd5e1)", color: "#0a0e17", fontSize: 12, fontWeight: 800, display: "flex", alignItems: "center", justifyContent: "center", border: "1px solid #94a3b8", boxShadow: "0 2px 6px rgba(0,0,0,.5)" },
  betChip: { position: "absolute", left: "50%", top: "108%", transform: "translateX(-50%)", background: "#0a0e17cc", border: "1px solid #f59e0b66", color: "#fde68a", fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 12, whiteSpace: "nowrap" },

  actionBar: { position: "relative", minHeight: 92, borderTop: "1px solid #141e2e", background: "#0a0f19", padding: "12px 24px", display: "flex", alignItems: "center", justifyContent: "center" },
  undoBtn: { position: "absolute", left: 20, top: "50%", transform: "translateY(-50%)", padding: "9px 14px", background: "#1e293b", border: "1px solid #475569", borderRadius: 8, color: "#e2e8f0", fontSize: 13, fontWeight: 700, cursor: "pointer" },
  barCenter: { display: "flex", flexDirection: "column", alignItems: "center", gap: 10 },
  barHint: { fontSize: 13, color: "#94a3b8" },
  dealBtn: { padding: "13px 40px", background: "linear-gradient(135deg,#f59e0b,#d97706)", color: "#0a0e17", border: "none", borderRadius: 10, fontSize: 15, fontWeight: 800, letterSpacing: 1.5, cursor: "pointer", boxShadow: "0 0 26px #f59e0b44" },
  completeBtn: { padding: "13px 40px", background: "linear-gradient(135deg,#22c55e,#16a34a)", color: "#06210f", border: "none", borderRadius: 10, fontSize: 15, fontWeight: 800, letterSpacing: 1.5, cursor: "pointer", boxShadow: "0 0 26px #22c55e44" },
  actionRow: { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", justifyContent: "center" },
  actorTag: { textAlign: "right", marginRight: 6 },
  actorName: { fontSize: 14, fontWeight: 800, color: "#fde68a" },
  actorSub: { fontSize: 11, color: "#64748b" },
  actBtn: { padding: "12px 18px", border: "none", borderRadius: 9, fontSize: 13, fontWeight: 800, letterSpacing: 0.5, cursor: "pointer", color: "#fff", whiteSpace: "nowrap" },
  foldBtn: { background: "#b91c1c" },
  checkBtn: { background: "#0e7490" },
  raiseBtn: { background: "linear-gradient(135deg,#f59e0b,#d97706)", color: "#0a0e17" },
  allinBtn: { background: "#7c3aed" },
  raiseGroup: { display: "flex", alignItems: "center", gap: 8, background: "#0e1626", border: "1px solid #1e293b", borderRadius: 10, padding: "6px 8px" },
  sizeBtns: { display: "flex", gap: 4 },
  sizeBtn: { padding: "5px 8px", background: "#1e293b", border: "1px solid #334155", borderRadius: 6, color: "#cbd5e1", fontSize: 11, cursor: "pointer" },
  betInput: { width: 92, padding: "8px", background: "#0a0f1a", border: "1px solid #334155", borderRadius: 7, color: "#fde68a", fontSize: 13, fontWeight: 700, textAlign: "center", outline: "none" },
  slider: { width: 120, accentColor: "#f59e0b" },

  errorInline: { color: "#fca5a5", fontSize: 13, marginBottom: 8 },

  pickerOverlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,.6)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50 },
  pickerBox: { background: "#0e1626", border: "1px solid #334155", borderRadius: 12, padding: 16, boxShadow: "0 20px 60px rgba(0,0,0,.6)" },
  pickerTitle: { fontSize: 12, fontWeight: 700, letterSpacing: 1, color: "#94a3b8", marginBottom: 10, textTransform: "uppercase" },
  pickerCard: { width: 38, height: 34, background: "#f8fafc", border: "1px solid #cbd5e1", borderRadius: 5, fontSize: 12, fontWeight: 800, padding: 0 },
  pickerClear: { padding: "7px 16px", background: "#1e293b", border: "1px solid #334155", borderRadius: 7, color: "#cbd5e1", fontSize: 12, cursor: "pointer" },

  modalOverlay: { position: "fixed", inset: 0, background: "rgba(3,6,12,.78)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 40, padding: 20 },
  modal: { width: 560, maxWidth: "100%", maxHeight: "90vh", overflowY: "auto", background: "#0d1320", border: "1px solid #1e293b", borderRadius: 14, padding: 22, display: "flex", flexDirection: "column", gap: 12, boxShadow: "0 30px 80px rgba(0,0,0,.6)" },
  modalHead: { display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 15, fontWeight: 800, color: "#f8fafc" },
  checkRow: { display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "#cbd5e1", cursor: "pointer" },
  genBtn: { padding: "12px", background: "linear-gradient(135deg,#f59e0b,#d97706)", color: "#0a0e17", border: "none", borderRadius: 9, fontSize: 14, fontWeight: 800, letterSpacing: 1, cursor: "pointer" },
  preview: { margin: 0, padding: 14, background: "#06090f", color: "#a5d6a7", fontSize: 12, lineHeight: 1.6, fontFamily: "'JetBrains Mono',monospace", whiteSpace: "pre-wrap", wordBreak: "break-word", borderRadius: 8, border: "1px solid #1e293b", maxHeight: 320, overflowY: "auto", minHeight: 140 },
  modalBtns: { display: "flex", gap: 10 },
  modalNote: { fontSize: 11, color: "#64748b", textAlign: "center" },
  potBreakdown: { background: "#0e1626", border: "1px solid #1e293b", borderRadius: 9, padding: "10px 12px", display: "flex", flexDirection: "column", gap: 6 },
  potRow: { display: "flex", justifyContent: "space-between", fontSize: 13, color: "#e2e8f0" },
  potWinner: { color: "#4ade80", fontWeight: 700 },
  chopTag: { color: "#fbbf24", fontWeight: 800, letterSpacing: 1 },
  dlBtn: { flex: 1, padding: 12, background: "#22c55e", color: "#06210f", border: "none", borderRadius: 8, fontSize: 13, fontWeight: 800, letterSpacing: 1, cursor: "pointer" },
  nextBtn: { flex: 1, padding: 12, background: "#1e293b", color: "#e2e8f0", border: "1px solid #334155", borderRadius: 8, fontSize: 13, fontWeight: 700, letterSpacing: 1, cursor: "pointer" },
};
