# Site refresh: launch notes (October 2026)

## What workers see

- **The original look loads first.** A one-time *What's new* popup:
  - offers to switch to the new look,
  - explains the side-game changes and the $0.25 payout pay,
  - reminds workers to mark streams complete to earn the stream bonus.
- **Switching looks.**
  - In the new look, **Classic view** in the top bar switches back; in the original, **✨ New look** switches forward.
  - The choice is remembered per browser.
  - To make the new look the default for everyone, set `UI_DEFAULT=new` on Railway.
- **Side Game** opens a menu: **Squid Game** (one button each), **Progressive Squid Game** or **Bounty Game**.
  - Picking one asks for its structure: buttons and $ per button, or when bounties start and what each win pays.
  - A chip in the top bar reopens the game, with holders, streaks, a new round and End game.
  - Each seat shows a small round marker: violet for squid buttons, copper for the bounty streak.
  - The setup panel no longer sits in the sidebar.
- **Side games run on their own, but money waits for a yes.**
  - A hand that pays a bounty asks "Did *player* just win the bounty game?"
  - A hand that ends a squid round asks "Has squid ended, and are all squids correct?"
  - Each question shows exactly who pays whom. **Yes** saves the hand and pays out. **No** saves nothing so the streaks or buttons can be fixed.
- **New deck (new look):** each card face is its suit's color, with a large white rank and a small suit in the corner.
- Keyboard play in the new look: `F` fold, `C` check/call, `R`/`B` raise, `A` all-in, `Enter` deal or next, `Z` undo.

## Pay

| | |
|---|---|
| Per piece (filled card or action) | $0.03 |
| Per hand | $0.10 |
| Per bounty payout or squid round settled in a hand | **$0.25** (new) |
| Per hand in a stream once it's marked complete | $0.05 |
| Month bonus (assigned month, error tiers) | $150 default |
| Tutorial | $1 |

## Payment fixes

- **Double submissions.** A double-click or retry saved and paid the same hand twice: 70 times in production, $69.22 in total.
  - The server now saves such a hand once.
  - Existing copies are marked `duplicate` on boot and left out of pay, counts and exports. They are kept, not deleted. Undo with `UPDATE hands SET type='hand' WHERE type='duplicate'`.
- **Month bonuses.**
  - Every assigned month pays its own bonus; before, one arbitrary month counted.
  - A month finished after its deadline is **late** and pays nothing.
- **Earnings owed** on the Admin page no longer includes the admin account's own test hands.
- **Square up** (Admin → worker): logs one payment so everything through a chosen day reads as paid. The day defaults to last Friday, Eastern time. What's left owed is exactly the work since that day.
- loja was squared up through **Fri 2026-10-09**.

## Needs you

1. **The $69.22 of duplicate hands was already paid.** loja's ledger is square through Friday without it. To deduct it from the next payment, record a -$69.22 adjustment.
2. **loja's January bonus is past its deadline** (2026-06-18). With deadlines now enforced, finishing January would pay $0. Extend the deadline in Admin → Months if that isn't what you want.
3. **Stream bonus:** no stream has ever been marked complete, so it has paid $0 so far. The What's New popup and the dashboard now remind workers.
4. **Stream auto-refresh is blocked in production.** YouTube's bot check rejects Railway. Add a free YouTube Data API key as `YOUTUBE_API_KEY` on the PokerHHCreator service.
5. **Railway's `railway.json` stops working on 2026-12-01.** Migrate it with `railway config migrate` before then.
6. **Side-game rule call:** "won the hand" means the main pot on the first board.
7. **The `staging` Railway environment** is still running and bills while it runs. Remove it with `railway environment delete staging`.
