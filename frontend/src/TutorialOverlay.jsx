import { useState, useLayoutEffect, useRef } from "react";

// Lightweight custom product tour (no library). A dark spotlight overlay with a
// hole over the target element + an arrowed tooltip that positions itself to
// whichever side has room. The overlay is pointer-events:none, so the real UI
// underneath keeps working and outside clicks neither advance nor block — only
// Next / Back / Skip drive the tour.

const TOOLTIP_W = 304;
const GAP = 14;

function choosePlacement(r, W) {
  if (!r) return "center";
  const vw = window.innerWidth, vh = window.innerHeight;
  if (r.top + r.height + 240 < vh) return "below";
  if (r.top - 240 > 0) return "above";
  if (r.left + r.width + W + 40 < vw) return "right";
  if (r.left - W - 40 > 0) return "left";
  return "below";
}

// Returns { box: styleForTooltip, arrow: styleForArrow }.
function position(r, placement, W) {
  if (!r || placement === "center") {
    return { box: { top: "50%", left: "50%", transform: "translate(-50%,-50%)" }, arrow: null };
  }
  const vw = window.innerWidth;
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  if (placement === "below" || placement === "above") {
    const leftEdge = Math.min(Math.max(cx - W / 2, 8), vw - W - 8);
    const arrowX = Math.min(Math.max(cx - leftEdge, 18), W - 18);
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

export default function TutorialOverlay({ steps, onFinish, bonusAwarded }) {
  const [i, setI] = useState(0);
  const [rect, setRect] = useState(null);
  const [slide, setSlide] = useState(0);
  const rectRef = useRef(null);
  const step = steps[i];
  const W = step.wide ? 400 : TOOLTIP_W;

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

  const placement = choosePlacement(rect, W);
  const pos = position(rect, placement, W);
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
      <div style={{ ...st.box, ...pos.box, width: W }}>
        {pos.arrow && <div style={{ ...st.arrow, ...pos.arrow }} />}
        <div style={st.stepNo}>Step {i + 1} of {steps.length}</div>
        <div style={st.title}>{step.final ? (bonusAwarded ? "🎉 Tutorial complete!" : "🎉 Tutorial complete!") : step.title}</div>

        {step.howto ? (
          <>
            <div style={st.slideFrame}>{SLIDES[slide]}</div>
            <div style={st.slideNav}>
              <button style={st.slideArrow} disabled={slide === 0} onClick={() => setSlide((s) => Math.max(0, s - 1))}>‹</button>
              <div style={st.dots}>
                {SLIDES.map((_, k) => (
                  <span key={k} style={{ ...st.dot, ...(k === slide ? st.dotOn : {}) }} onClick={() => setSlide(k)} />
                ))}
              </div>
              <button style={st.slideArrow} disabled={slide === SLIDES.length - 1} onClick={() => setSlide((s) => Math.min(SLIDES.length - 1, s + 1))}>›</button>
            </div>
            <div style={st.desc}>{step.description || "Pause the video at the start of each hand, click Share, check 'Start at', and paste the link."}</div>
          </>
        ) : step.final ? (
          <div style={{ ...st.desc, fontSize: 13.5 }}>
            {bonusAwarded
              ? "Tutorial complete! You're all caught up."
              : "Tutorial complete! $1.00 has been added to your balance. You're ready to start transcribing!"}
          </div>
        ) : (
          <div style={st.desc}>{step.description}</div>
        )}

        <div style={st.actions}>
          {i > 0 && <button style={st.back} onClick={() => setI(i - 1)}>Back</button>}
          <span style={{ flex: 1 }} />
          <button style={st.next} onClick={() => (last ? onFinish(true) : setI(i + 1))}>{last ? "Finish" : "Next"}</button>
        </div>
        {!step.final && <button style={st.skip} onClick={() => onFinish(false)}>Skip Tutorial</button>}
      </div>
    </div>
  );
}

// Schematic SVG mocks (no real screenshots) for the "how to get a timestamped
// link" slideshow.
const SLIDES = [
  (
    <svg viewBox="0 0 372 150" width="100%" style={{ display: "block" }}>
      <rect x="6" y="6" width="360" height="108" rx="8" fill="#0f0f0f" stroke="#333" />
      <rect x="18" y="90" width="336" height="4" rx="2" fill="#555" />
      <rect x="18" y="90" width="150" height="4" rx="2" fill="#ff0000" />
      <circle cx="168" cy="92" r="6" fill="#ff0000" />
      <path d="M20 101 l0 14 l12 -7 z" fill="#fff" />
      <text x="42" y="113" fill="#ddd" fontSize="9" fontFamily="sans-serif">44:19 / 1:23:45</text>
      <rect x="280" y="101" width="62" height="16" rx="4" fill="#272727" />
      <text x="289" y="113" fill="#fff" fontSize="9" fontWeight="700" fontFamily="sans-serif">↪ Share</text>
      <ellipse cx="311" cy="109" rx="42" ry="15" fill="none" stroke="#ff2d2d" strokeWidth="2.5" />
      <path d="M236 64 q44 6 64 30" fill="none" stroke="#ff2d2d" strokeWidth="2.5" />
      <path d="M298 92 l4 -13 l-12 5 z" fill="#ff2d2d" />
      <text x="118" y="140" fill="#f59e0b" fontSize="12.5" fontWeight="800" fontFamily="sans-serif">1. Click Share</text>
    </svg>
  ),
  (
    <svg viewBox="0 0 372 150" width="100%" style={{ display: "block" }}>
      <rect x="36" y="4" width="300" height="120" rx="10" fill="#212121" stroke="#3a3a3a" />
      <text x="52" y="24" fill="#fff" fontSize="11" fontWeight="700" fontFamily="sans-serif">Share</text>
      <rect x="52" y="60" width="196" height="22" rx="5" fill="#121212" stroke="#444" />
      <text x="60" y="75" fill="#9ca3af" fontSize="9" fontFamily="monospace">youtu.be/…?t=2659</text>
      <rect x="256" y="60" width="60" height="22" rx="5" fill="#3ea6ff" />
      <text x="270" y="75" fill="#0a0e17" fontSize="10" fontWeight="800" fontFamily="sans-serif">Copy</text>
      <rect x="254" y="58" width="64" height="26" rx="6" fill="none" stroke="#ff2d2d" strokeWidth="2.2" />
      <rect x="52" y="94" width="14" height="14" rx="3" fill="#3ea6ff" />
      <path d="M55 101 l3 4 l6 -8" fill="none" stroke="#0a0e17" strokeWidth="2" />
      <text x="74" y="105" fill="#fff" fontSize="10" fontFamily="sans-serif">Start at  44:19</text>
      <rect x="48" y="92" width="124" height="18" rx="5" fill="none" stroke="#ff2d2d" strokeWidth="2.2" />
      <text x="40" y="142" fill="#f59e0b" fontSize="11" fontWeight="800" fontFamily="sans-serif">2. Check &apos;Start at&apos; then click Copy</text>
    </svg>
  ),
];

const st = {
  root: { position: "fixed", inset: 0, zIndex: 1000, pointerEvents: "none", fontFamily: "'Inter','Segoe UI',system-ui,sans-serif" },
  spot: { position: "fixed", borderRadius: 10, border: "3px solid #f59e0b", boxShadow: "0 0 0 9999px rgba(3,6,12,.66), 0 0 22px 4px rgba(245,158,11,.6)", transition: "all .25s cubic-bezier(.4,0,.2,1)", pointerEvents: "none" },
  dim: { position: "fixed", inset: 0, background: "rgba(3,6,12,.66)", pointerEvents: "none" },
  box: { position: "fixed", width: TOOLTIP_W, boxSizing: "border-box", background: "#0e1626", border: "1px solid #f59e0b", borderRadius: 12, padding: "14px 16px 12px", boxShadow: "0 20px 60px rgba(0,0,0,.6)", pointerEvents: "auto", transition: "top .25s, left .25s", color: "#e2e8f0" },
  arrow: { position: "absolute", width: 0, height: 0, borderStyle: "solid" },
  stepNo: { fontSize: 10.5, fontWeight: 800, letterSpacing: 1, textTransform: "uppercase", color: "#f59e0b", marginBottom: 4 },
  title: { fontSize: 15, fontWeight: 800, color: "#f8fafc", marginBottom: 5 },
  desc: { fontSize: 12.5, lineHeight: 1.5, color: "#cbd5e1" },
  slideFrame: { background: "#0a0f1a", border: "1px solid #1e293b", borderRadius: 8, padding: 8, marginBottom: 6 },
  slideNav: { display: "flex", alignItems: "center", justifyContent: "center", gap: 10, marginBottom: 8 },
  slideArrow: { width: 24, height: 24, borderRadius: 6, background: "#16243a", border: "1px solid #2b3a52", color: "#cbd5e1", fontSize: 14, fontWeight: 800, cursor: "pointer", lineHeight: 1 },
  dots: { display: "flex", gap: 6 },
  dot: { width: 8, height: 8, borderRadius: "50%", background: "#334155", cursor: "pointer" },
  dotOn: { background: "#f59e0b" },
  actions: { display: "flex", alignItems: "center", gap: 8, marginTop: 12 },
  back: { padding: "6px 12px", background: "#1e293b", border: "1px solid #334155", borderRadius: 7, color: "#cbd5e1", fontSize: 12, fontWeight: 700, cursor: "pointer" },
  next: { padding: "7px 18px", background: "linear-gradient(135deg,#f59e0b,#d97706)", border: "none", borderRadius: 7, color: "#0a0e17", fontSize: 12.5, fontWeight: 800, cursor: "pointer" },
  skip: { display: "block", margin: "8px auto 0", background: "none", border: "none", color: "#64748b", fontSize: 11, fontWeight: 600, cursor: "pointer", textDecoration: "underline" },
};
