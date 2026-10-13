import { useState, useMemo, useEffect, useRef, useCallback } from "react";
import { api } from "./api.js";
import Auth from "./Auth.jsx";
import Dashboard from "./Dashboard.jsx";
import Admin from "./Admin.jsx";
import TutorialOverlay from "./TutorialOverlay.jsx";
import { switchUi, whatsNewSeen, markWhatsNewSeen } from "./uiSwitch.js";
import WhatsNew from "./WhatsNew.jsx";

// Onboarding tour — each step points at an element tagged with data-tour.
// Order follows the real workflow: set up the table → paste link → deal → cards
// → actions → complete.
const TUTORIAL_STEPS = [
  { selector: '[data-tour="roster"]', title: "Roster", description: "First, enter all player names and their starting stacks." },
  { selector: '[data-tour="stakes"]', title: "Stakes & BB ante", description: "Set the game stakes and ante amount." },
  { selector: '[data-tour="button"]', title: "Button seat", description: "Set the dealer button position." },
  { selector: '[data-tour="straddle"]', title: "Mandatory straddle", description: "Check this if the game has a mandatory straddle." },
  { selector: '[data-tour="setdefault"]', title: "Default lineup", description: "Save this lineup as default for the stream." },
  { selector: '[data-tour="youtube"]', title: "YouTube link", howto: true, wide: true, description: "Before EVERY hand, paste a timestamped YouTube link. You cannot deal without one." },
  { selector: '[data-tour="deal"]', title: "Deal a hand", description: "Click Deal to start the hand." },
  { selector: '[data-tour="seat"]', title: "Hole cards", description: "Assign each player's cards from the stream overlay." },
  { selector: '[data-tour="board"]', title: "Community cards", description: "Set the board cards." },
  { selector: '[data-tour="actionbar"]', title: "Action bar", description: "Enter actions in order." },
  { selector: '[data-tour="completehand"]', title: "Complete the hand", description: "Finish the hand and auto-save." },
  { selector: '[data-tour="handmanager"]', title: "Hand Manager", description: "View and manage completed hands." },
  { selector: '[data-tour="earnings"]', title: "Earnings", description: "Track your pay." },
  { selector: '[data-tour="nav-calendar"]', title: "Calendar", description: "View stream progress." },
  { selector: '[data-tour="nav-dashboard"]', title: "Dashboard", description: "Your stats and bonus." },
  { final: true, title: "Tutorial complete!" },
];
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
import {
  applySideGameNames, mainPotWinners, settleBounty, settleSquid, sideGameDeltas, sideGamePayload,
  DEFAULT_BOUNTY, DEFAULT_SQUID, SIDE_GAMES, squidGameKey, SIDE_PAYOUT_BONUS,
  DEFAULT_SEVEN_TWO, is72, settleSevenTwo,
} from "./sideGames.js";
import SideGameDialog, { SidePayConfirm } from "./SideGameDialog.jsx";
import Calendar from "./Calendar.jsx";

// When the Calendar's "Open in Hand Builder" hands a YouTube URL to the main
// tool, it's stashed here and the Hand Builder picks it up on mount.
const PENDING_YT_KEY = "pokerTable.pendingYoutube";
// "Resume" from the Calendar stashes a fuller snapshot here (URL + last hand's
// roster/stacks + rotated button + hand #) so the builder can continue a stream.
const PENDING_RESUME_KEY = "pokerTable.pendingResume";
function readAndClearPendingResume() {
  try {
    const raw = localStorage.getItem(PENDING_RESUME_KEY);
    if (raw) { localStorage.removeItem(PENDING_RESUME_KEY); return JSON.parse(raw); }
  } catch { /* ignore */ }
  return null;
}

// ── Card constants ──────────────────────────────────────────────────────────
const RANKS = ["A", "K", "Q", "J", "T", "9", "8", "7", "6", "5", "4", "3", "2"];
// Four-color deck, inverted: the suit colors the whole face, with a large
// white rank and a small white suit in the top-right corner.
const SUITS = [
  { v: "s", symbol: "♠", face: "linear-gradient(180deg,#45413b,#2b2824)", edge: "rgba(255,255,255,.28)" },
  { v: "h", symbol: "♥", face: "linear-gradient(180deg,#c24b4b,#9c2f31)", edge: "rgba(255,210,210,.35)" },
  { v: "d", symbol: "♦", face: "linear-gradient(180deg,#3d79b8,#285a92)", edge: "rgba(205,225,255,.35)" },
  { v: "c", symbol: "♣", face: "linear-gradient(180deg,#3a8c5a,#286b42)", edge: "rgba(205,245,220,.35)" },
];
// What Undo / Redo step through: everything about the hand in progress.
const HIST_KEYS = ["phase", "eng", "engHistory", "holeCards", "board", "extraBoards", "numRuns", "runsChosen",
  "runWinners", "winner", "buyButton", "customStraddle", "buttonSeat", "roster", "bountyStreaks", "squidButtons", "squidLeft"];

const SUIT = Object.fromEntries(SUITS.map((s) => [s.v, s]));
const STREET_LABEL = { preflop: "Preflop", flop: "Flop", turn: "Turn", river: "River" };
// Position chips on the seats: gold for the button, then the muted signal hues.
const BADGE_COLOR = {
  BTN: "#dec078", "BTN/SB": "#dec078", SB: "#8fb0cf", BB: "#a99cd8",
  CO: "#7eb88a", HJ: "#7eb88a", LJ: "#7eb88a", STR: "#d4815f",
};
const BADGE_DEFAULT = "#a89b7e";

// Piece-rate pricing.
const PIECE_RATE = 0.03; // per filled card / per voluntary action
const COMPLETION_BONUS = 0.1; // per completed hand
const BLIND_POSTS = new Set(["posts_sb", "posts_bb", "posts_ante", "posts_straddle"]);


// Side-game pricing: a flat base per counted hand + a stream-completion bonus.
// Backend-wise a side-game hand is just a 0-piece submission, which already
// prices at COMPLETION_BONUS ($0.10) + STREAM_BONUS ($0.05) = $0.15 — so no
// special server pricing is needed; these constants drive the live counter only.
const SIDE_GAME_RATE = 0.10;        // $ per side-game hand (base)
// Squid, Progressive Squid and Bounty are started from the Side Game popup
// and transcribed in full (holdings in each name plus a SIDE GAME block, see
// sideGames.js). The quick counter is the popup's "Other game: just count
// hands", for anything else.
const SIDE_GAME_TYPES = ["Other"];

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
function extractVideoId(url) {
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

// Today's LOCAL date as YYYY-MM-DD, the default video date for a fresh session.
// (toISOString is UTC, which is already tomorrow on a US evening.)
const TODAY_ISO = (() => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
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
  // f: the "+" on an empty slot; r: the rank; s: the corner suit.
  const dims = { sm: { w: 30, h: 42, f: 15, r: 21, s: 9 }, md: { w: 44, h: 62, f: 20, r: 30, s: 11 }, lg: { w: 56, h: 78, f: 26, r: 38, s: 13 } }[size];
  const base = {
    width: dims.w, height: dims.h, borderRadius: 3, display: "flex",
    flexDirection: "column", alignItems: "center", justifyContent: "center",
    cursor: onClick ? "pointer" : "default", userSelect: "none", flexShrink: 0,
    fontWeight: 600, fontSize: dims.f, lineHeight: 1, fontFamily: "var(--font-display)",
    transition: "transform .15s ease, box-shadow .2s ease",
  };
  if (!card) {
    if (faceDownIfEmpty)
      return (
        <div onClick={onClick} style={{ ...base, background: "repeating-linear-gradient(45deg,#15130f,#15130f 4px,#1f1b14 4px,#1f1b14 8px)", border: "1px solid rgba(200,168,90,.45)", boxShadow: "inset 0 0 0 2px #0d0c0a, inset 0 0 0 3px rgba(200,168,90,.25), 0 3px 8px rgba(0,0,0,.5)" }} />
      );
    return (
      <div onClick={onClick} style={{ ...base, background: "rgba(13,12,10,.55)", border: "1px dashed rgba(200,168,90,.28)", color: "var(--gold-dim)", fontSize: dims.f - 6, fontFamily: "var(--font-ui)" }}>＋</div>
    );
  }
  const rank = card[0] === "T" ? "10" : card[0];
  const sd = SUIT[card[1]] || SUITS[0];
  return (
    <div onClick={onClick} className={onClick ? "ui-card-pick" : undefined} style={{ ...base, position: "relative", background: sd.face, border: `1px solid ${sd.edge}`, color: "#fff", boxShadow: "0 3px 9px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.18)" }}>
      <span style={{ ...styles.cardRank, fontSize: dims.r, letterSpacing: rank === "10" ? -1.5 : 0 }}>{rank}</span>
      <span style={{ ...styles.cardSuit, fontSize: dims.s }}>{sd.symbol}</span>
    </div>
  );
}

// ── Card picker popover (rank grid + suit grid) ─────────────────────────────
function CardPicker({ used, onPick, onClose, title }) {
  return (
    <div className="ui-fade" style={styles.pickerOverlay} onClick={onClose}>
      <div className="ui-pop" style={styles.pickerBox} onClick={(e) => e.stopPropagation()}>
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
                    background: s.face,
                    border: `1px solid ${s.edge}`,
                    opacity: taken ? 0.22 : 1,
                    cursor: taken ? "not-allowed" : "pointer",
                  }}
                >
                  <span style={{ ...styles.cardRank, fontSize: 19, letterSpacing: r === "T" ? -1.5 : 0 }}>{r === "T" ? "10" : r}</span>
                  <span style={{ ...styles.cardSuit, fontSize: 9 }}>{s.symbol}</span>
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

// ── Custom-straddle popup: pick any position + any amount ────────────────────
function StraddleMenu({ seats, positions, defaultSeat, defaultAmount, onConfirm, onClose }) {
  const [seat, setSeat] = useState(defaultSeat ?? (seats[0] ? seats[0].seat : 1));
  const [amount, setAmount] = useState(defaultAmount ? String(defaultAmount) : "");
  const amt = Math.max(0, Math.round(Number(amount) || 0));
  const ok = amt > 0 && seats.length > 0;
  return (
    <div className="ui-fade" style={styles.pickerOverlay} onClick={onClose}>
      <div className="ui-pop" style={styles.confirmBox} onClick={(e) => e.stopPropagation()}>
        <div style={styles.confirmTitle}>Custom straddle</div>
        <div style={styles.confirmBody}>
          Straddle from any position for any amount — independent of the UTG 2× BB straddle.
        </div>
        <label style={styles.label}>Position</label>
        <select style={styles.input} value={seat} onChange={(e) => setSeat(Number(e.target.value))}>
          {seats.map((s) => (
            <option key={s.seat} value={s.seat}>
              {(s.name || `Seat ${s.seat}`)}{positions[s.seat] ? ` — ${positions[s.seat]}` : ` — Seat ${s.seat}`}
            </option>
          ))}
        </select>
        <label style={styles.label}>Straddle amount ($)</label>
        <input
          style={styles.input}
          type="number"
          min={1}
          value={amount}
          placeholder="e.g. 300"
          autoFocus
          onChange={(e) => setAmount(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && ok) onConfirm(seat, amt); }}
        />
        <div style={styles.confirmBtnRow}>
          <button style={{ ...styles.confirmYes, opacity: ok ? 1 : 0.5 }} disabled={!ok} onClick={() => onConfirm(seat, amt)}>
            Set straddle
          </button>
          <button style={styles.confirmNo} onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

// ── Player seat ─────────────────────────────────────────────────────────────
function Seat({ p, empty, pos, badge, isButton, isActor, folded, committed, cards, desc, isWinner, bought, bounty, onBounty, squid, onSquid, phase, sitting, sitValue, onSit, onSitChange, onSitCommit, onNameClick, onCardClick, onStackClick, editingStack, stackValue, onStackChange, onStackCommit, dataTour }) {
  const posStyle = { ...styles.seat, left: `${pos.left}%`, top: `${pos.top}%` };

  if (empty) {
    return (
      <div style={posStyle} data-tour={dataTour}>
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
    <div style={{ ...posStyle, opacity: folded ? 0.4 : 1, filter: folded ? "grayscale(0.7)" : "none" }} data-tour={dataTour}>
      {/* bet chip toward center */}
      {committed > 0 && (
        <div style={styles.betChip}>{fmtChips(committed)}</div>
      )}
      <div className={isActor ? "seat-actor" : undefined} style={{ ...styles.seatBox, ...(isActor ? { boxShadow: GOLD_GLOW, borderColor: "var(--gold)" } : isWinner ? { boxShadow: GAIN_GLOW, borderColor: "var(--gain)" } : { borderColor: "var(--line-strong)" }) }}>
        {badge && (
          <div style={{ ...styles.badge, background: BADGE_COLOR[badge] || BADGE_DEFAULT }}>{badge}</div>
        )}
        {isButton && <div style={styles.dealerBtn}>D</div>}
        {/* Side-game markers, like the dealer button: squid buttons held, bounty streak. */}
        {(squid > 0 || bounty > 0) && (
          <div style={styles.gameDiscs}>
            {squid > 0 && <div style={styles.squidDisc} title={`${squid} squid button${squid === 1 ? "" : "s"}`}>{squid}</div>}
            {bounty > 0 && <div style={styles.bountyDisc} title={`Bounty: ${bounty} in a row`}>{bounty}</div>}
          </div>
        )}
        {/* Hole cards are locked until the hand is dealt (workflow: deal → cards). */}
        <div style={{ ...styles.cardsRow, opacity: phase === "setup" ? 0.35 : 1 }} title={phase === "setup" ? "Deal a hand first" : undefined}>
          <Card card={cards[0]} size="sm" faceDownIfEmpty={phase !== "setup"} onClick={phase === "setup" ? undefined : () => onCardClick(0)} />
          <Card card={cards[1]} size="sm" faceDownIfEmpty={phase !== "setup"} onClick={phase === "setup" ? undefined : () => onCardClick(1)} />
        </div>
        <div style={{ ...styles.seatName, cursor: onNameClick ? "pointer" : "default" }} onClick={onNameClick} title={onNameClick ? "Buy the button / straddle" : undefined}>{p.name}</div>
        {bought && <div style={styles.boughtTag}>BOUGHT {bought === "str" ? "STR" : "BTN"}</div>}
        {bounty != null && (
          <div style={styles.bountyRow} title="Win streak going into this hand — written into the PT4 name">
            <button style={styles.bountyBtn} disabled={bounty <= 0} onClick={() => onBounty(-1)}>−</button>
            <span style={{ ...styles.bountyTag, opacity: bounty > 0 ? 1 : 0.45 }}>{bounty > 0 ? `${bounty}bounty` : "no streak"}</span>
            <button style={styles.bountyBtn} onClick={() => onBounty(1)}>+</button>
          </div>
        )}
        {squid != null && (
          <div style={styles.bountyRow} title="Squid buttons held going into this hand — written into the PT4 name">
            <button style={styles.bountyBtn} disabled={squid <= 0} onClick={() => onSquid(-1)}>−</button>
            <span style={{ ...styles.squidTag, opacity: squid > 0 ? 1 : 0.45 }}>{squid > 0 ? `${squid}squid` : "no buttons"}</span>
            <button style={styles.bountyBtn} onClick={() => onSquid(1)}>+</button>
          </div>
        )}
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
          <div style={{ ...styles.handDesc, color: isWinner ? "var(--gain-bright)" : "var(--text-mute)" }}>
            {isWinner ? "◆ " : ""}{desc}
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
// 8 empty seats at $10,000 — names are filled in per session (or via Restore
// Default). Empty seats render greyed out until a name is entered.
const DEFAULT_ROSTER = [
  { seat: 1, name: "", stack: 10000 },
  { seat: 2, name: "", stack: 10000 },
  { seat: 3, name: "", stack: 10000 },
  { seat: 4, name: "", stack: 10000 },
  { seat: 5, name: "", stack: 10000 },
  { seat: 6, name: "", stack: 10000 },
  { seat: 7, name: "", stack: 10000 },
  { seat: 8, name: "", stack: 10000 },
];
const DEFAULT_BUTTON_SEAT = 4;
function loadSession() {
  try {
    const raw = typeof localStorage !== "undefined" && localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}
// Write the session back. Session hands are already on the server (they're only
// recorded after a successful submit), so the local copy keeps the latest 200 and
// stores each hand's PT4 text once (it used to be stored twice, as text + pt4Text).
// If storage is still full, keep fewer rather than silently saving nothing.
function saveSession(snapshot) {
  const hands = (snapshot.sessionHands || []).slice(-200).map(({ text, ...h }) => ({ ...h, pt4Text: h.pt4Text || text || "" }));
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...snapshot, sessionHands: hands }));
  } catch {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...snapshot, sessionHands: hands.slice(-30) })); } catch { /* unavailable */ }
  }
}

// Per-stream default lineups (seat → name), keyed by video id. Mirrored to the
// server too, but cached locally so they survive offline / reloads.
const DEFAULTS_KEY = "pokerTable.defaultLineups.v1";
function loadDefaultLineups() {
  try { return JSON.parse(localStorage.getItem(DEFAULTS_KEY)) || {}; } catch { return {}; }
}

// Light parsers for server-stored PT4 text (which has no structured summary),
// so server hands can show a winner / pot / players in the Hand Manager.
function parsePt4Winner(pt4) {
  if (!pt4) return "";
  const m = pt4.match(/^(.+?) collected \$[\d,]+ from pot/m);
  return m ? m[1].trim() : "";
}
function parsePt4Pot(pt4) {
  if (!pt4) return null;
  const m = pt4.match(/Total pot \$([\d,]+)/);
  return m ? Number(m[1].replace(/,/g, "")) : null;
}
function parsePt4Players(pt4) {
  if (!pt4) return [];
  // Only the seat list at the top: the SUMMARY repeats every "Seat N:" line,
  // which counted each player twice ("6 players" for a 3-handed hand).
  const head = pt4.split("*** HOLE CARDS ***")[0];
  const out = [];
  const re = /^Seat \d+: (.+?)(?= \(| folded| showed| mucked| collected| won| lost|$)/gm;
  let m;
  while ((m = re.exec(head))) { const nm = m[1].trim(); if (nm) out.push(nm); }
  return out;
}
function parsePt4Summary(pt4) {
  const w = parsePt4Winner(pt4);
  const pot = parsePt4Pot(pt4);
  if (w && pot != null) return `${w} wins $${pot.toLocaleString()}`;
  if (w) return `${w} wins`;
  return "(hand)";
}

// ── Hand Manager panel ──────────────────────────────────────────────────────
// Right-side collapsible panel: every completed hand rendered as a selectable
// card, grouped by video and sorted date→timestamp, with range/toggle selection
// (like a file explorer), bulk + per-card delete, and drag-to-export plus
// Export Selected / Export All buttons. Hands live in App state (→ localStorage).
function HandManager({ localHands, serverHands, stream, lastTimestamp, streamId, streamUrl, onResumeFrom, onRestore, onDeleteLocal, onDeleteServer, onExportHands, onFinishStream, streamComplete, isAdmin }) {
  const [selected, setSelected] = useState(() => new Set());
  const [anchor, setAnchor] = useState(null); // last single-clicked card (range pivot)
  const [dragOver, setDragOver] = useState(false);

  // Merge server hands (completed, authoritative) with localStorage hands
  // (unsaved/in-progress). A local hand that matches a server hand by
  // (video, timestamp) is dropped in favour of the saved server copy.
  const sorted = useMemo(() => {
    const sv = (serverHands || []).map((h) => ({
      key: "s" + h.id, source: "server", serverId: h.id,
      startSec: h.timestamp_seconds || 0, timestamp: fmtClock(h.timestamp_seconds || 0),
      youtubeUrl: h.youtube_url, pt4Text: h.pt4_text, cards: h.cards_count, actions: h.actions_count,
      videoId: h.stream_id, videoDate: stream?.date, sessionLabel: stream?.title,
      summary: parsePt4Summary(h.pt4_text), potSize: parsePt4Pot(h.pt4_text),
      winner: parsePt4Winner(h.pt4_text), players: parsePt4Players(h.pt4_text),
      nextState: h.next_state || null,
    }));
    const haveServer = new Set(sv.map((c) => `${c.videoId}|${c.startSec}`));
    // Only show hands from the active stream. With no stream selected, show none.
    let loc = streamId ? (localHands || []).filter((h) => (h.videoId || "") === streamId) : [];
    const lc = loc
      .filter((h) => !haveServer.has(`${h.videoId}|${h.startSec || 0}`))
      .map((h) => ({
        key: "l" + h.n, source: "local", n: h.n,
        startSec: h.startSec || 0, timestamp: h.timestamp || fmtClock(h.startSec || 0),
        youtubeUrl: h.youtubeUrl, pt4Text: h.pt4Text || h.text, cards: h.cards, actions: h.actions,
        videoId: h.videoId, videoDate: h.videoDate, sessionLabel: h.sessionLabel,
        summary: h.summary, potSize: h.potSize, playerCount: h.playerCount,
        winner: null, players: null,
      }));
    return [...sv, ...lc].sort(cmpHand);
  }, [serverHands, localHands, stream, streamId]);

  const ids = useMemo(() => sorted.map((h) => h.key), [sorted]);

  // Effective selection — intersected with cards that still exist.
  const sel = useMemo(() => {
    const live = new Set(ids);
    return new Set([...selected].filter((id) => live.has(id)));
  }, [selected, ids]);

  // Group by video — one header per video.
  const groups = useMemo(() => {
    const m = new Map();
    for (const h of sorted) {
      const key = h.videoId || h.videoDate || "ungrouped";
      if (!m.has(key)) m.set(key, []);
      m.get(key).push(h);
    }
    return [...m.values()].map((hs) => ({
      key: hs[0].videoId || hs[0].videoDate || "ungrouped",
      videoDate: hs[0].videoDate, label: hs[0].sessionLabel, videoId: hs[0].videoId, hands: hs,
    }));
  }, [sorted]);

  const allSelected = ids.length > 0 && ids.every((id) => sel.has(id));
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(ids));

  // File-explorer selection: Ctrl/Cmd = toggle, Shift = range.
  function selectCard(e, id) {
    const idx = ids.indexOf(id);
    if (e.shiftKey && anchor != null) {
      const a = ids.indexOf(anchor);
      if (a >= 0 && idx >= 0) {
        const [lo, hi] = a < idx ? [a, idx] : [idx, a];
        setSelected(new Set(ids.slice(lo, hi + 1)));
        return;
      }
    }
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
    setAnchor(id);
  }

  async function deleteCard(card) {
    if (card.source === "server") await onDeleteServer(card.serverId);
    else onDeleteLocal(card.n);
  }
  async function deleteOne(card) {
    if (!window.confirm(`Delete hand at ${card.timestamp || "?"}?`)) return;
    await deleteCard(card);
    setSelected((prev) => { const n = new Set(prev); n.delete(card.key); return n; });
  }
  async function deleteSelected() {
    const cards = sorted.filter((h) => sel.has(h.key));
    if (!cards.length) return;
    if (!window.confirm(`Delete ${cards.length} hand${cards.length === 1 ? "" : "s"}?`)) return;
    for (const c of cards) await deleteCard(c);
    setSelected(new Set());
  }
  const exportSelected = () => {
    const hs = sorted.filter((h) => sel.has(h.key));
    if (hs.length) onExportHands(hs);
  };
  const exportAll = () => sorted.length && onExportHands(sorted);

  function onDrop(e) {
    e.preventDefault();
    setDragOver(false);
    const dragged = e.dataTransfer.getData("text/plain");
    const set = new Set(sel);
    if (dragged) set.add(dragged);
    const hs = sorted.filter((h) => set.has(h.key));
    if (hs.length) onExportHands(hs);
  }

  const span = (stream?.duration_minutes ? stream.duration_minutes * 60
    : Math.max(lastTimestamp || 0, ...sorted.map((c) => c.startSec), 1)) || 1;

  return (
    <div style={styles.hmInner}>
      <div style={styles.hmHeadRow}>
        <span style={{ fontFamily: "var(--font-display)", fontSize: 22, fontWeight: 500, color: "var(--text-strong)" }}>Hand <em style={{ color: "var(--gold)" }}>Manager</em></span>
        <span style={styles.hmCount}>{sorted.length} hand{sorted.length === 1 ? "" : "s"}</span>
      </div>

      {/* Stream timeline — markers for every completed hand, with gaps visible */}
      {streamId && (
        <div style={styles.hmTimelineWrap}>
          <div style={styles.hmTimelineHead}>
            <span>STREAM TIMELINE</span>
            <span>{stream?.duration_minutes ? `${Math.floor(stream.duration_minutes / 60)}h ${stream.duration_minutes % 60}m` : "duration unknown"}</span>
          </div>
          <div style={styles.hmTrack}>
            {lastTimestamp != null && <div style={{ ...styles.hmProgress, width: `${Math.min(100, (lastTimestamp / span) * 100)}%` }} />}
            {sorted.map((c) => (
              <div
                key={c.key}
                title={`${c.timestamp} — ${c.summary || ""}`}
                style={{ ...styles.hmMarker, left: `${Math.min(99.5, (c.startSec / span) * 100)}%`, background: c.source === "server" ? "#4ade80" : "var(--gold-bright)" }}
                onClick={() => onRestore(c)}
              />
            ))}
          </div>
          {lastTimestamp != null ? (
            <div style={styles.hmTimelineFoot}>
              <span>Last completed: <a href={`${streamUrl}?t=${lastTimestamp}`} target="_blank" rel="noreferrer" style={styles.hmLink}>{fmtClock(lastTimestamp)} ↗</a></span>
              <button style={styles.hmResumeBtn} onClick={() => onResumeFrom(`${streamUrl}?t=${lastTimestamp}`)}>Resume from {fmtClock(lastTimestamp)}</button>
            </div>
          ) : (
            <div style={styles.hmTimelineFoot}><span style={{ color: "var(--text-mute)" }}>No completed hands on this stream yet.</span></div>
          )}
          {/* Finish the stream → awards the $0.05/hand stream bonus to contributors. */}
          <div style={styles.hmFinishRow}>
            {streamComplete ? (
              <span style={styles.hmCompleteTag}>✓ Stream complete — bonus awarded</span>
            ) : (
              <button
                style={{ ...styles.hmFinishBtn, opacity: sorted.length ? 1 : 0.45 }}
                disabled={!sorted.length}
                onClick={onFinishStream}
                title="Mark this stream finished and award the $0.05/hand stream bonus"
              >Finish stream</button>
            )}
          </div>
        </div>
      )}

      {!streamId ? (
        <div style={styles.hmEmpty}>Paste a YouTube URL to see hands from that stream.</div>
      ) : sorted.length === 0 ? (
        <div style={styles.hmEmpty}>No hands yet for this stream. Complete a hand and it lands here.</div>
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
            {isAdmin && <button style={{ ...styles.hmBtn, opacity: sel.size ? 1 : 0.45 }} disabled={!sel.size} onClick={exportSelected}>Export Selected</button>}
            {isAdmin && <button style={styles.hmBtnGreen} onClick={exportAll}>Export All</button>}
            <button style={{ ...styles.hmBtnRed, opacity: sel.size ? 1 : 0.45, flex: isAdmin ? undefined : 1 }} disabled={!sel.size} onClick={deleteSelected}>Delete Selected</button>
          </div>

          <div style={styles.hmList}>
            {groups.map((g) => (
              <div key={g.key}>
                <div style={styles.hmGroupHead}>
                  {g.videoDate ? fmtDate(g.videoDate) : (g.label || g.videoId || "Hands")}{g.videoDate && g.label ? ` — ${g.label}` : ""}
                </div>
                {g.hands.map((c) => {
                  const isSel = sel.has(c.key);
                  return (
                    <div
                      key={c.key}
                      draggable
                      onDragStart={(e) => {
                        if (!sel.has(c.key)) { setSelected(new Set([c.key])); setAnchor(c.key); }
                        e.dataTransfer.effectAllowed = "copy";
                        e.dataTransfer.setData("text/plain", c.key);
                      }}
                      onClick={(e) => { if (e.shiftKey || e.ctrlKey || e.metaKey) selectCard(e, c.key); else onRestore(c); }}
                      className="ui-hover-lift"
                      style={{ ...styles.hmCard, ...(isSel ? styles.hmCardSel : {}) }}
                      title="Click to restore the table to after this hand"
                    >
                      <div style={styles.hmCardTop}>
                        <input
                          type="checkbox"
                          checked={isSel}
                          title="Select"
                          onClick={(e) => e.stopPropagation()}
                          onChange={(e) => selectCard(e, c.key)}
                          style={{ marginRight: 2 }}
                        />
                        <span style={styles.hmTime}>{c.timestamp || "—"}</span>
                        {c.youtubeUrl && (
                          <a
                            href={c.youtubeUrl}
                            target="_blank"
                            rel="noreferrer"
                            title="Open YouTube at this timestamp"
                            style={styles.hmYtIcon}
                            onClick={(e) => e.stopPropagation()}
                          >↗</a>
                        )}
                        {c.source === "local" && <span style={styles.hmUnsaved}>unsaved</span>}
                        <span style={{ flex: 1 }} />
                        <button style={styles.hmCardX} title="Delete this hand" onClick={(e) => { e.stopPropagation(); deleteOne(c); }}>✕</button>
                      </div>
                      <div style={styles.hmSummary}>{c.summary || "(hand)"}</div>
                      <div style={styles.hmCardMeta}>
                        <span>{c.playerCount != null ? `${c.playerCount} to flop` : (c.players?.length ? `${c.players.length} players` : "")}</span>
                        <span>{c.potSize != null ? `Pot ${fmtChips(c.potSize)}` : ""}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>

          {isAdmin && (
            <div
              style={{ ...styles.hmDrop, ...(dragOver ? styles.hmDropActive : {}) }}
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDrop}
            >
              {dragOver ? "Release to export" : "⤓ Drop hands here to export"}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ── Hand Builder (the poker table workspace) ─────────────────────────────────
function HandBuilder({ me, refreshMe }) {
  // The saved session is read fresh on every mount. It used to be read once at
  // page load, so coming back from the Calendar or Dashboard rebuilt the table
  // from that stale snapshot — and then saved it over the newer session.
  const [saved] = useState(loadSession);
  const pick = (key, fallback) => (saved[key] !== undefined ? saved[key] : fallback);
  // A "Resume" handoff from the Calendar, consumed once on mount.
  const [pendingResume] = useState(() => readAndClearPendingResume());

  // Session (persists across hands) — initialised from any saved session.
  const [stakes, setStakes] = useState(() => pick("stakes", "50/100"));
  const [ante, setAnte] = useState(() => pick("ante", 100));
  const [buttonSeat, setButtonSeat] = useState(() => (pendingResume?.buttonSeat != null ? pendingResume.buttonSeat : pick("buttonSeat", DEFAULT_BUTTON_SEAT)));
  const [straddleCount, setStraddleCount] = useState(() => pick("straddleCount", 0)); // # of UTG-style straddles
  const [tripleBlind, setTripleBlind] = useState(() => pick("tripleBlind", false)); // SB/BB/mandatory-STR game
  const [anteGame, setAnteGame] = useState(() => pick("anteGame", false)); // ante-game: blinds + cascading straddles, BTN double
  const [anteAmount, setAnteAmount] = useState(() => pick("anteAmount", 25));
  const [buyButton, setBuyButton] = useState(() => pick("buyButton", null)); // {seat, type:'btn'|'str'}
  const [buyMenuSeat, setBuyMenuSeat] = useState(null); // seat whose buy-the-button menu is open
  // A one-off straddle from ANY position for ANY amount (distinct from the UTG
  // 2× BB straddle). {seat, amount} | null. Cleared when advancing to next hand.
  const [customStraddle, setCustomStraddle] = useState(() => pick("customStraddle", null));
  const [straddleMenuOpen, setStraddleMenuOpen] = useState(false); // custom-straddle popup
  const [roster, setRoster] = useState(() => (pendingResume?.roster?.length ? pendingResume.roster : pick("roster", DEFAULT_ROSTER)));
  const [autoNumber, setAutoNumber] = useState(() => pick("autoNumber", true)); // drag renumbers seats 1..n
  // Drag-and-drop reorder state (UI only): the row being dragged + the insertion slot.
  const [dragIdx, setDragIdx] = useState(null);
  const [dropIdx, setDropIdx] = useState(null);

  // Hand state
  const [phase, setPhase] = useState(() => pick("phase", "setup")); // setup | holecards | betting | complete
  const [eng, setEng] = useState(() => pick("eng", null));
  const [engHistory, setEngHistory] = useState(() => pick("engHistory", [])); // prior engine states for undo
  const [holeCards, setHoleCards] = useState(() => pick("holeCards", {})); // seat -> [c1,c2]
  const [board, setBoard] = useState(() => pick("board", ["", "", "", "", ""])); // run 1 (full board)
  const [winner, setWinner] = useState(() => pick("winner", ""));
  // Run-it-multiple-times (all-in): numRuns 1–4. Run 1 uses `board`; runs 2..N
  // use extraBoards (each a 5-card array that mirrors the shared pre-all-in
  // cards). runWinners holds the picked winner per run.
  const [numRuns, setNumRuns] = useState(() => pick("numRuns", 1));
  const [runsChosen, setRunsChosen] = useState(() => pick("runsChosen", false));
  const [extraBoards, setExtraBoards] = useState(() => pick("extraBoards", [])); // runs 2..N
  const [runWinners, setRunWinners] = useState(() => pick("runWinners", [])); // winner per run
  const [handNumber, setHandNumber] = useState(() => (pendingResume?.handNumber != null ? pendingResume.handNumber : pick("handNumber", 1)));
  const [youtubeLink, setYoutubeLink] = useState(() => {
    // A Resume snapshot or "Open in Hand Builder" URL wins (consumed once);
    // otherwise fall back to the saved per-hand link.
    if (pendingResume?.youtubeUrl) return pendingResume.youtubeUrl;
    try {
      const pending = localStorage.getItem(PENDING_YT_KEY);
      if (pending) { localStorage.removeItem(PENDING_YT_KEY); return pending; }
    } catch { /* ignore */ }
    return pick("youtubeLink", "");
  });
  const [videoDate, setVideoDate] = useState(() => pick("videoDate", TODAY_ISO)); // session's YouTube video date
  const [sessionLabel, setSessionLabel] = useState(() => pick("sessionLabel", "")); // stream name, e.g. "HCL Stream"
  // {videoId, startSec} of the LAST recorded hand — used to reject re-pasting the
  // exact same timestamp (every hand needs its own). Same video, different time is fine.
  const [lastHandKey, setLastHandKey] = useState(() => pick("lastHandKey", null));

  // ── Side game mode ─────────────────────────────────────────────────────────
  // A simplified flow for other, untranscribed side games:
  // no hole cards / actions / board — just count hands as positions rotate. Each
  // counted hand is submitted as a 0-piece hand so it pays $0.10 (+$0.05 on
  // stream completion) and counts toward the stream's handsCompleted, but it
  // generates no PT4 text.
  const [sideGameMode, setSideGameMode] = useState(() => pick("sideGameMode", false));
  const [sideGameType] = useState(() => {
    const t = pick("sideGameType", "Other");
    return SIDE_GAME_TYPES.includes(t) ? t : "Other";
  });
  const [sideGameOther, setSideGameOther] = useState(() => pick("sideGameOther", ""));
  const [sideGameHands, setSideGameHands] = useState(() => pick("sideGameHands", [])); // [{type,gameType,timestamp,handNumber,videoId}]
  const [endingSideGame, setEndingSideGame] = useState(false); // showing the adjust-stacks prompt
  const [sideStackDraft, setSideStackDraft] = useState({}); // seat -> stack string while confirming

  // 7-2 game: the sole winner of a hand holding 7-2 collects sevenTwoValue from
  // every other player dealt in. Hands are transcribed in full; the SIDE GAME
  // block records the holders and payouts. Started from the Side Game popup.
  const [sevenTwoActive, setSevenTwoActive] = useState(() =>
    pendingResume?.sevenTwoActive != null ? !!pendingResume.sevenTwoActive : pick("sevenTwoActive", false));
  const [sevenTwoValue, setSevenTwoValue] = useState(() =>
    pendingResume?.sevenTwoValue ?? pick("sevenTwoValue", DEFAULT_SEVEN_TWO.value));
  const [sevenTwoDetect, setSevenTwoDetect] = useState(null); // {seat,name,cards}: "is the 7-2 game on?" popup

  // Bounty game: each player's win streak going into the hand is written into
  // their PT4 name (raver2bounty). Keyed by base name, advanced on every saved
  // hand, and carried in next_state so restoring or resuming a stream picks up
  // the streaks as they stood after that hand.
  const [bountyActive, setBountyActive] = useState(() =>
    pendingResume?.bountyActive != null ? !!pendingResume.bountyActive : pick("bountyActive", false));
  const [bountyStreaks, setBountyStreaks] = useState(() =>
    pendingResume?.bountyStreaks ? pendingResume.bountyStreaks : pick("bountyStreaks", {}));
  const setBountyStreak = (name, delta) =>
    setBountyStreaks((s) => {
      const n = Math.max(0, (s[name] || 0) + delta);
      const next = { ...s };
      if (n > 0) next[name] = n;
      else delete next[name];
      return next;
    });
  const [bountySchedule, setBountySchedule] = useState(() =>
    pendingResume?.bountySchedule || pick("bountySchedule", DEFAULT_BOUNTY));

  // Squid game: buttons held per player (base name, seated or not), the round,
  // and how many buttons are still to be won. Invariant: held + left = total.
  const [squidActive, setSquidActive] = useState(() =>
    pendingResume?.squidActive != null ? !!pendingResume.squidActive : pick("squidActive", false));
  const [squidConfig, setSquidConfigState] = useState(() =>
    pendingResume?.squidConfig || pick("squidConfig", DEFAULT_SQUID));
  const [squidRound, setSquidRound] = useState(() => pendingResume?.squidRound ?? pick("squidRound", 1));
  const [squidButtons, setSquidButtons] = useState(() => pendingResume?.squidButtons || pick("squidButtons", {}));
  const [squidLeft, setSquidLeft] = useState(() => {
    const saved = pendingResume?.squidLeft ?? pick("squidLeft", null);
    if (saved != null) return saved;
    const cfg = pendingResume?.squidConfig || pick("squidConfig", DEFAULT_SQUID);
    const held = Object.values(pendingResume?.squidButtons || pick("squidButtons", {})).reduce((a, n) => a + n, 0);
    return Math.max(0, cfg.total - held);
  });
  const squidHeld = Object.values(squidButtons).reduce((a, n) => a + n, 0);
  // Changing the button count mid-round keeps everything already won.
  const setSquidConfig = (cfg) => {
    setSquidConfigState(cfg);
    setSquidLeft(Math.max(0, cfg.total - squidHeld));
  };
  // Manual correction of one player's buttons; buttons left moves the other way.
  const setSquidButton = (name, delta) => {
    const cur = squidButtons[name] || 0;
    const n = Math.max(0, cur + delta);
    const change = n - cur;
    if (change > 0 && squidLeft < change) return; // none left to hand out
    setSquidButtons((b) => {
      const next = { ...b };
      if (n > 0) next[name] = n;
      else delete next[name];
      return next;
    });
    setSquidLeft((l) => l - change);
  };
  const newSquidRound = () => {
    setSquidButtons({});
    setSquidRound((r) => r + 1);
    setSquidLeft(squidConfig.total);
  };
  // Side Game popup: null, "menu", or a game ("squid" | "progressive" | "bounty").
  const [sideDialog, setSideDialog] = useState(null);
  const squidGame = squidGameKey(squidConfig);
  // Start a side game with the structure the popup asked for. A new game starts
  // clean: no streaks, every button back in play, round 1.
  function startSideGameType(game, cfg) {
    if (game === "sevenTwo") {
      setSevenTwoValue(cfg.value);
      setSevenTwoActive(true);
    } else if (game === "bounty") {
      setBountySchedule({ start: cfg.start, first: cfg.first, step: cfg.step });
      setBountyStreaks({});
      setBountyActive(true);
    } else {
      setSquidConfigState({ total: cfg.total, value: cfg.value, mode: SIDE_GAMES[game].mode });
      setSquidButtons({});
      setSquidRound(1);
      setSquidLeft(cfg.total);
      setSquidActive(true);
    }
    setLastSideGame(null); // a summary from an earlier game would mislead
    setSideDialog(null);
  }
  // Change a running game's structure, or switch between the two squid games;
  // everything already won stays won.
  function saveSideGameType(game, cfg) {
    if (game === "sevenTwo") setSevenTwoValue(cfg.value);
    else if (game === "bounty") setBountySchedule({ start: cfg.start, first: cfg.first, step: cfg.step });
    else setSquidConfig({ total: cfg.total, value: cfg.value, mode: SIDE_GAMES[game].mode });
    setSideDialog(null);
  }
  function endSideGameType(game) {
    if (game === "sevenTwo") {
      setSevenTwoActive(false);
    } else if (game === "bounty") {
      setBountyActive(false);
      setBountyStreaks({});
    } else {
      setSquidActive(false);
      setSquidButtons({});
      setSquidLeft(squidConfig.total);
    }
    setSideDialog(null);
  }
  // Last saved hand's side-game results, for the panel.
  const [lastSideGame, setLastSideGame] = useState(() => pick("lastSideGame", null));

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
  // One timer for the toast: separate timers let an older toast's timeout
  // clear a newer message early.
  const flashTimer = useRef(null);
  const showFlash = (msg, ms = 2800) => {
    clearTimeout(flashTimer.current);
    setFlash(msg);
    flashTimer.current = setTimeout(() => setFlash(""), ms);
  };

  const named = useMemo(() => roster.filter((p) => p.name.trim()), [roster]);
  // The engine keys players by name and by seat, so two seats sharing either
  // silently scramble winners, showdowns and side pots. Caught before dealing.
  const rosterProblem = () => {
    const names = new Set();
    const seats = new Set();
    for (const p of named) {
      const key = p.name.trim().toLowerCase();
      if (names.has(key)) return `Two seats are both named "${p.name.trim()}" — every player needs a different name.`;
      names.add(key);
      if (seats.has(p.seat)) return `Seat ${p.seat} is used twice — fix the seat numbers in the roster.`;
      seats.add(p.seat);
    }
    return null;
  };

  // ── Per-stream default lineup ──────────────────────────────────────────────
  const [defaultLineups, setDefaultLineups] = useState(loadDefaultLineups);
  const currentStreamId = useMemo(() => extractVideoId(youtubeLink), [youtubeLink]);
  const currentDefault = currentStreamId ? defaultLineups[currentStreamId] : null;

  // A valid, timestamped YouTube link is required before a hand can be dealt.
  const ytReady = useMemo(() => {
    const l = youtubeLink.trim();
    return /youtube\.com|youtu\.be/i.test(l) && /[?&](?:t|start)=/.test(l) && !!extractVideoId(l);
  }, [youtubeLink]);

  // The pasted link is a DUPLICATE of the last recorded hand when it's the same
  // video AND the same timestamp. Same video with a new timestamp is allowed.
  const ytDuplicate = useMemo(() => {
    if (!ytReady || !lastHandKey) return false;
    const { id, startSec } = parseYouTube(youtubeLink);
    return !!id && id === lastHandKey.videoId && (startSec || 0) === (lastHandKey.startSec || 0);
  }, [ytReady, youtubeLink, lastHandKey]);
  // The deal button only lights up for a valid, NON-duplicate timestamped link.
  const canDeal = ytReady && !ytDuplicate;

  // ── Server-stored hands (for the Hand Manager + timeline) ──────────────────
  // {hands:[...], stream:{...}|null, last_timestamp:int|null}. Scoped to the
  // active stream when a YouTube link is set, else the user's whole history.
  const [handsData, setHandsData] = useState({ hands: [], stream: null, last_timestamp: null });
  const fetchHands = useCallback(() => {
    // Sidebar is scoped to ONE stream — without a YouTube URL there's no stream
    // to fetch. The consumers below gate on currentStreamId so nothing shows.
    if (!currentStreamId) return Promise.resolve();
    return api(`/api/hands?video_id=${currentStreamId}`)
      .then((d) => setHandsData({ hands: d.hands || [], stream: d.stream || null, last_timestamp: d.last_timestamp ?? null }))
      .catch(() => {});
  }, [currentStreamId]);
  useEffect(() => { fetchHands(); }, [fetchHands]);

  // Hands/stream data scoped to the ACTIVE stream — empty when no URL is set, so
  // a stale fetch from a previous stream never leaks into the sidebar.
  const streamHands = useMemo(() => (currentStreamId ? handsData.hands : []), [currentStreamId, handsData]);
  const streamMeta = currentStreamId ? handsData.stream : null;
  const streamLastTs = currentStreamId ? handsData.last_timestamp : null;

  // Canonical stream URL + the resume timestamp for "Resume from".
  const streamUrl = currentStreamId ? `https://youtu.be/${currentStreamId}` : "";

  // Count of hands for the ACTIVE stream only (server hands + any local hands on
  // this stream not yet mirrored to the server) — drives the Hand Manager badge.
  const streamHandCount = useMemo(() => {
    if (!currentStreamId) return 0;
    const haveServer = new Set(streamHands.map((h) => `${h.stream_id}|${h.timestamp_seconds || 0}`));
    const localExtra = (sessionHands || []).filter(
      (h) => (h.videoId || "") === currentStreamId && !haveServer.has(`${h.videoId}|${h.startSec || 0}`)
    );
    return streamHands.length + localExtra.length;
  }, [currentStreamId, streamHands, sessionHands]);

  const streamComplete = !!(streamMeta && streamMeta.is_complete);

  // Mark the active stream finished — flips is_complete, which awards the derived
  // $0.05/hand stream bonus to every contributor. Shows this worker's share.
  async function finishStream() {
    if (!currentStreamId || streamComplete) return;
    try {
      const resp = await api("/api/streams/complete", {
        method: "POST",
        body: { id: currentStreamId, complete: true, youtubeUrl: streamUrl, date: videoDate, title: sessionLabel || undefined },
      });
      const mine = (resp.shares || []).find((s) => s.user_id === (me.user && me.user.id));
      const amt = mine ? mine.bonus : 0;
      showFlash(`Stream finished — stream bonus awarded${amt ? `: +$${amt.toFixed(2)}` : ""}`, 3200);
      if (refreshMe) refreshMe();
      fetchHands();
    } catch (e) {
      setError(`Could not finish the stream: ${e.message}`);
    }
  }

  useEffect(() => {
    try { localStorage.setItem(DEFAULTS_KEY, JSON.stringify(defaultLineups)); } catch { /* ignore */ }
  }, [defaultLineups]);

  // Pull the server's saved default for the active stream (server is source of truth).
  useEffect(() => {
    if (!currentStreamId) return;
    api(`/api/streams/${currentStreamId}/default`)
      .then((d) => { if (d.lineup && d.lineup.length) setDefaultLineups((m) => ({ ...m, [currentStreamId]: d.lineup })); })
      .catch(() => {});
  }, [currentStreamId]);

  // Auto-fill the Video Date from the stream catalog whenever the YouTube link's
  // video id changes. If the stream is unknown (or has no date) the field is left
  // as-is for manual entry.
  useEffect(() => {
    if (!currentStreamId) return;
    let cancelled = false;
    api(`/api/streams/${currentStreamId}`)
      .then((s) => { if (!cancelled && s && s.date) setVideoDate(s.date); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [currentStreamId]);

  // Seats whose current name differs from the saved default (shown as a marker).
  const defaultDiffers = useMemo(() => {
    const out = new Set();
    if (!currentDefault) return out;
    const bySeat = new Map(currentDefault.map((r) => [r.seat, (r.name || "").trim()]));
    roster.forEach((p) => {
      const def = bySeat.get(p.seat);
      if (def !== undefined && def !== p.name.trim()) out.add(p.seat);
    });
    return out;
  }, [currentDefault, roster]);

  // Save current names+seats (NOT stacks) as the default for this stream.
  function setAsDefaultLineup() {
    if (!currentStreamId) return;
    const lineup = named.map((p) => ({ seat: p.seat, name: p.name.trim() }));
    setDefaultLineups((m) => ({ ...m, [currentStreamId]: lineup }));
    api(`/api/streams/${currentStreamId}/default`, { method: "POST", body: { lineup } }).catch(() => {});
  }

  // Restore default NAMES by seat; stacks are left exactly as they are.
  function restoreDefaultLineup() {
    if (!currentDefault) return;
    const bySeat = new Map(currentDefault.map((r) => [r.seat, r.name]));
    setRoster((r) => r.map((p) => (bySeat.has(p.seat) ? { ...p, name: bySeat.get(p.seat) } : p)));
  }
  // Session/roster are editable until betting actually starts — so the button
  // and stacks can still be changed after dealing or resetting a hand.
  const locked = phase === "betting" || phase === "complete";
  const positions = useMemo(() => positionLabels(named, buttonSeat), [named, buttonSeat]);
  const anteX = useMemo(() => Number(anteAmount) || 25, [anteAmount]);
  const { sb, bb } = useMemo(() => {
    if (anteGame) return { sb: anteX, bb: anteX }; // both blinds = the ante amount
    const [s, b] = stakes.split("/").map(Number);
    return { sb: s || 50, bb: b || 100 };
  }, [stakes, anteGame, anteX]);
  // Stakes string used for the PT4 header: ante game is "$X/$X".
  const ptStakes = anteGame ? `${anteX}/${anteX}` : stakes;
  // Triple-blind mandatory UTG straddle = the 3rd stakes value (e.g. 10/20/40 → $40),
  // falling back to 2× BB if omitted.
  const mandatoryStraddle = useMemo(() => Number(stakes.split("/")[2]) || 2 * bb, [stakes, bb]);
  // UTG-style straddles (UTG, UTG+1, …) computed from the count + button.
  const straddleList = useMemo(
    () => utgStraddles(named, buttonSeat, bb, straddleCount),
    [named, buttonSeat, bb, straddleCount]
  );
  // Ante-game straddles: every occupied seat between BB and BTN (clockwise) posts
  // $X, and the BTN posts $2X — action ends on the BTN (the biggest straddler).
  const anteStraddles = useMemo(() => {
    if (!anteGame) return null;
    const sorted = [...named].sort((a, b) => a.seat - b.seat);
    const n = sorted.length;
    if (n < 3) return [];
    const bi = sorted.findIndex((p) => p.seat === Number(buttonSeat));
    if (bi < 0) return [];
    const ring = Array.from({ length: n }, (_, k) => sorted[(bi + k) % n]); // [btn, sb, bb, mid…]
    const middles = ring.slice(3); // UTG … CO
    return [
      ...middles.map((p) => ({ seat: p.seat, amount: anteX })),
      { seat: ring[0].seat, amount: 2 * anteX }, // BTN posts the double
    ];
  }, [anteGame, named, buttonSeat, anteX]);
  // The straddles actually posted this hand.
  const activeStraddles = useMemo(() => {
    if (anteGame) return anteStraddles || [];
    // A custom straddle (any position, any amount) takes precedence over the
    // UTG-style straddle and is the sole straddle for the hand.
    if (customStraddle && customStraddle.amount > 0 && named.some((p) => p.seat === customStraddle.seat)) {
      return [{ seat: customStraddle.seat, amount: customStraddle.amount }];
    }
    if (!tripleBlind) return straddleList;
    const count = straddleCount > 0 ? 2 : 1;
    const seats = utgStraddles(named, buttonSeat, bb, count); // reuse seat selection
    return seats.map((st, i) => ({ seat: st.seat, amount: i === 0 ? mandatoryStraddle : 2 * mandatoryStraddle }));
  }, [anteGame, anteStraddles, customStraddle, tripleBlind, straddleCount, straddleList, named, buttonSeat, bb, mandatoryStraddle]);

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
    if (numRuns >= 2) extraBoards.forEach((b) => (b || []).forEach((c) => c && set.add(c)));
    return set;
  }, [holeCards, board, extraBoards, numRuns]);

  // ── Derived flow flags ────────────────────────────────────────────────────
  const inBetting = phase === "betting" && eng;
  const hasActor = inBetting && eng.actorSeat != null;
  const needBoard = inBetting && eng.actorSeat == null && !eng.handOver && eng.street !== "river" && (eng.streetComplete || eng.bettingClosed);
  const nextStreet = needBoard ? STREETS[STREETS.indexOf(eng.street) + 1] : null;
  const canComplete = inBetting && eng.actorSeat == null && (eng.handOver || eng.street === "river");
  const surv = eng ? survivors(eng) : [];

  // ── Run-it-multiple-times (all-in runout) flow ─────────────────────────────
  const allInStreetIdx = eng ? STREETS.indexOf(eng.street) : 0;
  const sharedCount = [0, 3, 4, 5][allInStreetIdx] || 0; // board cards dealt pre-all-in
  // All-in detected before the river: everyone left is all-in / can't bet more.
  const allInRunout = needBoard && eng.bettingClosed;
  const showRunPrompt = allInRunout && !runsChosen;
  const showMultiRun = allInRunout && runsChosen && numRuns >= 2;
  const showDealNext = needBoard && !allInRunout || (allInRunout && runsChosen && numRuns === 1);
  // Choose how many times to run it; seed each extra run with the shared cards.
  function chooseRuns(n) {
    setNumRuns(n);
    setRunsChosen(true);
    if (n >= 2) {
      const seedShared = (b) => board.map((c, i) => (i < sharedCount ? b[i] ?? c : ""));
      setExtraBoards(Array.from({ length: n - 1 }, () => seedShared(board)));
      setRunWinners(Array.from({ length: n }, () => surv[0] || ""));
    } else {
      setExtraBoards([]);
      setRunWinners([]);
    }
  }
  const setRunWinner = (i, name) => setRunWinners((w) => { const c = [...w]; c[i] = name; return c; });
  const runBoardsAll = numRuns >= 2 ? [board, ...extraBoards] : [board];

  // ── Piece-rate earnings tracker ───────────────────────────────────────────
  // Live count of billable pieces for the CURRENT hand: each filled hole/board
  // card + each voluntary action (auto blind/ante/straddle posts don't count).
  const handPieces = useMemo(() => {
    const cards =
      Object.values(holeCards).reduce((s, arr) => s + arr.filter(Boolean).length, 0) +
      board.filter(Boolean).length +
      (numRuns >= 2 ? extraBoards.reduce((s, b) => s + (b || []).filter(Boolean).length, 0) : 0);
    const actions = eng
      ? Object.values(eng.actionsByStreet).reduce(
          (s, arr) => s + arr.filter((a) => !BLIND_POSTS.has(a.action)).length,
          0
        )
      : 0;
    return { cards, actions, total: cards + actions };
  }, [holeCards, board, extraBoards, numRuns, eng]);

  // When every surviving player has hole cards AND the board is complete, the
  // showdown can be evaluated authoritatively — the winner is no longer a guess.
  const showdownEval = useMemo(() => {
    if (!eng || numRuns >= 2) return null;
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
  }, [eng, holeCards, board, numRuns]);

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

  // ── Auto-persist the full session ─────────────────────────────────────────
  // Debounced: serialising the whole session (engine history, saved hands) on
  // every keystroke made typing lag. The latest snapshot is flushed when the tab
  // hides or closes and when the builder unmounts, so nothing is lost.
  const persistRef = useRef(null);
  useEffect(() => {
    persistRef.current = {
          stakes, ante, buttonSeat, straddleCount, tripleBlind, anteGame, anteAmount, buyButton, customStraddle, roster, autoNumber,
          phase, eng, engHistory, holeCards, board, numRuns, runsChosen, extraBoards, runWinners,
          winner, handNumber, youtubeLink, videoDate, sessionLabel, sessionHands, preview, evalResult,
          sideGameMode, sideGameType, sideGameOther, sideGameHands, sevenTwoActive, sevenTwoValue, lastHandKey,
          bountyActive, bountyStreaks, bountySchedule,
          squidActive, squidConfig, squidRound, squidLeft, squidButtons, lastSideGame,
    };
    const t = setTimeout(() => saveSession(persistRef.current), 400);
    return () => clearTimeout(t);
  }, [stakes, ante, buttonSeat, straddleCount, tripleBlind, anteGame, anteAmount, buyButton, customStraddle, roster, autoNumber, phase, eng, engHistory, holeCards, board, numRuns, runsChosen, extraBoards, runWinners, winner, handNumber, youtubeLink, videoDate, sessionLabel, sessionHands, preview, evalResult, sideGameMode, sideGameType, sideGameOther, sideGameHands, sevenTwoActive, sevenTwoValue, lastHandKey, bountyActive, bountyStreaks, bountySchedule, squidActive, squidConfig, squidRound, squidLeft, squidButtons, lastSideGame]);
  useEffect(() => {
    const flush = () => { if (persistRef.current) saveSession(persistRef.current); };
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", flush);
    return () => {
      flush();
      window.removeEventListener("pagehide", flush);
      document.removeEventListener("visibilitychange", flush);
    };
  }, []);


  // Board cards required to deal the next street
  function boardReadyFor(street) {
    if (street === "flop") return board[0] && board[1] && board[2];
    if (street === "turn") return board[3];
    if (street === "river") return board[4];
    return true;
  }

  // ── Roster editing ────────────────────────────────────────────────────────
  // Roster is now ordered TOP-TO-BOTTOM by array position (no longer sorted by
  // seat at render). With "Auto-number seats" on, seats are kept as 1..n by
  // position; with it off, seats are whatever the user typed (gaps allowed).
  const renumber = (arr) => arr.map((p, i) => ({ ...p, seat: i + 1 }));
  const applyAuto = (arr) => (autoNumber ? renumber(arr) : arr);

  // By-seat edit (used by the poker-table sit/stack menus, which key off seat).
  const updateRoster = (seat, patch) =>
    setRoster((r) => r.map((p) => (p.seat === seat ? { ...p, ...patch } : p)));
  // By-index edits for the roster PANEL rows (display order == array order).
  const updateRosterAt = (index, patch) =>
    setRoster((r) => r.map((p, i) => (i === index ? { ...p, ...patch } : p)));
  const emptySeatAt = (index) => updateRosterAt(index, { name: "" });
  const removeSeatAt = (index) =>
    setRoster((r) => applyAuto(r.filter((_, i) => i !== index)));

  const addSeat = () => {
    if (roster.length >= 9) return;
    setRoster((r) => {
      const used = new Set(r.map((p) => p.seat));
      let s = 1;
      while (used.has(s) && s <= 9) s++;
      return applyAuto([...r, { seat: s, name: "", stack: 10000 }]); // appended at the bottom
    });
  };

  // Move the dragged row to an insertion slot (0..length), then renumber if auto.
  const moveRosterRow = (from, to) => {
    if (from == null || to == null) return;
    setRoster((r) => {
      const arr = [...r];
      const [item] = arr.splice(from, 1);
      const dest = from < to ? to - 1 : to; // removing an earlier row shifts the target
      arr.splice(dest, 0, item);
      return applyAuto(arr);
    });
  };

  // ── Card editing ──────────────────────────────────────────────────────────
  // The picker steps through a group of slots: hole cards [0,1], the flop
  // [0,1,2], turn [3], river [4]. After a pick it advances to the next still-
  // empty slot in the group and only closes once the group is filled.
  const openHole = (seat, idx) => setEditor({ kind: "hole", seat, group: [0, 1], cur: idx });
  const openBoard = (idx) => setEditor({ kind: "board", group: idx <= 2 ? [0, 1, 2] : [idx], cur: idx });
  // Extra runs (2..N): `run` is the index into extraBoards (0 = run 2).
  const openRunBoard = (run, idx) => setEditor({ kind: "run", run, group: idx <= 2 ? [0, 1, 2] : [idx], cur: idx });
  const pickCard = (c) => {
    if (!editor) return;
    const { kind, seat, run, group, cur } = editor;
    const base = kind === "hole" ? (holeCards[seat] ? [...holeCards[seat]] : ["", ""])
      : kind === "board" ? [...board]
      : [...(extraBoards[run] || ["", "", "", "", ""])];
    base[cur] = c;
    if (kind === "hole") {
      setHoleCards((hc) => ({ ...hc, [seat]: base }));
      // 7-2 detection: as soon as a player's two hole cards form a 7-2 and the
      // game isn't already on, offer to enable it.
      if (is72(base) && !sevenTwoActive && !sevenTwoDetect) {
        const player = named.find((p) => p.seat === seat);
        if (player) setSevenTwoDetect({ seat, name: player.name, cards: base });
      }
    }
    else if (kind === "board") setBoard(base);
    else setExtraBoards((arr) => { const cpy = arr.map((b) => [...b]); cpy[run] = base; return cpy; });
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
      initHand({ players: named, buttonSeat, sb, bb, ante: anteGame ? 0 : Number(ante) || 0, straddles: buyButton ? [] : activeStraddles, buyButton })
    );
    setEngHistory([]);
    setHoleCards({});
    setBoard(["", "", "", "", ""]);
    setNumRuns(1);
    setRunsChosen(false);
    setExtraBoards([]);
    setRunWinners([]);
    setWinner("");
    setPreview("");
    setError("");
    setPhase("holecards");
  }

  function dealHand() {
    setError("");
    if (!ytReady) {
      setError("Paste a timestamped YouTube link to deal.");
      return;
    }
    if (ytDuplicate) {
      setError("This is the same timestamp as the last hand. Each hand needs its own timestamp.");
      return;
    }
    if (named.length < 2) {
      setError("Add at least 2 players in the sidebar.");
      setSidebarOpen(true);
      return;
    }
    const problem = rosterProblem();
    if (problem) {
      setError(problem);
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
    const problem = rosterProblem();
    if (problem) {
      setError(problem);
      setSidebarOpen(true);
      return;
    }
    if (!named.some((p) => p.seat === Number(buttonSeat))) {
      setError("Button seat must be an occupied seat.");
      setSidebarOpen(true);
      return;
    }
    setEng(
      initHand({ players: named, buttonSeat, sb, bb, ante: anteGame ? 0 : Number(ante) || 0, straddles: buyButton ? [] : activeStraddles, buyButton })
    );
    setEngHistory([]);
    setPhase("betting");
  }

  // Each engine transition snapshots the prior state so it can be undone.
  // A double-click would apply a second action — to the NEXT player, since the
  // same button now belongs to them — so presses within 200ms are ignored.
  const lastActAt = useRef(0);
  const act = (type, amount) => {
    const now = Date.now();
    if (now - lastActAt.current < 200) return;
    lastActAt.current = now;
    setEngHistory((h) => [...h, eng]);
    setEng((s) => applyAction(s, { type, amount }));
  };
  const dealNextStreet = () => {
    setEngHistory((h) => [...h, eng]);
    setEng((st) => advanceStreet(st));
  };

  // One click: validate the YouTube link, auto-generate the PT4 text, save it to
  // the session, flash confirmation, and advance to the next hand.
  // A second click while the first save is in flight would submit (and pay
  // for) the same hand twice.
  const submittingRef = useRef(false);
  // A hand that moves side-game money waits for a yes: {bounty, squid, …}.
  const [sidePayConfirm, setSidePayConfirm] = useState(null);
  async function completeHand(opts) {
    if (submittingRef.current) return;
    const sideConfirmed = opts?.sideConfirmed === true;
    submittingRef.current = true;
    try { await runCompleteHand({ sideConfirmed }); } finally { submittingRef.current = false; }
  }
  async function runCompleteHand({ sideConfirmed = false } = {}) {
    setError("");
    // 1. YouTube link validation
    const link = youtubeLink.trim();
    if (!link) return setError("Add a timestamped YouTube link before completing.");
    if (!/youtube\.com|youtu\.be/i.test(link)) return setError("Enter a valid YouTube link (youtube.com or youtu.be).");
    if (!/[?&](?:t|start)=/.test(link)) return setError("This link has no timestamp. Add ?t=SECONDS to the URL.");
    // 2. Winner must be determinable. For a single board that means cards in (so
    //    the evaluator can score it); for multi-run the worker picks each winner.
    if (numRuns < 2 && surv.length >= 2 && !autoEval) {
      return setError("Enter hole cards for every player still in so the winner can be scored.");
    }
    if (numRuns >= 2 && runWinners.slice(0, numRuns).some((w) => !w)) {
      return setError("Pick a winner for every run.");
    }

    try {
      const effWinner = autoEval ? autoEval.winner : winner || surv[0];
      const effWinners = autoEval ? autoEval.winners : surv.length === 1 ? [surv[0]] : [winner || surv[0]];
      const hand = numRuns >= 2
        ? buildHandDict(eng, { stakes: ptStakes, holeCards, board, runBoards: runBoardsAll, runWinners, numRuns, allInStreetIdx, positions: buyButton ? {} : positions })
        : buildHandDict(eng, { stakes: ptStakes, holeCards, board, winner: effWinner, winners: effWinners, positions: buyButton ? {} : positions });
      if (buyButton) hand.buy_button_seat = buyButton.seat;
      const { url, startSec, id: videoId } = parseYouTube(link);
      // PT4 header time comes from the YouTube timestamp (t=), never the clock —
      // 00:00:00 when the link carries no timestamp.
      hand.timestamp_start = secsToHMS(startSec);
      if (url) hand.table_name = url; // timestamped link → PT4 table name
      if (videoDate) hand.video_date = videoDate; // video date → PT4 header date

      // Bounty / squid: who won the hand (main pot, first board), what each game
      // pays, the names the hand history carries (holdings ENTERING the hand),
      // and the state the next hand starts from — payouts included in stacks.
      // The roster keeps base names; only the submitted dict is renamed.
      const dealtIn = [...eng.players].sort((a, b) => a.seat - b.seat).map((p) => ({ seat: p.seat, name: p.name }));
      const won = mainPotWinners(hand);
      const bountyRes = bountyActive ? settleBounty({ dealtIn, streaks: bountyStreaks, schedule: bountySchedule, winners: won }) : null;
      const squidRes = squidActive
        ? settleSquid({ dealtIn, buttons: squidButtons, config: squidConfig, round: squidRound, left: squidLeft, winners: won })
        : null;
      // Streaks and buttons move on their own; money waits for a yes. A bounty
      // being paid or the squid round settling asks first, and No goes back
      // unsaved so the streaks or buttons can be fixed.
      const sevenTwoRes = sevenTwoActive
        ? settleSevenTwo({ dealtIn, holeCards, value: sevenTwoValue, winners: won })
        : null;
      if (!sideConfirmed && (bountyRes?.payouts.length || squidRes?.settled || sevenTwoRes?.payouts.length)) {
        setSidePayConfirm({
          bounty: bountyRes?.payouts.length ? bountyRes : null,
          squid: squidRes?.settled ? squidRes : null,
          sevenTwo: sevenTwoRes?.payouts.length ? sevenTwoRes : null,
          sevenTwoValue,
          squidValue: squidConfig.value,
          squidTotal: squidConfig.total,
        });
        return;
      }
      const outHand = applySideGameNames(hand, bountyActive ? bountyStreaks : {}, squidActive ? squidButtons : {});
      if (bountyRes || squidRes || sevenTwoRes) {
        outHand.side_game = sideGamePayload({
          bounty: bountyRes, bountySchedule, squid: squidRes, squidConfig, squidRound,
          sevenTwo: sevenTwoRes, sevenTwoValue,
        });
      }
      const side = {
        streaks: bountyRes ? bountyRes.nextStreaks : bountyStreaks,
        squid: squidRes ? squidRes.next : null,
        deltas: sideGameDeltas(bountyRes?.payouts, squidRes?.payouts, sevenTwoRes?.payouts),
      };

      const n = handNumber;
      const { cards, actions } = handPieces;

      // ── Hand-card metadata for the Hand Manager (computed locally, no PT4) ──
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

      // Submit to the server, which FORMATS the PT4 server-side and stores it.
      // Only admins get the text back — workers never receive raw PT4. next_state
      // snapshots the table for the NEXT hand so the stream can be resumed.
      let resp;
      try {
        resp = await api("/api/hands/submit", {
          method: "POST",
          body: {
            hand: outHand, hand_index: n - 1, stream_url: url, start_sec: startSec, end_sec: startSec,
            stream_id: videoId, youtube_url: url, timestamp_seconds: startSec,
            cards_count: cards, actions_count: actions, next_state: computeNextHandState(side),
          },
        });
      } catch (ex) {
        return setError(ex.message || "Could not save the hand.");
      }
      const pt4 = resp.pt4_text || ""; // present for admins only

      setSessionHands((hs) =>
        [
          ...hs.filter((h) => h.n !== n),
          { n, text: pt4, pt4Text: pt4, cards, actions, youtubeUrl: url, videoId,
            timestamp: startSec > 0 ? fmtClock(startSec) : "", startSec, videoDate,
            sessionLabel, summary, potSize, playerCount },
        ].sort((a, b) => a.n - b.n)
      );
      setHandPanelOpen(true); // surface the new card in the Hand Manager
      setLastHandKey({ videoId, startSec }); // block re-using this exact timestamp
      if (refreshMe) refreshMe();
      fetchHands();

      // Earnings breakdown toast.
      const cardPay = (cards * PIECE_RATE).toFixed(2);
      const actPay = (actions * PIECE_RATE).toFixed(2);
      const sideEvents = (bountyRes?.payouts.length ? 1 : 0) + (squidRes?.settled ? 1 : 0) + (sevenTwoRes?.payouts.length ? 1 : 0);
      const total = (cards * PIECE_RATE + actions * PIECE_RATE + COMPLETION_BONUS + sideEvents * SIDE_PAYOUT_BONUS).toFixed(2);
      const sidePaid = [...(bountyRes?.payouts || []), ...(squidRes?.payouts || []), ...(sevenTwoRes?.payouts || [])].reduce((a, p) => a + p.amount, 0);
      showFlash(`Hand #${n}: ${cards} cards ($${cardPay}) + ${actions} actions ($${actPay}) + bonus ($${COMPLETION_BONUS.toFixed(2)})`
        + (sideEvents ? ` + side-game payout ($${(sideEvents * SIDE_PAYOUT_BONUS).toFixed(2)})` : "") + ` = $${total}`
        + (sidePaid ? ` · side games moved $${sidePaid.toLocaleString()} (in the next stacks)` : ""), sidePaid ? 4200 : 2800);
      if (bountyRes) setBountyStreaks(bountyRes.nextStreaks);
      if (squidRes) {
        setSquidButtons(squidRes.next.buttons);
        setSquidRound(squidRes.next.round);
        setSquidLeft(squidRes.next.left);
      }
      if (bountyRes || squidRes || sevenTwoRes) {
        setLastSideGame({
          hand: n,
          bounty: bountyRes && { payouts: bountyRes.payouts },
          squid: squidRes && { payouts: squidRes.payouts, settled: squidRes.settled },
          sevenTwo: sevenTwoRes && { payouts: sevenTwoRes.payouts },
        });
      }
      nextHand(side.deltas); // carries over stacks (+ side-game money), rotates button, clears, increments hand #
    } catch (e) {
      setError(`Couldn't save the hand: ${e.message}`);
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
        tripleBlind,
        anteGame,
        anteAmount,
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
      if (c.tripleBlind != null) setTripleBlind(c.tripleBlind);
      if (c.anteGame != null) setAnteGame(c.anteGame);
      if (c.anteAmount != null) setAnteAmount(c.anteAmount);
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
    setButtonSeat(DEFAULT_BUTTON_SEAT);
    setStraddleCount(0);
    setTripleBlind(false);
    setAnteGame(false);
    setAnteAmount(25);
    setBuyButton(null);
    setCustomStraddle(null);
    setRoster(DEFAULT_ROSTER);
    setEng(null);
    setEngHistory([]);
    setHoleCards({});
    setBoard(["", "", "", "", ""]);
    setNumRuns(1);
    setRunsChosen(false);
    setExtraBoards([]);
    setRunWinners([]);
    setWinner("");
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
    setSevenTwoActive(false);
    setSevenTwoDetect(null);
    setLastHandKey(null);
    setBountyActive(false);
    setBountyStreaks({});
    setSquidActive(false);
    setSquidButtons({});
    setSquidRound(1);
    setSquidLeft(squidConfig.total);
    setLastSideGame(null);
    setPhase("setup");
  }

  // Snapshot of the table for the NEXT hand (ending stacks carried over, button
  // rotated, hand # bumped) — used as the stream's server-side resume point.
  // Side-game payouts move chips between hands: add them to the carried-over
  // stacks (never below 0 — a player short of what they owe tops up from their
  // pocket). Applied after busts, so paying a side game never stands anyone up.
  const applyDeltas = (rows, deltas) =>
    deltas && Object.keys(deltas).length
      ? rows.map((p) => (p.name.trim() && deltas[p.name] ? { ...p, stack: Math.max(0, p.stack + deltas[p.name]) } : p))
      : rows;

  function computeNextHandState(side = {}) {
    let newRoster = roster;
    if (eng) {
      const resolved = eng.players.filter((p) => !p.folded).length === 1 || eng.handOver || eng.street === "river" || numRuns >= 2 || phase === "complete";
      if (resolved) {
        const ends = computeEndStacks(eng, { numRuns, runWinners, winner: autoEval ? autoEval.winner : winner, winners: autoEval ? autoEval.winners : winner ? [winner] : [], holeCards, board });
        newRoster = roster.map((p) => {
          if (ends[p.seat] == null) return p;
          const s = ends[p.seat];
          return s <= 0 ? { ...p, name: "", stack: 0 } : { ...p, stack: s };
        });
      }
    }
    newRoster = applyDeltas(newRoster, side.deltas);
    const occ = newRoster.filter((p) => p.name.trim()).sort((a, b) => a.seat - b.seat);
    let nextSeat = Number(buttonSeat);
    if (occ.length) {
      const i = occ.findIndex((p) => p.seat === Number(buttonSeat));
      nextSeat = i >= 0 ? occ[(i + 1) % occ.length].seat : (occ.find((p) => p.seat > Number(buttonSeat)) || occ[0]).seat;
    }
    return {
      roster: newRoster.map(({ seat, name, stack }) => ({ seat, name, stack })),
      buttonSeat: nextSeat,
      handNumber: handNumber + 1,
      bountyActive,
      bountyStreaks: side.streaks ?? bountyStreaks,
      bountySchedule,
      squidActive,
      squidConfig,
      squidRound: side.squid ? side.squid.round : squidRound,
      squidLeft: side.squid ? side.squid.left : squidLeft,
      squidButtons: side.squid ? side.squid.buttons : squidButtons,
      sevenTwoActive,
      sevenTwoValue,
    };
  }

  // Restore the table to immediately AFTER a clicked hand: load its post-hand
  // snapshot (names/seats/stacks + rotated button + next hand #), set the
  // YouTube link to that hand's timestamp, and clear any in-progress hand so the
  // worker can deal the next one straight away.
  function restoreAfterHand(card) {
    const snap = card && card.nextState;
    if (!snap || !Array.isArray(snap.roster) || !snap.roster.length) {
      // No snapshot (older / unsaved hand) — fall back to opening the video.
      if (card && card.youtubeUrl) window.open(card.youtubeUrl, "_blank", "noopener");
      return;
    }
    setRoster(snap.roster.map((p) => ({ seat: p.seat, name: p.name || "", stack: p.stack || 0 })));
    if (snap.buttonSeat != null) setButtonSeat(snap.buttonSeat);
    if (snap.handNumber != null) setHandNumber(snap.handNumber);
    if (snap.bountyActive != null) setBountyActive(!!snap.bountyActive);
    setBountyStreaks(snap.bountyStreaks || {});
    if (snap.bountySchedule) setBountySchedule(snap.bountySchedule);
    if (snap.sevenTwoActive != null) setSevenTwoActive(!!snap.sevenTwoActive);
    if (snap.sevenTwoValue != null) setSevenTwoValue(snap.sevenTwoValue);
    // A snapshot without squid data (older, or imported) means no buttons held.
    if (snap.squidActive != null) setSquidActive(!!snap.squidActive);
    const cfg = snap.squidConfig || squidConfig;
    const btns = snap.squidButtons || {};
    setSquidConfigState(cfg);
    setSquidButtons(btns);
    if (snap.squidRound != null) setSquidRound(snap.squidRound);
    setSquidLeft(snap.squidLeft ?? Math.max(0, cfg.total - Object.values(btns).reduce((a, n) => a + n, 0)));
    // YouTube link at this hand's timestamp (the stored url already carries ?t=).
    let link = card.youtubeUrl || "";
    if (card.startSec > 0 && !/[?&]t=/.test(link)) link += (link.includes("?") ? "&" : "?") + "t=" + card.startSec;
    setYoutubeLink(link);
    // Clear the in-progress hand → ready for the next one.
    setEng(null); setEngHistory([]); setHoleCards({});
    setBoard(["", "", "", "", ""]);
    setNumRuns(1); setRunsChosen(false); setExtraBoards([]); setRunWinners([]);
    setWinner(""); setPreview(""); setError(""); setBuyButton(null); setBuyMenuSeat(null); setCustomStraddle(null);
    setPhase("setup");
    // Live hands carry a session hand number; imported/parsed snapshots don't —
    // fall back to the local hand # or the timestamp for the confirmation label.
    const afterLabel =
      snap.handNumber != null ? `Hand #${snap.handNumber - 1}`
        : card.n ? `Hand #${card.n}`
        : card.timestamp ? `the hand at ${card.timestamp}`
        : "that hand";
    showFlash(`Restored to after ${afterLabel} — ready for next hand`, 3200);
  }

  function nextHand(deltas) {
    // Carry over ending stacks when the hand reached a result; players who bust
    // (≤ 0 chips) are stood up but their seat stays at the table.
    let newRoster = roster;
    if (eng) {
      const survList = eng.players.filter((p) => !p.folded);
      // The hand reached a result whenever it's uncontested, ran to the river,
      // was an all-in run-out (incl. multi-run), or was explicitly completed.
      const resolved = survList.length === 1 || eng.handOver || eng.street === "river" || numRuns >= 2 || phase === "complete";
      if (resolved) {
        const ends = computeEndStacks(eng, { numRuns, runWinners, winner: autoEval ? autoEval.winner : winner, winners: autoEval ? autoEval.winners : winner ? [winner] : [], holeCards, board });
        newRoster = roster.map((p) => {
          if (ends[p.seat] == null) return p;
          const s = ends[p.seat];
          return s <= 0 ? { ...p, name: "", stack: 0 } : { ...p, stack: s };
        });
      }
    }
    newRoster = applyDeltas(newRoster, deltas);
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
    setNumRuns(1);
    setRunsChosen(false);
    setExtraBoards([]);
    setRunWinners([]);
    setWinner("");
    setPreview("");
    setError("");
    setYoutubeLink("");
    setBuyButton(null);
    setBuyMenuSeat(null);
    setCustomStraddle(null);
    setPhase("setup");
    setHandNumber((h) => h + 1);
  }

  // ── Side game mode ─────────────────────────────────────────────────────────
  const sideGameLabel =
    sideGameType === "Other" ? (sideGameOther.trim() || "Side Game") : sideGameType;
  const sideGameEarnings = sideGameHands.length * SIDE_GAME_RATE;

  // ── Undo / redo (Ctrl+Z / Ctrl+Y) ────────────────────────────────────────
  // Every change to the hand in progress is one step: an action or bet, a
  // card, the board, runs and winners, the button, stacks, side-game streaks
  // and buttons. Undo steps back a change, Redo forward again. A saved hand
  // (a new hand number) starts a fresh history, so Undo never reaches into a
  // hand that's already on the server. Changes are noticed while rendering
  // (React's "adjust state when inputs change" pattern), so none are missed.
  const handSnap = {
    phase, eng, engHistory, holeCards, board, extraBoards, numRuns, runsChosen,
    runWinners, winner, buyButton, customStraddle, buttonSeat, roster, bountyStreaks, squidButtons, squidLeft,
  };
  const [hist, setHist] = useState(() => ({ past: [], future: [], last: handSnap, hand: handNumber }));
  if (hist.hand !== handNumber) {
    setHist({ past: [], future: [], last: handSnap, hand: handNumber });
  } else if (HIST_KEYS.some((k) => hist.last[k] !== handSnap[k])) {
    setHist({ past: [...hist.past, hist.last].slice(-300), future: [], last: handSnap, hand: handNumber });
  }
  const canUndo = hist.past.length > 0;
  const canRedo = hist.future.length > 0;
  function applySnap(s) {
    setPhase(s.phase); setEng(s.eng); setEngHistory(s.engHistory);
    setHoleCards(s.holeCards); setBoard(s.board); setExtraBoards(s.extraBoards);
    setNumRuns(s.numRuns); setRunsChosen(s.runsChosen); setRunWinners(s.runWinners); setWinner(s.winner);
    setBuyButton(s.buyButton); setCustomStraddle(s.customStraddle); setButtonSeat(s.buttonSeat); setRoster(s.roster);
    setBountyStreaks(s.bountyStreaks); setSquidButtons(s.squidButtons); setSquidLeft(s.squidLeft);
  }
  function undoChange() {
    if (!hist.past.length) return;
    const prev = hist.past[hist.past.length - 1];
    setHist({ past: hist.past.slice(0, -1), future: [handSnap, ...hist.future], last: prev, hand: handNumber });
    applySnap(prev);
  }
  function redoChange() {
    if (!hist.future.length) return;
    const next = hist.future[0];
    setHist({ past: [...hist.past, handSnap], future: hist.future.slice(1), last: next, hand: handNumber });
    applySnap(next);
  }

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
    if (submittingRef.current) return;
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
    submittingRef.current = true;
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
    finally { submittingRef.current = false; }

    // Positions auto-rotate: move the button to the next occupied seat.
    const occ = named.slice().sort((a, b) => a.seat - b.seat);
    if (occ.length) {
      const i = occ.findIndex((p) => p.seat === Number(buttonSeat));
      const nextSeat = i >= 0 ? occ[(i + 1) % occ.length].seat : occ[0].seat;
      setButtonSeat(nextSeat);
    }
    setHandNumber((h) => h + 1);
    showFlash(`Side game hand #${n} recorded · +$${SIDE_GAME_RATE.toFixed(2)}`, 1800);
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
    // Firefox only downloads from an anchor that's in the document, and revoking
    // the URL in the same tick can cancel the download before it starts.
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // Remove hands by number; the persistence effect mirrors the change to storage.
  function deleteHandIds(idsArr) {
    const drop = new Set(idsArr);
    setSessionHands((hs) => hs.filter((h) => !drop.has(h.n)));
  }
  const deleteLocalHand = (n) => deleteHandIds([n]);
  async function deleteServerHand(id) {
    try { await api(`/api/hands/${id}`, { method: "DELETE" }); } catch { /* ignore */ }
    fetchHands();
    if (refreshMe) refreshMe();
  }

  // ── Keyboard ──────────────────────────────────────────────────────────────
  // F fold · C check/call · R bet/raise to the amount shown · A all-in · Enter
  // for whatever's next (deal, start betting, deal the street, complete) · Z or
  // Ctrl+Z undo · Ctrl+Y (or Ctrl+Shift+Z) redo. Ignored while typing in a
  // field or with a dialog open. The
  // handler reads the latest state through a ref so it's registered once.
  const keyState = useRef(null);
  useEffect(() => {
    keyState.current = {
      modal: !!(editor || sevenTwoDetect || straddleMenuOpen || buyMenuSeat != null || sideDialog || sidePayConfirm),
      sideGameMode, phase, hasActor, legal, betTo, canDeal, showDealNext, nextStreet, canComplete, showRunPrompt,
      act, dealHand, startBetting, dealNextStreet, completeHand, undoChange, redoChange, boardReadyFor,
    };
  });
  useEffect(() => {
    const onKey = (e) => {
      const k = keyState.current;
      if (!k || k.modal || k.sideGameMode || e.altKey) return;
      const t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
      const key = e.key.toLowerCase();
      const ctrl = e.ctrlKey || e.metaKey;
      if (ctrl && key !== "z" && key !== "y") return;
      let handled = true;
      if ((ctrl && key === "y") || (key === "z" && e.shiftKey)) k.redoChange();
      else if (key === "z") k.undoChange();
      else if (k.hasActor && k.legal && key === "f") k.act("fold");
      else if (k.hasActor && k.legal && key === "c") k.act(k.legal.canCheck ? "check" : "call");
      else if (k.hasActor && k.legal && key === "a") k.act("allin");
      else if (k.hasActor && k.legal && (key === "r" || key === "b") && (k.legal.canBet || k.legal.canRaise)) {
        k.act(k.legal.canBet ? "bet" : "raise", Math.min(Math.max(k.betTo, k.legal.minRaiseTo), k.legal.maxTo));
      } else if (key === "enter") {
        if (k.phase === "setup" && k.canDeal) k.dealHand();
        else if (k.phase === "holecards") k.startBetting();
        else if (k.showDealNext && k.boardReadyFor(k.nextStreet)) k.dealNextStreet();
        else if (k.canComplete) k.completeHand();
        else handled = false;
      } else handled = false;
      if (handled) e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

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
            {anteGame ? (
              <div style={styles.row} data-tour="stakes">
                <div style={styles.col}>
                  <label style={styles.label}>Ante Amount</label>
                  <input style={styles.input} type="number" value={anteAmount} onChange={(e) => setAnteAmount(e.target.value)} disabled={locked} />
                </div>
              </div>
            ) : (
              <div style={styles.row} data-tour="stakes">
                <div style={styles.col}>
                  <label style={styles.label}>Stakes</label>
                  <input style={styles.input} value={stakes} onChange={(e) => setStakes(e.target.value)} disabled={locked} />
                </div>
                <div style={styles.col}>
                  <label style={styles.label}>BB Ante</label>
                  <input style={styles.input} type="number" value={ante} onChange={(e) => setAnte(e.target.value)} disabled={locked} />
                </div>
              </div>
            )}

            <label style={{ ...styles.checkRow, marginTop: 2 }} data-tour="straddle">
              <input type="checkbox" checked={anteGame} disabled={locked} onChange={(e) => { setAnteGame(e.target.checked); if (e.target.checked) setTripleBlind(false); }} />
              <span>Ante Game <span style={styles.sideHint}>· blinds + cascading straddles, BTN double</span></span>
            </label>
            {anteGame && (
              <div style={styles.straddlePreview}>
                {activeStraddles.length
                  ? `$${anteX}/$${anteX} blinds · ${activeStraddles.map((st) => { const nm = named.find((p) => p.seat === st.seat)?.name || `Seat ${st.seat}`; return `${nm} STR $${st.amount}`; }).join(" · ")}`
                  : "seat at least 3 players for an ante game"}
              </div>
            )}

            {!anteGame && (
              <label style={{ ...styles.checkRow, marginTop: 2 }}>
                <input type="checkbox" checked={tripleBlind} disabled={locked} onChange={(e) => setTripleBlind(e.target.checked)} />
                <span>Triple blind <span style={styles.sideHint}>· SB/BB/STR, e.g. 10/20/40</span></span>
              </label>
            )}
            {!anteGame && tripleBlind && (
              <div style={styles.straddlePreview}>Mandatory UTG straddle: ${mandatoryStraddle} (auto-posted)</div>
            )}

            <div style={styles.row} data-tour="button">
              <div style={styles.col}>
                <label style={styles.label}>Button Seat</label>
                <select style={styles.input} value={buttonSeat} onChange={(e) => setButtonSeat(Number(e.target.value))} disabled={locked}>
                  {seatOptions.map((s) => (<option key={s} value={s}>Seat {s}</option>))}
                </select>
              </div>
            </div>

            <div style={styles.row}>
              <div style={styles.col}>
                <label style={styles.label}>Video Date <span style={styles.sideHint}>· auto-filled from the YouTube link</span></label>
                <input style={styles.input} type="date" value={videoDate} onChange={(e) => setVideoDate(e.target.value)} />
              </div>
            </div>

            {!anteGame && (
            <label style={{ ...styles.checkRow, marginTop: 4 }}>
              <input type="checkbox" checked={straddleCount > 0} disabled={locked} onChange={(e) => { setStraddleCount(e.target.checked ? 1 : 0); if (e.target.checked) setCustomStraddle(null); }} />
              <span>{tripleBlind ? `Voluntary double straddle (2× mandatory = $${2 * mandatoryStraddle})` : "UTG straddle (2× BB)"}</span>
            </label>
            )}

            {!anteGame && straddleCount > 0 && (
              <div style={styles.straddleBox}>
                {!tripleBlind && (
                  <div style={styles.straddleRow}>
                    <span style={styles.label}>Straddlers</span>
                    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                      <button style={styles.stepBtn} disabled={locked || straddleCount <= 1} onClick={() => setStraddleCount((c) => Math.max(1, c - 1))}>−</button>
                      <span style={{ minWidth: 16, textAlign: "center", fontWeight: 700 }}>{straddleCount}</span>
                      <button style={styles.stepBtn} disabled={locked} onClick={() => setStraddleCount((c) => c + 1)}>+</button>
                    </div>
                  </div>
                )}
                <div style={styles.straddlePreview}>
                  {activeStraddles.length
                    ? activeStraddles.map((st, i) => {
                        const nm = named.find((p) => p.seat === st.seat)?.name || `Seat ${st.seat}`;
                        return `${i === 0 ? "UTG" : `UTG+${i}`} ${nm}: $${st.amount}`;
                      }).join("  ·  ")
                    : "not enough players to straddle"}
                </div>
              </div>
            )}
            {!anteGame && tripleBlind && straddleCount === 0 && activeStraddles.length > 0 && (
              <div style={styles.straddlePreview}>
                {activeStraddles.map((st) => {
                  const nm = named.find((p) => p.seat === st.seat)?.name || `Seat ${st.seat}`;
                  return `UTG ${nm}: $${st.amount}`;
                }).join("  ·  ")}
              </div>
            )}

            {!anteGame && (
              <div style={{ marginTop: 6 }}>
                {customStraddle ? (
                  <div style={styles.straddleBox}>
                    <div style={styles.straddleRow}>
                      <span style={{ ...styles.straddlePreview, color: "var(--violet)" }}>
                        Custom straddle: <strong>{named.find((p) => p.seat === customStraddle.seat)?.name || `Seat ${customStraddle.seat}`}</strong> posts ${customStraddle.amount}
                      </span>
                      {!locked && (
                        <div style={{ display: "flex", gap: 6 }}>
                          <button style={styles.stepBtn} title="Edit" onClick={() => setStraddleMenuOpen(true)}>✎</button>
                          <button style={styles.stepBtn} title="Clear" onClick={() => setCustomStraddle(null)}>✕</button>
                        </div>
                      )}
                    </div>
                  </div>
                ) : (
                  <button style={styles.addBtn} disabled={locked} onClick={() => setStraddleMenuOpen(true)}>
                    + Custom straddle <span style={styles.sideHint}>· any position, any amount</span>
                  </button>
                )}
              </div>
            )}

            <div style={{ ...styles.sideHead, marginTop: 16 }} data-tour="roster">ROSTER <span style={styles.sideHint}>· blank name = empty seat</span></div>
            {!locked && (
              <label style={styles.autoNumRow} title="When on, dragging renumbers seats 1,2,3… top-to-bottom. When off, dragging reorders rows but keeps your typed seat numbers.">
                <input type="checkbox" checked={autoNumber} onChange={(e) => setAutoNumber(e.target.checked)} />
                Auto-number seats
              </label>
            )}
            {roster.map((p, i) => (
              <div key={i}>
                {/* Blue insertion line shown above the row the drag will drop before. */}
                {dragIdx != null && dropIdx === i && <div style={styles.dropLine} />}
                <div
                  style={{ ...styles.rosterRow, ...(dragIdx === i ? styles.rosterRowDragging : {}) }}
                  draggable={!locked}
                  onDragStart={(e) => {
                    // The row is draggable, but a drag that starts inside the
                    // name/stack/seat inputs (or the buttons) is cancelled so text
                    // editing/selection still works — only the ≡ handle or the row
                    // chrome begins a reorder.
                    if (locked || ["INPUT", "BUTTON", "SELECT"].includes(e.target.tagName)) {
                      e.preventDefault();
                      return;
                    }
                    setDragIdx(i);
                    e.dataTransfer.effectAllowed = "move";
                    e.dataTransfer.setData("text/plain", String(i));
                  }}
                  onDragEnd={() => { setDragIdx(null); setDropIdx(null); }}
                  onDragOver={(e) => {
                    if (dragIdx == null) return;
                    e.preventDefault();
                    const rect = e.currentTarget.getBoundingClientRect();
                    setDropIdx(e.clientY - rect.top > rect.height / 2 ? i + 1 : i);
                  }}
                  onDrop={(e) => { e.preventDefault(); moveRosterRow(dragIdx, dropIdx); setDragIdx(null); setDropIdx(null); }}
                >
                  {!locked && (
                    <span style={styles.dragHandle} title="Drag to reorder">≡</span>
                  )}
                  <input
                    style={{ ...styles.seatNumInput, ...(autoNumber ? styles.seatNumAuto : {}) }}
                    type="number" min={1} max={9} value={p.seat}
                    title={autoNumber ? "Auto-numbered by position — turn off Auto-number to edit" : "Type any seat number"}
                    onChange={(e) => {
                      const v = Math.max(1, Math.min(9, Number(e.target.value) || 1));
                      updateRosterAt(i, { seat: v });
                    }}
                    disabled={locked || autoNumber} />
                  <span style={{ ...styles.diffDot, opacity: defaultDiffers.has(p.seat) ? 1 : 0 }} title="Differs from default lineup">●</span>
                  <input style={{ ...styles.input, flex: 1 }} value={p.name} placeholder="(empty)" onChange={(e) => updateRosterAt(i, { name: e.target.value })} disabled={locked} />
                  <input style={{ ...styles.input, width: 74 }} type="number" value={p.stack} onChange={(e) => updateRosterAt(i, { stack: Math.max(0, Number(e.target.value) || 0) })} disabled={locked} />
                  {!locked && p.name.trim() && <button style={styles.miniX} title="Stand up (keep seat)" onClick={() => emptySeatAt(i)}>⏏</button>}
                  {!locked && <button style={styles.miniX} title="Remove seat from table" onClick={() => removeSeatAt(i)}>✕</button>}
                </div>
              </div>
            ))}
            {/* Insertion line for dropping at the very bottom. */}
            {dragIdx != null && dropIdx === roster.length && <div style={styles.dropLine} />}
            {!locked && roster.length < 9 && (
              <button style={styles.addBtn} onClick={addSeat}>+ Add seat</button>
            )}
            {locked && <div style={styles.lockNote}>Session locked during betting — Reset Hand to edit.</div>}

            {/* Default lineup (per stream) */}
            <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
              <button
                style={{ ...styles.addBtn, flex: 1, marginTop: 0, opacity: currentStreamId ? 1 : 0.4 }}
                disabled={!currentStreamId}
                title={currentStreamId ? "Save current names + seats as this stream's default" : "Paste a YouTube link first to identify the stream"}
                onClick={setAsDefaultLineup}
                data-tour="setdefault"
              >★ Set as Default</button>
              <button
                style={{ ...styles.addBtn, flex: 1, marginTop: 0, opacity: currentDefault ? 1 : 0.4 }}
                disabled={!currentDefault || locked}
                title="Restore default player names (keeps current stacks)"
                onClick={restoreDefaultLineup}
              >↺ Restore Default</button>
            </div>
            <div style={styles.sideHint}>
              {currentStreamId
                ? (currentDefault ? `Default set for ${currentStreamId}${defaultDiffers.size ? ` · ${defaultDiffers.size} differ` : ""}` : `No default yet for ${currentStreamId}`)
                : "Default lineup is per stream — add a YouTube link to enable."}
            </div>

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
            <button style={{ ...styles.addBtn, marginTop: 6, color: "var(--loss-bright)", borderColor: "var(--loss-deep)" }} onClick={newSession}>⟲ New session (clear saved)</button>
          </div>
        )}
      </aside>

      <button style={{ ...styles.sidebarToggle, left: sidebarOpen ? 280 : 0 }} onClick={() => setSidebarOpen((o) => !o)}>
        {sidebarOpen ? "‹" : "›"}
      </button>

      {/* Main area */}
      <main style={styles.main}>
        <div style={styles.topBar}>
          <div style={styles.titleCol}>
            <div style={styles.logo}>Hand <em style={{ color: "var(--gold)" }}>Builder</em></div>
            <div style={styles.histRow}>
              <div style={styles.histItem}>
                <button style={{ ...styles.histBtn, opacity: canUndo ? 1 : 0.4 }} disabled={!canUndo} onClick={undoChange} title="Undo the last change: an action, bet, card…">↶ Undo</button>
                <span style={styles.histKey}>Ctrl + Z</span>
              </div>
              <div style={styles.histItem}>
                <button style={{ ...styles.histBtn, opacity: canRedo ? 1 : 0.4 }} disabled={!canRedo} onClick={redoChange} title="Redo what was undone">Redo ↷</button>
                <span style={styles.histKey}>Ctrl + Y</span>
              </div>
            </div>
          </div>
          <div style={styles.ytField}>
            <div
              style={{ ...styles.ytWrap, ...(ytDuplicate ? styles.ytWrapDup : {}) }}
              className={!youtubeLink.trim() ? "yt-empty-flash" : undefined}
              data-tour="youtube"
            >
              <span style={styles.ytIcon}>▶</span>
              <input
                style={styles.ytInput}
                value={youtubeLink}
                onChange={(e) => setYoutubeLink(e.target.value)}
                // Paste the link, press Enter: that's the deal.
                onKeyDown={(e) => { if (e.key === "Enter" && phase === "setup" && canDeal && !sideGameMode) dealHand(); }}
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
            {!youtubeLink.trim() ? (
              <div style={styles.ytHelp}>Paste a new timestamped YouTube link to continue</div>
            ) : ytDuplicate ? (
              <div style={styles.ytWarn}>⚠ Same timestamp as the last hand — each hand needs its own.</div>
            ) : null}
          </div>
          <div style={styles.topMeta}>
            <div style={styles.statTracker} data-tour="earnings">
              <div>
                Hands: <strong style={{ color: "var(--text-strong)" }}>{me.dashboard.hands}</strong>
                {"  ·  "}Pieces: <strong style={{ color: "var(--text-strong)" }}>{me.dashboard.pieces}</strong>
              </div>
              <div style={styles.statPreview}>
                Base: <strong style={{ color: "var(--text)" }}>${me.dashboard.base.toFixed(2)}</strong>
                {" + "}Stream Bonuses: <strong style={{ color: "var(--gold-bright)" }}>${me.dashboard.stream_bonus.toFixed(2)}</strong>
                {" = "}Total: <strong style={{ color: "var(--gain-bright)", fontSize: 14 }}>${me.dashboard.total.toFixed(2)}</strong>
              </div>
              {eng && handPieces.total > 0 && (
                <div style={styles.statPreview}>
                  This hand so far: ~${(handPieces.total * PIECE_RATE).toFixed(2)} ({handPieces.cards} cards · {handPieces.actions} actions)
                </div>
              )}
              {(sideGameMode || sideGameHands.length > 0) && (
                <div style={{ ...styles.statPreview, color: "var(--violet)" }}>
                  Side game: <strong style={{ color: "var(--violet)" }}>{sideGameHands.length} hands</strong> · <strong style={{ color: "var(--violet)" }}>${sideGameEarnings.toFixed(2)}</strong>
                </div>
              )}
            </div>
            <span>{anteGame ? `Ante Game $${anteX}/$${2 * anteX}` : `${stakes}${Number(ante) > 0 ? ` · $${ante} ante` : ""}`}</span>
            {/* Side games. While the hand counter for an untranscribed game runs it
                shows here; otherwise Side Game opens the popup, and each game in
                play gets a chip that reopens it (holders, streaks, structure). */}
            {sideGameMode ? (
              <>
                <input
                  style={{ ...styles.input, width: 130 }}
                  value={sideGameOther}
                  onChange={(e) => setSideGameOther(e.target.value)}
                  placeholder="Game name…"
                />
                <button
                  style={{ ...styles.topBtn, border: "1px solid var(--violet)", color: "var(--violet)" }}
                  title="End the side game (adjust stacks, then resume)"
                  onClick={requestEndSideGame}
                >
                  Counting hands · End
                </button>
              </>
            ) : (
              <>
                {squidActive && (
                  <button style={styles.sideChip} title="Squid buttons, who holds them, and the structure" onClick={() => setSideDialog(squidGame)}>
                    {SIDE_GAMES[squidGame].short} · {squidLeft}/{squidConfig.total} left
                  </button>
                )}
                {sevenTwoActive && (
                  <button style={styles.sideChip} title="7-2 game: what each player pays" onClick={() => setSideDialog("sevenTwo")}>
                    7-2 · ${sevenTwoValue.toLocaleString()} each
                  </button>
                )}
                {bountyActive && (
                  <button style={styles.sideChip} title="Bounty streaks and the structure" onClick={() => setSideDialog("bounty")}>
                    Bounty · from {bountySchedule.start} in a row
                  </button>
                )}
                <button style={styles.topBtn} title="Squid Game, Progressive Squid Game or Bounty Game" onClick={() => setSideDialog("menu")}>
                  Side Game
                </button>
              </>
            )}
            {!sideGameMode && phase !== "setup" && (
              <>
                <button style={styles.topBtn} title="Restart this hand (same players & stacks)" onClick={resetHand}>↺ Reset Hand</button>
                <button style={styles.topBtn} title="Next hand (carry over stacks, rotate button)" onClick={() => nextHand()}>Next Hand ↻</button>
              </>
            )}
            {streamHandCount > 0 && (
              <button style={styles.sessionBtn} title="Open the Hand Manager" onClick={() => setHandPanelOpen((o) => !o)}>
                Hand Manager ({streamHandCount})
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
              <div style={styles.feltBrand}>♠<div style={styles.feltBrandText}>Poker Suite</div></div>

              {/* Pot */}
              <div style={styles.potArea}>
                <div style={styles.potLabel}>POT</div>
                <div style={styles.potValue}>{fmtChips(potTotal(eng))}</div>
              </div>

              {/* Community cards (one row per run when running it 2–4 times) */}
              <div style={styles.boardArea} data-tour="board">
                {numRuns >= 2 && <div style={styles.runLabel}>RUN 1</div>}
                <div style={styles.boardRow}>
                  {[0, 1, 2, 3, 4].map((i) => (
                    <Card key={i} card={board[i]} size="lg" faceDownIfEmpty={false} onClick={() => openBoard(i)} />
                  ))}
                </div>
                {numRuns >= 2 && extraBoards.map((rb, ri) => (
                  <div key={ri}>
                    <div style={styles.runLabel}>RUN {ri + 2}</div>
                    <div style={styles.boardRow}>
                      {[0, 1, 2, 3, 4].map((i) => (
                        <Card key={i} card={rb[i]} size="lg" faceDownIfEmpty={false} onClick={() => openRunBoard(ri, i)} />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Dealer cutout: concave notch + chip tray */}
            <div style={styles.dealerCut} />
            <div style={styles.chipTray}>
              {["#c8a85a", "#f1e7cf", "#0d0c0a", "#8b2f2f", "#dec078"].map((c, i) => (
                <div key={i} style={{ ...styles.chipWell }}>
                  <div style={{ ...styles.chipStack, background: c, borderColor: c === "#0d0c0a" ? "rgba(200,168,90,.6)" : "rgba(13,12,10,.45)" }} />
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
                dataTour={i === 0 ? "seat" : undefined}
                pos={ellipsePos(i, tableSeats.length)}
                badge={buyButton ? (p.seat === buyButton.seat ? "BTN" : "") : (anteGame && p.seat === Number(buttonSeat)) ? "BTN" : activeStraddles.some((st) => st.seat === p.seat) ? "STR" : positions[p.seat]}
                isButton={Number(buttonSeat) === p.seat}
                isActor={hasActor && eng.actorSeat === p.seat}
                folded={enginePlayer?.folded}
                committed={enginePlayer?.committedStreet || 0}
                cards={holeCards[p.seat] || ["", ""]}
                desc={autoEval?.descriptions?.[p.name]}
                isWinner={autoEval && autoEval.winners.includes(p.name)}
                bought={buyButton && buyButton.seat === p.seat ? buyButton.type : null}
                bounty={bountyActive && !p.empty && p.name.trim() ? bountyStreaks[p.name] || 0 : null}
                onBounty={(d) => setBountyStreak(p.name, d)}
                squid={squidActive && !p.empty && p.name.trim() ? squidButtons[p.name] || 0 : null}
                onSquid={(d) => setSquidButton(p.name, d)}
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
                  updateRoster(p.seat, { stack: Math.max(0, Number(stackDraft) || 0) });
                  setEditingStack(null);
                }}
              />
            );
          })}
        </div>

        {/* Action bar */}
        <div style={styles.actionBar} data-tour="actionbar">
          {error && <div key={error} className="ui-fade" style={styles.errorInline}>⚠ {error}</div>}

          {/* Side game mode: count hands fast. No cards / actions / board. */}
          {sideGameMode && !endingSideGame && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>
                <strong style={{ color: "var(--violet)" }}>{sideGameLabel}</strong> — paste the first hand's timestamped link above, then tap through hands. Positions rotate automatically; no cards or actions needed.
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
                  <label key={p.seat} style={{ display: "inline-flex", alignItems: "center", gap: 5, color: "var(--text-2)", fontSize: 12 }}>
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
              <div style={{ ...styles.barHint, color: canDeal ? "var(--text-dim)" : "var(--gold-bright)" }}>
                {ytDuplicate
                  ? "⚠ This is the same timestamp as the last hand. Each hand needs its own timestamp."
                  : !ytReady
                  ? "⚠ Paste a new timestamped YouTube link to deal"
                  : `${named.length} players seated · button on Seat ${buttonSeat}`}
              </div>
              <button
                className="ui-sheen" style={{ ...styles.dealBtn, opacity: canDeal ? 1 : 0.4, cursor: canDeal ? "pointer" : "not-allowed", boxShadow: canDeal ? styles.dealBtn.boxShadow : "none" }}
                disabled={!canDeal}
                onClick={dealHand}
                data-tour="deal"
                title={canDeal ? undefined : ytDuplicate ? "Paste a link with a new timestamp" : "Paste a timestamped YouTube link first"}
              >♠ DEAL HAND<span className="ui-kbd">↵</span></button>
            </div>
          )}

          {phase === "holecards" && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>Set hole cards (optional) and adjust the button / stacks if needed, then start.</div>
              <button className="ui-sheen" style={styles.dealBtn} onClick={startBetting}>START BETTING ▶<span className="ui-kbd">↵</span></button>
            </div>
          )}

          {inBetting && hasActor && legal && (
            <div style={styles.actionRow}>
              <div style={styles.actorTag}>
                <div style={styles.actorName}>{actorName} to act</div>
                <div style={styles.actorSub}>{STREET_LABEL[eng.street]} · facing {fmtChips(legal.facing)} · stack {fmtChips(legal.player.stack)}</div>
              </div>
              <button style={{ ...styles.actBtn, ...styles.foldBtn }} onClick={() => act("fold")}>FOLD<span className="ui-kbd">F</span></button>
              {legal.canCheck ? (
                <button style={{ ...styles.actBtn, ...styles.checkBtn }} onClick={() => act("check")}>CHECK<span className="ui-kbd">C</span></button>
              ) : (
                <button style={{ ...styles.actBtn, ...styles.checkBtn }} onClick={() => act("call")}>
                  CALL {fmtChips(legal.callAmount)}{legal.callIsAllin ? " (ALL-IN)" : ""}<span className="ui-kbd">C</span>
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
                        <button style={styles.sizeBtn} onClick={() => setBetTo(Math.min(legal.maxTo, legal.facing + potTotal(eng) + legal.toCall))}>pot</button>
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
                    className="ui-sheen"
                    style={{ ...styles.actBtn, ...styles.raiseBtn }}
                    onClick={() => {
                      const to = Math.min(Math.max(betTo, legal.minRaiseTo), legal.maxTo);
                      act(legal.canBet ? "bet" : "raise", to);
                    }}
                  >
                    {legal.canBet ? "BET" : "RAISE TO"} {fmtChips(Math.min(Math.max(betTo, legal.minRaiseTo), legal.maxTo))}<span className="ui-kbd">R</span>
                  </button>
                </div>
              )}
              {!legal.callIsAllin && (
                <button style={{ ...styles.actBtn, ...styles.allinBtn }} onClick={() => act("allin")}>ALL-IN<span className="ui-kbd">A</span></button>
              )}
            </div>
          )}

          {showRunPrompt && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>All players are effectively all-in. How many times are they running it?</div>
              <div style={{ display: "flex", gap: 8 }}>
                <button style={styles.dealBtn} onClick={() => chooseRuns(1)}>1 (normal)</button>
                <button style={styles.runBtn} onClick={() => chooseRuns(2)}>2 (twice)</button>
                <button style={styles.runBtn} onClick={() => chooseRuns(3)}>3 (three times)</button>
                <button style={styles.runBtn} onClick={() => chooseRuns(4)}>4 (four times)</button>
              </div>
            </div>
          )}

          {showDealNext && (
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
                DEAL {STREET_LABEL[nextStreet].toUpperCase()} ▶<span className="ui-kbd">↵</span>
              </button>
            </div>
          )}

          {showMultiRun && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>
                Running it <strong>{numRuns}×</strong> — set each run's remaining cards on the table, then pick a winner per run.
              </div>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", justifyContent: "center" }}>
                {Array.from({ length: numRuns }).map((_, i) => (
                  <div key={i} style={styles.runWinnerBox}>
                    <span style={styles.runLabelSm}>RUN {i + 1}</span>
                    <select style={{ ...styles.input, width: 130 }} value={runWinners[i] || ""} onChange={(e) => setRunWinner(i, e.target.value)}>
                      <option value="">winner…</option>
                      {surv.map((nm) => (<option key={nm} value={nm}>{nm}</option>))}
                    </select>
                  </div>
                ))}
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button style={styles.completeBtn} onClick={completeHand}>✓ COMPLETE HAND ({numRuns} runs)</button>
                <button style={styles.topBtn} onClick={() => { setRunsChosen(false); setNumRuns(1); setExtraBoards([]); setRunWinners([]); }}>↺ change runs</button>
              </div>
            </div>
          )}

          {canComplete && (
            <div style={styles.barCenter}>
              <div style={styles.barHint}>
                {eng.handOver ? `${surv[0]} wins uncontested.` : `Showdown — ${surv.join(", ")}`}
              </div>
              <button style={styles.completeBtn} onClick={completeHand} data-tour="completehand">✓ COMPLETE HAND<span className="ui-kbd">↵</span></button>
            </div>
          )}
        </div>
      </main>

      {/* Hand Manager — right-side collapsible panel */}
      <aside style={{ ...styles.handPanel, width: handPanelOpen ? 348 : 0, padding: handPanelOpen ? "18px 14px" : 0 }}>
        {handPanelOpen && (
          <HandManager
            localHands={sessionHands}
            serverHands={streamHands}
            stream={streamMeta}
            lastTimestamp={streamLastTs}
            streamId={currentStreamId}
            streamUrl={streamUrl}
            onResumeFrom={(u) => setYoutubeLink(u)}
            onRestore={restoreAfterHand}
            onDeleteLocal={deleteLocalHand}
            onDeleteServer={deleteServerHand}
            onExportHands={downloadHands}
            onFinishStream={finishStream}
            streamComplete={streamComplete}
            isAdmin={!!(me.user && me.user.is_admin)}
          />
        )}
      </aside>
      <button
        style={{ ...styles.handToggle, right: handPanelOpen ? 348 : 0 }}
        title={handPanelOpen ? "Hide Hand Manager" : "Show Hand Manager"}
        onClick={() => setHandPanelOpen((o) => !o)}
        data-tour="handmanager"
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

      {/* Side Game popup: pick a game, set its structure, see who holds what */}
      {sideDialog && (
        <SideGameDialog
          ui={SIDE_DIALOG_UI}
          view={sideDialog}
          setView={setSideDialog}
          onClose={() => setSideDialog(null)}
          seatedNames={named.map((p) => p.name)}
          bountyActive={bountyActive}
          bountySchedule={bountySchedule}
          bountyStreaks={bountyStreaks}
          squidActive={squidActive}
          squidConfig={squidConfig}
          squidRound={squidRound}
          squidLeft={squidLeft}
          squidButtons={squidButtons}
          sevenTwoActive={sevenTwoActive}
          sevenTwoValue={sevenTwoValue}
          lastSideGame={lastSideGame}
          onStart={startSideGameType}
          onSave={saveSideGameType}
          onEnd={endSideGameType}
          onSquidButton={setSquidButton}
          onNewSquidRound={newSquidRound}
          onCountOnly={() => { setSideDialog(null); startSideGame(); }}
        />
      )}

      {/* A side-game payout waits for a yes */}
      {sidePayConfirm && (
        <SidePayConfirm
          ui={SIDE_DIALOG_UI}
          confirm={sidePayConfirm}
          onYes={() => { setSidePayConfirm(null); completeHand({ sideConfirmed: true }); }}
          onNo={() => setSidePayConfirm(null)}
        />
      )}

      {/* 7-2 detection: offer to enable the 7-2 game */}
      {sevenTwoDetect && (
        <div className="ui-fade" style={styles.pickerOverlay} onClick={() => setSevenTwoDetect(null)}>
          <div className="ui-pop" style={styles.confirmBox} onClick={(e) => e.stopPropagation()}>
            <div style={styles.confirmTitle}>7-2 detected</div>
            <div style={styles.confirmBody}>
              <strong>{sevenTwoDetect.name || `Seat ${sevenTwoDetect.seat}`}</strong> was dealt{" "}
              <span style={styles.confirmCards}>{(sevenTwoDetect.cards || []).filter(Boolean).join(" ")}</span>.
              <br />Is the 7-2 game on?
            </div>
            <div style={styles.confirmBtnRow}>
              <button style={styles.confirmYes} onClick={() => { setSevenTwoDetect(null); setSideDialog("sevenTwo"); }}>Yes, set it up</button>
              <button style={styles.confirmNo} onClick={() => setSevenTwoDetect(null)}>No</button>
            </div>
          </div>
        </div>
      )}

      {/* Custom straddle popup */}
      {straddleMenuOpen && (
        <StraddleMenu
          // The blinds already have a forced bet in; a blind "straddling" posts
          // twice, which the formatter rejects. Offer every other seat.
          seats={named.filter((p) => !["SB", "BB", "BTN/SB"].includes(positions[p.seat]))}
          positions={positions}
          defaultSeat={customStraddle ? customStraddle.seat : undefined}
          defaultAmount={customStraddle ? customStraddle.amount : 2 * bb}
          onConfirm={(seat, amount) => { setCustomStraddle({ seat, amount }); setStraddleCount(0); setBuyButton(null); setStraddleMenuOpen(false); }}
          onClose={() => setStraddleMenuOpen(false)}
        />
      )}

      {/* Buy-the-button menu */}
      {buyMenuSeat != null && (() => {
        const p = named.find((x) => x.seat === buyMenuSeat);
        const isBuyer = buyButton && buyButton.seat === buyMenuSeat;
        return (
          <div className="ui-fade" style={styles.pickerOverlay} onClick={() => setBuyMenuSeat(null)}>
            <div className="ui-pop" style={styles.buyBox} onClick={(e) => e.stopPropagation()}>
              <div style={styles.pickerTitle}>{p ? p.name : `Seat ${buyMenuSeat}`} — buy the button</div>
              <div style={styles.buyNote}>
                Posts a single live blind + the other blinds/ante as dead money; action starts to their left.
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button style={{ ...styles.buyBtn, background: buyButton?.type === "btn" && isBuyer ? "var(--gain-deep)" : "var(--steel-deep)" }} onClick={() => buyTheButton(buyMenuSeat, "btn")}>
                  Buy the BTN<div style={styles.buySub}>BB live · SB+ante dead</div>
                </button>
                <button style={{ ...styles.buyBtn, background: buyButton?.type === "str" && isBuyer ? "var(--gain-deep)" : "var(--violet-deep)" }} onClick={() => buyTheButton(buyMenuSeat, "str")}>
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
      {flash && <div key={flash} className="ui-toast" style={styles.flashToast}><span style={{ color: "var(--gold)" }}>✓</span> {flash}</div>}
    </div>
  );
}

// ── Styles ──────────────────────────────────────────────────────────────────
// Sophia's look: warm black, gold, hairlines, DM Sans + Cormorant Garamond.
// Every colour is a token from theme.css. Hover, focus, press and entrance
// motion live in theme.css (classes .ui-*), since inline styles can't express them.
const FELT = "radial-gradient(ellipse at 50% 40%, #221f19 0%, #171510 42%, #0e0d0a 78%, #0a0908 100%)";
const GOLD_GLOW = "0 0 0 1px var(--gold), 0 0 24px rgba(200,168,90,.38)";
const GAIN_GLOW = "0 0 0 1px var(--gain), 0 0 22px rgba(126,184,138,.34)";
const PANEL_BG = "linear-gradient(180deg, #1a1814 0%, #12110e 100%)";
const ghostBtn = { background: "transparent", border: "1px solid var(--line-strong)", borderRadius: 3, color: "var(--text-2)", fontWeight: 500 };
const goldBtn = { background: "var(--gold-grad)", border: "none", borderRadius: 3, color: "var(--ink)", fontWeight: 700 };
const SIDE_DIALOG_UI = {
  overlay: { position: "fixed", inset: 0, background: "var(--overlay)", backdropFilter: "blur(3px)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 60, padding: 16 },
  box: { width: 470, maxWidth: "94vw", maxHeight: "88vh", overflowY: "auto", boxSizing: "border-box", background: PANEL_BG, border: "1px solid var(--line-gold)", borderRadius: 3, padding: 24, boxShadow: "var(--shadow-lg)", display: "flex", flexDirection: "column", gap: 12, color: "var(--text)", fontFamily: "var(--font-ui)" },
  title: { display: "flex", alignItems: "center", gap: 10, fontSize: 26, fontWeight: 500, color: "var(--gold-bright)", fontFamily: "var(--font-display)" },
  sub: { fontSize: 12.5, color: "var(--text-dim)", lineHeight: 1.55 },
  head: { fontSize: 9.5, fontWeight: 600, letterSpacing: 2.2, color: "var(--gold)", marginTop: 6 },
  card: { display: "flex", flexDirection: "column", gap: 4, textAlign: "left", padding: "12px 14px", background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 2, color: "var(--text)", cursor: "pointer", fontFamily: "inherit" },
  cardOn: { border: "1px solid var(--gold)", boxShadow: "0 0 0 1px rgba(200,168,90,.25)" },
  cardTitle: { display: "flex", alignItems: "center", gap: 8, fontSize: 19, fontWeight: 500, fontFamily: "var(--font-display)", color: "var(--text-strong)" },
  cardText: { fontSize: 12, color: "var(--text-dim)", lineHeight: 1.5 },
  badge: { fontSize: 8.5, fontWeight: 700, letterSpacing: 1.2, color: "var(--ink)", background: "var(--gold)", borderRadius: 999, padding: "2px 7px", fontFamily: "var(--font-ui)" },
  field: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" },
  label: { fontSize: 12.5, color: "var(--text-2)" },
  input: { width: 80, padding: "7px 9px", background: "var(--field)", border: "1px solid var(--line)", borderRadius: 3, color: "var(--text)", fontSize: 13, outline: "none", fontFamily: "inherit" },
  preview: { fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.6, background: "var(--field)", border: "1px solid var(--line)", borderRadius: 2, padding: "10px 12px" },
  hint: { fontSize: 11.5, color: "var(--text-mute)", lineHeight: 1.5 },
  warn: { fontSize: 11.5, color: "var(--gold-bright)", lineHeight: 1.5 },
  row: { display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" },
  value: { fontSize: 12.5, color: "var(--text-2)" },
  accent: { color: "var(--gold-bright)", fontWeight: 600 },
  gain: { color: "var(--gain-bright)", fontWeight: 600 },
  loss: { color: "var(--loss-bright)", fontWeight: 600 },
  stepBtn: { width: 22, height: 22, padding: 0, ...ghostBtn, fontSize: 13, lineHeight: 1 },
  away: { fontSize: 8.5, fontWeight: 700, letterSpacing: 1, color: "var(--ink)", background: "var(--text-dim)", borderRadius: 999, padding: "1px 6px" },
  actions: { display: "flex", gap: 8, flexWrap: "wrap", marginTop: 4 },
  primary: { flex: 1, minWidth: 150, padding: "10px 14px", ...goldBtn, fontSize: 12.5 },
  btn: { padding: "10px 14px", ...ghostBtn, fontSize: 12.5 },
  danger: { padding: "10px 14px", ...ghostBtn, border: "1px solid rgba(198,120,120,.4)", color: "var(--loss-bright)", fontSize: 12.5 },
  link: { background: "none", border: "none", padding: 0, color: "var(--text-mute)", fontSize: 12, textDecoration: "underline", cursor: "pointer", fontFamily: "inherit" },
  footer: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginTop: 4 },
};
const styles = {
  // ── shell ──
  app: { display: "flex", flex: 1, minHeight: 0, background: "var(--bg)", color: "var(--text)", fontFamily: "var(--font-ui)" },
  sidebar: { background: "var(--surface)", borderRight: "1px solid var(--line)", overflowX: "hidden", overflowY: "auto", transition: "width .22s var(--ease), padding .22s var(--ease)", flexShrink: 0, boxSizing: "border-box" },
  sidebarInner: { display: "flex", flexDirection: "column", gap: 11, minWidth: 256 },
  sidebarToggle: { position: "fixed", top: 66, zIndex: 30, width: 18, height: 46, ...ghostBtn, background: "var(--surface-3)", color: "var(--gold)", borderRadius: "0 3px 3px 0", borderLeft: "none", transition: "left .22s var(--ease)", fontSize: 13, padding: 0 },
  sideHead: { fontSize: 9.5, fontWeight: 600, letterSpacing: 2.4, textTransform: "uppercase", color: "var(--gold)", paddingBottom: 7, borderBottom: "1px solid transparent", borderImage: "linear-gradient(90deg, rgba(200,168,90,.4), transparent 85%) 1" },
  sideHint: { fontSize: 10, fontWeight: 400, letterSpacing: 0.2, color: "var(--text-mute)", textTransform: "none" },
  row: { display: "flex", gap: 8 },
  col: { flex: 1, display: "flex", flexDirection: "column", gap: 5 },
  label: { fontSize: 9.5, fontWeight: 600, textTransform: "uppercase", letterSpacing: 1.3, color: "var(--text-mute)" },
  input: { padding: "8px 10px", background: "var(--field)", border: "1px solid var(--line)", borderRadius: 3, color: "var(--text)", fontSize: 12.5, fontFamily: "inherit", outline: "none", boxSizing: "border-box", width: "100%" },
  checkRow: { display: "flex", alignItems: "center", gap: 9, fontSize: 12.5, color: "var(--text-2)", cursor: "pointer" },
  rosterRow: { display: "flex", gap: 6, alignItems: "center", padding: "2px 0", background: "var(--surface)" },
  rosterRowDragging: { opacity: 0.5, boxShadow: "0 8px 20px rgba(0,0,0,.5)", borderRadius: 3 },
  diffDot: { color: "var(--gold-bright)", fontSize: 8, width: 8, transition: "opacity .15s" },
  dragHandle: { cursor: "grab", color: "var(--text-faint)", fontSize: 14, lineHeight: 1, padding: "0 2px", userSelect: "none", touchAction: "none" },
  seatNumInput: { width: 32, textAlign: "center", padding: "8px 2px", background: "var(--field)", border: "1px solid var(--line)", borderRadius: 3, color: "var(--text)", fontSize: 12, fontWeight: 600, outline: "none", boxSizing: "border-box" },
  seatNumAuto: { color: "var(--text-mute)", background: "transparent", border: "1px solid var(--line)" },
  dropLine: { height: 2, background: "var(--gold)", borderRadius: 2, margin: "2px 0", boxShadow: "0 0 8px rgba(200,168,90,.7)" },
  autoNumRow: { display: "flex", alignItems: "center", gap: 7, fontSize: 11.5, color: "var(--text-dim)", margin: "0 0 4px", cursor: "pointer", userSelect: "none" },
  miniX: { width: 24, height: 24, ...ghostBtn, border: "1px solid var(--line)", color: "var(--text-mute)", fontSize: 11, padding: 0 },
  addBtn: { marginTop: 4, padding: "8px 10px", ...ghostBtn, fontSize: 12 },
  lockNote: { marginTop: 6, fontSize: 11, color: "var(--text-mute)", fontStyle: "italic", fontFamily: "var(--font-display)" },
  configBox: { width: "100%", height: 62, boxSizing: "border-box", padding: "8px 10px", background: "var(--field)", border: "1px solid var(--line)", borderRadius: 3, color: "var(--text-dim)", fontSize: 10.5, fontFamily: "var(--font-mono)", outline: "none", resize: "vertical" },
  autosaveNote: { fontSize: 10.5, color: "var(--gain)", marginBottom: 4 },
  straddleBox: { background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 2, padding: "9px 11px", display: "flex", flexDirection: "column", gap: 7 },
  straddleRow: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  stepBtn: { width: 24, height: 24, ...ghostBtn, fontSize: 14, lineHeight: 1, padding: 0 },
  straddlePreview: { fontSize: 11, color: "var(--gold-bright)", lineHeight: 1.45 },

  // ── top bar ──
  main: { flex: 1, display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0 },
  topBar: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 14, padding: "11px 22px 11px 32px", borderBottom: "1px solid var(--line)", background: "linear-gradient(180deg, rgba(200,168,90,.035), transparent)", flexShrink: 0, flexWrap: "wrap" },
  titleCol: { display: "flex", flexDirection: "column", gap: 7, flexShrink: 0 },
  histRow: { display: "flex", gap: 8 },
  histItem: { display: "flex", flexDirection: "column", alignItems: "center", gap: 3 },
  histBtn: { padding: "4px 11px", ...ghostBtn, fontSize: 11.5, whiteSpace: "nowrap", fontFamily: "inherit" },
  histKey: { fontSize: 9, letterSpacing: 0.6, color: "var(--text-mute)", whiteSpace: "nowrap" },
  logo: { display: "flex", alignItems: "center", gap: 10, fontFamily: "var(--font-display)", fontSize: 21, fontWeight: 500, letterSpacing: 0.4, color: "var(--text-strong)", whiteSpace: "nowrap" },
  chip: { display: "inline-flex", width: 26, height: 26, borderRadius: "50%", background: "var(--gold-grad)", color: "var(--ink)", alignItems: "center", justifyContent: "center", fontSize: 14, boxShadow: "0 0 0 1px rgba(200,168,90,.35), 0 0 16px rgba(200,168,90,.2)" },
  ytField: { display: "flex", flexDirection: "column", gap: 4, flex: 1, minWidth: 260, maxWidth: 480 },
  ytWrap: { display: "flex", alignItems: "center", gap: 9, width: "100%", background: "var(--field)", border: "1px solid var(--line-strong)", borderRadius: 3, padding: "6px 11px", boxSizing: "border-box" },
  ytWrapDup: { border: "1px solid var(--loss)", background: "var(--loss-wash)" },
  ytHelp: { fontSize: 10.5, color: "var(--gold-bright)", paddingLeft: 2, fontWeight: 500 },
  ytWarn: { fontSize: 10.5, color: "var(--loss-bright)", paddingLeft: 2, fontWeight: 600 },
  ytIcon: { fontSize: 13, color: "var(--gold)", opacity: 0.9 },
  ytInput: { flex: 1, background: "transparent", border: "none", outline: "none", boxShadow: "none", color: "var(--text)", fontSize: 12.5, fontFamily: "inherit", minWidth: 0 },
  ytParsed: { fontSize: 10.5, color: "var(--gold-bright)", whiteSpace: "nowrap", fontWeight: 500, textDecoration: "none", fontFamily: "var(--font-mono)" },
  topMeta: { display: "flex", gap: 10, alignItems: "center", fontSize: 12.5, color: "var(--text-dim)", flexWrap: "wrap", justifyContent: "flex-end" },
  sideChip: { padding: "6px 12px", background: "var(--gold-wash)", border: "1px solid var(--line-gold)", borderRadius: 999, color: "var(--gold-bright)", fontSize: 11.5, fontWeight: 600, whiteSpace: "nowrap", fontFamily: "inherit", cursor: "pointer" },
  topBtn: { padding: "7px 12px", ...ghostBtn, fontSize: 12, whiteSpace: "nowrap" },
  sessionBtn: { padding: "7px 12px", background: "var(--gold-wash)", border: "1px solid var(--gold-dim)", borderRadius: 3, color: "var(--gold-bright)", fontSize: 12, fontWeight: 600, whiteSpace: "nowrap" },
  statTracker: { background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 2, padding: "6px 12px", fontSize: 12, color: "var(--text-dim)", whiteSpace: "nowrap", lineHeight: 1.5 },
  statPreview: { fontSize: 11, color: "var(--gold-bright)" },
  handTag: { border: "1px solid var(--gold-dim)", color: "var(--gold-bright)", padding: "3px 12px", borderRadius: 999, fontFamily: "var(--font-display)", fontSize: 15, fontWeight: 600, whiteSpace: "nowrap" },
  flashToast: { position: "fixed", top: 68, left: "50%", transform: "translateX(-50%)", zIndex: 80, background: PANEL_BG, color: "var(--text-strong)", fontWeight: 500, fontSize: 13, padding: "11px 20px", borderRadius: 3, border: "1px solid var(--gold-dim)", boxShadow: "0 16px 44px rgba(0,0,0,.6), 0 0 26px rgba(200,168,90,.1)", maxWidth: "min(760px, 92vw)", textAlign: "center" },

  // ── hand manager ──
  handPanel: { background: "var(--surface)", borderLeft: "1px solid var(--line)", overflowX: "hidden", overflowY: "auto", transition: "width .22s var(--ease), padding .22s var(--ease)", flexShrink: 0, boxSizing: "border-box" },
  handToggle: { position: "fixed", top: 66, zIndex: 30, width: 18, height: 46, ...ghostBtn, background: "var(--surface-3)", color: "var(--gold)", borderRadius: "3px 0 0 3px", borderRight: "none", transition: "right .22s var(--ease)", fontSize: 13, padding: 0 },
  hmInner: { display: "flex", flexDirection: "column", gap: 10, minHeight: "100%", minWidth: 322 },
  hmHeadRow: { display: "flex", justifyContent: "space-between", alignItems: "baseline" },
  hmCount: { fontSize: 11, color: "var(--text-mute)" },
  hmEmpty: { fontSize: 13, color: "var(--text-mute)", lineHeight: 1.55, marginTop: 6, fontFamily: "var(--font-display)", fontStyle: "italic" },
  hmToolbar: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  hmSelAll: { display: "flex", alignItems: "center", gap: 7, fontSize: 12, color: "var(--text-2)", cursor: "pointer" },
  hmSelCount: { fontSize: 11, color: "var(--gold-bright)", fontWeight: 600 },
  hmBtnRow: { display: "flex", flexWrap: "wrap", gap: 6 },
  hmBtn: { flex: "1 1 46%", padding: "7px 8px", ...ghostBtn, fontSize: 11.5, whiteSpace: "nowrap" },
  hmBtnGreen: { flex: "1 1 46%", padding: "7px 8px", ...goldBtn, fontSize: 11.5, whiteSpace: "nowrap" },
  hmBtnRed: { flex: "1 1 100%", padding: "7px 8px", background: "transparent", border: "1px solid rgba(198,120,120,.4)", borderRadius: 3, color: "var(--loss-bright)", fontSize: 11.5, fontWeight: 500, whiteSpace: "nowrap" },
  hmList: { flex: 1, display: "flex", flexDirection: "column", gap: 4 },
  hmGroupHead: { position: "sticky", top: -18, background: "var(--surface)", fontSize: 14, fontFamily: "var(--font-display)", fontWeight: 600, color: "var(--gold-bright)", padding: "8px 0 5px", zIndex: 1 },
  hmCard: { position: "relative", background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 2, padding: "9px 11px 8px", marginBottom: 6, cursor: "pointer", userSelect: "none" },
  hmCardSel: { border: "1px solid var(--gold)", boxShadow: "0 0 0 1px var(--gold), 0 0 16px rgba(200,168,90,.22)", background: "var(--surface-3)" },
  hmCardX: { position: "absolute", top: 6, right: 6, width: 18, height: 18, lineHeight: "16px", textAlign: "center", padding: 0, ...ghostBtn, border: "1px solid var(--line)", color: "var(--text-mute)", fontSize: 10 },
  hmCardTop: { display: "flex", alignItems: "center", gap: 8, marginBottom: 4, paddingRight: 20 },
  hmTime: { fontSize: 12, fontWeight: 600, color: "var(--gold-pale)", fontFamily: "var(--font-mono)" },
  hmSummary: { fontSize: 13, fontWeight: 500, color: "var(--text-strong)", lineHeight: 1.35, marginBottom: 4 },
  hmCardMeta: { display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--text-dim)" },
  hmLink: { display: "inline-block", fontSize: 11, color: "var(--gold-bright)", fontWeight: 500, textDecoration: "none" },
  hmDrop: { flexShrink: 0, marginTop: 2, padding: "14px 10px", border: "1px dashed var(--line-strong)", borderRadius: 3, textAlign: "center", fontSize: 12, color: "var(--text-mute)", background: "rgba(13,12,10,.4)", transition: "all .15s" },
  hmDropActive: { border: "1px dashed var(--gold)", color: "var(--gold-bright)", background: "var(--gold-wash)" },
  hmUnsaved: { fontSize: 8.5, fontWeight: 700, letterSpacing: 0.8, color: "var(--ink)", background: "var(--gold-bright)", borderRadius: 999, padding: "1px 6px" },
  hmYtIcon: { fontSize: 12, fontWeight: 700, color: "var(--gold)", textDecoration: "none" },
  hmTimelineWrap: { background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 2, padding: "10px 11px", marginBottom: 6, flexShrink: 0 },
  hmTimelineHead: { display: "flex", justifyContent: "space-between", fontSize: 9.5, fontWeight: 600, letterSpacing: 1.6, color: "var(--text-mute)", marginBottom: 7 },
  hmTrack: { position: "relative", height: 22, background: "var(--field)", border: "1px solid var(--line)", borderRadius: 2, overflow: "hidden" },
  hmProgress: { position: "absolute", left: 0, top: 0, bottom: 0, background: "linear-gradient(90deg, rgba(200,168,90,.05), rgba(200,168,90,.16))", borderRight: "1px solid var(--gold-dim)" },
  hmMarker: { position: "absolute", top: 4, width: 3, height: 14, borderRadius: 2, transform: "translateX(-50%)", cursor: "pointer", boxShadow: "0 0 4px rgba(0,0,0,.6)" },
  hmTimelineFoot: { display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 11, color: "var(--text-dim)", marginTop: 8, gap: 8 },
  hmResumeBtn: { padding: "6px 11px", ...goldBtn, fontSize: 11, whiteSpace: "nowrap" },
  hmFinishRow: { display: "flex", justifyContent: "center", marginTop: 9 },
  hmFinishBtn: { width: "100%", padding: "8px 12px", background: "transparent", border: "1px solid rgba(126,184,138,.45)", borderRadius: 3, color: "var(--gain-bright)", fontSize: 12, fontWeight: 600 },
  hmCompleteTag: { width: "100%", textAlign: "center", padding: "7px 12px", background: "var(--gain-wash)", border: "1px solid rgba(126,184,138,.35)", borderRadius: 3, color: "var(--gain-bright)", fontSize: 12, fontWeight: 600 },

  // ── the table: black felt, gold-trimmed rail ──
  tableWrap: { position: "relative", flex: 1, margin: "10px 18px 4px", minHeight: 300 },
  rail: {
    position: "absolute", left: "0.5%", top: "5%", width: "99%", height: "90%", borderRadius: 9999,
    background: "linear-gradient(165deg, #1d1b16 0%, #110f0c 50%, #070605 100%)",
    border: "1px solid rgba(200,168,90,.42)",
    boxShadow: "0 30px 80px rgba(0,0,0,.75), inset 0 0 0 6px #0b0a08, inset 0 0 0 7px rgba(200,168,90,.24), inset 0 2px 12px rgba(255,236,190,.06)",
  },
  felt: {
    position: "absolute", inset: 24, borderRadius: 9999, background: FELT,
    boxShadow: "inset 0 0 100px rgba(0,0,0,.8), inset 0 0 0 1px rgba(200,168,90,.2)", overflow: "hidden",
  },
  bettingLine: { position: "absolute", inset: "19% 13%", borderRadius: 9999, border: "1px solid rgba(200,168,90,.16)" },
  feltBrand: { position: "absolute", left: "50%", top: "50%", transform: "translate(-50%,-50%)", fontSize: 58, color: "rgba(200,168,90,.07)", textAlign: "center", pointerEvents: "none", fontFamily: "var(--font-display)", lineHeight: 1 },
  feltBrandText: { fontSize: 17, letterSpacing: 9, marginTop: 6, fontStyle: "italic" },
  dealerCut: {
    position: "absolute", left: "50%", bottom: "-1%", transform: "translateX(-50%)",
    width: "30%", maxWidth: 330, height: "21%", minHeight: 66,
    background: "var(--bg)", borderRadius: "130px 130px 0 0",
    border: "1px solid rgba(200,168,90,.4)", borderBottom: "none",
    boxShadow: "inset 0 10px 24px rgba(0,0,0,.75)", zIndex: 3,
  },
  chipTray: {
    position: "absolute", left: "50%", bottom: "16%", transform: "translateX(-50%)",
    display: "flex", gap: 5, padding: "5px 8px", borderRadius: 3, zIndex: 5,
    background: "linear-gradient(#17140f,#0b0a07)", border: "1px solid rgba(200,168,90,.3)",
    boxShadow: "0 6px 14px rgba(0,0,0,.65)",
  },
  chipWell: { width: 20, height: 26, borderRadius: 3, background: "#060504", display: "flex", alignItems: "flex-end", justifyContent: "center", padding: 2, boxShadow: "inset 0 2px 5px rgba(0,0,0,.85)" },
  chipStack: { width: 18, height: 18, borderRadius: "50%", border: "2px dashed", boxShadow: "0 -3px 0 rgba(255,240,200,.16), 0 -6px 0 rgba(0,0,0,.3)" },
  dealerLabel: { position: "absolute", left: "50%", bottom: "5.5%", transform: "translateX(-50%)", fontSize: 9, letterSpacing: 4, color: "var(--gold-dim)", fontWeight: 600, zIndex: 5 },
  potArea: { position: "absolute", left: "50%", top: "29%", transform: "translate(-50%,-50%)", textAlign: "center" },
  potLabel: { fontSize: 9.5, letterSpacing: 4, color: "var(--gold-dim)", fontWeight: 600 },
  potValue: { fontSize: 30, fontWeight: 500, color: "var(--gold-bright)", fontFamily: "var(--font-display)", textShadow: "0 0 24px rgba(222,192,120,.25), 0 2px 6px rgba(0,0,0,.6)", fontVariantNumeric: "tabular-nums", lineHeight: 1.1 },
  boardArea: { position: "absolute", left: "50%", top: "53%", transform: "translate(-50%,-50%)", display: "flex", flexDirection: "column", alignItems: "center", gap: 5 },
  runLabel: { fontSize: 9, letterSpacing: 3, color: "var(--gold-dim)", fontWeight: 600 },
  boardRow: { display: "flex", gap: 10 },

  // ── seats ──
  seat: { position: "absolute", transform: "translate(-50%,-50%)", zIndex: 5 },
  emptySeat: { width: 98, padding: "10px 8px", textAlign: "center", borderRadius: 3, border: "1px dashed var(--line-strong)", background: "rgba(13,12,10,.55)", display: "flex", flexDirection: "column", alignItems: "center", gap: 6 },
  emptySeatNum: { fontSize: 9.5, fontWeight: 600, letterSpacing: 2, color: "var(--text-faint)" },
  sitBtn: { padding: "5px 12px", ...ghostBtn, color: "var(--gold-bright)", borderColor: "var(--line-gold)", fontSize: 11 },
  sitInput: { width: 82, padding: "4px 6px", background: "var(--field)", border: "1px solid var(--gold-dim)", borderRadius: 3, color: "var(--text)", fontSize: 12, textAlign: "center", outline: "none" },
  seatBox: { position: "relative", width: 130, background: PANEL_BG, border: "1px solid var(--line-strong)", borderRadius: 3, padding: "9px 8px 7px", textAlign: "center", transition: "box-shadow .25s ease, border-color .25s ease, opacity .25s ease", boxShadow: "0 10px 24px rgba(0,0,0,.5)" },
  cardsRow: { display: "flex", gap: 4, justifyContent: "center", marginBottom: 6 },
  seatName: { fontSize: 13, fontWeight: 600, color: "var(--text-strong)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", letterSpacing: 0.2 },
  seatStack: { fontSize: 13, fontWeight: 600, color: "var(--gold-bright)", cursor: "pointer", marginTop: 1, fontVariantNumeric: "tabular-nums" },
  handDesc: { fontSize: 10, fontWeight: 500, marginTop: 3, lineHeight: 1.2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" },
  boughtTag: { marginTop: 4, fontSize: 8.5, fontWeight: 700, letterSpacing: 1, color: "var(--ink)", background: "var(--gold-bright)", borderRadius: 999, padding: "1px 7px", display: "inline-block" },
  bountyRow: { marginTop: 4, display: "flex", alignItems: "center", justifyContent: "center", gap: 4 },
  bountyTag: { fontSize: 8.5, fontWeight: 700, letterSpacing: 0.6, color: "var(--copper)", border: "1px solid rgba(212,129,95,.45)", borderRadius: 999, padding: "0 7px" },
  squidTag: { fontSize: 8.5, fontWeight: 700, letterSpacing: 0.6, color: "var(--violet)", border: "1px solid rgba(169,156,216,.45)", borderRadius: 999, padding: "0 7px" },
  bountyBtn: { width: 16, height: 16, padding: 0, ...ghostBtn, border: "1px solid var(--line)", fontSize: 11, lineHeight: 1 },
  stackInput: { width: 84, marginTop: 2, padding: "2px 6px", background: "var(--field)", border: "1px solid var(--gold-dim)", borderRadius: 3, color: "var(--gold-bright)", fontSize: 13, textAlign: "center", outline: "none" },
  badge: { position: "absolute", top: -9, left: -9, fontSize: 8.5, fontWeight: 700, color: "var(--ink)", padding: "2px 7px", borderRadius: 999, letterSpacing: 0.8, boxShadow: "0 2px 6px rgba(0,0,0,.5)" },
  gameDiscs: { position: "absolute", top: -10, right: -10, display: "flex", gap: 3, zIndex: 2 },
  squidDisc: { width: 22, height: 22, borderRadius: "50%", background: "radial-gradient(circle at 35% 30%,#e4dcff,#a99cd8 55%,#5f4f99)", color: "#16112c", fontSize: 11, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "0 2px 6px rgba(0,0,0,.6), inset 0 0 0 2px rgba(255,255,255,.2)" },
  bountyDisc: { width: 22, height: 22, borderRadius: "50%", background: "radial-gradient(circle at 35% 30%,#f6cdb5,#d4815f 55%,#8e4a2f)", color: "#1d0d06", fontSize: 11, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "0 2px 6px rgba(0,0,0,.6), inset 0 0 0 2px rgba(255,255,255,.2)" },
  dealerBtn: { position: "absolute", bottom: -9, right: -9, width: 24, height: 24, borderRadius: "50%", background: "radial-gradient(circle at 35% 30%,#fff7df,#d9bf7a 55%,#a8893f)", color: "var(--ink)", fontSize: 11, fontWeight: 700, display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "0 3px 8px rgba(0,0,0,.6)", fontFamily: "var(--font-display)" },
  betChip: { position: "absolute", left: "50%", top: "108%", transform: "translateX(-50%)", background: "rgba(8,7,6,.85)", border: "1px solid var(--gold-dim)", color: "var(--gold-pale)", fontSize: 11, fontWeight: 600, padding: "2px 9px", borderRadius: 999, whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" },

  // ── action bar ──
  actionBar: { position: "relative", minHeight: 88, flexShrink: 0, borderTop: "1px solid var(--line)", background: "linear-gradient(180deg, var(--surface) 0%, var(--bg-deep) 100%)", padding: "12px 24px", display: "flex", alignItems: "center", justifyContent: "center" },
  barCenter: { display: "flex", flexDirection: "column", alignItems: "center", gap: 10 },
  barHint: { fontSize: 12.5, color: "var(--text-dim)", textAlign: "center" },
  dealBtn: { padding: "13px 42px", ...goldBtn, fontSize: 12.5, letterSpacing: 2.6, boxShadow: "0 0 0 1px rgba(200,168,90,.35), 0 12px 32px rgba(200,168,90,.18)" },
  runBtn: { padding: "13px 22px", ...ghostBtn, color: "var(--text)", fontSize: 13 },
  runWinnerBox: { display: "flex", flexDirection: "column", alignItems: "center", gap: 5, background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 2, padding: "7px 9px" },
  runLabelSm: { fontSize: 9, letterSpacing: 2, color: "var(--gold-dim)", fontWeight: 600 },
  completeBtn: { padding: "13px 42px", background: "linear-gradient(135deg,#93c79d,#5f9a6d)", color: "var(--ink)", border: "none", borderRadius: 3, fontSize: 12.5, fontWeight: 700, letterSpacing: 2.4, boxShadow: "0 0 0 1px rgba(126,184,138,.35), 0 12px 32px rgba(126,184,138,.16)" },
  actionRow: { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", justifyContent: "center" },
  actorTag: { textAlign: "right", marginRight: 8 },
  actorName: { fontSize: 21, fontWeight: 500, color: "var(--gold-bright)", fontFamily: "var(--font-display)", lineHeight: 1.1 },
  actorSub: { fontSize: 11, color: "var(--text-mute)", fontVariantNumeric: "tabular-nums" },
  actBtn: { padding: "12px 18px", border: "1px solid transparent", borderRadius: 3, fontSize: 12, fontWeight: 700, letterSpacing: 1.4, color: "var(--text-strong)", whiteSpace: "nowrap" },
  foldBtn: { background: "linear-gradient(180deg,#8d4848,#6b3333)", borderColor: "rgba(198,120,120,.35)" },
  checkBtn: { background: "linear-gradient(180deg,#4d6d8b,#38536c)", borderColor: "rgba(143,176,207,.3)" },
  raiseBtn: { background: "var(--gold-grad)", color: "var(--ink)" },
  allinBtn: { background: "linear-gradient(180deg,#71659f,#544982)", borderColor: "rgba(169,156,216,.35)" },
  raiseGroup: { display: "flex", alignItems: "center", gap: 8, background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 3, padding: "6px 8px" },
  sizeBtns: { display: "flex", gap: 4 },
  sizeBtn: { padding: "5px 9px", ...ghostBtn, fontSize: 11 },
  betInput: { width: 94, padding: "8px", background: "var(--field)", border: "1px solid var(--line-strong)", borderRadius: 3, color: "var(--gold-pale)", fontSize: 13, fontWeight: 600, textAlign: "center", outline: "none" },
  slider: { width: 120, accentColor: "var(--gold)" },
  errorInline: { position: "absolute", top: -2, left: "50%", transform: "translate(-50%,-100%)", background: "rgba(107,51,51,.96)", color: "var(--text-strong)", border: "1px solid var(--loss)", borderRadius: 3, padding: "10px 18px", fontSize: 13, fontWeight: 500, whiteSpace: "nowrap", boxShadow: "0 12px 32px rgba(0,0,0,.6)", zIndex: 20 },

  // ── overlays ──
  pickerOverlay: { position: "fixed", inset: 0, background: "var(--overlay)", backdropFilter: "blur(3px)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50 },
  pickerBox: { background: PANEL_BG, border: "1px solid var(--line-gold)", borderRadius: 3, padding: 18, boxShadow: "var(--shadow-lg)" },
  confirmBox: { width: 400, maxWidth: "92vw", background: PANEL_BG, border: "1px solid var(--line-gold)", borderRadius: 3, padding: 24, boxShadow: "var(--shadow-lg)", display: "flex", flexDirection: "column", gap: 14 },
  confirmTitle: { fontSize: 24, fontWeight: 500, color: "var(--gold-bright)", fontFamily: "var(--font-display)" },
  confirmBody: { fontSize: 13.5, lineHeight: 1.65, color: "var(--text)" },
  confirmCards: { fontFamily: "var(--font-mono)", fontWeight: 500, color: "var(--gold-pale)", background: "var(--gold-wash)", border: "1px solid var(--line-gold)", padding: "1px 6px", borderRadius: 3 },
  confirmBtnRow: { display: "flex", gap: 8, flexWrap: "wrap" },
  confirmYes: { flex: 1, minWidth: 150, padding: "10px 14px", ...goldBtn, fontSize: 12.5 },
  confirmNo: { padding: "10px 16px", ...ghostBtn, fontSize: 12.5 },
  pickerTitle: { fontSize: 9.5, fontWeight: 600, letterSpacing: 2.4, color: "var(--gold)", marginBottom: 12, textTransform: "uppercase" },
  pickerCard: { position: "relative", width: 38, height: 40, borderRadius: 3, color: "#fff", padding: 0, display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "0 2px 6px rgba(0,0,0,.4), inset 0 1px 0 rgba(255,255,255,.18)", transition: "transform .12s ease, box-shadow .15s ease, opacity .15s" },
  // Card face: a large white rank, and the suit small in the top-right corner.
  cardRank: { fontFamily: "var(--font-ui)", fontWeight: 700, lineHeight: 1, color: "#fff", textShadow: "0 1px 2px rgba(0,0,0,.35)" },
  cardSuit: { position: "absolute", top: 2, right: 3, lineHeight: 1, color: "rgba(255,255,255,.92)" },
  pickerClear: { padding: "7px 16px", ...ghostBtn, fontSize: 12 },
  buyBox: { background: PANEL_BG, border: "1px solid var(--line-gold)", borderRadius: 3, padding: 20, maxWidth: 440, boxShadow: "var(--shadow-lg)" },
  buyNote: { fontSize: 12, color: "var(--text-dim)", marginBottom: 14, lineHeight: 1.5 },
  buyBtn: { flex: 1, padding: "12px 10px", border: "1px solid transparent", borderRadius: 3, color: "var(--text-strong)", fontSize: 13.5, fontWeight: 700, letterSpacing: 0.6 },
  buySub: { fontSize: 10, fontWeight: 400, opacity: 0.85, marginTop: 3, letterSpacing: 0 },
};

// ── Top nav: switch between Hand Builder and Calendar ────────────────────────
function NavItem({ to, label, active, navigate, tour }) {
  return (
    <button
      data-tour={tour}
      className="ui-nav"
      onClick={() => navigate(to)}
      style={{ ...navStyles.item, ...(active ? navStyles.itemActive : {}) }}
    >
      {label}
    </button>
  );
}

function NavBar({ route, navigate, me, onLogout, onRedoTutorial }) {
  return (
    <nav style={navStyles.bar}>
      <div style={navStyles.brand}>
        <span style={navStyles.chip}>♠</span>
        <span>Poker <em style={navStyles.brandEm}>Suite</em></span>
      </div>
      <div style={navStyles.items}>
        <NavItem to="/" label="Hand Builder" active={route === "builder"} navigate={navigate} />
        <NavItem to="/calendar" label="Calendar" active={route === "calendar"} navigate={navigate} tour="nav-calendar" />
        <NavItem to="/dashboard" label="Dashboard" active={route === "dashboard"} navigate={navigate} tour="nav-dashboard" />
        {me.user.is_admin && (
          <NavItem to="/admin" label="Admin" active={route === "admin"} navigate={navigate} />
        )}
      </div>
      <div style={navStyles.right}>
        <button style={navStyles.switchBtn} title="Switch to the original site" onClick={() => switchUi("classic")}>Classic view</button>
        <span style={navStyles.user}><span style={navStyles.userDot} />{me.user.username}</span>
        <button style={navStyles.help} title="Redo the tutorial" onClick={onRedoTutorial}>?</button>
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

  // Initial session check. Only a 401 means "logged out": a network blip or a
  // server restart used to drop the user on the login screen mid-session.
  const [offline, setOffline] = useState(false);
  const checkSession = useCallback(() => {
    return api("/api/me")
      .then((m) => { setOffline(false); setMe(m); })
      .catch((e) => { if (e.status === 401) setMe(false); else setOffline(true); });
  }, []);
  useEffect(() => { checkSession(); }, [checkSession]);

  const refreshMe = useCallback(async () => {
    try { const m = await api("/api/me"); setMe(m); return m; }
    catch (e) { if (e.status === 401) setMe(false); return null; }
  }, []);


  const navigate = (path) => {
    try {
      if (window.location.pathname !== path) window.history.pushState({}, "", path);
    } catch { /* ignore */ }
    setRoute(pathToRoute(path));
    // The Dashboard renders the cached /api/me payload; refresh it on the way in
    // so payments or bonuses recorded since are shown.
    if (pathToRoute(path) === "dashboard") refreshMe();
  };

  // Hand a YouTube URL to the Hand Builder, then switch to it.
  const openInBuilder = (url) => {
    try { localStorage.setItem(PENDING_YT_KEY, url || ""); } catch { /* ignore */ }
    navigate("/");
  };

  // Resume a stream in the Hand Builder: URL + last hand's roster/stacks/button.
  const resumeInBuilder = (payload) => {
    try { localStorage.setItem(PENDING_RESUME_KEY, JSON.stringify(payload || {})); } catch { /* ignore */ }
    navigate("/");
  };

  const logout = async () => {
    try { await api("/api/logout", { method: "POST" }); } catch { /* ignore */ }
    setMe(false);
    navigate("/");
  };

  // Onboarding tour: auto-runs on the Hand Builder until the user's record says
  // tutorial_completed. Derived (no effect) so it can't re-trigger itself.
  const [tutorialDismissed, setTutorialDismissed] = useState(false);
  const showTutorial = !!(me && me.user && !me.user.tutorial_completed && !tutorialDismissed && route === "builder");
  // One-time What's New popup (new workers see it after the tutorial).
  const [whatsNew, setWhatsNew] = useState(() => !whatsNewSeen());
  const closeWhatsNew = () => { markWhatsNewSeen(); setWhatsNew(false); };
  // award = finished via the last step (not skipped); the server only pays once.
  const finishTutorial = async (award) => {
    setTutorialDismissed(true);
    try { await api("/api/tutorial-complete", { method: "POST", body: { completed: true, award: !!award } }); } catch { /* ignore */ }
    refreshMe();
  };
  const redoTutorial = async () => {
    try { await api("/api/tutorial-complete", { method: "POST", body: { completed: false } }); } catch { /* ignore */ }
    await refreshMe();
    setTutorialDismissed(false);
    navigate("/");
  };

  if (me === null) {
    return offline ? (
      <div style={navStyles.loading}>
        Can't reach the server.&nbsp;
        <button style={navStyles.logout} onClick={checkSession}>Retry</button>
      </div>
    ) : <div style={navStyles.loading}>Loading…</div>;
  }
  if (!me) return <Auth onAuthed={(m) => { setMe(m); navigate("/"); }} />;

  let view;
  if (route === "calendar") view = <Calendar me={me} onOpenInBuilder={openInBuilder} onResumeStream={resumeInBuilder} refreshMe={refreshMe} />;
  else if (route === "dashboard") view = <Dashboard user={me.user} dashboard={me.dashboard} />;
  else if (route === "admin") {
    view = me.user.is_admin
      ? <Admin />
      : <div style={navStyles.loading}>Admin access only.</div>;
  } else view = <HandBuilder me={me} refreshMe={refreshMe} />;

  return (
    <div style={navStyles.root}>
      <NavBar route={route} navigate={navigate} me={me} onLogout={logout} onRedoTutorial={redoTutorial} />
      <div style={navStyles.view}>{view}</div>
      {showTutorial && <TutorialOverlay steps={TUTORIAL_STEPS} bonusAwarded={!!(me.dashboard && me.dashboard.tutorial_bonus)} onFinish={finishTutorial} />}
      {whatsNew && !showTutorial && <WhatsNew ui={WHATS_NEW_UI} current="new" onClose={closeWhatsNew} />}
    </div>
  );
}

// The What's New popup's look in this UI.
const WHATS_NEW_UI = {
  overlay: { position: "fixed", inset: 0, background: "var(--overlay)", backdropFilter: "blur(3px)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1100, padding: 16 },
  box: { width: 540, maxWidth: "94vw", maxHeight: "90vh", overflowY: "auto", boxSizing: "border-box", background: "linear-gradient(180deg, #1a1814 0%, #12110e 100%)", border: "1px solid var(--line-gold)", borderRadius: 3, padding: 26, boxShadow: "var(--shadow-lg)", display: "flex", flexDirection: "column", gap: 14, color: "var(--text)", fontFamily: "var(--font-ui)" },
  title: { fontSize: 30, fontWeight: 500, color: "var(--text-strong)", fontFamily: "var(--font-display)" },
  section: { display: "flex", flexDirection: "column", gap: 6, padding: "13px 15px", background: "var(--surface-2)", border: "1px solid var(--line)", borderRadius: 2 },
  head: { fontSize: 19, fontWeight: 500, color: "var(--gold-bright)", fontFamily: "var(--font-display)" },
  text: { fontSize: 13, lineHeight: 1.6, color: "var(--text-2)" },
  strong: { color: "var(--gold-pale)", fontWeight: 600 },
  primary: { flex: 1, minWidth: 160, padding: "11px 14px", background: "var(--gold-grad)", border: "none", borderRadius: 3, color: "var(--ink)", fontSize: 13, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" },
  btn: { padding: "11px 16px", background: "transparent", border: "1px solid var(--line-strong)", borderRadius: 3, color: "var(--text-2)", fontSize: 13, fontWeight: 500, cursor: "pointer", fontFamily: "inherit" },
};

const navStyles = {
  root: { display: "flex", flexDirection: "column", height: "100vh", background: "var(--bg)" },
  view: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" },
  bar: {
    position: "relative", zIndex: 100, height: 52, flexShrink: 0, boxSizing: "border-box",
    display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16,
    padding: "0 20px", background: "linear-gradient(180deg, #13120f 0%, var(--bg) 100%)",
    borderBottom: "1px solid var(--line)", boxShadow: "0 1px 0 rgba(200,168,90,.07)",
    fontFamily: "var(--font-ui)",
  },
  brand: { display: "flex", alignItems: "center", gap: 10, fontFamily: "var(--font-display)", fontSize: 22, fontWeight: 500, letterSpacing: 0.3, color: "var(--text-strong)", whiteSpace: "nowrap" },
  brandEm: { color: "var(--gold)", fontStyle: "italic" },
  chip: { display: "inline-flex", width: 26, height: 26, borderRadius: "50%", background: "var(--gold-grad)", color: "var(--ink)", alignItems: "center", justifyContent: "center", fontSize: 14, fontFamily: "var(--font-ui)", boxShadow: "0 0 0 1px rgba(200,168,90,.35), 0 0 16px rgba(200,168,90,.2)" },
  items: { display: "flex", gap: 4, alignItems: "center", height: "100%" },
  item: { height: 34, padding: "0 14px", background: "transparent", border: "none", borderRadius: 3, color: "var(--text-dim)", fontSize: 12.5, fontWeight: 500, letterSpacing: 0.4, fontFamily: "inherit" },
  itemActive: { color: "var(--text-strong)", background: "var(--gold-wash)", boxShadow: "inset 0 -2px 0 var(--gold)" },
  right: { display: "flex", gap: 10, alignItems: "center" },
  switchBtn: { padding: "6px 11px", background: "transparent", border: "1px solid var(--line)", borderRadius: 3, color: "var(--text-mute)", fontSize: 11.5, fontFamily: "inherit" },
  user: { display: "inline-flex", alignItems: "center", gap: 7, fontSize: 12.5, fontWeight: 500, color: "var(--text-2)", padding: "0 4px" },
  userDot: { width: 6, height: 6, borderRadius: "50%", background: "var(--gold)", boxShadow: "0 0 8px rgba(200,168,90,.6)" },
  help: { width: 28, height: 28, borderRadius: "50%", background: "transparent", border: "1px solid var(--line-strong)", color: "var(--gold)", fontSize: 13, fontWeight: 600, fontFamily: "inherit" },
  logout: { padding: "7px 13px", background: "transparent", border: "1px solid var(--line-strong)", borderRadius: 3, color: "var(--text-2)", fontSize: 12, fontWeight: 500, fontFamily: "inherit" },
  loading: { flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 10, minHeight: "60vh", color: "var(--text-mute)", fontFamily: "var(--font-display)", fontStyle: "italic", fontSize: 20 },
};
