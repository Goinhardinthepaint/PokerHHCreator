import { useState, useMemo, useEffect, useRef } from "react";
import { api } from "./api.js";
import SEED_STREAMS from "./data/hcl_streams.json";

// ─────────────────────────────────────────────────────────────────────────────
// Calendar / Stream Manager — a monthly grid of HCL streams. Each day cell shows
// its streams with a status colour; clicking a day opens a side panel to add
// streams, edit progress, mark complete, or jump into the Hand Builder.
//
// The server (GET /api/streams) is the source of truth for which streams exist,
// so a stream added once reaches every worker. localStorage is a cache: it paints
// instantly and keeps working offline, and the bundled scrape
// (scripts/scrape_hcl_streams.py → src/data/hcl_streams.json) only seeds a client
// that has never talked to the server. Deletions are local and stick — they're
// remembered by id so the server merge can't resurrect them.
// ─────────────────────────────────────────────────────────────────────────────

const STORAGE_KEY = "pokerStreams.v1";
const DEFAULT_ESTIMATE = 150; // assumed hands/stream when not supplied

// Bundled scrape, normalised to the full stream shape (adds addedAt).
const seedStreams = () => SEED_STREAMS.map((s) => ({ ...s, addedAt: s.addedAt || "" }));

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// ── Persistence ──────────────────────────────────────────────────────────────
function loadStreams() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return seedStreams(); // never opened → seed
    const data = JSON.parse(raw);
    const streams = Array.isArray(data.streams) ? data.streams : [];
    // Seed once: only if this client has never been marked seeded AND is empty.
    if (!data.seeded && streams.length === 0) return seedStreams();
    return streams;
  } catch {
    return seedStreams();
  }
}

// Ids the user deleted here. Kept so merging the server catalog back in doesn't
// resurrect them (deleting used to stick only because we never re-read the seed).
function loadDeleted() {
  try {
    const data = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    return Array.isArray(data.deleted) ? data.deleted : [];
  } catch {
    return [];
  }
}

// Fold the server catalog into the local list. Server rows win on metadata (it's
// the shared truth), locally-added streams that never reached the server survive,
// and deleted ids stay gone. That includes handsEstimated, which is why
// updateStream mirrors manual estimate edits up — the server row has to already
// carry them or this would hand back the scraped guess on the next load.
function mergeCatalog(local, serverRows, deleted) {
  const gone = new Set(deleted);
  const byId = new Map(local.map((s) => [s.id, s]));
  for (const row of serverRows) {
    if (gone.has(row.id)) continue;
    const prev = byId.get(row.id);
    byId.set(row.id, prev ? { ...prev, ...row, addedAt: prev.addedAt || row.addedAt } : row);
  }
  return [...byId.values()];
}

// Debounced metadata upsert. The estimate input fires on every keystroke, and
// each one would otherwise be its own request.
const mirrorTimers = new Map();
function mirrorEstimate(stream) {
  clearTimeout(mirrorTimers.get(stream.id));
  mirrorTimers.set(
    stream.id,
    setTimeout(() => {
      mirrorTimers.delete(stream.id);
      api("/api/streams", { method: "POST", body: stream }).catch(() => {});
    }, 600),
  );
}

// ── Small helpers ──────────────────────────────────────────────────────────
const pad2 = (n) => String(n).padStart(2, "0");
const ymd = (y, m, d) => `${y}-${pad2(m + 1)}-${pad2(d)}`; // m is 0-based
const genId = () => Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-4);
const nowISO = () => { try { return new Date().toISOString(); } catch { return ""; } };

function fmtDateLong(dateStr) {
  const [y, m, d] = String(dateStr).split("-").map(Number);
  if (!y || !m || !d) return dateStr;
  return `${MONTHS[m - 1]} ${d}, ${y}`;
}
function fmtDuration(min) {
  const n = Number(min) || 0;
  if (!n) return "—";
  const h = Math.floor(n / 60), m = n % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}
function streamStatus(s) {
  if (s.isComplete) return "green";
  if ((s.handsCompleted || 0) > 0) return "yellow";
  return "red";
}
const STATUS_COLOR = { grey: "#475569", red: "#dc2626", yellow: "#f59e0b", green: "#16a34a" };
const STATUS_LABEL = { grey: "No stream", red: "Not started", yellow: "In progress", green: "Complete" };

// Build a 6×7 grid (always 42 cells) for the given month, including the
// trailing/leading days of adjacent months (greyed out in the UI).
function buildGrid(year, month, todayStr) {
  const startDow = new Date(year, month, 1).getDay();
  const start = new Date(year, month, 1 - startDow);
  const cells = [];
  for (let i = 0; i < 42; i++) {
    const dt = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    const date = ymd(dt.getFullYear(), dt.getMonth(), dt.getDate());
    cells.push({ date, day: dt.getDate(), inMonth: dt.getMonth() === month, isToday: date === todayStr });
  }
  return cells;
}

// Parse a pasted/uploaded CSV: `date, youtube_url, title, duration_minutes`.
// A header row (containing "date" and a url/youtube column) is skipped. Titles
// may contain commas — the last numeric field is treated as the duration.
const unquote = (v) => String(v).trim().replace(/^"(.*)"$/s, "$1").trim();
function parseCSV(text) {
  const out = [];
  const lines = String(text).split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  for (let i = 0; i < lines.length; i++) {
    const parts = lines[i].split(",").map((p) => p.trim());
    if (parts.length < 2) continue;
    // Skip an obvious header row.
    if (i === 0 && /date/i.test(parts[0]) && parts.slice(1).some((p) => /url|youtube/i.test(p))) continue;
    const date = unquote(parts[0]);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) continue; // need a valid ISO date
    const youtubeUrl = unquote(parts[1] || "");
    const lastNumeric = /^\d+$/.test(parts[parts.length - 1]);
    const durationMinutes = lastNumeric && parts.length >= 4 ? Number(parts[parts.length - 1]) : 0;
    const titleParts = parts.slice(2, lastNumeric && parts.length >= 4 ? parts.length - 1 : parts.length);
    const title = unquote(titleParts.join(", ")) || "HCL Stream";
    out.push({ date, youtubeUrl, title, durationMinutes });
  }
  return out;
}

// ── Calendar page ─────────────────────────────────────────────────────────
export default function Calendar({ me, onOpenInBuilder, onResumeStream, refreshMe }) {
  const [streams, setStreams] = useState(loadStreams);
  const [deletedIds, setDeletedIds] = useState(loadDeleted);
  // Server truth for per-stream { handsCompleted, isComplete }, overlaid below.
  const [serverState, setServerState] = useState({});
  const [monthsMap, setMonthsMap] = useState({}); // "YYYY-MM" -> { owner, progress, … }
  const today = useMemo(() => { const d = new Date(); return { y: d.getFullYear(), m: d.getMonth(), str: ymd(d.getFullYear(), d.getMonth(), d.getDate()) }; }, []);
  const [view, setView] = useState(() => ({ year: today.y, month: today.m }));
  const [selectedDate, setSelectedDate] = useState(null); // "YYYY-MM-DD" or null
  const [importOpen, setImportOpen] = useState(false);

  // Mirror every change to localStorage. `seeded: true` is recorded so a client
  // that later deletes all streams isn't re-seeded from the bundle.
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ streams, seeded: true, deleted: deletedIds }));
    } catch { /* ignore */ }
  }, [streams, deletedIds]);

  const fetchState = () => api("/api/streams/state").then((d) => setServerState(d.states || {})).catch(() => {});
  useEffect(() => { fetchState(); }, []);

  // Shared catalog → local cache. Runs once on mount; deletions are read straight
  // from storage so a stream the user removed earlier doesn't come back. A failure
  // here (offline, logged out) just leaves the cached list in place.
  useEffect(() => {
    api("/api/streams")
      .then((d) => {
        const rows = Array.isArray(d.streams) ? d.streams : [];
        if (rows.length) setStreams((local) => mergeCatalog(local, rows, loadDeleted()));
      })
      .catch(() => {});
  }, []);

  // Month ownership + progress (who's responsible for each month).
  useEffect(() => {
    api("/api/months")
      .then((d) => setMonthsMap(Object.fromEntries((d.months || []).map((m) => [m.month, m]))))
      .catch(() => {});
  }, []);
  const viewMonthKey = `${view.year}-${String(view.month + 1).padStart(2, "0")}`;
  const viewMonth = monthsMap[viewMonthKey];
  const isMyMonth = viewMonth && viewMonth.owner && me && viewMonth.owner.id === me.user.id;

  // Overlay live server hand counts + completion onto the local catalog.
  const merged = useMemo(() => streams.map((s) => {
    const sv = serverState[s.id];
    return sv ? { ...s, handsCompleted: sv.handsCompleted, isComplete: sv.isComplete } : s;
  }), [streams, serverState]);

  // Index streams by date for quick cell lookup.
  const byDate = useMemo(() => {
    const m = new Map();
    for (const s of merged) {
      if (!m.has(s.date)) m.set(s.date, []);
      m.get(s.date).push(s);
    }
    return m;
  }, [merged]);

  // Aggregate stats across ALL streams (not just the visible month).
  const stats = useMemo(() => {
    const total = merged.length;
    const completed = merged.filter((s) => s.isComplete).length;
    const handsDone = merged.reduce((a, s) => a + (s.handsCompleted || 0), 0);
    const handsEst = merged.reduce((a, s) => a + (s.handsEstimated || 0), 0);
    return { total, completed, handsDone, handsEst, pct: total ? Math.round((completed / total) * 100) : 0 };
  }, [merged]);

  const grid = useMemo(() => buildGrid(view.year, view.month, today.str), [view, today.str]);

  const prevMonth = () => setView((v) => (v.month === 0 ? { year: v.year - 1, month: 11 } : { year: v.year, month: v.month - 1 }));
  const nextMonth = () => setView((v) => (v.month === 11 ? { year: v.year + 1, month: 0 } : { year: v.year, month: v.month + 1 }));
  const goToday = () => { setView({ year: today.y, month: today.m }); setSelectedDate(today.str); };

  // ── Stream mutations ──────────────────────────────────────────────────────
  const addStream = ({ date, youtubeUrl, title, durationMinutes, handsEstimated }) => {
    const s = {
      id: genId(),
      youtubeUrl: youtubeUrl || "",
      title: (title || "").trim() || "HCL Stream",
      date,
      durationMinutes: Number(durationMinutes) || 0,
      handsCompleted: 0,
      handsEstimated: Number(handsEstimated) || DEFAULT_ESTIMATE,
      isComplete: false,
      addedAt: nowISO(),
    };
    setStreams((arr) => [...arr, s]);
    api("/api/streams", { method: "POST", body: s }).catch(() => {}); // mirror to server catalog
  };
  const updateStream = (id, patch) => {
    setStreams((arr) => arr.map((s) => (s.id === id ? { ...s, ...patch } : s)));
    // handsEstimated is a manual override. It only lived in localStorage before,
    // so the server merge would reset it on the next load — push it up instead.
    // Debounced: this fires on every keystroke in the number input.
    if ("handsEstimated" in patch) {
      const prev = streams.find((s) => s.id === id);
      if (prev) mirrorEstimate({ ...prev, ...patch });
    }
  };
  const removeStream = (id) => {
    setStreams((arr) => arr.filter((s) => s.id !== id));
    setDeletedIds((ids) => (ids.includes(id) ? ids : [...ids, id]));
  };

  // Mark a stream complete: optimistic locally, authoritative on the server
  // (which awards the $0.05/hand stream bonus to the worker(s) who built it).
  const markComplete = async (stream, complete = true) => {
    updateStream(stream.id, { isComplete: complete });
    try {
      await api("/api/streams/complete", {
        method: "POST",
        body: {
          id: stream.id, complete, youtubeUrl: stream.youtubeUrl, title: stream.title,
          date: stream.date, durationMinutes: stream.durationMinutes, handsEstimated: stream.handsEstimated,
        },
      });
      fetchState();
      if (refreshMe) refreshMe();
    } catch { /* keep the optimistic local toggle */ }
  };

  // Resume a stream: pull the last hand's saved table + timestamp from the
  // server and hand it to the Hand Builder so work continues where it stopped.
  const resumeStream = async (stream) => {
    let payload = { youtubeUrl: stream.youtubeUrl || `https://youtu.be/${stream.id}` };
    try {
      const r = await api(`/api/streams/${stream.id}/resume`);
      const ts = r.last_timestamp || 0;
      payload = {
        youtubeUrl: `https://youtu.be/${stream.id}${ts ? `?t=${ts}` : ""}`,
        roster: r.resume_state?.roster,
        buttonSeat: r.resume_state?.buttonSeat,
        handNumber: r.resume_state?.handNumber,
      };
    } catch { /* fall back to just the URL */ }
    onResumeStream(payload);
  };

  const importStreams = (rows) => {
    // De-dupe against existing (date + url) so re-imports don't pile up.
    const seen = new Set(streams.map((s) => `${s.date}|${s.youtubeUrl}`));
    const fresh = [];
    for (const r of rows) {
      const key = `${r.date}|${r.youtubeUrl}`;
      if (seen.has(key)) continue;
      seen.add(key);
      fresh.push({
        id: genId(),
        youtubeUrl: r.youtubeUrl,
        title: r.title || "HCL Stream",
        date: r.date,
        durationMinutes: Number(r.durationMinutes) || 0,
        handsCompleted: 0,
        handsEstimated: DEFAULT_ESTIMATE,
        isComplete: false,
        addedAt: nowISO(),
      });
    }
    if (fresh.length) {
      setStreams((arr) => [...arr, ...fresh]);
      api("/api/streams/import", { method: "POST", body: { rows: fresh } }).catch(() => {});
    }
    return { added: fresh.length, skipped: rows.length - fresh.length };
  };

  const dayStreams = selectedDate ? byDate.get(selectedDate) || [] : [];

  return (
    <div style={s.page}>
      {/* Stats banner */}
      <StatsBanner stats={stats} />

      {/* Toolbar: month nav + actions */}
      <div style={s.toolbar}>
        <div style={s.monthNav}>
          <button style={s.navArrow} onClick={prevMonth} title="Previous month">‹</button>
          <div style={s.monthTitle}>{MONTHS[view.month]} {view.year}</div>
          <button style={s.navArrow} onClick={nextMonth} title="Next month">›</button>
          <button style={s.todayBtn} onClick={goToday}>Today</button>
          {viewMonth && (isMyMonth ? (
            <span style={s.yourMonth}>★ YOUR MONTH</span>
          ) : viewMonth.owner ? (
            <span style={s.ownedBy}>{viewMonth.owner.username}'s month</span>
          ) : null)}
          {viewMonth && viewMonth.is_complete && <span style={s.monthDone}>✓ complete</span>}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button style={s.addBtn} onClick={() => setSelectedDate(today.str)}>+ Add Stream</button>
          <button style={s.importBtn} onClick={() => setImportOpen(true)}>⇪ Import Stream List</button>
        </div>
      </div>

      {/* Weekday header */}
      <div style={s.weekRow}>
        {WEEKDAYS.map((w) => (<div key={w} style={s.weekCell}>{w}</div>))}
      </div>

      {/* Month grid */}
      <div style={s.grid}>
        {grid.map((cell) => {
          const cs = byDate.get(cell.date) || [];
          return (
            <div
              key={cell.date}
              style={{ ...s.cell, ...(cell.inMonth ? {} : s.cellOut), ...(cell.isToday ? s.cellToday : {}) }}
              onClick={() => setSelectedDate(cell.date)}
            >
              <div style={s.cellHead}>
                <span style={{ ...s.cellDay, ...(cell.isToday ? s.cellDayToday : {}) }}>{cell.day}</span>
                {cs.length > 1 && <span style={s.cellMulti}>{cs.length}</span>}
              </div>
              <div style={s.cellStreams}>
                {cs.slice(0, 3).map((st) => {
                  const status = streamStatus(st);
                  return (
                    <div key={st.id} style={{ ...s.streamChip, borderLeft: `3px solid ${STATUS_COLOR[status]}` }} title={st.title}>
                      <div style={s.chipTitle}>{st.title || "HCL Stream"}</div>
                      <div style={s.chipMeta}>
                        <span style={{ ...s.statusDot, background: STATUS_COLOR[status] }} />
                        {(st.handsCompleted || 0)}/~{st.handsEstimated || DEFAULT_ESTIMATE}
                      </div>
                    </div>
                  );
                })}
                {cs.length > 3 && <div style={s.moreRow}>+{cs.length - 3} more</div>}
              </div>
            </div>
          );
        })}
      </div>

      {/* Day panel */}
      {selectedDate && (
        <DayPanel
          key={selectedDate}
          date={selectedDate}
          streams={dayStreams}
          onClose={() => setSelectedDate(null)}
          onAdd={addStream}
          onUpdate={updateStream}
          onComplete={markComplete}
          onResume={resumeStream}
          onRemove={removeStream}
          onOpenInBuilder={onOpenInBuilder}
          isAdmin={!!(me && me.user && me.user.is_admin)}
        />
      )}

      {/* Import modal */}
      {importOpen && (
        <ImportModal onClose={() => setImportOpen(false)} onImport={importStreams} />
      )}
    </div>
  );
}

// ── Stats banner ──────────────────────────────────────────────────────────
function StatsBanner({ stats }) {
  const pct = stats.total ? (stats.completed / stats.total) * 100 : 0;
  return (
    <div style={s.banner}>
      <div style={s.statGroup}>
        <div style={s.statBlock}>
          <div style={s.statLabel}>Total streams</div>
          <div style={s.statValue}>{stats.total.toLocaleString()}</div>
        </div>
        <div style={s.statBlock}>
          <div style={s.statLabel}>Completed</div>
          <div style={s.statValue}>{stats.completed.toLocaleString()} / {stats.total.toLocaleString()} <span style={s.statPct}>({stats.pct}%)</span></div>
        </div>
        <div style={s.statBlock}>
          <div style={s.statLabel}>Total hands</div>
          <div style={s.statValue}>{stats.handsDone.toLocaleString()} <span style={s.statSub}>/ ~{stats.handsEst.toLocaleString()}</span></div>
        </div>
      </div>
      <div style={s.progressWrap}>
        <div style={s.progressTrack}>
          <div style={{ ...s.progressFill, width: `${pct}%` }} />
        </div>
        <div style={s.progressCaption}>{stats.pct}% of streams complete</div>
      </div>
    </div>
  );
}

// ── Day panel (right drawer) ────────────────────────────────────────────────
function DayPanel({ date, streams, onClose, onAdd, onUpdate, onComplete, onResume, onRemove, onOpenInBuilder, isAdmin }) {
  const [showAdd, setShowAdd] = useState(streams.length === 0);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [duration, setDuration] = useState("");
  const [estimate, setEstimate] = useState(String(DEFAULT_ESTIMATE));

  const submitAdd = () => {
    if (!url.trim() && !title.trim()) return;
    onAdd({ date, youtubeUrl: url.trim(), title: title.trim(), durationMinutes: duration, handsEstimated: estimate });
    setUrl(""); setTitle(""); setDuration(""); setEstimate(String(DEFAULT_ESTIMATE));
    setShowAdd(false);
  };

  return (
    <div style={s.drawerOverlay} onClick={onClose}>
      <div style={s.drawer} onClick={(e) => e.stopPropagation()}>
        <div style={s.drawerHead}>
          <div>
            <div style={s.drawerTitle}>{fmtDateLong(date)}</div>
            <div style={s.drawerSub}>{streams.length} stream{streams.length === 1 ? "" : "s"}</div>
          </div>
          <button style={s.closeBtn} onClick={onClose}>✕</button>
        </div>

        <div style={s.drawerBody}>
          {streams.length === 0 && !showAdd && (
            <div style={s.emptyDay}>No streams on this day yet.</div>
          )}

          {streams.map((st) => {
            const status = streamStatus(st);
            return (
              <div key={st.id} style={s.streamCard}>
                <div style={s.streamCardTop}>
                  <span style={{ ...s.statusBadge, background: STATUS_COLOR[status] }}>{STATUS_LABEL[status]}</span>
                  <button style={s.miniX} title="Delete stream" onClick={() => { if (window.confirm("Delete this stream?")) onRemove(st.id); }}>✕</button>
                </div>
                <div style={s.streamTitle}>{st.title || "HCL Stream"}</div>
                {st.youtubeUrl ? (
                  <a href={st.youtubeUrl} target="_blank" rel="noreferrer" style={s.streamUrl} title={st.youtubeUrl}>{st.youtubeUrl}</a>
                ) : (
                  <div style={{ ...s.streamUrl, color: "#64748b" }}>No URL</div>
                )}
                <div style={s.streamMetaRow}>
                  <span>⏱ {fmtDuration(st.durationMinutes)}</span>
                  <span>🃏 {(st.handsCompleted || 0)}/~{st.handsEstimated || DEFAULT_ESTIMATE} hands</span>
                </div>

                <div style={s.progressRow}>
                  <label style={s.fieldLabel}>Done</label>
                  <span style={s.doneCount}>{st.handsCompleted || 0}</span>
                  <label style={s.fieldLabel}>of est.</label>
                  <input
                    style={s.numInput}
                    type="number"
                    min={0}
                    value={st.handsEstimated || 0}
                    onChange={(e) => onUpdate(st.id, { handsEstimated: Math.max(0, Number(e.target.value) || 0) })}
                  />
                </div>

                <div style={s.streamActions}>
                  <button
                    style={st.isComplete ? s.completeOn : s.completeBtn}
                    onClick={() => onComplete(st, !st.isComplete)}
                  >
                    {st.isComplete ? "✓ Complete" : "Mark Complete"}
                  </button>
                  <button
                    style={s.resumeBtn}
                    title="Continue from the last completed hand on this stream"
                    onClick={() => onResume(st)}
                  >
                    ▸ Resume
                  </button>
                </div>
                <button
                  style={{ ...s.openBtn, width: "100%", marginTop: 6 }}
                  disabled={!st.youtubeUrl && !st.id}
                  onClick={() => onOpenInBuilder(st.youtubeUrl || `https://youtu.be/${st.id}`)}
                >
                  Open in Hand Builder ↗
                </button>
                {isAdmin && (
                  <a
                    style={s.downloadBtn}
                    href={`/api/admin/export?stream_id=${encodeURIComponent(st.id)}`}
                    target="_blank"
                    rel="noreferrer"
                    title="Download all hands for this stream as one .txt"
                  >⬇ Download Stream</a>
                )}
              </div>
            );
          })}

          {showAdd ? (
            <div style={s.addForm}>
              <div style={s.addFormTitle}>Add a stream</div>
              <label style={s.fieldLabel}>YouTube URL</label>
              <input style={s.textInput} value={url} placeholder="https://youtube.com/watch?v=…" onChange={(e) => setUrl(e.target.value)} />
              <label style={s.fieldLabel}>Title</label>
              <input style={s.textInput} value={title} placeholder="HCL $50/100 NL — …" onChange={(e) => setTitle(e.target.value)} />
              <div style={{ display: "flex", gap: 8 }}>
                <div style={{ flex: 1 }}>
                  <label style={s.fieldLabel}>Duration (min)</label>
                  <input style={s.textInput} type="number" value={duration} placeholder="420" onChange={(e) => setDuration(e.target.value)} />
                </div>
                <div style={{ flex: 1 }}>
                  <label style={s.fieldLabel}>Est. hands</label>
                  <input style={s.textInput} type="number" value={estimate} onChange={(e) => setEstimate(e.target.value)} />
                </div>
              </div>
              <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
                <button style={s.saveBtn} onClick={submitAdd}>Add Stream</button>
                {streams.length > 0 && <button style={s.cancelBtn} onClick={() => setShowAdd(false)}>Cancel</button>}
              </div>
            </div>
          ) : (
            <button style={s.addAnother} onClick={() => setShowAdd(true)}>+ Add Stream</button>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Bulk import modal ───────────────────────────────────────────────────────
function ImportModal({ onClose, onImport }) {
  const [text, setText] = useState("");
  const [result, setResult] = useState(null);
  const fileRef = useRef(null);

  const onFile = (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setText(String(reader.result || ""));
    reader.readAsText(file);
  };

  const doImport = () => {
    const rows = parseCSV(text);
    if (!rows.length) { setResult({ added: 0, skipped: 0, parsed: 0 }); return; }
    const res = onImport(rows);
    setResult({ ...res, parsed: rows.length });
  };

  return (
    <div style={s.modalOverlay} onClick={onClose}>
      <div style={s.modal} onClick={(e) => e.stopPropagation()}>
        <div style={s.modalHead}>
          <span>Import Stream List</span>
          <button style={s.closeBtn} onClick={onClose}>✕</button>
        </div>
        <div style={s.modalNote}>
          CSV columns: <code style={s.code}>date, youtube_url, title, duration_minutes</code><br />
          Date must be <code style={s.code}>YYYY-MM-DD</code>. A header row is detected and skipped.
        </div>

        <div style={{ display: "flex", gap: 8 }}>
          <button style={s.fileBtn} onClick={() => fileRef.current && fileRef.current.click()}>📁 Upload CSV file</button>
          <input ref={fileRef} type="file" accept=".csv,text/csv,text/plain" style={{ display: "none" }} onChange={onFile} />
        </div>

        <textarea
          style={s.csvBox}
          value={text}
          spellCheck={false}
          placeholder={"2026-06-01, https://youtube.com/watch?v=abc, HCL $50/100 NL - June 1, 420\n2026-06-02, https://youtube.com/watch?v=def, HCL $25/50 NL, 360"}
          onChange={(e) => setText(e.target.value)}
        />

        {result && (
          <div style={s.importResult}>
            Parsed {result.parsed} row{result.parsed === 1 ? "" : "s"} · added <strong style={{ color: "#4ade80" }}>{result.added}</strong>
            {result.skipped > 0 ? ` · skipped ${result.skipped} (duplicate/invalid)` : ""}
          </div>
        )}

        <div style={{ display: "flex", gap: 10 }}>
          <button style={s.saveBtn} disabled={!text.trim()} onClick={doImport}>Import</button>
          <button style={s.cancelBtn} onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}

// ── Styles ──────────────────────────────────────────────────────────────────
const s = {
  page: { flex: 1, minHeight: 0, overflowY: "auto", padding: "16px 20px 32px", color: "#e2e8f0", fontFamily: "'Inter','Segoe UI',system-ui,sans-serif" },

  // Stats banner
  banner: { display: "flex", flexWrap: "wrap", gap: 18, alignItems: "center", justifyContent: "space-between", background: "#0d1320", border: "1px solid #1e293b", borderRadius: 12, padding: "14px 20px", marginBottom: 16 },
  statGroup: { display: "flex", gap: 28, flexWrap: "wrap" },
  statBlock: { display: "flex", flexDirection: "column", gap: 3 },
  statLabel: { fontSize: 10, fontWeight: 700, letterSpacing: 1, textTransform: "uppercase", color: "#64748b" },
  statValue: { fontSize: 20, fontWeight: 800, color: "#f8fafc" },
  statPct: { fontSize: 13, fontWeight: 700, color: "#4ade80" },
  statSub: { fontSize: 13, fontWeight: 600, color: "#64748b" },
  progressWrap: { flex: "1 1 240px", minWidth: 200 },
  progressTrack: { height: 10, borderRadius: 6, background: "#0a0f1a", border: "1px solid #1e293b", overflow: "hidden" },
  progressFill: { height: "100%", background: "linear-gradient(90deg,#16a34a,#4ade80)", transition: "width .3s" },
  progressCaption: { fontSize: 11, color: "#94a3b8", marginTop: 4, textAlign: "right" },

  // Toolbar
  toolbar: { display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", justifyContent: "space-between", marginBottom: 12 },
  monthNav: { display: "flex", alignItems: "center", gap: 10 },
  navArrow: { width: 34, height: 34, borderRadius: 8, background: "#16243a", border: "1px solid #2b3a52", color: "#e2e8f0", fontSize: 20, lineHeight: 1, cursor: "pointer" },
  monthTitle: { fontSize: 18, fontWeight: 800, color: "#f8fafc", minWidth: 170, textAlign: "center" },
  yourMonth: { fontSize: 10.5, fontWeight: 800, letterSpacing: 0.5, color: "#0a0e17", background: "#f59e0b", borderRadius: 8, padding: "4px 10px" },
  ownedBy: { fontSize: 10.5, fontWeight: 700, color: "#cbd5e1", background: "#16243a", border: "1px solid #2b3a52", borderRadius: 8, padding: "4px 10px" },
  monthDone: { fontSize: 10, fontWeight: 800, color: "#06210f", background: "#16a34a", borderRadius: 8, padding: "3px 8px" },
  todayBtn: { padding: "7px 14px", borderRadius: 8, background: "#16243a", border: "1px solid #2b3a52", color: "#cbd5e1", fontSize: 12, fontWeight: 700, cursor: "pointer" },
  addBtn: { padding: "8px 14px", borderRadius: 8, background: "#16243a", border: "1px solid #2b3a52", color: "#7dd3fc", fontSize: 12.5, fontWeight: 700, cursor: "pointer" },
  importBtn: { padding: "8px 14px", borderRadius: 8, background: "linear-gradient(135deg,#f59e0b,#d97706)", border: "none", color: "#0a0e17", fontSize: 12.5, fontWeight: 800, cursor: "pointer" },

  // Grid
  weekRow: { display: "grid", gridTemplateColumns: "repeat(7,1fr)", gap: 6, marginBottom: 6 },
  weekCell: { textAlign: "center", fontSize: 11, fontWeight: 700, letterSpacing: 1, color: "#64748b", textTransform: "uppercase" },
  grid: { display: "grid", gridTemplateColumns: "repeat(7,1fr)", gridAutoRows: "minmax(108px, auto)", gap: 6 },
  cell: { background: "#0d1320", border: "1px solid #1e293b", borderRadius: 9, padding: 6, display: "flex", flexDirection: "column", gap: 4, cursor: "pointer", overflow: "hidden", transition: "border-color .12s" },
  cellOut: { background: "#0a0d15", opacity: 0.5 },
  cellToday: { borderColor: "#f59e0b", boxShadow: "0 0 0 1px #f59e0b55" },
  cellHead: { display: "flex", alignItems: "center", justifyContent: "space-between" },
  cellDay: { fontSize: 12, fontWeight: 700, color: "#94a3b8" },
  cellDayToday: { background: "#f59e0b", color: "#0a0e17", borderRadius: "50%", width: 20, height: 20, display: "inline-flex", alignItems: "center", justifyContent: "center" },
  cellMulti: { fontSize: 9, fontWeight: 800, color: "#0a0e17", background: "#7dd3fc", borderRadius: 8, padding: "1px 6px" },
  cellStreams: { display: "flex", flexDirection: "column", gap: 3, overflow: "hidden" },
  streamChip: { background: "#0e1626", borderRadius: 5, padding: "3px 5px", overflow: "hidden" },
  chipTitle: { fontSize: 10.5, fontWeight: 700, color: "#e2e8f0", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" },
  chipMeta: { display: "flex", alignItems: "center", gap: 4, fontSize: 9.5, color: "#94a3b8", marginTop: 1 },
  statusDot: { width: 7, height: 7, borderRadius: "50%", flexShrink: 0 },
  moreRow: { fontSize: 9.5, color: "#64748b", fontWeight: 600, paddingLeft: 2 },

  // Day drawer
  drawerOverlay: { position: "fixed", inset: 0, background: "rgba(3,6,12,.6)", zIndex: 120, display: "flex", justifyContent: "flex-end" },
  drawer: { width: 400, maxWidth: "100%", height: "100%", background: "#0d1320", borderLeft: "1px solid #1e293b", display: "flex", flexDirection: "column", boxShadow: "-12px 0 40px rgba(0,0,0,.5)" },
  drawerHead: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", padding: "16px 18px", borderBottom: "1px solid #1e293b" },
  drawerTitle: { fontSize: 16, fontWeight: 800, color: "#f8fafc" },
  drawerSub: { fontSize: 12, color: "#64748b", marginTop: 2 },
  closeBtn: { width: 30, height: 30, borderRadius: 7, background: "#1e293b", border: "1px solid #334155", color: "#cbd5e1", fontSize: 13, cursor: "pointer" },
  drawerBody: { flex: 1, overflowY: "auto", padding: 16, display: "flex", flexDirection: "column", gap: 12 },
  emptyDay: { fontSize: 13, color: "#64748b", textAlign: "center", padding: "12px 0" },

  streamCard: { background: "#0e1626", border: "1px solid #1e293b", borderRadius: 10, padding: 12 },
  streamCardTop: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 },
  statusBadge: { fontSize: 10, fontWeight: 800, letterSpacing: 0.5, color: "#0a0e17", borderRadius: 8, padding: "2px 8px" },
  miniX: { width: 22, height: 22, borderRadius: 6, background: "#1e293b", border: "1px solid #334155", color: "#94a3b8", fontSize: 11, cursor: "pointer" },
  streamTitle: { fontSize: 14, fontWeight: 700, color: "#f1f5f9", marginBottom: 3 },
  streamUrl: { display: "block", fontSize: 11, color: "#7dd3fc", textDecoration: "none", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", marginBottom: 8 },
  streamMetaRow: { display: "flex", justifyContent: "space-between", fontSize: 12, color: "#94a3b8", marginBottom: 8 },
  progressRow: { display: "flex", alignItems: "center", gap: 6, marginBottom: 10 },
  numInput: { width: 64, padding: "5px 7px", background: "#0a0f1a", border: "1px solid #334155", borderRadius: 6, color: "#e2e8f0", fontSize: 12, textAlign: "center", outline: "none" },
  doneCount: { fontSize: 14, fontWeight: 800, color: "#4ade80", minWidth: 28, textAlign: "center" },
  streamActions: { display: "flex", gap: 8 },
  completeBtn: { flex: 1, padding: "8px", borderRadius: 8, background: "#16243a", border: "1px solid #2b3a52", color: "#cbd5e1", fontSize: 12, fontWeight: 700, cursor: "pointer" },
  completeOn: { flex: 1, padding: "8px", borderRadius: 8, background: "#16a34a", border: "1px solid #16a34a", color: "#06210f", fontSize: 12, fontWeight: 800, cursor: "pointer" },
  openBtn: { flex: 1, padding: "8px", borderRadius: 8, background: "#0e7490", border: "1px solid #155e75", color: "#e0f2fe", fontSize: 12, fontWeight: 700, cursor: "pointer" },
  resumeBtn: { flex: 1, padding: "8px", borderRadius: 8, background: "linear-gradient(135deg,#f59e0b,#d97706)", border: "none", color: "#0a0e17", fontSize: 12, fontWeight: 800, cursor: "pointer" },
  downloadBtn: { display: "block", textAlign: "center", marginTop: 6, padding: "8px", borderRadius: 8, background: "#16a34a", color: "#06210f", fontSize: 12, fontWeight: 800, textDecoration: "none" },

  addForm: { background: "#0e1626", border: "1px dashed #334155", borderRadius: 10, padding: 12, display: "flex", flexDirection: "column", gap: 4 },
  addFormTitle: { fontSize: 12, fontWeight: 800, letterSpacing: 0.5, color: "#f59e0b", marginBottom: 4, textTransform: "uppercase" },
  fieldLabel: { fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, color: "#64748b" },
  textInput: { padding: "8px 10px", background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 7, color: "#e2e8f0", fontSize: 13, fontFamily: "inherit", outline: "none", boxSizing: "border-box", width: "100%", marginBottom: 4 },
  saveBtn: { flex: 1, padding: "9px", borderRadius: 8, background: "linear-gradient(135deg,#f59e0b,#d97706)", border: "none", color: "#0a0e17", fontSize: 13, fontWeight: 800, cursor: "pointer" },
  cancelBtn: { flex: 1, padding: "9px", borderRadius: 8, background: "#1e293b", border: "1px solid #334155", color: "#cbd5e1", fontSize: 13, fontWeight: 700, cursor: "pointer" },
  addAnother: { padding: "9px", borderRadius: 8, background: "#16243a", border: "1px solid #2b3a52", color: "#7dd3fc", fontSize: 12.5, fontWeight: 700, cursor: "pointer" },

  // Import modal
  modalOverlay: { position: "fixed", inset: 0, background: "rgba(3,6,12,.78)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 130, padding: 20 },
  modal: { width: 580, maxWidth: "100%", maxHeight: "90vh", overflowY: "auto", background: "#0d1320", border: "1px solid #1e293b", borderRadius: 14, padding: 22, display: "flex", flexDirection: "column", gap: 12, boxShadow: "0 30px 80px rgba(0,0,0,.6)" },
  modalHead: { display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 16, fontWeight: 800, color: "#f8fafc" },
  modalNote: { fontSize: 12, color: "#94a3b8", lineHeight: 1.6 },
  code: { background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 4, padding: "1px 5px", fontSize: 11, color: "#fbbf24", fontFamily: "'JetBrains Mono',monospace" },
  fileBtn: { padding: "8px 14px", borderRadius: 8, background: "#16243a", border: "1px solid #2b3a52", color: "#cbd5e1", fontSize: 12.5, fontWeight: 700, cursor: "pointer" },
  csvBox: { width: "100%", height: 180, boxSizing: "border-box", padding: 12, background: "#06090f", border: "1px solid #1e293b", borderRadius: 8, color: "#a5d6a7", fontSize: 12, fontFamily: "'JetBrains Mono',monospace", outline: "none", resize: "vertical" },
  importResult: { fontSize: 12.5, color: "#cbd5e1", background: "#0e1626", border: "1px solid #1e293b", borderRadius: 8, padding: "8px 12px" },
};
