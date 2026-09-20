"""Service-type labels for MTA buses.

To switch to custom emojis later, only edit the "emoji" values below.
Discord custom emoji format: "<:name:id>" (animated: "<a:name:id>").
Note: custom emojis render in embed descriptions and field values,
but not in embed titles or footers.
"""
import re

SERVICE_TYPES = {
    "local":   {"label": "Local",              "emoji": "🟢"},
    "limited": {"label": "Limited",            "emoji": "🟡"},
    "sbs":     {"label": "Select Bus Service", "emoji": "🔵"},
    "express": {"label": "Express",            "emoji": "🟥"},
    "rush":    {"label": "Rush",               "emoji": "🟣"},
}

# BM1, BxM1, QM5, SIM1C, X27, ...
_EXPRESS = re.compile(r"^(BM|BXM|QM|SIM|X)\d")

# Rush is a route-level designation from MTA's Queens Bus Network Redesign.
# The Bus Time feed shows plain names like "Q4", so it has to be a lookup.
# This is the full list of Rush routes; update it if MTA changes the network.
RUSH_ROUTES = {
    "Q4", "Q9", "Q13", "Q27", "Q28", "Q30", "Q35", "Q36", "Q40",
    "Q46", "Q48", "Q63", "Q77", "Q84", "Q85", "Q87", "Q89", "Q111",
}

# Limited routes, same idea: plain names in the feed, so it's a lookup.
LIMITED_ROUTES = {"Q50", "Q61", "Q74", "Q90", "Q98", "Q100"}


def service_type(route_name="", destination=""):
    """Return one of: local, limited, sbs, express, rush."""
    name = str(route_name or "").upper().strip()
    dest = str(destination or "").upper()
    text = f"{name} {dest}"
    if _EXPRESS.match(name):
        return "express"
    if name.endswith(("-SBS", "+")) or re.search(r"\bSBS\b", text):
        return "sbs"
    if name in RUSH_ROUTES:
        return "rush"
    if name in LIMITED_ROUTES:
        return "limited"
    if name.endswith("-LTD") or re.search(r"\b(LTD|LIMITED)\b", text):
        return "limited"
    return "local"


def emoji(kind):
    return SERVICE_TYPES.get(kind, SERVICE_TYPES["local"])["emoji"]


def label(kind):
    return SERVICE_TYPES.get(kind, SERVICE_TYPES["local"])["label"]


def badge(kind):
    return f"{emoji(kind)} {label(kind)}"
