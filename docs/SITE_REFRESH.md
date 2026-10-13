# Site refresh (branch `site-refresh`)

**Preview:** https://pokerhhcreator-staging.up.railway.app. Log in with your usual admin password.

The preview runs in its own Railway environment (`staging`) on its own database. That database is a copy of production taken 2026-10-12. Anything saved there stays there, so no real work should happen on it. Production is unchanged until launch.

## What's new

- **Two versions, one switch.**
  - The new design is the default.
  - **Classic view** in its top bar switches to the original site. **✨ New look** switches back.
  - The choice is remembered per browser. Both versions share the same data and session.
- **Bounty and squid games, in both versions.**
  - Turn them on in the sidebar. A panel sets the exact amounts:
    - Bounty: pays from N in a row, first bounty $, plus $ per extra win.
    - Squid: number of buttons, $ per button, progressive or one-per-player.
  - The panel shows who holds which streak or buttons, plus what last hand paid.
  - Stacks for the next hand already include side-game money.
  - Hand histories carry the notation from `docs/SIDE_GAME_HH_FORMAT.md`.
- **Redesign** in Sophia's look: black, gold, ivory cards, Cormorant headings, smooth drawers and modals, and a table that fits the screen.
- **Keyboard play** in the builder:
  - `F` fold, `C` check/call, `R`/`B` bet or raise to the slider amount, `A` all-in.
  - `Enter` deals or advances, `Z` undoes.
- **Faster.** On a copy of production data, the admin page dropped from 233 ms to 42 ms. The dashboard, which reloads after every saved hand, dropped from 118 ms to 15 ms.

## Bugs fixed (both versions)

**Money and data**
- Going to Calendar, Dashboard or Admin and back could restore an old session over the current one: roster, stacks, button, hand number.
- A network blip logged workers out mid-session.
- Pay was computed from numbers the browser sent, so a crafted request could claim any amount. The server now caps it at what the submitted hand can actually contain.
- The saved session grew without limit until browser storage filled, then silently stopped saving.
- Marking a stream complete from the builder overwrote the catalog's title and date.

**Hand histories**
- An all-in for less than the call was written as an invalid raise.
- A straddle shorter than the big blind broke the betting.
- A blind or ante that put a player all-in was missing "and is all-in".
- A big blind all-in for the ante alone could win the whole pot.
- Duplicate player names or seat numbers silently broke winners and side pots. They are now refused.
- The custom straddle menu offered the blinds.
- The "pot" raise size left out the call amount.
- All of these were found by fuzzing about 50,000 random hands through the engine, formatter and linter. All of those hands pass now.

**Security**
- Four scraper endpoints (`run`, `stop`, `download`, `status`) had no login check. They are now admin-only.
- Unknown `/api/` paths returned the web page instead of an error.
- Cross-site requests were accepted from any origin.
- Payments accepted NaN or infinity.

**Screens**
- The PT4 import counted each player twice.
- Streams added by hand got random IDs, so their progress stayed at 0.
- "Mark complete" showed ✓ even when the server rejected it.
- Any admin error replaced the whole page.
- Downloads failed in Firefox and Safari.
- Toasts cut each other off.
- After 8pm ET, the default video date was tomorrow.
- A blank stack became NaN.
- A double-click applied two actions.
- Some borders turned white after a highlight went away.
- Opening a worker in Admin downloaded every hand's full text. It now shows 100 hands at a time and fetches a hand's text when you expand it.

## Needs you

1. **Stream auto-refresh is blocked in production.** YouTube's bot check rejects the Railway server. Production logs say "nothing fetched". New streams only appear when the catalog is refreshed by hand. The fix is a free YouTube Data API key: add it as `YOUTUBE_API_KEY` on the PokerHHCreator service, and the refresher uses it automatically.
2. **Railway config deadline: 2026-12-01.** Railway has deprecated `railway.json` and says it stops working on that date. It should be migrated before then (`railway config migrate`).
3. **Side-game rule call.** "Won the hand" means won the main pot on the first board. Someone who only wins a side pot does not advance a bounty or take a squid button. Say if you want it different.
4. **Stream bonus is never paid.** No stream has ever been marked complete, so the $0.05/hand stream bonus is $0 for everyone. Marking streams complete in the Calendar releases it.
5. **Not changed, for your judgment:**
   - Anyone can register an account.
   - Payments can't be deleted. Reversals are negative payments, e.g. the ±$1,614.68 pair on 2026-08-14.
   - The app runs on Flask's development server.

## Launch

- Say the word and `site-refresh` is merged into `main`. `main` has no commits the branch lacks, so the merge is clean. Railway then deploys production as usual.
- Everyone lands on the new design, with Classic one click away. To make Classic the default at first, set `UI_DEFAULT=classic`.
- The database change is one new index, created automatically on boot.
- After launch, delete the preview environment, since it bills while it runs: `railway environment delete staging`.
