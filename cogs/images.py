import os
import discord
from discord import app_commands
from discord.ext import commands

IMAGES_DIR = os.getenv("BIG_IMAGES_DIR", "/home/eric/discord-bot-assets/images")


def _list_images():
    if not os.path.isdir(IMAGES_DIR):
        return []
    return sorted(os.listdir(IMAGES_DIR))


class ImagesCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="big", description="Post an image by name")
    @app_commands.describe(name="The image to post")
    async def big(self, interaction: discord.Interaction, name: str):
        for f in _list_images():
            if os.path.splitext(f)[0] == name:
                await interaction.response.send_message(file=discord.File(os.path.join(IMAGES_DIR, f)))
                return
        await interaction.response.send_message(f"No image found matching `{name}`.", ephemeral=True)

    @big.autocomplete("name")
    async def big_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        current = current.lower()
        names = [os.path.splitext(f)[0] for f in _list_images()]
        matches = [n for n in names if current in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in matches[:25]]


async def setup(bot):
    await bot.add_cog(ImagesCog(bot))
