"""MTA-style route view for /bus.

A line runs down the left, every stop is a row, and where a bus is the row
turns into a small card (status, route badge, destination) like the MTA site.

Every symbol lives in GLYPHS. To use custom emojis, replace the values with
"<:name:id>" -- they render in embed descriptions, not in titles or footers.
"""
import re

from depots import depot_badge, fleet_number
from service_types import emoji as service_emoji, label as service_label

GLYPHS = {
    "start": "┏",   # first stop of the route
    "stop": "┣",    # a stop on the line
    "passed": "┣",  # a stop every bus has already passed (can look different)
    "end": "┗",     # last stop of the route
    "line": "┃",    # the line between rows (inside bus cards)
    "bus": "🚌",    # a stop with a bus at it (replaces the tick)
    "arrow": "⬇️",  # under the bus: direction of travel
    "badge": "🚌",  # in front of the route number on a bus card
    "gap": "┇",     # a hidden stretch of stops
}
G = GLYPHS

_KEEP_UPPER = {
    "JFK", "LGA", "LIRR", "QBCC", "BQE", "FDR", "NYU", "CUNY", "MTA", "LIC",
    "NJ", "SI", "PS", "IS", "JHS", "MS", "HS", "BX", "BK", "NY", "USPS", "VA",
}
_ORDINAL = re.compile(r"^\d+(ST|ND|RD|TH)$")


def pretty(text):
    """'SPRINGFIELD BLVD/93 AV' -> 'Springfield Blvd/93 Av' (only if all caps)."""
    text = str(text or "")
    if text != text.upper():
        return text

    def fix(match):
        word = match.group(0)
        if word in _KEEP_UPPER:
            return word
        if _ORDINAL.match(word):
            return word.lower()
        if any(ch.isdigit() for ch in word):
            return word
        if word == "S" and match.start() > 0 and text[match.start() - 1] == "'":
            return "s"
        return word.capitalize()

    return re.sub(r"[A-Za-z0-9]+", fix, text)


def _bus_block(route, stop, buses, spacer=True):
    lines = [f"{G['bus']} **{pretty(stop)}**"]
    for i, v in enumerate(buses[:3]):
        lines.append(f"{G['arrow'] if i == 0 else G['line']} Bus {v.get('proximity') or v['status']}")
        number = fleet_number(v["vehicle"])
        parts = [
            f"{G['badge']} **{route}**",
            f"{service_emoji(v['service'])} {service_label(v['service'])}",
            f"#{number}" if number is not None else v["vehicle"],
            depot_badge(v["vehicle"]),
        ]
        lines.append(f"{G['line']} " + " · ".join(p for p in parts if p))
        if v["destination"] != "Destination unavailable":
            lines.append(f"{G['line']} *To {pretty(v['destination'])}*")
    if len(buses) > 3:
        lines.append(f"{G['line']} +{len(buses) - 3} more here")
    if spacer:
        lines.append(G["line"])
    return lines


def render_cards(route, vehicles, limit=3500):
    """Bus cards only (used when a station filter is on)."""
    blocks, size = [], 0
    for index, v in enumerate(vehicles):
        block = "\n".join(_bus_block(route, v["stop"], [v], spacer=False))
        if size + len(block) > limit:
            blocks.append(f"*…and {len(vehicles) - index} more buses*")
            break
        blocks.append(block)
        size += len(block) + 2
    return "\n\n".join(blocks)


def _collapse(blocks, keep_run):
    """Replace the middle of long quiet stretches with one row."""
    lines, i = [], 0
    while i < len(blocks):
        if blocks[i]["bus"]:
            lines.extend(blocks[i]["lines"])
            i += 1
            continue
        j = i
        while j < len(blocks) and not blocks[j]["bus"]:
            j += 1
        run = blocks[i:j]
        if len(run) > keep_run + 2:
            lines.extend(run[0]["lines"])
            lines.append(f"{G['gap']} {len(run) - 2} more stops")
            lines.extend(run[-1]["lines"])
        else:
            for block in run:
                lines.extend(block["lines"])
        i = j
    return lines


def render_route(route, stations, shown, matches, limit=3800):
    """The full stop list with bus cards, shrunk to fit an embed if needed."""
    at = [[v for v in shown if matches(stop, v["stop"])] for stop in stations]
    placed = {id(v) for buses in at for v in buses}
    first = next((i for i, buses in enumerate(at) if buses), None)
    last = len(stations) - 1

    blocks = []
    for i, stop in enumerate(stations):
        if at[i]:
            blocks.append({"bus": True, "lines": _bus_block(route, stop, at[i])})
            continue
        if i == 0:
            glyph = G["start"]
        elif i == last:
            glyph = G["end"]
        elif first is not None and i < first:
            glyph = G["passed"]
        else:
            glyph = G["stop"]
        blocks.append({"bus": False, "lines": [f"{glyph} {pretty(stop)}"]})

    text = ""
    for keep_run in (10 ** 6, 10, 6, 4, 3, 2, 1, 0):
        text = "\n".join(_collapse(blocks, keep_run))
        if len(text) <= limit:
            break
    else:
        text = text[:limit].rsplit("\n", 1)[0] + "\n… more stops hidden, use the station filter."

    unplaced = [v for v in shown if id(v) not in placed]
    if unplaced:
        noun = "bus" if len(unplaced) == 1 else "buses"
        text += f"\n\n*+{len(unplaced)} {noun} not on this stop list (other direction)*"
    return text
