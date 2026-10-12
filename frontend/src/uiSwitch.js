// Switch between the two UIs that ship side by side: the redesign (index.html)
// and the original (classic.html). The server serves whichever the `ui` cookie
// names (server.py _ui_page), so switching is: set the cookie, reload. The
// session in localStorage is shared, so an in-progress hand survives the swap.
export const currentUi = () => (document.cookie.match(/(?:^|;\s*)ui=(\w+)/) || [])[1] || null;

export function switchUi(to) {
  document.cookie = `ui=${to}; path=/; max-age=${60 * 60 * 24 * 365}; samesite=lax`;
  window.location.reload();
}
