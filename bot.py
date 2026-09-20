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

# Render Web Services expect the process to listen on PORT.
health_app = Flask(__name__)


@health_app.get("/")
def health_check():
    return "MTA Bus Time bot is running", 200


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)


# MTA publishes separate static GTFS feeds for each NYC bus area/operator.
GTFS_FEED_URLS = (
    "https://web.mta.info/developers/data/nyct/bus/google_transit_bronx.zip",
    "https://web.mta.info/developers/data/nyct/bus/google_transit_brooklyn.zip",
    "https://web.mta.info/developers/data/nyct/bus/google_transit_manhattan.zip",
    "https://web.mta.info/developers/data/nyct/bus/google_transit_queens.zip",
    "https://web.mta.info/developers/data/nyct/bus/google_transit_staten_island.zip",
    "https://web.mta.info/developers/data/busco/google_transit.zip",
)

_stop_names: dict[str, str] = {}
_stop_lookup_loaded = False
_stop_lookup_lock = asyncio.Lock()


async def load_stop_names() -> None:
    """Download and cache the GTFS stop_id -> stop_name mapping once per process."""
    global _stop_lookup_loaded

    if _stop_lookup_loaded:
        return

    async with _stop_lookup_lock:
        if _stop_lookup_loaded:
            return

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for feed_url in GTFS_FEED_URLS:
                try:
                    async with session.get(feed_url) as response:
                        response.raise_for_status()
                        archive_data = await response.read()

                    with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
                        with archive.open("stops.txt") as stops_file:
                            text_file = io.TextIOWrapper(stops_file, encoding="utf-8-sig", newline="")
                            for row in csv.DictReader(text_file):
                                stop_id = (row.get("stop_id") or "").strip()
                                stop_name = (row.get("stop_name") or "").strip()
                                if stop_id and stop_name:
                                    _stop_names.setdefault(stop_id, stop_name)
                except (aiohttp.ClientError, OSError, KeyError, zipfile.BadZipFile) as exc:
                    print(f"Could not load GTFS feed {feed_url}: {exc}")

        _stop_lookup_loaded = True
        print(f"Loaded {len(_stop_names)} MTA bus stops from GTFS feeds")


async def get_stop_name(stop_id: str) -> str | None:
    await load_stop_names()
    return _stop_names.get(stop_id)


def _normalize_stop_id(stop_id: str) -> str:
    return stop_id.strip()


def _extract_distance(monitored_call: dict) -> str:
    extensions = monitored_call.get("Extensions") or {}
    distances = extensions.get("Distances") or {}

    for key in ("PresentableDistance", "DistanceFromStop", "DistanceFromCall"):
        value = distances.get(key)
        if value:
            return str(value)

    return "N/A"


def _format_arrival_status(monitored_call: dict) -> str:
    proximity = monitored_call.get("ArrivalProximityText")
    expected = monitored_call.get("ExpectedArrivalTime")

    if expected:
        try:
            arrival_dt = datetime.datetime.fromisoformat(expected.replace("Z", "+00:00"))
            now = datetime.datetime.now(arrival_dt.tzinfo or datetime.timezone.utc)
            minutes = max(int((arrival_dt - now).total_seconds() // 60), 0)
            if minutes == 0:
                return "🟢 Arriving now"
            return f"🟢 {minutes} mins"
        except ValueError:
            pass

    if proximity:
        return f"🟡 {proximity}"

    return "⚪ No ETA"


async def get_bus_times(stop_id: str):
    """Fetch live MTA BusTime arrivals for a stop ID."""
    stop_id = _normalize_stop_id(stop_id)
    if not MTA_KEY:
        raise RuntimeError("MTA_API_KEY environment variable not found.")

    url = "https://bustime.mta.info/api/siri/stop-monitoring.json"
    params = {"key": MTA_KEY, "MonitoringRef": stop_id}

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                payload = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        print(f"Error fetching MTA bus times for stop {stop_id}: {exc}")
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
            if not journey:
                continue

            monitored_call = journey.get("MonitoredCall", {})
            buses.append(
                {
                    "route": str(journey.get("PublishedLineName") or journey.get("LineRef") or "Unknown"),
                    "destination": str(journey.get("DestinationName") or "Unknown destination"),
                    "status": _format_arrival_status(monitored_call),
                    "distance": _extract_distance(monitored_call),
                }
            )

    return buses


def create_bus_embed(stop_id: str, stop_name: str | None, bus_data: list) -> discord.Embed:
    current_time = datetime.datetime.now().strftime("%I:%M %p")
    location = stop_name or "Unknown stop"
    description = f"📍 **{location}**\nStop ID: **{stop_id}**\n\u200b"

    embed = discord.Embed(
        title="🚌 Real-Time Bus Arrivals",
        description=description,
        color=0x0039A6,
    )

    if bus_data:
        for bus in bus_data:
            embed.add_field(
                name=f"📌 Route **{bus['route']}**",
                value=(
                    f"**Destination:** {bus['destination']}\n"
                    f"**Status:** {bus['status']} ({bus['distance']})\n\u200b"
                ),
                inline=False,
            )
    else:
        embed.add_field(
            name="⚠️ No Services",
            value="No upcoming buses detected for this stop.",
            inline=False,
        )

    embed.set_footer(text=f"Last Updated: {current_time} • Tap Refresh to update times")
    return embed


class BusRefreshView(discord.ui.View):
    def __init__(self, stop_id: str, stop_name: str | None):
        super().__init__(timeout=180)
        self.stop_id = stop_id
        self.stop_name = stop_name

    @discord.ui.button(label="🔄 Refresh Live Times", style=discord.ButtonStyle.success)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        data = await get_bus_times(self.stop_id)
        updated_embed = create_bus_embed(self.stop_id, self.stop_name, data)
        await interaction.edit_original_response(embed=updated_embed, view=self)


class BusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()


bot = BusBot()


@bot.tree.command(name="station", description="Get real-time bus arrival times for an MTA bus stop")
@app_commands.describe(station_number="The MTA bus stop number, for example 500003")
async def station(interaction: discord.Interaction, station_number: str):
    await interaction.response.defer()

    station_number = _normalize_stop_id(station_number)
    stop_name = await get_stop_name(station_number)

    try:
        data = await get_bus_times(station_number)
    except RuntimeError as exc:
        await interaction.followup.send(str(exc))
        return

    embed = create_bus_embed(station_number, stop_name, data)
    await interaction.followup.send(
        embed=embed,
        view=BusRefreshView(station_number, stop_name),
    )


if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN environment variable not found.")
    else:
        Thread(target=run_health_server, daemon=True).start()
        bot.run(TOKEN)
