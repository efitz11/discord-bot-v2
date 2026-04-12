import asyncio
from core.mlb_client import MLBClient

async def main():
    client = MLBClient()
    # Test Player Highlight (should automatically resolve as player)
    p_hls = await client.get_highlights("juan soto")
    print("Soto Highlights:", len(p_hls))
    for h in p_hls:
        print(h.title, h.url)
        
    print("\n----------------\n")
    
    # Test Team Highlight (should automatically resolve as team)
    t_hls = await client.get_highlights("wsh")
    print("WSH Highlights:", len(t_hls))
    for h in t_hls:
        print(h.title, h.url)
        
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
