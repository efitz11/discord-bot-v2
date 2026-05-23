import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import io
from cogs.mlb import MLBSlash
from core.mlb_client import PlayerGameStats, PlayerPercentiles, StandingsGroup, BullpenData, BoxScoreData

@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.mlb_client = AsyncMock()
    bot.favorite_team = "WSH"
    bot.favorite_team_name = "nationals"
    return bot

@pytest.fixture
def cog(mock_bot):
    return MLBSlash(bot=mock_bot)


@pytest.mark.asyncio
async def test_line_command_batting(cog, mock_bot):
    # Mock return stats list from MLBClient
    mock_stats = MagicMock(spec=PlayerGameStats)
    mock_stats.player_name = "Fernando Tatis Jr."
    mock_stats.player_id = 665489
    mock_stats.date = "2026-05-23"
    mock_stats.team_abbrev = "SD"
    mock_stats.opp_abbrev = "WSH"
    mock_stats.is_home = True
    mock_stats.headshot_url = "https://headshots.mlb.com/665489.png"
    mock_stats.batting_stats = {"AB": 3, "R": 2, "H": 2, "HR": 1, "RBI": 3}
    mock_stats.pitching_stats = None
    mock_stats.info_message = None
    mock_stats.format_discord_code_block.return_value = "AB  R  H HR RBI\n3  2  2  1   3"

    mock_bot.mlb_client.get_player_game_stats.return_value = [mock_stats]

    # Mock interaction
    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    # Call command via callback
    await cog.line.callback(cog, mock_interaction, player="665489", date="today")

    # Verify defer and call
    mock_interaction.response.defer.assert_called_once()
    mock_bot.mlb_client.get_player_game_stats.assert_called_once()

    # Verify followup message contains our embed and a PlayerAbsView (since he batted but didn't pitch)
    args, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    assert "view" in kwargs
    embed = kwargs["embed"]
    assert embed.title == "Fernando Tatis Jr. (SD) 2026-05-23 vs WSH"
    assert "AB  R  H HR RBI" in embed.description
    assert embed.thumbnail.url == "https://headshots.mlb.com/665489.png"


@pytest.mark.asyncio
async def test_percentiles_command(cog, mock_bot):
    mock_percentiles = MagicMock(spec=PlayerPercentiles)
    mock_percentiles.player_name = "Juan Soto"
    mock_percentiles.team_abbrev = "NYY"
    mock_percentiles.stat_type = "Batter"
    mock_percentiles.year = "2026"
    
    mock_bot.mlb_client.get_player_percentiles.return_value = mock_percentiles

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.percentiles.callback(cog, mock_interaction, player="665742", year="2026")

    mock_interaction.response.defer.assert_called_once()
    mock_bot.mlb_client.get_player_percentiles.assert_called_once_with("665742", year="2026")
    mock_percentiles.apply_to_embed.assert_called_once()

    args, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    embed = kwargs["embed"]
    assert embed.title == "2026 Batter Percentiles — Juan Soto (NYY)"


@pytest.mark.asyncio
async def test_compare_percentiles_command(cog, mock_bot):
    p1 = MagicMock(spec=PlayerPercentiles)
    p1.player_name = "Juan Soto"
    p1.team_abbrev = "NYY"
    p1.stat_type = "Batter"
    p1.year = "2026"
    p1.percentiles = [{"stat": "xwoba", "value": 98, "raw": 0.420}]

    p2 = MagicMock(spec=PlayerPercentiles)
    p2.player_name = "Aaron Judge"
    p2.team_abbrev = "NYY"
    p2.stat_type = "Batter"
    p2.year = "2026"
    p2.percentiles = [{"stat": "xwoba", "value": 99, "raw": 0.435}]

    mock_bot.mlb_client.get_player_percentiles.side_effect = [p1, p2]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    # Patch image generator to return mock bytes
    mock_buf = io.BytesIO(b"FakeImageBytes")
    with patch("cogs.mlb.generate_compare_percentiles_image", return_value=mock_buf) as mock_gen:
        await cog.compare_percentiles.callback(cog, mock_interaction, player1="Juan Soto", player2="Aaron Judge", year="2026")

    mock_interaction.response.defer.assert_called_once()
    assert mock_bot.mlb_client.get_player_percentiles.call_count == 2
    mock_gen.assert_called_once()

    args, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    assert "file" in kwargs
    assert isinstance(kwargs["file"], discord.File)
    assert kwargs["file"].filename == "percentile_comparison.png"


@pytest.mark.asyncio
async def test_bullpen_command(cog, mock_bot):
    mock_bd = MagicMock(spec=BullpenData)
    mock_bd.team_name = "Washington Nationals"
    mock_bd.format_table.return_value = "Kyle Finnegan: Fresh\nJake Irvin: SP"
    mock_bot.mlb_client.get_bullpen.return_value = mock_bd

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.bullpen.callback(cog, mock_interaction, team="nats")

    mock_interaction.response.defer.assert_called_once()
    mock_bot.mlb_client.get_bullpen.assert_called_once_with(team_query="nats")
    
    args, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    embed = kwargs["embed"]
    assert embed.title == "Washington Nationals Bullpen Availability"
    assert "Kyle Finnegan: Fresh" in embed.description


@pytest.mark.asyncio
async def test_standings_command(cog, mock_bot):
    mock_group = MagicMock(spec=StandingsGroup)
    mock_group.title = "NL East"
    mock_group.format_discord_code_block.return_value = "WSH  10  5  .667"
    mock_bot.mlb_client.get_standings.return_value = [mock_group]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = MagicMock()
    mock_interaction.followup.send = AsyncMock()

    # Pass in NL East Choice
    choice = discord.app_commands.Choice(name="NL East", value="NL East")
    await cog.standings.callback(cog, mock_interaction, query=choice)

    mock_interaction.response.defer.assert_called_once()
    mock_bot.mlb_client.get_standings.assert_called_once_with("NL East")
    
    args, kwargs = mock_interaction.followup.send.call_args
    assert "embeds" in kwargs
    embeds = kwargs["embeds"]
    assert len(embeds) == 1
    assert embeds[0].title == "NL East"
    assert "WSH  10  5  .667" in embeds[0].description


from core.mlb_client import BatterVsPitcher

@pytest.mark.asyncio
async def test_box_score_command(cog, mock_bot):
    mock_box = MagicMock(spec=BoxScoreData)
    mock_box.title = "Washington Nationals 4, Atlanta Braves 2"
    mock_box.team_name = "Washington Nationals"
    mock_box.team_abbrev = "WSH"
    mock_box.format_batting.return_value = "Tatis Jr. RF  4  1  2  2  0  1"
    mock_box.format_pitching.return_value = "Finnegan S  1.0  0  0  0  0  1"
    mock_box.team_notes = None
    mock_box.game_info = None

    mock_bot.mlb_client.get_box_score.return_value = mock_box

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.box_score.callback(cog, mock_interaction, team="nats")

    mock_interaction.response.defer.assert_called_once()
    mock_bot.mlb_client.get_box_score.assert_called_once_with(team_query="nats", date=None)
    
    args, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    embed = kwargs["embed"]
    assert embed.title == "Washington Nationals 4, Atlanta Braves 2"
    assert "Tatis Jr." in embed.description


@pytest.mark.asyncio
async def test_matchup_command(cog, mock_bot):
    m1 = MagicMock(spec=BatterVsPitcher)
    m1.batter_name = "Fernando Tatis Jr."
    m1.pa = 10
    m1.avg = ".300"
    m1.ops = ".950"
    m1.hr = 2
    m1.so = 1
    m1.h = 3
    m1.d = 1
    m1.t = 0
    m1.bb = 1
    m1.hbp = 0
    
    mock_bot.mlb_client.get_matchup.return_value = {
        'pitcher': "Gerrit Cole",
        'matchups': [m1]
    }

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.matchup.callback(cog, mock_interaction, team="nats", pitcher="Cole")

    mock_interaction.response.defer.assert_called_once()
    mock_bot.mlb_client.get_matchup.assert_called_once_with("nats", "Cole")

    args, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    embed = kwargs["embed"]
    assert embed.title == "⚔️ Matchup: NATS vs Gerrit Cole"
    assert "Fernando Tatis" in embed.description


from core.mlb_client import Game

@pytest.mark.asyncio
async def test_score_command_single(cog, mock_bot):
    mock_game = MagicMock(spec=Game)
    mock_game.abstract_state = "Live"
    mock_game.status = "In Progress"
    mock_game.away = MagicMock()
    mock_game.away.abbreviation = "WSH"
    mock_game.home = MagicMock()
    mock_game.home.abbreviation = "ATL"
    mock_game.format_score_line.return_value = "WSH 4 @ ATL 2 (Live)"
    mock_game.format_last_play.return_value = "Tatis Jr. struck out."

    mock_bot.mlb_client.get_todays_games.return_value = [mock_game]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.score.callback(cog, mock_interaction, team="nats", date=None, live=False, division=None)

    mock_interaction.response.defer.assert_called_once()
    mock_bot.mlb_client.get_todays_games.assert_called_once_with(team_query="nats", date=None)

    args, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    embed = kwargs["embed"]
    assert "WSH @ ATL" in embed.title
    assert "In Progress" in embed.title
    assert "WSH 4 @ ATL 2 (Live)" in embed.description
    assert "Tatis Jr. struck out." in embed.description


@pytest.mark.asyncio
async def test_score_command_all(cog, mock_bot):
    g1 = MagicMock(spec=Game)
    g1.abstract_state = "Final"
    g1.status = "Final"
    g1.inning = 9
    g1.away = MagicMock()
    g1.away.abbreviation = "WSH"
    g1.home = MagicMock()
    g1.home.abbreviation = "ATL"
    g1.format_score_line.return_value = "WSH 4 @ ATL 2 (Final)"
    g1.format_last_play.return_value = ""

    g2 = MagicMock(spec=Game)
    g2.abstract_state = "Live"
    g2.status = "In Progress"
    g2.away = MagicMock()
    g2.away.abbreviation = "NYY"
    g2.home = MagicMock()
    g2.home.abbreviation = "BOS"
    g2.format_score_line.return_value = "NYY 1 @ BOS 5 (Live)"
    g2.format_last_play.return_value = ""

    mock_bot.mlb_client.get_todays_games.return_value = [g1, g2]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.score.callback(cog, mock_interaction, team="all", date=None, live=False, division=None)

    mock_interaction.response.defer.assert_called_once()
    mock_bot.mlb_client.get_todays_games.assert_called_once_with(team_query=None, date=None)

    args, kwargs = mock_interaction.followup.send.call_args
    assert "embeds" in kwargs
    embeds = kwargs["embeds"]
    assert len(embeds) == 1
    assert embeds[0].title == "MLB Scores"
    
    fields = embeds[0].fields
    assert len(fields) == 2
    assert "WSH @ ATL" in fields[0].name
    assert "NYY @ BOS" in fields[1].name


@pytest.mark.asyncio
async def test_score_command_live_filter(cog, mock_bot):
    live_game = MagicMock(spec=Game)
    live_game.abstract_state = "Live"
    live_game.status = "In Progress"
    live_game.away = MagicMock()
    live_game.away.abbreviation = "NYY"
    live_game.home = MagicMock()
    live_game.home.abbreviation = "BOS"
    live_game.format_score_line.return_value = "NYY 1 @ BOS 3 (Live)"
    live_game.format_last_play.return_value = ""

    final_game = MagicMock(spec=Game)
    final_game.abstract_state = "Final"
    final_game.status = "Final"
    final_game.inning = 9
    final_game.away = MagicMock()
    final_game.away.abbreviation = "WSH"
    final_game.home = MagicMock()
    final_game.home.abbreviation = "ATL"
    final_game.format_score_line.return_value = "WSH 4 @ ATL 2 (Final)"
    final_game.format_last_play.return_value = ""

    mock_bot.mlb_client.get_todays_games.return_value = [live_game, final_game]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.score.callback(cog, mock_interaction, team="all", date=None, live=True, division=None)

    # One live game after filtering → single-game embed path
    args, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    assert "NYY @ BOS" in kwargs["embed"].title


@pytest.mark.asyncio
async def test_score_command_live_no_games(cog, mock_bot):
    final_game = MagicMock(spec=Game)
    final_game.abstract_state = "Final"

    mock_bot.mlb_client.get_todays_games.return_value = [final_game]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.score.callback(cog, mock_interaction, team="all", date=None, live=True, division=None)

    args, kwargs = mock_interaction.followup.send.call_args
    assert args[0] == "No live games right now."


# --- None / empty return paths ---

@pytest.mark.asyncio
async def test_line_command_no_stats(cog, mock_bot):
    mock_bot.mlb_client.get_player_game_stats.return_value = []

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.line.callback(cog, mock_interaction, player="nobody", date=None)

    args, kwargs = mock_interaction.followup.send.call_args
    assert "Could not find stats" in args[0]


@pytest.mark.asyncio
async def test_line_command_pitcher_only(cog, mock_bot):
    mock_stats = MagicMock(spec=PlayerGameStats)
    mock_stats.player_name = "Gerrit Cole"
    mock_stats.player_id = 543037
    mock_stats.date = "2026-05-23"
    mock_stats.team_abbrev = "NYY"
    mock_stats.opp_abbrev = "BOS"
    mock_stats.is_home = False
    mock_stats.headshot_url = None
    mock_stats.batting_stats = None
    mock_stats.pitching_stats = {"IP": "6.0", "H": 3, "R": 1, "ER": 1, "BB": 1, "SO": 8}
    mock_stats.info_message = None
    mock_stats.format_discord_code_block.return_value = "IP   H  R ER BB SO\n6.0  3  1  1  1  8"

    mock_bot.mlb_client.get_player_game_stats.return_value = [mock_stats]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.line.callback(cog, mock_interaction, player="543037", date="today")

    args, kwargs = mock_interaction.followup.send.call_args
    assert "embed" in kwargs
    # Pitcher-only: no PlayerAbsView button
    assert "view" not in kwargs
    embed = kwargs["embed"]
    assert "Gerrit Cole" in embed.title
    assert "IP   H  R ER" in embed.description


@pytest.mark.asyncio
async def test_percentiles_command_no_data(cog, mock_bot):
    mock_bot.mlb_client.get_player_percentiles.return_value = None

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.percentiles.callback(cog, mock_interaction, player="nobody", year=None)

    args, kwargs = mock_interaction.followup.send.call_args
    assert "No savant data found" in args[0]


@pytest.mark.asyncio
async def test_compare_percentiles_no_data_p1(cog, mock_bot):
    mock_bot.mlb_client.get_player_percentiles.side_effect = [None, MagicMock()]

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.compare_percentiles.callback(cog, mock_interaction, player1="nobody", player2="Judge", year=None)

    args, kwargs = mock_interaction.followup.send.call_args
    assert "No Savant data found" in args[0]
    assert "nobody" in args[0]


@pytest.mark.asyncio
async def test_bullpen_command_no_data(cog, mock_bot):
    mock_bot.mlb_client.get_bullpen.return_value = None

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.bullpen.callback(cog, mock_interaction, team="xyz")

    args, kwargs = mock_interaction.followup.send.call_args
    assert "Could not find bullpen data" in args[0]


@pytest.mark.asyncio
async def test_standings_command_no_data(cog, mock_bot):
    mock_bot.mlb_client.get_standings.return_value = []

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    choice = discord.app_commands.Choice(name="NL East", value="NL East")
    await cog.standings.callback(cog, mock_interaction, query=choice)

    args, kwargs = mock_interaction.followup.send.call_args
    assert "Could not find matching standings" in args[0]


@pytest.mark.asyncio
async def test_box_score_command_no_data(cog, mock_bot):
    mock_bot.mlb_client.get_box_score.return_value = None

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.box_score.callback(cog, mock_interaction, team="xyz")

    args, kwargs = mock_interaction.followup.send.call_args
    assert "Could not find a game" in args[0]


@pytest.mark.asyncio
async def test_matchup_command_no_data(cog, mock_bot):
    mock_bot.mlb_client.get_matchup.return_value = None

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = AsyncMock()
    mock_interaction.followup = AsyncMock()

    await cog.matchup.callback(cog, mock_interaction, team="nats", pitcher="Nobody")

    args, kwargs = mock_interaction.followup.send.call_args
    assert "No career matchup data found" in args[0]
