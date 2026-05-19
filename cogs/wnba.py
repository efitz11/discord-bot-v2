from discord import app_commands
from discord import Interaction
from cogs.basketball import BasketballCog


class WNBACog(BasketballCog):
    SLUG  = "wnba"
    SPORT = "WNBA"

    wnba = app_commands.Group(name="wnba", description="WNBA scores and info")

    @wnba.command(name="score", description="Get today's score for a WNBA team")
    @app_commands.describe(team="WNBA team")
    async def score(self, interaction: Interaction, team: str):
        await self._score_impl(interaction, team)

    @score.autocomplete("team")
    async def team_autocomplete(self, interaction: Interaction, current: str):
        return await self._autocomplete_impl(current)


async def setup(bot):
    await bot.add_cog(WNBACog(bot))
