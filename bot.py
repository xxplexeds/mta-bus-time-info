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
from depots import DEPOTS, depot_badge, set_emojis as set_depot_emojis
from route_layout import pretty, render_cards, render_route

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
}
ALL_FEEDS = tuple(dict.fromkeys(FEEDS.values()))
_stop_cache: OrderedDict[str, str] = OrderedDict()
_route_cache: OrderedDict[str, list[str]] = OrderedDict()
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
    # Blocking work: always call through asyncio.to_thread.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = {}
        for row in csv_rows(archive, "stops.txt"):
            stop_id, name = (row.get("stop_id") or "").strip(), (row.get("stop_name") or "").strip()
            if stop_id and name:
                names[stop_id] = name
        trip_id = None
        for row in csv_rows(archive, "trips.txt"):
            if (row.get("route_id") or "").strip().upper() == route:
                trip_id = (row.get("trip_id") or "").strip()
                if trip_id:
                    break
        if not trip_id:
            return []
        ordered = []
        for row in csv_rows(archive, "stop_times.txt"):
            if (row.get("trip_id") or "").strip() == trip_id:
                try:
                    sequence = int(row.get("stop_sequence") or 0)
                except ValueError:
                    sequence = 0
                stop_id = (row.get("stop_id") or "").strip()
                if stop_id in names:
                    ordered.append((sequence, names[stop_id]))
        return [name for _, name in sorted(ordered)]


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


async def get_route_buses(route):
    if not MTA_KEY:
        raise RuntimeError("MTA_API_KEY environment variable not found.")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            params = {"key": MTA_KEY, "LineRef": route, "VehicleMonitoringDetailLevel": "calls"}
            async with session.get("https://bustime.mta.info/api/siri/vehicle-monitoring.json", params=params) as response:
                response.raise_for_status()
                payload = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []
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
                result.append({"proximity": proximity, "vehicle": str(journey.get("VehicleRef") or "bus"), "destination": destination, "stop": str(call.get("StopPointName") or call.get("StopPointRef") or "Location unavailable"), "status": status(call), "service": service_type(journey.get("PublishedLineName") or journey.get("LineRef"), destination)})
    return result


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


class StationView(discord.ui.View):
    def __init__(self, stop_id, stop_name):
        super().__init__(timeout=180)
        self.stop_id, self.stop_name = stop_id, stop_name

    @discord.ui.button(label="🔄 Refresh Live Times", style=discord.ButtonStyle.success)
    async def refresh(self, interaction, button):
        await interaction.response.defer()
        await interaction.edit_original_response(embed=station_embed(self.stop_id, self.stop_name, await get_stop_times(self.stop_id)), view=self)


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
    if missing:
        print("No emoji found for: " + ", ".join(missing))
        print("Emoji names I can see: " + ", ".join(sorted(e.name for e in found)[:60]))


@bot.tree.command(name="station", description="Get real-time bus arrival times for an MTA bus stop")
@app_commands.describe(station_number="The MTA bus stop number, for example 500003")
async def station(interaction: discord.Interaction, station_number: str):
    await interaction.response.defer()
    stop_id = normalize(station_number)
    try:
        name, buses = await asyncio.gather(get_stop_name(stop_id), get_stop_times(stop_id))
    except RuntimeError as exc:
        await interaction.followup.send(str(exc))
        return
    await interaction.followup.send(embed=station_embed(stop_id, name, buses), view=StationView(stop_id, name))


@bot.tree.command(name="bus", description="Show live buses operating on an MTA route")
@app_commands.describe(bus="The route, for example M15, Q44, or B44")
async def bus(interaction: discord.Interaction, bus: str):
    await interaction.response.defer()
    route = normalize(bus).upper()
    try:
        vehicles, stations = await asyncio.gather(get_route_buses(route), get_route_stops(route))
    except RuntimeError as exc:
        await interaction.followup.send(str(exc))
        return
    await interaction.followup.send(embed=route_embed(route, vehicles, stations), view=BusView(route, vehicles, stations))


if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN environment variable not found.")
    else:
        Thread(target=run_health_server, daemon=True).start()
        bot.run(TOKEN)
