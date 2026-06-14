import os
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import discord
from datetime import datetime, timezone

# We'll import monitor and patch state files using a fixture
import cogs.monitor as monitor

@pytest.fixture
def temp_state_cog(tmp_path):
    # Override state file paths to point to a temporary test directory
    original_hr_file = monitor.HR_STATE_FILE
    original_nh_file = monitor.NH_STATE_FILE
    original_summary_file = monitor.SUMMARY_STATE_FILE
    
    monitor.HR_STATE_FILE = os.path.join(str(tmp_path), "hr_posted.json")
    monitor.NH_STATE_FILE = os.path.join(str(tmp_path), "nh_state.json")
    monitor.SUMMARY_STATE_FILE = os.path.join(str(tmp_path), "summary_state.json")
    
    # Mock bot dependency
    mock_bot = MagicMock()
    mock_bot.mlb_client = MagicMock()
    
    # Instantiate the cog with Loop.start mocked to prevent background thread spawning in tests
    with patch("discord.ext.tasks.Loop.start") as mock_start:
        cog = monitor.MonitorCog(bot=mock_bot)
    
    yield cog
    
    # Restore original paths
    monitor.HR_STATE_FILE = original_hr_file
    monitor.NH_STATE_FILE = original_nh_file
    monitor.SUMMARY_STATE_FILE = original_summary_file


def test_hr_state_persistence(temp_state_cog):
    cog = temp_state_cog
    
    # 1. Assert initial state is empty
    assert len(cog._hr_posted) == 0
    assert cog._hr_clear_date is None
    
    # 2. Add some home runs and a clear date
    cog._hr_posted.add("123456_5")
    cog._hr_posted.add("123456_8")
    cog._hr_clear_date = "2026-05-23"
    
    # 3. Save state to disk
    cog._save_hr_state()
    assert os.path.exists(monitor.HR_STATE_FILE)
    
    # 4. Create a new cog instance to verify it loads the persisted state
    mock_bot = MagicMock()
    with patch("discord.ext.tasks.Loop.start") as mock_start:
        new_cog = monitor.MonitorCog(bot=mock_bot)
    
    assert "123456_5" in new_cog._hr_posted
    assert "123456_8" in new_cog._hr_posted
    assert len(new_cog._hr_posted) == 2
    assert new_cog._hr_clear_date == "2026-05-23"


def test_nh_state_persistence(temp_state_cog):
    cog = temp_state_cog
    
    # 1. Assert initial state is empty
    assert len(cog._nh_alerted) == 0
    assert len(cog._nh_broken_posted) == 0
    
    # 2. Add an active no-hitter alert state
    game_pk = 123456
    cog._nh_alerted[game_pk] = {
        "key": (7, True), # (inning, is_top)
        "perfect": True,
        "pitching_abbr": "WSH",
        "tune_in_inning": 0,
    }
    cog._nh_broken_posted.add(game_pk)
    
    # 3. Save state to disk
    cog._save_nh_state()
    assert os.path.exists(monitor.NH_STATE_FILE)
    
    # 4. Load in a new cog instance
    mock_bot = MagicMock()
    with patch("discord.ext.tasks.Loop.start") as mock_start:
        new_cog = monitor.MonitorCog(bot=mock_bot)
    
    # 5. Verify the key was correctly loaded back as a tuple (JSON converts it to a list, we must parse back to a tuple)
    assert game_pk in new_cog._nh_alerted
    assert new_cog._nh_alerted[game_pk]["key"] == (7, True)
    assert new_cog._nh_alerted[game_pk]["perfect"] is True
    assert game_pk in new_cog._nh_broken_posted


@pytest.mark.asyncio
async def test_duplicate_nh_alert_prevention(temp_state_cog):
    cog = temp_state_cog
    game_pk = 777777
    channel = MagicMock()
    
    # Mock the delayed alert method so we don't trigger real network calls/tasks
    cog._delayed_nh_alert = AsyncMock()
    
    # 1. Setup mock gameData indicating a Live No-Hitter in progress
    mock_feed = {
        "gameData": {
            "status": {"abstractGameState": "Live"},
            "flags": {"noHitter": True, "perfectGame": False},
            "teams": {
                "away": {"abbreviation": "NYM"},
                "home": {"abbreviation": "WSH"}
            }
        },
        "liveData": {
            "linescore": {
                "currentInning": 8,
                "isTopInning": True, # Away team (NYM) is batting; WSH is pitching the top
                "inningState": "Top",
                "teams": {
                    "away": {"hits": 0}, # NYM has 0 hits -> WSH throwing NH
                    "home": {"hits": 5}
                }
            }
        }
    }

    # Mock self._fetch_live_feed and check_alert to run locally
    cog._fetch_live_feed = AsyncMock(return_value=mock_feed)
    cog._scheduled_games[game_pk] = {"away": "NYM", "home": "WSH", "abstract_state": "Live"}

    # Call _process_game for the first time.
    # WSH (home) throws the top, so their half is complete once inningState leaves "Top".
    # "Middle" marks the top of the 8th just ended -> WSH finished a clean half -> should_alert.
    mock_feed["liveData"]["linescore"]["inningState"] = "Middle"

    await cog._process_game(game_pk, channel)

    # It should have scheduled a delayed NH alert
    cog._delayed_nh_alert.assert_called_once()
    assert game_pk in cog._nh_alerted
    assert cog._nh_alerted[game_pk]["key"] == (8, "top")
    
    # Reset mock call history
    cog._delayed_nh_alert.reset_mock()
    
    # 2. Run _process_game AGAIN with the exact same game state
    await cog._process_game(game_pk, channel)
    
    # It should NOT alert again because the alert_key (8, False) hasn't changed
    cog._delayed_nh_alert.assert_not_called()


@pytest.mark.asyncio
async def test_perfect_game_broken_no_hitter_continues(temp_state_cog):
    cog = temp_state_cog
    game_pk = 555555
    channel = MagicMock()

    cog._delayed_pg_broken_alert = AsyncMock()
    cog._delayed_nh_alert = AsyncMock()
    cog._delayed_nh_tune_in_alert = AsyncMock()

    # Pre-existing state: WSH's perfect game has already been announced.
    cog._nh_alerted[game_pk] = {
        "key": (6, "top"),
        "perfect": True,
        "pitching_abbr": "WSH",
        "tune_in_inning": 0,
        "alert_posted": True,
        "pg_broken_posted": False,
    }

    # Now the perfect game is broken by an error, but the no-hitter is still alive.
    mock_feed = {
        "gameData": {
            "status": {"abstractGameState": "Live"},
            "flags": {"noHitter": True, "perfectGame": False},
            "teams": {"away": {"abbreviation": "NYM"}, "home": {"abbreviation": "WSH"}},
        },
        "liveData": {
            "linescore": {
                "currentInning": 7,
                "isTopInning": True,      # WSH (home) pitching the top
                "inningState": "Top",
                "outs": 1,
                "teams": {"away": {"hits": 0, "runs": 0}, "home": {"hits": 4, "runs": 2}},
            },
            "plays": {
                "allPlays": [
                    {
                        "about": {"inning": 7, "halfInning": "top", "outs": 1},
                        "result": {"eventType": "field_error",
                                   "description": "Francisco Lindor reaches on a throwing error."},
                        "matchup": {"pitcher": {"fullName": "MacKenzie Gore"},
                                    "batter": {"fullName": "Francisco Lindor"}},
                    }
                ],
            },
        },
    }
    cog._fetch_live_feed = AsyncMock(return_value=mock_feed)
    cog._scheduled_games[game_pk] = {"away": "NYM", "home": "WSH", "abstract_state": "Live"}

    await cog._process_game(game_pk, channel)

    # Perfect-game-over alert fires once; the no-hitter is still intact (no break-up alert)
    cog._delayed_pg_broken_alert.assert_called_once()
    assert cog._nh_alerted[game_pk]["pg_broken_posted"] is True
    assert cog._nh_alerted[game_pk]["perfect"] is False

    # Repeat poll with the same state must not re-fire
    cog._delayed_pg_broken_alert.reset_mock()
    await cog._process_game(game_pk, channel)
    cog._delayed_pg_broken_alert.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_hr_alert_prevention_logic():
    # Verify that a home run key skips processing if it's already in _hr_posted
    mock_bot = MagicMock()
    mock_bot.wait_until_ready = AsyncMock()
    
    with patch("discord.ext.tasks.Loop.start") as mock_start:
        cog = monitor.MonitorCog(bot=mock_bot)
        
    cog._hr_posted = {"999999_3"} # Game 999999, at-bat index 3 is already posted
    
    # We mock _fetch_live_feed to return a game containing that exact home run play
    mock_feed = {
        "gameData": {
            "status": {"abstractGameState": "Live"},
        },
        "liveData": {
            "linescore": {
                "teams": {
                    "away": {"runs": 2},
                    "home": {"runs": 1}
                }
            },
            "plays": {
                "allPlays": [
                    {
                        "about": {"atBatIndex": 3, "endTime": "2026-05-23T18:00:00.000Z"},
                        "result": {"eventType": "home_run", "description": "Juan Soto homers!"},
                        "matchup": {"batter": {"fullName": "Juan Soto"}},
                        "playEvents": []
                    }
                ]
            }
        }
    }
    
    cog._fetch_live_feed = AsyncMock(return_value=mock_feed)
    cog._scheduled_games[999999] = {"away": "NYY", "home": "BOS", "abstract_state": "Live"}
    cog._fetch_content = AsyncMock() # Should not be called
    
    channel = MagicMock()
    await cog._process_game(999999, channel)
    
    # Since 999999_3 is already in _hr_posted, it should immediately skip and not fetch content highlights
    cog._fetch_content.assert_not_called()
