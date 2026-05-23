import pytest
from unittest.mock import AsyncMock, MagicMock
import discord
from discord import app_commands
from cogs.mlb import MLBSlash

@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.mlb_client = AsyncMock()
    bot.favorite_team_name = "nationals" # Lowercase name
    return bot

@pytest.fixture
def cog(mock_bot):
    return MLBSlash(bot=mock_bot)


@pytest.mark.asyncio
async def test_player_autocomplete_short_query(cog):
    # Query less than 2 characters should return empty list instantly without calling API
    interaction = MagicMock(spec=discord.Interaction)
    results = await cog.player_autocomplete(interaction, current="J")
    assert results == []
    cog.bot.mlb_client.search_players.assert_not_called()


@pytest.mark.asyncio
async def test_player_autocomplete_sorting_and_limits(cog, mock_bot):
    # Mock search_players to return a list of players, including one from Nationals (favorite)
    mock_bot.mlb_client.search_players.return_value = [
        {"id": 111, "name": "John Player", "name_display_club": "Padres", "mlb": 1},
        {"id": 222, "name": "Nats Hitter", "name_display_club": "Nationals", "mlb": 1},
        {"id": 333, "name": "Other Guy", "name_display_club": "Yankees", "mlb": 1},
    ]

    interaction = MagicMock(spec=discord.Interaction)
    results = await cog.player_autocomplete(interaction, current="John")

    mock_bot.mlb_client.search_players.assert_called_once_with("John")
    assert len(results) == 3

    # Assert that Nats Hitter is sorted FIRST because Nationals is the favorite team
    assert results[0].name == "Nats Hitter (Nationals)"
    assert results[0].value == "222"

    # Assert that John Player is second
    assert results[1].name == "John Player (Padres)"
    assert results[1].value == "111"


@pytest.mark.asyncio
async def test_player_autocomplete_cap_at_25(cog, mock_bot):
    # Return 30 players from search
    mock_bot.mlb_client.search_players.return_value = [
        {"id": i, "name": f"Player {i}", "name_display_club": "SD", "mlb": 1}
        for i in range(30)
    ]

    interaction = MagicMock(spec=discord.Interaction)
    results = await cog.player_autocomplete(interaction, current="Player")

    # Discord has a hard limit of 25 choices in autocomplete popups
    assert len(results) == 25


@pytest.mark.asyncio
async def test_team_autocomplete(cog):
    interaction = MagicMock(spec=discord.Interaction)

    # Search matches "Nationals" and "wsh"
    results = await cog.team_autocomplete(interaction, current="wsh")
    assert len(results) == 1
    assert results[0].name == "Washington Nationals"
    assert results[0].value == "wsh"

    # Search "ch" matches "Chicago Cubs" (chc), "Chicago White Sox" (cws)
    results_ch = await cog.team_autocomplete(interaction, current="ch")
    assert len(results_ch) >= 2
    match_names = [choice.name for choice in results_ch]
    assert "Chicago Cubs" in match_names
    assert "Chicago White Sox" in match_names
