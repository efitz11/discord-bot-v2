"""Shared logic for NBA and WNBA score commands."""
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta

ET_OFFSET = timedelta(hours=4)

ESPN_BASE     = "https://site.api.espn.com/apis/site/v2/sports/basketball"
ESPN_WEB_BASE = "https://site.web.api.espn.com/apis/site/v2/sports/basketball"


def _utc_to_et(iso_str: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(iso_str, fmt) - ET_OFFSET
        except ValueError:
            continue
    return None


def _format_linescore(away_abbr, away_qs, home_abbr, home_qs, away_total, home_total) -> str:
    max_periods = max(len(away_qs), len(home_qs), 4)

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

    tot_w    = max(len("T"), len(away_tot), len(home_tot))
    q_widths = [max(len(lbl), len(av), len(hv)) for lbl, av, hv in zip(q_labels, away_vals, home_vals)]
    abbr_w   = max(len(away_abbr), len(home_abbr))
    sep      = "  "

    def fmt_row(abbr, tot, vals):
        return abbr.ljust(abbr_w) + sep + tot.rjust(tot_w) + " | " + sep.join(v.rjust(w) for v, w in zip(vals, q_widths))

    header = " " * abbr_w + sep + "T".rjust(tot_w) + " | " + sep.join(lbl.rjust(w) for lbl, w in zip(q_labels, q_widths))
    return "\n".join([header, fmt_row(away_abbr, away_tot, away_vals), fmt_row(home_abbr, home_tot, home_vals)])


def _score_player(stats: list[str], labels: list[str]) -> float:
    def get(key):
        try:
            raw = stats[labels.index(key)]
            return float(raw.split("-")[0]) if raw not in ("-", "") else 0.0
        except (ValueError, IndexError):
            return 0.0

    return (
        get("PTS") * 1.0 +
        get("REB") * 1.2 +
        get("AST") * 1.5 +
        get("STL") * 2.0 +
        get("BLK") * 2.0 -
        get("TO")  * 1.0
    )


def _summarize_player(stats: list[str], labels: list[str]) -> str:
    def get_int(key):
        try:
            raw = stats[labels.index(key)]
            return int(float(raw.split("-")[0])) if raw not in ("-", "") else 0
        except (ValueError, IndexError):
            return 0

    parts = [f"{get_int('PTS')} PTS"]
    for key, label in [("REB", "REB"), ("AST", "AST"), ("STL", "STL"), ("BLK", "BLK"), ("TO", "TO")]:
        v = get_int(key)
        if v:
            parts.append(f"{v} {label}")
    return ", ".join(parts)


async def _fetch_top_performers(session, summary_url: str, event_id: str) -> dict[str, list[dict]]:
    try:
        async with session.get(summary_url, params={"event": event_id}) as resp:
            data = await resp.json()
    except Exception:
        return {}

    result = {}
    for team_block in data.get("boxscore", {}).get("players", []):
        abbr   = team_block.get("team", {}).get("abbreviation", "")
        groups = team_block.get("statistics", [])
        if not groups:
            continue
        sg     = groups[0]
        labels = sg.get("labels", [])
        scored = []
        for athlete in sg.get("athletes", []):
            if athlete.get("didNotPlay") or not athlete.get("active"):
                continue
            raw = athlete.get("stats", [])
            if not raw:
                continue
            try:
                if "MIN" in labels and int(raw[labels.index("MIN")]) == 0:
                    continue
            except (ValueError, IndexError):
                continue
            scored.append({
                "name":    athlete.get("athlete", {}).get("displayName", "Unknown"),
                "score":   _score_player(raw, labels),
                "summary": _summarize_player(raw, labels),
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        result[abbr] = scored[:2]

    return result


class BasketballCog(commands.Cog):
    """Base class — subclasses set SLUG and SPORT and define the command group."""
    SLUG:  str = ""
    SPORT: str = ""

    def __init__(self, bot):
        self.bot   = bot
        self._teams: list[dict] = []
        self._scoreboard_url = f"{ESPN_BASE}/{self.SLUG}/scoreboard"
        self._teams_url      = f"{ESPN_BASE}/{self.SLUG}/teams"
        self._summary_url    = f"{ESPN_WEB_BASE}/{self.SLUG}/summary"

    @commands.Cog.listener()
    async def on_ready(self):
        await self._load_teams()

    async def _load_teams(self):
        try:
            session = await self.bot.mlb_client.get_session()
            async with session.get(self._teams_url) as resp:
                data = await resp.json()
            league = data.get("sports", [{}])[0].get("leagues", [{}])[0]
            self._teams = [
                {"abbreviation": t["team"]["abbreviation"], "displayName": t["team"]["displayName"]}
                for t in league.get("teams", [])
            ]
            print(f"[{self.SLUG}] loaded {len(self._teams)} teams")
        except Exception as e:
            print(f"[{self.SLUG}] failed to load teams: {e}")

    def _parse_comp(self, comp: dict) -> dict:
        """Extract display fields from a competition dict."""
        away = next(x for x in comp["competitors"] if x["homeAway"] == "away")
        home = next(x for x in comp["competitors"] if x["homeAway"] == "home")

        away_abbr  = away["team"]["abbreviation"]
        home_abbr  = home["team"]["abbreviation"]
        away_total = away.get("score", "0")
        home_total = home.get("score", "0")
        away_qs    = [int(ls["value"]) for ls in away.get("linescores", [])]
        home_qs    = [int(ls["value"]) for ls in home.get("linescores", [])]

        status      = comp["status"]
        status_name = status["type"]["name"]
        period      = status.get("period", 0)
        clock       = status.get("displayClock", "")

        if status_name == "STATUS_SCHEDULED":
            tipoff_et  = _utc_to_et(comp.get("date", ""))
            status_str = tipoff_et.strftime("%-I:%M %p ET") if tipoff_et else "TBD"
        elif status_name in ("STATUS_FINAL", "STATUS_FINAL_OT"):
            status_str = "Final"
        else:
            if period <= 4:
                period_label = f"Q{period}"
            elif period == 5:
                period_label = "OT"
            else:
                period_label = f"OT{period - 4}"
            status_str = f"{period_label} | {clock}"

        return {
            "away_abbr": away_abbr, "home_abbr": home_abbr,
            "away_total": away_total, "home_total": home_total,
            "away_qs": away_qs, "home_qs": home_qs,
            "status_name": status_name, "status_str": status_str,
            "comp": comp,
        }

    async def _score_impl(self, interaction: discord.Interaction, team: str):
        await interaction.response.defer()

        session = await self.bot.mlb_client.get_session()
        async with session.get(self._scoreboard_url) as resp:
            data = await resp.json()

        events     = data.get("events", [])
        team_upper = team.upper()
        all_games  = team_upper == "ALL"

        if all_games:
            games = [(event["id"], event["competitions"][0]) for event in events]
        else:
            games = [
                (event["id"], event["competitions"][0])
                for event in events
                if any(x["team"]["abbreviation"].upper() == team_upper for x in event["competitions"][0]["competitors"])
            ]

        if not games:
            msg = f"No games today." if all_games else f"No game today for **{team_upper}**."
            await interaction.followup.send(msg)
            return

        if all_games:
            # Show all games — linescore only, no last play or performers
            blocks = []
            any_live = False
            for _event_id, comp in games:
                p = self._parse_comp(comp)
                table = _format_linescore(p["away_abbr"], p["away_qs"], p["home_abbr"], p["home_qs"], p["away_total"], p["home_total"])
                if p["status_name"] not in ("STATUS_SCHEDULED", "STATUS_FINAL", "STATUS_FINAL_OT"):
                    any_live = True
                blocks.append(f"**{p['away_abbr']} @ {p['home_abbr']} | {p['status_str']}**\n```\n{table}\n```")

            color = discord.Color.orange() if any_live else discord.Color.blue()
            embed = discord.Embed(
                title=f"Today's {self.SPORT} Scores",
                description="\n".join(blocks),
                color=color,
            )
            await interaction.followup.send(embed=embed)
            return

        # Single team
        event_id, comp = games[0]
        p = self._parse_comp(comp)
        table  = _format_linescore(p["away_abbr"], p["away_qs"], p["home_abbr"], p["home_qs"], p["away_total"], p["home_total"])
        color  = discord.Color.orange() if p["status_name"] not in ("STATUS_SCHEDULED", "STATUS_FINAL", "STATUS_FINAL_OT") else discord.Color.blue()
        title  = f"{p['away_abbr']} @ {p['home_abbr']} | {p['status_str']}"
        body   = f"```\n{table}\n```"

        if p["status_name"] not in ("STATUS_SCHEDULED", "STATUS_FINAL", "STATUS_FINAL_OT"):
            situation = p["comp"].get("situation", {})
            lp        = situation.get("lastPlay", {})
            lp_text   = lp.get("text", "")
            if lp_text:
                lp_team_id = lp.get("team", {}).get("id", "")
                lp_abbr    = next(
                    (x["team"]["abbreviation"] for x in p["comp"]["competitors"] if x["team"].get("id") == lp_team_id),
                    ""
                )
                body += f"\nLast play: {lp_abbr + ' — ' if lp_abbr else ''}{lp_text}"

        if p["status_name"] != "STATUS_SCHEDULED":
            top = await _fetch_top_performers(session, self._summary_url, event_id)
            perf_blocks = []
            for abbr in (p["away_abbr"], p["home_abbr"]):
                players = top.get(abbr, [])
                if players:
                    lines = [f"{abbr}:"] + [f"**{p2['name']}** {p2['summary']}" for p2 in players]
                    perf_blocks.append("\n".join(lines))
            if perf_blocks:
                body += "\n\n" + "\n\n".join(perf_blocks)

        await interaction.followup.send(embed=discord.Embed(title=title, description=body, color=color))

    async def _autocomplete_impl(self, current: str) -> list[app_commands.Choice]:
        cur     = current.lower()
        matches = [t for t in self._teams if cur in t["displayName"].lower() or cur in t["abbreviation"].lower()]
        choices = [app_commands.Choice(name=t["displayName"], value=t["abbreviation"]) for t in matches[:9]]
        if not current or "all" in cur:
            choices.insert(0, app_commands.Choice(name="All Teams", value="ALL"))
        return choices[:10]
