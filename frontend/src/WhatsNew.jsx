// One-time "What's new" popup, shared by both UIs. In the original UI it asks
// whether to try the new look; in both it explains the reworked side games and
// reminds workers that marking streams complete is what pays the stream bonus.
// The host passes its look in `ui`; anything left out uses the classic styling.

const CLASSIC = {
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,.65)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1100, padding: 16 },
  box: { width: 520, maxWidth: "94vw", maxHeight: "90vh", overflowY: "auto", boxSizing: "border-box", background: "#0e1626", border: "1px solid #334155", borderRadius: 14, padding: 24, boxShadow: "0 30px 80px rgba(0,0,0,.6)", display: "flex", flexDirection: "column", gap: 14, color: "#e2e8f0" },
  title: { fontSize: 19, fontWeight: 800, color: "#fbbf24", letterSpacing: 0.3 },
  section: { display: "flex", flexDirection: "column", gap: 6, padding: "12px 14px", background: "#0d1320", border: "1px solid #1e293b", borderRadius: 10 },
  head: { fontSize: 14, fontWeight: 800, color: "#f8fafc" },
  text: { fontSize: 13, lineHeight: 1.55, color: "#cbd5e1" },
  strong: { color: "#fbbf24", fontWeight: 700 },
  actions: { display: "flex", gap: 8, flexWrap: "wrap", marginTop: 2 },
  primary: { flex: 1, minWidth: 160, padding: "10px 14px", background: "linear-gradient(135deg,#fbbf24,#d97706)", border: "none", borderRadius: 9, color: "#0a0e17", fontSize: 13, fontWeight: 800, cursor: "pointer" },
  btn: { padding: "10px 16px", background: "#1e293b", border: "1px solid #334155", borderRadius: 9, color: "#cbd5e1", fontSize: 13, fontWeight: 700, cursor: "pointer" },
};

export default function WhatsNew({ ui = {}, current, onNewLook, onClose }) {
  const s = { ...CLASSIC, ...ui };
  const classic = current === "classic";
  return (
    <div className="ui-fade" style={s.overlay}>
      <div className="ui-pop" style={s.box} role="dialog" aria-label="What's new">
        <div style={s.title}>What&apos;s new in Poker Suite</div>

        <div style={s.section}>
          <div style={s.head}>{classic ? "Want to see the new look?" : "You're on the new look"}</div>
          <div style={s.text}>
            {classic
              ? <>Poker Suite has been redesigned: darker, cleaner and quicker, with keyboard shortcuts in the hand builder (<span style={s.strong}>F</span> fold, <span style={s.strong}>C</span> check/call, <span style={s.strong}>R</span> raise, <span style={s.strong}>A</span> all-in, <span style={s.strong}>Enter</span> next). Your work carries over, and you can switch back any time.</>
              : <>Your work carried over. <span style={s.strong}>Classic view</span> in the top bar switches back to the original any time.</>}
          </div>
        </div>

        <div style={s.section}>
          <div style={s.head}>Squid and Bounty games</div>
          <div style={s.text}>
            <span style={s.strong}>Side Game</span> in the hand builder now offers <span style={s.strong}>Squid Game</span>,{" "}
            <span style={s.strong}>Progressive Squid Game</span> and <span style={s.strong}>Bounty Game</span>. Picking one asks for its
            structure: how many buttons and what each is worth, or how many wins in a row start the bounty and what each win pays.
          </div>
          <div style={s.text}>
            From then on the game keeps itself: buttons and streaks are written into each player&apos;s name in the hand history,
            and the chip in the top bar shows who holds what. When a hand pays a bounty or ends a squid round, you&apos;re asked to
            confirm it first, then the payouts go into the next hand&apos;s stacks.
          </div>
        </div>

        <div style={s.section}>
          <div style={s.head}>Pay for side games</div>
          <div style={s.text}>
            Squid and Bounty hands are transcribed and paid like any other hand, plus <span style={s.strong}>$0.25</span> every
            time you record a bounty payout or a squid round being settled.
          </div>
        </div>

        <div style={s.section}>
          <div style={s.head}>Mark streams complete</div>
          <div style={s.text}>
            When you finish a stream, mark it complete: <span style={s.strong}>Mark Complete</span> in the Calendar, or{" "}
            <span style={s.strong}>Finish stream</span> in the Hand Manager. That&apos;s what pays the end-of-stream bonus,{" "}
            <span style={s.strong}>$0.05 for every hand</span> you entered in it. A stream that&apos;s never marked complete never pays it.
          </div>
        </div>

        <div style={s.actions}>
          {classic ? (
            <>
              <button style={s.primary} onClick={onNewLook}>See the new look</button>
              <button style={s.btn} onClick={onClose}>Not now</button>
            </>
          ) : (
            <button style={s.primary} onClick={onClose}>Got it</button>
          )}
        </div>
      </div>
    </div>
  );
}
