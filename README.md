# MLB Discord Bot

A powerful, high-performance Discord bot designed for MLB fans, featuring deep integration with Baseball Savant (Statcast) and the MLB Stats API. Built with a focus on rich visual data representation and modern Discord features like Slash Commands and Autocomplete.

## 🚀 Key Features

- **📊 Statcast Percentiles (`/mlb percentiles`)**: Beautifully formatted Baseball Savant percentile rankings for any player, complete with dynamic emoji status indicators (🔥 to 🧊).
- **🎥 Video Highlights (`/mlb highlights`)**: Instantly retrieve the latest video clips for any player or team. No raw URLs—just clean, clickable link embeds.
- **⚾ Pitch Arsenal (`/mlb arsenal`)**: Detailed pitcher breakdown including pitch usage, whiff rates, K%, and run value per 100 pitches.
- **📈 Statcast Leaderboards (`/mlb savant_leaders`)**: Live leaderboards for Exit Velocity, Barrel %, Sprint Speed, OAA, and more.
- **🏆 Live Standings (`/mlb standings`)**: Real-time divisional, league, and wildcard leaderboards with automatic "NL East" defaults for Nats fans.
- **🔢 Player Stats (`/mlb stats`)**: Full season and career stats for both hitters and pitchers, with support for year-by-year historical ranges.
- **🔍 Smart Autocomplete**: Predictive search for over 20,000 MLB players as you type.

## 🛠️ Technology Stack

- **Python 3.12+**: Utilizing the latest language features for performance and type safety.
- **discord.py**: A robust, modern wrapper for the Discord API.
- **MLB Stats API**: Native integration for scores, standings, and player metadata.
- **Baseball Savant (Statcast)**: Experimental scraping and CSV API integration for advanced analytics.
- **Aiohttp**: Fully asynchronous networking for high-concurrency performance.

## ⚙️ Installation & Setup

### 1. Requirements
Ensure you have Python 3.12 installed on your system.

### 2. Clone and Install
```bash
git clone https://github.com/your-repo/discord-bot-v2.git
cd discord-bot-v2
python -m venv venv
source venv/bin/activate  # Or venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### 3. Environment Variables
Create a `.env` file in the root directory:
```env
DISCORD_TOKEN=your_bot_token_here
```

### 4. Running the Bot
```bash
python main.py
```

## 🚢 Deployment (Self-Hosted)

The project includes a `natsbot.service` file for persistent deployment on Linux servers using `systemd`.

### Automated Deployment
This repo is configured with a GitHub Actions workflow (`.github/workflows/deploy.yml`) that automatically deploys to a self-hosted runner whenever code is pushed to the `main` branch.

## 🧪 Development

### Testing
Diagnostic and diagnostic scripts are located in the `/tests/` directory. You can run individual tests using:
```bash
PYTHONPATH=. python tests/test_highlights.py
```
