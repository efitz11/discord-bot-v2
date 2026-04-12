import asyncio
from core.mlb_client import MLBClient

async def main():
    client = MLBClient()
    
    # Test pitch arsenal (should auto-fallback to 2025)
    print("=== Corbin Burnes Arsenal (auto year) ===")
    arsenal = await client.get_pitch_arsenal("corbin burnes")
    if arsenal:
        print(f"Year: {arsenal.year}")
        print(arsenal.format_discord_code_block())
    else:
        print("No data")
    
    print()
    
    # Test savant leaderboard (should auto-fallback to 2025 if 2026 empty)
    print("=== Exit Velocity Leaders (auto year) ===")
    lb = await client.get_savant_leaderboard("exit_velocity_avg", count=5)
    if lb:
        print(f"Title: {lb.title}")
        print(lb.format_discord_code_block())
    else:
        print("No data")
    
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
