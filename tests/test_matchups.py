import asyncio
from core.mlb_client import MLBClient

async def main():
    client = MLBClient()
    
    # Test Juan Soto (NYY) vs Blake Snell (SF) - a classic matchup
    print("=== NYY Hitting vs Blake Snell ===")
    data = await client.get_matchup("nyy", "blake snell")
    if data:
        print(f"Pitcher: {data['pitcher']}")
        for m in data['matchups']:
            print(f"{m.batter_name.ljust(18)} | PA: {str(m.pa).rjust(2)} | AVG: {m.avg} | OPS: {m.ops} | HR: {m.hr}")
    else:
        print("No data found")
        
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
