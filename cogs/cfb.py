import discord
from discord import app_commands
from discord import Interaction
from cogs.espn_base import _format_linescore, _format_pregame, FINAL_STATUSES
from cogs.football import FootballCog

CONFERENCES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard/conferences"
RANKED_VALUE    = "RANKED"
TOP_RANKED_N    = 10


class CFBCog(FootballCog):
    SLUG  = "college-football"
    SPORT = "NCAAF"
    TEAM_PARAMS = {"groups": "80", "limit": "300"}  # FBS teams only

    def __init__(self, bot):
        super().__init__(bot)
        self._conferences: list[dict] = []

    async def _load_extra(self):
        await super()._load_extra()
        try:
            session = await self.bot.mlb_client.get_session()
            async with session.get(CONFERENCES_URL) as resp:
                data = await resp.json()
            self._conferences = [c for c in data.get("conferences", []) if c.get("groupId") != "80"]
            print(f"[{self.SLUG}] loaded {len(self._conferences)} conferences")
        except Exception as e:
            print(f"[{self.SLUG}] failed to load conferences: {e}")

    cfb = app_commands.Group(name="cfb", description="College football scores and info")

    @cfb.command(name="score", description="Get the score for a college football team, conference, or the top 25")
    @app_commands.describe(
        team="Team, conference, or 'Top 25 Games'",
        week="Week to show (defaults to the current week)",
    )
    async def score(self, interaction: Interaction, team: str, week: str = None):
        extra_params, week_label = self._resolve_week(week)

        if team == RANKED_VALUE:
            await self._ranked_games_impl(interaction, extra_params, week_label)
            return

        conf = next((c for c in self._conferences if c["groupId"] == team), None)
        if conf:
            await self._score_impl(interaction, groups=team, group_label=f"{conf['shortName']} Games",
                                    extra_params=extra_params, when_label=week_label)
            return

        await self._score_impl(interaction, team, extra_params=extra_params, when_label=week_label)

    @score.autocomplete("team")
    async def team_autocomplete(self, interaction: Interaction, current: str):
        cur     = current.lower()
        choices = []
        if not current or "top" in cur or "rank" in cur or "25" in cur:
            choices.append(app_commands.Choice(name="Top 25 Games", value=RANKED_VALUE))
        conf_matches = [c for c in self._conferences if cur in c["name"].lower() or cur in c["shortName"].lower()]
        choices += [app_commands.Choice(name=f"{c['shortName']} (Conference)", value=c["groupId"]) for c in conf_matches[:10]]
        remaining = 25 - len(choices)
        team_matches = [t for t in self._teams if cur in t["displayName"].lower() or cur in t["abbreviation"].lower()]
        choices += [app_commands.Choice(name=t["displayName"], value=t["abbreviation"]) for t in team_matches[:remaining]]
        return choices[:25]

    @score.autocomplete("week")
    async def week_autocomplete(self, interaction: Interaction, current: str):
        return await self._week_autocomplete(current)

    async def _ranked_games_impl(self, interaction: Interaction, extra_params: dict = None, week_label: str = None):
        await interaction.response.defer()

        params = {"groups": "80"}
        if extra_params:
            params.update(extra_params)

        session = await self.bot.mlb_client.get_session()
        async with session.get(self._scoreboard_url, params=params) as resp:
            data = await resp.json()

        if not week_label:
            wk = data.get("week", {}).get("number")
            week_label = f"Week {wk}" if wk else "This Week"

        ranked = []
        for event in data.get("events", []):
            comp  = event["competitions"][0]
            ranks = [c.get("curatedRank", {}).get("current") or 99 for c in comp["competitors"]]
            best  = min(r if r <= 25 else 99 for r in ranks)
            if best <= 25:
                ranked.append((best, comp))
        ranked.sort(key=lambda x: x[0])
        top = ranked[:TOP_RANKED_N]

        if not top:
            await interaction.followup.send(f"No ranked games for {week_label}.")
            return

        top.sort(key=lambda x: x[1].get("date", ""))

        blocks   = []
        any_live = False
        for _best, comp in top:
            p = self._parse_comp(comp)
            if p["status_name"] == "STATUS_SCHEDULED":
                table = _format_pregame(p["away_prefix"], p["away_abbr"], p["home_prefix"], p["home_abbr"],
                                         p["odds"], p["broadcast"])
            else:
                labels = self._linescore_labels(max(len(p["away_qs"]), len(p["home_qs"]), 1))
                table  = _format_linescore(p["away_prefix"], p["away_abbr"], p["away_qs"],
                                            p["home_prefix"], p["home_abbr"], p["home_qs"],
                                            p["away_total"], p["home_total"], labels)
            if p["status_name"] not in FINAL_STATUSES and p["status_name"] != "STATUS_SCHEDULED":
                any_live = True
            blocks.append(f"**{p['away_display']} @ {p['home_display']} | {p['status_str']}**\n```\n{table}\n```")

        color = discord.Color.orange() if any_live else discord.Color.blue()
        await interaction.followup.send(embed=discord.Embed(
            title=f"Top 25 Games — {week_label}",
            description="\n".join(blocks),
            color=color,
        ))


async def setup(bot):
    await bot.add_cog(CFBCog(bot))
