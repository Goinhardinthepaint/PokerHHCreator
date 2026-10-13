# Parser update: add the 7-2 game (paste into the parser session)

The PokerHHCreator app now records a third side game, the **7-2 game**, in the same `*** SIDE GAME ***` section as bounty and squid. Please extend the parser as follows. Nothing about bounty or squid has changed. The full canonical spec, now including 7-2, is `docs/SIDE_GAME_HH_FORMAT.md` in the PokerHHCreator repo.

## The game

A player who wins a hand holding a 7 and a 2 (any suits) collects a fixed amount from every other player dealt in. "Won the hand" uses the same rule as the other games: the sole winner of the MAIN pot on the FIRST board. A chop pays nothing.

## Where the lines appear

- 7-2 lines appear only in a hand where at least one player dealt in holds 7-2. Hands with no 7-2 dealt have no 7-2 lines, even while the game is on. A missing 7-2 line does NOT mean the game was off.
- A SIDE GAME section can now contain ONLY 7-2 lines. That happens when no bounty or squid game is running.
- With bounty and/or squid also on, the order inside the section is: bounty lines, then squid lines, then 7-2 lines.
- The 7-2 game carries no state between hands, so it never adds a suffix to player names. Name handling for bounty and squid is unchanged.

## New line types

    7-2 game: $<value> from each player dealt in
    7-2 hand: Seat <n> (<base>) [<card> <card>] <won|chopped|lost>
    7-2 payout: Seat <p> (<payer base>) pays Seat <w> (<winner base>) $<amount> for 7-2

Regexes, anchored, one line each. Each SIDE GAME line still matches exactly one regex across the whole set:

    T_GAME    = r"^7-2 game: \$(\d+(?:\.\d{1,2})?) from each player dealt in$"
    T_HAND    = r"^7-2 hand: Seat (\d+) \((.+)\) \[([2-9TJQKA][shdc]) ([2-9TJQKA][shdc])\] (won|chopped|lost)$"
    T_PAYOUT  = r"^7-2 payout: Seat (\d+) \((.+?)\) pays Seat (\d+) \((.+?)\) \$(\d+(?:\.\d{1,2})?) for 7-2$"

## Semantics

- **The game line:** exactly one `7-2 game` line, before the holder lines. `value` is what each other dealt-in player pays a winner.
- **Holder lines:** one `7-2 hand` line per player holding 7-2, in seat order. Two players can both hold 7-2. Cards are `<rank><suit>`, with `T` for ten.
- **`won`:** the holder was the sole winner of the hand.
- **`chopped`:** the holder was one of several winners.
- **`lost`:** everything else, including folding. Whether the holder chose to play the hand is in the actions as usual.
- **Payouts:** only `won` pays, with one `7-2 payout` line from every OTHER seat dealt in, each for the full `value`. The lines are authoritative, so use the rules only for validation.
- **Stacks:** like the other games, 7-2 money is not part of the pot. The next hand's starting stacks already include it.

## Validation (warn, never crash)

- `7-2 game` is present if and only if at least one `7-2 hand` line is.
- Each `7-2 hand` holds a 7 and a 2.
- Payout lines appear exactly for `won` holders, one per other dealt-in seat, with amount = `value`.
- If the holder's cards are shown elsewhere in the hand, they match the `7-2 hand` line.

## Data model

Add to the per-hand `side_game` object:

    seven_two: {value, holders: [{seat, base, cards: [c1, c2], result}], payouts: [{payer_seat, payer, winner_seat, winner, amount}]}

Include 7-2 in per-player side-game P&L as its own game, next to bounty and squid.

## Examples

### G. raver wins holding 7-2 and collects $200 from each of 3 opponents

    *** SIDE GAME ***
    7-2 game: $200 from each player dealt in
    7-2 hand: Seat 2 (raver) [7h 2c] won
    7-2 payout: Seat 1 (JT) pays Seat 2 (raver) $200 for 7-2
    7-2 payout: Seat 3 (Nik) pays Seat 2 (raver) $200 for 7-2
    7-2 payout: Seat 4 (Britney) pays Seat 2 (raver) $200 for 7-2

### G2. A 7-2 that folds or loses

It has no payout lines:

    *** SIDE GAME ***
    7-2 game: $200 from each player dealt in
    7-2 hand: Seat 4 (Britney) [2d 7d] lost

### G3. 7-2 alongside the bounty game

Bounty lines come first in the same section:

    *** SIDE GAME ***
    Bounty schedule: 3=$300 4=$400 5=$500 then +$100
    Bounty streak: Seat 1 (JT) 0 -> 0 lost
    Bounty streak: Seat 2 (raver) 2 -> 3 won
    Bounty streak: Seat 3 (Nik) 0 -> 0 lost
    Bounty payout: Seat 1 (JT) pays Seat 2 (raver) $300 for 3 in a row
    Bounty payout: Seat 3 (Nik) pays Seat 2 (raver) $300 for 3 in a row
    7-2 game: $200 from each player dealt in
    7-2 hand: Seat 2 (raver) [7h 2c] won
    7-2 payout: Seat 1 (JT) pays Seat 2 (raver) $200 for 7-2
    7-2 payout: Seat 3 (Nik) pays Seat 2 (raver) $200 for 7-2

## History note

Before 2026-10-13, the app turned a hand where the 7-2 holder voluntarily played into an untranscribed "side game" count. Those hands have no hand history at all, so they can't be recovered. From now on 7-2 hands are transcribed in full with the lines above.

## Tests to add

- Exact parse with zero warnings for G, G2 and G3.
- Deliberately broken variants, each expecting its warning:
  - a payout for a `lost` holder,
  - a missing payout,
  - a wrong amount,
  - a game line with no holder lines.
