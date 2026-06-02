import { useState, useMemo, useEffect, useRef, useCallback } from "react";
import { api } from "./api.js";
import Auth from "./Auth.jsx";
import Dashboard from "./Dashboard.jsx";
import Admin from "./Admin.jsx";
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
import Calendar from "./Calendar.jsx";

// When the Calendar's "Open in Hand Builder" hands a YouTube URL to the main
// tool, it's stashed here and the Hand Builder picks it up on mount.
const PENDING_YT_KEY = "pokerTable.pendingYoutube";

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

// Piece-rate pricing.
const PIECE_RATE = 0.03; // per filled card / per voluntary action
const COMPLETION_BONUS = 0.1; // per completed hand
const BLIND_POSTS = new Set(["posts_sb", "posts_bb", "posts_ante", "posts_straddle"]);

// Side-game pricing: a flat base per counted hand + a stream-completion bonus.
// Backend-wise a side-game hand is just a 0-piece submission, which already
// prices at COMPLETION_BONUS ($0.10) + STREAM_BONUS ($0.05) = $0.15 — so no
// special server pricing is needed; these constants drive the live counter only.
const SIDE_GAME_RATE = 0.10;        // $ per side-game hand (base)
const SIDE_GAME_STREAM_BONUS = 0.05; // additional once the stream is completed
const SIDE_GAME_TYPES = ["Squid Game", "Bounty Game", "Other"];

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
// Extract the 11-char YouTube video id from ANY common URL shape — the video id
// is the key everything matches on (a hand's stream_id, a calendar stream's id).
// Handles:
//   youtube.com/watch?v=VIDEO_ID
//   youtu.be/VIDEO_ID
//   youtube.com/live/VIDEO_ID   ← was previously unhandled → hands didn't match
//   youtube.com/embed/VIDEO_ID, youtube.com/shorts/VIDEO_ID
// …with any combination of &t=, ?t=, &si=, &list=, etc. A bare id passes through.
export function extractVideoId(url) {
  if (!url) return "";
  const s = String(url).trim();
  if (/^[A-Za-z0-9_-]{11}$/.test(s)) return s; // already a bare id
  const patterns = [
    /[?&]v=([A-Za-z0-9_-]{11})/,      // watch?v=ID (&si=, &t= etc. after = fine)
    /youtu\.be\/([A-Za-z0-9_-]{11})/,  // youtu.be/ID
    /\/live\/([A-Za-z0-9_-]{11})/,     // youtube.com/live/ID
    /\/embed\/([A-Za-z0-9_-]{11})/,    // /embed/ID
    /\/shorts\/([A-Za-z0-9_-]{11})/,   // /shorts/ID
  ];
  for (const re of patterns) {
    const m = s.match(re);
    if (m) return m[1];
  }
  return "";
}
function parseYouTube(link) {
  if (!link || !link.trim()) return { url: "", startSec: 0, id: "" };
  const id = extractVideoId(link);
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

// Today's date as YYYY-MM-DD, used as the default video date for a fresh session.
const TODAY_ISO = (() => {
  try { return new Date().toISOString().slice(0, 10); } catch { return ""; }
})();
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
// Compact clock for a hand's timestamp: "33:20" (m:ss) or "1:02:03" (h:mm:ss).
function fmtClock(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const p = (x) => String(x).padStart(2, "0");
  return h > 0 ? `${h}:${p(m)}:${p(ss)}` : `${m}:${p(ss)}`;
}
// "2026-06-01" → "Jun 1, 2026"; falls back gracefully for blanks / bad input.
function fmtDate(iso) {
  if (!iso) return "No date";
  const [y, m, d] = String(iso).split("-").map(Number);
  if (!y || !m || !d) return iso;
  return `${MONTHS[m - 1] || "?"} ${d}, ${y}`;
}
// Sort order for hand cards: video date, then video, then timestamp within the
// video, then hand #. Keeps each video's hands contiguous and chronological.
function cmpHand(a, b) {
  return (
    (a.videoDate || "").localeCompare(b.videoDate || "") ||
    (a.videoId || "").localeCompare(b.videoId || "") ||
    (a.startSec || 0) - (b.startSec || 0) ||
    (a.n || 0) - (b.n || 0)
  );
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

// ── Hand Manager panel ──────────────────────────────────────────────────────
// Right-side collapsible panel: every completed hand rendered as a selectable
// card, grouped by video and sorted date→timestamp, with range/toggle selection
// (like a file explorer), bulk + per-card delete, and drag-to-export plus
// Export Selected / Export All buttons. Hands live in App state (→ localStorage).
function HandManager({ hands, onDeleteIds, onExportHands }) {
  const [selected, setSelected] = useState(() => new Set());
  const [anchor, setAnchor] = useState(null); // last single-clicked card (range pivot)
  const [dragOver, setDragOver] = useState(false);

  const sorted = useMemo(() => [...hands].sort(cmpHand), [hands]);
  const ids = useMemo(() => sorted.map((h) => h.n), [sorted]);

  // Effective selection — raw state intersected with the hands that still exist,
  // so a deleted hand's id can't linger and skew the count (derived, no effect).
  const sel = useMemo(() => {
    const live = new Set(ids);
    return new Set([...selected].filter((id) => live.has(id)));
  }, [selected, ids]);

  // Group consecutive (sorted) hands by their video — one header per video.
  const groups = useMemo(() => {
    const m = new Map();
    for (const h of sorted) {
      const key = h.videoId || h.videoDate || "ungrouped";
      if (!m.has(key)) m.set(key, []);
      m.get(key).push(h);
    }
    return [...m.values()].map((hs) => ({
      key: hs[0].videoId || hs[0].videoDate || "ungrouped",
      videoDate: hs[0].videoDate,
      label: hs[0].sessionLabel,
      hands: hs,
    }));
  }, [sorted]);

  const allSelected = ids.length > 0 && ids.every((id) => sel.has(id));
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(ids));

  // File-explorer selection: plain = single, Ctrl/Cmd = toggle, Shift = range.
  function clickCard(e, id) {
    const idx = ids.indexOf(id);
    if (e.shiftKey && anchor != null) {
      const a = ids.indexOf(anchor);
      if (a >= 0 && idx >= 0) {
        const [lo, hi] = a < idx ? [a, idx] : [idx, a];
        setSelected(new Set(ids.slice(lo, hi + 1)));
        return;
      }
    }
    if (e.ctrlKey || e.metaKey) {
      setSelected((prev) => {
        const n = new Set(prev);
        if (n.has(id)) n.delete(id); else n.add(id);
        return n;
      });
      setAnchor(id);
      return;
    }
    setSelected(new Set([id]));
    setAnchor(id);
  }

  function deleteOne(h) {
    if (window.confirm(`Delete hand at ${h.timestamp || "?"}?`)) onDeleteIds([h.n]);
  }
  function deleteSelected() {
    const c = sel.size;
    if (!c) return;
    if (window.confirm(`Delete ${c} hand${c === 1 ? "" : "s"}?`)) {
      onDeleteIds([...sel]);
      setSelected(new Set());
    }
  }
  const exportSelected = () => {
    const hs = sorted.filter((h) => sel.has(h.n));
    if (hs.length) onExportHands(hs);
  };
  const exportAll = () => sorted.length && onExportHands(sorted);

  // Dropping into the export zone exports the current selection plus whichever
  // card was dragged (covers dragging an as-yet-unselected card).
  function onDrop(e) {
    e.preventDefault();
    setDragOver(false);
    const dragged = Number(e.dataTransfer.getData("text/plain"));
    const set = new Set(sel);
    if (!Number.isNaN(dragged)) set.add(dragged);
    const hs = sorted.filter((h) => set.has(h.n));
    if (hs.length) onExportHands(hs);
  }

  return (
    <div style={styles.hmInner}>
      <div style={styles.hmHeadRow}>
        <span style={styles.sideHead}>HAND MANAGER</span>
        <span style={styles.hmCount}>{sorted.length} hand{sorted.length === 1 ? "" : "s"}</span>
      </div>

      {sorted.length === 0 ? (
        <div style={styles.hmEmpty}>No hands yet. Complete a hand and it lands here.</div>
      ) : (
        <>
          <div style={styles.hmToolbar}>
            <label style={styles.hmSelAll}>
              <input type="checkbox" checked={allSelected} onChange={toggleAll} />
              <span>Select All</span>
            </label>
            <span style={styles.hmSelCount}>{sel.size} of {sorted.length} selected</span>
          </div>

          <div style={styles.hmBtnRow}>
            <button style={{ ...styles.hmBtn, opacity: sel.size ? 1 : 0.45 }} disabled={!sel.size} onClick={exportSelected}>Export Selected</button>
            <button style={styles.hmBtnGreen} onClick={exportAll}>Export All</button>
            <button style={{ ...styles.hmBtnRed, opacity: sel.size ? 1 : 0.45 }} disabled={!sel.size} onClick={deleteSelected}>Delete Selected</button>
          </div>

          <div style={styles.hmList}>
            {groups.map((g) => (
              <div key={g.key}>
                <div style={styles.hmGroupHead}>
                  {fmtDate(g.videoDate)}{g.label ? ` — ${g.label}` : ""}
                </div>
                {g.hands.map((h) => {
                  const isSel = sel.has(h.n);
                  return (
                    <div
                      key={h.n}
                      draggable
                      onDragStart={(e) => {
                        if (!sel.has(h.n)) { setSelected(new Set([h.n])); setAnchor(h.n); }
                        e.dataTransfer.effectAllowed = "copy";
                        e.dataTransfer.setData("text/plain", String(h.n));
                      }}
                      onClick={(e) => clickCard(e, h.n)}
                      style={{ ...styles.hmCard, ...(isSel ? styles.hmCardSel : {}) }}
                    >
                      <button
                        style={styles.hmCardX}
                        title="Delete this hand"
                        onClick={(e) => { e.stopPropagation(); deleteOne(h); }}
                      >✕</button>
                      <div style={styles.hmCardTop}>
                        <span style={styles.hmTime}>⏱ {h.timestamp || "—"}</span>
                        <span style={styles.hmHandNo}>#{h.n}</span>
                      </div>
                      <div style={styles.hmSummary}>{h.summary || "(hand)"}</div>
                      <div style={styles.hmCardMeta}>
                        <span>{h.playerCount != null ? `${h.playerCount} to flop` : ""}</span>
                        <span>{h.potSize != null ? `Pot ${fmtChips(h.potSize)}` : ""}</span>
                      </div>
                      {h.youtubeUrl ? (
                        <a href={h.youtubeUrl} target="_blank" rel="noreferrer" style={styles.hmLink} onClick={(e) => e.stopPropagation()}>
                          ▶ watch ↗
                        </a>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            ))}
          </div>

          <div
            style={{ ...styles.hmDrop, ...(dragOver ? styles.hmDropActive : {}) }}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={onDrop}
          >
            {dragOver ? "Release to export" : "⤓ Drop hands here to export"}
          </div>
        </>
      )}
    </div>
  );
}

// ── Hand Builder (the poker table workspace) ─────────────────────────────────
function HandBuilder({ me, refreshMe }) {
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
  const [youtubeLink, setYoutubeLink] = useState(() => {
    // A URL handed over from the Calendar's "Open in Hand Builder" wins, and is
    // consumed once; otherwise fall back to the saved per-hand link.
    try {
      const pending = localStorage.getItem(PENDING_YT_KEY);
      if (pending) { localStorage.removeItem(PENDING_YT_KEY); return pending; }
    } catch { /* ignore */ }
    return pick("youtubeLink", "");
  });
  const [videoDate, setVideoDate] = useState(() => pick("videoDate", TODAY_ISO)); // session's YouTube video date
  const [sessionLabel, setSessionLabel] = useState(() => pick("sessionLabel", "")); // stream name, e.g. "HCL Stream"

  // ── Side game mode ─────────────────────────────────────────────────────────
  // A simplified flow for non-standard side games (Squid Game, Bounty Game, …):
  // no hole cards / actions / board — just count hands as positions rotate. Each
  // counted hand is submitted as a 0-piece hand so it pays $0.10 (+$0.05 on
  // stream completion) and counts toward the stream's handsCompleted, but it
  // generates no PT4 text.
  const [sideGameMode, setSideGameMode] = useState(() => pick("sideGameMode", false));
  const [sideGameType, setSideGameType] = useState(() => pick("sideGameType", "Squid Game"));
  const [sideGameOther, setSideGameOther] = useState(() => pick("sideGameOther", ""));
  const [sideGameHands, setSideGameHands] = useState(() => pick("sideGameHands", [])); // [{type,gameType,timestamp,handNumber,videoId}]
  const [endingSideGame, setEndingSideGame] = useState(false); // showing the adjust-stacks prompt
  const [sideStackDraft, setSideStackDraft] = useState({}); // seat -> stack string while confirming

  // UI state
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [handPanelOpen, setHandPanelOpen] = useState(false); // right-side Hand Manager
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

  // ── Piece-rate earnings tracker ───────────────────────────────────────────
  // Live count of billable pieces for the CURRENT hand: each filled hole/board
  // card + each voluntary action (auto blind/ante/straddle posts don't count).
  const handPieces = useMemo(() => {
    const cards =
      Object.values(holeCards).reduce((s, arr) => s + arr.filter(Boolean).length, 0) +
      board.filter(Boolean).length +
      board2.filter(Boolean).length;
    const actions = eng
      ? Object.values(eng.actionsByStreet).reduce(
          (s, arr) => s + arr.filter((a) => !BLIND_POSTS.has(a.action)).length,
          0
        )
      : 0;
    return { cards, actions, total: cards + actions };
  }, [holeCards, board, board2, eng]);

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
          winner, winner2, handNumber, youtubeLink, videoDate, sessionLabel, sessionHands, preview, evalResult,
          sideGameMode, sideGameType, sideGameOther, sideGameHands,
        })
      );
    } catch {
      /* localStorage full or unavailable — ignore */
    }
  }, [stakes, ante, buttonSeat, straddleCount, buyButton, roster, phase, eng, engHistory, holeCards, board, board2, rit, winner, winner2, handNumber, youtubeLink, videoDate, sessionLabel, sessionHands, preview, evalResult, sideGameMode, sideGameType, sideGameOther, sideGameHands]);


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
      const { url, startSec, id: videoId } = parseYouTube(link);
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
      const { cards, actions } = handPieces;

      // ── Hand-card metadata for the Hand Manager ──────────────────────────
      // Pot actually contested (gross committed minus any returned uncalled bet).
      const grossPot = potTotal(eng);
      const uncalled = eng.uncalled?.amount || 0;
      const potSize = grossPot - uncalled;
      // Where the hand ended: a 2+ way survivor pool means it was shown down,
      // otherwise it ended on whatever street the last fold happened.
      const endStreet = surv.length >= 2 ? "showdown" : eng.street;
      const summary =
        effWinners.length > 1
          ? `${effWinners.join(" & ")} split ${fmtChips(potSize)} (${endStreet})`
          : `${effWinner} wins ${fmtChips(potSize)} (${endStreet})`;
      // Players who saw the flop: everyone not folded during preflop, but only if
      // the hand actually reached the flop (else nobody saw it).
      const foldedPreflop = new Set(
        (eng.actionsByStreet.preflop || []).filter((a) => a.action === "folds").map((a) => a.player)
      );
      const reachedFlop = STREETS.indexOf(eng.street) >= 1;
      const playerCount = reachedFlop ? eng.players.filter((p) => !foldedPreflop.has(p.name)).length : 0;

      setSessionHands((hs) =>
        [
          ...hs.filter((h) => h.n !== n),
          {
            n,
            text: data.text, // kept for backward compatibility
            pt4Text: data.text,
            cards,
            actions,
            youtubeUrl: url,
            videoId,
            timestamp: startSec > 0 ? fmtClock(startSec) : "",
            startSec,
            videoDate,
            sessionLabel,
            summary,
            potSize,
            playerCount,
          },
        ].sort((a, b) => a.n - b.n)
      );
      setHandPanelOpen(true); // surface the new card in the Hand Manager

      // Persist server-side for cross-device tracking + earnings/bonuses, then
      // refresh the header totals. Local copy is kept even if the write fails.
      try {
        await api("/api/hands/submit", {
          method: "POST",
          body: {
            stream_id: videoId,
            youtube_url: url,
            timestamp_seconds: startSec,
            pt4_text: data.text,
            cards_count: cards,
            actions_count: actions,
          },
        });
        if (refreshMe) refreshMe();
      } catch { /* offline / server write failed — local Hand Manager still has it */ }

      // Earnings breakdown toast.
      const cardPay = (cards * PIECE_RATE).toFixed(2);
      const actPay = (actions * PIECE_RATE).toFixed(2);
      const total = (cards * PIECE_RATE + actions * PIECE_RATE + COMPLETION_BONUS).toFixed(2);
      setFlash(`Hand #${n}: ${cards} cards ($${cardPay}) + ${actions} actions ($${actPay}) + bonus ($${COMPLETION_BONUS.toFixed(2)}) = $${total}`);
      setTimeout(() => setFlash(""), 2800);
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
    setVideoDate(TODAY_ISO);
    setSessionLabel("");
    setSessionHands([]);
    setPreview("");
    setEvalResult(null);
    setConfigDraft("");
    setError("");
    setSideGameMode(false);
    setSideGameOther("");
    setSideGameHands([]);
    setEndingSideGame(false);
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

  // ── Side game mode ─────────────────────────────────────────────────────────
  const sideGameLabel =
    sideGameType === "Other" ? (sideGameOther.trim() || "Side Game") : sideGameType;
  const sideGameEarnings = sideGameHands.length * SIDE_GAME_RATE;

  // Enter side game mode: drop any in-progress hand and switch to the counter UI.
  function startSideGame() {
    setEng(null);
    setEngHistory([]);
    setError("");
    setPhase("setup");
    setSideGameMode(true);
  }

  // Count one side-game hand: validate the link, submit a 0-piece hand (so it
  // pays + counts toward the stream but makes no PT4 text), record it locally,
  // rotate the button, and bump the hand number.
  async function nextSideGameHand() {
    setError("");
    const link = youtubeLink.trim();
    if (!link) return setError("Paste the timestamped YouTube link for the side game's first hand.");
    if (!/youtube\.com|youtu\.be/i.test(link)) return setError("Enter a valid YouTube link (youtube.com, youtu.be, or /live/).");
    const { url, startSec, id: videoId } = parseYouTube(link);
    if (!videoId) return setError("Couldn't read a video ID from that link.");

    const n = handNumber;
    setSideGameHands((hs) => [
      ...hs,
      { type: "sidegame", gameType: sideGameLabel, timestamp: startSec > 0 ? fmtClock(startSec) : "", handNumber: n, videoId },
    ]);

    // 0-piece submission → $0.10 base (+$0.05 on stream completion), counts toward
    // the stream's handsCompleted, no PT4 text.
    try {
      await api("/api/hands/submit", {
        method: "POST",
        body: {
          stream_id: videoId,
          youtube_url: url,
          timestamp_seconds: startSec,
          pt4_text: "",
          cards_count: 0,
          actions_count: 0,
          hand_type: "sidegame",
          side_game: sideGameLabel,
        },
      });
      if (refreshMe) refreshMe();
    } catch { /* offline — the local counter still advances */ }

    // Positions auto-rotate: move the button to the next occupied seat.
    const occ = named.slice().sort((a, b) => a.seat - b.seat);
    if (occ.length) {
      const i = occ.findIndex((p) => p.seat === Number(buttonSeat));
      const nextSeat = i >= 0 ? occ[(i + 1) % occ.length].seat : occ[0].seat;
      setButtonSeat(nextSeat);
    }
    setHandNumber((h) => h + 1);
    setFlash(`Side game hand #${n} recorded · +$${SIDE_GAME_RATE.toFixed(2)}`);
    setTimeout(() => setFlash(""), 1800);
  }

  // Begin ending the side game — surface the stack-adjust prompt seeded with
  // each seated player's current stack.
  function requestEndSideGame() {
    const draft = {};
    named.forEach((p) => { draft[p.seat] = String(p.stack); });
    setSideStackDraft(draft);
    setEndingSideGame(true);
  }

  // Confirm adjusted stacks → apply to the roster → resume normal transcription.
  function confirmEndSideGame() {
    setRoster((r) =>
      r.map((p) => {
        const v = sideStackDraft[p.seat];
        if (v == null || v === "") return p;
        const num = Number(v);
        return Number.isFinite(num) ? { ...p, stack: num } : p;
      })
    );
    setEndingSideGame(false);
    setSideGameMode(false);
    setEng(null);
    setError("");
    setPhase("setup");
  }

  // Buy-the-button: the buyer becomes the button and posts the lone live blind.
  function buyTheButton(seat, type) {
    setBuyButton({ seat, type });
    setButtonSeat(seat);
    setStraddleCount(0); // a bought straddle/button replaces normal straddles
    setBuyMenuSeat(null);
  }

  // Export a set of hands as one PT4 .txt: sorted by date→timestamp, each hand's
  // text concatenated with two blank lines between (matches the session format).
  function downloadHands(hands) {
    if (!hands.length) return;
    const ordered = [...hands].sort(cmpHand);
    const text = ordered.map((h) => (h.pt4Text || h.text || "").trimEnd()).join("\n\n\n") + "\n";
    const blob = new Blob([text], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `hands_${ordered.length}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  }

  // Remove hands by number; the persistence effect mirrors the change to storage.
  function deleteHandIds(idsArr) {
    const drop = new Set(idsArr);
    setSessionHands((hs) => hs.filter((h) => !drop.has(h.n)));
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

            <div style={styles.row}>
              <div style={styles.col}>
                <label style={styles.label}>Video Date</label>
                <input style={styles.input} type="date" value={videoDate} onChange={(e) => setVideoDate(e.target.value)} />
              </div>
              <div style={styles.col}>
                <label style={styles.label}>Stream Name</label>
                <input style={styles.input} value={sessionLabel} placeholder="HCL Stream" onChange={(e) => setSessionLabel(e.target.value)} />
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
            <div style={styles.statTracker}>
              <div>
                Hands: <strong style={{ color: "#f8fafc" }}>{me.dashboard.hands}</strong>
                {"  ·  "}Pieces: <strong style={{ color: "#f8fafc" }}>{me.dashboard.pieces}</strong>
              </div>
              <div style={styles.statPreview}>
                Base: <strong style={{ color: "#e2e8f0" }}>${me.dashboard.base.toFixed(2)}</strong>
                {" + "}Stream Bonuses: <strong style={{ color: "#fbbf24" }}>${me.dashboard.stream_bonus.toFixed(2)}</strong>
                {" = "}Total: <strong style={{ color: "#4ade80", fontSize: 14 }}>${me.dashboard.total.toFixed(2)}</strong>
              </div>
              {eng && handPieces.total > 0 && (
                <div style={styles.statPreview}>
                  This hand so far: ~${(handPieces.total * PIECE_RATE).toFixed(2)} ({handPieces.cards} cards · {handPieces.actions} actions)
                </div>
              )}
              {(sideGameMode || sideGameHands.length > 0) && (
                <div style={{ ...styles.statPreview, color: "#a78bfa" }}>
                  🎮 Side Game: <strong style={{ color: "#c4b5fd" }}>{sideGameHands.length} hands</strong> · <strong style={{ color: "#c4b5fd" }}>${sideGameEarnings.toFixed(2)}</strong>
                </div>
              )}
            </div>
            <span>{stakes} {Number(ante) > 0 ? `· $${ante} ante` : ""}</span>
            {/* Side Game Mode toggle + (when on) the game-type picker. */}
            {sideGameMode && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <select
                  style={{ ...styles.input, width: "auto" }}
                  value={sideGameType}
                  onChange={(e) => setSideGameType(e.target.value)}
                >
                  {SIDE_GAME_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
                {sideGameType === "Other" && (
                  <input
                    style={{ ...styles.input, width: 130 }}
                    value={sideGameOther}
                    onChange={(e) => setSideGameOther(e.target.value)}
                    placeholder="Game name…"
                  />
                )}
              </span>
            )}
            <button
              style={{ ...styles.topBtn, ...(sideGameMode ? { borderColor: "#a78bfa", color: "#c4b5fd" } : {}) }}
              title={sideGameMode ? "End the side game (adjust stacks, then resume)" : "Switch to Side Game Mode — count hands quickly, no cards/actions"}
              onClick={() => (sideGameMode ? requestEndSideGame() : startSideGame())}
            >
              🎮 {sideGameMode ? "Side Game ON" : "Side Game Mode"}
            </button>
            {!sideGameMode && phase !== "setup" && (
              <>
                <button style={styles.topBtn} title="Restart this hand (same players & stacks)" onClick={resetHand}>↺ Reset Hand</button>
                <button style={styles.topBtn} title="Next hand (carry over stacks, rotate button)" onClick={nextHand}>Next Hand ↻</button>
              </>
            )}
            {sessionHands.length > 0 && (
              <button style={styles.sessionBtn} title="Open the Hand Manager" onClick={() => setHandPanelOpen((o) => !o)}>
                🗂 Hand Manager ({sessionHands.length})
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
          {!sideGameMode && (phase === "betting" || phase === "complete") && engHistory.length > 0 && (
            <button style={styles.undoBtn} title="Undo last action" onClick={undo}>↶ Undo</button>
          )}
          {error && <div style={styles.errorInline}>⚠ {error}</div>}

          {/* Side game mode: count hands fast. No cards / actions / board. */}
          {sideGameMode && !endingSideGame && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>
                <strong style={{ color: "#c4b5fd" }}>{sideGameLabel}</strong> — paste the first hand's timestamped link above, then tap through hands. Positions rotate automatically; no cards or actions needed.
              </div>
              <div style={{ display: "flex", gap: 10, justifyContent: "center" }}>
                <button style={styles.dealBtn} onClick={nextSideGameHand}>▶ NEXT SIDE GAME HAND (#{handNumber})</button>
                <button style={styles.topBtn} title="End the side game and adjust stacks" onClick={requestEndSideGame}>■ End Side Game</button>
              </div>
            </div>
          )}

          {/* Ending a side game: confirm current stacks before resuming. */}
          {sideGameMode && endingSideGame && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>Side game ended. Please adjust player stacks to current values and resume normal transcription.</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 10, justifyContent: "center", margin: "8px 0" }}>
                {named.map((p) => (
                  <label key={p.seat} style={{ display: "inline-flex", alignItems: "center", gap: 5, color: "#cbd5e1", fontSize: 12 }}>
                    <span style={{ minWidth: 60, textAlign: "right" }}>{p.name}</span>
                    <input
                      style={{ ...styles.input, width: 96 }}
                      type="number"
                      value={sideStackDraft[p.seat] ?? ""}
                      onChange={(e) => setSideStackDraft((d) => ({ ...d, [p.seat]: e.target.value }))}
                    />
                  </label>
                ))}
              </div>
              <button style={styles.completeBtn} onClick={confirmEndSideGame}>✓ Confirm Stacks &amp; Resume</button>
            </div>
          )}

          {!sideGameMode && phase === "setup" && (
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

      {/* Hand Manager — right-side collapsible panel */}
      <aside style={{ ...styles.handPanel, width: handPanelOpen ? 348 : 0, padding: handPanelOpen ? "18px 14px" : 0 }}>
        {handPanelOpen && (
          <HandManager hands={sessionHands} onDeleteIds={deleteHandIds} onExportHands={downloadHands} />
        )}
      </aside>
      <button
        style={{ ...styles.handToggle, right: handPanelOpen ? 348 : 0 }}
        title={handPanelOpen ? "Hide Hand Manager" : "Show Hand Manager"}
        onClick={() => setHandPanelOpen((o) => !o)}
      >
        {handPanelOpen ? "›" : "‹"}
      </button>

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
    </div>
  );
}

// ── Styles ──────────────────────────────────────────────────────────────────
const FELT = "radial-gradient(ellipse at center, #1b6b43 0%, #0f4d2f 70%, #0a3a23 100%)";
const styles = {
  app: { display: "flex", minHeight: "calc(100vh - 46px)", background: "#070b12", color: "#e2e8f0", fontFamily: "'Inter','Segoe UI',system-ui,sans-serif" },
  sidebar: { background: "#0d1320", borderRight: "1px solid #1e293b", overflow: "hidden", transition: "width .18s ease", flexShrink: 0, boxSizing: "border-box" },
  sidebarInner: { display: "flex", flexDirection: "column", gap: 10 },
  sideHead: { fontSize: 11, fontWeight: 800, letterSpacing: 2, color: "#f59e0b" },
  sideHint: { fontSize: 9, fontWeight: 500, letterSpacing: 0.5, color: "#475569" },
  sidebarToggle: { position: "fixed", top: 60, zIndex: 30, width: 22, height: 44, background: "#1e293b", color: "#cbd5e1", border: "1px solid #334155", borderRadius: "0 8px 8px 0", cursor: "pointer", transition: "left .18s ease" },
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
  statTracker: { background: "#0d1320", border: "1px solid #1e293b", borderRadius: 8, padding: "5px 12px", fontSize: 13, color: "#94a3b8", whiteSpace: "nowrap", lineHeight: 1.4 },
  statPreview: { fontSize: 11, color: "#fbbf24", marginTop: 1 },
  flashToast: { position: "fixed", top: 64, left: "50%", transform: "translateX(-50%)", zIndex: 80, background: "#16a34a", color: "#f0fdf4", fontWeight: 800, fontSize: 14, letterSpacing: 0.3, padding: "12px 24px", borderRadius: 10, boxShadow: "0 8px 30px rgba(34,197,94,.5)", maxWidth: "90vw", textAlign: "center", whiteSpace: "nowrap" },
  handTag: { background: "#f59e0b22", color: "#f59e0b", padding: "4px 10px", borderRadius: 20, fontWeight: 700, fontSize: 12 },

  // ── Hand Manager panel ──
  handPanel: { background: "#0d1320", borderLeft: "1px solid #1e293b", overflow: "hidden", transition: "width .18s ease", flexShrink: 0, boxSizing: "border-box" },
  handToggle: { position: "fixed", top: 60, zIndex: 30, width: 22, height: 44, background: "#1e293b", color: "#cbd5e1", border: "1px solid #334155", borderRadius: "8px 0 0 8px", cursor: "pointer", transition: "right .18s ease" },
  hmInner: { display: "flex", flexDirection: "column", gap: 10, height: "100%", minWidth: 320 },
  hmHeadRow: { display: "flex", justifyContent: "space-between", alignItems: "baseline" },
  hmCount: { fontSize: 11, color: "#64748b", fontWeight: 600 },
  hmEmpty: { fontSize: 12, color: "#64748b", lineHeight: 1.5, marginTop: 6 },
  hmToolbar: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  hmSelAll: { display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#cbd5e1", cursor: "pointer", fontWeight: 600 },
  hmSelCount: { fontSize: 11, color: "#7dd3fc", fontWeight: 700 },
  hmBtnRow: { display: "flex", flexWrap: "wrap", gap: 6 },
  hmBtn: { flex: "1 1 46%", padding: "7px 8px", background: "#16243a", border: "1px solid #2b3a52", borderRadius: 7, color: "#e2e8f0", fontSize: 11.5, fontWeight: 700, cursor: "pointer", whiteSpace: "nowrap" },
  hmBtnGreen: { flex: "1 1 46%", padding: "7px 8px", background: "#16a34a", border: "1px solid #16a34a", borderRadius: 7, color: "#06210f", fontSize: 11.5, fontWeight: 800, cursor: "pointer", whiteSpace: "nowrap" },
  hmBtnRed: { flex: "1 1 100%", padding: "7px 8px", background: "#3a1416", border: "1px solid #7f1d1d", borderRadius: 7, color: "#fca5a5", fontSize: 11.5, fontWeight: 700, cursor: "pointer", whiteSpace: "nowrap" },
  hmList: { flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 4, margin: "0 -2px", padding: "0 2px" },
  hmGroupHead: { position: "sticky", top: 0, background: "#0d1320", fontSize: 11, fontWeight: 800, letterSpacing: 0.5, color: "#f59e0b", padding: "6px 0 4px", zIndex: 1 },
  hmCard: { position: "relative", background: "#0e1626", border: "1px solid #1e293b", borderRadius: 9, padding: "8px 10px 7px", marginBottom: 6, cursor: "pointer", userSelect: "none" },
  hmCardSel: { borderColor: "#3b82f6", boxShadow: "0 0 0 1px #3b82f6, 0 0 14px rgba(59,130,246,.35)", background: "#10203a" },
  hmCardX: { position: "absolute", top: 5, right: 5, width: 18, height: 18, lineHeight: "16px", textAlign: "center", padding: 0, background: "#1e293b", border: "1px solid #334155", borderRadius: 5, color: "#94a3b8", fontSize: 10, cursor: "pointer" },
  hmCardTop: { display: "flex", alignItems: "center", gap: 8, marginBottom: 3, paddingRight: 18 },
  hmTime: { fontSize: 12, fontWeight: 800, color: "#fde68a" },
  hmHandNo: { fontSize: 10, color: "#64748b", fontWeight: 700 },
  hmSummary: { fontSize: 12.5, fontWeight: 700, color: "#f1f5f9", lineHeight: 1.3, marginBottom: 4 },
  hmCardMeta: { display: "flex", justifyContent: "space-between", fontSize: 11, color: "#94a3b8" },
  hmLink: { display: "inline-block", marginTop: 5, fontSize: 10.5, color: "#7dd3fc", fontWeight: 600, textDecoration: "none" },
  hmDrop: { flexShrink: 0, marginTop: 2, padding: "14px 10px", border: "2px dashed #2b3a52", borderRadius: 10, textAlign: "center", fontSize: 12, fontWeight: 700, color: "#64748b", background: "rgba(10,15,26,.4)", transition: "all .12s" },
  hmDropActive: { borderColor: "#22c55e", color: "#4ade80", background: "rgba(34,197,94,.12)" },

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

  errorInline: { position: "absolute", top: -2, left: "50%", transform: "translate(-50%,-100%)", background: "#7f1d1d", color: "#fee2e2", border: "1px solid #ef4444", borderRadius: 8, padding: "10px 18px", fontSize: 14, fontWeight: 700, whiteSpace: "nowrap", boxShadow: "0 -4px 20px rgba(239,68,68,.4)", zIndex: 20 },

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

// ── Top nav: switch between Hand Builder and Calendar ────────────────────────
function NavItem({ to, label, icon, active, navigate }) {
  return (
    <button
      onClick={() => navigate(to)}
      style={{ ...navStyles.item, ...(active ? navStyles.itemActive : {}) }}
    >
      <span style={{ marginRight: 6 }}>{icon}</span>{label}
    </button>
  );
}

function NavBar({ route, navigate, me, onLogout }) {
  return (
    <nav style={navStyles.bar}>
      <div style={navStyles.brand}>
        <span style={navStyles.chip}>♠</span> POKER SUITE
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <NavItem to="/" label="Hand Builder" icon="🛠" active={route === "builder"} navigate={navigate} />
        <NavItem to="/calendar" label="Calendar" icon="📅" active={route === "calendar"} navigate={navigate} />
        <NavItem to="/dashboard" label="Dashboard" icon="📊" active={route === "dashboard"} navigate={navigate} />
        {me.user.is_admin && (
          <NavItem to="/admin" label="Admin" icon="🛡" active={route === "admin"} navigate={navigate} />
        )}
        <span style={navStyles.user}>{me.user.username}</span>
        <button style={navStyles.logout} onClick={onLogout}>Log out</button>
      </div>
    </nav>
  );
}

const pathToRoute = (p) => {
  if (p.startsWith("/calendar")) return "calendar";
  if (p.startsWith("/dashboard")) return "dashboard";
  if (p.startsWith("/admin")) return "admin";
  return "builder";
};

// ── Root: auth gating + history-based router (SPA fallback in server.py) ──────
export default function App() {
  const [route, setRoute] = useState(() => {
    try { return pathToRoute(window.location.pathname); } catch { return "builder"; }
  });
  const [me, setMe] = useState(null); // null = checking, false = anonymous, obj = authed

  useEffect(() => {
    const onPop = () => setRoute(pathToRoute(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Initial session check.
  useEffect(() => {
    api("/api/me").then(setMe).catch(() => setMe(false));
  }, []);

  const refreshMe = useCallback(async () => {
    try { const m = await api("/api/me"); setMe(m); return m; }
    catch { setMe(false); return null; }
  }, []);

  const navigate = (path) => {
    try {
      if (window.location.pathname !== path) window.history.pushState({}, "", path);
    } catch { /* ignore */ }
    setRoute(pathToRoute(path));
  };

  // Hand a YouTube URL to the Hand Builder, then switch to it.
  const openInBuilder = (url) => {
    try { localStorage.setItem(PENDING_YT_KEY, url || ""); } catch { /* ignore */ }
    navigate("/");
  };

  const logout = async () => {
    try { await api("/api/logout", { method: "POST" }); } catch { /* ignore */ }
    setMe(false);
    navigate("/");
  };

  if (me === null) return <div style={navStyles.loading}>Loading…</div>;
  if (!me) return <Auth onAuthed={(m) => { setMe(m); navigate("/"); }} />;

  let view;
  if (route === "calendar") view = <Calendar onOpenInBuilder={openInBuilder} refreshMe={refreshMe} />;
  else if (route === "dashboard") view = <Dashboard user={me.user} dashboard={me.dashboard} />;
  else if (route === "admin") {
    view = me.user.is_admin
      ? <Admin />
      : <div style={navStyles.loading}>Admin access only.</div>;
  } else view = <HandBuilder me={me} refreshMe={refreshMe} />;

  return (
    <div style={navStyles.root}>
      <NavBar route={route} navigate={navigate} me={me} onLogout={logout} />
      <div style={navStyles.view}>{view}</div>
    </div>
  );
}

const navStyles = {
  root: { display: "flex", flexDirection: "column", minHeight: "100vh", background: "#070b12" },
  view: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column" },
  bar: {
    position: "sticky", top: 0, zIndex: 100, height: 46, boxSizing: "border-box",
    display: "flex", alignItems: "center", justifyContent: "space-between",
    padding: "0 18px", background: "#0a0f1a", borderBottom: "1px solid #1e293b",
    fontFamily: "'Inter','Segoe UI',system-ui,sans-serif",
  },
  brand: { display: "flex", alignItems: "center", gap: 8, fontWeight: 800, letterSpacing: 1.5, fontSize: 14, color: "#e2e8f0" },
  chip: { display: "inline-flex", width: 22, height: 22, borderRadius: "50%", background: "linear-gradient(135deg,#f59e0b,#d97706)", color: "#0a0e17", alignItems: "center", justifyContent: "center", fontSize: 13 },
  item: { padding: "7px 16px", background: "transparent", border: "1px solid transparent", borderRadius: 8, color: "#94a3b8", fontSize: 13, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" },
  itemActive: { background: "#16243a", border: "1px solid #2b3a52", color: "#f8fafc" },
  user: { fontSize: 12.5, fontWeight: 700, color: "#7dd3fc", marginLeft: 6, padding: "0 4px" },
  logout: { padding: "7px 14px", background: "#1e293b", border: "1px solid #334155", borderRadius: 8, color: "#cbd5e1", fontSize: 12.5, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" },
  loading: { flex: 1, display: "flex", alignItems: "center", justifyContent: "center", minHeight: "60vh", color: "#64748b", fontFamily: "'Inter','Segoe UI',system-ui,sans-serif", fontSize: 14 },
};
