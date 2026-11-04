# rasmio-mcp

A free [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server for
**[rasmio.com](https://rasmio.com)** (رسمیو) — Iran's company-registry aggregator.
Query Iranian company registration data, executives, registered capital and official
gazette notices (آگهی‌های روزنامه رسمی) directly from any MCP host (Claude Desktop,
ZCode, Cursor, …).

**No API key, no paid subscription.** The server uses rasmio.com's unauthenticated
web search API and parses the public server-rendered company pages.

## Tools

| Tool | Description |
|---|---|
| `search_rasmio(query, page=0, page_size=10)` | Search companies / persons / registered brands. Returns `title`, `kind`, `national_id`, `id`, `summary`, `information`, `link` per hit. |
| `get_company_info(national_id)` | Full public profile: title, شماره ثبت, کد اقتصادی, نوع شرکت, وضعیت, تاریخ تاسیس, سرمایه ثبتی (display + rial integer), موضوع فعالیت, نشانی, کد پستی. |
| `get_company_members(national_id)` | Board members, CEO and auditors with their roles (مدیرعامل, رئیس هیئت مدیره, نایب رئیس, عضو, بازرس اصلی/علی‌البدل) and profile links. |
| `get_company_gazettes(national_id)` | Recent official-gazette notices with title, تاریخ چاپ, تاریخ نامه ثبت, distribution type, body text and newspaper URL. |
| `search_official_gazette(query, max_rows=20)` | Search the **official gazette directly on rrk.ir** (روزنامه رسمی کشور, open data — the source rasmio aggregates, no quota): notice title/text, national ID, dates (تاریخ نامه/چاپ), newspaper no, tracking no, status, city, notice code and rrk.ir detail URL. |

Persian digits (۰-۹) and Arabic characters (ي/ك) in queries and national IDs are
normalized automatically.

## Install

Requires Python 3.10+.

```bash
cd rasmio-mcp
uv venv .venv && uv pip install -e . --python .venv/bin/python
# or: python3 -m venv .venv && .venv/bin/pip install -e .
```

## MCP host configuration

Point your MCP client at the server over stdio:

```json
{
  "mcpServers": {
    "rasmio": {
      "command": "/path/to/rasmio-mcp/.venv/bin/python",
      "args": ["-m", "rasmio_mcp.server"]
    }
  }
}
```

Or, if installed in a persistent environment, use the console script:

```json
{
  "mcpServers": {
    "rasmio": {
      "command": "rasmio-mcp"
    }
  }
}
```

### Environment variables (optional)

| Variable | Default | Meaning |
|---|---|---|
| `RASMIO_TIMEOUT` | `30` | HTTP timeout in seconds. |
| `RASMIO_COMPANY_TTL` | `21600` (6 h) | Seconds a parsed company page stays cached. One page fetch serves all three company tools. |
| `RASMIO_MIN_INTERVAL` | `4` | Minimum seconds between company-page fetches (politeness pacing). |
| `RASMIO_THROTTLE_RETRIES` | `1` | Automatic retries (with backoff) when rasmio serves its quota page. `0` disables. |
| `RKK_TIMEOUT` | `45` | HTTP timeout in seconds for rrk.ir requests. |
| `RKK_INTERMEDIATE_PEM` | *(auto)* | Path to a PEM of the Certum intermediate that signs rrk.ir's leaf certificate. |

## The rasmio.com free quota (important)

rasmio.com limits **anonymous** company-page views to about **5 views per
4 days per IP** (the site's own notice: «سقف مجاز 5 درخواست شما برای 4 روز
تکمیل شده است»). Once the quota is exhausted, every company-page request is
redirected to a data-less shell page (`/company/{id}/direct/`) until the
window resets. The search API is separate and was not observed to be limited.

This server is built around that constraint:

1. each company page is fetched **once** and info, members and gazettes are
   all parsed from that single fetch, then cached for `RASMIO_COMPANY_TTL`
   (default 6 h) — so querying 3 tools for the same company costs 1 page view;
2. consecutive page fetches are paced (`RASMIO_MIN_INTERVAL`);
3. when the quota page appears, the tool fails fast with a clear, actionable
   MCP error instead of hanging.

If you hit the quota: wait for the 4-day window (or use a different IP), and
raise `RASMIO_COMPANY_TTL` if your agents re-query the same companies.

## Example MCP usage

```
search_rasmio {"query": "دیجی کالا"}
# → hits with national_id "14007650912" for شرکت گروه تجارت الکترونیک دیجی کالا

get_company_info {"national_id": "14007650912"}
# → شماره ثبت 527349, کد اقتصادی 411619758786, نوع شرکت سهامی خاص, ...

get_company_members {"national_id": "14007650912"}
# → مدیرعامل: سعید محمدی, رئیس هیئت مدیره, بازرس اصلی, ...

get_company_gazettes {"national_id": "14007650912"}
# → آگهی‌های روزنامه رسمی with print dates and full text
```

Other sample IDs: Digikala `14007650912`, Rasmio (پیشگامان رسمی پردازش دانا)
`14009396050`.

## Tests

```bash
.venv/bin/python tests/test_smoke.py
```

Four tiers, all green regardless of quota state:

1. **Offline normalization** — Persian/Arabic digit and character folding.
2. **Offline parsing** — full parser verification against
   `tests/fixtures/company_page_digikala.html`, a real captured rasmio.com
   company page (Digikala): all registration fields, 9 merged members/auditors,
   3 gazette notices.
3. **Live checks** — the rasmio search API always runs live; the company-page
   tools run live when the anonymous quota has room, and otherwise verify the
   clean `RateLimitedError` path instead. rrk.ir's official-gazette search
   runs live in both modes (free text + national ID).
4. **MCP stdio session** — spawns the real server process and performs
   initialize → tools/list → tools/call (including `search_official_gazette`)
   like an MCP host, keeping stdin open while responses arrive.

## How it works / limitations

- **Sources:** rasmio.com's free JSON search endpoint (`/api/search/search/all`)
  and SSR company page (`/company/{national_id}/`), plus rrk.ir's APEX
  open-data search (page 199 «داده باز»). Parsing is label-driven (visible
  Persian labels, `aria-label` attributes), not CSS-class driven, so it
  survives styling changes.
- **rrk.ir (official gazette):** the portal is an Oracle APEX app whose
  certificate chain is served incomplete; the client fetches the missing
  Certum intermediate automatically (or set `RKK_INTERMEDIATE_PEM`). The
  replicated flow: search hub → checksummed open-data link → APEX page submit
  with the full page-items protection context (per-item `ck` checksums,
  `protected` list, `salt`) → Interactive Grid data request for the notices
  region (`AGAHI_MATN`). If rrk.ir's layout changes, `RkkError` explains where.
- **Quota page detection:** a rasmio company page whose redirect lands on
  `/company/{id}/direct/` (or that lacks any registration data) is treated as
  the anonymous-quota shell, not as an empty result.
- **Shareholders:** granular shareholder data is not published on the free
  rasmio page; shareholders appear only inside gazette notice texts — which is
  exactly what `search_official_gazette` exposes.
- **Gazettes:** `get_company_gazettes` shows the recent notices embedded on
  the rasmio page; `search_official_gazette` reaches the full open-data index
  on rrk.ir (up to 100 rows per call).
- Data belongs to rasmio.com / روزنامه رسمی کشور; this client only reads
  public pages at human-like pace and expects you to respect their terms of
  service.
