import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import urllib.parse
from datetime import datetime
from core.mlb_client import MLBClient, Team, Game, PlayerGameStats, PlayerPercentiles, SavantLeaderboard, PitchArsenal, BoxScoreData, BullpenData

def make_mock_response(json_data=None, text_data=None, read_data=None, status=200):
    resp = AsyncMock()
    resp.status = status
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    if text_data is not None:
        resp.text = AsyncMock(return_value=text_data)
    if read_data is not None:
        resp.read = AsyncMock(return_value=read_data)
    return MagicMock(__aenter__=AsyncMock(return_value=resp))


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.get = MagicMock()
    return session

@pytest.fixture
def client(mock_session):
    c = MLBClient()
    c.get_session = AsyncMock(return_value=mock_session)
    return c


@pytest.mark.asyncio
async def test_get_team_id(client, mock_session):
    # Set get to return the same mock response for all calls
    mock_session.get.return_value = make_mock_response(json_data={
        "teams": [
            {"id": 120, "name": "Washington Nationals", "teamName": "Nationals", "abbreviation": "WSH"},
            {"id": 147, "name": "New York Yankees", "teamName": "Yankees", "abbreviation": "NYY"},
        ]
    })

    # Test exact abbreviation matching
    assert await client.get_team_id("WSH") == 120
    assert await client.get_team_id("nyy") == 147
    # Test nickname matching
    assert await client.get_team_id("Nationals") == 120
    assert await client.get_team_id("yankees") == 147
    # Test alias mapping ("nats" -> "nationals")
    assert await client.get_team_id("nats") == 120
    # Test no match
    assert await client.get_team_id("unknown") is None


@pytest.mark.asyncio
async def test_resolve_player_by_id(client, mock_session):
    mock_session.get.return_value = make_mock_response(json_data={
        "people": [{"id": 665489, "fullName": "Fernando Tatis Jr."}]
    })

    res = await client.resolve_player("665489")
    assert res == {"id": "665489", "name": "Fernando Tatis Jr."}


@pytest.mark.asyncio
async def test_resolve_player_by_name_savant(client, mock_session):
    mock_session.get.return_value = make_mock_response(json_data=[
        {"id": 665489, "name": "Fernando Tatis Jr.", "name_display_club": "SD", "mlb": 1}
    ])

    res = await client.resolve_player("Fernando Tatis")
    assert res == {"id": "665489", "name": "Fernando Tatis Jr."}


@pytest.mark.asyncio
async def test_resolve_player_by_name_statsapi_fallback(client, mock_session):
    def get_side_effect(url, *args, **kwargs):
        if "player/search-all" in url:
            return make_mock_response(json_data=[])
        elif "people/search" in url:
            return make_mock_response(json_data={
                "people": [
                    {
                        "id": 665489, 
                        "fullName": "Fernando Tatis Jr.", 
                        "currentTeam": {"id": 135, "name": "San Diego Padres"}
                    }
                ]
            })
        return make_mock_response(json_data={})

    mock_session.get.side_effect = get_side_effect

    res = await client.resolve_player("Fernando Tatis")
    assert res == {"id": "665489", "name": "Fernando Tatis Jr."}


@pytest.mark.asyncio
async def test_get_team_schedule(client, mock_session):
    client.get_team_id = AsyncMock(return_value=120)

    mock_session.get.return_value = make_mock_response(json_data={
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 745000,
                        "gameDate": "2026-05-23T23:05:00Z",
                        "teams": {
                            "away": {"team": {"id": 120, "name": "Washington Nationals", "abbreviation": "WSH"}, "score": 4},
                            "home": {"team": {"id": 144, "name": "Atlanta Braves", "abbreviation": "ATL"}, "score": 2}
                        },
                        "status": {"abstractGameState": "Final", "detailedState": "Final"}
                    }
                ]
            }
        ]
    })

    games = await client.get_team_schedule("nats", num_games=1, past=True)
    
    assert len(games) == 1
    g = games[0]
    assert g.game_pk == 745000
    assert g.away.name == "Washington Nationals"
    assert g.away.score == 4
    assert g.home.name == "Atlanta Braves"
    assert g.home.score == 2
    assert g.status == "Final"


@pytest.mark.asyncio
async def test_get_player_game_stats_batting(client, mock_session):
    client.resolve_player = AsyncMock(return_value={"id": "665489", "name": "Fernando Tatis Jr."})
    client.get_team_abbrevs = AsyncMock(return_value={135: "SD"})

    def get_side_effect(url, *args, **kwargs):
        url_str = str(url)
        print(f"\n[GET URL]: {url_str}")
        if "people/665489" in url_str and "/people/665489/stats" not in url_str:
            print("[MOCK MATCH]: people info")
            return make_mock_response(json_data={
                "people": [{"id": 665489, "fullName": "Fernando Tatis Jr.", "currentTeam": {"id": 135}}]
            })
        elif "stats?stats=gameLog" in url_str and "group=hitting" in url_str:
            print("[MOCK MATCH]: gameLog hitting")
            return make_mock_response(json_data={
                "stats": [{"splits": [{"date": "2026-05-23", "game": {"gamePk": 745000}, "team": {"id": 135}}]}]
            })
        elif "stats?stats=gameLog" in url_str and "group=pitching" in url_str:
            print("[MOCK MATCH]: gameLog pitching")
            return make_mock_response(json_data={"stats": []})
        elif "game/745000/boxscore" in url_str:
            print("[MOCK MATCH]: boxscore")
            return make_mock_response(json_data={
                "teams": {
                    "away": {
                        "team": {"id": 120, "abbreviation": "WSH"}
                    },
                    "home": {
                        "team": {"id": 135, "abbreviation": "SD"},
                        "players": {
                            "ID665489": {
                                "stats": {
                                    "batting": {"plateAppearances": 4, "atBats": 3, "runs": 2, "hits": 2, "homeRuns": 1, "rbi": 3},
                                    "pitching": None
                                }
                            }
                        }
                    }
                }
            })
        print("[MOCK FALLBACK]: empty dict")
        return make_mock_response(json_data={})

    mock_session.get.side_effect = get_side_effect

    stats_list = await client.get_player_game_stats("Fernando Tatis", date="2026-05-23")
    
    assert len(stats_list) == 1
    stats = stats_list[0]
    assert stats.player_name == "Fernando Tatis Jr."
    assert stats.team_abbrev == "SD"
    assert stats.opp_abbrev == "WSH"
    assert stats.batting_stats is not None
    assert stats.batting_stats["hits"] == 2
    assert stats.batting_stats["homeRuns"] == 1
    assert stats.batting_stats["rbi"] == 3


@pytest.mark.asyncio
async def test_get_player_percentiles_batter(client, mock_session):
    client.resolve_player = AsyncMock(return_value={"id": "665489", "name": "Fernando Tatis Jr."})
    client.get_team_abbrevs = AsyncMock(return_value={135: "SD"})

    def get_side_effect(url, *args, **kwargs):
        if "people/665489" in url:
            return make_mock_response(json_data={
                "people": [{"id": 665489, "currentTeam": {"id": 135}}]
            })
        elif "savant-player/665489" in url:
            html = 'statcast: [{"year": 2026, "aggregate": "0", "grouping_cat": "Batter", "percent_rank_exit_velocity_avg": 95, "percent_rank_barrel_batted_rate": 88, "percent_rank_xwoba": 91, "exit_velocity_avg": 93.4, "barrel_batted_rate": 14.5, "xwoba": 0.385}],\n'
            return make_mock_response(text_data=html)
        return make_mock_response(json_data={})

    mock_session.get.side_effect = get_side_effect

    percentiles = await client.get_player_percentiles("Fernando Tatis", year="2026")
    
    assert percentiles is not None
    assert percentiles.player_name == "Fernando Tatis Jr."
    assert percentiles.team_abbrev == "SD"
    assert percentiles.stat_type == "Batter"
    assert percentiles.year == "2026"
    assert len(percentiles.percentiles) > 0
    
    ev_stat = next(p for p in percentiles.percentiles if p["stat"] == "exit_velocity_avg")
    assert ev_stat["value"] == 95
    assert ev_stat["raw"] == "93.4"


@pytest.mark.asyncio
async def test_get_savant_leaderboard(client, mock_session):
    csv_content = """"last_name, first_name",player_id,team_name_abbrev,exit_velocity_avg
"Soto, Juan",665742,NYY,95.6
"Judge, Aaron",592450,NYY,96.2
"""
    mock_session.get.return_value = make_mock_response(read_data=csv_content.encode("utf-8-sig"))

    lb = await client.get_savant_leaderboard("exit_velocity_avg", count=2, year="2026")
    
    assert lb is not None
    assert "Avg Exit Velocity Leaders" in lb.title
    assert len(lb.rows) == 2
    assert lb.rows[0]['name'] == "Judge, Aaron"
    assert lb.rows[0]['value'] == "96.2"
    assert lb.rows[1]['name'] == "Soto, Juan"
    assert lb.rows[1]['value'] == "95.6"


@pytest.mark.asyncio
async def test_get_pitch_arsenal(client, mock_session):
    client.resolve_player = AsyncMock(return_value={"id": "543037", "name": "Gerrit Cole"})

    def get_side_effect(url, *args, **kwargs):
        if "statcast-pitches-breakdown" in url:
            js_data = 'window.serverVals.pitchDetails = [{"api_pitch_type": "FF", "pitch_percent": 55.2, "whiff_percent": 24.5, "k_percent": 28.0, "release_speed": 96.7, "ba": 0.200, "xba": 0.190, "run_value": 5}, {"api_pitch_type": "CU", "pitch_percent": 18.5, "whiff_percent": 42.0, "k_percent": 38.0, "release_speed": 82.4, "ba": 0.150, "xba": 0.160, "run_value": 2}];'
            return make_mock_response(text_data=js_data)
        elif "people/543037" in url:
            return make_mock_response(json_data={
                "people": [{"id": 543037, "currentTeam": {"id": 147}}]
            })
        elif "teams/147" in url:
            return make_mock_response(json_data={
                "teams": [{"id": 147, "abbreviation": "NYY"}]
            })
        return make_mock_response(json_data={})

    mock_session.get.side_effect = get_side_effect

    arsenal = await client.get_pitch_arsenal("Gerrit Cole", year="2026")
    
    assert arsenal is not None
    assert arsenal.player_name == "Gerrit Cole"
    assert len(arsenal.pitches) == 2
    assert arsenal.pitches[0]["name"] == "Four-Seam Fastball"
    assert arsenal.pitches[0]["usage"] == 55.2
    assert arsenal.pitches[0]["avg_speed"] == 96.7

    block = arsenal.format_discord_code_block()
    assert "Four-Seam" in block
    assert "55%" in block
    assert "24%" in block


@pytest.mark.asyncio
async def test_get_box_score(client, mock_session):
    client.get_team_id = AsyncMock(return_value=120)
    client.get_team_abbrevs = AsyncMock(return_value={120: "WSH", 144: "ATL"})

    def get_side_effect(url, *args, **kwargs):
        if "schedule" in url:
            return make_mock_response(json_data={
                "dates": [{
                    "games": [{
                        "gamePk": 745000,
                        "status": {"abstractGameState": "Final", "detailedState": "Final"},
                        "teams": {
                            "away": {"team": {"id": 120, "name": "Washington Nationals", "abbreviation": "WSH"}},
                            "home": {"team": {"id": 144, "name": "Atlanta Braves", "abbreviation": "ATL"}}
                        }
                    }]
                }]
            })
        elif "game/745000/boxscore" in url:
            return make_mock_response(json_data={
                "teams": {
                    "away": {
                        "team": {"id": 120, "abbreviation": "WSH", "name": "Washington Nationals"},
                        "teamStats": {"batting": {"runs": 4, "hits": 8, "errors": 0}},
                        "players": {
                            "ID665489": {
                                "person": {"fullName": "Fernando Tatis Jr.", "boxscoreName": "Tatis Jr."},
                                "position": {"abbreviation": "RF"},
                                "stats": {"batting": {"atBats": 4, "runs": 1, "hits": 2, "rbi": 2, "baseOnBalls": 0, "strikeOuts": 1}},
                                "battingOrder": "100"
                            }
                        },
                        "battingOrder": ["ID665489"],
                        "batters": [665489]
                    },
                    "home": {
                        "team": {"id": 144, "abbreviation": "ATL", "name": "Atlanta Braves"},
                        "teamStats": {"batting": {"runs": 2, "hits": 5, "errors": 1}},
                        "players": {},
                        "battingOrder": [],
                        "batters": []
                    }
                }
            })
        return make_mock_response(json_data={})

    mock_session.get.side_effect = get_side_effect

    box = await client.get_box_score("nats")
    
    assert box is not None
    assert box.team_abbrev == "WSH"
    assert box.team_name == "Washington Nationals"
    assert len(box.batting_rows) == 1
    assert box.batting_rows[0]["name"] == "Tatis Jr."
    assert box.batting_rows[0]["pos"] == "RF"
    assert box.batting_rows[0]["h"] == "2"


def test_bullpen_data_formatting():
    dates = ["5/20", "5/21", "5/22", "5/23"]
    bullpen = [
        {"name": "Finnegan", "t": "R", "era": "2.10", "5/20": "15", "5/21": "", "5/22": "12", "5/23": ""}
    ]
    starters = [
        {"name": "Irvin", "t": "R", "era": "3.50", "5/20": "95", "5/21": "", "5/22": "", "5/23": ""}
    ]
    
    bd = BullpenData("Washington Nationals", "WSH", dates, bullpen, starters)
    
    assert bd._get_status(bullpen[0]) == "🟢"
    
    table = bd.format_table()
    assert "Finnegan" in table
    assert "Irvin" in table
