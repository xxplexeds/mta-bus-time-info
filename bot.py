import asyncio
import csv
import datetime
import io
import os
import zipfile
from collections import OrderedDict
from threading import Thread

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask

from service_types import service_type, badge
from depots import DEPOTS, depot_badge, fleet_number, set_emojis as set_depot_emojis
from fleet_status import fleet_status
from route_layout import GLYPHS as ROUTE_GLYPHS, pretty, render_cards, render_route, route_pages
try:
    from route_layout import set_glyph_emojis
except ImportError:  # an older route_layout.py without name-based emoji lookup: keep its hard-coded GLYPHS
    def set_glyph_emojis(emojis):
        return []
import presets
from service_status import parse_alerts, status_text

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
MTA_KEY = os.getenv("MTA_API_KEY")

health_app = Flask(__name__)
FEEDS = {
    "BX": "https://web.mta.info/developers/data/nyct/bus/google_transit_bronx.zip",
    "B": "https://web.mta.info/developers/data/nyct/bus/google_transit_brooklyn.zip",
    "M": "https://web.mta.info/developers/data/nyct/bus/google_transit_manhattan.zip",
    "Q": "https://web.mta.info/developers/data/nyct/bus/google_transit_queens.zip",
    "S": "https://web.mta.info/developers/data/nyct/bus/google_transit_staten_island.zip",
    "SIM": "https://web.mta.info/developers/data/nyct/bus/google_transit_staten_island.zip",
    # MTA Bus Company: a separate operator (former private franchises), covers B100/B103,
    # Bx23, the BxM express routes, and a handful of Queens routes (e.g. Q50) that the
    # regular borough zips above don't include. No single-letter prefix maps to it, so it
    # isn't picked as anyone's "preferred" feed -- it just rides along in ALL_FEEDS below
    # as a fallback every route falls through to if its usual borough feed comes up empty.
    "BUSCO": "https://web.mta.info/developers/data/busco/google_transit.zip",
}
ALL_FEEDS = tuple(dict.fromkeys(FEEDS.values()))
_stop_cache: OrderedDict[str, str] = OrderedDict()
_route_cache: OrderedDict[str, list[dict]] = OrderedDict()
_gtfs_lock = asyncio.Lock()


@health_app.get("/")
def health_check():
    return "MTA Bus Time bot is running", 200


def run_health_server():
    health_app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), use_reloader=False)


def feed_urls(route: str):
    route = route.upper()
    prefix = "BX" if route.startswith("BX") else "SIM" if route.startswith("SIM") else route[:1]
    preferred = FEEDS.get(prefix)
    return ([preferred] if preferred else []) + [u for u in ALL_FEEDS if u != preferred]


def csv_rows(archive, filename):
    with archive.open(filename) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


async def download(session, url):
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.read()


FEED_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError, KeyError, zipfile.BadZipFile)


def _find_stop_name(data, stop_id):
    # Blocking work: always call through asyncio.to_thread.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for row in csv_rows(archive, "stops.txt"):
            if (row.get("stop_id") or "").strip() == stop_id:
                name = (row.get("stop_name") or "").strip()
                if name:
                    return name
    return None


def _find_route_stops(data, route):
    """Return every distinct GTFS stop pattern for a route, including short-turn trips."""
    # Blocking work: always call through asyncio.to_thread.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        stop_info = {}
        for row in csv_rows(archive, "stops.txt"):
            stop_id = (row.get("stop_id") or "").strip()
            name = (row.get("stop_name") or "").strip()
            if not stop_id or not name:
                continue
            try:
                lat = float(row.get("stop_lat") or "")
                lon = float(row.get("stop_lon") or "")
            except ValueError:
                lat = lon = None
            stop_info[stop_id] = {"name": name, "lat": lat, "lon": lon}

        route = route.upper()
        route_ids = {route}
        try:
            for row in csv_rows(archive, "routes.txt"):
                route_id = (row.get("route_id") or "").strip()
                short_name = (row.get("route_short_name") or "").strip().upper()
                if route_id and short_name == route:
                    route_ids.add(route_id)
        except KeyError:
            pass

        valid_route_ids = {item.upper() for item in route_ids}
        trips = {}
        for row in csv_rows(archive, "trips.txt"):
            route_id = (row.get("route_id") or "").strip().upper()
            if route_id not in valid_route_ids:
                continue
            trip_id = (row.get("trip_id") or "").strip()
            if not trip_id:
                continue
            trips[trip_id] = {
                "direction_id": (row.get("direction_id") or "").strip(),
                "headsign": (row.get("trip_headsign") or "").strip(),
            }
        if not trips:
            return []

        stop_times = {trip_id: [] for trip_id in trips}
        for row in csv_rows(archive, "stop_times.txt"):
            trip_id = (row.get("trip_id") or "").strip()
            if trip_id not in stop_times:
                continue
            try:
                sequence = int(row.get("stop_sequence") or 0)
            except ValueError:
                sequence = 0
            stop_id = (row.get("stop_id") or "").strip()
            if stop_id in stop_info:
                stop_times[trip_id].append((sequence, stop_id))

        # Trips with the same direction and ordered stop IDs share a pattern.
        # Different terminal trips remain separate, even when they share most stops.
        grouped = {}
        for trip_id, trip in trips.items():
            ordered = tuple(stop_id for _, stop_id in sorted(stop_times[trip_id]))
            if len(ordered) < 2:
                continue
            headsign = trip["headsign"]
            # Keep distinct terminal/headsign variants even when their stop IDs happen
            # to match; service destinations can differ by time of day.
            key = (trip["direction_id"], ordered, headsign)
            grouped.setdefault(key, {"headsign": headsign, "stop_ids": ordered})

        patterns = []
        for (direction_id, ordered, headsign), pattern_data in grouped.items():
            info = [stop_info[stop_id] for stop_id in ordered]
            patterns.append({
                "direction_id": direction_id,
                "headsign": headsign,
                "stops": [item["name"] for item in info],
                "stop_ids": list(ordered),
                "start_lat": info[0]["lat"],
                "start_lon": info[0]["lon"],
                "end_lat": info[-1]["lat"],
                "end_lon": info[-1]["lon"],
            })
        patterns.sort(key=lambda pattern: (pattern["direction_id"], pattern["headsign"], pattern["stops"][-1]))
        return patterns

async def get_stop_name(stop_id: str):
    if stop_id in _stop_cache:
        _stop_cache.move_to_end(stop_id)
        return _stop_cache[stop_id]
    async with _gtfs_lock:
        if stop_id in _stop_cache:
            return _stop_cache[stop_id]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            for url in ALL_FEEDS:
                try:
                    data = await download(session, url)
                    name = await asyncio.to_thread(_find_stop_name, data, stop_id)
                except FEED_ERRORS:
                    continue
                if name:
                    _stop_cache[stop_id] = name
                    while len(_stop_cache) > 256:
                        _stop_cache.popitem(last=False)
                    return name
    return None


async def get_route_stops(route: str):
    route = route.upper()
    if route in _route_cache:
        _route_cache.move_to_end(route)
        return _route_cache[route]
    async with _gtfs_lock:
        if route in _route_cache:
            return _route_cache[route]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            for url in feed_urls(route):
                try:
                    data = await download(session, url)
                    stops = await asyncio.to_thread(_find_route_stops, data, route)
                except FEED_ERRORS:
                    continue
                if stops:
                    _route_cache[route] = stops
                    while len(_route_cache) > 8:
                        _route_cache.popitem(last=False)
                    return stops
    return []


def normalize(value):
    return " ".join(str(value or "").lower().replace("/", " ").split())


def matches(route_stop, live_stop):
    left, right = normalize(route_stop), normalize(live_stop)
    return left == right or left in right or right in left


def status(call):
    expected = call.get("ExpectedArrivalTime")
    if expected:
        try:
            arrival = datetime.datetime.fromisoformat(expected.replace("Z", "+00:00"))
            minutes = max(int((arrival - datetime.datetime.now(arrival.tzinfo)).total_seconds() // 60), 0)
            return "approaching" if minutes <= 2 else f"in {minutes} mins"
        except ValueError:
            pass
    return str(call.get("ArrivalProximityText") or "approaching")


async def get_stop_times(stop_id):
    if not MTA_KEY:
        raise RuntimeError("MTA_API_KEY environment variable not found.")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get("https://bustime.mta.info/api/siri/stop-monitoring.json", params={"key": MTA_KEY, "MonitoringRef": stop_id}) as response:
                response.raise_for_status()
                payload = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []
    deliveries = payload.get("Siri", {}).get("ServiceDelivery", {}).get("StopMonitoringDelivery", [])
    if isinstance(deliveries, dict):
        deliveries = [deliveries]
    result = []
    for delivery in deliveries:
        visits = delivery.get("MonitoredStopVisit", [])
        if isinstance(visits, dict):
            visits = [visits]
        for visit in visits:
            journey = visit.get("MonitoredVehicleJourney", {})
            if journey:
                route_name = str(journey.get("PublishedLineName") or journey.get("LineRef") or "Unknown")
                destination = str(journey.get("DestinationName") or "Unknown destination")
                result.append({"route": route_name, "destination": destination, "status": status(journey.get("MonitoredCall", {})), "service": service_type(route_name, destination), "vehicle": str(journey.get("VehicleRef") or "")})
    return result


async def get_route_data(route):
    """(vehicles, alerts) from one Bus Time call. alerts is None when the call failed."""
    if not MTA_KEY:
        raise RuntimeError("MTA_API_KEY environment variable not found.")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            params = {"key": MTA_KEY, "LineRef": route, "VehicleMonitoringDetailLevel": "calls"}
            async with session.get("https://bustime.mta.info/api/siri/vehicle-monitoring.json", params=params) as response:
                response.raise_for_status()
                payload = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return [], None
    deliveries = payload.get("Siri", {}).get("ServiceDelivery", {}).get("VehicleMonitoringDelivery", [])
    if isinstance(deliveries, dict):
        deliveries = [deliveries]
    result = []
    for delivery in deliveries:
        activities = delivery.get("VehicleActivity", [])
        if isinstance(activities, dict):
            activities = [activities]
        for activity in activities:
            journey = activity.get("MonitoredVehicleJourney", {})
            call = journey.get("MonitoredCall", {}) or {}
            if journey:
                destination = str(journey.get("DestinationName") or "Destination unavailable")
                distances = (call.get("Extensions") or {}).get("Distances") or {}
                proximity = str(distances.get("PresentableDistance") or call.get("ArrivalProximityText") or "")
                result.append({"proximity": proximity, "vehicle": str(journey.get("VehicleRef") or "bus"), "destination": destination, "stop": str(call.get("StopPointName") or call.get("StopPointRef") or "Location unavailable"), "direction_ref": str(journey.get("DirectionRef") or ""), "status": status(call), "service": service_type(journey.get("PublishedLineName") or journey.get("LineRef"), destination)})
    try:
        alerts = parse_alerts(payload, route)
    except (AttributeError, TypeError, ValueError, KeyError):
        alerts = None  # shown as "status unavailable" instead of breaking /bus
    return result, alerts


async def get_route_buses(route):
    return (await get_route_data(route))[0]


def _single_value(value):
    return value if isinstance(value, list) else ([value] if value else [])


def _destination_score(destination, pattern):
    left = normalize(destination)
    right = normalize(pattern.get("headsign") or "")
    terminal = normalize((pattern.get("stops") or [""])[-1])
    if not left:
        return 0
    if left == right or left == terminal:
        return 100
    if left in right or right in left or left in terminal or terminal in left:
        return 70
    words = set(left.split())
    return max(
        int(45 * len(words & set(right.split())) / max(len(words), 1)),
        int(45 * len(words & set(terminal.split())) / max(len(words), 1)),
    )


def _pattern_progress(patterns, stop_id, stop_name, destination, direction_ref=''):
    candidates = []
    for pattern in patterns:
        ids = pattern.get("stop_ids") or []
        stops = pattern.get("stops") or []
        index = ids.index(stop_id) if stop_id and stop_id in ids else None
        if index is None:
            index = next((i for i, name in enumerate(stops) if matches(name, stop_name)), None)
        if index is None:
            continue
        score = (100 if stop_id and index < len(ids) and ids[index] == stop_id else 50)
        score += _destination_score(destination, pattern)
        if direction_ref and str(pattern.get('direction_id') or '') == str(direction_ref):
            score += 15
        candidates.append((score, pattern, index))
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1], candidates[0][2]


async def _live_vehicle_by_number(number):
    if not MTA_KEY:
        raise RuntimeError("MTA_API_KEY environment variable not found.")
    errors = []
    successful_lookup = False
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=18)) as session:
        for vehicle_ref in (f"MTA NYCT_{number}", f"MTABC_{number}"):
            try:
                params = {"key": MTA_KEY, "VehicleRef": vehicle_ref, "VehicleMonitoringDetailLevel": "calls"}
                async with session.get("https://bustime.mta.info/api/siri/vehicle-monitoring.json", params=params) as response:
                    response.raise_for_status()
                    payload = await response.json()
                successful_lookup = True
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                errors.append(exc)
                continue
            deliveries = _single_value((payload.get("Siri", {}).get("ServiceDelivery", {}) or {}).get("VehicleMonitoringDelivery", []))
            for delivery in deliveries:
                for activity in _single_value(delivery.get("VehicleActivity", [])):
                    journey = activity.get("MonitoredVehicleJourney", {}) or {}
                    vehicle_ref_actual = str(journey.get("VehicleRef") or "")
                    if fleet_number(vehicle_ref_actual) != number:
                        continue
                    call = journey.get("MonitoredCall", {}) or {}
                    distances = (call.get("Extensions") or {}).get("Distances") or {}
                    onward = _single_value((journey.get("OnwardCalls") or {}).get("OnwardCall", []))
                    onward_names = [str(item.get("StopPointName") or item.get("StopPointRef") or "").strip() for item in onward]
                    line = str(journey.get("PublishedLineName") or journey.get("LineRef") or "Unknown")
                    if "_" in line:
                        line = line.split("_")[-1]
                    return {
                        "number": number, "vehicle": vehicle_ref_actual, "route": line,
                        "destination": str(journey.get("DestinationName") or "Destination unavailable"),
                        "stop": str(call.get("StopPointName") or call.get("StopPointRef") or ""),
                        "stop_id": str(call.get("StopPointRef") or ""),
                        "proximity": str(distances.get("PresentableDistance") or call.get("ArrivalProximityText") or ""),
                        "arrival_status": status(call), "direction_ref": str(journey.get("DirectionRef") or ""),
                        "onward_names": [name for name in onward_names if name],
                    }
    if not successful_lookup and errors:
        raise RuntimeError("The live MTA Bus Time lookup failed. Please try again shortly.")
    return None


async def get_bus_number_info(number):
    live = await _live_vehicle_by_number(number)
    depot = depot_badge(f"MTA NYCT_{number}") or depot_badge(f"MTABC_{number}")
    info = {
        "number": number,
        "depot": depot or "Unknown / not listed in the depot roster",
        "fleet_status": fleet_status(number, is_live=live is not None),
        "live": live, "pattern": None, "stop_index": None, "stops": [],
    }
    if not live:
        return info
    patterns = await get_route_stops(live["route"])
    pattern, index = _pattern_progress(patterns, live["stop_id"], live["stop"], live["destination"], live.get("direction_ref", ""))
    info.update({"pattern": pattern, "stop_index": index, "stops": (pattern or {}).get("stops", [])})
    return info


def _trip_progress_text(stops, index):
    """Render compact trip progress using the same custom route glyphs as /bus."""
    if not stops or index is None or not 0 <= index < len(stops):
        return "GTFS stop pattern unavailable for this trip."

    glyphs = ROUTE_GLYPHS
    last = len(stops) - 1
    visible = sorted({0, last, max(0, index - 1), index, min(last, index + 1)})
    lines = []
    previous_index = None

    for stop_index in visible:
        if previous_index is not None:
            hidden = stop_index - previous_index - 1
            if hidden > 0:
                lines.append(f"{glyphs['gap']} *{hidden} more stops*")

        name = pretty(stops[stop_index])
        if stop_index == index:
            marker = glyphs["bus"]
            label = f"{marker} **Bus here: {name}**"
        elif stop_index == 0:
            label = f"{glyphs['start']} **Start:** {name}"
        elif stop_index == last:
            label = f"{glyphs['end']} **End:** {name}"
        else:
            label = f"{glyphs['stop']} {name}"
        lines.append(label)
        previous_index = stop_index

    remaining = max(last - index, 0)
    lines.extend(["", f"**{index} stops before this stop · {remaining} stops to terminal**"])
    return "\n".join(lines)


class BusNumberLayout(discord.ui.LayoutView):
    """Components V2 layout for /bus_number, using the shared custom route glyphs."""

    def __init__(self, info):
        super().__init__(timeout=180)
        self.info = info
        number = info["number"]
        live = info.get("live")

        container = discord.ui.Container(accent_colour=V2_ACCENT)
        container.add_item(discord.ui.TextDisplay(f"## 🚌 Bus #{number}"))
        container.add_item(discord.ui.TextDisplay(
            f"**Fleet status:** {info['fleet_status']}    ·    **Depot:** {info['depot']}"
        ))
        container.add_item(discord.ui.Separator())

        if not live:
            container.add_item(discord.ui.TextDisplay(
                "⚠️ **No live trip currently reported**\n"
                "Absence from Bus Time does not prove that a bus is retired. "
                "Retired and training labels come from the verified override list."
            ))
        else:
            route_service = f"**{pretty(live['route'])}** · {badge(service_type(live['route'], live['destination']))}"
            container.add_item(discord.ui.TextDisplay(f"**Route / service**\n{route_service}"))
            container.add_item(discord.ui.TextDisplay(
                f"**Going toward**\n{pretty(live['destination'])}\n\n"
                f"**Monitored stop**\n{pretty(live['stop'] or 'Unavailable')}\n"
                f"{live['arrival_status']}" + (f" · {live['proximity']}" if live.get("proximity") else "")
            ))
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay("### Trip progress"))
            pattern, index = info.get("pattern"), info.get("stop_index")
            stops = info.get("stops") or []
            if pattern and index is not None:
                progress = _trip_progress_text(stops, index)
            else:
                upcoming = live.get("onward_names") or []
                progress = "\n".join(f"{ROUTE_GLYPHS['stop']} {pretty(name)}" for name in upcoming[:3])
                if not progress:
                    progress = "GTFS stop pattern unavailable."
                progress += "\n\n**Stops remaining to terminal:** unavailable — could not match the live trip to a GTFS stop pattern."
            container.add_item(discord.ui.TextDisplay(progress))

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"-# Updated <t:{int(datetime.datetime.now().timestamp())}:t> · Refresh for current positions"
        ))
        refresh = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.success)
        refresh.callback = self.refresh_clicked
        container.add_item(discord.ui.ActionRow(refresh))
        self.add_item(container)

    async def refresh_clicked(self, interaction):
        await interaction.response.defer()
        try:
            info = await get_bus_number_info(self.info["number"])
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.edit_original_response(view=BusNumberLayout(info))


def route_embed(route, vehicles, stations, destination=None):
    shown = [v for v in vehicles if not destination or v["destination"] == destination]
    destination_title = destination or (shown[0]["destination"] if len({v["destination"] for v in shown}) == 1 and shown else "Bus Service")
    embed = discord.Embed(title=f"🚌 {route} — {pretty(destination_title)}", color=0x0039A6)
    header = f"{badge(service_type(route))}\n\n"
    if stations:
        body = render_route(route, stations, shown, matches)
    else:
        cards = render_cards(route, shown[:15])
        body = f"No station list was found for this route, so here are the live buses:\n\n{cards}" if cards else "No station list or live buses were found for this route."
    embed.description = header + body
    embed.set_footer(text=f"Updated {datetime.datetime.now().strftime('%I:%M %p')} • Refresh for current positions")
    return embed


class BusView(discord.ui.View):
    def __init__(self, route, vehicles, stations, destination=None):
        super().__init__(timeout=120)
        self.route, self.vehicles, self.stations, self.destination = route, vehicles, stations, destination
        destinations = sorted({v["destination"] for v in vehicles if v["destination"] != "Destination unavailable"})[:24]
        dest = discord.ui.Select(placeholder="Filter by destination", options=[discord.SelectOption(label="All destinations", value="__all__")] + [discord.SelectOption(label=d[:100], value=d) for d in destinations], row=0)
        dest.callback = self.destination_changed
        self.add_item(dest)
        refresh = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.success, row=1)
        refresh.callback = self.refresh_clicked
        self.add_item(refresh)

    async def destination_changed(self, interaction):
        self.destination = interaction.data["values"][0]
        if self.destination == "__all__":
            self.destination = None
        await interaction.response.edit_message(embed=route_embed(self.route, self.vehicles, self.stations, self.destination), view=self)

    async def refresh_clicked(self, interaction):
        await interaction.response.defer()
        vehicles = await get_route_buses(self.route)
        await interaction.edit_original_response(embed=route_embed(self.route, vehicles, self.stations, self.destination), view=BusView(self.route, vehicles, self.stations, self.destination))


# ---- Components V2 layouts (used by /station and /saved_stations) ----
# Limits per message: 40 components (nested ones count) and 4000 characters of text in total.
V2_ACCENT = 0x0039A6
V2_MAX_TEXT = 3800   # leave headroom under the 4000-character text limit
V2_MAX_BUSES = 15    # each bus uses 2 components (text + separator); 15 buses + controls stays under 40


def bus_text(bus):
    depot = depot_badge(bus.get("vehicle"))
    depot_line = f"**Depot:** {depot}\n" if depot else ""
    return f"### 📌 Route {bus['route']}\n**Service:** {badge(bus['service'])}\n{depot_line}**Destination:** {bus['destination']}\n**Status:** {bus['status']}"


class StationLayout(discord.ui.LayoutView):
    def __init__(self, stop_id, stop_name, buses=(), route=None):
        super().__init__(timeout=180)
        self.stop_id, self.stop_name, self.buses, self.route = stop_id, stop_name, list(buses), route
        shown = [b for b in self.buses if not route or b["route"] == route]

        header = f"## 🚌 Real-Time Bus Arrivals\n📍 **{stop_name or 'Unknown stop'}**\nStop ID: **{stop_id}**"
        container = discord.ui.Container(accent_colour=V2_ACCENT)
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator())

        used, count = len(header) + 200, 0  # 200 characters reserved for the footer
        for bus in shown:
            text = bus_text(bus)
            if count >= V2_MAX_BUSES or used + len(text) > V2_MAX_TEXT:
                break
            if count:
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(text))
            used += len(text)
            count += 1
        if not shown:
            container.add_item(discord.ui.TextDisplay("⚠️ **No Services**\nNo upcoming buses detected for this stop."))

        hidden = len(shown) - count
        # <t:...:t> shows each viewer's own local time
        footer = f"-# Updated <t:{int(datetime.datetime.now().timestamp())}:t>" + (f" · +{hidden} more not shown, use the filter" if hidden else "")
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(footer))

        routes = sorted({b["route"] for b in self.buses})[:24]
        if routes:
            select = discord.ui.Select(placeholder="Filter by bus", options=[discord.SelectOption(label="All buses", value="__all__", default=route is None)] + [discord.SelectOption(label=r[:100], value=r, default=r == route) for r in routes])
            select.callback = self.route_changed
            container.add_item(discord.ui.ActionRow(select))

        refresh = discord.ui.Button(label="🔄 Refresh Live Times", style=discord.ButtonStyle.success)
        refresh.callback = self.refresh_clicked
        buttons = [refresh]
        if stop_name or self.buses:  # nothing to save for an unknown stop
            save = discord.ui.Button(label="⭐ Save Stop", style=discord.ButtonStyle.secondary)
            save.callback = self.save_clicked
            buttons.append(save)
        container.add_item(discord.ui.ActionRow(*buttons))
        self.add_item(container)

    async def route_changed(self, interaction):
        value = interaction.data["values"][0]
        await interaction.response.edit_message(view=StationLayout(self.stop_id, self.stop_name, self.buses, None if value == "__all__" else value))

    async def refresh_clicked(self, interaction):
        await interaction.response.defer()
        buses = await get_stop_times(self.stop_id)
        await interaction.edit_original_response(view=StationLayout(self.stop_id, self.stop_name, buses, self.route))

    async def save_clicked(self, interaction):
        # Saves to whoever clicked, and replies privately so the shared message isn't changed.
        result = await asyncio.to_thread(presets.pin, interaction.user.id, self.stop_id, self.stop_name)
        name = self.stop_name or self.stop_id
        message = {
            "saved": f"⭐ Saved **{name}**. Open it with /saved_stations, or pick it when you type in /station.",
            "exists": f"**{name}** is already in your saved stops. Manage them with /saved_stations.",
            "full": f"You can save up to {presets.MAX_PINNED} stops. Remove one with /saved_stations first.",
        }.get(result, "Couldn't save that right now, try again in a bit.")
        await interaction.response.send_message(message, ephemeral=True)


class SavedLayout(discord.ui.LayoutView):
    def __init__(self, stops):
        super().__init__(timeout=180)
        self.names = dict(stops)
        container = discord.ui.Container(accent_colour=V2_ACCENT)
        container.add_item(discord.ui.TextDisplay("## ⭐ Saved Stops"))
        container.add_item(discord.ui.Separator())
        if stops:
            container.add_item(discord.ui.TextDisplay("\n".join(f"**{name or 'Unknown stop'}** · Stop ID {stop_id}" for stop_id, name in stops)))
            container.add_item(discord.ui.Separator())
            for placeholder, callback in (("Open a saved stop", self.open_stop), ("Remove a saved stop", self.remove_stop)):
                select = discord.ui.Select(placeholder=placeholder, options=[discord.SelectOption(label=(name or stop_id)[:100], description=f"Stop {stop_id}", value=stop_id) for stop_id, name in stops[:presets.MAX_PINNED]])
                select.callback = callback
                container.add_item(discord.ui.ActionRow(select))
        else:
            container.add_item(discord.ui.TextDisplay("You haven't saved any stops yet. Use **⭐ Save Stop** under a /station result."))
        self.add_item(container)

    async def open_stop(self, interaction):
        stop_id = interaction.data["values"][0]
        await interaction.response.defer()
        try:
            buses = await get_stop_times(stop_id)
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        name = self.names.get(stop_id) or await get_stop_name(stop_id)
        await interaction.edit_original_response(view=StationLayout(stop_id, name, buses))

    async def remove_stop(self, interaction):
        await asyncio.to_thread(presets.unpin, interaction.user.id, interaction.data["values"][0])
        stops = await asyncio.to_thread(presets.pinned, interaction.user.id)
        await interaction.response.edit_message(view=SavedLayout(stops))


class RouteLayout(discord.ui.LayoutView):
    """/bus as a Components V2 card: header, service status, the stop list with buses, and the controls."""

    def __init__(self, route, vehicles, stations, alerts, destination=None, page=0):
        super().__init__(timeout=120)
        self.route, self.vehicles, self.stations, self.alerts, self.destination = route, vehicles, stations, alerts, destination
        shown = [v for v in vehicles if not destination or v["destination"] == destination]
        title = destination or (shown[0]["destination"] if len({v["destination"] for v in shown}) == 1 and shown else "Bus Service")
        header = f"## 🚌 {route} — {pretty(title)}\n{badge(service_type(route))}"
        status_block = status_text(alerts)
        # Discord allows 4000 characters of text per message: give the stop list whatever the rest leaves
        limit = max(1200, V2_MAX_TEXT + 100 - len(header) - len(status_block) - 220)
        if stations:
            pages = route_pages(route, stations, shown, matches, limit=limit)
            self.page = max(0, min(page, len(pages) - 1))
            body = pages[self.page]
            page_note = f" · Page {self.page + 1}/{len(pages)}" if len(pages) > 1 else ""
        else:
            self.page = 0
            pages = [""]
            cards = render_cards(route, shown[:15], limit=min(3500, limit))
            body = f"No station list was found for this route, so here are the live buses:\n\n{cards}" if cards else "No station list or live buses were found for this route."
            page_note = ""
        self.total_pages = len(pages)
        footer = f"-# Updated <t:{int(datetime.datetime.now().timestamp())}:t> · Refresh for current positions{page_note}"

        container = discord.ui.Container(accent_colour=V2_ACCENT)
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.TextDisplay(status_block))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(body))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(footer))

        destinations = sorted({v["destination"] for v in vehicles if v["destination"] != "Destination unavailable"})[:24]
        select = discord.ui.Select(placeholder="Filter by destination", options=[discord.SelectOption(label="All destinations", value="__all__", default=destination is None)] + [discord.SelectOption(label=d[:100], value=d, default=d == destination) for d in destinations])
        select.callback = self.destination_changed
        container.add_item(discord.ui.ActionRow(select))
        if self.total_pages > 1:
            prev_button = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
            prev_button.callback = self.prev_page
            next_button = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1)
            next_button.callback = self.next_page
            container.add_item(discord.ui.ActionRow(prev_button, next_button))
        refresh = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.success)
        refresh.callback = self.refresh_clicked
        container.add_item(discord.ui.ActionRow(refresh))
        self.add_item(container)

    async def destination_changed(self, interaction):
        value = interaction.data["values"][0]
        await interaction.response.edit_message(view=RouteLayout(self.route, self.vehicles, self.stations, self.alerts, None if value == "__all__" else value))

    async def prev_page(self, interaction):
        await interaction.response.edit_message(view=RouteLayout(self.route, self.vehicles, self.stations, self.alerts, self.destination, self.page - 1))

    async def next_page(self, interaction):
        await interaction.response.edit_message(view=RouteLayout(self.route, self.vehicles, self.stations, self.alerts, self.destination, self.page + 1))

    async def refresh_clicked(self, interaction):
        await interaction.response.defer()
        vehicles, alerts = await get_route_data(self.route)
        await interaction.edit_original_response(view=RouteLayout(self.route, vehicles, self.stations, alerts, self.destination, self.page))


def station_embed(stop_id, name, buses):
    embed = discord.Embed(title="🚌 Real-Time Bus Arrivals", description=f"📍 **{name or 'Unknown stop'}**\nStop ID: **{stop_id}**", color=0x0039A6)
    for bus in buses:
        depot = depot_badge(bus.get("vehicle"))
        depot_line = f"**Depot:** {depot}\n" if depot else ""
        embed.add_field(name=f"📌 Route **{bus['route']}**", value=f"**Service:** {badge(bus['service'])}\n{depot_line}**Destination:** {bus['destination']}\n**Status:** {bus['status']}", inline=False)
    if not buses:
        embed.add_field(name="⚠️ No Services", value="No upcoming buses detected for this stop.", inline=False)
    return embed


class BusBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        await refresh_depot_emojis()

    async def on_guild_emojis_update(self, guild, before, after):
        await refresh_depot_emojis()


bot = BusBot()


async def refresh_depot_emojis():
    found = list(bot.emojis)  # emojis from servers the bot is in
    try:
        found += await bot.fetch_application_emojis()  # emojis uploaded to the bot itself
    except Exception as exc:
        print(f"Could not fetch application emojis: {exc}")
    missing = set_depot_emojis(found)
    print(f"Depot emojis matched: {len(DEPOTS) - len(missing)}/{len(DEPOTS)}")
    missing_glyphs = set_glyph_emojis(found)
    if missing_glyphs:
        print("Route line emojis not found (using text symbols): " + ", ".join(missing_glyphs))
    if missing:
        print("No emoji found for: " + ", ".join(missing))
        print("Emoji names I can see: " + ", ".join(sorted(e.name for e in found)[:60]))


def everywhere(func):
    """Let a command work in servers, DMs and group DMs, including when the app is installed to a user account."""
    func = app_commands.allowed_installs(guilds=True, users=True)(func)
    return app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(func)


@bot.tree.command(name="station", description="Get real-time bus arrival times for an MTA bus stop")
@everywhere
@app_commands.describe(station_number="The MTA bus stop number, for example 500003")
async def station(interaction: discord.Interaction, station_number: str):
    await interaction.response.defer()
    stop_id = normalize(station_number)
    try:
        name, buses = await asyncio.gather(get_stop_name(stop_id), get_stop_times(stop_id))
    except RuntimeError as exc:
        await interaction.followup.send(str(exc))
        return
    await interaction.followup.send(view=StationLayout(stop_id, name, buses))
    if name or buses:  # only remember stops that turned out to be real
        await asyncio.to_thread(presets.save, interaction.user.id, stop_id, name)


@station.autocomplete("station_number")
async def station_autocomplete(interaction: discord.Interaction, current: str):
    saved = await asyncio.to_thread(presets.pinned, interaction.user.id, current)
    recents = await asyncio.to_thread(presets.recent, interaction.user.id, current)
    seen = {stop_id for stop_id, _ in saved}
    rows = [("⭐ ", s, n) for s, n in saved] + [("", s, n) for s, n in recents if s not in seen]
    return [app_commands.Choice(name=(f"{star}{name} ({stop_id})" if name else f"{star}{stop_id}")[:100], value=stop_id) for star, stop_id, name in rows[:25]]


@bot.tree.command(name="saved_stations", description="See your saved bus stops and open their live times")
@everywhere
async def saved_stations(interaction: discord.Interaction):
    stops = await asyncio.to_thread(presets.pinned, interaction.user.id)
    await interaction.response.send_message(view=SavedLayout(stops), ephemeral=True)


@bot.tree.command(name="bus", description="Show live buses operating on an MTA route")
@everywhere
@app_commands.describe(bus="The route, for example M15, Q44, or B44")
async def bus(interaction: discord.Interaction, bus: str):
    await interaction.response.defer()
    route = normalize(bus).upper()
    try:
        (vehicles, alerts), stations = await asyncio.gather(get_route_data(route), get_route_stops(route))
    except RuntimeError as exc:
        await interaction.followup.send(str(exc))
        return
    await interaction.followup.send(view=RouteLayout(route, vehicles, stations, alerts))


@bot.tree.command(name="bus_number", description="Look up a bus fleet number and its live trip progress")
@everywhere
@app_commands.describe(bus_number="Fleet number, for example 8052")
async def bus_number(interaction: discord.Interaction, bus_number: str):
    raw_number = str(bus_number).strip()
    if not raw_number.isdigit():
        await interaction.response.send_message("Enter the numeric fleet number only, for example 8052.", ephemeral=True)
        return
    number = int(raw_number)
    if number < 1 or number > 99999:
        await interaction.response.send_message("That doesn't look like a valid fleet number.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        info = await get_bus_number_info(number)
    except RuntimeError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    await interaction.followup.send(view=BusNumberLayout(info), ephemeral=False)


if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN environment variable not found.")
    else:
        Thread(target=run_health_server, daemon=True).start()
        bot.run(TOKEN)
