// Switch between the two UIs that ship side by side: the redesign (index.html)
// and the original (classic.html). The server serves whichever the `ui` cookie
// names (server.py _ui_page), so switching is: set the cookie, reload. The
// session in localStorage is shared, so an in-progress hand survives the swap.
export const currentUi = () => (document.cookie.match(/(?:^|;\s*)ui=(\w+)/) || [])[1] || null;

export function switchUi(to) {
  document.cookie = `ui=${to}; path=/; max-age=${60 * 60 * 24 * 365}; samesite=lax`;
  window.location.reload();
}

// The What's New popup shows once per browser (both UIs share localStorage).
// Bump WHATS_NEW_ID to announce something new.
const WHATS_NEW_KEY = "pokerhh_whats_new_seen";
const WHATS_NEW_ID = "2026-10-side-games-72";
export function whatsNewSeen() {
  try { return localStorage.getItem(WHATS_NEW_KEY) === WHATS_NEW_ID; } catch { return true; }
}
export function markWhatsNewSeen() {
  try { localStorage.setItem(WHATS_NEW_KEY, WHATS_NEW_ID); } catch { /* storage blocked: it just shows again */ }
}
