from discord import app_commands
from discord import Interaction
from cogs.football import FootballCog


class NFLCog(FootballCog):
    SLUG  = "nfl"
    SPORT = "NFL"

    nfl = app_commands.Group(name="nfl", description="NFL scores and info")

    @nfl.command(name="score", description="Get the score for an NFL team")
    @app_commands.describe(team="NFL team or 'All Teams'", week="Week to show (defaults to the current week)")
    async def score(self, interaction: Interaction, team: str, week: str = None):
        extra_params, week_label = self._resolve_week(week)
        await self._score_impl(interaction, team, extra_params=extra_params, when_label=week_label)

    @score.autocomplete("team")
    async def team_autocomplete(self, interaction: Interaction, current: str):
        return await self._autocomplete_impl(current)

    @score.autocomplete("week")
    async def week_autocomplete(self, interaction: Interaction, current: str):
        return await self._week_autocomplete(current)


async def setup(bot):
    await bot.add_cog(NFLCog(bot))
