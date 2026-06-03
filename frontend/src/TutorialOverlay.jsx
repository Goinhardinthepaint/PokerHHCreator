import { useState, useLayoutEffect, useRef } from "react";

// Lightweight custom product tour (no library). A dark spotlight overlay with a
// hole over the target element + an arrowed tooltip that positions itself to
// whichever side has room. The overlay is pointer-events:none, so the real UI
// underneath keeps working and outside clicks neither advance nor block — only
// Next / Back / Skip drive the tour.

const TOOLTIP_W = 304;
const GAP = 14;

function choosePlacement(r) {
  if (!r) return "center";
  const vw = window.innerWidth, vh = window.innerHeight;
  if (r.top + r.height + 210 < vh) return "below";
  if (r.top - 210 > 0) return "above";
  if (r.left + r.width + TOOLTIP_W + 40 < vw) return "right";
  if (r.left - TOOLTIP_W - 40 > 0) return "left";
  return "below";
}

// Returns { box: styleForTooltip, arrow: styleForArrow }.
function position(r, placement) {
  if (!r || placement === "center") {
    return { box: { top: "50%", left: "50%", transform: "translate(-50%,-50%)" }, arrow: null };
  }
  const vw = window.innerWidth;
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  if (placement === "below" || placement === "above") {
    const leftEdge = Math.min(Math.max(cx - TOOLTIP_W / 2, 8), vw - TOOLTIP_W - 8);
    const arrowX = Math.min(Math.max(cx - leftEdge, 18), TOOLTIP_W - 18);
    if (placement === "below") {
      return { box: { top: r.top + r.height + GAP, left: leftEdge }, arrow: { top: -7, left: arrowX - 7, borderWidth: "0 7px 8px 7px", borderColor: "transparent transparent #f59e0b transparent" } };
    }
    return { box: { top: r.top - GAP, left: leftEdge, transform: "translateY(-100%)" }, arrow: { bottom: -7, left: arrowX - 7, borderWidth: "8px 7px 0 7px", borderColor: "#f59e0b transparent transparent transparent" } };
  }
  // left / right — vertically centred on the target
  const top = Math.min(Math.max(cy, 130), window.innerHeight - 130);
  if (placement === "right") {
    return { box: { top, left: r.left + r.width + GAP, transform: "translateY(-50%)" }, arrow: { left: -7, top: "calc(50% - 7px)", borderWidth: "7px 8px 7px 0", borderColor: "transparent #f59e0b transparent transparent" } };
  }
  return { box: { top, left: r.left - GAP, transform: "translate(-100%,-50%)" }, arrow: { right: -7, top: "calc(50% - 7px)", borderWidth: "7px 0 7px 8px", borderColor: "transparent transparent transparent #f59e0b" } };
}

export default function TutorialOverlay({ steps, onFinish }) {
  const [i, setI] = useState(0);
  const [rect, setRect] = useState(null);
  const rectRef = useRef(null);
  const step = steps[i];

  useLayoutEffect(() => {
    let raf;
    const measure = () => {
      const el = step.selector ? document.querySelector(step.selector) : null;
      let r = null;
      if (el) {
        try { el.scrollIntoView({ block: "nearest", inline: "nearest" }); } catch { /* ignore */ }
        const b = el.getBoundingClientRect();
        if (b.width || b.height) r = { top: b.top, left: b.left, width: b.width, height: b.height };
      }
      const prev = rectRef.current;
      const same = (!r && !prev) || (r && prev && r.top === prev.top && r.left === prev.left && r.width === prev.width && r.height === prev.height);
      if (!same) { rectRef.current = r; setRect(r); }
    };
    raf = requestAnimationFrame(measure);                 // defer (avoids sync setState in effect)
    const id = setInterval(measure, 350);                 // track transitions / layout shifts
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => { cancelAnimationFrame(raf); clearInterval(id); window.removeEventListener("resize", measure); window.removeEventListener("scroll", measure, true); };
  }, [i, step.selector]);

  const placement = choosePlacement(rect);
  const pos = position(rect, placement);
  const last = i === steps.length - 1;
  const pad = 6;

  return (
    <div style={st.root}>
      {/* Spotlight (or a full dim layer when the target is missing) */}
      {rect ? (
        <div style={{
          ...st.spot,
          top: rect.top - pad, left: rect.left - pad,
          width: rect.width + pad * 2, height: rect.height + pad * 2,
        }} />
      ) : (
        <div style={st.dim} />
      )}

      {/* Tooltip */}
      <div style={{ ...st.box, ...pos.box }}>
        {pos.arrow && <div style={{ ...st.arrow, ...pos.arrow }} />}
        <div style={st.stepNo}>Step {i + 1} of {steps.length}</div>
        <div style={st.title}>{step.title}</div>
        <div style={st.desc}>{step.description}</div>
        <div style={st.actions}>
          {i > 0 && <button style={st.back} onClick={() => setI(i - 1)}>Back</button>}
          <span style={{ flex: 1 }} />
          <button style={st.next} onClick={() => (last ? onFinish() : setI(i + 1))}>{last ? "Done" : "Next"}</button>
        </div>
        <button style={st.skip} onClick={onFinish}>Skip Tutorial</button>
      </div>
    </div>
  );
}

const st = {
  root: { position: "fixed", inset: 0, zIndex: 1000, pointerEvents: "none", fontFamily: "'Inter','Segoe UI',system-ui,sans-serif" },
  spot: { position: "fixed", borderRadius: 10, border: "3px solid #f59e0b", boxShadow: "0 0 0 9999px rgba(3,6,12,.66), 0 0 22px 4px rgba(245,158,11,.6)", transition: "all .25s cubic-bezier(.4,0,.2,1)", pointerEvents: "none" },
  dim: { position: "fixed", inset: 0, background: "rgba(3,6,12,.66)", pointerEvents: "none" },
  box: { position: "fixed", width: TOOLTIP_W, boxSizing: "border-box", background: "#0e1626", border: "1px solid #f59e0b", borderRadius: 12, padding: "14px 16px 12px", boxShadow: "0 20px 60px rgba(0,0,0,.6)", pointerEvents: "auto", transition: "top .25s, left .25s", color: "#e2e8f0" },
  arrow: { position: "absolute", width: 0, height: 0, borderStyle: "solid" },
  stepNo: { fontSize: 10.5, fontWeight: 800, letterSpacing: 1, textTransform: "uppercase", color: "#f59e0b", marginBottom: 4 },
  title: { fontSize: 15, fontWeight: 800, color: "#f8fafc", marginBottom: 5 },
  desc: { fontSize: 12.5, lineHeight: 1.5, color: "#cbd5e1" },
  actions: { display: "flex", alignItems: "center", gap: 8, marginTop: 12 },
  back: { padding: "6px 12px", background: "#1e293b", border: "1px solid #334155", borderRadius: 7, color: "#cbd5e1", fontSize: 12, fontWeight: 700, cursor: "pointer" },
  next: { padding: "7px 18px", background: "linear-gradient(135deg,#f59e0b,#d97706)", border: "none", borderRadius: 7, color: "#0a0e17", fontSize: 12.5, fontWeight: 800, cursor: "pointer" },
  skip: { display: "block", margin: "8px auto 0", background: "none", border: "none", color: "#64748b", fontSize: 11, fontWeight: 600, cursor: "pointer", textDecoration: "underline" },
};
