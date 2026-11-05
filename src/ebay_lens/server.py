"""ebay-lens MCP server: eBay search that Claude can see.

Run:  EBAY_CLIENT_ID=... EBAY_CLIENT_SECRET=... ebay-lens
"""

from __future__ import annotations

import asyncio
import base64
import html
import re
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from . import images as img
from .ebay import SORTS, EbayClient, EbayError

INSTRUCTIONS = """\
Read-only eBay search via the official Browse API. Never tries to buy, bid, or message sellers;
give the user listing links so they decide and act themselves.

Suggested workflow:
1. search_ebay with a focused query. It returns numbered results plus a CONTACT SHEET image of
   thumbnails with matching numbers - look at it to screen visually (style, color, wear, model,
   obvious mismatches) before spending calls on details.
2. To narrow: show_refinements=True reveals available categories and aspect values (Size, Brand,
   Color...). suggest_ebay_categories / get_ebay_category_aspects give category ids and valid
   aspect names; aspects filters need a category id.
3. get_ebay_item for specifics, condition notes, description, shipping, returns.
4. get_ebay_item_photos to inspect every photo; zoom_ebay_photo to read tags, labels, nameplates,
   serial plates, gauges, or inspect flaws at full resolution.
5. search_ebay_by_image to find visually similar listings from a photo file, an image URL, or an
   existing listing (similar_to_item).

Used clothing: verify tagged size against measured dimensions (pit-to-pit, length, inseam, rise)
in specifics/description/photos; zoom on brand/size/care tags to confirm authenticity, fabric and
era; inspect photos for stains, pilling, holes, fading; tagged size often runs differently by era.
Industrial equipment: read the nameplate (model, serial, voltage, phase, Hz, amps, pressure
rating); check for missing parts, hours/cycle counters, damage, 'untested'/'as-is' language;
weigh freight shipping cost vs local pickup (local_pickup_radius_miles, sort='distance').
"""

mcp = FastMCP("ebay-lens", instructions=INSTRUCTIONS)
_client: EbayClient | None = None
_sem = asyncio.Semaphore(8)

Condition = Literal["new", "new_other", "refurbished", "used", "for_parts"]
Buying = Literal["fixed_price", "auction", "best_offer"]
Sort = Literal["best_match", "price_asc", "price_desc", "newly_listed", "ending_soonest", "distance"]

Content = list[TextContent | ImageContent]


def client() -> EbayClient:
    global _client
    if _client is None:
        _client = EbayClient.from_env()
    return _client


def _text(s: str) -> TextContent:
    return TextContent(type="text", text=s)


def _image(data: bytes) -> ImageContent:
    return ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType="image/jpeg")


def _money(m: dict | None) -> str:
    if not m:
        return "?"
    cur = m.get("currency", "")
    sym = "$" if cur == "USD" else f"{cur} "
    return f"{sym}{float(m.get('value', 0)):,.2f}"


def _shipping(opts: list | None) -> str:
    if not opts:
        return "ship: see listing"
    o = opts[0]
    if o.get("shippingCostType") == "CALCULATED" and not o.get("shippingCost"):
        return "ship: calculated"
    cost = o.get("shippingCost")
    if cost and float(cost.get("value", 0)) == 0:
        return "free ship"
    return f"+{_money(cost)} ship" if cost else "ship: see listing"


def _summary_line(n: int, it: dict) -> str:
    opts = it.get("buyingOptions") or []
    price = _money(it.get("currentBidPrice") or it.get("price"))
    bits = [f"[{n}] {it.get('title', '').strip()}", f"{price} {_shipping(it.get('shippingOptions'))}"]
    if it.get("condition"):
        bits.append(it["condition"])
    if "AUCTION" in opts:
        a = f"auction, {it.get('bidCount', 0)} bids"
        if it.get("itemEndDate"):
            a += f", ends {it['itemEndDate'][:16].replace('T', ' ')}Z"
        bits.append(a + (" (also BIN)" if "FIXED_PRICE" in opts else ""))
    if "BEST_OFFER" in opts:
        bits.append("best offer")
    s = it.get("seller") or {}
    if s:
        bits.append(f"seller {s.get('feedbackPercentage', '?')}% ({s.get('feedbackScore', '?')})")
    loc = it.get("itemLocation") or {}
    where = ", ".join(x for x in (loc.get("city"), loc.get("stateOrProvince"), loc.get("postalCode"),
                                  loc.get("country")) if x)
    if where:
        bits.append(f"loc {where}")
    d = it.get("distanceFromPickupLocation")
    if d:
        bits.append(f"{d.get('value')} {d.get('unitOfMeasure', 'mi')} away")
    bits.append(f"id {it.get('itemId')}")
    return " | ".join(bits) + f"\n    {it.get('itemWebUrl', '')}"


def _refinements(res: dict, max_vals: int = 12) -> str:
    ref = res.get("refinement") or {}
    out = []
    cats = ref.get("categoryDistributions") or []
    if cats:
        out.append("Categories: " + "; ".join(f"{c['categoryName']} (id {c['categoryId']}, {c['matchCount']})"
                                             for c in cats[:max_vals]))
    conds = ref.get("conditionDistributions") or []
    if conds:
        out.append("Conditions: " + "; ".join(f"{c['condition']} ({c['matchCount']})" for c in conds))
    for a in (ref.get("aspectDistributions") or [])[:15]:
        vals = a.get("aspectValueDistributions") or []
        out.append(f"{a.get('localizedAspectName')}: " +
                   "; ".join(f"{v['localizedAspectValue']} ({v['matchCount']})" for v in vals[:max_vals]))
    if ref.get("dominantCategoryId"):
        out.append(f"Dominant category id: {ref['dominantCategoryId']}")
    return "\n".join(out)


async def _fetch(url: str | None) -> bytes | None:
    if not url:
        return None
    async with _sem:
        try:
            return await client().fetch_bytes(url)
        except Exception:
            return None


async def _results_content(res: dict, offset: int, make_sheet: bool, header: str) -> Content:
    items = res.get("itemSummaries") or []
    total = res.get("total", len(items))
    lines = [f"{header} - showing {offset + 1}-{offset + len(items)} of {total:,}"]
    lines += [_summary_line(offset + i + 1, it) for i, it in enumerate(items)]
    if not items:
        lines.append("No results. Try broader keywords, fewer filters, or a different category.")
    refs = _refinements(res)
    if refs:
        lines.append("\nRefinements:\n" + refs)
    out: Content = [_text("\n".join(lines))]
    if make_sheet and items:
        urls = [img.at_size((it.get("image") or (it.get("thumbnailImages") or [{}])[0]).get("imageUrl", ""), 300)
                or None for it in items]
        thumbs = await asyncio.gather(*(_fetch(u) for u in urls))
        sheet = img.contact_sheet([(offset + i + 1, t) for i, t in enumerate(thumbs)])
        out.append(_text("Contact sheet (numbers match the result list):"))
        out.append(_image(sheet))
    return out


def _err(e: Exception) -> Content:
    return [_text(f"Error: {e}")]


# ====================================================================== tools
@mcp.tool()
async def search_ebay(
    query: str | None = None,
    category_id: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    condition: list[Condition] | None = None,
    buying_options: list[Buying] | None = None,
    aspects: dict[str, str] | None = None,
    item_location_country: str | None = None,
    local_pickup_radius_miles: int | None = None,
    free_shipping: bool = False,
    sort: Sort = "best_match",
    limit: int = 20,
    offset: int = 0,
    show_refinements: bool = False,
    contact_sheet: bool = True,
) -> Content:
    """Keyword search of active eBay listings, returning numbered results and a thumbnail contact sheet.

    aspects: item-specific filters such as {"Size": "M", "Brand": "Patagonia|Arc'teryx"} ('|' = OR);
      requires category_id. Use show_refinements=True to discover valid aspect names/values.
    buying_options: defaults to fixed_price + auction (eBay alone would return only Buy It Now).
    local_pickup_radius_miles: only items offering local pickup within this radius of the server's
      EBAY_POSTAL_CODE (useful for heavy equipment).
    limit: 1-50 recommended (contact sheet gets unwieldy above ~30).
    """
    try:
        c = client()
        flt = c.build_filter(min_price, max_price, condition, buying_options or ["fixed_price", "auction"],
                             item_location_country, local_pickup_radius_miles, free_shipping)
        res = await c.search(q=query, category_ids=category_id, filter=flt,
                             aspect_filter=c.build_aspect_filter(category_id, aspects),
                             sort=SORTS[sort], limit=min(limit, 50), offset=offset,
                             refinements=show_refinements)
        return await _results_content(res, offset, contact_sheet, f"eBay search '{query or ''}'")
    except Exception as e:
        return _err(e)


@mcp.tool()
async def search_ebay_by_image(
    image_path: str | None = None,
    image_url: str | None = None,
    similar_to_item: str | None = None,
    category_id: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    condition: list[Condition] | None = None,
    buying_options: list[Buying] | None = None,
    aspects: dict[str, str] | None = None,
    sort: Sort = "best_match",
    limit: int = 20,
    offset: int = 0,
    contact_sheet: bool = True,
) -> Content:
    """Visual-similarity search (eBay's experimental searchByImage). Give exactly one source:
    image_path (local file), image_url, or similar_to_item (item id/URL; uses its main photo).
    Adding category_id greatly improves relevance. Results come with a contact sheet."""
    try:
        c = client()
        sources = [s for s in (image_path, image_url, similar_to_item) if s]
        if len(sources) != 1:
            return _err(EbayError("Provide exactly one of image_path, image_url, similar_to_item."))
        if image_path:
            raw = Path(image_path).expanduser().read_bytes()
        elif image_url:
            raw = await c.fetch_bytes(image_url)
        else:
            item = await c.get_item(similar_to_item)
            url = (item.get("image") or {}).get("imageUrl")
            if not url:
                return _err(EbayError("That listing has no photo."))
            raw = await c.fetch_bytes(img.at_size(url, 800))
            if not category_id:
                category_id = item.get("categoryId")
        b64 = base64.b64encode(img.to_jpeg(img.open_image(raw), 800)).decode()
        flt = c.build_filter(min_price, max_price, condition, buying_options or ["fixed_price", "auction"])
        res = await c.search_by_image(b64, category_ids=category_id, filter=flt,
                                      aspect_filter=c.build_aspect_filter(category_id, aspects),
                                      sort=SORTS[sort], limit=min(limit, 50), offset=offset)
        return await _results_content(res, offset, contact_sheet, "eBay image search")
    except Exception as e:
        return _err(e)


def _strip_html(s: str, limit: int) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s).strip()
    return s if len(s) <= limit else s[:limit] + " ...[truncated]"


@mcp.tool()
async def get_ebay_item(item: str, description_chars: int = 4000) -> Content:
    """Full listing details: price, condition notes, item specifics (size, measurements, brand,
    model, MPN...), seller description, shipping, returns, location, variations, photo count.
    item: RESTful id (v1|...|0), numeric legacy id, or an ebay.com/itm/ URL."""
    try:
        it = await client().get_item(item)
    except Exception as e:
        return _err(e)
    L = [f"{it.get('title')}", f"URL: {it.get('itemWebUrl')}", f"Item id: {it.get('itemId')}" + (f"  (legacy {it['legacyItemId']})" if it.get("legacyItemId") else "")]
    L.append(f"Price: {_money(it.get('price'))}" +
             (f"  | current bid {_money(it['currentBidPrice'])}, {it.get('bidCount', 0)} bids" if it.get("currentBidPrice") else "") +
             (f"  | options: {', '.join(it['buyingOptions'])}" if it.get("buyingOptions") else ""))
    if it.get("itemEndDate"):
        L.append(f"Ends: {it['itemEndDate']}")
    if it.get("condition"):
        L.append(f"Condition: {it['condition']}" + (f" - {it['conditionDescription']}" if it.get("conditionDescription") else ""))
    for cd in it.get("conditionDescriptors") or []:
        vals = ", ".join(v.get("content", "") for v in cd.get("values", []))
        L.append(f"  {cd.get('name')}: {vals}")
    if it.get("categoryPath"):
        L.append(f"Category: {it['categoryPath']} (id {it.get('categoryId')})")
    for k in ("brand", "mpn", "color", "size", "material", "gtin"):
        if it.get(k):
            L.append(f"{k.upper()}: {it[k]}")
    asp = it.get("localizedAspects") or []
    if asp:
        L.append("Item specifics:\n" + "\n".join(f"  {a.get('name')}: {a.get('value')}" for a in asp))
    if it.get("_variations"):
        L.append("Variations:")
        for v in it["_variations"]:
            L.append(f"  {v['aspects']} {_money(v.get('price'))} [{v.get('availability')}] id {v['itemId']}")
    s = it.get("seller") or {}
    if s:
        L.append(f"Seller: {s.get('username', '?')} {s.get('feedbackPercentage', '?')}% ({s.get('feedbackScore', '?')})")
    loc = it.get("itemLocation") or {}
    where = ", ".join(x for x in (loc.get("city"), loc.get("stateOrProvince"), loc.get("postalCode"), loc.get("country")) if x)
    if where:
        L.append(f"Location: {where}")
    for o in (it.get("shippingOptions") or [])[:4]:
        svc = o.get("shippingServiceCode") or o.get("type") or o.get("shippingCarrierCode") or "option"
        L.append(f"Shipping ({svc}): {_money(o.get('shippingCost')) if o.get('shippingCost') else (o.get('shippingCostType') or 'see listing').lower()}"
                 + (f", arrives {o.get('minEstimatedDeliveryDate', '')[:10]}-{o.get('maxEstimatedDeliveryDate', '')[:10]}" if o.get("maxEstimatedDeliveryDate") else ""))
    if any(p.get("pickupLocation") or p.get("type") == "LOCAL_PICKUP" for p in it.get("pickupOptions") or []) \
            or "LOCAL_PICKUP" in str(it.get("shippingOptions")):
        L.append("Local pickup: offered")
    rt = it.get("returnTerms") or {}
    if rt:
        L.append(f"Returns: {'accepted' if rt.get('returnsAccepted') else 'not accepted'}"
                 + (f", {rt['returnPeriod'].get('value')} {rt['returnPeriod'].get('unit', '').lower()}" if rt.get("returnPeriod") else "")
                 + (f", return shipping paid by {rt.get('returnShippingCostPayer', '').lower()}" if rt.get("returnShippingCostPayer") else ""))
    n_photos = (1 if it.get("image") else 0) + len(it.get("additionalImages") or [])
    L.append(f"Photos: {n_photos} (use get_ebay_item_photos)")
    if it.get("shortDescription"):
        L.append(f"Short description: {it['shortDescription']}")
    if it.get("description"):
        L.append("Seller description:\n" + _strip_html(it["description"], description_chars))
    return [_text("\n".join(L))]


def _photo_urls(it: dict) -> list[str]:
    return [p["imageUrl"] for p in [it.get("image")] + (it.get("additionalImages") or []) if p and p.get("imageUrl")]


@mcp.tool()
async def get_ebay_item_photos(item: str, start: int = 0, max_photos: int = 8, max_px: int = 1024) -> Content:
    """Download a listing's photos and return them as images for visual inspection.
    Photos are numbered from 0 (0 = main photo). Page with start; max_px 512-1600."""
    try:
        c = client()
        it = await c.get_item(item)
        urls = _photo_urls(it)
        if not urls:
            return [_text("This listing has no photos.")]
        max_px = max(256, min(int(max_px), 1600))
        sel = urls[start:start + max(1, min(max_photos, 12))]
        datas = await asyncio.gather(*(_fetch(img.at_size(u, 1600)) for u in sel))
        out: Content = [_text(f"{it.get('title')} - photos {start}-{start + len(sel) - 1} of {len(urls)} total")]
        for i, d in enumerate(datas):
            out.append(_text(f"Photo {start + i}:"))
            if d is None:
                out.append(_text("(download failed)"))
                continue
            try:
                out.append(_image(img.to_jpeg(img.open_image(d), max_px)))
            except Exception:
                out.append(_text("(could not decode image)"))
        if start + len(sel) < len(urls):
            out.append(_text(f"More photos available: call again with start={start + len(sel)}"))
        return out
    except Exception as e:
        return _err(e)


@mcp.tool()
async def zoom_ebay_photo(item: str, photo_index: int, box: list[float], max_px: int = 1400) -> Content:
    """Crop one listing photo at full resolution (eBay's largest, 1600px) to read tags, labels,
    nameplates, serial plates, or inspect a flaw. box = [x0, y0, x1, y1] as fractions of the photo
    (e.g. [0.5, 0.0, 1.0, 0.5] = top-right quarter)."""
    try:
        c = client()
        urls = _photo_urls(await c.get_item(item))
        if not 0 <= photo_index < len(urls):
            return _err(EbayError(f"photo_index must be 0-{len(urls) - 1}"))
        data = await c.fetch_bytes(img.at_size(urls[photo_index], 1600))
        full = img.open_image(data)
        crop = img.crop_fraction(full, tuple(box))  # type: ignore[arg-type]
        note = f"Photo {photo_index} crop {box} -> {crop.width}x{crop.height}px of {full.width}x{full.height}"
        if crop.width < 300:
            note += " (low source resolution; detail may be limited)"
        # upscale small crops a little so text is legible to the model
        if max(crop.size) < 768:
            scale = 768 / max(crop.size)
            crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), img.PILImage.LANCZOS)
        return [_text(note), _image(img.to_jpeg(crop, max_px, quality=90))]
    except Exception as e:
        return _err(e)


@mcp.tool()
async def suggest_ebay_categories(query: str) -> Content:
    """Suggest eBay category ids for a description (e.g. 'men's waxed cotton jacket',
    'hydraulic gear pump'). Use the id as category_id in searches and aspect filters."""
    try:
        res = await client().category_suggestions(query)
    except Exception as e:
        return _err(e)
    lines = []
    for s in (res.get("categorySuggestions") or [])[:12]:
        cat = s.get("category") or {}
        anc = " > ".join(a.get("categoryName", "") for a in reversed(s.get("categoryTreeNodeAncestors") or []))
        lines.append(f"{cat.get('categoryId')}: {anc + ' > ' if anc else ''}{cat.get('categoryName')}")
    return [_text("\n".join(lines) or "No suggestions.")]


@mcp.tool()
async def get_ebay_category_aspects(category_id: str, max_values: int = 15) -> Content:
    """List the item-specific aspects for a category (e.g. Size, Size Type, Chest Size, Inseam,
    Brand; or Voltage, Phase, Horsepower) and common values, for use in search aspects filters."""
    try:
        res = await client().category_aspects(category_id)
    except Exception as e:
        return _err(e)
    lines = []
    for a in res.get("aspects") or []:
        con = a.get("aspectConstraint") or {}
        vals = [v.get("localizedValue") for v in (a.get("aspectValues") or [])[:max_values]]
        flag = " (required)" if con.get("aspectRequired") else (" (recommended)" if con.get("aspectUsage") == "RECOMMENDED" else "")
        lines.append(f"{a.get('localizedAspectName')}{flag}: {', '.join(v for v in vals if v)}")
    return [_text("\n".join(lines) or "No aspects for that category (try a leaf category).")]


def main() -> None:
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep stderr quiet
    mcp.run()


if __name__ == "__main__":
    main()
