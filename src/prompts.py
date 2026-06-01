HAND_PARSE_SYSTEM_PROMPT = """You are a poker hand history parser. You extract structured hand data from poker livestream commentary transcripts.

You will receive a chunk of transcript from a poker livestream. Your job is to identify each complete hand and extract all available information into structured JSON.

RULES:
1. Each hand starts when the commentator mentions a "new hand", "button moves", or describes blinds being posted.
2. A hand ends at showdown, or when the commentator says a player "takes it down" / "wins the pot" preflop or on a street.
3. Extract EVERY action in order: posts, folds, calls, bets, raises, checks, all-ins.
4. Card notation: use standard shorthand. "king of diamonds" = Kd, "ace of spades" = As, "two of hearts" = 2h, "ten of clubs" = Tc, etc.
5. Convert all spoken amounts to integers: "fifteen hundred" = 1500, "seventy five hundred" = 7500, "fourteen five fifty" = 14550, "five thousand two fifty" = 5250.
6. If the commentator mentions seat numbers, record them. If not, infer position from action order.
7. Positions: UTG, UTG+1 (or MP), HJ (hijack), CO (cutoff), BTN (button), SB (small blind), BB (big blind).
8. If a player's hole cards are mentioned, include them. If not, leave as null.
9. Record the board cards for each street as they are announced.
10. If the commentator mentions stack sizes, capture them.

OUTPUT FORMAT:
Return a JSON object with a "hands" array. Each hand object has this structure:

```json
{
  "hands": [
    {
      "hand_number": 1,
      "timestamp_start": "00:14:32",
      "timestamp_end": "00:16:29",
      "game": {
        "stakes": "50/100",
        "ante": 500,
        "game_type": "NL Hold'em",
        "max_players": null
      },
      "button_seat": 4,
      "players": [
        {
          "name": "Wesley",
          "seat": null,
          "position": "SB",
          "stack": 62000,
          "hole_cards": null
        },
        {
          "name": "Greedo",
          "seat": null,
          "position": "BB",
          "stack": 97800,
          "hole_cards": ["Kd", "8d"]
        }
      ],
      "action": {
        "preflop": [
          {"player": "Wesley", "action": "posts_sb", "amount": 250},
          {"player": "Greedo", "action": "posts_bb", "amount": 500},
          {"player": "Otto", "action": "raises", "amount": 1500},
          {"player": "Handz", "action": "calls", "amount": 1500},
          {"player": "Wesley", "action": "folds"},
          {"player": "Greedo", "action": "calls", "amount": 1500}
        ],
        "flop": [
          {"player": "Greedo", "action": "bets", "amount": 3000},
          {"player": "Otto", "action": "folds"},
          {"player": "Handz", "action": "calls", "amount": 3000}
        ],
        "turn": [
          {"player": "Greedo", "action": "bets", "amount": 7500},
          {"player": "Handz", "action": "calls", "amount": 7500}
        ],
        "river": [
          {"player": "Greedo", "action": "bets", "amount": 14550},
          {"player": "Handz", "action": "calls", "amount": 14550}
        ]
      },
      "board": {
        "flop": ["2h", "8d", "3c"],
        "turn": "Kd",
        "river": "7s"
      },
      "showdown": [
        {
          "player": "Greedo",
          "hole_cards": ["Kd", "8d"],
          "hand_description": "two pair kings and eights",
          "result": "wins"
        },
        {
          "player": "Handz",
          "hole_cards": ["9s", "9c"],
          "hand_description": "pair of nines",
          "result": "loses"
        }
      ],
      "pot": {
        "total": 55350
      },
      "confidence": {
        "overall": 0.95,
        "flags": ["exact suits of Handz's nines not stated - inferred"]
      }
    }
  ]
}
```

CONFIDENCE SCORING:
- Rate your overall confidence 0.0-1.0 for each hand.
- In the "flags" array, note anything you're uncertain about:
  - Exact suits of cards not explicitly stated
  - Ambiguous bet amounts ("puts in a bet" with no number)
  - Unclear action (did they check or was it just not mentioned?)
  - Players whose positions you inferred rather than heard stated
  - Missing stack sizes
- These flags tell the video verification layer exactly which frames to pull.

IMPORTANT:
- Only output complete hands. If a hand is cut off mid-action, do NOT include it.
- If the commentator mentions an ante, include it in the game object.
- Spoken numbers can be ambiguous: "fourteen five fifty" = 14,550 not 1,450. Use context (pot size, stack sizes) to disambiguate.
- Return ONLY valid JSON. No markdown, no explanation, no preamble.
"""

HAND_PARSE_USER_PROMPT = """Parse the following poker livestream transcript into structured hand histories.

TRANSCRIPT:
{transcript_chunk}

Return the JSON object with all hands found in this chunk."""


VISION_FRAME_PROMPT = """You are analyzing a frame from a poker livestream. Extract all visible game state information.

Look for and extract:
1. Player names and their stack sizes (usually shown in player boxes at bottom of screen)
2. Hole cards for each player (shown face-up next to their name)
3. Board cards (community cards shown in the center)
4. Pot size (usually shown center-right)
5. Current bet amounts (shown near each player)
6. Player positions (BTN, SB, BB, etc. - sometimes labeled)
7. Action text (BET, RAISE, CALL, FOLD, CHECK, ALL-IN)
8. Stakes/game info (usually shown near the pot)

Return a JSON object:
```json
{
  "timestamp": "{timestamp}",
  "players": [
    {
      "name": "PlayerName",
      "stack": 50000,
      "hole_cards": ["Kh", "Qd"],
      "position": "HJ",
      "current_bet": 1500,
      "action_text": "BET"
    }
  ],
  "board": ["2h", "8d", "3c"],
  "pot": 5250,
  "stakes": "50/100",
  "confidence": 0.95,
  "notes": "any ambiguities or partial reads"
}
```

Card notation: Rank + suit letter. A=Ace, K=King, Q=Queen, J=Jack, T=Ten, 9-2 as numbers. s=spades, h=hearts, d=diamonds, c=clubs.

Return ONLY valid JSON."""


RECONCILE_PROMPT = """You have two data sources for the same poker hand:

1. TRANSCRIPT PARSE (from commentary):
{transcript_data}

2. FRAME READS (from video overlay at key timestamps):
{frame_data}

Reconcile these into a single authoritative hand history. Rules:
- For bet amounts: trust the frame read (exact pixels) over transcript (spoken words) when they disagree.
- For cards: trust the frame read. Card graphics are unambiguous.
- For action sequence: trust the transcript. The commentator narrates in real-time order.
- For player names: trust the frame read (exact text on screen).
- For stack sizes: trust the frame read.
- Note any irreconcilable conflicts in the confidence.flags array.

Return the reconciled hand in the same JSON format as the transcript parser output."""
