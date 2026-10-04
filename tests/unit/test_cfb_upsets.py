import pytest
from unittest.mock import MagicMock, AsyncMock

import cogs.cfb as cfb


def _comp(fav_score, dog_score, period, clock, name="STATUS_IN_PROGRESS", state="in",
          fav_rank=5, dog_rank=99):
    def competitor(home_away, abbr, rank, score):
        return {"homeAway": home_away, "score": str(score), "curatedRank": {"current": rank},
                "team": {"id": abbr, "abbreviation": abbr, "location": abbr}, "linescores": []}
    return {
        "competitors": [competitor("home", "FAV", fav_rank, fav_score),
                        competitor("away", "DOG", dog_rank, dog_score)],
        "status": {"period": period, "clock": clock, "displayClock": "0:00",
                   "type": {"name": name, "state": state}},
    }


@pytest.fixture
def cog(tmp_path, monkeypatch):
    monkeypatch.setattr(cfb, "UPSET_STATE_FILE", str(tmp_path / "cfb_upset_state.json"))
    c = cfb.CFBCog(MagicMock())
    c._build_upset_embed = MagicMock(return_value="embed")
    return c


def test_upset_sides():
    assert cfb._upset_sides(_comp(0, 0, 1, 900)) is not None
    assert cfb._upset_sides(_comp(0, 0, 1, 900, fav_rank=99)) is None            # two unranked teams
    fav, dog = cfb._upset_sides(_comp(0, 0, 1, 900, fav_rank=12, dog_rank=3))   # higher-ranked team is fav
    assert fav["team"]["abbreviation"] == "DOG" and dog["team"]["abbreviation"] == "FAV"


def test_upset_stage():
    assert cfb._upset_stage(_comp(0, 0, 3, 300)) is None
    assert cfb._upset_stage(_comp(0, 0, 3, 0, name="STATUS_END_PERIOD")) == cfb.STAGE_Q4
    assert cfb._upset_stage(_comp(0, 0, 4, 899)) == cfb.STAGE_Q4
    assert cfb._upset_stage(_comp(0, 0, 4, 120)) == cfb.STAGE_TWO_MIN
    assert cfb._upset_stage(_comp(0, 0, 5, 0)) is None
    assert cfb._upset_stage(_comp(0, 0, 4, 0, name="STATUS_FINAL", state="post")) == cfb.STAGE_FINAL


async def test_full_game_posts_each_stage_once(cog):
    channel = MagicMock(send=AsyncMock())
    for comp in (_comp(10, 7, 2, 300),
                 _comp(10, 14, 3, 0, name="STATUS_END_PERIOD"),
                 _comp(10, 14, 4, 880),     # still Q4 stage — no repeat
                 _comp(10, 14, 4, 110),
                 _comp(10, 14, 4, 50),      # still two-minute stage — no repeat
                 _comp(10, 14, 4, 0, name="STATUS_FINAL", state="post"),
                 _comp(10, 14, 4, 0, name="STATUS_FINAL", state="post")):
        await cog._check_upset("1", comp, channel, "20261003")
    assert channel.send.await_count == 3
    stages = [call.args[3] for call in cog._build_upset_embed.call_args_list]
    assert stages == [cfb.STAGE_Q4, cfb.STAGE_TWO_MIN, cfb.STAGE_FINAL]


async def test_favorite_leading_stages_are_skipped_but_final_upset_posts(cog):
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("1", _comp(21, 7, 4, 800), channel, "20261003")
    await cog._check_upset("1", _comp(21, 14, 4, 100), channel, "20261003")
    await cog._check_upset("1", _comp(21, 24, 4, 0, name="STATUS_FINAL", state="post"), channel, "20261003")
    stages = [call.args[3] for call in cog._build_upset_embed.call_args_list]
    assert stages == [cfb.STAGE_FINAL]


async def test_final_not_seen_live_is_ignored(cog):
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("1", _comp(10, 14, 4, 0, name="STATUS_FINAL", state="post"), channel, "20261003")
    channel.send.assert_not_awaited()


async def test_late_start_skips_missed_q4_alert(cog):
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("1", _comp(10, 14, 4, 90), channel, "20261003")
    stages = [call.args[3] for call in cog._build_upset_embed.call_args_list]
    assert stages == [cfb.STAGE_TWO_MIN]
    assert cog._upset_state["1"]["stages"] == [cfb.STAGE_Q4, cfb.STAGE_TWO_MIN]


async def test_state_survives_restart(cog):
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("1", _comp(10, 14, 4, 880), channel, "20261003")

    restarted = cfb.CFBCog(MagicMock())
    restarted._build_upset_embed = MagicMock(return_value="embed")
    await restarted._check_upset("1", _comp(10, 14, 4, 700), channel, "20261003")   # same stage — no repeat
    await restarted._check_upset("1", _comp(10, 14, 4, 100), channel, "20261003")
    assert channel.send.await_count == 2


async def test_failed_send_is_retried(cog):
    channel = MagicMock(send=AsyncMock(side_effect=[Exception("Missing Permissions"), None]))
    with pytest.raises(Exception):
        await cog._check_upset("1", _comp(10, 14, 4, 880), channel, "20261003")
    assert cfb.STAGE_Q4 not in cog._upset_state["1"]["stages"]
    await cog._check_upset("1", _comp(10, 14, 4, 820), channel, "20261003")
    assert cog._upset_state["1"]["stages"] == [cfb.STAGE_Q4]
    assert channel.send.await_count == 2


async def test_prune_keeps_live_games_on_empty_scoreboard(cog):
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("live", _comp(10, 14, 4, 880), channel, "20261003")
    await cog._check_upset("done", _comp(10, 7, 4, 880), channel, "20261003")
    await cog._check_upset("done", _comp(10, 7, 4, 0, name="STATUS_FINAL", state="post"), channel, "20261003")
    await cog._check_upset("old", _comp(10, 14, 2, 300), channel, "20261001")

    cog._prune_upset_state({}, expire_before="20261002")
    assert set(cog._upset_state) == {"live"}

    cog._prune_upset_state({}, expire_before="20261004")
    assert cog._upset_state == {}
