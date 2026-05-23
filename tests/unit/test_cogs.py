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
    assert kwargs["file"]._filename == "percentile_comparison.png"


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
