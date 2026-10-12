# Side-game hand-history format (Bounty + Squid)

Canonical spec for how the Hand Builder writes bounty and squid games into hand
histories. Cole's separate database parser is built from this exact text, so a
change to the notation must be made here AND in that parser together.

---

Build a parser for PokerStars-format No-Limit Hold'em hand histories exported by my PokerHHCreator app, including two side games — the BOUNTY game and the SQUID game — that are recorded with the extensions below. PT4 compatibility is NOT a goal for side-game hands; this parser is their consumer. Hands without a side game are plain PokerStars format and must parse exactly as normal.

## 1. File and hand layout

- UTF-8, `\n` line endings. Hands are separated by blank lines; every hand begins with a line starting `PokerStars Hand #<digits>:`. Split on that.
- Normal sections, in order: header line, `Table '...' 9-max Seat #<n> is the button`, `Seat <n>: <name> ($<stack> in chips)` lines, blind/ante/straddle posts, `*** HOLE CARDS ***`, `*** FLOP ***` / `*** TURN ***` / `*** RIVER ***` as reached, optional `*** SHOW DOWN ***`, `*** SUMMARY ***`.
- The table name is the stream's YouTube link with the hand's timestamp, e.g. `Table 'https://youtu.be/AbCdEfGhIjK?t=1200'`. The 11-char video id identifies the stream; `t` (seconds) orders hands within it.
- Money is whole dollars with a `$` and no thousands separators (`$47968`). Accept an optional `.dd` anyway.
- A side-game hand has exactly one extra section, always the LAST section of the hand: a line `*** SIDE GAME ***`, running to the end of the hand. A hand with no side game has no such section.
- Side-game money is NOT part of the pot: it never appears in `collected` lines, `Total pot`, or uncalled-bet lines. It is recorded only in the SIDE GAME section.

## 2. Player names carry side-game state ("display names")

In the poker part of a side-game hand, each player's name has the state they held ENTERING that hand appended:

- Bounty: `<base><streak>bounty` when streak ≥ 1. `JT3bounty` = JT won (or chopped) the previous 3 hands in a row.
- Squid: `<base><buttons>squid` when holding ≥ 1 squid button. `raver2squid` = raver holds 2 buttons.
- Both games at once: bounty suffix first, e.g. `JT3bounty1squid`.
- Count 0 → no suffix (plain base name).

So one real player appears under several display names across hands (`JT`, `JT1bounty`, `JT2bounty`, …). Group players by BASE name.

**Never recover the base name by stripping the suffix with a regex.** Base names can end in digits. This is real data: `Adam221bounty` is `Adam22` on a 1-hand streak, not `Adam` on 221. Always map display → base through the SIDE GAME section, which lists every seat's base name. Join on seat number: `Seat <n>:` in the seat lines ↔ `Seat <n> (<base>)` in the side-game lines.

## 3. SIDE GAME section — line grammar

Every seat listed in the hand's `Seat <n>:` lines was dealt in, and gets exactly one state line per active game, in ascending seat order. Bounty lines come before squid lines. Line order within a game is as listed.

Squid buttons belong to the player, not the seat: a player who leaves the table keeps their buttons and is still paid at settlement. Such a player isn't in the seat lines, so they appear as `Away (<base>)` instead of `Seat <n> (<base>)`. Away lines come after the seated lines, sorted by base name, and only appear while the player holds ≥ 1 button. Bounty never uses `Away`: only players dealt into the hand pay or collect bounties.

Bounty game:

    Bounty schedule: <level>=$<amount> [<level>=$<amount> ...] then +$<step>
    Bounty streak: Seat <n> (<base>) <before> -> <after> <won|chopped|lost>
    Bounty payout: Seat <p> (<payer base>) pays Seat <w> (<winner base>) $<amount> for <k> in a row

Squid game:

    Squid round: <round>, <total> buttons, $<value> per button, <progressive|one-per-player>
    Squid buttons left: <before> -> <after>
    Squid buttons: <Seat <n>|Away> (<base>) <before> -> <after>[ won]
    Squid settled: <k> player[s] without a button pay[s] $<value> per button held
    Squid payout: Seat <p> (<payer base>) pays <Seat <h>|Away> (<holder base>) $<amount> for <b> button[s]

Python regexes (anchored, one line each):

    SECTION   = r"^\*\*\* SIDE GAME \*\*\*$"
    B_SCHED   = r"^Bounty schedule: ((?:\d+=\$\d+(?:\.\d{1,2})? )+)then \+\$(\d+(?:\.\d{1,2})?)$"
    B_STREAK  = r"^Bounty streak: Seat (\d+) \((.+)\) (\d+) -> (\d+) (won|chopped|lost)$"
    B_PAYOUT  = r"^Bounty payout: Seat (\d+) \((.+?)\) pays Seat (\d+) \((.+?)\) \$(\d+(?:\.\d{1,2})?) for (\d+) in a row$"
    S_ROUND   = r"^Squid round: (\d+), (\d+) buttons, \$(\d+(?:\.\d{1,2})?) per button, (progressive|one-per-player)$"
    S_LEFT    = r"^Squid buttons left: (\d+) -> (\d+)$"
    S_BUTTONS = r"^Squid buttons: (?:Seat (\d+)|Away) \((.+)\) (\d+) -> (\d+)( won)?$"
    S_SETTLED = r"^Squid settled: (\d+) players? without a button pays? \$(\d+(?:\.\d{1,2})?) per button held$"
    S_PAYOUT  = r"^Squid payout: Seat (\d+) \((.+?)\) pays (?:Seat (\d+)|Away) \((.+?)\) \$(\d+(?:\.\d{1,2})?) for (\d+) buttons?$"

B_SCHED group 1 is a space-separated list of `<level>=$<amount>` pairs. For seated players the seat number is the key within a hand; names in parentheses are the base names (use them for grouping, and cross-check them against the seat lines). In S_BUTTONS and S_PAYOUT the holder's seat group is `None` for an `Away` holder; key those by base name. Payers are always seated.

## 4. Semantics

### Bounty game

- A player's streak counts consecutive hands in which they won OR chopped part of the pot while the bounty game was on. `won`/`chopped` → `after = before + 1`. `lost` (folded, lost at showdown, collected nothing) → `after = 0`.
- Schedule → amount for a streak `s`: if `s` is below the lowest level, nothing. If `s` is a listed level, that amount. Above the highest listed level, `highest_amount + (s − highest_level) × step`. With `3=$300 4=$400 5=$500 then +$100`, a 6-streak pays $600, a 7-streak pays $700.
- Bounties are paid on every qualifying WIN, and they stack. On going 2→3 the winner collects the level-3 amount from every other player dealt in. On 3→4 they collect the level-4 amount from each of them again. Over a 4-hand run every opponent has paid that player $300 + $400 = $700.
- A CHOP never pays a bounty, even at a qualifying level, but the streak still goes up. Every player who chopped gets +1, so a 0-streak player who chops starts a streak at 1. Example: JT chops at 3→4 and collects nothing; if he then wins outright 4→5, he collects the level-5 amount.
- `Bounty payout` lines are authoritative: one line per payer per winner, for every OTHER seat dealt in, with the full amount. Don't recompute payouts from rules; use the rules only for validation.

### Squid game

- A round has `total` buttons (usually 7 or 8, often one fewer than the number of players), each worth `value`. Winning a hand outright takes a button while any remain.
- Who "won the hand" for squid: the sole winner of the pot. That's the last player in when everyone else folds, or the single best hand at showdown. Run it twice (or more): only the FIRST board counts. A tie at showdown (a chop) gives NO button to anyone. With side pots, the main pot's sole winner takes the button. At most one button changes hands per hand.
- `progressive`: one player can hold several buttons. `one-per-player`: a player who already holds a button takes none when they win again. The ` won` flag on a `Squid buttons` line means "took a button this hand", so it's absent in that case and `buttons left` doesn't change.
- `Squid buttons left: before -> after` drops by the number of ` won` flags (0 or 1). Invariant: sum of every `after` (seated AND `Away`) = `total − left_after`.
- A holder who leaves the table keeps their buttons (an `Away` line every squid hand until the round settles) and is paid at settlement like anyone else.
- The round settles in the hand where buttons left reaches 0. That hand has one `Squid settled` line, then a `Squid payout` line for every (seated player with 0 buttons) × (holder, seated or away) pair. Payers are the players dealt into that hand who hold no button. A payer pays each holder `value × buttons held`, so each payer pays `value × total` in total, and every button gets paid once per player left without one.
  - Example: $200/button, 7 buttons. Player 1 holds 2, players 2–6 hold 1 each, players 7 and 8 hold none. Players 7 and 8 each pay player 1 $400 and each of players 2–6 $200 ($1400 each). Every button is paid twice. If only one player is left without a button, every button is paid once.
- After a settlement, the next squid hand (if the game continues) starts a new round: `Squid round: <round+1>`, buttons left back to `total`, every count 0, and the `squid` suffixes disappear from names.

### Both games

- Side-game payouts happen after the pot is awarded. The next hand on the same stream starts with stacks that already include them. Amounts are the full amounts owed even when larger than the payer's remaining stack (live players top up from their pocket).

## 5. Validation (warn, never crash)

- The section exists if and only if some name carries a suffix or a side-game line exists. Each seat has exactly one state line per active game. Rebuilding each display name from base + the `before` counts reproduces the seat-line name exactly.
- Bounty: `after` follows the won/chopped/lost rule. Payouts appear only for `won` seats at qualifying levels, one per other dealt-in seat, with amounts matching the schedule. Chopped seats have no payouts.
- Squid: the invariant in §4 holds (seated + Away). At most one ` won` flag per hand, never on a seat that tied at showdown. Settlement lines appear exactly when `left_after == 0`, and the payout pairs are complete (every seated non-holder × every holder, Away included). Every holder from the previous hand of the same round appears again, seated or Away.
- Cross-hand continuity, per stream (video id) in timestamp order: a hand's `before` values should equal the previous side-game hand's `after` values. Hands can be skipped or edited, so report mismatches but don't fail.
- Stack continuity, optional: the next hand's starting stack should be close to `ending stack + payouts received − payouts paid`. Workers can adjust stacks by hand, so this is a report only.

## 6. Legacy hands (bounty v1)

Some hands transcribed on 2026-09-29, before this format existed, have bounty suffixes in names but NO SIDE GAME section. Real names seen: `Adam221bounty`, `CK1bounty`, `Nicolette1bounty`, `Raver1bounty`, `Raver2bounty`, `Raver3bounty`, `Sashimi1bounty`, `Sashimi3bounty`. For hands with no section:

- Resolve the base name by preferring a split whose base matches a plain (suffix-free) name seen elsewhere in the same stream. Otherwise assume a single-digit streak: `^(.+)(\d)bounty$` (`Adam221bounty` → `Adam22`, 1).
- Mark these `legacy=True` with no schedule or payouts. Payouts weren't recorded then.

## 7. Worked examples

### A. Bounty: JT wins his 3rd straight and collects $300 from each of 3 opponents

    PokerStars Hand #700000000001:  Hold'em No Limit ($50/$100 USD) - 2026/09/22 00:20:00 ET
    Table 'https://youtu.be/AbCdEfGhIjK?t=1200' 9-max Seat #1 is the button
    Seat 1: JT2bounty ($10000 in chips)
    Seat 2: raver ($10000 in chips)
    Seat 3: Nik ($10000 in chips)
    Seat 4: Britney ($10000 in chips)
    raver: posts small blind $50
    Nik: posts big blind $100
    *** HOLE CARDS ***
    Britney: folds
    JT2bounty: raises $200 to $300
    raver: folds
    Nik: folds
    Uncalled bet ($200) returned to JT2bounty
    JT2bounty collected $250 from pot
    *** SUMMARY ***
    Total pot $250 | Rake $0
    Seat 1: JT2bounty (button) collected $250 from pot
    Seat 2: raver (small blind) folded before Flop
    Seat 3: Nik (big blind) folded before Flop
    Seat 4: Britney folded before Flop (didn't bet)
    *** SIDE GAME ***
    Bounty schedule: 3=$300 4=$400 5=$500 then +$100
    Bounty streak: Seat 1 (JT) 2 -> 3 won
    Bounty streak: Seat 2 (raver) 0 -> 0 lost
    Bounty streak: Seat 3 (Nik) 0 -> 0 lost
    Bounty streak: Seat 4 (Britney) 0 -> 0 lost
    Bounty payout: Seat 2 (raver) pays Seat 1 (JT) $300 for 3 in a row
    Bounty payout: Seat 3 (Nik) pays Seat 1 (JT) $300 for 3 in a row
    Bounty payout: Seat 4 (Britney) pays Seat 1 (JT) $300 for 3 in a row

The next hand's seat lines show the carried-over names and stacks: JT 10000 − 300 + 200 + 250 + 900 = 11050; raver 10000 − 50 − 300 = 9650; Nik 10000 − 100 − 300 = 9600; Britney 10000 − 300 = 9700.

    PokerStars Hand #700000000002:  Hold'em No Limit ($50/$100 USD) - 2026/09/22 00:23:10 ET
    Table 'https://youtu.be/AbCdEfGhIjK?t=1390' 9-max Seat #2 is the button
    Seat 1: JT3bounty ($11050 in chips)
    Seat 2: raver ($9650 in chips)
    Seat 3: Nik ($9600 in chips)
    Seat 4: Britney ($9700 in chips)
    ...

### B. Bounty: a chop. Both choppers advance, nobody is paid

    *** SIDE GAME ***
    Bounty schedule: 3=$300 4=$400 5=$500 then +$100
    Bounty streak: Seat 1 (JT) 3 -> 4 chopped
    Bounty streak: Seat 2 (raver) 0 -> 1 chopped
    Bounty streak: Seat 3 (Nik) 0 -> 0 lost
    Bounty streak: Seat 4 (Britney) 0 -> 0 lost

In the next hand they appear as `JT4bounty` and `raver1bounty`.

### C. Squid: the last of 7 buttons is taken and the round settles ($200/button, progressive, 8 players)

Seat lines entering this hand: `JT2squid`, `raver1squid`, `Nik1squid`, `Britney1squid`, `Rampage1squid`, `Mariano`, `Sashimi`, `Adam22`. Earlier in the round Adam22 never held a button. If he had held one, he'd have appeared as `Adam221squid`, which is why §2 forbids suffix-stripping. Mariano wins the hand:

    *** SIDE GAME ***
    Squid round: 1, 7 buttons, $200 per button, progressive
    Squid buttons left: 1 -> 0
    Squid buttons: Seat 1 (JT) 2 -> 2
    Squid buttons: Seat 2 (raver) 1 -> 1
    Squid buttons: Seat 3 (Nik) 1 -> 1
    Squid buttons: Seat 4 (Britney) 1 -> 1
    Squid buttons: Seat 5 (Rampage) 1 -> 1
    Squid buttons: Seat 6 (Mariano) 0 -> 1 won
    Squid buttons: Seat 7 (Sashimi) 0 -> 0
    Squid buttons: Seat 8 (Adam22) 0 -> 0
    Squid settled: 2 players without a button pay $200 per button held
    Squid payout: Seat 7 (Sashimi) pays Seat 1 (JT) $400 for 2 buttons
    Squid payout: Seat 7 (Sashimi) pays Seat 2 (raver) $200 for 1 button
    Squid payout: Seat 7 (Sashimi) pays Seat 3 (Nik) $200 for 1 button
    Squid payout: Seat 7 (Sashimi) pays Seat 4 (Britney) $200 for 1 button
    Squid payout: Seat 7 (Sashimi) pays Seat 5 (Rampage) $200 for 1 button
    Squid payout: Seat 7 (Sashimi) pays Seat 6 (Mariano) $200 for 1 button
    Squid payout: Seat 8 (Adam22) pays Seat 1 (JT) $400 for 2 buttons
    Squid payout: Seat 8 (Adam22) pays Seat 2 (raver) $200 for 1 button
    Squid payout: Seat 8 (Adam22) pays Seat 3 (Nik) $200 for 1 button
    Squid payout: Seat 8 (Adam22) pays Seat 4 (Britney) $200 for 1 button
    Squid payout: Seat 8 (Adam22) pays Seat 5 (Rampage) $200 for 1 button
    Squid payout: Seat 8 (Adam22) pays Seat 6 (Mariano) $200 for 1 button

Each payer pays $1400 = $200 × 7. JT receives $800, and each other holder $400.

### D. Squid mid-round: JT takes a second button

    *** SIDE GAME ***
    Squid round: 1, 7 buttons, $200 per button, progressive
    Squid buttons left: 3 -> 2
    Squid buttons: Seat 1 (JT) 1 -> 2 won
    Squid buttons: Seat 2 (raver) 1 -> 1
    Squid buttons: Seat 3 (Nik) 1 -> 1
    Squid buttons: Seat 4 (Britney) 1 -> 1
    Squid buttons: Seat 5 (Rampage) 0 -> 0
    Squid buttons: Seat 6 (Mariano) 0 -> 0
    Squid buttons: Seat 7 (Sashimi) 0 -> 0
    Squid buttons: Seat 8 (Adam22) 0 -> 0

### E. Squid: a holder has left the table, and is still paid when the round settles

Rampage left with 1 button. Seat 5 is empty now, so he appears as `Away`. Seven players are dealt in, and Mariano takes the last button:

    *** SIDE GAME ***
    Squid round: 1, 7 buttons, $200 per button, progressive
    Squid buttons left: 1 -> 0
    Squid buttons: Seat 1 (JT) 2 -> 2
    Squid buttons: Seat 2 (raver) 1 -> 1
    Squid buttons: Seat 3 (Nik) 1 -> 1
    Squid buttons: Seat 4 (Britney) 1 -> 1
    Squid buttons: Seat 6 (Mariano) 0 -> 1 won
    Squid buttons: Seat 7 (Sashimi) 0 -> 0
    Squid buttons: Seat 8 (Adam22) 0 -> 0
    Squid buttons: Away (Rampage) 1 -> 1
    Squid settled: 2 players without a button pay $200 per button held
    Squid payout: Seat 7 (Sashimi) pays Seat 1 (JT) $400 for 2 buttons
    Squid payout: Seat 7 (Sashimi) pays Seat 2 (raver) $200 for 1 button
    Squid payout: Seat 7 (Sashimi) pays Seat 3 (Nik) $200 for 1 button
    Squid payout: Seat 7 (Sashimi) pays Seat 4 (Britney) $200 for 1 button
    Squid payout: Seat 7 (Sashimi) pays Seat 6 (Mariano) $200 for 1 button
    Squid payout: Seat 7 (Sashimi) pays Away (Rampage) $200 for 1 button
    Squid payout: Seat 8 (Adam22) pays Seat 1 (JT) $400 for 2 buttons
    Squid payout: Seat 8 (Adam22) pays Seat 2 (raver) $200 for 1 button
    Squid payout: Seat 8 (Adam22) pays Seat 3 (Nik) $200 for 1 button
    Squid payout: Seat 8 (Adam22) pays Seat 4 (Britney) $200 for 1 button
    Squid payout: Seat 8 (Adam22) pays Seat 6 (Mariano) $200 for 1 button
    Squid payout: Seat 8 (Adam22) pays Away (Rampage) $200 for 1 button

### F. Squid: a chop at showdown gives no button to anyone

Raver and Nik split the pot. Nobody takes a button, and `buttons left` is unchanged:

    *** SIDE GAME ***
    Squid round: 1, 7 buttons, $200 per button, progressive
    Squid buttons left: 4 -> 4
    Squid buttons: Seat 1 (JT) 2 -> 2
    Squid buttons: Seat 2 (raver) 0 -> 0
    Squid buttons: Seat 3 (Nik) 1 -> 1
    ...

(Run it twice is different: only the first board decides the button, so the first board's sole winner gets ` won` even if the second board went the other way.)

## 8. What to build

1. A parser that returns, per hand: hand id, stream video id, timestamp seconds, stakes, button seat, players (seat, display name, BASE name, starting stack), actions, board, showdown, pots, and a `side_game` object:
   `bounty: {schedule: {levels: {3: 300, …}, step: 100}, streaks: [{seat, base, before, after, result}], payouts: [{payer_seat, payer, winner_seat, winner, amount, streak}]}`
   `squid: {round, total, value, mode, left_before, left_after, buttons: [{seat|None, away: bool, base, before, after, won}], settled: bool, payouts: [{payer_seat, payer, holder_seat|None, holder, amount, buttons}]}`
   plus `legacy: bool` and a list of validation warnings.
2. Player grouping by base name across every display variant.
3. Per-player side-game P&L, kept separate from pot winnings: payouts received − payouts paid, per game, per stream and overall.
4. Unit tests built from examples A–F (exact parse + zero warnings), from a deliberately broken variant of each (expect the matching warning), and from the `Adam221bounty` legacy case.
