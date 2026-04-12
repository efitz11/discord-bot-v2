import asyncio
from core.mlb_client import MLBClient

async def main():
    client = MLBClient()
    # Test Player Highlight
    p_hls = await client.get_highlights("juan soto", is_team=False)
    print("Soto Highlights:", len(p_hls))
    for h in p_hls:
        print(h.title, h.url)
        
    print("\n----------------\n")
    
    # Test Team Highlight
    t_hls = await client.get_highlights("wsh", is_team=True)
    print("WSH Highlights:", len(t_hls))
    for h in t_hls:
        print(h.title, h.url)
        
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
