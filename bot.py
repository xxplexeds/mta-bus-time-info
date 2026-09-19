import os
import discord
from discord import app_commands
from discord.ext import commands
import datetime
import aiohttp
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
MTA_KEY = os.getenv("MTA_API_KEY")

# --- PLACEHOLDER FOR LIVE MTA DATA ---
async def get_bus_times(stop_id: str):
    """
    This structure demonstrates how the MTA API passes destinations.
    We capture both the route name and the destination/service type.
    """
    return [
        {"route": "M15-SBS", "destination": "Lower East Side via 2 Av", "status": "🟢 3 mins", "distance": "0.4 mi away"},
        {"route": "M15", "destination": "South Ferry via 2 Av", "status": "🟡 12 mins", "distance": "1.8 mi away"},
        {"route": "M116", "destination": "West Side - Broadway", "status": "🔴 24 mins", "distance": "3.1 mi away"}
    ]

# --- EMBEDS V2 (WITH DESTINATIONS) ---
def create_bus_embed_v2(stop_id: str, bus_data: list) -> discord.Embed:
    current_time = datetime.datetime.now().strftime("%I:%M %p")
    
    embed = discord.Embed(
        title="🚌 Real-Time Bus Arrivals",
        description=f"Live tracking for Stop ID: **{stop_id}**\n\u200b",
        color=0x0039A6 # MTA Blue
    )
    
    if bus_data:
        for bus in bus_data:
            # Displays the route number prominently, followed by its destination type
            embed.add_field(
                name=f"📌 Route **{bus['route']}**",
                value=f"**Destination:** {bus['destination']}\n**Status:** {bus['status']} ({bus['distance']})\n\u200b",
                inline=False
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

@bot.tree.command(name="bus", description="Get real-time layout arrival times for an MTA bus stop")
@app_commands.describe(stop_id="The 6-digit MTA Bus Stop ID")
async def bus(interaction: discord.Interaction, stop_id: str):
    await interaction.response.defer()

    data = await get_bus_times(stop_id)
    embed = create_bus_embed_v2(stop_id, data)

    await interaction.followup.send(embed=embed, view=BusRefreshView(stop_id))

if TOKEN:
    bot.run(TOKEN)
else:
    print("Error: DISCORD_TOKEN environment variable not found.")
