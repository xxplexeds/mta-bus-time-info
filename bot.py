import os
import datetime
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

# Render Web Services expect the process to listen on the PORT environment variable.
health_app = Flask(__name__)


@health_app.get("/")
def health_check():
    return "MTA Bus Time bot is running", 200


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)


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
    """
    Fetch live MTA BusTime data for a specific stop ID and return a list of buses,
    each containing route, destination, status, and distance.
    """
    stop_id = _normalize_stop_id(stop_id)
    if not MTA_KEY:
        raise RuntimeError("MTA_API_KEY environment variable not found.")

    url = "https://bustime.mta.info/api/siri/stop-monitoring.json"
    params = {"key": MTA_KEY, "MonitoringRef": stop_id}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
                response.raise_for_status()
                payload = await response.json()
    except Exception as exc:
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

            route = journey.get("PublishedLineName") or journey.get("LineRef") or "Unknown"
            destination = journey.get("DestinationName") or "Unknown destination"
            monitored_call = journey.get("MonitoredCall", {})

            buses.append(
                {
                    "route": str(route),
                    "destination": str(destination),
                    "status": _format_arrival_status(monitored_call),
                    "distance": _extract_distance(monitored_call),
                }
            )

    return buses


# --- EMBEDS V2 (WITH DESTINATIONS) ---
def create_bus_embed_v2(stop_id: str, bus_data: list) -> discord.Embed:
    current_time = datetime.datetime.now().strftime("%I:%M %p")

    embed = discord.Embed(
        title="🚌 Real-Time Bus Arrivals",
        description=f"Live tracking for Stop ID: **{stop_id}**\n\u200b",
        color=0x0039A6,  # MTA Blue
    )

    if bus_data:
        for bus in bus_data:
            embed.add_field(
                name=f"📌 Route **{bus['route']}**",
                value=f"**Destination:** {bus['destination']}\n**Status:** {bus['status']} ({bus['distance']})\n\u200b",
                inline=False,
            )
    else:
        embed.add_field(name="⚠️ No Services", value="No upcoming buses detected for this stop.", inline=False)

    embed.set_footer(text=f"Last Updated: {current_time} • Tap Refresh to update times")
    return embed


# --- INTERACTIVE COMPONENTS ---
class BusRefreshView(discord.ui.View):
    def __init__(self, stop_id: str):
        super().__init__(timeout=180)
        self.stop_id = stop_id

    @discord.ui.button(label="🔄 Refresh Live Times", style=discord.ButtonStyle.success)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        data = await get_bus_times(self.stop_id)
        updated_embed = create_bus_embed_v2(self.stop_id, data)

        await interaction.edit_original_response(embed=updated_embed, view=self)


# --- BOT ROUTINES ---
class BusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()


bot = BusBot()


@bot.tree.command(name="bus", description="Get real-time arrival times for an MTA bus stop")
@app_commands.describe(stop_id="The MTA Bus Stop ID")
async def bus(interaction: discord.Interaction, stop_id: str):
    await interaction.response.defer()

    try:
        data = await get_bus_times(stop_id)
    except RuntimeError as exc:
        await interaction.followup.send(str(exc))
        return

    embed = create_bus_embed_v2(stop_id, data)
    await interaction.followup.send(embed=embed, view=BusRefreshView(stop_id))


if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN environment variable not found.")
    else:
        Thread(target=run_health_server, daemon=True).start()
        bot.run(TOKEN)
