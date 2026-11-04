#!/usr/bin/env python3
"""Smoke tests for the rasmio MCP server.

Run with the project venv:
    .venv/bin/python tests/test_smoke.py

Tiers:
  1. Offline checks (always run): normalization helpers + full parser
     verification against tests/fixtures/company_page_digikala.html — a real
     captured rasmio.com company page.
  2. Live search API (always run): rasmio's search endpoint is not covered by
     the anonymous page-view quota.
  3. Live company pages (quota-dependent): rasmio.com allows anonymous IPs
     only ~5 company-page views per 4 days. If the quota is exhausted the
     client must fail with a clear RateLimitedError — which this suite then
     verifies instead of the happy path.
  4. MCP stdio session: initialize → tools/list → tools/call against the real
     server process, exactly like an MCP host.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rasmio_mcp.client import (  # noqa: E402
    NotFoundError,
    RasmioClient,
    RasmioError,
    RateLimitedError,
    normalize_digits,
    normalize_national_id,
    normalize_query,
)
from rasmio_mcp.rkk_client import RkkClient, RkkError  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from rasmio_mcp import client as rc  # noqa: E402

PASS = "✓"
FAIL = "✗"
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {PASS if condition else FAIL} {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


DIGIKALA_ID = "14007650912"
RASMIO_ID = "14009396050"

# ---------------------------------------------------------------------------
print("\n[1/4] offline normalization checks")
# ---------------------------------------------------------------------------
check("persian digits → ascii", normalize_digits("۱۴۰۰۷۶۵۰۹۱۲") == "14007650912")
check("arabic digits → ascii", normalize_digits("٠١٢٣") == "0123")
check("arabic yeh/kaf → persian", normalize_query("كود ي") == "کود ی")
check("query whitespace collapse", normalize_query("  دیجی   کالا  ") == "دیجی کالا")
check("national id 11 digits", normalize_national_id("۱۴۰۰۷۶۵۰۹۱۲") == "14007650912")
try:
    normalize_national_id("12345")
    check("national id rejects short input", False)
except ValueError:
    check("national id rejects short input", True)

# ---------------------------------------------------------------------------
print("\n[2/4] offline parser checks against captured page fixture")
# ---------------------------------------------------------------------------
FIXTURE = ROOT / "tests" / "fixtures" / "company_page_digikala.html"
soup = BeautifulSoup(FIXTURE.read_text(encoding="utf-8"), "html.parser")

info = rc._parse_basic_info(soup)
check("fixture: title", rc._page_title(soup) == "شرکت گروه تجارت الکترونیک دیجی کالا",
      str(rc._page_title(soup)))
check("fixture: national id", info.get("national_id") == DIGIKALA_ID, str(info.get("national_id")))
check("fixture: registration number", info.get("registration_number") == "527349",
      str(info.get("registration_number")))
check("fixture: economic code", info.get("economic_code") == "411619758786",
      str(info.get("economic_code")))
check("fixture: legal type", info.get("legal_type") == "سهامی خاص", str(info.get("legal_type")))
check("fixture: status", info.get("status") == "فعال", str(info.get("status")))
check("fixture: establishment date", (info.get("establishment_date") or "").startswith("1397/03/23"),
      str(info.get("establishment_date")))
check("fixture: last gazette date", (info.get("last_gazette_date") or "").startswith("1405/05/28"),
      str(info.get("last_gazette_date")))
check("fixture: capital", rc._capital_rial(info.get("registered_capital")) == 13137000000000,
      str(info.get("registered_capital")))
check("fixture: activity subject", bool(info.get("activity_subject") and
                                        len(info["activity_subject"]) > 500))
check("fixture: address", bool(info.get("registered_address") and "تهران" in info["registered_address"]))
check("fixture: postal code", info.get("postal_code") == "1983969948", str(info.get("postal_code")))

members = rc._parse_members(soup)
check("fixture: members found", len(members) == 9, str(len(members)))
by_name = {m["name"]: m for m in members}
ceo = by_name.get("سعید محمدی")
check("fixture: CEO roles merged", bool(ceo) and set(ceo["roles"]) == {"مدیرعامل", "عضو هیئت مدیره"},
      str(ceo))
check("fixture: chairman present", "محمدمهدی عباسخانی" in by_name and
      "رئیس هیئت مدیره" in " ".join(by_name["محمدمهدی عباسخانی"]["roles"]))
check("fixture: vice chairman", "حمید محمدی" in by_name and
      any("نائب" in r for r in by_name["حمید محمدی"]["roles"]))
auditors = [m for m in members if any("بازرس" in r for r in m["roles"])]
check("fixture: both auditors", len(auditors) == 2 and
      any(m["entity"] == "company" for m in auditors), str([a["name"] for a in auditors]))
check("fixture: auditor role text clean",
      all(r in ("بازرس اصلی", "بازرس علی‌البدل") for m in auditors for r in m["roles"]),
      str([m["roles"] for m in auditors]))

gazettes = rc._parse_gazettes(soup)
check("fixture: gazettes found", len(gazettes) == 3, str(len(gazettes)))
check("fixture: gazette ids", [g["newspaper_id"] for g in gazettes] ==
      ["1133869", "1125842", "1116969"], str([g["newspaper_id"] for g in gazettes]))
check("fixture: gazette dates", all(g["print_date"] and "/" in g["print_date"] for g in gazettes),
      str([g["print_date"] for g in gazettes]))
check("fixture: gazette bodies", all((g["body"] or "").startswith("به شماره ثبت") for g in gazettes))
check("fixture: gazette urls", all(g["url"].startswith("https://rasmio.com/newspaper/") for g in gazettes))

# ---------------------------------------------------------------------------
print("\n[3/4] live rasmio.com checks (search API + company quota handling)")
# ---------------------------------------------------------------------------
client = RasmioClient(max_retries=0)  # fail fast on exhausted quota
quota_exhausted = False
try:
    t0 = time.time()
    result = client.search("دیجی کالا")
    print(f"  (search took {time.time() - t0:.1f}s)")
    check("live: total_count present", isinstance(result["total_count"], int) and result["total_count"] > 0,
          f"got {result.get('total_count')}")
    check("live: hits non-empty", len(result["hits"]) > 0)
    company_hits = [h for h in result["hits"] if h["kind"] == "company"]
    check("live: Digikala company hit", any(h["national_id"] == DIGIKALA_ID for h in company_hits),
          f"company hits: {[h['national_id'] for h in company_hits]}")
    check("live: hit summary present", all(h["summary"] is not None for h in result["hits"][:3]))

    by_id = client.search("۱۴۰۰۷۶۵۰۹۱۲")
    check("live: persian-digit ID query finds company",
          any(h["kind"] == "company" and h["national_id"] == DIGIKALA_ID for h in by_id["hits"]),
          str(by_id["hits"][:2]))

    try:
        t0 = time.time()
        live_info = client.get_company_info(DIGIKALA_ID)
        print(f"  (company info took {time.time() - t0:.1f}s — quota available)")
        check("live: info title", bool(live_info["title"]), str(live_info.get("title")))
        check("live: info registration number", live_info["registration_number"] == "527349",
              str(live_info.get("registration_number")))
        members_live = client.get_company_members(DIGIKALA_ID)  # served from cache
        gazettes_live = client.get_company_gazettes(DIGIKALA_ID)  # served from cache
        check("live: members from same fetch", members_live["count"] == 9)
        check("live: gazettes from same fetch", gazettes_live["count"] == 3)
        info2 = client.get_company_info(RASMIO_ID)
        check("live: second company (rasmio) info", bool(info2["title"]))
    except RateLimitedError as exc:
        quota_exhausted = True
        print("  (anonymous page-view quota exhausted — verifying the clean error path)")
        check("live: quota error is informative", "5 company-page views" in str(exc.message), str(exc))
    except NotFoundError as exc:
        check("live: company page fetch", False, f"unexpected NotFound: {exc}")

    # Invalid national ID must raise a clean error (404 when quota allows,
    # quota page otherwise).
    try:
        client.get_company_info("0000000000")
        check("live: invalid id raises", False, "no error raised")
    except NotFoundError:
        check("live: invalid id raises NotFoundError", True)
    except RateLimitedError:
        check("live: invalid id raises NotFoundError", quota_exhausted,
              "RateLimitedError while quota was available")
    except Exception as exc:  # noqa: BLE001
        check("live: invalid id raises", False, f"unexpected {exc.__class__.__name__}: {exc}")
finally:
    client.close()

# ---------------------------------------------------------------------------
print("\n[3.5/4] live rrk.ir official-gazette checks (open data, no quota)")
# ---------------------------------------------------------------------------
rkk = RkkClient()
try:
    t0 = time.time()
    gaz = rkk.search_notices("دیجی کالا", max_rows=10)
    print(f"  (rrk search took {time.time() - t0:.1f}s)")
    check("rrk: notices returned", gaz["count"] > 0, str(gaz.get("count")))
    n0 = gaz["notices"][0]
    check("rrk: notice has title_text", bool(n0.get("title_text")))
    check("rrk: notice has national_id", bool(n0.get("national_id")))
    check("rrk: notice has newspaper_date", bool(n0.get("newspaper_date")))
    check("rrk: notice has detail_url",
          str(n0.get("detail_url", "")).startswith("https://rrk.ir/"))
    check("rrk: notice has notice_code", bool(n0.get("notice_code")))

    gaz2 = rkk.search_notices(DIGIKALA_ID, max_rows=10)
    check("rrk: national-ID search returns notices", gaz2["count"] > 0)
except RkkError as exc:
    print(f"  (rrk.ir unavailable: {exc.message[:120]})")
    check("rrk: live search", False, exc.message[:200])
finally:
    rkk.close()

# ---------------------------------------------------------------------------
print("\n[4/4] MCP stdio handshake + tool calls")
# ---------------------------------------------------------------------------
MESSAGES = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "smoke-test", "version": "0.0.0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "search_rasmio", "arguments": {"query": "دیجی کالا"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "get_company_info", "arguments": {"national_id": DIGIKALA_ID}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "search_official_gazette", "arguments": {"query": DIGIKALA_ID, "max_rows": 5}}},
]

env = dict(os.environ)
env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
t0 = time.time()

# Behave like a real MCP host: keep stdin open while reading responses, and
# only close it after every request has been answered (closing stdin early
# makes the SDK cancel in-flight requests).
import threading
import queue

proc = subprocess.Popen(
    [sys.executable, "-m", "rasmio_mcp.server"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, cwd=str(ROOT), env=env,
)
assert proc.stdin is not None and proc.stdout is not None
for m in MESSAGES:
    proc.stdin.write(json.dumps(m, ensure_ascii=False) + "\n")
proc.stdin.flush()

lines_q: queue.Queue[str] = queue.Queue()
def _reader() -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        lines_q.put(line)
    lines_q.put("")  # EOF sentinel
threading.Thread(target=_reader, daemon=True).start()

responses: dict[int | str, dict] = {}
deadline = time.time() + 240
while time.time() < deadline and len(responses) < len(MESSAGES) - 1:  # -1: notification
    try:
        line = lines_q.get(timeout=max(0.1, deadline - time.time()))
    except queue.Empty:
        break
    if not line:
        break
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "id" in msg:
        responses[msg["id"]] = msg

proc.stdin.close()
try:
    proc.wait(timeout=15)
except subprocess.TimeoutExpired:
    proc.kill()
stderr_out = proc.stderr.read() if proc.stderr else ""
print(f"  (stdio session took {time.time() - t0:.1f}s)")

check("stdio: initialize response", 1 in responses and "result" in responses.get(1, {}),
      stderr_out[-300:] if 1 not in responses else "")
check("stdio: server name = rasmio",
      responses.get(1, {}).get("result", {}).get("serverInfo", {}).get("name") == "rasmio")
tools = responses.get(2, {}).get("result", {}).get("tools", [])
tool_names = sorted(t["name"] for t in tools)
check("stdio: tools/list has 5 tools",
      tool_names == ["get_company_gazettes", "get_company_info", "get_company_members",
                     "search_official_gazette", "search_rasmio"],
      str(tool_names))

call3 = responses.get(3, {})
check("stdio: search_rasmio call answered", bool(call3.get("result")) and not call3.get("error"),
      json.dumps(call3, ensure_ascii=False)[:200])
check("stdio: search_rasmio call ok",
      bool(call3.get("result")) and not call3["result"].get("isError"),
      json.dumps(call3, ensure_ascii=False)[:200])
if call3.get("result"):
    text3 = "".join(c.get("text", "") for c in call3["result"].get("content", []))
    check("stdio: search payload has Digikala company", DIGIKALA_ID in text3, text3[:200])

call4 = responses.get(4, {})
result4 = call4.get("result") or {}
if result4 and not result4.get("isError"):
    text4 = "".join(c.get("text", "") for c in result4.get("content", []))
    check("stdio: company info payload", "527349" in text4 and "14007650912" in text4, text4[:200])
elif result4.get("isError"):
    text4 = "".join(c.get("text", "") for c in result4.get("content", []))
    check("stdio: graceful quota error via MCP", "5 company-page views" in text4, text4[:300])
else:
    check("stdio: company info call answered", False, json.dumps(call4, ensure_ascii=False)[:200])

call5 = responses.get(5, {})
result5 = call5.get("result") or {}
if result5 and not result5.get("isError"):
    text5 = "".join(c.get("text", "") for c in result5.get("content", []))
    check("stdio: gazette search call ok", "rrk.ir" in text5 and "notices" in text5, text5[:200])
elif result5.get("isError"):
    text5 = "".join(c.get("text", "") for c in result5.get("content", []))
    check("stdio: gazette search call ok", False, text5[:300])
else:
    check("stdio: gazette search call answered", False, json.dumps(call5, ensure_ascii=False)[:200])

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
mode = "QUOTA-EXHAUSTED (error path verified; parsers verified offline)" if quota_exhausted else "full live"
print(f"RESULT: {'FAILED ' + str(failures) if failures else 'ALL CHECKS PASSED'} — mode: {mode}")
if failures:
    sys.exit(1)
