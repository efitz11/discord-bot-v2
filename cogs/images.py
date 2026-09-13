import os
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands

IMAGES_DIR = os.getenv("BIG_IMAGES_DIR", "/home/eric/discord-bot-assets/images")
AUTHORIZED_UPLOADERS = {
    int(uid) for uid in os.getenv("BIG_UPLOAD_USER_IDS", "").split(",") if uid.strip()
}


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

    @app_commands.command(name="bigadd", description="Upload a new image for use with /big")
    @app_commands.describe(image="The image to add", name="Name to post it with later, e.g. /big <name>")
    async def bigadd(self, interaction: discord.Interaction, image: discord.Attachment, name: str):
        if AUTHORIZED_UPLOADERS and interaction.user.id not in AUTHORIZED_UPLOADERS:
            await interaction.response.send_message("You're not authorized to add images.", ephemeral=True)
            return

        if not image.content_type or not image.content_type.startswith("image/"):
            await interaction.response.send_message("That attachment isn't an image.", ephemeral=True)
            return

        ext = os.path.splitext(image.filename)[1].lower()
        base_name = name.strip().lower().replace(" ", "_")
        if not base_name or "/" in base_name or "\\" in base_name or ".." in base_name:
            await interaction.response.send_message("Invalid name.", ephemeral=True)
            return

        os.makedirs(IMAGES_DIR, exist_ok=True)
        dest = os.path.join(IMAGES_DIR, f"{base_name}{ext}")
        if os.path.exists(dest):
            await interaction.response.send_message(
                f"An image named `{base_name}` already exists. Pick a different name.", ephemeral=True
            )
            return

        await image.save(Path(dest))
        await interaction.response.send_message(f"Added `{base_name}` — use `/big {base_name}` to post it.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(ImagesCog(bot))
