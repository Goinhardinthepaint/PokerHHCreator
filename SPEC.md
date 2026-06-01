# Poker Livestream Hand Scraper — Complete Specification
# For Claude Code: rebuild src/export/pt4_formatter.py and action inference in src/video/pipeline.py from scratch

## OVERVIEW

This tool scrapes poker hands from YouTube livestream videos. The video pipeline extracts frames at 1fps, sends them to Claude Vision API, and reads the on-screen overlay graphics. From sequences of frame reads, it must:

1. Detect hand boundaries (new hand starts)
2. Identify all players, their positions, stacks, and hole cards
3. Reconstruct the complete action sequence (who bet/raised/called/folded and when)
4. Detect blinds, antes, and straddles
5. Export a PokerStars-format .txt file that imports cleanly into PokerTracker 4

---

## PT4 HAND HISTORY FORMAT (CONFIRMED WORKING)

### Header
```
PokerStars Hand #<deterministic_hash>:  Hold'em No Limit ($<sb>/$<bb> USD) - <YYYY/MM/DD HH:MM:SS> ET
Table '<table_name>' 9-max Seat #<button_seat> is the button
```
- MUST say "PokerStars" — PT4 does not recognize "PokerBros" or custom site names
- Hand ID: deterministic hash of stream_url + timestamp + hand_index (for idempotency)
- Always "9-max" regardless of how many players are seated

### Seat Listings
```
Seat 1: PlayerName ($<adjusted_stack> in chips)
Seat 2: PlayerName ($<adjusted_stack> in chips)
...
```
- Stack is the ADJUSTED stack (see Ante Stack Adjustment below)
- Players who are sitting out or not dealt in should NOT be listed

### Posting Order (CRITICAL — must be exactly this order)
```
<player>: posts the ante $<ante_per_player>     ← ALL players, one line each
<player>: posts the ante $<ante_per_player>     ← (repeat for each player)
<sb_player>: posts small blind $<sb>
<bb_player>: posts big blind $<bb>
<straddle_player>: posts straddle $<straddle>   ← only if straddle detected
```
- Antes BEFORE blinds BEFORE straddle
- Ante lines listed in seat order starting from the button (or any consistent order)

### Hole Cards
```
*** HOLE CARDS ***
Dealt to <first_player_with_visible_cards> [<card1> <card2>]
```
- PT4 expects exactly ONE "Dealt to" line (the "hero")
- Pick the first player whose hole cards are visible in the video

### Street Actions
```
*** FLOP *** [<card1> <card2> <card3>]
*** TURN *** [<flop1> <flop2> <flop3>] [<turn_card>]
*** RIVER *** [<flop1> <flop2> <flop3> <turn_card>] [<river_card>]
```

### Action Format
```
PlayerName: folds
PlayerName: checks
PlayerName: calls $<amount_added>
PlayerName: bets $<amount>
PlayerName: raises $<increment> to $<total>
PlayerName: raises $<increment> to $<total> and is all-in
```

### CRITICAL: Call and Raise Math
- **Calls show the amount ADDED, not the total.** If a player has $50 in (from SB) and calls a $600 raise, they "calls $550".
- **Raises show increment AND total.** "raises $500 to $600" means they added $500 on top of the previous $100 bet, making their total $600.
- **Antes are DEAD MONEY.** Antes do NOT count as existing commitment for call/raise calculations. Only blinds and straddle count as existing commitment.
  - Example: Player posted $17 ante and $50 SB. Facing a $600 raise. Their commitment is $50 (NOT $67). They "calls $550".

### Showdown
```
*** SHOW DOWN ***
PlayerName: shows [<card1> <card2>] (<hand_description>)
PlayerName: shows [<card1> <card2>] (<hand_description>)
<winner_name> collected $<pot_total> from pot
```
- Hand descriptions: "a pair of Kings", "two pair Aces and Tens", "a flush Ace high", "three of a kind Jacks", "a straight Ten to Ace", "a full house Kings full of Nines", "four of a kind Aces", "a straight flush", "a royal flush", "high card Ace"
- Only players who reached showdown appear here (not folders)
- Winner determined by hand evaluation against the board

### No Showdown (everyone folded to a bet)
```
Uncalled bet ($<uncalled_amount>) returned to <winner_name>
<winner_name> collected $<pot_amount> from pot
```
- Uncalled amount = the final bet/raise amount that nobody called
- Pot amount = total pot minus the uncalled portion

### Summary Section
```
*** SUMMARY ***
Total pot $<total_pot> | Rake $0
Board [<all_board_cards>]
Seat 1: PlayerName (button) folded before Flop (didn't bet)
Seat 2: PlayerName (small blind) folded before Flop
Seat 3: PlayerName (big blind) folded before Flop (didn't bet)
Seat 4: PlayerName showed [Tc 8d] and lost with a pair of Kings
Seat 5: PlayerName showed [Ah 5d] and won ($51450) with a pair of Kings
Seat 6: PlayerName folded before Flop (didn't bet)
```

### Summary Position Labels (ONLY these three are allowed)
- `(button)` — for the button player
- `(small blind)` — for the SB player  
- `(big blind)` — for the BB player
- ALL other positions: NO label. Just "Seat 4: Otto showed..." not "Seat 4: Otto (under the gun) showed..."

### Summary Fold Descriptions
- `folded before Flop` — folded preflop after putting money in voluntarily (called then folded to a reraise)
- `folded before Flop (didn't bet)` — folded preflop without voluntarily putting money in (just folded their hand)
- `folded on the Flop` — folded on the flop
- `folded on the Turn` — folded on the turn
- `folded on the River` — folded on the river

### Summary Win/Loss
- `showed [cards] and won ($amount) with <hand_description>`
- `showed [cards] and lost with <hand_description>`
- `collected ($amount)` — won without showdown

---

## ANTE HANDLING (BB ANTE → SPREAD ANTE CONVERSION)

### Problem
HCL and many live games use a "Big Blind Ante" where only the BB player posts the ante (e.g., $100). PT4 does NOT support BB-only ante in PokerStars format. It DOES support traditional all-player antes.

### Solution
Spread the BB ante equally across all players and adjust starting stacks so effective stacks after antes match reality.

### Algorithm
```
bb_ante_total = 100  (the actual BB ante from the game)
num_players = 6
ante_per_player = bb_ante_total // num_players  (integer division, e.g., 17)
rounding_error = bb_ante_total - (ante_per_player * num_players)  (e.g., 100 - 102 = -2, or 100 - 96 = 4)

For each player:
  if player is the BB:
    adjusted_stack = real_stack - (bb_ante_total - ante_per_player)
    # BB paid $100 in reality, will pay $17 in HH, so subtract $83 from stack
  else:
    adjusted_stack = real_stack + ante_per_player
    # Non-BB players paid $0 in reality, will pay $17 in HH, so add $17 to stack
```

### Result
After antes are posted in the HH, every player's remaining stack matches their real-game stack. The total ante money in the pot equals the real BB ante (approximately, within rounding).

---

## STRADDLE HANDLING

### Detection from Video Frames
A straddle is detected when:
- A player has `current_bet >= 2 * bb` in early preflop frames (first 8 frames of the hand)
- AND that player has NO action_text overlay (it's a forced post, not a voluntary raise)
- The straddle amount is typically 2x BB (e.g., $200 in a $50/$100 game)

### Mississippi Straddle (Any Position)
HCL allows straddles from ANY position. The straddler's seat determines preflop action order:
- **Preflop:** Action starts with the player to the LEFT of the straddler, goes clockwise. Straddler acts LAST.
- **Postflop:** Normal order. Earliest position acts first (SB → BB → UTG → ... → BTN).

### Position Order Examples (6-max, BTN = Seat 1)
Seats: 1=BTN, 2=SB, 3=BB, 4=UTG, 5=HJ, 6=CO

**No straddle:** UTG(4), HJ(5), CO(6), BTN(1), SB(2), BB(3)
**UTG straddle (seat 4):** HJ(5), CO(6), BTN(1), SB(2), BB(3), STR(4)
**CO straddle (seat 6):** BTN(1), SB(2), BB(3), UTG(4), HJ(5), STR(6)
**BTN straddle (seat 1):** SB(2), BB(3), UTG(4), HJ(5), CO(6), STR(1)

### HH Format
```
<straddler>: posts straddle $200
```
- Appears after the big blind posting line
- PT4 accepts this from any seat position (confirmed with mississippi straddle test)

### Straddle vs Button Overlay
IMPORTANT: The button player may show `current_bet=200` in early frames due to overlay rendering, NOT because they straddled. To disambiguate:
- Check if the player has a "STR" position label
- Check if the player is NOT the button (button having cur_bet=200 is suspicious)
- Check if the commentary/transcript mentions a straddle and by whom
- Cross-reference: if pot = SB + BB + ante + 200, and only one player has cur_bet=200 with no action_text, that's the straddler

---

## ACTION INFERENCE FROM VIDEO FRAMES

### Core Concept
Each frame is a snapshot of the game state at ~1 second intervals. Actions are inferred by comparing consecutive frames on the same street.

### Street Detection
- Board cards = 0 → PREFLOP
- Board cards = 3 → FLOP  
- Board cards = 4 → TURN
- Board cards = 5 → RIVER
- Streets only progress FORWARD. If board count drops (camera cut), stay on current street.

### Blind Posting Inference
1. Scan first 15 preflop frames
2. Find players with position="SB" → posts small blind
3. Find players with position="BB" → posts big blind
4. VISION BUG: Sometimes both SB and BB are labeled "SB". When two players are labeled SB, the first is the real SB, the second is the BB.

### Voluntary Action Inference
For each street's frames, for each player:
- Read their `action_text` field (BET, RAISE, CALL, FOLD, CHECK, ALL-IN, 3-BET, etc.)
- Read their `current_bet` for the amount
- Read their `stack` for stack changes

### Action Text Mapping
```
"RAISE" or "RAISE $X" → raises
"3-BET" or "3-BET $X" → raises  
"BET" or "BET $X" → bets
"CALL" or "CALL $X" → calls
"FOLD" → folds
"CHECK" → checks
"ALL-IN" or "ALL IN" → all_in
"$X TO CALL" → skip (this is informational, not an action)
```

### Deduplication Rules
1. **Per-street:** For each (player, action_type) pair, keep only the LAST occurrence (highest frame index). Early OCR misreads get overwritten.
2. **Cross-street:** If the same (player, action, amount) appears on multiple streets, it's overlay bleed-through. Only keep the first occurrence.
3. **All-in persistence:** Once a player goes all-in, ignore any further action_text for them on subsequent streets.
4. **Supersession:** If a player has both a "calls" and a "raises" on the same street, and the raise frame is AFTER the call frame, remove the call (it was a misread before the raise resolved).

### Fold Inference
1. **Preflop folds:** Any player who had cards during preflop frames but does NOT appear in any postflop frame → folded preflop.
2. **Flop folds:** Any player who appeared in flop frames but NOT in turn/river frames → folded on the flop.
3. **Turn folds:** Any player in turn frames but NOT in river frames → folded on the turn.

### Action Sorting
**DO NOT sort by frame timestamp.** Sort by POSITION ORDER for the current street.
- Preflop: determined by straddle position (see Straddle Handling above)
- Postflop: SB first, clockwise to BTN

### Commitment Tracking for Call Amounts
Track how much each player has invested on each street:
```
commitments = {}

# Initialize with blind/straddle amounts (NOT antes)
commitments["sb_player"] = sb_amount  # e.g., 50
commitments["bb_player"] = bb_amount  # e.g., 100
commitments["straddler"] = straddle_amount  # e.g., 200

# When player calls:
call_amount = current_bet - commitments.get(player, 0)
# Output: "PlayerName: calls $<call_amount>"
# Update: commitments[player] = current_bet

# When player raises:
raise_increment = new_total - current_bet
# Output: "PlayerName: raises $<raise_increment> to $<new_total>"
# Update: commitments[player] = new_total; current_bet = new_total
```

### Reset per street
Commitments reset to 0 at the start of each new street (flop, turn, river). Only preflop has initial commitments from blinds/straddle.

---

## HAND BOUNDARY DETECTION

### Visual Boundaries (from frame comparison)
A new hand starts when:
1. **Board cleared (debounced):** Board cards go from >0 to 0 for 3+ consecutive frames. Single-frame flickers (camera cuts) are ignored.
2. **Pot reset:** Pot drops to <20% of previous value (winner collected). Fires immediately, no debounce needed.

### After boundary fires:
- Reset board_empty_streak counter
- Reset pot tracking baseline to 0 (prevents double-fire)

---

## SIDE GAME FILTERING

### Transcript-based detection
Scan the .vtt transcript for these patterns (case-insensitive):
- "squid game", "stand up game", "stupid game"
- "bounty game", "bounty"
- "bomb pot", "double board"
- "plo", "pot limit omaha", "four cards"

Hands whose start timestamp falls within 120 seconds of any flagged transcript segment are skipped.

### Vision-based detection
- If ANY player has more than 2 hole cards in ANY frame → PLO bomb pot → skip
- If the frame's `is_side_game` flag is true → skip

### Replay overlay filter
- If a hand has board cards but ZERO preflop frames (board=0 frames) → it's a replay graphic → skip

---

## WINNER DETECTION

### Method 1: Hand Evaluation (showdown)
If 2+ players remain at river:
1. Evaluate each player's best 5-card hand from their hole cards + board
2. Highest hand wins
3. Use standard poker hand rankings

### Method 2: Stack Delta (no showdown)
Compare first and last frame stacks. Player whose stack increased the most is the winner.

### Method 3: Last Aggressor (everyone folded)
If all players folded to a bet/raise, the last aggressor wins.

---

## POT CALCULATION

Total pot = sum of ALL money put in by ALL players across ALL streets.
This includes:
- All antes (the spread ante total, which approximately equals the real BB ante)
- Small blind
- Big blind
- Straddle
- All calls, bets, raises

When a bet is uncalled:
- Pot for "collected" line = total pot MINUS the uncalled portion
- Add "Uncalled bet ($X) returned to PlayerName" line

---

## CARD NOTATION
```
Ranks: A, K, Q, J, T, 9, 8, 7, 6, 5, 4, 3, 2
Suits: s (spades), h (hearts), d (diamonds), c (clubs)
Examples: As = Ace of spades, Td = Ten of diamonds, 2c = Two of clubs
```

---

## PLAYER NAME NORMALIZATION
- All player names title-cased: "OTTO" → "Otto", "NIK AIRBALL" → "Nik Airball"
- Applied at the vision analyzer level before any downstream processing
- Case-insensitive matching when comparing names across frames

---

## FILE STRUCTURE
```
src/export/pt4_formatter.py    — Takes a hand dict, outputs PT4-compatible text
src/export/hand_evaluator.py   — Evaluates poker hands for showdown descriptions
src/video/pipeline.py          — Contains action inference, hand assembly, straddle detection
src/vision/analyzer.py         — Vision API calls, name normalization
src/video/detector.py          — Hand boundary detection, side game filtering
src/video/extractor.py         — Frame extraction from video files
```

---

## EXAMPLE OUTPUT (what a correct hand looks like)

For a $50/$100 game with $100 BB ante and $200 CO straddle, 6 players:

```
PokerStars Hand #790175622847:  Hold'em No Limit ($50/$100 USD) - 2026/05/15 15:59:00 ET
Table 'Livestream p0Q7LW64ecM' 9-max Seat #1 is the button
Seat 1: Greedo ($19017 in chips)
Seat 2: Francisco ($18967 in chips)
Seat 3: Dylan ($18717 in chips)
Seat 4: Otto ($24017 in chips)
Seat 5: Alex ($22017 in chips)
Seat 6: Airball ($48817 in chips)
Greedo: posts the ante $17
Francisco: posts the ante $17
Dylan: posts the ante $17
Otto: posts the ante $17
Alex: posts the ante $17
Airball: posts the ante $17
Francisco: posts small blind $50
Dylan: posts big blind $100
Airball: posts straddle $200
*** HOLE CARDS ***
Dealt to Greedo [4s 4h]
Greedo: raises $400 to $600
Francisco: calls $550
Dylan: folds
Otto: calls $600
Alex: raises $2400 to $3000
Airball: folds
Greedo: calls $2400
Francisco: folds
Otto: calls $2400
*** FLOP *** [Kd 9s 6s]
Otto: checks
Alex: bets $3000
Otto: raises $18000 to $21000 and is all-in
Alex: calls $18000
Greedo: folds
*** TURN *** [Kd 9s 6s] [Ks]
*** RIVER *** [Kd 9s 6s Ks] [2s]
*** SHOW DOWN ***
Otto: shows [Tc 8d] (a pair of Kings)
Alex: shows [Ah 5d] (a pair of Kings)
Alex collected $51450 from pot
*** SUMMARY ***
Total pot $51450 | Rake $0
Board [Kd 9s 6s Ks 2s]
Seat 1: Greedo (button) folded on the Flop
Seat 2: Francisco (small blind) folded before Flop
Seat 3: Dylan (big blind) folded before Flop (didn't bet)
Seat 4: Otto showed [Tc 8d] and lost with a pair of Kings
Seat 5: Alex showed [Ah 5d] and won ($51450) with a pair of Kings
Seat 6: Airball folded before Flop (didn't bet)
```

### Math verification for example:
- Antes: 6 × $17 = $102
- SB: $50, BB: $100, Straddle: $200
- Dead money: $452
- Preflop: Greedo $600, Francisco $600 (then folded), Otto $600→$3000, Alex $3000, Greedo calls to $3000, Otto calls to $3000
- Players entering flop: Greedo ($3000), Otto ($3000), Alex ($3000)
- Preflop pot: $452 + $600(Fran, folded but put in $600) + $3000 + $3000 + $3000 = ~$10,052 (approximately — exact math depends on when Francisco folded)
- Flop: Alex $3000 + Otto $21000 + Alex calls $18000 = additional action
- Final pot: $51,450 as read from the video overlay

### Stack adjustment verification:
- BB ante = $100, 6 players, ante_per_player = $17
- Dylan (BB): real stack $18,800 → adjusted $18,800 - ($100 - $17) = $18,717
- Everyone else: real stack + $17
  - Greedo: $19,000 + $17 = $19,017
  - Francisco: $18,950 + $17 = $18,967
  - Otto: $24,000 + $17 = $24,017
  - Alex: $22,000 + $17 = $22,017
  - Airball: $48,800 + $17 = $48,817

### Call amount verification:
- Preflop facing = $200 (straddle level)
- Greedo raises to $600: increment = $600 - $200 = $400. "raises $400 to $600" ✓
- Francisco (SB, $50 committed from blind) calls $600: added = $600 - $50 = $550. "calls $550" ✓
- Otto (no prior commitment) calls $600: added = $600 - $0 = $600. "calls $600" ✓
- Alex raises to $3000: increment = $3000 - $600 = $2400. "raises $2400 to $3000" ✓
- Greedo (has $600 in) calls $3000: added = $3000 - $600 = $2400. "calls $2400" ✓
- Otto (has $600 in) calls $3000: added = $3000 - $600 = $2400. "calls $2400" ✓
- Flop: Otto raises to $21000 from $0. Alex bet $3000, Otto raises $18000 to $21000. Alex calls $18000 (has $3000 in). ✓

### Preflop action order verification (CO straddle by Airball, seat 6):
Start left of straddler → BTN(1) → SB(2) → BB(3) → UTG(4) → HJ(5) → STR(6)
= Greedo → Francisco → Dylan → Otto → Alex → Airball ✓
