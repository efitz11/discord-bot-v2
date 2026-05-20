from discord import app_commands, Interaction
from cogs.hockey import HockeyCog


class NHLCog(HockeyCog):
    SLUG  = "nhl"
    SPORT = "NHL"

    nhl = app_commands.Group(name="nhl", description="NHL scores and info")

    @nhl.command(name="score", description="Get today's score for an NHL team")
    @app_commands.describe(team="NHL team", date="Date (e.g. yesterday, -1, 5/19, 5/19/26)")
    async def score(self, interaction: Interaction, team: str, date: str = None):
        await self._score_impl(interaction, team, date)

    @score.autocomplete("team")
    async def team_autocomplete(self, interaction: Interaction, current: str):
        return await self._autocomplete_impl(current)


async def setup(bot):
    await bot.add_cog(NHLCog(bot))
