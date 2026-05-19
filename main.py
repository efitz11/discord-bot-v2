import asyncio
import os
import discord
from discord.ext import commands
from core.mlb_client import MLBClient
from dotenv import load_dotenv

# Modern Discord bots require explicit Intents
intents = discord.Intents.default()
intents.message_content = True  # Enable if you also want it to read regular messages eventually

class ModernNatsBot(commands.Bot):
    def __init__(self):
        # A command_prefix is still required by the superclass, but we'll be using slash commands
        super().__init__(command_prefix='!', intents=intents)
        self.mlb_client = MLBClient()
        self.favorite_team = os.getenv("FAVORITE_TEAM", "").upper() or None
        self.alert_channel_id = int(os.getenv("ALERT_CHANNEL_ID", "0")) or None
        # Populated by _load_team_config() at startup
        self.favorite_team_name = None       # e.g. "nationals" (lowercase teamName)
        self.favorite_team_full = None       # e.g. "Washington Nationals"
        self.favorite_team_affiliates = []   # lowercase full names of MiLB affiliate teams
        self.favorite_team_milb_pins = []    # raw dicts for MiLB score autocomplete pins
    async def _load_team_config(self):
        """Fetch favorite team name and MiLB affiliates from the MLB API."""
        session = await self.mlb_client.get_session()
        try:
            async with session.get(f"{self.mlb_client.BASE_URL}/teams?sportId=1") as resp:
                data = await resp.json()
            for t in data.get('teams', []):
                if t.get('abbreviation', '').upper() == self.favorite_team:
                    self.favorite_team_name = t.get('teamName', '').lower()
                    self.favorite_team_full = t.get('name', '')
                    break
        except Exception as e:
            print(f"[config] Team lookup failed: {e}")

        try:
            milb_teams = await self.mlb_client.get_milb_teams()
            affiliates = [t for t in milb_teams if t.get('parent_abbrev', '').upper() == self.favorite_team]
            self.favorite_team_affiliates = [t['name'].lower() for t in affiliates]
            if self.favorite_team_full:
                self.favorite_team_milb_pins = [
                    {'name': f"{self.favorite_team} — {self.favorite_team_full} (All Affiliates)", 'value': self.favorite_team}
                ] + [
                    {'name': f"{t['abbreviation']} — {t['name']} ({t['level']})", 'value': t['abbreviation']}
                    for t in affiliates
                ]
        except Exception as e:
            print(f"[config] MiLB affiliate lookup failed: {e}")

        self.mlb_client.favorite_team_name = self.favorite_team_name
        self.mlb_client.favorite_team_affiliates = self.favorite_team_affiliates
        print(f"[config] Favorite team: {self.favorite_team} ({self.favorite_team_full}), {len(self.favorite_team_affiliates)} affiliates")

    async def setup_hook(self):
        if self.favorite_team:
            await self._load_team_config()

        # Load our new modern cog
        await self.load_extension('cogs.mlb')

        # Load the live game monitor (no-hitters, big HRs)
        await self.load_extension('cogs.monitor')

        # Load optional extended commands (weather, etc.) if enabled
        if os.getenv("EXTENDED_COMMANDS", "").lower() in ("1", "true", "yes"):
            await self.load_extension('cogs.extended')
            print("Extended commands enabled.")

        if os.getenv("WNBA", "").lower() in ("1", "true", "yes"):
            await self.load_extension('cogs.wnba')
            print("WNBA commands enabled.")
        
        # Sync slash commands in the background so startup isn't blocked
        async def _sync():
            try:
                synced = await self.tree.sync()
                print(f"Slash commands synced globally! ({len(synced)} commands)")
            except Exception as e:
                print(f"Command sync error: {e}")
        asyncio.create_task(_sync())
        
    async def close(self):
        # Cleanly close the aiohttp session when the bot shuts down
        await self.mlb_client.close()
        await super().close()

bot = ModernNatsBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    print('------')

if __name__ == "__main__":
    # Load environment variables from a .env file if it exists
    load_dotenv()
    
    discord_token = os.getenv("DISCORD_TOKEN")
    if not discord_token:
        raise ValueError("No DISCORD_TOKEN found in environment variables. Make sure you have a .env file setup!")

    bot.run(discord_token)