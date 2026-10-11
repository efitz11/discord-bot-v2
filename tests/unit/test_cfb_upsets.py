import pytest
from datetime import datetime, timedelta
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


def _final(fav_score, dog_score):
    return _comp(fav_score, dog_score, 4, 0, name="STATUS_FINAL", state="post")


SETTLE = timedelta(seconds=cfb.UPSET_FINAL_SETTLE_SECONDS)


@pytest.fixture
def clock(monkeypatch):
    now = [datetime(2026, 10, 3, 15, 0)]
    monkeypatch.setattr(cfb, "et_now", lambda: now[0])
    return now


@pytest.fixture
def cog(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(cfb, "UPSET_STATE_FILE", str(tmp_path / "cfb_upset_state.json"))
    c = cfb.CFBCog(MagicMock())
    c._build_upset_embed = MagicMock(return_value="embed")
    return c


async def _settle_final(cog, clock, event_id, comp, channel):
    """Check a Final snapshot, then again once the settle window has passed."""
    await cog._check_upset(event_id, comp, channel, "20261003")
    clock[0] += SETTLE
    await cog._check_upset(event_id, comp, channel, "20261003")


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


async def test_full_game_posts_each_stage_once(cog, clock):
    channel = MagicMock(send=AsyncMock())
    for comp in (_comp(10, 7, 2, 300),
                 _comp(10, 14, 3, 0, name="STATUS_END_PERIOD"),
                 _comp(10, 14, 4, 880),     # still Q4 stage — no repeat
                 _comp(10, 14, 4, 110),
                 _comp(10, 14, 4, 50)):     # still two-minute stage — no repeat
        await cog._check_upset("1", comp, channel, "20261003")
    await _settle_final(cog, clock, "1", _final(10, 14), channel)
    clock[0] += SETTLE
    await cog._check_upset("1", _final(10, 14), channel, "20261003")   # final already posted
    assert channel.send.await_count == 3
    stages = [call.args[3] for call in cog._build_upset_embed.call_args_list]
    assert stages == [cfb.STAGE_Q4, cfb.STAGE_TWO_MIN, cfb.STAGE_FINAL]


async def test_favorite_leading_stages_are_skipped_but_final_upset_posts(cog, clock):
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("1", _comp(21, 7, 4, 800), channel, "20261003")
    await cog._check_upset("1", _comp(21, 14, 4, 100), channel, "20261003")
    await _settle_final(cog, clock, "1", _final(21, 24), channel)
    stages = [call.args[3] for call in cog._build_upset_embed.call_args_list]
    assert stages == [cfb.STAGE_FINAL]


async def test_upset_avoided_closes_out_earlier_alert(cog, clock):
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("1", _comp(10, 14, 4, 800), channel, "20261003")     # alert
    await cog._check_upset("1", _comp(17, 14, 4, 100), channel, "20261003")     # fav back ahead
    await _settle_final(cog, clock, "1", _final(17, 14), channel)
    calls = cog._build_upset_embed.call_args_list
    assert [(c.args[3], c.kwargs.get("avoided", False)) for c in calls] == \
        [(cfb.STAGE_Q4, False), (cfb.STAGE_FINAL, True)]
    clock[0] += SETTLE
    await cog._check_upset("1", _final(17, 14), channel, "20261003")            # no repeat
    assert channel.send.await_count == 2


async def test_tied_game_alerts_before_final(cog, clock):
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("1", _comp(14, 14, 3, 0, name="STATUS_END_PERIOD"), channel, "20261003")
    await cog._check_upset("1", _comp(21, 21, 4, 100), channel, "20261003")
    await _settle_final(cog, clock, "1", _final(24, 21), channel)
    calls = cog._build_upset_embed.call_args_list
    assert [(c.args[3], c.kwargs.get("avoided", False)) for c in calls] == \
        [(cfb.STAGE_Q4, False), (cfb.STAGE_TWO_MIN, False), (cfb.STAGE_FINAL, True)]


async def test_no_upset_avoided_without_earlier_alert(cog, clock):
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("1", _comp(17, 14, 4, 800), channel, "20261003")
    await _settle_final(cog, clock, "1", _final(17, 14), channel)
    channel.send.assert_not_awaited()


async def test_final_not_seen_live_is_ignored(cog, clock):
    channel = MagicMock(send=AsyncMock())
    await _settle_final(cog, clock, "1", _final(10, 14), channel)
    channel.send.assert_not_awaited()


async def test_final_waits_for_last_play_score(cog, clock):
    """Texas–OU 2026: ESPN went Final at 24–24 before crediting OU's FG at the gun."""
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("1", _comp(24, 24, 4, 3), channel, "20261003")  # tied — two-minute alert
    await cog._check_upset("1", _final(24, 24), channel, "20261003")       # stale score
    assert cfb.STAGE_FINAL not in cog._upset_state["1"]["stages"]
    clock[0] += timedelta(seconds=60)
    await cog._check_upset("1", _final(24, 27), channel, "20261003")       # still settling
    assert channel.send.await_count == 1
    clock[0] += SETTLE
    await cog._check_upset("1", _final(24, 27), channel, "20261003")
    calls = cog._build_upset_embed.call_args_list
    assert [(c.args[3], c.kwargs.get("avoided", False)) for c in calls] == \
        [(cfb.STAGE_TWO_MIN, False), (cfb.STAGE_FINAL, False)]


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


async def test_prune_keeps_live_games_on_empty_scoreboard(cog, clock):
    channel = MagicMock(send=AsyncMock())
    await cog._check_upset("live", _comp(10, 14, 4, 880), channel, "20261003")
    await cog._check_upset("done", _comp(10, 7, 4, 880), channel, "20261003")
    await _settle_final(cog, clock, "done", _final(10, 7), channel)
    await cog._check_upset("old", _comp(10, 14, 2, 300), channel, "20261001")

    cog._prune_upset_state({}, expire_before="20261002")
    assert set(cog._upset_state) == {"live"}

    cog._prune_upset_state({}, expire_before="20261004")
    assert cog._upset_state == {}


def _sched(state, kickoff_utc=""):
    return {"date": kickoff_utc, "status": {"type": {"state": state}}}


def test_next_fetch_live_game_polls_every_tick():
    from datetime import datetime
    now = datetime(2026, 10, 3, 21, 0)
    assert cfb._next_upset_fetch({"1": _sched("in"), "2": _sched("pre", "2026-10-04T03:00Z")},
                                 now, now - cfb.timedelta(hours=6)) is None


def test_next_fetch_waits_for_next_kickoff():
    from datetime import datetime
    now = datetime(2026, 10, 3, 9, 0)    # ET
    comps = {"1": _sched("post"), "2": _sched("pre", "2026-10-03T19:30Z"), "3": _sched("pre", "2026-10-03T16:00Z")}
    assert cfb._next_upset_fetch(comps, now, now - cfb.timedelta(hours=6)) == datetime(2026, 10, 3, 12, 0)


def test_next_fetch_delayed_kickoff_rechecks_soon():
    from datetime import datetime
    now = datetime(2026, 10, 3, 12, 30)
    comps = {"1": _sched("pre", "2026-10-03T16:00Z")}  # noon ET kickoff, still pregame
    assert cfb._next_upset_fetch(comps, now, now - cfb.timedelta(hours=6)) == datetime(2026, 10, 3, 12, 40)


def test_next_fetch_no_games_sleeps_until_rollover():
    from datetime import datetime
    for now in (datetime(2026, 10, 3, 23, 30), datetime(2026, 10, 4, 2, 0)):
        assert cfb._next_upset_fetch({}, now, now - cfb.timedelta(hours=6)) == datetime(2026, 10, 4, 6, 0)
