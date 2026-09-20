"""Route service status for /bus.

Bus Time adds a SituationExchangeDelivery section to its VehicleMonitoring response
when a route has active service alerts, so the alerts come from the request /bus
already makes -- no extra call or key.

That feed has no "planned vs unplanned" flag, so the kind of alert is worked out from
the alert's condition and wording. It is a best guess, not an official category.
Tune the word lists below if you see alerts landing in the wrong bucket.
"""
import datetime
import re

from discord.utils import escape_markdown, escape_mentions

# kind -> (rank, emoji, label). Higher rank = shown first / decides the headline.
STATUS = {
    "suspended": (5, "🔴", "Service Suspended"),
    "delays": (4, "🟡", "Delays"),
    "planned": (3, "🟠", "Planned Work"),
    "detour": (2, "🟠", "Detour"),
    "change": (1, "🔵", "Service Change"),
}
GOOD = ("🟢", "Good Service")
UNKNOWN = ("⚪", "Service status unavailable")

SUSPENDED_WORDS = ("no service", "suspended", "not running")
DELAY_WORDS = ("delay",)
PLANNED_WORDS = (
    "planned", "construction", "road work", "roadwork", "street fair", "street closure",
    "parade", "marathon", "special event", "maintenance", "utility work", "paving",
)
DETOUR_WORDS = ("detour",)

MAX_SHOWN = 2       # alert lines listed under the headline
CLIP = 160          # characters kept from each alert's text


def _list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value):
    """Alert text can arrive as a string, {"value": ...}, or a list of those."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("value") or value.get("#text") or value.get("text") or ""
    return " ".join(str(value or "").split())


def _time(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def line_key(ref):
    """'MTA NYCT_Q44+' / 'Q44-SBS' / 'q44' -> 'Q44' so route names can be compared."""
    name = re.sub(r"[^A-Z0-9]", "", str(ref or "").split("_")[-1].upper())
    return re.sub(r"(SBS|LTD)$", "", name)


def classify(number, condition, text):
    """Return one of the STATUS kinds for a single alert."""
    cond = re.sub(r"[^a-z]", "", str(condition or "").lower())
    words = f"{text} {number}".lower()
    if "noservice" in cond or any(w in words for w in SUSPENDED_WORDS):
        return "suspended"
    if "delay" in cond or any(w in words for w in DELAY_WORDS):
        return "delays"
    if any(w in words for w in PLANNED_WORDS):
        return "planned"
    if "divert" in cond or any(w in words for w in DETOUR_WORDS):
        return "detour"
    return "change"


def parse_alerts(payload, route):
    """Active alerts for `route` from a VehicleMonitoring payload: [{"kind", "summary"}], worst first."""
    deliveries = _list((payload.get("Siri") or {}).get("ServiceDelivery", {}).get("SituationExchangeDelivery"))
    want, now, found = line_key(route), datetime.datetime.now().astimezone(), {}
    for delivery in deliveries:
        for situation in _list((delivery.get("Situations") or {}).get("PtSituationElement")):
            number = str(situation.get("SituationNumber") or "")
            journeys = _list(((situation.get("Affects") or {}).get("VehicleJourneys") or {}).get("AffectedVehicleJourney"))
            lines = {line_key(j.get("LineRef")) for j in journeys if j.get("LineRef")}
            if lines and want not in lines:
                continue  # an alert for other routes that shares a stop with this one
            end = _time((situation.get("PublicationWindow") or {}).get("EndTime"))
            if end and (end if end.tzinfo else end.astimezone()) < now:
                continue
            summary = _text(situation.get("Summary")) or _text(situation.get("Description"))
            consequences = _list((situation.get("Consequences") or {}).get("Consequence"))
            condition = next((c.get("Condition") for c in consequences if c.get("Condition")), "")
            kind = classify(number, condition, f"{summary} {_text(situation.get('Description'))}")
            found[number or summary] = {"kind": kind, "summary": summary}
    return sorted(found.values(), key=lambda a: -STATUS[a["kind"]][0])


def _safe(text, limit=CLIP):
    text = text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return escape_mentions(escape_markdown(text))


def status_text(alerts):
    """Markdown for the status block. alerts=None means the lookup failed; [] means all clear."""
    if alerts is None:
        emoji, label = UNKNOWN
        return f"{emoji} **{label}**"
    if not alerts:
        emoji, label = GOOD
        return f"{emoji} **{label}** · no active alerts"
    _, emoji, label = STATUS[alerts[0]["kind"]]
    count = f" · {len(alerts)} active alerts" if len(alerts) > 1 else ""
    lines = [f"{emoji} **{label}**{count}"]
    for alert in alerts[:MAX_SHOWN]:
        lines.append(f"> {STATUS[alert['kind']][1]} {_safe(alert['summary'])}")
    if len(alerts) > MAX_SHOWN:
        lines.append(f"-# +{len(alerts) - MAX_SHOWN} more")
    return "\n".join(lines)
