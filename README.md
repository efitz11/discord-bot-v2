# MLB Discord Bot

A Discord bot for baseball (but also all sports) fans. Baseball data comes from the MLB Stats API and Baseball Savant (Statcast). NBA/WNBA/NHL/NFL/college football/World Cup data comes from the ESPN API. All commands use Discord slash commands with autocomplete.

## Baseball Commands

### /mlb

| Command | Description |
|---|---|
| `line` | Stat line for a player on a given date (or today) |
| `abs` | At-bats and video highlights for a player on a given date |
| `plot` | Pitch plot for a specific at-bat |
| `game plays` | Play-by-play for a team's batting side of an inning, with video links |
| `game scoringplays` | Scoring plays for a team's game |
| `game linescore` | Inning-by-inning linescore for a team's game |
| `game winprob` | Win probability chart for a team's game |
| `pace` | Projected 162-game season totals for a player |
| `log` | Game-by-game stat log for a player's last N games (optionally ending on a date) |
| `highlights` | Video highlights for a player or team |
| `standings` | Division, league, or wildcard standings |
| `matchup` | Career stats for a team's roster against a specific pitcher |
| `stats` | Season or career stats for a player |
| `last` | Aggregated stats over a player's last N games or N days (multiple spans, e.g. `5/10/20`) |
| `compare` | Side-by-side stat comparison for multiple players |
| `score` | Today's scores or a specific team's game (supports date, live-only, and division filters) |
| `splits` | Platoon/home-away/month splits for a batter or pitcher; use `split2` to compare two splits side-by-side |
| `box` | Box score for a team's game today |
| `bullpen` | Bullpen availability and recent pitch counts for a team |
| `leaders` | MLB player stat leaderboards |
| `team_leaders` | MLB team stat leaderboards |
| `homeruns` | All home runs hit across the league today |
| `next` | Upcoming schedule for a team |
| `past` | Recently completed games for a team |
| `transactions` | Transaction history for a player in a given season |

### /milb

| Command | Description |
|---|---|
| `stats` | Season or career stats for a minor league player |
| `line` | Stat line for a minor league player on a given date |
| `abs` | At-bats and video highlights for a minor league player |
| `log` | Game-by-game stat log for a minor league player's last N games |
| `last` | Aggregated stats over a minor league player's last N games or N days |
| `score` | Scores for a MiLB team, or all of an MLB org's affiliates (filterable by level) |
| `box` | Box score for a MiLB team's game (all, batting, pitching, or notes) |

### /savant

| Command | Description |
|---|---|
| `percentiles` | Baseball Savant percentile rankings for a player |
| `compare_percentiles` | Side-by-side Savant percentile comparison for two players |
| `compare_years` | One player's Savant percentiles compared across two seasons |
| `chart` | Rolling Statcast chart for a player (e.g. rolling xwOBA over 50/100/250 PAs) |
| `arsenal` | Pitch arsenal breakdown (usage, whiff rate, K%, run value) |
| `game` | Statcast exit velocity data for a team's game or a player's at-bats |
| `gamespray` | Spray chart of a game's batted balls for a team, or a player's batted balls in their game |
| `leaders` | Statcast leaderboards (exit velocity, barrel %, sprint speed, OAA, etc.) |
| `pitches` | Pitch counts by inning, recent pitches, and pitch mix for a pitcher |
| `pitchchart` | Every pitch a pitcher threw in a game, plotted on the strike zone by pitch type |
| `zoneplot` | Hitting zone heatmap for a batter |
| `spraychart` | Batted-ball spray chart for a batter (a season or career) |

## Sports Score Commands

Each sport is disabled by default. Enable individually in `.env` (no API keys required). NBA, WNBA, NHL, and World Cup commands take a `date` argument (e.g. `yesterday`, `-1`, `5/19`, `5/19/26`); NFL and college football take a `week` argument instead.

### /nba

| Command | Description |
|---|---|
| `score <team>` | Live linescore, status, and top performers for an NBA team's game. Use `ALL` for all games. |

Enable with `NBA=true` in `.env`.

### /wnba

| Command | Description |
|---|---|
| `score <team>` | Live linescore, status, and top performers for a WNBA team's game. Use `ALL` for all games. |

Enable with `WNBA=true` in `.env`.

### /nhl

| Command | Description |
|---|---|
| `score <team>` | Live linescore, status, and top performers for an NHL team's game. Use `ALL` for all games. |

Enable with `NHL=true` in `.env`.

### /nfl

| Command | Description |
|---|---|
| `score <team>` | Score for an NFL team's game. Use `All Teams` for every game. Optional `week` argument. |

Enable with `NFL=true` in `.env`.

### /cfb

| Command | Description |
|---|---|
| `score <team>` | Score for an FBS team's game, every game in a conference, or the top 10 games involving ranked teams (`Top 25 Games`). Optional `week` argument. |

Enable with `CFB=true` in `.env`. Set `CFB_UPSET_CHANNEL_ID` to also post upset alerts when a Top 25 team is trailing or tied with a lower-ranked or unranked opponent (at the start of the 4th quarter, at 2:00 left, and at the final), plus an "Upset Avoided" follow-up when an alerted game ends with the favorite winning.

### /worldcup

| Command | Description |
|---|---|
| `score` | World Cup matches for today or a specific date, or a single country's match |
| `standings` | Group standings (one group or all) |

Enable with `WORLDCUP=true` in `.env`.

## Image Commands

Always enabled.

| Command | Description |
|---|---|
| `/big <name>` | Post a saved image by name (autocompletes from the image folder) |
| `/bigadd <image> <name>` | Upload a new image for `/big`. Limited to `BIG_UPLOAD_USER_IDS` when that's set. |

## Extended Commands

Disabled by default. Enable by setting `EXTENDED_COMMANDS=true` in `.env` (no API keys required).

| Command | Description |
|---|---|
| `/weather` | Current conditions (plus air quality if `WAQI_TOKEN` is set) and today's forecast for a location |
| `/forecast` | 3-day weather forecast for a location |
| `/radar` | Weather radar map image for a location (location is optional; defaults to `RADAR_DEFAULT_LOCATION`) |
| `/stock` | Stock quote and chart for a ticker, or the Dow/S&P 500/Nasdaq if omitted (1H to 5Y ranges) |
| `/polymarket` | Search Polymarket prediction markets for current odds |

## Alert Preview Commands

Text commands (prefix `!`) that post a mock or real-data preview of a background alert to the current channel, for checking formatting.

| Command | Description |
|---|---|
| `!hr_test` | HR alert |
| `!multi_hr_test` | Multi-homer game alert |
| `!milb_hr_test` | MiLB HR alert |
| `!nh_test [away] [perfect]` | No-hitter / perfect game alerts |
| `!cycle_test <player> [date]` | Cycle alert, rendered from a real player's at-bats |
| `!delay_test` | Rain delay and resume alerts |
| `!lineup_test` | Favorite-team lineup post for today's game |
| `!transaction_test` | Favorite-team transaction alert (most recent transaction) |
| `!summary_test [YYYY-MM-DD]` | Morning performance summary |
| `!milb_summary_test [YYYY-MM-DD]` | MiLB affiliate summary |
| `!upset_test` | College football upset alert stages and "Upset Avoided" (requires `CFB=true`) |

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

# Optional: favorite team — floats this team's players to the top of autocomplete
FAVORITE_TEAM=WSH

# Optional: always post HR alerts for this team regardless of distance
# (can be the same as FAVORITE_TEAM or different; omit to disable)
HR_ALERT_TEAM=WSH

# Optional: Discord channel ID for live game alerts (no-hitters, HR alerts)
ALERT_CHANNEL_ID=123456789012345678

# Optional: enable /weather and /radar commands (no API key required)
EXTENDED_COMMANDS=true

# Optional: default location for /radar when none is specified
RADAR_DEFAULT_LOCATION=Nationals Park

# Optional: air quality on /weather (free token from aqicn.org)
WAQI_TOKEN=your_waqi_token

# Optional: enable sport score commands (ESPN API, no key required)
NBA=true
WNBA=true
NHL=true
NFL=true
CFB=true
WORLDCUP=true

# Optional (requires CFB=true): channel for college football upset alerts — a Top 25 team
# trailing or tied with a lower-ranked/unranked opponent at the start of the 4th and at 2:00
# left, an upset at the final, or an "Upset Avoided" follow-up to an earlier alert
CFB_UPSET_CHANNEL_ID=123456789012345678

# Optional: folder for /big images, and comma-separated Discord user IDs allowed to /bigadd
# (omit the IDs to let anyone upload)
BIG_IMAGES_DIR=/path/to/images
BIG_UPLOAD_USER_IDS=123456789012345678,234567890123456789

# Optional: directory for alert state files (defaults to the working directory)
STATE_DIR=/path/to/state
```

### Run

```bash
python main.py
```

## Deployment

A `natsbot.service` systemd unit file is included for persistent deployment on Linux. A GitHub Actions workflow (`.github/workflows/deploy.yml`) automatically deploys to a self-hosted runner on pushes to `main`.
