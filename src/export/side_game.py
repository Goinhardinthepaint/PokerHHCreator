"""
Render a hand's bounty / squid side games as the `*** SIDE GAME ***` block.

The notation is a contract with Cole's separate database parser — see
docs/SIDE_GAME_HH_FORMAT.md. The builder (frontend/src/sideGames.js) works out
who won, streaks, buttons and every payment, and sends them as `hand["side_game"]`;
this module only writes them down, in exactly the spec's line grammar.
"""


def _money(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:.2f}"


def _who(seat, base) -> str:
    return f"Seat {int(seat)} ({base})" if seat is not None else f"Away ({base})"


def side_game_lines(sg) -> list[str]:
    """Lines for the SIDE GAME section, or [] when no side game was on."""
    if not sg or not (sg.get("bounty") or sg.get("squid")):
        return []
    out = ["*** SIDE GAME ***"]

    b = sg.get("bounty")
    if b:
        levels = {int(k): v for k, v in (b.get("levels") or {}).items()}
        out.append(
            "Bounty schedule: "
            + " ".join(f"{lvl}=${_money(levels[lvl])}" for lvl in sorted(levels))
            + f" then +${_money(b.get('step', 0))}"
        )
        for r in b.get("rows") or []:
            out.append(
                f"Bounty streak: {_who(r['seat'], r['base'])} "
                f"{int(r['before'])} -> {int(r['after'])} {r['result']}"
            )
        for p in b.get("payouts") or []:
            out.append(
                f"Bounty payout: {_who(p['payer_seat'], p['payer'])} pays "
                f"{_who(p['winner_seat'], p['winner'])} ${_money(p['amount'])} "
                f"for {int(p['streak'])} in a row"
            )

    s = sg.get("squid")
    if s:
        out.append(
            f"Squid round: {int(s['round'])}, {int(s['total'])} buttons, "
            f"${_money(s['value'])} per button, {s['mode']}"
        )
        out.append(f"Squid buttons left: {int(s['left_before'])} -> {int(s['left_after'])}")
        for r in s.get("rows") or []:
            out.append(
                f"Squid buttons: {_who(r.get('seat'), r['base'])} "
                f"{int(r['before'])} -> {int(r['after'])}" + (" won" if r.get("won") else "")
            )
        if s.get("settled"):
            k = sum(1 for r in s.get("rows") or [] if r.get("seat") is not None and int(r["after"]) == 0)
            out.append(
                f"Squid settled: {k} player{'' if k == 1 else 's'} without a button "
                f"{'pays' if k == 1 else 'pay'} ${_money(s['value'])} per button held"
            )
            for p in s.get("payouts") or []:
                b_ = int(p["buttons"])
                out.append(
                    f"Squid payout: {_who(p['payer_seat'], p['payer'])} pays "
                    f"{_who(p.get('holder_seat'), p['holder'])} ${_money(p['amount'])} "
                    f"for {b_} button{'' if b_ == 1 else 's'}"
                )
    return out
