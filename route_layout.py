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
    "start": "<:route_start:1551236999321616404>",   # first stop of the route
    "stop": "<:route_stop:1551237000097566742>",    # a stop on the line
    "passed": "<:route_stop:1551237000097566742>",  # a stop every bus has already passed (can look different)
    "end": "<:route_end:1551236995949535415>",     # last stop of the route
    "line": "<:route_line:1551236998302539976>",    # the line between rows (inside bus cards)
    "bus": "<:route_bus:1551236995261538474>",    # a stop with a bus at it (replaces the tick)
    "arrow": "<:route_arrow_down:1554670313189408799>",  # DOWN direction header
    "up": "<:route_arrow_up_2:1554670488301735956>",  # UP direction header
    "badge": "<:route_badge:1551236994355433663>",  # in front of the route number on a bus card
    "gap": "<:route_gap:1551236997169807541>",     # a hidden stretch of stops
}
G = GLYPHS
_DEFAULT_GLYPHS = dict(GLYPHS)

# Emoji name -> the GLYPHS entries it replaces. Upload emojis with these names (server or
# application emojis) and the bot swaps them in at startup; anything missing keeps its default.
GLYPH_EMOJIS = {
    "route_start": ("start",),
    "route_stop": ("stop", "passed"),
    "route_end": ("end",),
    "route_line": ("line",),
    "route_gap": ("gap",),
    "route_bus": ("bus",),      # the marker on a stop that has a bus at it
    "route_arrow_down": ("arrow",),  # DOWN direction header
    "route_arrow_up_2": ("up",),  # UP direction header
    "route_badge": ("badge",),  # small bus icon in front of the route number
}


def set_glyph_emojis(emojis):
    """Swap in custom emojis by name. Returns the emoji names that weren't found."""
    by_name = {}
    for emoji in emojis:
        by_name.setdefault(emoji.name.lower(), emoji)
    GLYPHS.update(_DEFAULT_GLYPHS)
    missing = []
    for name, keys in GLYPH_EMOJIS.items():
        emoji = by_name.get(name)
        if not emoji:
            missing.append(name)
            continue
        for key in keys:
            GLYPHS[key] = str(emoji)
    return missing

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
        lines.append(f"{G['gap'] if i == 0 else G['line']} Bus {v.get('proximity') or v['status']}")
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


def _collapse(blocks, keep):
    """Shrink long quiet stretches: keep `keep` stops at each end and hide the middle."""
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
        if len(run) > 2 * keep + 2:
            for block in run[:keep]:
                lines.extend(block["lines"])
            lines.append(f"{G['gap']} {len(run) - 2 * keep} more stops")
            for block in run[-keep:]:
                lines.extend(block["lines"])
        else:
            for block in run:
                lines.extend(block["lines"])
        i = j
    return lines


def _normalise(text):
    return " ".join(str(text or "").lower().replace("/", " ").split())


def _text_match_score(left, right):
    """A small fuzzy score for matching live destinations to GTFS headsigns/terminals."""
    left, right = _normalise(left), _normalise(right)
    if not left or not right:
        return 0
    if left == right:
        return 100
    if left in right or right in left:
        return 80
    left_words, right_words = set(left.split()), set(right.split())
    overlap = len(left_words & right_words)
    if not overlap:
        return 0
    return int(50 * overlap / max(len(left_words), len(right_words)))


def _pattern_direction(pattern):
    """Use terminal geography for UP/DOWN; direction_id is only a fallback."""
    start_lat, end_lat = pattern.get("start_lat"), pattern.get("end_lat")
    if start_lat is not None and end_lat is not None and abs(end_lat - start_lat) > 0.001:
        return ("UP", G["up"]) if end_lat > start_lat else ("DOWN", G["arrow"])
    return ("UP", G["up"]) if str(pattern.get("direction_id")) == "0" else ("DOWN", G["arrow"])


def _as_patterns(stations):
    if stations and isinstance(stations[0], dict):
        return stations
    # Backwards compatibility for callers still passing the old single stop list.
    if stations:
        return [{
            "direction_id": "",
            "headsign": stations[-1],
            "stops": stations,
            "start_lat": None,
            "end_lat": None,
        }]
    return []


def _assign_vehicle_patterns(patterns, vehicles):
    """Assign each live bus to the most plausible GTFS pattern, once only."""
    assigned = {index: [] for index in range(len(patterns))}
    unplaced = []
    for vehicle in vehicles:
        destination = vehicle.get("destination", "")
        live_stop = vehicle.get("stop", "")
        direction_ref = str(vehicle.get("direction_ref") or "")
        scores = []
        for index, pattern in enumerate(patterns):
            stops = pattern.get("stops") or []
            terminal = stops[-1] if stops else ""
            destination_score = max(
                _text_match_score(destination, pattern.get("headsign", "")),
                _text_match_score(destination, terminal),
            )
            stop_match = any(_normalise(stop) == _normalise(live_stop) for stop in stops)
            direction_match = bool(direction_ref and direction_ref == str(pattern.get("direction_id") or ""))
            score = destination_score * 10 + (5 if direction_match else 0) + (1 if stop_match else 0)
            scores.append((score, index, destination_score, stop_match))

        eligible = [item for item in scores if item[2] > 0]
        if eligible:
            eligible.sort(key=lambda item: (-item[0], item[1]))
            best = eligible[0]
            tied = [item for item in eligible if item[0] == best[0]]
            containing_stop = [item for item in tied if item[3]]
            chosen = (containing_stop or tied)[0]
            assigned[chosen[1]].append(vehicle)
            continue

        containing = [item for item in scores if item[3]]
        if len(containing) == 1:
            assigned[containing[0][1]].append(vehicle)
        else:
            unplaced.append(vehicle)
    return assigned, unplaced


def _pattern_blocks(route, pattern, vehicles, matches):
    stops = pattern.get("stops") or []
    direction, arrow = _pattern_direction(pattern)
    terminal = pattern.get("headsign") or (stops[-1] if stops else "Unknown terminal")
    blocks = [[f"{arrow} **{direction} — To {pretty(terminal)}**"]]
    at = [[v for v in vehicles if matches(stop, v.get("stop", ""))] for stop in stops]
    first = next((i for i, buses in enumerate(at) if buses), None)
    last = len(stops) - 1
    for index, stop in enumerate(stops):
        if at[index]:
            blocks.append(_bus_block(route, stop, at[index], spacer=False))
            continue
        if index == 0:
            glyph = G["start"]
        elif index == last:
            glyph = G["end"]
        elif first is not None and index < first:
            glyph = G["passed"]
        else:
            glyph = G["stop"]
        blocks.append([f"{glyph} {pretty(stop)}"])
    return blocks


def render_route(route, stations, shown, matches, limit=3800):
    """Render route patterns; the Components V2 view supplies pagination."""
    pages = route_pages(route, stations, shown, matches, limit=limit)
    if len(pages) <= 1:
        return pages[0] if pages else ""
    return pages[0] + "\n\n*More stops are available on the paginated route view.*"


def route_pages(route, stations, shown, matches, limit=3800):
    """Render every distinct GTFS pattern and paginate without silently dropping stops."""
    patterns = _as_patterns(stations)
    if not patterns:
        return [""]

    assigned, unplaced = _assign_vehicle_patterns(patterns, shown)
    blocks = []
    for index, pattern in enumerate(patterns):
        # Each direction header starts a fresh route segment; don't draw a stray
        # vertical route-line emoji immediately above the UP/DOWN arrow.
        blocks.extend(_pattern_blocks(route, pattern, assigned[index], matches))

    pages, current, size = [], [], 0
    for block in blocks:
        text = "\n".join(block)
        block_len = len(text) + 1
        if current and size + block_len > limit:
            pages.append("\n".join(current))
            current, size = [], 0
        current.append(text)
        size += block_len
    if current or not pages:
        pages.append("\n".join(current))

    if unplaced:
        noun = "bus" if len(unplaced) == 1 else "buses"
        note = f"*+{len(unplaced)} {noun} could not be matched to a stop pattern.*"
        if len(pages[-1]) + len(note) + 2 <= limit:
            pages[-1] += "\n\n" + note
        else:
            pages.append(note)
    return pages