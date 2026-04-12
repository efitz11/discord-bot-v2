import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta

def parse_date(date_str: str) -> str:
    """Parses a user input date string into YYYY-MM-DD format."""
    # Use Eastern Time as the baseline for MLB dates
    now = datetime.utcnow() - timedelta(hours=5)
    if not date_str:
        return None
        
    date_str = date_str.lower().strip()
    if date_str == 'yesterday':
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    elif date_str == 'tomorrow':
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    elif date_str.startswith('+') or date_str.startswith('-'):
        try:
            days = int(date_str)
            return (now + timedelta(days=days)).strftime("%Y-%m-%d")
        except ValueError:
            pass
    elif '/' in date_str or '-' in date_str:
        parts = date_str.replace('-', '/').split('/')
        try:
            month, day = int(parts[0]), int(parts[1])
            year = int(parts[2]) if len(parts) == 3 else now.year
            if year < 100: year += 2000
            return f"{year:04d}-{month:02d}-{day:02d}"
        except (ValueError, IndexError):
            pass
            
    return None

class PlayerAbsView(discord.ui.View):
    def __init__(self, cog, player_id: str, date: str, milb: bool):
        super().__init__(timeout=600)
        self.cog = cog
        self.player_id = player_id
        self.date = date
        self.milb = milb

    @discord.ui.button(label="Show At-Bats", style=discord.ButtonStyle.secondary, emoji="⚾")
    async def show_abs(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._send_player_abs(interaction, self.player_id, self.date, self.milb, edit_original=True)
        self.stop()

class MLBSlash(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    # Create a slash command group: /mlb
    mlb = app_commands.Group(name="mlb", description="MLB stats and scores commands")
    milb = app_commands.Group(name="milb", description="MiLB stats and scores commands")

    @mlb.command(name="line", description="Get a player's stat line for today or a specific date")
    @app_commands.describe(player="The player to search for")
    @app_commands.describe(date="A specific date (e.g. 4/7/26, yesterday, +2, -5)")
    async def line(self, interaction: discord.Interaction, player: str, date: str = None):
        await interaction.response.defer()
        parsed_date = parse_date(date)

        # Because we linked autocomplete below, "player" will typically contain the exact Player ID
        stats_list = await self.bot.mlb_client.get_player_game_stats(player, date=parsed_date)

        if not stats_list:
            await interaction.followup.send("Could not find stats for that player.")
            return

        embed = discord.Embed(color=discord.Color.blue())
        
        if len(stats_list) == 1:
            stats = stats_list[0]
            embed.title = f"{stats.player_name} ({stats.team_abbrev}) {stats.date} {'vs' if stats.is_home else '@'} {stats.opp_abbrev}"
            embed.description = f"```python\n{stats.format_discord_code_block()}\n```"
        else:
            stats = stats_list[0]
            embed.title = f"{stats.player_name} ({stats.team_abbrev}) - {stats.date}"
            for i, s in enumerate(stats_list, 1):
                name = f"Game {i}: {'vs' if s.is_home else '@'} {s.opp_abbrev}"
                embed.add_field(name=name, value=f"```python\n{s.format_discord_code_block()}\n```", inline=False)
                
        if stats_list[0].headshot_url:
            embed.set_thumbnail(url=stats_list[0].headshot_url)
        
        # Add button if any game had batting stats
        view = None
        has_batting = any(s.batting_stats is not None for s in stats_list if not s.info_message)
        if has_batting:
            first = stats_list[0]
            view = PlayerAbsView(self, first.player_id, first.date, milb=False)
                
        await interaction.followup.send(embed=embed, view=view)


    @line.autocomplete('player')
    async def player_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not current or len(current) < 2:
            return []

        players = await self.bot.mlb_client.search_players(current)
        
        nats_choices = []
        other_choices = []
        
        for p in players:
            team = p.get('name_display_club')
            # The Savant API returns mlb=1 for active Major Leaguers
            if team and p.get('mlb') == 1:
                name = p.get('name', 'Unknown')
                choice = app_commands.Choice(name=f"{name} ({team})"[:100], value=str(p.get('id', '')))
                
                if "nationals" in team.lower():
                    nats_choices.append(choice)
                else:
                    other_choices.append(choice)
                    
        # Combine and return up to 25 matches for Discord's popup menu
        return (nats_choices + other_choices)[:25]

    @mlb.command(name="abs", description="Get a player's at-bats and video highlights for today or a specific date")
    @app_commands.describe(player="The player to search for")
    @app_commands.describe(date="A specific date (e.g. 4/7/26, yesterday, +2, -5)")
    async def abs_command(self, interaction: discord.Interaction, player: str, date: str = None):
        await self._send_player_abs(interaction, player, date, milb=False)

    @abs_command.autocomplete('player')
    async def abs_player_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)

    async def team_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        teams = [
            ("All Teams", "all"),
            ("Washington Nationals", "wsh"),
            ("Atlanta Braves", "atl"),
            ("Miami Marlins", "mia"),
            ("New York Mets", "nym"),
            ("Philadelphia Phillies", "phi"),
            ("Chicago Cubs", "chc"),
            ("Cincinnati Reds", "cin"),
            ("Milwaukee Brewers", "mil"),
            ("Pittsburgh Pirates", "pit"),
            ("St. Louis Cardinals", "stl"),
            ("Arizona Diamondbacks", "ari"),
            ("Colorado Rockies", "col"),
            ("Los Angeles Dodgers", "lad"),
            ("San Diego Padres", "sd"),
            ("San Francisco Giants", "sf"),
            ("Baltimore Orioles", "bal"),
            ("Boston Red Sox", "bos"),
            ("New York Yankees", "nyy"),
            ("Tampa Bay Rays", "tb"),
            ("Toronto Blue Jays", "tor"),
            ("Chicago White Sox", "cws"),
            ("Cleveland Guardians", "cle"),
            ("Detroit Tigers", "det"),
            ("Kansas City Royals", "kc"),
            ("Minnesota Twins", "min"),
            ("Houston Astros", "hou"),
            ("Los Angeles Angels", "laa"),
            ("Athletics", "ath"),
            ("Seattle Mariners", "sea"),
            ("Texas Rangers", "tex"),
        ]
        
        choices = []
        q = current.lower()
        for name, value in teams:
            if q in name.lower() or q in value.lower():
                choices.append(app_commands.Choice(name=name, value=value))
        return choices[:25]

    @mlb.command(name="sp", description="Get all scoring plays for a team in a given game.")
    @app_commands.describe(team="The team to get scoring plays for (e.g. wsh, nationals)")
    @app_commands.describe(date="A specific date (e.g. 4/7/26, yesterday, +2, -5)")
    async def scoring_plays(self, interaction: discord.Interaction, team: str, date: str = None):
        await interaction.response.defer()
        parsed_date = parse_date(date)

        games = await self.bot.mlb_client.get_games_with_scoring_plays(team, date=parsed_date)

        if not games:
            await interaction.followup.send("Could not find a game for that team on the specified date.")
            return
        
        embeds = []
        for i, game in enumerate(games, 1):
            embed = discord.Embed(color=discord.Color.blue())

            final_str = f"{game.status}/{game.inning}" if game.inning != 9 and game.inning > 0 else game.status
            title = f"🏁 {game.away.abbreviation} @ {game.home.abbreviation} - {final_str}" if game.abstract_state == "Final" else \
                    f"🔴 {game.away.abbreviation} @ {game.home.abbreviation} - {game.status}" if game.abstract_state == "Live" else \
                    f"🗓️ {game.away.abbreviation} @ {game.home.abbreviation} - {game.status}"
            if len(games) > 1: title += f" (Game {i})"
            embed.title = title
            
            desc = f"```python\n{game.format_score_line()}\n```\n"
            if game.scoring_plays:
                desc += "### Scoring Plays\n"
                for sp in game.scoring_plays:
                    desc += f"**{sp.inning.title()}:** {sp.description}\n"
                    if sp.video_url: desc += f"> [🎥 **{sp.video_blurb}**]({sp.video_url})\n"
                    desc += "\n"
            else:
                desc += "\nNo scoring plays found for this team in this game."

            embed.description = desc[:4096].strip()
            embeds.append(embed)

        await interaction.followup.send(embeds=embeds)

    @mlb.command(name="percentiles", description="Get a player's Baseball Savant percentiles")
    @app_commands.describe(player="Player name to search for", year="Target year (e.g., 2024)")
    async def percentiles(self, interaction: discord.Interaction, player: str, year: str = None):

        await interaction.response.defer()
        try:
            stats_raw = await self.bot.mlb_client.get_player_percentiles(player, year=year)

            if not stats_raw:
                await interaction.followup.send(f"No savant data found for **{player}**.")
                return

            embed = discord.Embed(
                title=f"{stats_raw.year} {stats_raw.stat_type} Percentiles — {stats_raw.player_name}",
                color=discord.Color.red()
            )
            stats_raw.apply_to_embed(embed)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"Error fetching percentiles: {e}")

    @percentiles.autocomplete('player')
    async def percentiles_player_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)



    @mlb.command(name="highlights", description="Get video highlights for a player or team")
    @app_commands.describe(query="Player or team name", date="Date (YYYY-MM-DD)")
    async def highlights(self, interaction: discord.Interaction, query: str, date: str = None):
        await interaction.response.defer()
        try:
            hl_list = await self.bot.mlb_client.get_highlights(query, date=date)
            if not hl_list:
                await interaction.followup.send(f"No highlights found for **{query}**.")
                return

            embed = discord.Embed(
                title=f"Highlights: {query.title()}",
                color=discord.Color.blue()
            )
            
            desc = ""
            for hi in hl_list:
                line = f"🎥 **[{hi.title}]({hi.url})** ({hi.duration})\n"
                if hi.description:
                    line += f"> *{hi.description}*\n\n"
                else:
                    line += "\n"
                
                # Discord embeds max out at 4096 characters in the description
                if len(desc) + len(line) > 4096:
                    break
                desc += line
                    
            embed.description = desc.strip()
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"Error fetching highlights: {e}")

    @mlb.command(name="standings", description="Get MLB standings by league, division, or wildcard")
    @app_commands.describe(query="Standings to display. Defaults to NL East.")
    @app_commands.choices(query=[
        app_commands.Choice(name="NL East", value="NL East"),
        app_commands.Choice(name="NL Central", value="NL Central"),
        app_commands.Choice(name="NL West", value="NL West"),
        app_commands.Choice(name="AL East", value="AL East"),
        app_commands.Choice(name="AL Central", value="AL Central"),
        app_commands.Choice(name="AL West", value="AL West"),
        app_commands.Choice(name="NL Wildcard", value="NL Wildcard"),
        app_commands.Choice(name="AL Wildcard", value="AL Wildcard"),
        app_commands.Choice(name="All Wildcards", value="Wildcard"),
        app_commands.Choice(name="National League (All)", value="NL"),
        app_commands.Choice(name="American League (All)", value="AL"),
        app_commands.Choice(name="All MLB Divisions", value="All"),
    ])
    async def standings(self, interaction: discord.Interaction, query: app_commands.Choice[str]):
        await interaction.response.defer()
        try:
            target = query.value if query else "NL East"
            if target == "All":
                target = None

            groups = await self.bot.mlb_client.get_standings(target)
            if not groups:
                await interaction.followup.send("Could not find matching standings.")
                return
            
            embeds = []
            is_wc = "wc" in (target or "").lower() or "wild" in (target or "").lower()
            
            for grp in groups:
                embed = discord.Embed(
                    title=grp.title,
                    color=discord.Color.green(),
                    description=f"```\n{grp.format_discord_code_block(is_wc=is_wc)}\n```"
                )
                embeds.append(embed)
            
            await interaction.followup.send(embeds=embeds[:10])
        except Exception as e:
            await interaction.followup.send(f"Error fetching standings: {e}")

    @mlb.command(name="matchup", description="Get career stats for a team's roster against a pitcher")
    @app_commands.describe(team="Team abbreviation or name", pitcher="Pitcher name")
    async def matchup(self, interaction: discord.Interaction, team: str, pitcher: str):

        await interaction.response.defer()
        try:
            data = await self.bot.mlb_client.get_matchup(team, pitcher)
            if not data or not data['matchups']:
                await interaction.followup.send(f"No career matchup data found for **{team}** hitters against **{pitcher}**.")
                return

            pitcher_name = data['pitcher']
            matchups = data['matchups']
            
            embed = discord.Embed(
                title=f"⚔️ Matchup: {team.upper()} vs {pitcher_name}",
                color=discord.Color.dark_red()
            )
            
            # Create Table
            header = "BATTER          PA  AVG    OPS  HR  SO\n"
            rows = []
            for m in matchups[:20]: # Show top 20 by PA
                name = m.batter_name[:14].ljust(14)
                pa = str(m.pa).rjust(3)
                avg = m.avg.rjust(5)
                ops = m.ops.rjust(6)
                hr = str(m.hr).rjust(3)
                so = str(m.so).rjust(3)
                rows.append(f"{name} {pa} {avg} {ops} {hr} {so}")
            
            table = f"```\n{header}{'='*40}\n" + "\n".join(rows) + "\n```"
            embed.description = table

            # Buckets
            hitter_owns = []
            pitcher_owns = []
            
            for m in matchups:
                if m.pa < 5: continue
                
                try:
                    ops_f = float(m.ops)
                except: ops_f = 0.0
                
                if ops_f > 1.100:
                    hitter_owns.append(f"**{m.batter_name}** ({m.pa} PA, {m.ops} OPS)")
                elif ops_f < .500:
                    pitcher_owns.append(f"**{m.batter_name}** ({m.pa} PA, {m.ops} OPS)")
            
            if hitter_owns:
                embed.add_field(name="👑 Hitter Owns Pitcher", value="\n".join(hitter_owns[:5]), inline=False)
            if pitcher_owns:
                embed.add_field(name="🔒 Pitcher Owns Hitter", value="\n".join(pitcher_owns[:5]), inline=False)
                
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"Error fetching matchup: {e}")

    @matchup.autocomplete('pitcher')
    async def matchup_pitcher_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)

    @matchup.autocomplete('team')
    async def matchup_team_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_autocomplete(interaction, current)


    @mlb.command(name="arsenal", description="Get a pitcher's pitch arsenal breakdown from Savant")
    @app_commands.describe(player="Pitcher name", year="Year (e.g. 2025)")
    async def arsenal(self, interaction: discord.Interaction, player: str, year: str = None):

        await interaction.response.defer()
        try:
            data = await self.bot.mlb_client.get_pitch_arsenal(player, year=year)
            if not data:
                await interaction.followup.send(f"No pitch arsenal data found for **{player}**. They may not be a pitcher or may not have enough data.")
                return

            embed = discord.Embed(
                title=f"⚾ {data.year} Pitch Arsenal — {data.player_name} ({data.team})",
                color=discord.Color.dark_teal(),
                description=f"```\n{data.format_discord_code_block()}\n```"
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"Error fetching arsenal: {e}")

    @arsenal.autocomplete('player')
    async def arsenal_player_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)


    @mlb.command(name="savant_leaders", description="Get Statcast leaderboards from Baseball Savant")
    @app_commands.describe(
        stat="Statcast metric to rank by",
        player_type="Batters or Pitchers",
        year="Year (e.g. 2025)",
        count="Number of players to show (default 10)"
    )
    @app_commands.choices(stat=[
        app_commands.Choice(name="Exit Velocity", value="exit_velocity_avg"),
        app_commands.Choice(name="Barrel %", value="barrel_batted_rate"),
        app_commands.Choice(name="Hard Hit %", value="hard_hit_percent"),
        app_commands.Choice(name="xBA", value="xba"),
        app_commands.Choice(name="xSLG", value="xslg"),
        app_commands.Choice(name="xwOBA", value="xwoba"),
        app_commands.Choice(name="K %", value="k_percent"),
        app_commands.Choice(name="BB %", value="bb_percent"),
        app_commands.Choice(name="Whiff %", value="whiff_percent"),
        app_commands.Choice(name="Chase Rate", value="chase_percent"),
        app_commands.Choice(name="Sprint Speed", value="sprint_speed"),
        app_commands.Choice(name="OAA", value="outs_above_average"),
        app_commands.Choice(name="Sweet Spot %", value="sweet_spot_percent"),
        app_commands.Choice(name="xERA", value="xera"),
        app_commands.Choice(name="Bat Speed", value="bat_speed"),
        app_commands.Choice(name="Swing Length", value="swing_length"),
    ])
    @app_commands.choices(player_type=[
        app_commands.Choice(name="Batters", value="batter"),
        app_commands.Choice(name="Pitchers", value="pitcher"),
    ])
    async def savant_leaders(self, interaction: discord.Interaction, stat: app_commands.Choice[str], player_type: app_commands.Choice[str] = None, year: str = None, count: int = 10):
        await interaction.response.defer()
        try:
            p_type = player_type.value if player_type else "batter"
            count = min(max(count, 1), 25)
            data = await self.bot.mlb_client.get_savant_leaderboard(stat.value, year=year, player_type=p_type, count=count)
            if not data:
                await interaction.followup.send(f"No Savant leaderboard data found for **{stat.name}**.")
                return

            embed = discord.Embed(
                title=f"📊 {data.title} ({p_type.title()}s)",
                color=discord.Color.gold(),
                description=f"```\n{data.format_discord_code_block()}\n```"
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"Error fetching leaderboard: {e}")

    @mlb.command(name="stats", description="Get a player's season or career stats")
    @app_commands.describe(player="The player to search for")
    @app_commands.describe(year="A specific year or range (e.g. 2020-2023). Blank for most recent.")
    @app_commands.describe(stat_type="Hitting or Pitching. Leave blank for default.")
    @app_commands.describe(career="Get career totals instead of a single season")
    @app_commands.choices(stat_type=[
        app_commands.Choice(name="Hitting", value="hitting"),
        app_commands.Choice(name="Pitching", value="pitching")
    ])
    async def stats(self, interaction: discord.Interaction, player: str, year: str = None, stat_type: app_commands.Choice[str] = None, career: bool = False):

        await interaction.response.defer()

        s_type = stat_type.value if stat_type else None

        season_stats_list = await self.bot.mlb_client.get_player_season_stats(player, stat_type=s_type, year=year, career=career)

        if not season_stats_list:
            await interaction.followup.send("Could not find stats for that player.")
            return

        embed = discord.Embed(color=discord.Color.blue())
        
        first_stats = season_stats_list[0]
        display_team = first_stats.team_abbrev
        if not first_stats.is_career and first_stats.stats:
            teams = []
            for s in first_stats.stats:
                t = s.get('team')
                if t and t not in ['MLB', 'MiLB'] and t not in teams:
                    teams.append(t)
            if teams:
                display_team = "/".join(teams)

        if first_stats.is_career:
            years_str = f" ({first_stats.years})" if first_stats.years and first_stats.years != "Career" else ""
            if len(season_stats_list) > 1:
                embed.title = f"Career Stats for {first_stats.player_name}{years_str}"
            else:
                embed.title = f"Career {first_stats.stat_type.capitalize()} Stats for {first_stats.player_name}{years_str}"
        else:
            if len(season_stats_list) > 1:
                embed.title = f"{first_stats.years} Stats for {first_stats.player_name} ({display_team})"
            else:
                embed.title = f"{first_stats.years} {first_stats.stat_type.capitalize()} Stats for {first_stats.player_name} ({display_team})"
            
        description = f"{first_stats.info_line}\n\n"
        for st in season_stats_list:
            if len(season_stats_list) > 1:
                prefix = "Career " if st.is_career else f"{st.years} "
                description += f"*{prefix}{st.stat_type.capitalize()}*\n"
            description += f"```python\n{st.format_discord_code_block()}\n```\n"
            
        embed.description = description.strip()
        if first_stats.headshot_url:
            embed.set_thumbnail(url=first_stats.headshot_url)
        
        await interaction.followup.send(embed=embed)

    @stats.autocomplete('player')
    async def stats_player_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)


    @mlb.command(name="last", description="Get a player's stats over their last N games")
    @app_commands.describe(player="The player to search for")
    @app_commands.describe(games="Number of recent games to aggregate (default 10, max 50)")
    @app_commands.describe(stat_type="Hitting or Pitching. Leave blank for default.")
    @app_commands.choices(stat_type=[
        app_commands.Choice(name="Hitting", value="hitting"),
        app_commands.Choice(name="Pitching", value="pitching")
    ])
    async def last_games(self, interaction: discord.Interaction, player: str, games: int = 10, stat_type: app_commands.Choice[str] = None):

        await interaction.response.defer()
        num_games = max(1, min(50, games))
        s_type = stat_type.value if stat_type else None

        stats_list = await self.bot.mlb_client.get_player_last_games(player, num_games=num_games, stat_type=s_type)

        if not stats_list:
            await interaction.followup.send("Could not find stats for that player.")
            return

        embed = discord.Embed(color=discord.Color.blue())

        first_stats = stats_list[0]
        if len(stats_list) > 1:
            embed.title = f"Last {num_games} Games for {first_stats.player_name} ({first_stats.team_abbrev})"
        else:
            embed.title = f"Last {num_games} Games {first_stats.stat_type.capitalize()} for {first_stats.player_name} ({first_stats.team_abbrev})"

        description = f"{first_stats.info_line}\n\n"
        for st in stats_list:
            if len(stats_list) > 1:
                description += f"*{st.stat_type.capitalize()}*\n"
            description += f"```python\n{st.format_discord_code_block()}\n```\n"

        embed.description = description.strip()
        if first_stats.headshot_url:
            embed.set_thumbnail(url=first_stats.headshot_url)

        await interaction.followup.send(embed=embed)

    @last_games.autocomplete('player')
    async def last_games_player_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)


    @mlb.command(name="compare", description="Compare multiple players' stats side-by-side")
    @app_commands.describe(players="Player names separated by / (e.g. soto/abrams/wood)")
    @app_commands.describe(year="A specific year (e.g. 2023). Leave blank for current.")
    @app_commands.describe(stat_type="Hitting or Pitching. Leave blank to auto-detect.")
    @app_commands.describe(career="Compare career totals instead of a single season")
    @app_commands.choices(stat_type=[
        app_commands.Choice(name="Hitting", value="hitting"),
        app_commands.Choice(name="Pitching", value="pitching")
    ])
    async def compare(self, interaction: discord.Interaction, players: str, year: str = None, stat_type: app_commands.Choice[str] = None, career: bool = False):
        await interaction.response.defer()

        player_names = [p.strip() for p in players.split('/') if p.strip()]
        if len(player_names) < 2:
            await interaction.followup.send("Please provide at least 2 players separated by `/` (e.g. `soto/abrams`).")
            return

        s_type = stat_type.value if stat_type else None
        result = await self.bot.mlb_client.get_compare_stats(player_names, stat_type=s_type, year=year, career=career)

        if not result:
            await interaction.followup.send("Could not find stats for those players.")
            return

        embed = discord.Embed(color=discord.Color.blue())
        embed.title = result.title

        desc = f"```python\n{result.format_discord_code_block()}\n```"
        if result.errors:
            desc += "\n" + "\n".join(f"⚠️ {e}" for e in result.errors)

        embed.description = desc
        await interaction.followup.send(embed=embed)

    @milb.command(name="stats", description="Get a minor league player's season or career stats")
    @app_commands.describe(player="The minor league player to search for")
    @app_commands.describe(year="A specific year (e.g. 2023). Leave blank for most recent.")
    @app_commands.describe(stat_type="Hitting or Pitching. Leave blank for default.")
    @app_commands.describe(career="Get career totals instead of a single season")
    @app_commands.choices(stat_type=[
        app_commands.Choice(name="Hitting", value="hitting"),
        app_commands.Choice(name="Pitching", value="pitching")
    ])
    async def milb_stats(self, interaction: discord.Interaction, player: str, year: str = None, stat_type: app_commands.Choice[str] = None, career: bool = False):
        await interaction.response.defer()
        s_type = stat_type.value if stat_type else None
        season_stats_list = await self.bot.mlb_client.get_player_season_stats(player, stat_type=s_type, year=year, career=career, milb=True)

        if not season_stats_list:
            await interaction.followup.send("Could not find stats for that minor league player.")
            return

        embed = discord.Embed(color=discord.Color.blue())
        first_stats = season_stats_list[0]
        display_team = first_stats.team_abbrev
        if not first_stats.is_career and first_stats.stats:
            teams = [s.get('team') for s in first_stats.stats if s.get('team') and s.get('team') not in ['MLB', 'MiLB']]
            if teams:
                display_team = "/".join(dict.fromkeys(teams))
                
        if first_stats.parent_org_abbrev:
            level_str = f" {first_stats.level_abbrev}" if first_stats.level_abbrev else ""
            display_team = f"{display_team} - {first_stats.parent_org_abbrev}{level_str}"
        elif first_stats.level_abbrev:
            display_team = f"{display_team} ({first_stats.level_abbrev})"

        years_str = f" ({first_stats.years})" if first_stats.years and first_stats.years != "Career" else ""
        embed.title = f"{'Career' if first_stats.is_career else first_stats.years} Stats for {first_stats.player_name}{years_str if first_stats.is_career else ' (' + display_team + ')'}"
            
        description = f"{first_stats.info_line}\n\n"
        for st in season_stats_list:
            if len(season_stats_list) > 1:
                description += f"*{'Career ' if st.is_career else st.years + ' '}{st.stat_type.capitalize()}*\n"
            description += f"```python\n{st.format_discord_code_block()}\n```\n"
            
        embed.description = description.strip()
        if first_stats.headshot_url:
            embed.set_thumbnail(url=first_stats.headshot_url)
        await interaction.followup.send(embed=embed)

    @milb_stats.autocomplete('player')
    async def milb_stats_player_autocomplete(self, interaction: discord.Interaction, current: str):
        if len(current) < 3: return []
        players = await self.bot.mlb_client.search_players(current, milb=True)
        nats_choices, other_choices = [], []
        for p in players:
            team, name = p.get('name_display_club', 'Unknown'), p.get('name', 'Unknown')
            choice = app_commands.Choice(name=f"{name} ({team})"[:100], value=str(p.get('id', '')))
            nats_choices.append(choice) if any(aff in team.lower() for aff in ['nationals', 'senators', 'red wings', 'blue rocks', 'frednats', 'rochester', 'harrisburg', 'wilmington', 'fredericksburg']) else other_choices.append(choice)
        return (nats_choices + other_choices)[:25]

    @milb.command(name="line", description="Get a minor league player's stat line for today or a specific date")
    @app_commands.describe(player="The minor league player to search for")
    @app_commands.describe(date="A specific date (e.g. 4/7/26, yesterday, +2, -5)")
    async def milb_line(self, interaction: discord.Interaction, player: str, date: str = None):
        await interaction.response.defer()
        parsed_date = parse_date(date)

        stats_list = await self.bot.mlb_client.get_player_game_stats(player, date=parsed_date, milb=True)

        if not stats_list:
            await interaction.followup.send("Could not find stats for that minor league player.")
            return

        embed = discord.Embed(color=discord.Color.blue())
        
        if len(stats_list) == 1:
            stats = stats_list[0]
            embed.title = f"{stats.player_name} ({stats.team_abbrev}) {stats.date} {'vs' if stats.is_home else '@'} {stats.opp_abbrev}"
            embed.description = f"```python\n{stats.format_discord_code_block()}\n```"
        else:
            stats = stats_list[0]
            embed.title = f"{stats.player_name} ({stats.team_abbrev}) - {stats.date}"
            for i, s in enumerate(stats_list, 1):
                name = f"Game {i}: {'vs' if s.is_home else '@'} {s.opp_abbrev}"
                embed.add_field(name=name, value=f"```python\n{s.format_discord_code_block()}\n```", inline=False)
                
        if stats_list[0].headshot_url:
            embed.set_thumbnail(url=stats_list[0].headshot_url)
        
        # Add button if any game had batting stats
        view = None
        has_batting = any(s.batting_stats is not None for s in stats_list if not s.info_message)
        if has_batting:
            first = stats_list[0]
            view = PlayerAbsView(self, first.player_id, first.date, milb=True)
                
        await interaction.followup.send(embed=embed, view=view)



    @milb_line.autocomplete('player')
    async def milb_line_player_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.milb_stats_player_autocomplete(interaction, current)

    @milb.command(name="abs", description="Get a minor league player's at-bats and video highlights")
    @app_commands.describe(player="The minor league player to search for")
    @app_commands.describe(date="A specific date (e.g. 4/7/26, yesterday, +2, -5)")
    async def milb_abs(self, interaction: discord.Interaction, player: str, date: str = None):
        await self._send_player_abs(interaction, player, date, milb=True)

    @milb_abs.autocomplete('player')
    async def milb_abs_player_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.milb_stats_player_autocomplete(interaction, current)

    async def _send_player_abs(self, interaction: discord.Interaction, player: str, date: str, milb: bool, edit_original: bool = False):
        if edit_original:
            await interaction.response.defer()
        else:
            await interaction.response.defer()
        
        parsed_date = parse_date(date)


        stats_list = await self.bot.mlb_client.get_player_game_stats(player, date=parsed_date, milb=milb, include_abs=True)

        if not stats_list:
            await interaction.followup.send("Could not find stats for that player.")
            return

        embeds = []
        for i, stats in enumerate(stats_list, 1):
            embed = discord.Embed(color=discord.Color.blue())
            
            if len(stats_list) == 1:
                embed.title = f"{stats.player_name} ({stats.team_abbrev}) {stats.date} {'vs' if stats.is_home else '@'} {stats.opp_abbrev}"
            else:
                embed.title = f"{stats.player_name} ({stats.team_abbrev}) - {stats.date} (Game {i}: {'vs' if stats.is_home else '@'} {stats.opp_abbrev})"
                
            if stats.headshot_url:
                embed.set_thumbnail(url=stats.headshot_url)
                
            desc = f"```python\n{stats.format_discord_code_block()}\n```\n"
            
            if stats.at_bats:
                desc += "### Play-by-Play\n"
                for ab in stats.at_bats:
                    if not ab.is_complete:
                        desc += f"**{ab.inning.title()}:** Currently at bat.\n\n"
                        continue
                        
                    scoring = "__" if ab.is_scoring else ""
                    ab_text = f"**{ab.inning.title()}:** {scoring}With **{ab.pitcher_name}** pitching, {ab.description}{scoring}"
                    
                    if ab.pitch_data or ab.statcast_data:
                        extras = " | ".join(filter(None, [ab.pitch_data, ab.statcast_data]))
                        ab_text += f" *({extras})*"
                        
                    desc += ab_text + "\n"
                    
                    if ab.video_url:
                        desc += f"> [🎥 **{ab.video_blurb}**]({ab.video_url})\n"
                        
                    desc += "\n"
                    
            if len(desc) > 4096:
                desc = desc[:4093] + "..."
                
            embed.description = desc.strip()
            embeds.append(embed)
            
        if edit_original:
            await interaction.edit_original_response(embeds=embeds, view=None)
        else:
            await interaction.followup.send(embeds=embeds)


    DIVISION_TEAMS = {
        'nle': {'WSH', 'NYM', 'ATL', 'PHI', 'MIA'},
        'nlc': {'CHC', 'MIL', 'STL', 'CIN', 'PIT'},
        'nlw': {'LAD', 'SD', 'SF', 'ARI', 'COL'},
        'ale': {'NYY', 'BOS', 'BAL', 'TB', 'TOR'},
        'alc': {'CLE', 'MIN', 'DET', 'CWS', 'KC'},
        'alw': {'HOU', 'SEA', 'TEX', 'LAA', 'ATH'},
    }
    DIVISION_TEAMS['nl'] = DIVISION_TEAMS['nle'] | DIVISION_TEAMS['nlc'] | DIVISION_TEAMS['nlw']
    DIVISION_TEAMS['al'] = DIVISION_TEAMS['ale'] | DIVISION_TEAMS['alc'] | DIVISION_TEAMS['alw']

    @mlb.command(name="score", description="Get today's MLB games or a specific team's game")
    @app_commands.describe(team="The team abbreviation or name (e.g. wsh, lad). Default is Nats. Use 'all' for everyone.")
    @app_commands.describe(date="A specific date (e.g. 4/7/26, yesterday, +2, -5)")
    @app_commands.describe(live="Only show games currently in progress")
    @app_commands.describe(division="Filter by division or league")

    @app_commands.choices(division=[
        app_commands.Choice(name="NL East", value="nle"),
        app_commands.Choice(name="NL Central", value="nlc"),
        app_commands.Choice(name="NL West", value="nlw"),
        app_commands.Choice(name="AL East", value="ale"),
        app_commands.Choice(name="AL Central", value="alc"),
        app_commands.Choice(name="AL West", value="alw"),
        app_commands.Choice(name="National League", value="nl"),
        app_commands.Choice(name="American League", value="al"),
    ])
    async def score(self, interaction: discord.Interaction, team: str = None, date: str = None, live: bool = False, division: app_commands.Choice[str] = None):
        # Defer the response immediately. The MLB API might take longer than 3 seconds to respond.
        await interaction.response.defer()

        # Handle defaults: None -> WSH, "all" -> None
        team_query = team
        if team_query is None:
            team_query = "wsh"
        elif team_query.lower() == "all":
            team_query = None

        # Parse the date if provided
        parsed_date = parse_date(date)

        # Fetch the games using our new async API client
        games = await self.bot.mlb_client.get_todays_games(team_query=team_query, date=parsed_date)

        if live:
            games = [g for g in games if g.abstract_state == "Live"]

        if division:
            div_teams = self.DIVISION_TEAMS.get(division.value, set())
            games = [g for g in games if g.away.abbreviation in div_teams or g.home.abbreviation in div_teams]

        if games:
            embeds = []
            title = f"MLB Scores ({parsed_date})" if parsed_date else "MLB Scores"
            if division:
                title += f" - {division.name}"
            if live:
                title += " - Live"
            current_embed = discord.Embed(title=title, color=discord.Color.blue())
            
            for game in games:
                # Use emojis in the field title to indicate game status
                if game.abstract_state == "Live":
                    name = f"🔴 {game.away.abbreviation} @ {game.home.abbreviation} - {game.status}"
                elif game.abstract_state == "Final":
                    final_str = f"{game.status}/{game.inning}" if game.inning != 9 and game.inning > 0 else game.status
                    name = f"🏁 {game.away.abbreviation} @ {game.home.abbreviation} - {final_str}"
                else:
                    name = f"🗓️ {game.away.abbreviation} @ {game.home.abbreviation} - {game.status}"

                value = f"```python\n{game.format_score_line()}\n```"
                last_play = game.format_last_play()
                if last_play:
                    value += f"\n{last_play}"
                
                # Discord limits embeds to 25 fields and 6000 total characters
                if len(current_embed.fields) >= 25 or len(current_embed) + len(name) + len(value) > 5900:
                    embeds.append(current_embed)
                    current_embed = discord.Embed(title=f"{title} (Cont.)", color=discord.Color.blue())
                    
                current_embed.add_field(name=name, value=value, inline=False)
                
            embeds.append(current_embed)
            await interaction.followup.send(embeds=embeds)
        else:
            if division and live:
                msg = f"No live {division.name} games right now."
            elif division:
                msg = f"No {division.name} games found."
            elif live:
                msg = "No live games right now."
            else:
                msg = "No games found."
            await interaction.followup.send(msg)

    @score.autocomplete('team')
    async def score_team_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_autocomplete(interaction, current)


    @mlb.command(name="box", description="Get the box score for a team's game today")
    @app_commands.describe(team="The team abbreviation or name (e.g. wsh, dodgers)")
    @app_commands.describe(date="A specific date (e.g. 4/7/26, yesterday). Leave blank for today.")
    @app_commands.describe(part="Which part of the box score to show")
    @app_commands.choices(part=[
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="Batting", value="batting"),
        app_commands.Choice(name="Pitching", value="pitching"),
        app_commands.Choice(name="Notes and Info", value="notes_info"),
        app_commands.Choice(name="ABS Challenges", value="abs")
    ])
    async def box_score(self, interaction: discord.Interaction, team: str, date: str = None, part: app_commands.Choice[str] = None):
        await interaction.response.defer()

        parsed_date = parse_date(date)
        box = await self.bot.mlb_client.get_box_score(team_query=team, date=parsed_date)

        if not box:
            await interaction.followup.send("Could not find a game for that team/date.")
            return

        embed = discord.Embed(title=box.title, color=discord.Color.blue())
        show_part = part.value if part else "batting"

        desc = ""
        if show_part in ["all", "batting"]:
            desc += f"**{box.team_name} Batting**\n```python\n{box.format_batting()}\n```\n"

        if show_part in ["all", "pitching"]:
            desc += f"**{box.team_name} Pitching**\n```python\n{box.format_pitching()}\n```\n"

        if show_part == "abs":
            desc += box.format_abs_info()
            if not desc:
                desc = "No ABS Challenges found for this game."

        if show_part == "notes_info":
            desc += "No batting/pitching stats requested."

        if len(desc) > 4096:
            # If all are somehow too long for one embed description, split by block
            # This is extremely rare, but just in case
            embed.description = desc[:4093] + "..."
        else:
            embed.description = desc.strip()

        # Add notes and info as fields if they exist and fit, only if "all" or "notes_info" is selected
        if show_part in ["all", "notes_info"]:
            notes = ""
            if box.team_notes:
                notes += box.format_notes() + "\n"
            if box.game_info:
                notes += box.format_game_info()
            
            notes = notes.strip()
            if notes and len(embed) + len(notes) < 6000:
                if len(notes) > 1024:
                    notes = notes[:1021] + "..."
                embed.add_field(name="Notes & Info", value=notes, inline=False)
            elif not notes and show_part == "notes_info":
                embed.description = "No Notes or Game Info available."

        await interaction.followup.send(embed=embed)


    @mlb.command(name="bullpen", description="Get a team's bullpen availability and recent pitch counts")
    @app_commands.describe(team="The team abbreviation or name (e.g. wsh, dodgers)")
    async def bullpen(self, interaction: discord.Interaction, team: str):
        await interaction.response.defer()
        
        bullpen_data = await self.bot.mlb_client.get_bullpen(team_query=team)
        if not bullpen_data:
            await interaction.followup.send("Could not find bullpen data for that team. Make sure they have a game around today.")
            return

        embed = discord.Embed(title=f"{bullpen_data.team_name} Bullpen Availability", color=discord.Color.blue())
        embed.description = f"```\n{bullpen_data.format_table()}\n```"
        
        await interaction.followup.send(embed=embed)

    async def stat_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # Map common names to API internal stat keys
        stats_map = {
            "Home Runs (Hitting - HR)": "hitting|homeRuns",
            "Home Runs Allowed (Pitching - HR)": "pitching|homeRuns",
            "Batting Average (AVG)": "hitting|battingAverage",
            "Batting Average Against (BAA)": "pitching|battingAverage",
            "RBI": "hitting|runsBattedIn",
            "On Base Percentage (OBP)": "hitting|onBasePercentage",
            "Slugging Percentage (SLG)": "hitting|sluggingPercentage",
            "OPS": "hitting|onBasePlusSlugging",
            "Hits (Hitting - H)": "hitting|hits",
            "Hits Allowed (Pitching - H)": "pitching|hits",
            "Runs (Hitting - R)": "hitting|runs",
            "Runs Allowed (Pitching - R)": "pitching|runs",
            "Stolen Bases (SB)": "hitting|stolenBases",
            "Walks (Hitting - BB)": "hitting|walks",
            "Walks (Pitching - BB)": "pitching|walks",
            "Strikeouts (Hitting - SO)": "hitting|strikeouts", 
            "Wins (W)": "pitching|wins",
            "ERA": "pitching|earnedRunAverage",
            "WHIP": "pitching|walksAndHitsPerInningPitched",
            "Saves (SV)": "pitching|saves",
            "Strikeouts (Pitching - SO)": "pitching|strikeouts",
            "Innings Pitched (IP)": "pitching|inningsPitched",
            "Games Played (Hitting - G)": "hitting|gamesPlayed",
            "Games Played (Pitching - G)": "pitching|gamesPlayed",
            "Doubles (2B)": "hitting|doubles",
            "Triples (3B)": "hitting|triples",
            "At Bats (AB)": "hitting|atBats",
            "Total Bases (TB)": "hitting|totalBases",
        }
        raw_stats = ['assists', 'shutouts', 'homeRuns', 'sacrificeBunts', 'sacrificeFlies', 'runs', 'groundoutToFlyoutRatio', 'stolenBases', 'battingAverage', 'groundOuts', 'numberOfPitches', 'onBasePercentage', 'caughtStealing', 'groundIntoDoublePlays', 'totalBases', 'earnedRunAverage', 'fieldingPercentage', 'walksAndHitsPerInningPitched', 'flyouts', 'hitByPitches', 'gamesPlayed', 'walks', 'sluggingPercentage', 'onBasePlusSlugging', 'runsBattedIn', 'triples', 'extraBaseHits', 'hits', 'atBats', 'strikeouts', 'doubles', 'totalPlateAppearances', 'intentionalWalks', 'wins', 'losses', 'saves', 'wildPitch', 'airOuts', 'balk', 'blownSaves', 'catcherEarnedRunAverage', 'catchersInterference', 'chances', 'completeGames', 'doublePlays', 'earnedRun', 'errors', 'gamesFinished', 'gamesStarted', 'hitBatsman', 'hitsPer9Inn', 'holds', 'innings', 'inningsPitched', 'outfieldAssists', 'passedBalls', 'pickoffs', 'pitchesPerInning', 'putOuts', 'rangeFactorPerGame', 'rangeFactorPer9Inn', 'saveOpportunities', 'stolenBasePercentage', 'strikeoutsPer9Inn', 'strikeoutWalkRatio', 'throwingErrors', 'totalBattersFaced', 'triplePlays', 'walksPer9Inn', 'winPercentage']

        choices = []
        for readable, stat_id in stats_map.items():
            if current.lower() in readable.lower() or current.lower() in stat_id.lower():
                choices.append(app_commands.Choice(name=readable, value=stat_id))
                
        existing_values = {c.value.split('|')[1] if '|' in c.value else c.value for c in choices}
        for rs in raw_stats:
            if current.lower() in rs.lower() and rs not in existing_values:
                choices.append(app_commands.Choice(name=rs, value=rs))
                
        return choices[:25]

    @mlb.command(name="leaders", description="View MLB player stat leaderboards")
    @app_commands.describe(stat="The statistic to view leaders for")
    @app_commands.describe(league="The league to filter by (AL/NL/All)")
    @app_commands.describe(year="The year to view leaders for (e.g. 2023). Defaults to current year.")
    @app_commands.choices(league=[
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="AL", value="al"),
        app_commands.Choice(name="NL", value="nl")
    ])
    @app_commands.describe(position="Position filter (e.g. C, 1B, 2B, SS, 3B, OF, P, RP)")
    @app_commands.describe(player_pool="Player pool filter (Qualified, Rookies, All)")
    @app_commands.choices(player_pool=[
        app_commands.Choice(name="Qualified", value="QUALIFIED"),
        app_commands.Choice(name="All", value="ALL"),
        app_commands.Choice(name="Rookies", value="ROOKIES")
    ])
    @app_commands.describe(stat_group="Filter by stat group (Hitting/Pitching/etc.)")
    @app_commands.choices(stat_group=[
        app_commands.Choice(name="Hitting", value="hitting"),
        app_commands.Choice(name="Pitching", value="pitching"),
        app_commands.Choice(name="Fielding", value="fielding"),
        app_commands.Choice(name="Catching", value="catching")
    ])
    @app_commands.describe(team="Filter by a specific team (e.g. wsh, dodgers)")
    async def leaders(
        self, 
        interaction: discord.Interaction, 
        stat: str, 
        stat_group: app_commands.Choice[str] = None,
        league: app_commands.Choice[str] = None, 
        year: str = None,
        position: str = None, 
        player_pool: app_commands.Choice[str] = None,
        team: str = None
    ):

        await interaction.response.defer()
        
        team_id = None
        team_display = None
        if team:
            team_id = await self.bot.mlb_client.get_team_id(team)
            if not team_id:
                await interaction.followup.send("Could not find that team.")
                return
            team_display = team.upper() if len(team) <= 3 else team.capitalize()
        
        lg_val = league.value if league else None
        pool_val = player_pool.value if player_pool else None

        parts = stat.split("|")
        if len(parts) == 2:
            group_val = parts[0]
            stat_val = parts[1]
        else:
            default_pitching_stats = {"earnedRunAverage", "wins", "saves", "walksAndHitsPerInningPitched", "strikeoutsPer9Inn", "hitsPer9Inn", "walksPer9Inn", "homeRunsPer9", "strikeoutWalkRatio", "inningsPitched", "shutouts", "completeGames", "blownSaves", "holds", "balk", "wildPitch", "hitBatsman", "saveOpportunities", "runsScoredPer9"}
            group_val = stat_group.value if stat_group else ("pitching" if stat in default_pitching_stats else "hitting")
            stat_val = stat
        
        leaders_list = await self.bot.mlb_client.get_leaders(stat=stat_val, stat_group=group_val, league=lg_val, position=position, player_pool=pool_val, team_id=team_id, year=year)

        if not leaders_list:
            await interaction.followup.send("Could not find any leaders for those filters.")
            return
            
        desc = "```python\n"
        for leader in leaders_list:
            desc += f"{leader.format()}\n"
        desc += "```"

        title_parts = []
        if year: title_parts.append(year)
        if team_display: title_parts.append(team_display)

        if pool_val == "ROOKIES": title_parts.append("Rookie")
        if lg_val and lg_val != "all": title_parts.append(lg_val.upper())
        if position: title_parts.append(position.upper())
        
        display_stat = stat_val.capitalize()
        stats_map_display = {
            "Home Runs": "homeRuns", "Batting Average": "battingAverage", "RBI": "runsBattedIn",
            "OBP": "onBasePercentage", "SLG": "sluggingPercentage", "OPS": "onBasePlusSlugging",
            "Hits": "hits", "Runs": "runs", "Stolen Bases": "stolenBases", "Walks": "walks",
            "Strikeouts": "strikeouts", "Wins": "wins", "ERA": "earnedRunAverage", 
            "WHIP": "walksAndHitsPerInningPitched", "Saves": "saves", "Innings Pitched": "inningsPitched",
            "Games Played": "gamesPlayed", "Doubles": "doubles", "Triples": "triples", 
            "At Bats": "atBats", "Total Bases": "totalBases"
        }
        for readable, stat_id in stats_map_display.items():
            if stat_id == stat_val:
                display_stat = readable
                break

        if display_stat in ["Strikeouts", "Walks", "Home Runs", "Hits", "Runs", "Games Played", "Doubles", "Triples", "Batting Average"]:
            if group_val == 'pitching' and display_stat in ["Home Runs", "Hits", "Runs", "Doubles", "Triples"]:
                display_stat = f"{display_stat} Allowed"
            elif group_val == 'pitching' and display_stat == "Batting Average":
                display_stat = "Batting Average Against (BAA)"
            else:
                display_stat = f"{display_stat} ({group_val.capitalize()})"
                
        title_parts.append(display_stat + " Leaders")
        title_str = " ".join(title_parts)

        embed = discord.Embed(title=title_str, description=desc, color=discord.Color.blue())
        await interaction.followup.send(embed=embed)


    @leaders.autocomplete('stat')
    async def leaders_stat_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.stat_autocomplete(interaction, current)

    @leaders.autocomplete('team')
    async def leaders_team_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_autocomplete(interaction, current)


    @mlb.command(name="team_leaders", description="View MLB team stat leaderboards")
    @app_commands.describe(stat="The statistic to view leaders for")
    @app_commands.describe(league="The league to filter by (AL/NL/All)")
    @app_commands.describe(year="The year to view leaders for (e.g. 2023). Defaults to current year.")


    @app_commands.choices(league=[
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="AL", value="103"),
        app_commands.Choice(name="NL", value="104")
    ])
    async def team_leaders(
        self, 
        interaction: discord.Interaction, 
        stat: str, 
        league: app_commands.Choice[str] = None,
        year: str = None
    ):

        await interaction.response.defer()
        
        lg_val = league.value if league else None

        parts = stat.split("|")
        if len(parts) == 2:
            group_val = parts[0]
            stat_val = parts[1]
        else:
            default_pitching_stats = {"earnedRunAverage", "wins", "saves", "walksAndHitsPerInningPitched", "strikeoutsPer9Inn", "hitsPer9Inn", "walksPer9Inn", "homeRunsPer9", "strikeoutWalkRatio", "inningsPitched", "shutouts", "completeGames", "blownSaves", "holds", "balk", "wildPitch", "hitBatsman", "saveOpportunities", "runsScoredPer9"}
            group_val = "pitching" if stat in default_pitching_stats else "hitting"
            stat_val = stat
            
        leaders_list = await self.bot.mlb_client.get_team_leaders(stat=stat_val, stat_group=group_val, league=lg_val, year=year)

        if not leaders_list:
            await interaction.followup.send("Could not find any team leaders for those filters.")
            return
            
        desc = "```python\n"
        for leader in leaders_list:
            desc += f"{leader.format(is_team=True)}\n"
        desc += "```"

        title_parts = []
        if year: title_parts.append(year)
        if lg_val:
            title_parts.append("AL" if lg_val == "103" else "NL")

            
        display_stat = stat_val.capitalize()
        stats_map_display = {
            "Home Runs": "homeRuns", "Batting Average": "battingAverage", "RBI": "runsBattedIn",
            "OBP": "onBasePercentage", "SLG": "sluggingPercentage", "OPS": "onBasePlusSlugging",
            "Hits": "hits", "Runs": "runs", "Stolen Bases": "stolenBases", "Walks": "walks",
            "Strikeouts": "strikeouts", "Wins": "wins", "ERA": "earnedRunAverage", 
            "WHIP": "walksAndHitsPerInningPitched", "Saves": "saves", "Innings Pitched": "inningsPitched",
            "Games Played": "gamesPlayed", "Doubles": "doubles", "Triples": "triples", 
            "At Bats": "atBats", "Total Bases": "totalBases"
        }
        for readable, stat_id in stats_map_display.items():
            if stat_id == stat_val:
                display_stat = readable
                break
                
        if display_stat in ["Strikeouts", "Walks", "Home Runs", "Hits", "Runs", "Games Played", "Doubles", "Triples", "Batting Average"]:
            if group_val == 'pitching' and display_stat in ["Home Runs", "Hits", "Runs", "Doubles", "Triples"]:
                display_stat = f"{display_stat} Allowed"
            elif group_val == 'pitching' and display_stat == "Batting Average":
                display_stat = "Batting Average Against (BAA)"
            else:
                display_stat = f"{display_stat} ({group_val.capitalize()})"
                
        title_parts.append(display_stat + " Team Leaders")
        title_str = " ".join(title_parts)

        embed = discord.Embed(title=title_str, description=desc, color=discord.Color.blue())
        await interaction.followup.send(embed=embed)

    @team_leaders.autocomplete('stat')
    async def team_leaders_stat_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.stat_autocomplete(interaction, current)



    @mlb.command(name="next", description="Get the upcoming games for a team")
    @app_commands.describe(team="The team abbreviation or name (e.g. wsh, dodgers)")
    @app_commands.describe(games="Number of games to show (default 3, max 10)")
    async def next_games(self, interaction: discord.Interaction, team: str, games: int = 3):
        await self._send_schedule(interaction, team, games, past=False)

    @mlb.command(name="past", description="Get the recently completed games for a team")
    @app_commands.describe(team="The team abbreviation or name (e.g. wsh, dodgers)")
    @app_commands.describe(games="Number of games to show (default 3, max 10)")
    async def past_games(self, interaction: discord.Interaction, team: str, games: int = 3):
        await self._send_schedule(interaction, team, games, past=True)

    async def _send_schedule(self, interaction: discord.Interaction, team: str, num_games: int, past: bool):
        await interaction.response.defer()
        num_games = max(1, min(10, num_games))

        games = await self.bot.mlb_client.get_team_schedule(team, num_games=num_games, past=past)

        if not games:
            await interaction.followup.send(f"Could not find any {'past' if past else 'upcoming'} games for that team.")
            return

        direction = "Past" if past else "Next"
        
        target_abbr = team.upper()
        query = team.lower()
        aliases = {"nats": "nationals", "yanks": "yankees", "cards": "cardinals", "dbacks": "diamondbacks", "barves": "braves"}
        query = aliases.get(query, query)
        for g in games:
            if query == g.away.abbreviation.lower() or query in g.away.name.lower():
                target_abbr = g.away.abbreviation
                break
            elif query == g.home.abbreviation.lower() or query in g.home.name.lower():
                target_abbr = g.home.abbreviation
                break

        embeds = []
        title = f"{direction} {len(games)} Games for {target_abbr}"
        current_embed = discord.Embed(title=title, color=discord.Color.blue())
        
        for game in games:
            date_str = game.game_date_str or "Unknown Date"
            if game.abstract_state == "Live":
                name = f"🔴 {game.away.abbreviation} @ {game.home.abbreviation} - {game.status} ({date_str})"
            elif game.abstract_state == "Final":
                final_str = f"{game.status}/{game.inning}" if game.inning != 9 and game.inning > 0 else game.status
                name = f"🏁 {game.away.abbreviation} @ {game.home.abbreviation} - {final_str} ({date_str})"
            else:
                name = f"🗓️ {game.away.abbreviation} @ {game.home.abbreviation} - {game.status} ({date_str})"

            value = f"```python\n{game.format_score_line()}\n```"
            
            if len(current_embed.fields) >= 25 or len(current_embed) + len(name) + len(value) > 5900:
                embeds.append(current_embed)
                current_embed = discord.Embed(title=f"{title} (Cont.)", color=discord.Color.blue())
                
            current_embed.add_field(name=name, value=value, inline=False)
            
        embeds.append(current_embed)
        await interaction.followup.send(embeds=embeds)

async def setup(bot):
    await bot.add_cog(MLBSlash(bot))