import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta, timezone

WNBA_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
WNBA_TEAMS      = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams"

ET_OFFSET = timedelta(hours=4)


def _utc_to_et(iso_str: str) -> datetime | None:
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%MZ")
        return dt - ET_OFFSET
    except ValueError:
        try:
            dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ")
            return dt - ET_OFFSET
        except ValueError:
            return None


def _format_linescore(away_abbr, away_qs, home_abbr, home_qs, away_total, home_total) -> str:
    # Determine number of period columns (always at least 4)
    max_periods = max(len(away_qs), len(home_qs), 4)

    # Quarter/OT header labels (no total label — total is left of the separator)
    q_labels = [f"Q{i}" for i in range(1, min(max_periods, 4) + 1)]
    if max_periods == 5:
        q_labels.append("OT")
    elif max_periods > 5:
        q_labels += [f"OT{i}" for i in range(1, max_periods - 3)]

    def pad_qs(qs, total):
        filled = list(qs) + [None] * (max_periods - len(qs))
        return str(total), [str(s) if s is not None else "-" for s in filled]

    away_tot, away_vals = pad_qs(away_qs, away_total)
    home_tot, home_vals = pad_qs(home_qs, home_total)

    tot_w = max(len("T"), len(away_tot), len(home_tot))
    q_widths = [
        max(len(lbl), len(av), len(hv))
        for lbl, av, hv in zip(q_labels, away_vals, home_vals)
    ]
    abbr_w = max(len(away_abbr), len(home_abbr))

    sep = "  "

    def fmt_row(abbr, tot, vals):
        total_part = abbr.ljust(abbr_w) + sep + tot.rjust(tot_w)
        q_part = sep.join(v.rjust(w) for v, w in zip(vals, q_widths))
        return total_part + " | " + q_part

    header = " " * abbr_w + sep + "T".rjust(tot_w) + " | " + sep.join(lbl.rjust(w) for lbl, w in zip(q_labels, q_widths))
    return "\n".join([header, fmt_row(away_abbr, away_tot, away_vals), fmt_row(home_abbr, home_tot, home_vals)])


class WNBACog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._teams: list[dict] = []

    @commands.Cog.listener()
    async def on_ready(self):
        await self._load_teams()

    async def _load_teams(self):
        try:
            session = await self.bot.mlb_client.get_session()
            async with session.get(WNBA_TEAMS) as resp:
                data = await resp.json()
            league = data.get("sports", [{}])[0].get("leagues", [{}])[0]
            self._teams = [
                {
                    "abbreviation": t["team"]["abbreviation"],
                    "displayName":  t["team"]["displayName"],
                }
                for t in league.get("teams", [])
            ]
            print(f"[wnba] loaded {len(self._teams)} teams")
        except Exception as e:
            print(f"[wnba] failed to load teams: {e}")

    wnba = app_commands.Group(name="wnba", description="WNBA scores and info")

    @wnba.command(name="score", description="Get today's score for a WNBA team")
    @app_commands.describe(team="WNBA team")
    async def score(self, interaction: discord.Interaction, team: str):
        await interaction.response.defer()

        session = await self.bot.mlb_client.get_session()
        async with session.get(WNBA_SCOREBOARD) as resp:
            data = await resp.json()

        team_upper = team.upper()
        comp = None
        for event in data.get("events", []):
            c = event["competitions"][0]
            if any(x["team"]["abbreviation"].upper() == team_upper for x in c["competitors"]):
                comp = c
                break

        if not comp:
            await interaction.followup.send(f"No game today for **{team_upper}**.")
            return

        away = next(x for x in comp["competitors"] if x["homeAway"] == "away")
        home = next(x for x in comp["competitors"] if x["homeAway"] == "home")

        away_abbr  = away["team"]["abbreviation"]
        home_abbr  = home["team"]["abbreviation"]
        away_total = away.get("score", "0")
        home_total = home.get("score", "0")

        away_qs = [int(ls["value"]) for ls in away.get("linescores", [])]
        home_qs = [int(ls["value"]) for ls in home.get("linescores", [])]

        status      = comp["status"]
        status_name = status["type"]["name"]
        period      = status.get("period", 0)
        clock       = status.get("displayClock", "")

        table = _format_linescore(away_abbr, away_qs, home_abbr, home_qs, away_total, home_total)

        if status_name == "STATUS_SCHEDULED":
            tipoff_et = _utc_to_et(comp.get("date", ""))
            tipoff_str = tipoff_et.strftime("%-I:%M %p ET") if tipoff_et else "TBD"
            status_str = tipoff_str
            color = discord.Color.blue()
        elif status_name in ("STATUS_FINAL", "STATUS_FINAL_OT"):
            status_str = "Final"
            color = discord.Color.blue()
        else:
            if period <= 4:
                period_label = f"Q{period}"
            elif period == 5:
                period_label = "OT"
            else:
                period_label = f"OT{period - 4}"
            status_str = f"{period_label} | {clock}"
            color = discord.Color.orange()

        title = f"{away_abbr} @ {home_abbr} | {status_str}"
        body = f"```\n{table}\n```"

        situation = comp.get("situation", {})
        last_play  = situation.get("lastPlay", {})
        lp_text    = last_play.get("text", "")
        if lp_text and status_name not in ("STATUS_SCHEDULED", "STATUS_FINAL", "STATUS_FINAL_OT"):
            lp_team_id = last_play.get("team", {}).get("id", "")
            lp_abbr = ""
            for x in comp["competitors"]:
                if x["team"].get("id", "") == lp_team_id:
                    lp_abbr = x["team"]["abbreviation"]
                    break
            lp_prefix = f"{lp_abbr} — " if lp_abbr else ""
            body += f"\nLast play: {lp_prefix}{lp_text}"

        embed = discord.Embed(
            title=title,
            description=body,
            color=color,
        )
        await interaction.followup.send(embed=embed)

    @score.autocomplete("team")
    async def team_autocomplete(self, interaction: discord.Interaction, current: str):
        cur = current.lower()
        matches = [
            t for t in self._teams
            if cur in t["displayName"].lower() or cur in t["abbreviation"].lower()
        ]
        return [
            app_commands.Choice(name=t["displayName"], value=t["abbreviation"])
            for t in matches[:10]
        ]


async def setup(bot):
    await bot.add_cog(WNBACog(bot))
