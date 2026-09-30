"""Explicit fleet status overrides for /bus_number.

Live Bus Time data determines whether a bus is currently reporting an active trip.
A bus absent from the live feed is shown as inactive/not reporting only when it is
present in the depot roster; absence alone does not prove retirement.

Maintain retired/training assignments here when verified from a current fleet source.
Numbers in these sets take precedence over the live-feed status.
"""

from depots import BY_NUMBER

# Add verified fleet numbers to these sets as needed. Keep them mutually exclusive.
RETIRED_BUSES = set()
TRAINING_BUSES = set()


def fleet_status(number, is_live=False):
    number = int(number)
    if number in TRAINING_BUSES:
        return "Training bus"
    if number in RETIRED_BUSES:
        return "Retired"
    if is_live:
        return "Active"
    if number in BY_NUMBER:
        return "Inactive (not currently reporting live)"
    return "Unknown (not in roster / no live trip)"
