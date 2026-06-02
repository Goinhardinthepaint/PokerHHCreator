import { useState } from "react";
import { api } from "./api.js";

// Login / Register — the default screen until a session exists. On success it
// hands the { user, dashboard } payload up so the app can render authed.
export default function Auth({ onAuthed }) {
  const [mode, setMode] = useState("login"); // "login" | "register"
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const path = mode === "login" ? "/api/login" : "/api/register";
      const body = mode === "login"
        ? { username, password }
        : { username, email, password };
      const payload = await api(path, { method: "POST", body });
      onAuthed(payload);
    } catch (ex) {
      setError(ex.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={s.page}>
      <form style={s.card} onSubmit={submit}>
        <div style={s.brand}><span style={s.chip}>♠</span> POKER SUITE</div>
        <div style={s.tabs}>
          <button type="button" style={{ ...s.tab, ...(mode === "login" ? s.tabOn : {}) }} onClick={() => { setMode("login"); setError(""); }}>Log in</button>
          <button type="button" style={{ ...s.tab, ...(mode === "register" ? s.tabOn : {}) }} onClick={() => { setMode("register"); setError(""); }}>Register</button>
        </div>

        <label style={s.label}>Username</label>
        <input style={s.input} value={username} autoFocus autoComplete="username" onChange={(e) => setUsername(e.target.value)} />

        {mode === "register" && (
          <>
            <label style={s.label}>Email</label>
            <input style={s.input} type="email" value={email} autoComplete="email" onChange={(e) => setEmail(e.target.value)} />
          </>
        )}

        <label style={s.label}>Password</label>
        <input style={s.input} type="password" value={password} autoComplete={mode === "login" ? "current-password" : "new-password"} onChange={(e) => setPassword(e.target.value)} />

        {error && <div style={s.error}>⚠ {error}</div>}

        <button style={s.submit} disabled={busy || !username || !password}>
          {busy ? "…" : mode === "login" ? "Log in" : "Create account"}
        </button>
        <div style={s.hint}>
          {mode === "login" ? "New worker? " : "Already have an account? "}
          <button type="button" style={s.linkBtn} onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>
            {mode === "login" ? "Register" : "Log in"}
          </button>
        </div>
      </form>
    </div>
  );
}

const s = {
  page: { minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "#070b12", color: "#e2e8f0", fontFamily: "'Inter','Segoe UI',system-ui,sans-serif" },
  card: { width: 360, maxWidth: "92vw", background: "#0d1320", border: "1px solid #1e293b", borderRadius: 14, padding: 26, display: "flex", flexDirection: "column", gap: 6, boxShadow: "0 30px 80px rgba(0,0,0,.6)" },
  brand: { display: "flex", alignItems: "center", gap: 10, fontWeight: 800, letterSpacing: 1.5, fontSize: 18, justifyContent: "center", marginBottom: 8 },
  chip: { display: "inline-flex", width: 28, height: 28, borderRadius: "50%", background: "linear-gradient(135deg,#f59e0b,#d97706)", color: "#0a0e17", alignItems: "center", justifyContent: "center", fontSize: 16 },
  tabs: { display: "flex", gap: 6, marginBottom: 10, background: "#0a0f1a", borderRadius: 9, padding: 4 },
  tab: { flex: 1, padding: "8px", background: "transparent", border: "none", borderRadius: 7, color: "#94a3b8", fontSize: 13, fontWeight: 700, cursor: "pointer" },
  tabOn: { background: "#16243a", color: "#f8fafc" },
  label: { fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: 1, color: "#64748b", marginTop: 6 },
  input: { padding: "9px 11px", background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 8, color: "#e2e8f0", fontSize: 14, fontFamily: "inherit", outline: "none" },
  error: { marginTop: 8, fontSize: 12.5, color: "#fca5a5", background: "#3a1416", border: "1px solid #7f1d1d", borderRadius: 8, padding: "8px 10px" },
  submit: { marginTop: 14, padding: "11px", background: "linear-gradient(135deg,#f59e0b,#d97706)", border: "none", borderRadius: 9, color: "#0a0e17", fontSize: 14, fontWeight: 800, letterSpacing: 0.5, cursor: "pointer" },
  hint: { fontSize: 12, color: "#64748b", textAlign: "center", marginTop: 10 },
  linkBtn: { background: "none", border: "none", color: "#7dd3fc", fontSize: 12, fontWeight: 700, cursor: "pointer", padding: 0 },
};
