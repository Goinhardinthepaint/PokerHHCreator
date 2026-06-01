import { useState, useEffect, useRef } from "react";

const PHASE_LABELS = {
  idle: "Ready",
  downloading: "Downloading Video",
  extracting: "Extracting Frames",
  analyzing: "Analyzing Frames",
  detecting: "Detecting Hands",
  formatting: "Formatting for PT4",
  complete: "Complete",
  error: "Error",
};

const OVERLAY_PROFILES = [
  { id: "hustler_wpt", name: "Hustler Casino Live (WPT)" },
  { id: "pokerstars", name: "PokerGO" },
  { id: "stones", name: "Stones Live" },
  { id: "latb", name: "Live at the Bike" },
  { id: "custom", name: "Custom Overlay" },
];

function PokerScraper() {
  const [url, setUrl] = useState("");
  const [bbAnte, setBbAnte] = useState(100);
  const [stakes, setStakes] = useState("50/100");
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [fps, setFps] = useState(1);
  const [overlay, setOverlay] = useState("hustler_wpt");
  const [visionModel, setVisionModel] = useState("claude");
  const [phase, setPhase] = useState("idle");
  const [progress, setProgress] = useState(0);
  const [totalFrames, setTotalFrames] = useState(0);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [handsFound, setHandsFound] = useState(0);
  const [handsSkipped, setHandsSkipped] = useState(0);
  const [logs, setLogs] = useState([]);
  const [results, setResults] = useState(null);
  const [estimatedCost, setEstimatedCost] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [isRunning, setIsRunning] = useState(false);
  const logEndRef = useRef(null);
  const timerRef = useRef(null);

  // Auto-scroll logs
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  // Timer
  useEffect(() => {
    if (isRunning) {
      timerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000);
    } else {
      clearInterval(timerRef.current);
    }
    return () => clearInterval(timerRef.current);
  }, [isRunning]);

  // Cost estimation
  useEffect(() => {
    if (!startTime || !endTime) {
      setEstimatedCost(0);
      return;
    }
    const start = parseTimeToSeconds(startTime);
    const end = parseTimeToSeconds(endTime);
    if (end > start) {
      const frames = (end - start) * fps;
      const costPerFrame = visionModel === "claude" ? 0.015 : 0.002;
      setEstimatedCost(frames * costPerFrame);
    }
  }, [startTime, endTime, fps, visionModel]);

  function parseTimeToSeconds(timeStr) {
    if (!timeStr) return 0;
    // Accept "33:20" or "2000" or "1:05:30"
    if (/^\d+$/.test(timeStr)) return parseInt(timeStr);
    const parts = timeStr.split(":").map(Number);
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    return 0;
  }

  function formatTime(secs) {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function addLog(msg, type = "info") {
    setLogs((prev) => [...prev.slice(-200), { msg, type, time: new Date().toLocaleTimeString() }]);
  }

  async function handleStart() {
    if (!url.trim()) return;
    setIsRunning(true);
    setElapsed(0);
    setPhase("downloading");
    setProgress(0);
    setHandsFound(0);
    setHandsSkipped(0);
    setResults(null);
    setLogs([]);
    addLog("Starting pipeline...");

    const params = {
      url: url.trim(),
      bb_ante: bbAnte,
      fps,
      start_sec: startTime ? parseTimeToSeconds(startTime) : null,
      end_sec: endTime ? parseTimeToSeconds(endTime) : null,
      vision_model: visionModel,
    };

    try {
      const resp = await fetch("http://localhost:8000/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
      });

      if (!resp.ok) throw new Error(`Server error: ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const event = JSON.parse(line);
            handleEvent(event);
          } catch {}
        }
      }
    } catch (err) {
      setPhase("error");
      addLog(`Error: ${err.message}`, "error");
    } finally {
      setIsRunning(false);
    }
  }

  function handleEvent(event) {
    switch (event.type) {
      case "phase":
        setPhase(event.phase);
        addLog(PHASE_LABELS[event.phase] || event.phase);
        break;
      case "progress":
        setCurrentFrame(event.current);
        setTotalFrames(event.total);
        setProgress((event.current / event.total) * 100);
        if (event.current % 20 === 0) {
          addLog(`Frame ${event.current}/${event.total} — ${event.detail || ""}`);
        }
        break;
      case "hand_detected":
        setHandsFound((h) => h + 1);
        addLog(`Hand detected: ${event.players || "?"} players, pot $${event.pot || "?"}`, "success");
        break;
      case "hand_skipped":
        setHandsSkipped((s) => s + 1);
        addLog(`Skipped: ${event.reason}`, "warn");
        break;
      case "complete":
        setPhase("complete");
        setResults(event);
        addLog(`Done! ${event.hands_exported} hands exported.`, "success");
        break;
      case "log":
        addLog(event.message, event.level || "info");
        break;
      case "error":
        setPhase("error");
        addLog(event.message, "error");
        break;
    }
  }

  function handleStop() {
    fetch("http://localhost:8000/api/stop", { method: "POST" });
    setIsRunning(false);
    setPhase("idle");
    addLog("Pipeline stopped by user.", "warn");
  }

  const progressPct = Math.min(100, progress);
  const isIdle = phase === "idle" || phase === "complete" || phase === "error";

  return (
    <div style={styles.container}>
      {/* Background grain */}
      <div style={styles.grain} />

      {/* Header */}
      <header style={styles.header}>
        <div style={styles.logoRow}>
          <div style={styles.chip}>♠</div>
          <h1 style={styles.title}>HAND SCRAPER</h1>
        </div>
        <p style={styles.subtitle}>YouTube Livestream → PokerTracker 4</p>
      </header>

      <div style={styles.main}>
        {/* Left Panel — Config */}
        <div style={styles.configPanel}>
          <div style={styles.section}>
            <label style={styles.label}>YouTube URL</label>
            <input
              style={styles.inputFull}
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://youtube.com/watch?v=..."
              disabled={!isIdle}
            />
          </div>

          <div style={styles.row}>
            <div style={styles.half}>
              <label style={styles.label}>Start Time</label>
              <input
                style={styles.input}
                value={startTime}
                onChange={(e) => setStartTime(e.target.value)}
                placeholder="33:20 or 2000"
                disabled={!isIdle}
              />
            </div>
            <div style={styles.half}>
              <label style={styles.label}>End Time</label>
              <input
                style={styles.input}
                value={endTime}
                onChange={(e) => setEndTime(e.target.value)}
                placeholder="1:08:20 or 4100"
                disabled={!isIdle}
              />
            </div>
          </div>

          <div style={styles.row}>
            <div style={styles.third}>
              <label style={styles.label}>Stakes</label>
              <select style={styles.select} value={stakes} onChange={(e) => setStakes(e.target.value)} disabled={!isIdle}>
                <option value="1/2">$1/$2</option>
                <option value="2/5">$2/$5</option>
                <option value="5/10">$5/$10</option>
                <option value="10/20">$10/$20</option>
                <option value="25/50">$25/$50</option>
                <option value="50/100">$50/$100</option>
                <option value="100/200">$100/$200</option>
                <option value="200/400">$200/$400</option>
                <option value="500/1000">$500/$1K</option>
              </select>
            </div>
            <div style={styles.third}>
              <label style={styles.label}>BB Ante</label>
              <input
                style={styles.input}
                type="number"
                value={bbAnte}
                onChange={(e) => setBbAnte(parseInt(e.target.value) || 0)}
                disabled={!isIdle}
              />
            </div>
            <div style={styles.third}>
              <label style={styles.label}>FPS</label>
              <select style={styles.select} value={fps} onChange={(e) => setFps(parseFloat(e.target.value))} disabled={!isIdle}>
                <option value={0.5}>0.5</option>
                <option value={1}>1</option>
                <option value={2}>2</option>
              </select>
            </div>
          </div>

          <div style={styles.row}>
            <div style={styles.half}>
              <label style={styles.label}>Overlay Profile</label>
              <select style={styles.select} value={overlay} onChange={(e) => setOverlay(e.target.value)} disabled={!isIdle}>
                {OVERLAY_PROFILES.map((p) => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>
            </div>
            <div style={styles.half}>
              <label style={styles.label}>Vision Model</label>
              <select style={styles.select} value={visionModel} onChange={(e) => setVisionModel(e.target.value)} disabled={!isIdle}>
                <option value="claude">Claude Sonnet ($$$)</option>
                <option value="openai">GPT-4o ($$$)</option>
                <option value="openai-mini">GPT-4o mini ($)</option>
              </select>
            </div>
          </div>

          {estimatedCost > 0 && (
            <div style={styles.costEstimate}>
              Est. cost: <strong>${estimatedCost.toFixed(2)}</strong> · ~{Math.ceil(estimatedCost / (visionModel === "claude" ? 0.015 : 0.002))} frames
            </div>
          )}

          <div style={styles.buttonRow}>
            {isIdle ? (
              <button style={styles.startBtn} onClick={handleStart} disabled={!url.trim()}>
                ▶ START SCRAPING
              </button>
            ) : (
              <button style={styles.stopBtn} onClick={handleStop}>
                ■ STOP
              </button>
            )}
          </div>
        </div>

        {/* Right Panel — Status */}
        <div style={styles.statusPanel}>
          {/* Progress */}
          <div style={styles.progressSection}>
            <div style={styles.phaseRow}>
              <span style={styles.phaseLabel}>{PHASE_LABELS[phase]}</span>
              <span style={styles.elapsed}>{formatTime(elapsed)}</span>
            </div>
            <div style={styles.progressBar}>
              <div
                style={{
                  ...styles.progressFill,
                  width: `${progressPct}%`,
                  backgroundColor: phase === "error" ? "#ef4444" : phase === "complete" ? "#22c55e" : "#f59e0b",
                }}
              />
            </div>
            {totalFrames > 0 && (
              <div style={styles.frameCount}>
                Frame {currentFrame} / {totalFrames}
              </div>
            )}
          </div>

          {/* Stats Cards */}
          <div style={styles.statsRow}>
            <div style={styles.statCard}>
              <div style={styles.statValue}>{handsFound}</div>
              <div style={styles.statLabel}>Hands Found</div>
            </div>
            <div style={styles.statCard}>
              <div style={styles.statValue}>{handsSkipped}</div>
              <div style={styles.statLabel}>Skipped</div>
            </div>
            <div style={{ ...styles.statCard, borderColor: "#22c55e33" }}>
              <div style={{ ...styles.statValue, color: "#22c55e" }}>{handsFound - handsSkipped}</div>
              <div style={styles.statLabel}>Exportable</div>
            </div>
          </div>

          {/* Download */}
          {phase === "complete" && results && (
            <div style={styles.downloadSection}>
              <button
                style={styles.downloadBtn}
                onClick={() => window.open("http://localhost:8000/api/download", "_blank")}
              >
                ↓ DOWNLOAD HAND HISTORIES
              </button>
              <p style={styles.downloadHint}>
                {results.hands_exported} hands · Import into PokerTracker 4 via<br />
                Play Poker → Get Hands From Disk
              </p>
            </div>
          )}

          {/* Log */}
          <div style={styles.logSection}>
            <div style={styles.logHeader}>Activity Log</div>
            <div style={styles.logBox}>
              {logs.map((l, i) => (
                <div key={i} style={{ ...styles.logLine, color: l.type === "error" ? "#ef4444" : l.type === "success" ? "#22c55e" : l.type === "warn" ? "#f59e0b" : "#94a3b8" }}>
                  <span style={styles.logTime}>{l.time}</span> {l.msg}
                </div>
              ))}
              <div ref={logEndRef} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

const styles = {
  container: {
    minHeight: "100vh",
    backgroundColor: "#0a0e17",
    color: "#e2e8f0",
    fontFamily: "'JetBrains Mono', 'Fira Code', 'SF Mono', monospace",
    position: "relative",
    overflow: "hidden",
  },
  grain: {
    position: "fixed",
    inset: 0,
    opacity: 0.03,
    backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")`,
    pointerEvents: "none",
    zIndex: 0,
  },
  header: {
    padding: "32px 40px 20px",
    borderBottom: "1px solid #1e293b",
    position: "relative",
    zIndex: 1,
  },
  logoRow: {
    display: "flex",
    alignItems: "center",
    gap: "14px",
  },
  chip: {
    width: "42px",
    height: "42px",
    borderRadius: "50%",
    background: "linear-gradient(135deg, #f59e0b, #d97706)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: "22px",
    color: "#0a0e17",
    fontWeight: "bold",
    boxShadow: "0 0 20px #f59e0b44",
  },
  title: {
    fontSize: "28px",
    fontWeight: 800,
    letterSpacing: "3px",
    color: "#f8fafc",
    margin: 0,
  },
  subtitle: {
    fontSize: "13px",
    color: "#64748b",
    marginTop: "6px",
    marginLeft: "56px",
    letterSpacing: "0.5px",
  },
  main: {
    display: "flex",
    gap: "24px",
    padding: "24px 40px",
    position: "relative",
    zIndex: 1,
    minHeight: "calc(100vh - 120px)",
  },
  configPanel: {
    width: "380px",
    flexShrink: 0,
    display: "flex",
    flexDirection: "column",
    gap: "16px",
  },
  statusPanel: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    gap: "16px",
  },
  section: { display: "flex", flexDirection: "column", gap: "6px" },
  row: { display: "flex", gap: "12px" },
  half: { flex: 1, display: "flex", flexDirection: "column", gap: "6px" },
  third: { flex: 1, display: "flex", flexDirection: "column", gap: "6px" },
  label: {
    fontSize: "11px",
    fontWeight: 600,
    textTransform: "uppercase",
    letterSpacing: "1.2px",
    color: "#64748b",
  },
  inputFull: {
    width: "100%",
    padding: "10px 14px",
    backgroundColor: "#111827",
    border: "1px solid #1e293b",
    borderRadius: "8px",
    color: "#e2e8f0",
    fontSize: "14px",
    fontFamily: "inherit",
    outline: "none",
    boxSizing: "border-box",
    transition: "border-color 0.15s",
  },
  input: {
    width: "100%",
    padding: "10px 14px",
    backgroundColor: "#111827",
    border: "1px solid #1e293b",
    borderRadius: "8px",
    color: "#e2e8f0",
    fontSize: "14px",
    fontFamily: "inherit",
    outline: "none",
    boxSizing: "border-box",
  },
  select: {
    width: "100%",
    padding: "10px 14px",
    backgroundColor: "#111827",
    border: "1px solid #1e293b",
    borderRadius: "8px",
    color: "#e2e8f0",
    fontSize: "14px",
    fontFamily: "inherit",
    outline: "none",
    boxSizing: "border-box",
    cursor: "pointer",
  },
  costEstimate: {
    padding: "10px 14px",
    backgroundColor: "#f59e0b11",
    border: "1px solid #f59e0b33",
    borderRadius: "8px",
    fontSize: "13px",
    color: "#f59e0b",
    textAlign: "center",
  },
  buttonRow: {
    marginTop: "8px",
  },
  startBtn: {
    width: "100%",
    padding: "14px",
    backgroundColor: "#f59e0b",
    color: "#0a0e17",
    border: "none",
    borderRadius: "10px",
    fontSize: "15px",
    fontWeight: 800,
    fontFamily: "inherit",
    letterSpacing: "2px",
    cursor: "pointer",
    transition: "all 0.15s",
    boxShadow: "0 0 30px #f59e0b33",
  },
  stopBtn: {
    width: "100%",
    padding: "14px",
    backgroundColor: "#ef4444",
    color: "#fff",
    border: "none",
    borderRadius: "10px",
    fontSize: "15px",
    fontWeight: 800,
    fontFamily: "inherit",
    letterSpacing: "2px",
    cursor: "pointer",
  },
  progressSection: {
    padding: "20px",
    backgroundColor: "#111827",
    border: "1px solid #1e293b",
    borderRadius: "12px",
  },
  phaseRow: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: "12px",
  },
  phaseLabel: {
    fontSize: "14px",
    fontWeight: 700,
    color: "#f8fafc",
    letterSpacing: "0.5px",
  },
  elapsed: {
    fontSize: "13px",
    color: "#64748b",
    fontVariantNumeric: "tabular-nums",
  },
  progressBar: {
    height: "8px",
    backgroundColor: "#1e293b",
    borderRadius: "4px",
    overflow: "hidden",
  },
  progressFill: {
    height: "100%",
    borderRadius: "4px",
    transition: "width 0.3s ease-out",
  },
  frameCount: {
    fontSize: "12px",
    color: "#475569",
    marginTop: "8px",
    textAlign: "right",
    fontVariantNumeric: "tabular-nums",
  },
  statsRow: {
    display: "flex",
    gap: "12px",
  },
  statCard: {
    flex: 1,
    padding: "16px",
    backgroundColor: "#111827",
    border: "1px solid #1e293b",
    borderRadius: "12px",
    textAlign: "center",
  },
  statValue: {
    fontSize: "32px",
    fontWeight: 800,
    color: "#f59e0b",
    fontVariantNumeric: "tabular-nums",
  },
  statLabel: {
    fontSize: "11px",
    color: "#64748b",
    textTransform: "uppercase",
    letterSpacing: "1px",
    marginTop: "4px",
  },
  downloadSection: {
    padding: "20px",
    backgroundColor: "#22c55e11",
    border: "1px solid #22c55e33",
    borderRadius: "12px",
    textAlign: "center",
  },
  downloadBtn: {
    padding: "14px 32px",
    backgroundColor: "#22c55e",
    color: "#0a0e17",
    border: "none",
    borderRadius: "10px",
    fontSize: "15px",
    fontWeight: 800,
    fontFamily: "inherit",
    letterSpacing: "2px",
    cursor: "pointer",
    boxShadow: "0 0 30px #22c55e33",
  },
  downloadHint: {
    fontSize: "12px",
    color: "#64748b",
    marginTop: "12px",
    lineHeight: "1.5",
  },
  logSection: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    minHeight: "200px",
  },
  logHeader: {
    fontSize: "11px",
    fontWeight: 600,
    textTransform: "uppercase",
    letterSpacing: "1.2px",
    color: "#475569",
    marginBottom: "8px",
  },
  logBox: {
    flex: 1,
    backgroundColor: "#0f1520",
    border: "1px solid #1e293b",
    borderRadius: "10px",
    padding: "12px 16px",
    overflowY: "auto",
    maxHeight: "300px",
    fontSize: "12px",
    lineHeight: "1.7",
  },
  logLine: {
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
  },
  logTime: {
    color: "#334155",
    marginRight: "8px",
  },
};

export default PokerScraper;
