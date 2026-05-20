from discord import app_commands
from discord import Interaction
from cogs.basketball import BasketballCog


class NBACog(BasketballCog):
    SLUG  = "nba"
    SPORT = "NBA"

    nba = app_commands.Group(name="nba", description="NBA scores and info")

    @nba.command(name="score", description="Get today's score for an NBA team")
    @app_commands.describe(team="NBA team", date="Date (e.g. yesterday, -1, 5/19, 5/19/26)")
    async def score(self, interaction: Interaction, team: str, date: str = None):
        await self._score_impl(interaction, team, date)

    @score.autocomplete("team")
    async def team_autocomplete(self, interaction: Interaction, current: str):
        return await self._autocomplete_impl(current)


async def setup(bot):
    await bot.add_cog(NBACog(bot))
