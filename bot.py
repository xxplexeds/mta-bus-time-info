import asyncio
import csv
import datetime
import io
import os
import zipfile
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


GTFS_FEED_URLS = (
    "https://web.mta.info/developers/data/nyct/bus/google_transit_bronx.zip",
    "https://web.mta.info/developers/data/nyct/bus/google_transit_brooklyn.zip",
    "https://web.mta.info/developers/data/nyct/bus/google_transit_manhattan.zip",
    "https://web.mta.info/developers/data/nyct/bus/google_transit_queens.zip",
    "https://web.mta.info/developers/data/nyct/bus/google_transit_staten_island.zip",
    "https://web.mta.info/developers/data/busco/google_transit.zip",
)
_stop_names: dict[str, str] = {}
_route_stops: dict[str, list[str]] = {}
_gtfs_loaded = False
_gtfs_lock = asyncio.Lock()


def _read_csv(archive: zipfile.ZipFile, filename: str):
    with archive.open(filename) as raw:
        return list(csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")))


async def load_gtfs():
    global _gtfs_loaded
    if _gtfs_loaded:
        return
    async with _gtfs_lock:
        if _gtfs_loaded:
            return
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            for url in GTFS_FEED_URLS:
                try:
                    async with session.get(url) as response:
                        response.raise_for_status()
                        data = await response.read()
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        for row in _read_csv(archive, "stops.txt"):
                            stop_id = (row.get("stop_id") or "").strip()
                            name = (row.get("stop_name") or "").strip()
                            if stop_id and name:
                                _stop_names.setdefault(stop_id, name)
                        trips = _read_csv(archive, "trips.txt")
                        stop_times = _read_csv(archive, "stop_times.txt")
                        representative = {}
                        for trip in trips:
                            route = (trip.get("route_id") or "").strip().upper()
                            trip_id = (trip.get("trip_id") or "").strip()
                            if route and trip_id:
                                representative.setdefault(route, trip_id)
                        sequences: dict[str, list[tuple[int, str]]] = {}
                        for row in stop_times:
                            trip_id = (row.get("trip_id") or "").strip()
                            stop_id = (row.get("stop_id") or "").strip()
                            if trip_id and stop_id:
                                try:
                                    seq = int(row.get("stop_sequence") or 0)
                                except ValueError:
                                    seq = 0
                                sequences.setdefault(trip_id, []).append((seq, stop_id))
                        for route, trip_id in representative.items():
                            ordered = sorted(sequences.get(trip_id, []))
                            names = [_stop_names[s] for _, s in ordered if s in _stop_names]
                            if names:
                                _route_stops.setdefault(route, names)
                except (aiohttp.ClientError, OSError, KeyError, zipfile.BadZipFile) as exc:
                    print(f"Could not load GTFS feed {url}: {exc}")
        _gtfs_loaded = True
        print(f"Loaded {len(_stop_names)} stops and {len(_route_stops)} routes from GTFS")


async def get_stop_name(stop_id: str):
    await load_gtfs()
    return _stop_names.get(stop_id)


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
    return next((str(distances[k]) for k in ("PresentableDistance", "DistanceFromStop", "DistanceFromCall") if distances.get(k)), "N/A")


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


def create_bus_embed(route: str, vehicles: list[dict], destination=None, station=None):
    shown = [v for v in vehicles if (not destination or v["destination"] == destination) and (not station or v["stop"] == station)]
    embed = discord.Embed(title=f"🚌 {route.upper()} Bus Service", color=0x0039A6)
    if station:
        embed.description = f"Station filter: **{station}**"
    if not shown:
        embed.description = (embed.description or "") + "\nNo live buses were found for this selection."
    else:
        lines = [f"🚌 **{v['vehicle']}** — 📍 {v['stop']}\n➡️ {v['destination']}" for v in shown[:15]]
        embed.description = (embed.description + "\n\n" if embed.description else "") + "\n\n".join(lines)
    embed.set_footer(text=f"Updated {datetime.datetime.now().strftime('%I:%M %p')} • Use Refresh for current positions")
    return embed


class BusRouteView(discord.ui.View):
    def __init__(self, route: str, vehicles: list[dict], destination=None, station_page=0):
        super().__init__(timeout=300)
        self.route, self.vehicles = route, vehicles
        self.destination, self.station_page = destination, station_page
        destinations = sorted({v["destination"] for v in vehicles if v["destination"] != "Destination unavailable"})[:24]
        options = [discord.SelectOption(label="All destinations", value="__all__")] + [discord.SelectOption(label=d[:100], value=d) for d in destinations]
        destination_select = discord.ui.Select(placeholder="Filter by destination", options=options, row=0)
        destination_select.callback = self.destination_changed
        self.add_item(destination_select)

        stations = _route_stops.get(route.upper(), [])
        start = station_page * 24
        page_stations = stations[start:start + 24]
        station_options = [discord.SelectOption(label="All stations", value="__all__")] + [discord.SelectOption(label=s[:100], value=s[:100]) for s in page_stations]
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
        await interaction.response.edit_message(embed=create_bus_embed(self.route, self.vehicles, self.destination), view=self)

    async def station_changed(self, interaction):
        value = interaction.data["values"][0]
        station = None if value == "__all__" else value
        await interaction.response.edit_message(embed=create_bus_embed(self.route, self.vehicles, self.destination, station), view=self)

    async def refresh_clicked(self, interaction):
        await interaction.response.defer()
        self.vehicles = await get_route_buses(self.route)
        await interaction.edit_original_response(embed=create_bus_embed(self.route, self.vehicles, self.destination), view=BusRouteView(self.route, self.vehicles, self.destination, self.station_page))

    async def previous_stations(self, interaction):
        await interaction.response.edit_message(embed=create_bus_embed(self.route, self.vehicles, self.destination), view=BusRouteView(self.route, self.vehicles, self.destination, self.station_page - 1))

    async def next_stations(self, interaction):
        await interaction.response.edit_message(embed=create_bus_embed(self.route, self.vehicles, self.destination), view=BusRouteView(self.route, self.vehicles, self.destination, self.station_page + 1))


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
        vehicles = await get_route_buses(route)
        await load_gtfs()
    except RuntimeError as exc:
        await interaction.followup.send(str(exc))
        return
    await interaction.followup.send(embed=create_bus_embed(route, vehicles), view=BusRouteView(route, vehicles))


if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN environment variable not found.")
    else:
        Thread(target=run_health_server, daemon=True).start()
        bot.run(TOKEN)
