# MLB Discord Bot

A Discord bot for MLB and MiLB fans, built on the MLB Stats API and Baseball Savant (Statcast). All commands use Discord slash commands with autocomplete for player and team search.

## Commands

### /mlb

| Command | Description |
|---|---|
| `line` | Stat line for a player on a given date (or today) |
| `abs` | At-bats and video highlights for a player on a given date |
| `plot` | Pitch plot for a specific at-bat |
| `sp` | Scoring plays for a team's game |
| `pace` | Projected 162-game season totals for a player |
| `highlights` | Video highlights for a player or team |
| `standings` | Division, league, or wildcard standings |
| `matchup` | Career stats for a team's roster against a specific pitcher |
| `stats` | Season or career stats for a player |
| `last` | Stats over a player's last N games |
| `compare` | Side-by-side stat comparison for multiple players |
| `score` | Today's scores or a specific team's game |
| `box` | Box score for a team's game today |
| `bullpen` | Bullpen availability and recent pitch counts for a team |
| `leaders` | MLB player stat leaderboards |
| `team_leaders` | MLB team stat leaderboards |
| `homeruns` | All home runs hit across the league today |
| `next` | Upcoming schedule for a team |
| `past` | Recently completed games for a team |

### /milb

| Command | Description |
|---|---|
| `stats` | Season or career stats for a minor league player |
| `line` | Stat line for a minor league player on a given date |
| `abs` | At-bats and video highlights for a minor league player |

### /savant

| Command | Description |
|---|---|
| `percentiles` | Baseball Savant percentile rankings for a player |
| `compare_percentiles` | Side-by-side Savant percentile comparison for two players |
| `arsenal` | Pitch arsenal breakdown (usage, whiff rate, K%, run value) |
| `game` | Statcast exit velocity data for a team's game or a player's at-bats |
| `leaders` | Statcast leaderboards (exit velocity, barrel %, sprint speed, OAA, etc.) |
| `pitches` | Pitch counts by inning, recent pitches, and pitch mix for a pitcher |
| `zoneplot` | Hitting zone heatmap for a batter |

## Setup

### Requirements

Python 3.12+

### Install

```bash
git clone https://github.com/efitz11/discord-bot-v2.git
cd discord-bot-v2
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Environment

Create a `.env` file in the project root:

```
DISCORD_TOKEN=your_bot_token_here
```

### Run

```bash
python main.py
```

## Deployment

A `natsbot.service` systemd unit file is included for persistent deployment on Linux. A GitHub Actions workflow (`.github/workflows/deploy.yml`) automatically deploys to a self-hosted runner on pushes to `master`.
