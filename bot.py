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
    port = int(os.getenv("PORT", "10000"))
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)


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


def _read_csv(archive: zipfile.ZipFile, filename: str) -> list[dict[str, str]]:
    with archive.open(filename) as raw_file:
        text_file = io.TextIOWrapper(raw_file, encoding="utf-8-sig", newline="")
        return list(csv.DictReader(text_file))


async def load_gtfs() -> None:
    """Cache stop names and an ordered representative stop list for each route."""
    global _gtfs_loaded
    if _gtfs_loaded:
        return

    async with _gtfs_lock:
        if _gtfs_loaded:
            return
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for feed_url in GTFS_FEED_URLS:
                try:
                    async with session.get(feed_url) as response:
                        response.raise_for_status()
                        archive_data = await response.read()
                    with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
                        for row in _read_csv(archive, "stops.txt"):
                            stop_id = (row.get("stop_id") or "").strip()
                            stop_name = (row.get("stop_name") or "").strip()
                            if stop_id and stop_name:
                                _stop_names.setdefault(stop_id, stop_name)

                        # Build route stop sequences from one representative trip per route.
                        trips = _read_csv(archive, "trips.txt")
                        stop_times = _read_csv(archive, "stop_times.txt")
                        trip_by_id = {}
                        for trip in trips:
                            route = (trip.get("route_id") or "").strip().upper()
                            trip_id = (trip.get("trip_id") or "").strip()
                            if route and trip_id:
                                trip_by_id.setdefault(route, trip_id)
                        stops_by_trip: dict[str, list[tuple[int, str]]] = {}
                        for row in stop_times:
                            trip_id = (row.get("trip_id") or "").strip()
                            stop_id = (row.get("stop_id") or "").strip()
                            if trip_id and stop_id:
                                try:
                                    sequence = int(row.get("stop_sequence") or 0)
                                except ValueError:
                                    sequence = 0
                                stops_by_trip.setdefault(trip_id, []).append((sequence, stop_id))
                        for route, trip_id in trip_by_id.items():
                            ordered = sorted(stops_by_trip.get(trip_id, []))
                            names = [_stop_names[stop_id] for _, stop_id in ordered if stop_id in _stop_names]
                            if names:
                                _route_stops.setdefault(route, names)
                except (aiohttp.ClientError, OSError, KeyError, zipfile.BadZipFile) as exc:
                    print(f"Could not load GTFS feed {feed_url}: {exc}")

        _gtfs_loaded = True
        print(f"Loaded {len(_stop_names)} stops and {len(_route_stops)} routes from GTFS")


async def get_stop_name(stop_id: str) -> str | None:
    await load_gtfs()
    return _stop_names.get(stop_id)


def _normalize(value: str) -> str:
    return value.strip()


def _extract_distance(monitored_call: dict) -> str:
    distances = (monitored_call.get("Extensions") or {}).get("Distances") or {}
    for key in ("PresentableDistance", "DistanceFromStop", "DistanceFromCall"):
        if distances.get(key):
            return str(distances[key])
    return "N/A"


def _format_arrival_status(monitored_call: dict) -> str:
    expected = monitored_call.get("ExpectedArrivalTime")
    if expected:
        try:
            arrival = datetime.datetime.fromisoformat(expected.replace("Z", "+00:00"))
            now = datetime.datetime.now(arrival.tzinfo or datetime.timezone.utc)
            minutes = max(int((arrival - now).total_seconds() // 60), 0)
            return "🟢 Arriving now" if minutes == 0 else f"🟢 {minutes} mins"
        except ValueError:
            pass
    return f"🟡 {monitored_call['ArrivalProximityText']}" if monitored_call.get("ArrivalProximityText") else "⚪ No ETA"


async def get_bus_times(stop_id: str):
    if not MTA_KEY:
        raise RuntimeError("MTA_API_KEY environment variable not found.")
    params = {"key": MTA_KEY, "MonitoringRef": _normalize(stop_id)}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
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
                buses.append({
                    "route": str(journey.get("PublishedLineName") or journey.get("LineRef") or "Unknown"),
                    "destination": str(journey.get("DestinationName") or "Unknown destination"),
                    "status": _format_arrival_status(call),
                    "distance": _extract_distance(call),
                })
    return buses


async def get_route_buses(route: str) -> list[dict]:
    """Get live vehicles for a route using SIRI VehicleMonitoring."""
    if not MTA_KEY:
        raise RuntimeError("MTA_API_KEY environment variable not found.")
    params = {"key": MTA_KEY, "LineRef": _normalize(route), "VehicleMonitoringDetailLevel": "calls"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get("https://bustime.mta.info/api/siri/vehicle-monitoring.json", params=params) as response:
                response.raise_for_status()
                payload = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        print(f"Error fetching route data: {exc}")
        return []

    activities = payload.get("Siri", {}).get("ServiceDelivery", {}).get("VehicleMonitoringDelivery", [])
    if isinstance(activities, dict):
        activities = [activities]
    vehicles = []
    for delivery in activities:
        rows = delivery.get("VehicleActivity", [])
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            journey = row.get("MonitoredVehicleJourney", {})
            call = journey.get("MonitoredCall", {}) or {}
            if not journey:
                continue
            vehicles.append({
                "vehicle": str(journey.get("VehicleRef") or "Unknown bus"),
                "destination": str(journey.get("DestinationName") or "Destination unavailable"),
                "stop": str(call.get("StopPointName") or call.get("StopPointRef") or "Location unavailable"),
                "direction": str(journey.get("DirectionRef") or ""),
            })
    return vehicles


def create_bus_embed(route: str, vehicles: list[dict], destination: str | None = None) -> discord.Embed:
    shown = [v for v in vehicles if not destination or v["destination"] == destination]
    embed = discord.Embed(title=f"🚌 {route.upper()} Bus Service", color=0x0039A6)
    if not shown:
        embed.description = "No live buses were found for this selection."
        return embed
    lines = []
    for bus in shown[:15]:
        lines.append(f"🚌 **{bus['vehicle']}** — 📍 {bus['stop']}\n➡️ {bus['destination']}")
    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"Updated {datetime.datetime.now().strftime('%I:%M %p')} • Use Refresh for current positions")
    return embed


class BusRouteView(discord.ui.View):
    def __init__(self, route: str, vehicles: list[dict]):
        super().__init__(timeout=300)
        self.route = route
        self.vehicles = vehicles
        destinations = sorted({v["destination"] for v in vehicles if v["destination"] != "Destination unavailable"})[:24]
        options = [discord.SelectOption(label="All destinations", value="__all__")]
        options += [discord.SelectOption(label=value[:100], value=value) for value in destinations]
        self.destination_select = discord.ui.Select(placeholder="Filter by destination", options=options, row=0)
        self.destination_select.callback = self.destination_changed
        self.add_item(self.destination_select)

        refresh = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.success, row=1)
        refresh.callback = self.refresh_clicked
        self.add_item(refresh)

    async def destination_changed(self, interaction: discord.Interaction):
        value = self.destination_select.values[0]
        destination = None if value == "__all__" else value
        await interaction.response.edit_message(embed=create_bus_embed(self.route, self.vehicles, destination), view=self)

    async def refresh_clicked(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.vehicles = await get_route_buses(self.route)
        await interaction.edit_original_response(embed=create_bus_embed(self.route, self.vehicles), view=BusRouteView(self.route, self.vehicles))


class StationRefreshView(discord.ui.View):
    def __init__(self, stop_id: str, stop_name: str | None):
        super().__init__(timeout=180)
        self.stop_id, self.stop_name = stop_id, stop_name

    @discord.ui.button(label="🔄 Refresh Live Times", style=discord.ButtonStyle.success)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        data = await get_bus_times(self.stop_id)
        await interaction.edit_original_response(embed=create_station_embed(self.stop_id, self.stop_name, data), view=self)


def create_station_embed(stop_id: str, stop_name: str | None, data: list[dict]) -> discord.Embed:
    embed = discord.Embed(title="🚌 Real-Time Bus Arrivals", description=f"📍 **{stop_name or 'Unknown stop'}**\nStop ID: **{stop_id}**", color=0x0039A6)
    if data:
        for bus in data:
            embed.add_field(name=f"📌 Route **{bus['route']}**", value=f"**Destination:** {bus['destination']}\n**Status:** {bus['status']} ({bus['distance']})", inline=False)
    else:
        embed.add_field(name="⚠️ No Services", value="No upcoming buses detected for this stop.", inline=False)
    embed.set_footer(text=f"Updated {datetime.datetime.now().strftime('%I:%M %p')}")
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
