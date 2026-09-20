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

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
MTA_KEY = os.getenv("MTA_API_KEY")

health_app = Flask(__name__)


@health_app.get("/")
def health_check():
    return "MTA Bus Time bot is running", 200


def run_health_server():
    health_app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), use_reloader=False)


GTFS_FEEDS = {
    "BX": "https://web.mta.info/developers/data/nyct/bus/google_transit_bronx.zip",
    "B": "https://web.mta.info/developers/data/nyct/bus/google_transit_brooklyn.zip",
    "M": "https://web.mta.info/developers/data/nyct/bus/google_transit_manhattan.zip",
    "Q": "https://web.mta.info/developers/data/nyct/bus/google_transit_queens.zip",
    "S": "https://web.mta.info/developers/data/nyct/bus/google_transit_staten_island.zip",
    "SIM": "https://web.mta.info/developers/data/nyct/bus/google_transit_staten_island.zip",
}
FALLBACK_FEEDS = tuple(dict.fromkeys(GTFS_FEEDS.values()))
MAX_STOP_CACHE = 256
MAX_ROUTE_CACHE = 8
_stop_cache: OrderedDict[str, str] = OrderedDict()
_route_cache: OrderedDict[str, list[str]] = OrderedDict()
_gtfs_lock = asyncio.Lock()


def route_feed_urls(route: str) -> list[str]:
    route = route.upper()
    prefix = "BX" if route.startswith("BX") else "SIM" if route.startswith("SIM") else route[:1]
    preferred = GTFS_FEEDS.get(prefix)
    return [preferred] + [url for url in FALLBACK_FEEDS if url != preferred] if preferred else list(FALLBACK_FEEDS)


def csv_rows(archive: zipfile.ZipFile, filename: str):
    with archive.open(filename) as raw_file:
        text_file = io.TextIOWrapper(raw_file, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text_file)


def cache_stop(stop_id: str, name: str):
    _stop_cache.pop(stop_id, None)
    _stop_cache[stop_id] = name
    while len(_stop_cache) > MAX_STOP_CACHE:
        _stop_cache.popitem(last=False)


def cache_route(route: str, stops: list[str]):
    _route_cache.pop(route, None)
    _route_cache[route] = stops
    while len(_route_cache) > MAX_ROUTE_CACHE:
        _route_cache.popitem(last=False)


async def download_archive(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.read()


async def get_stop_name(stop_id: str) -> str | None:
    stop_id = stop_id.strip()
    if stop_id in _stop_cache:
        _stop_cache.move_to_end(stop_id)
        return _stop_cache[stop_id]

    async with _gtfs_lock:
        if stop_id in _stop_cache:
            return _stop_cache[stop_id]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            for url in FALLBACK_FEEDS:
                try:
                    data = await download_archive(session, url)
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        for row in csv_rows(archive, "stops.txt"):
                            if (row.get("stop_id") or "").strip() == stop_id:
                                name = (row.get("stop_name") or "").strip()
                                if name:
                                    cache_stop(stop_id, name)
                                    return name
                except (aiohttp.ClientError, OSError, KeyError, zipfile.BadZipFile) as exc:
                    print(f"Could not search GTFS feed {url}: {exc}")
    return None


async def get_route_stops(route: str) -> list[str]:
    route = route.upper()
    if route in _route_cache:
        _route_cache.move_to_end(route)
        return _route_cache[route]

    async with _gtfs_lock:
        if route in _route_cache:
            return _route_cache[route]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            for url in route_feed_urls(route):
                try:
                    data = await download_archive(session, url)
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        stop_names = {}
                        for row in csv_rows(archive, "stops.txt"):
                            stop_id = (row.get("stop_id") or "").strip()
                            name = (row.get("stop_name") or "").strip()
                            if stop_id and name:
                                stop_names[stop_id] = name

                        trip_id = None
                        for row in csv_rows(archive, "trips.txt"):
                            if (row.get("route_id") or "").strip().upper() == route:
                                trip_id = (row.get("trip_id") or "").strip()
                                if trip_id:
                                    break
                        if not trip_id:
                            continue

                        ordered = []
                        for row in csv_rows(archive, "stop_times.txt"):
                            if (row.get("trip_id") or "").strip() != trip_id:
                                continue
                            stop_id = (row.get("stop_id") or "").strip()
                            try:
                                sequence = int(row.get("stop_sequence") or 0)
                            except ValueError:
                                sequence = 0
                            if stop_id in stop_names:
                                ordered.append((sequence, stop_names[stop_id]))
                        stops = [name for _, name in sorted(ordered)]
                        if stops:
                            cache_route(route, stops)
                            return stops
                except (aiohttp.ClientError, OSError, KeyError, zipfile.BadZipFile) as exc:
                    print(f"Could not load route feed {url}: {exc}")
    return []


def _normalize(value: str) -> str:
    return value.strip()


def _format_arrival_status(call: dict) -> str:
    expected = call.get("ExpectedArrivalTime")
    if expected:
        try:
            arrival = datetime.datetime.fromisoformat(expected.replace("Z", "+00:00"))
            now = datetime.datetime.now(arrival.tzinfo or datetime.timezone.utc)
            minutes = max(int((arrival - now).total_seconds() // 60), 0)
            return "🟢 Arriving now" if minutes == 0 else f"🟢 {minutes} mins"
        except ValueError:
            pass
    return f"🟡 {call['ArrivalProximityText']}" if call.get("ArrivalProximityText") else "⚪ No ETA"


def _extract_distance(call: dict) -> str:
    distances = (call.get("Extensions") or {}).get("Distances") or {}
    return next((str(distances[key]) for key in ("PresentableDistance", "DistanceFromStop", "DistanceFromCall") if distances.get(key)), "N/A")


def _station_matches(route_stop: str, live_stop: str) -> bool:
    """Match GTFS names with SIRI names, which often include a cross street."""
    left = " ".join(route_stop.lower().replace("/", " ").split())
    right = " ".join(live_stop.lower().replace("/", " ").split())
    return left == right or left in right or right in left


async def get_bus_times(stop_id: str):
    if not MTA_KEY:
        raise RuntimeError("MTA_API_KEY environment variable not found.")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            params = {"key": MTA_KEY, "MonitoringRef": _normalize(stop_id)}
            async with session.get("https://bustime.mta.info/api/siri/stop-monitoring.json", params=params) as response:
                response.raise_for_status()
                payload = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        print(f"Error fetching stop data: {exc}")
        return []

    deliveries = payload.get("Siri", {}).get("ServiceDelivery", {}).get("StopMonitoringDelivery", [])
    if isinstance(deliveries, dict):
        deliveries = [deliveries]
    buses = []
    for delivery in deliveries:
        visits = delivery.get("MonitoredStopVisit", [])
        if isinstance(visits, dict):
            visits = [visits]
        for visit in visits:
            journey = visit.get("MonitoredVehicleJourney", {})
            if journey:
                call = journey.get("MonitoredCall", {})
                buses.append({"route": str(journey.get("PublishedLineName") or journey.get("LineRef") or "Unknown"), "destination": str(journey.get("DestinationName") or "Unknown destination"), "status": _format_arrival_status(call), "distance": _extract_distance(call)})
    return buses


async def get_route_buses(route: str):
    if not MTA_KEY:
        raise RuntimeError("MTA_API_KEY environment variable not found.")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            params = {"key": MTA_KEY, "LineRef": _normalize(route), "VehicleMonitoringDetailLevel": "calls"}
            async with session.get("https://bustime.mta.info/api/siri/vehicle-monitoring.json", params=params) as response:
                response.raise_for_status()
                payload = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        print(f"Error fetching route data: {exc}")
        return []

    deliveries = payload.get("Siri", {}).get("ServiceDelivery", {}).get("VehicleMonitoringDelivery", [])
    if isinstance(deliveries, dict):
        deliveries = [deliveries]
    vehicles = []
    for delivery in deliveries:
        activities = delivery.get("VehicleActivity", [])
        if isinstance(activities, dict):
            activities = [activities]
        for activity in activities:
            journey = activity.get("MonitoredVehicleJourney", {})
            call = journey.get("MonitoredCall", {}) or {}
            if journey:
                vehicles.append({"vehicle": str(journey.get("VehicleRef") or "Unknown bus"), "destination": str(journey.get("DestinationName") or "Destination unavailable"), "stop": str(call.get("StopPointName") or call.get("StopPointRef") or "Location unavailable")})
    return vehicles


def create_bus_embed(route: str, vehicles: list[dict], stations: list[str], destination=None, station=None):
    shown = [v for v in vehicles if (not destination or v["destination"] == destination) and (not station or _station_matches(station, v["stop"]))]
    destinations = sorted({v["destination"] for v in shown if v["destination"] != "Destination unavailable"})
    heading = destinations[0] if len(destinations) == 1 else "Bus Service"
    embed = discord.Embed(title=f"🚌 {route.upper()} — {heading}", color=0x0039A6)

    if station:
        embed.description = f"Station filter: **{station}**\n\n"
        if shown:
            embed.description += "\n\n".join(f"🚌 **{v['vehicle']}** — 📍 {v['stop']}\n➡️ {v['destination']}" for v in shown[:15])
        else:
            embed.description += "No live buses were found for this selection."
    elif stations:
        approaching = []
        for index, route_stop in enumerate(stations, 1):
            buses_here = [v for v in shown if _station_matches(route_stop, v["stop"])]
            if buses_here:
                bus_ids = ", ".join(v["vehicle"] for v in buses_here[:3])
                approaching.append(f"🚌 **{index}. {route_stop} — approaching** ({bus_ids})")
            else:
                approaching.append(f"**{index}.** {route_stop}")
        # Keep the complete route useful without exceeding Discord's embed limit.
        text = "\n".join(approaching)
        if len(text) > 3900:
            text = text[:3850].rsplit("\n", 1)[0] + "\n… more stations available below."
        embed.description = text
    else:
        embed.description = "No station list was found for this route."

    embed.set_footer(text=f"Updated {datetime.datetime.now().strftime('%I:%M %p')} • Use Refresh for current positions")
    return embed


class BusRouteView(discord.ui.View):
    def __init__(self, route: str, vehicles: list[dict], stations: list[str], destination=None, station_page=0, selected_station=None):
        super().__init__(timeout=120)
        self.route, self.vehicles, self.stations = route, vehicles, stations
        self.destination, self.station_page, self.selected_station = destination, station_page, selected_station
        destinations = sorted({v["destination"] for v in vehicles if v["destination"] != "Destination unavailable"})[:24]
        destination_select = discord.ui.Select(placeholder="Filter by destination", options=[discord.SelectOption(label="All destinations", value="__all__")] + [discord.SelectOption(label=d[:100], value=d) for d in destinations], row=0)
        destination_select.callback = self.destination_changed
        self.add_item(destination_select)

        start = station_page * 24
        station_options = [discord.SelectOption(label="All stations", value="__all__")] + [discord.SelectOption(label=s[:100], value=s[:100]) for s in stations[start:start + 24]]
        station_select = discord.ui.Select(placeholder="Filter by stations", options=station_options, row=1)
        station_select.callback = self.station_changed
        self.add_item(station_select)

        refresh = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.success, row=2)
        refresh.callback = self.refresh_clicked
        self.add_item(refresh)
        if start > 0 or start + 24 < len(stations):
            previous = discord.ui.Button(label="◀ Previous stations", style=discord.ButtonStyle.secondary, row=2, disabled=start == 0)
            previous.callback = self.previous_stations
            self.add_item(previous)
            next_button = discord.ui.Button(label="Next stations ▶", style=discord.ButtonStyle.secondary, row=2, disabled=start + 24 >= len(stations))
            next_button.callback = self.next_stations
            self.add_item(next_button)

    async def destination_changed(self, interaction):
        value = interaction.data["values"][0]
        self.destination = None if value == "__all__" else value
        await interaction.response.edit_message(embed=create_bus_embed(self.route, self.vehicles, self.stations, self.destination, self.selected_station), view=self)

    async def station_changed(self, interaction):
        value = interaction.data["values"][0]
        self.selected_station = None if value == "__all__" else value
        await interaction.response.edit_message(embed=create_bus_embed(self.route, self.vehicles, self.stations, self.destination, self.selected_station), view=self)

    async def refresh_clicked(self, interaction):
        await interaction.response.defer()
        vehicles = await get_route_buses(self.route)
        await interaction.edit_original_response(embed=create_bus_embed(self.route, vehicles, self.stations, self.destination, self.selected_station), view=BusRouteView(self.route, vehicles, self.stations, self.destination, self.station_page, self.selected_station))

    async def previous_stations(self, interaction):
        await interaction.response.edit_message(embed=create_bus_embed(self.route, self.vehicles, self.stations, self.destination, self.selected_station), view=BusRouteView(self.route, self.vehicles, self.stations, self.destination, self.station_page - 1, self.selected_station))

    async def next_stations(self, interaction):
        await interaction.response.edit_message(embed=create_bus_embed(self.route, self.vehicles, self.stations, self.destination, self.selected_station), view=BusRouteView(self.route, self.vehicles, self.stations, self.destination, self.station_page + 1, self.selected_station))


class StationRefreshView(discord.ui.View):
    def __init__(self, stop_id, stop_name):
        super().__init__(timeout=180)
        self.stop_id, self.stop_name = stop_id, stop_name

    @discord.ui.button(label="🔄 Refresh Live Times", style=discord.ButtonStyle.success)
    async def refresh_button(self, interaction, button):
        await interaction.response.defer()
        await interaction.edit_original_response(embed=create_station_embed(self.stop_id, self.stop_name, await get_bus_times(self.stop_id)), view=self)


def create_station_embed(stop_id, stop_name, data):
    embed = discord.Embed(title="🚌 Real-Time Bus Arrivals", description=f"📍 **{stop_name or 'Unknown stop'}**\nStop ID: **{stop_id}**", color=0x0039A6)
    for bus in data:
        embed.add_field(name=f"📌 Route **{bus['route']}**", value=f"**Destination:** {bus['destination']}\n**Status:** {bus['status']} ({bus['distance']})", inline=False)
    if not data:
        embed.add_field(name="⚠️ No Services", value="No upcoming buses detected for this stop.", inline=False)
    return embed


class BusBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        await self.tree.sync()


bot = BusBot()


@bot.tree.command(name="station", description="Get real-time bus arrival times for an MTA bus stop")
@app_commands.describe(station_number="The MTA bus stop number, for example 500003")
async def station(interaction: discord.Interaction, station_number: str):
    await interaction.response.defer()
    station_number = _normalize(station_number)
    stop_name = await get_stop_name(station_number)
    try:
        data = await get_bus_times(station_number)
    except RuntimeError as exc:
        await interaction.followup.send(str(exc))
        return
    await interaction.followup.send(embed=create_station_embed(station_number, stop_name, data), view=StationRefreshView(station_number, stop_name))


@bot.tree.command(name="bus", description="Show live buses operating on an MTA route")
@app_commands.describe(bus="The route, for example M15, Q44, or B44")
async def bus(interaction: discord.Interaction, bus: str):
    await interaction.response.defer()
    route = _normalize(bus).upper()
    try:
        vehicles, stations = await asyncio.gather(get_route_buses(route), get_route_stops(route))
    except RuntimeError as exc:
        await interaction.followup.send(str(exc))
        return
    await interaction.followup.send(embed=create_bus_embed(route, vehicles, stations), view=BusRouteView(route, vehicles, stations))


if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN environment variable not found.")
    else:
        Thread(target=run_health_server, daemon=True).start()
        bot.run(TOKEN)
