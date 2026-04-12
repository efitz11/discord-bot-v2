import aiohttp
import asyncio
import re
import json

async def main():
    headers = {"User-Agent": "Mozilla/5.0"}
    url = "https://baseballsavant.mlb.com/savant-player/669221"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as res:
            text = await res.text()
            match = re.search(r"var\s+serverVals\s*=\s*({.*?});\n", text, re.DOTALL)
            if match:
                js_obj = match.group(1)
                # print("Len:", len(js_obj))
                js_json = re.sub(r'([{,]\s*)([a-zA-Z0-9_]+)(\s*:)', r'\1"\2"\3', js_obj)
                js_json = js_json.replace("'", '"') # very rough quotes fix
                # let's just try to parse it with ast.literal_eval if valid python dict-like, or dirty json
                try:
                    data = json.loads(js_json)
                    print("JSON Parsed!")
                except Exception as e:
                    print("Error:", e, js_json[:200])
                    
if __name__ == "__main__":
    asyncio.run(main())
