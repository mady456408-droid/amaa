import asyncio
import json
import httpx
from creators_api import init_creators_client, get_creators_client, DRAFT_PROFILE, _token_endpoint, _auth_header

async def main():
    await init_creators_client()
    client = get_creators_client()
    if not client:
        return
        
    token = await client._token_manager.get_token()
    body = {
        "itemIds": ["B07VTFN6HM"],
        "itemIdType": "ASIN",
        "partnerTag": client.partner_tag,
        "marketplace": client.marketplace,
        "resources": DRAFT_PROFILE,
        "offerCount": 10
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": _auth_header(token, client.version),
        "x-marketplace": client.marketplace,
    }
    url = "https://creatorsapi.amazon/catalog/v1/getItems"
    
    async with httpx.AsyncClient() as hc:
        resp = await hc.post(url, json=body, headers=headers)
        print(resp.status_code)
        print(json.dumps(resp.json(), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
