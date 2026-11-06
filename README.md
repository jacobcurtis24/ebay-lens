# ebay-lens

A local MCP server that lets Claude search eBay **and look at the photos**: thumbnail contact
sheets of whole result pages, full listing photo sets, full-resolution zoom crops (tags,
nameplates, flaws), and visual-similarity search from a photo or an existing listing.

It is read-only and uses only eBay's official APIs (Browse + Taxonomy) with your own developer
keys. It cannot buy, bid, offer, or message, and it stores nothing on disk.

## Tools

| Tool | What it does |
|---|---|
| `search_ebay` | Keyword search with price, condition, buying-format, aspect (Size, Brand…), location, local-pickup-radius and free-shipping filters; sort by price/newest/ending/distance. Returns numbered results **plus a contact-sheet image** of thumbnails. `show_refinements=True` adds available categories, conditions and aspect values with counts. |
| `search_ebay_by_image` | Visually similar listings from a local photo (`image_path`), an `image_url`, or an existing listing (`similar_to_item`, which inherits its category). Uses eBay's *experimental* `searchByImage`. |
| `get_ebay_item` | Full details: condition notes, item specifics (measurements, MPN…), seller description (HTML stripped), shipping, returns, location, variations. Accepts `v1|…|0` ids, legacy numeric ids, or `ebay.com/itm/…` URLs. |
| `get_ebay_item_photos` | Downloads a listing's photos (up to 12 per call, paged) and returns them as images Claude sees. |
| `zoom_ebay_photo` | Crops one photo at eBay's max (1600 px) resolution, e.g. `box=[0.5,0,1,0.5]`, to read labels, serial plates, gauges, or check damage. |
| `suggest_ebay_categories` | Category ids from a description, which aspect filters need. |
| `get_ebay_category_aspects` | Valid aspect names/values for a category (Size Type, Inseam, Voltage, Phase…). |

## Setup

### 1. eBay developer keys (free)
1. Join at <https://developer.ebay.com> and create an application keyset under **Production**.
   **App ID = `EBAY_CLIENT_ID`**, **Cert ID = `EBAY_CLIENT_SECRET`**.
2. eBay may ask you to configure *Marketplace Account Deletion* notifications before enabling
   production keys. This tool stores no eBay user data, so the exemption option applies.

Default quota is 5,000 Browse calls/day. A search is 1 call; photos/thumbnails come from eBay's
image CDN and are not API calls.

### 2. Install
Requires Python 3.10+. Install into a dedicated environment, not a shared or system one: `mcp`
pulls in a fair number of dependencies.

**Option A: uv**
```bash
cd ebay-lens
uv sync
```

**Option B: plain venv + pip** (no uv needed)
```bash
cd ebay-lens
python3 -m venv .venv          # python3 must be 3.10+; check with: python3 --version
.venv/bin/pip install -e .
```
If your default `python3` is older (e.g. an active conda `base` on 3.9), create the venv with a
newer interpreter's full path instead, e.g. `~/miniconda3/envs/py312/bin/python -m venv .venv`
or `/usr/bin/python3.10 -m venv .venv`. The venv keeps using that interpreter, so nothing needs
activating later.

On Windows (PowerShell), the venv puts executables in `Scripts\` instead of `bin/`:
```powershell
cd ebay-lens
py -3.12 -m venv .venv         # any installed 3.10+ works; list them with: py -0
.venv\Scripts\pip install -e .
```

Check the install with `.venv/bin/python tests/test_mocked.py` (Windows:
`.venv\Scripts\python tests\test_mocked.py`). No eBay keys needed.

### 3. Connect

**Claude Code** (available in all projects):
```bash
# Option A (uv)
claude mcp add ebay-lens -s user \
  -e EBAY_CLIENT_ID=YOUR_APP_ID -e EBAY_CLIENT_SECRET=YOUR_CERT_ID \
  -e EBAY_POSTAL_CODE=YOUR_POSTAL_CODE \
  -- uv run --directory /absolute/path/to/ebay-lens ebay-lens

# Option B (venv): point straight at the venv's entry point
claude mcp add ebay-lens -s user \
  -e EBAY_CLIENT_ID=YOUR_APP_ID -e EBAY_CLIENT_SECRET=YOUR_CERT_ID \
  -e EBAY_POSTAL_CODE=YOUR_POSTAL_CODE \
  -- /absolute/path/to/ebay-lens/.venv/bin/ebay-lens
```

**Claude desktop app** (macOS, Windows, Linux):

1. **Open the config file.** In the app, go to Settings → Developer → Edit Config. That works on
   every OS and creates the file if it doesn't exist. Or open it directly:

   | OS | Config file |
   |---|---|
   | macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
   | Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
   | Linux | `~/.config/Claude/claude_desktop_config.json` |

2. **Add the server.** Put `ebay-lens` under `"mcpServers"`. If the file already has content
   (other servers or settings), merge this entry in rather than replacing the file. Use the
   `command`/`args` that match how you installed:

   **uv** (any OS):
   ```json
   {
     "mcpServers": {
       "ebay-lens": {
         "command": "uv",
         "args": ["run", "--directory", "/absolute/path/to/ebay-lens", "ebay-lens"],
         "env": {
           "EBAY_CLIENT_ID": "YOUR_APP_ID",
           "EBAY_CLIENT_SECRET": "YOUR_CERT_ID",
           "EBAY_POSTAL_CODE": "YOUR_POSTAL_CODE"
         }
       }
     }
   }
   ```
   The app is launched from your desktop, not a terminal, so it may not see the `PATH` from your
   shell. If it can't find `uv`, use uv's full path as `command` (find it with `which uv` on
   macOS/Linux, `where uv` on Windows).

   **venv, macOS/Linux**: replace `command`/`args` with:
   ```json
   "command": "/absolute/path/to/ebay-lens/.venv/bin/ebay-lens",
   "args": []
   ```

   **venv, Windows**: replace `command`/`args` with (backslashes must be doubled in JSON;
   forward slashes also work):
   ```json
   "command": "C:\\Users\\you\\ebay-lens\\.venv\\Scripts\\ebay-lens.exe",
   "args": []
   ```

   Keep the same `env` block in every case.

3. **Restart the app fully.** Closing the window isn't enough: quit with Cmd+Q on macOS, or
   right-click the tray icon → Quit on Windows/Linux, then reopen it.

4. **Check it connected.** Settings → Developer shows each server's status. If `ebay-lens`
   failed, look at the `mcp*.log` files:

   | OS | Logs |
   |---|---|
   | macOS | `~/Library/Logs/Claude/` |
   | Windows | `%APPDATA%\Claude\logs\` |
   | Linux | `~/.config/Claude/logs/` |

   The most common error is `ENOENT` (on Linux: `Failed to spawn process: No such file or
   directory`), meaning the `command` can't be found. Check that it's a real absolute path, not
   the `/absolute/path/to/...` placeholder, and that the program exists: with `"command": "uv"`
   this also happens when uv isn't installed at all. In that case use the venv variant above.

A local stdio server works in Claude Code and the desktop app, not in claude.ai in a browser or
on mobile. Those would need it hosted as a remote HTTPS connector.

### Environment variables
| Var | Default | Purpose |
|---|---|---|
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | required | Production keyset |
| `EBAY_POSTAL_CODE` | unset | Accurate shipping estimates, `sort=distance`, local-pickup search |
| `EBAY_MARKETPLACE` | `EBAY_US` | e.g. `EBAY_GB`, `EBAY_DE` |
| `EBAY_COUNTRY` | `US` | Country paired with the postal code |

## Example prompts
- "Find a used waxed cotton jacket in size L under $150. Check the contact sheet, then inspect
  the best few for wear, torn linings and broken zips, and zoom on the tags to confirm the brand
  and estimate its age."
- "Here's a photo of a jacket I like (`photo.jpg`). Find similar ones on eBay in men's size L."
- "Find a used mechanical keyboard under $80 with free shipping. Check the contact sheet, then
  look through the photos of the best few for missing keycaps, yellowing or worn legends, and
  compare them."
- "Show me listings similar to https://www.ebay.com/itm/123456789012 but cheaper."

## eBay terms: how this stays compliant
- **Official API, your keys.** eBay's User Agreement (effective Feb 20 2026) prohibits scrapers
  and LLM-driven bots accessing eBay *without eBay's express permission*. The developer program
  and API License Agreement are that permission. This server never scrapes ebay.com pages.
- **No purchasing paths.** The code calls only Browse search/searchByImage/getItem and Taxonomy
  lookups. There are no Order, Offer/bidding, or Sell endpoints. Buying happens with you on ebay.com.
- **No Restricted APIs.** The API License restricts feeding "Restricted APIs" (market trends,
  pricing insights, AI-generated content, e.g. Marketplace Insights / sold-price data) into
  third-party AI tools. None are used. Don't add sold-listing tools without eBay's written consent.
- **No training, no persistence.** Data is used only to answer your query. There's a 10-minute
  in-memory item cache; nothing is written to disk.
- Not legal advice. Re-read the API License Agreement if you extend this.

## Testing
`.venv/bin/python tests/test_mocked.py` (or `uv run python tests/test_mocked.py`) drives every
tool through a real MCP client session against a mocked eBay API: filter construction,
auth/token caching, contact sheets, photo paging, zoom, image search, variation listings, and an
assertion that no non-GET call except token and searchByImage is ever made. It writes sample
images to `tests/out/` (git-ignored).

## Known limits
- `searchByImage` is marked experimental by eBay: quality varies, and adding `category_id` helps a lot.
- Aspect filters need exactly one category id (an eBay rule).
- Some listings only have low-resolution photos; zoom reports the source size.
