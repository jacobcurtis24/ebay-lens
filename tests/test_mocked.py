"""End-to-end test through a real MCP client session against a mocked eBay API.

Run: python tests/test_mocked.py
"""

import asyncio
import base64
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
from PIL import Image, ImageDraw
from mcp.shared.memory import create_connected_server_and_client_session

from ebay_lens import server
from ebay_lens.ebay import EbayClient

calls: list[httpx.Request] = []


def jpeg(color, size=(1600, 1200), text=None):
    im = Image.new("RGB", size, color)
    if text:
        ImageDraw.Draw(im).text((size[0] * 0.7, size[1] * 0.1), text, fill="black")
    b = io.BytesIO()
    im.save(b, "JPEG")
    return b.getvalue()


def summary(i, color):
    return {
        "itemId": f"v1|10{i}|0", "title": f"Vintage Barbour Beaufort Jacket {i}",
        "price": {"value": f"{100 + i}.00", "currency": "USD"},
        "buyingOptions": ["AUCTION", "FIXED_PRICE"] if i == 1 else ["FIXED_PRICE", "BEST_OFFER"],
        "bidCount": 3, "currentBidPrice": {"value": "61.00", "currency": "USD"} if i == 1 else None,
        "itemEndDate": "2026-11-08T19:00:00.000Z", "condition": "Pre-owned",
        "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00" if i else "12.50", "currency": "USD"}}],
        "seller": {"username": "s", "feedbackPercentage": "99.5", "feedbackScore": 812},
        "itemLocation": {"postalCode": "061**", "country": "US"},
        "image": {"imageUrl": f"https://i.ebayimg.com/images/g/{color}/s-l225.jpg"},
        "itemWebUrl": f"https://www.ebay.com/itm/10{i}",
    }


def handler(req: httpx.Request) -> httpx.Response:
    calls.append(req)
    p = req.url.path
    if p.endswith("/oauth2/token"):
        assert req.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
    if req.url.host == "i.ebayimg.com":
        color = p.split("/")[3]
        return httpx.Response(200, content=jpeg(color, text="MODEL X-200 SN 4471"))
    assert req.headers["authorization"] == "Bearer T"
    assert req.headers["x-ebay-c-marketplace-id"] == "EBAY_US"
    if p.endswith("/item_summary/search"):
        res = {"total": 1234, "itemSummaries": [summary(i, c) for i, c in enumerate(["red", "green", "blue", "navy", "olive", "tan", "gray"])]}
        if "ASPECT_REFINEMENTS" in req.url.params.get("fieldgroups", ""):
            res["refinement"] = {"dominantCategoryId": "57988",
                                 "categoryDistributions": [{"categoryName": "Coats, Jackets & Vests", "categoryId": "57988", "matchCount": 900}],
                                 "aspectDistributions": [{"localizedAspectName": "Size", "aspectValueDistributions": [{"localizedAspectValue": "M", "matchCount": 200}, {"localizedAspectValue": "L", "matchCount": 180}]}]}
        return httpx.Response(200, json=res)
    if p.endswith("/search_by_image"):
        body = json.loads(req.content)
        Image.open(io.BytesIO(base64.b64decode(body["image"])))  # must be a valid image
        return httpx.Response(200, json={"total": 2, "itemSummaries": [summary(5, "olive"), summary(6, "tan")]})
    if p.endswith("/get_item_by_legacy_id"):
        return httpx.Response(400, json={"errors": [{"errorId": 11006, "message": "item group"}]})
    if p.endswith("/get_items_by_item_group"):
        base = {"title": "Barbour Bedale", "itemWebUrl": "u", "price": {"value": "90", "currency": "USD"},
                "image": {"imageUrl": "https://i.ebayimg.com/images/g/olive/s-l1600.jpg"}}
        return httpx.Response(200, json={"items": [
            {**base, "itemId": "v1|555|1", "localizedAspects": [{"name": "Size", "value": "38"}, {"name": "Brand", "value": "Barbour"}]},
            {**base, "itemId": "v1|555|2", "localizedAspects": [{"name": "Size", "value": "40"}, {"name": "Brand", "value": "Barbour"}],
             "additionalImages": [{"imageUrl": "https://i.ebayimg.com/images/g/tan/s-l1600.jpg"}]}]})
    if "/buy/browse/v1/item/" in p:
        return httpx.Response(200, json={
            **summary(2, "green"), "categoryId": "57988", "categoryPath": "Clothing|Men|Coats",
            "conditionDescription": "Small tear near left pocket, see photo 3.",
            "localizedAspects": [{"name": "Size", "value": "40"}, {"name": "Chest Size", "value": "44 in"}],
            "additionalImages": [{"imageUrl": f"https://i.ebayimg.com/images/g/{c}/s-l1600.jpg"} for c in ["red", "blue", "navy"]],
            "returnTerms": {"returnsAccepted": True, "returnPeriod": {"value": 30, "unit": "DAY"}, "returnShippingCostPayer": "BUYER"},
            "description": "<p>Great jacket.<br>Pit to pit: 23&quot;</p><script>x()</script>",
        })
    if p.endswith("/get_default_category_tree_id"):
        return httpx.Response(200, json={"categoryTreeId": "0"})
    if p.endswith("/get_category_suggestions"):
        return httpx.Response(200, json={"categorySuggestions": [{"category": {"categoryId": "57988", "categoryName": "Coats, Jackets & Vests"},
                                                                  "categoryTreeNodeAncestors": [{"categoryName": "Men"}, {"categoryName": "Clothing"}]}]})
    if p.endswith("/get_item_aspects_for_category"):
        return httpx.Response(200, json={"aspects": [{"localizedAspectName": "Size", "aspectConstraint": {"aspectRequired": True},
                                                      "aspectValues": [{"localizedValue": "S"}, {"localizedValue": "M"}]}]})
    return httpx.Response(404, json={"errors": [{"errorId": 0, "message": f"unmocked {p}"}]})


def kinds(res):
    return [c.type for c in res.content]


async def main():
    server._client = EbayClient("id", "secret", postal_code="06511", transport=httpx.MockTransport(handler))
    out = Path(__file__).parent / "out"
    out.mkdir(exist_ok=True)
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as s:
        tools = [t.name for t in (await s.list_tools()).tools]
        print("tools:", tools)

        r = await s.call_tool("search_ebay", {"query": "barbour beaufort", "category_id": "57988",
                                              "aspects": {"Size": "40|42"}, "condition": ["used"],
                                              "max_price": 200, "sort": "price_asc", "show_refinements": True})
        assert kinds(r) == ["text", "text", "image"], (kinds(r), r.content[0].text)
        print(r.content[0].text, "\n")
        q = [c for c in calls if c.url.path.endswith("/search")][-1].url.params
        print("filter =", q["filter"], "| aspect_filter =", q["aspect_filter"], "| sort =", q["sort"])
        assert "conditionIds:{3000|4000|5000|6000}" in q["filter"] and "buyingOptions:{FIXED_PRICE|AUCTION}" in q["filter"]
        assert q["aspect_filter"] == "categoryId:57988,Size:{40|42}"
        assert "zip%3D06511" in [c for c in calls if c.url.path.endswith("/search")][-1].headers["x-ebay-c-enduserctx"]
        (out / "contact_sheet.jpg").write_bytes(base64.b64decode(r.content[2].data))
        assert any("s-l300" in str(c.url) for c in calls), "thumbnails should be upsized to s-l300"

        r = await s.call_tool("search_ebay", {"query": "air compressor", "local_pickup_radius_miles": 50, "sort": "distance", "contact_sheet": False})
        assert kinds(r) == ["text"]
        assert "pickupRadius:50" in [c for c in calls if c.url.path.endswith("/search")][-1].url.params["filter"]

        r = await s.call_tool("get_ebay_item", {"item": "v1|102|0"})
        t = r.content[0].text
        print("\n" + t)
        assert "Pit to pit: 23\"" in t and "x()" not in t and "Chest Size: 44 in" in t and "Photos: 4" in t

        r = await s.call_tool("get_ebay_item_photos", {"item": "v1|102|0", "max_photos": 3, "max_px": 800})
        assert kinds(r).count("image") == 3 and "start=3" in r.content[-1].text, kinds(r)
        im = Image.open(io.BytesIO(base64.b64decode([c for c in r.content if c.type == "image"][0].data)))
        assert max(im.size) == 800, im.size

        r = await s.call_tool("zoom_ebay_photo", {"item": "v1|102|0", "photo_index": 1, "box": [0.6, 0.0, 1.0, 0.3]})
        assert kinds(r) == ["text", "image"], r.content[0].text
        print("\n" + r.content[0].text)
        (out / "zoom.jpg").write_bytes(base64.b64decode(r.content[1].data))

        p = out / "query.jpg"
        p.write_bytes(jpeg("olive", (640, 480)))
        r = await s.call_tool("search_ebay_by_image", {"image_path": str(p), "category_id": "57988"})
        assert kinds(r) == ["text", "text", "image"], r.content[0].text
        r = await s.call_tool("search_ebay_by_image", {"similar_to_item": "v1|102|0"})
        assert kinds(r)[0] == "text" and "image search" in r.content[0].text, r.content[0].text
        sbi = [c for c in calls if c.url.path.endswith("/search_by_image")][-1]
        assert sbi.url.params["category_ids"] == "57988", "similar_to_item should inherit category"
        r = await s.call_tool("search_ebay_by_image", {"image_path": str(p), "image_url": "x"})
        assert "exactly one" in r.content[0].text

        r = await s.call_tool("get_ebay_item", {"item": "https://www.ebay.com/itm/Barbour-Bedale/123456789012?hash=x"})
        t = r.content[0].text
        print("\n" + t)
        assert "Variations:" in t and "{'Size': '38'}" in t and "Brand" not in t.split("Variations:")[1]
        r = await s.call_tool("get_ebay_item_photos", {"item": "123456789012"})
        assert kinds(r).count("image") == 2  # photos merged across variations, de-duplicated

        r = await s.call_tool("suggest_ebay_categories", {"query": "waxed jacket"})
        print("\n" + r.content[0].text)
        assert r.content[0].text.startswith("57988: Clothing > Men > Coats")
        r = await s.call_tool("get_ebay_category_aspects", {"category_id": "57988"})
        assert "Size (required): S, M" in r.content[0].text

        r = await s.call_tool("search_ebay", {"query": "x", "aspects": {"Size": "M"}})
        assert "require exactly one category" in r.content[0].text
        r = await s.call_tool("get_ebay_item", {"item": "v1|nope"})  # unmocked -> error surfaced, not crash
        print("\nerror path:", r.content[0].text[:80])

    methods = {(c.method, c.url.path) for c in calls if c.url.host == "api.ebay.com"}
    assert all(m == "GET" or path.endswith(("/oauth2/token", "/search_by_image")) for m, path in methods), methods
    assert sum(c.url.path.endswith("/oauth2/token") for c in calls) == 1, "token should be cached"
    print("\nAll checks passed. Non-GET calls:", sorted(p for m, p in methods if m != "GET"))


asyncio.run(main())
