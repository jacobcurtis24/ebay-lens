"""Thin async client for the eBay Browse and Taxonomy APIs.

Read-only by construction: the only endpoints called are search, searchByImage,
getItem / getItemByLegacyId / getItemsByItemGroup, and Taxonomy lookups. No
Order, Offer (bidding), Marketplace Insights, or Sell endpoints exist in this file.
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

API = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"  # public data only

CONDITION_IDS = {
    "new": "1000",
    "new_other": "1500|1750",  # new without tags/box, new with defects
    "refurbished": "2000|2010|2020|2030|2500",
    "used": "3000|4000|5000|6000",  # used, very good, good, acceptable
    "for_parts": "7000",
}
BUYING_OPTIONS = {"fixed_price": "FIXED_PRICE", "auction": "AUCTION", "best_offer": "BEST_OFFER"}
SORTS = {
    "best_match": None,
    "price_asc": "price",
    "price_desc": "-price",
    "newly_listed": "newlyListed",
    "ending_soonest": "endingSoonest",
    "distance": "distance",
}


class EbayError(RuntimeError):
    pass


@dataclass
class EbayClient:
    client_id: str
    client_secret: str
    marketplace: str = "EBAY_US"
    country: str = "US"
    postal_code: str | None = None
    transport: httpx.AsyncBaseTransport | None = None  # injectable for tests
    _token: str | None = None
    _token_exp: float = 0.0
    _item_cache: dict[str, tuple[float, dict]] = field(default_factory=dict)
    _http: httpx.AsyncClient | None = None

    @classmethod
    def from_env(cls) -> "EbayClient":
        cid, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
        if not cid or not secret:
            raise EbayError("Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (production keyset from developer.ebay.com).")
        return cls(
            client_id=cid,
            client_secret=secret,
            marketplace=os.environ.get("EBAY_MARKETPLACE", "EBAY_US"),
            country=os.environ.get("EBAY_COUNTRY", "US"),
            postal_code=os.environ.get("EBAY_POSTAL_CODE") or None,
        )

    # ---------------------------------------------------------------- plumbing
    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30, transport=self.transport, follow_redirects=True)
        return self._http

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 120:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = await self.http.post(
            f"{API}/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE},
        )
        if r.status_code != 200:
            raise EbayError(f"OAuth token request failed ({r.status_code}): {r.text[:300]}")
        j = r.json()
        self._token, self._token_exp = j["access_token"], time.time() + int(j.get("expires_in", 7200))
        return self._token

    def _headers(self, token: str) -> dict[str, str]:
        h = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": self.marketplace}
        if self.postal_code:  # makes shipping cost estimates and distance sort accurate
            h["X-EBAY-C-ENDUSERCTX"] = "contextualLocation=" + quote(
                f"country={self.country},zip={self.postal_code}", safe=""
            )
        return h

    async def _request(self, method: str, path: str, **kw) -> dict:
        token = await self._get_token()
        r = await self.http.request(method, f"{API}{path}", headers=self._headers(token), **kw)
        if r.status_code >= 400:
            try:
                errs = r.json().get("errors", [])
                msg = "; ".join(f"[{e.get('errorId')}] {e.get('message')}" for e in errs) or r.text[:300]
            except ValueError:
                msg = r.text[:300]
            raise EbayError(f"eBay API {r.status_code}: {msg}")
        return r.json() if r.content else {}

    # ---------------------------------------------------------------- filters
    def build_filter(
        self,
        min_price: float | None = None,
        max_price: float | None = None,
        conditions: list[str] | None = None,
        buying_options: list[str] | None = None,
        item_location_country: str | None = None,
        local_pickup_radius_miles: int | None = None,
        free_shipping: bool = False,
        currency: str = "USD",
    ) -> str | None:
        parts: list[str] = []
        if min_price is not None or max_price is not None:
            lo = "" if min_price is None else f"{min_price:g}"
            hi = "" if max_price is None else f"{max_price:g}"
            parts += [f"price:[{lo}..{hi}]", f"priceCurrency:{currency}"]
        if conditions:
            bad = [c for c in conditions if c not in CONDITION_IDS]
            if bad:
                raise EbayError(f"Unknown condition(s) {bad}; use {list(CONDITION_IDS)}")
            parts.append("conditionIds:{" + "|".join(CONDITION_IDS[c] for c in conditions) + "}")
        if buying_options:
            bad = [b for b in buying_options if b not in BUYING_OPTIONS]
            if bad:
                raise EbayError(f"Unknown buying option(s) {bad}; use {list(BUYING_OPTIONS)}")
            parts.append("buyingOptions:{" + "|".join(BUYING_OPTIONS[b] for b in buying_options) + "}")
        if item_location_country:
            parts.append(f"itemLocationCountry:{item_location_country.upper()}")
        if local_pickup_radius_miles:
            if not self.postal_code:
                raise EbayError("Local pickup search needs EBAY_POSTAL_CODE set in the server environment.")
            parts += [
                f"pickupCountry:{self.country}",
                f"pickupPostalCode:{self.postal_code}",
                f"pickupRadius:{int(local_pickup_radius_miles)}",
                "pickupRadiusUnit:mi",
                "deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}",
            ]
        if free_shipping:
            parts.append("maxDeliveryCost:0")
        return ",".join(parts) or None

    @staticmethod
    def build_aspect_filter(category_id: str | None, aspects: dict[str, str] | None) -> str | None:
        if not aspects:
            return None
        if not category_id:
            raise EbayError("Aspect filters (e.g. Size, Brand) require exactly one category id.")
        cat = category_id.split(",")[0].strip()
        return ",".join([f"categoryId:{cat}"] + [f"{k}:{{{v}}}" for k, v in aspects.items()])

    # ---------------------------------------------------------------- endpoints
    async def search(self, *, q=None, category_ids=None, filter=None, aspect_filter=None,
                     sort=None, limit=20, offset=0, refinements=False) -> dict:
        if not q and not category_ids:
            raise EbayError("Provide a query and/or category_ids.")
        params: dict[str, str | int] = {"limit": max(1, min(int(limit), 200)), "offset": max(0, int(offset))}
        if q:
            params["q"] = q
        if category_ids:
            params["category_ids"] = category_ids
        if filter:
            params["filter"] = filter
        if aspect_filter:
            params["aspect_filter"] = aspect_filter
        if sort:
            params["sort"] = sort
        if refinements:
            params["fieldgroups"] = "MATCHING_ITEMS,ASPECT_REFINEMENTS,CATEGORY_REFINEMENTS,CONDITION_REFINEMENTS"
        return await self._request("GET", "/buy/browse/v1/item_summary/search", params=params)

    async def search_by_image(self, image_b64: str, *, category_ids=None, filter=None,
                              aspect_filter=None, sort=None, limit=20, offset=0) -> dict:
        params: dict[str, str | int] = {"limit": max(1, min(int(limit), 200)), "offset": max(0, int(offset))}
        for k, v in (("category_ids", category_ids), ("filter", filter),
                     ("aspect_filter", aspect_filter), ("sort", sort)):
            if v:
                params[k] = v
        return await self._request("POST", "/buy/browse/v1/item_summary/search_by_image",
                                   params=params, json={"image": image_b64})

    async def get_item(self, item_ref: str) -> dict:
        """Accepts a RESTful id (v1|123|0), a legacy numeric id, or an ebay.com/itm/ URL."""
        ref = item_ref.strip()
        m = re.search(r"/itm/(?:[^/]+/)?(\d{9,15})", ref)
        if m:
            ref = m.group(1)
        cached = self._item_cache.get(ref)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        if ref.isdigit():
            try:
                item = await self._request("GET", "/buy/browse/v1/item/get_item_by_legacy_id",
                                           params={"legacy_item_id": ref})
            except EbayError as e:
                if "11006" not in str(e):  # 11006 = listing has variations (item group)
                    raise
                group = await self._request("GET", "/buy/browse/v1/item/get_items_by_item_group",
                                            params={"item_group_id": ref})
                item = _merge_group(group)
        else:
            item = await self._request("GET", f"/buy/browse/v1/item/{quote(ref, safe='')}")
        self._item_cache[ref] = (time.time(), item)
        if item.get("itemId"):
            self._item_cache[item["itemId"]] = (time.time(), item)
        if len(self._item_cache) > 300:  # tiny in-memory cache only; nothing persisted
            for k in sorted(self._item_cache, key=lambda k: self._item_cache[k][0])[:100]:
                self._item_cache.pop(k, None)
        return item

    async def _tree_id(self) -> str:
        if not getattr(self, "_tid", None):
            tree = await self._request("GET", "/commerce/taxonomy/v1/get_default_category_tree_id",
                                       params={"marketplace_id": self.marketplace})
            self._tid = tree["categoryTreeId"]
        return self._tid

    async def category_suggestions(self, q: str) -> dict:
        tid = await self._tree_id()
        return await self._request("GET", f"/commerce/taxonomy/v1/category_tree/{tid}/get_category_suggestions",
                                   params={"q": q})

    async def category_aspects(self, category_id: str) -> dict:
        tid = await self._tree_id()
        return await self._request("GET", f"/commerce/taxonomy/v1/category_tree/{tid}/get_item_aspects_for_category",
                                   params={"category_id": category_id})

    async def fetch_bytes(self, url: str, max_bytes: int = 15_000_000) -> bytes:
        r = await self.http.get(url)
        r.raise_for_status()
        if len(r.content) > max_bytes:
            raise EbayError(f"Image too large ({len(r.content)} bytes): {url}")
        return r.content


def _merge_group(group: dict) -> dict:
    """Represent a multi-variation listing as one item, keeping variation info."""
    items = group.get("items") or []
    if not items:
        return group
    base = dict(items[0])
    per_item = [{a.get("name"): a.get("value") for a in it.get("localizedAspects", [])} for it in items]
    keys = set().union(*per_item)
    varying = {k for k in keys if len({d.get(k) for d in per_item}) > 1}  # e.g. Size, Color
    base["_variations"] = [
        {
            "itemId": it.get("itemId"),
            "price": it.get("price"),
            "aspects": {k: d.get(k) for k in sorted(varying) if d.get(k) is not None},
            "availability": (it.get("estimatedAvailabilities") or [{}])[0].get("estimatedAvailabilityStatus"),
        }
        for it, d in zip(items, per_item)
    ]
    # gather every photo across variations, de-duplicated
    seen, imgs = set(), []
    for it in items:
        for im in [it.get("image")] + (it.get("additionalImages") or []):
            if im and im.get("imageUrl") not in seen:
                seen.add(im["imageUrl"])
                imgs.append(im)
    if imgs:
        base["image"], base["additionalImages"] = imgs[0], imgs[1:]
    return base
