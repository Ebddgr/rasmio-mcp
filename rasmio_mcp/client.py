"""HTTP client and parsers for rasmio.com public data (no API key required).

Two free data sources are used:

1. The unauthenticated search JSON API:
   ``GET /api/search/search/all?q=...&page=...&pageSize=...``
2. The server-rendered company profile page ``/company/{national_id}/``
   (a Next.js page whose DOM contains the basic registration info, the
   members/executives section and the recent official-gazette cards).

Everything is plain public web scraping with browser-like headers; the
parsers are label-driven (they match visible Persian labels such as
"شماره ثبت") rather than class-name driven, so minor markup changes on
rasmio.com are less likely to break them.

Throttling: rasmio.com allows anonymous visitors only about **5 company-page
views per 4 days per IP** (confirmed by the page's own quota notice). Once the
quota is exhausted, every company-page request is redirected to a data-less
``/company/{id}/direct/`` shell until the window resets. This client therefore
caches a fully parsed company page per national ID for hours by default (one
fetch serves info + members + gazettes), paces consecutive page fetches, and
retries briefly when the shell page appears. All tunables are set via
environment variables.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://rasmio.com"
SEARCH_PATH = "/api/search/search/all"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa,en;q=0.9",
}
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

MAX_GAZETTE_BODY_CHARS = 6000


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class RasmioError(Exception):
    """Base error with a user-facing message."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(RasmioError):
    pass


class RateLimitedError(RasmioError):
    """rasmio.com served its data-less throttle page or HTTP 429."""



# --------------------------------------------------------------------------
# Persian text helpers
# --------------------------------------------------------------------------

_DIGIT_TABLE = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# Arabic → Persian letter folding (ي/ك are the common ones).
_CHAR_TABLE = str.maketrans({
    "ي": "ی",
    "ك": "ک",
    "ﻻ": "لا",
})


def normalize_digits(text: str) -> str:
    """Convert Persian (۰-۹) and Arabic-Indic (٠-٩) digits to ASCII 0-9."""
    return (text or "").translate(_DIGIT_TABLE)


def clean_text(text: Any) -> str | None:
    """Collapse whitespace; return None for empty strings."""
    if text is None:
        return None
    collapsed = re.sub(r"\s+", " ", str(text)).strip()
    return collapsed or None


def normalize_query(query: str) -> str:
    """Normalize a user search query: Persian digits → ASCII, ي/ك → ی/ک."""
    normalized = normalize_digits(str(query)).translate(_CHAR_TABLE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def match_key(text: Any) -> str:
    """Canonical comparison key for labels/roles: drops ZWNJ/tatweel,
    folds Arabic letters (incl. yeh hamza) and Persian digits, lowercases
    Latin, and strips trailing colons used inside label paragraphs."""
    key = normalize_digits(str(text or "")).translate(_CHAR_TABLE)
    key = key.replace("\u200c", "").replace("\u0640", "")
    key = key.replace("ئ", "ی").replace("أ", "ا").replace("إ", "ا").replace("ء", "")
    key = re.sub(r"\s+", " ", key).strip().lower()
    return key.rstrip(":").strip()


def normalize_national_id(value: str | int) -> str:
    """Validate and normalize a national ID (شناسه ملی): 10 or 11 digits."""
    digits = re.sub(r"\D", "", normalize_digits(str(value)).strip())
    if len(digits) not in (10, 11):
        raise ValueError(
            f"شناسه ملی (national id) must be 10 or 11 digits, got {digits!r}"
        )
    return digits


# --------------------------------------------------------------------------
# Label-driven page parsing
# --------------------------------------------------------------------------

# Visible label on the page -> output field name.
_BASIC_INFO_LABELS = {
    "تاریخ تاسیس": "establishment_date",
    "وضعیت شرکت": "status",
    "نوع شرکت": "legal_type",
    "آخرین آگهی روزنامه رسمی": "last_gazette_date",
    "شماره ثبت": "registration_number",
    "کد اقتصادی": "economic_code",
    "شناسه ملی": "national_id",
    "سرمایه ثبتی": "registered_capital",
    "موضوع فعالیت": "activity_subject",
    "نشانی ثبتی شرکت": "registered_address",
    "نشانی": "registered_address",
    "کد پستی": "postal_code",
}

_ROLE_PATTERN = re.compile(
    r"(مدیرعامل|هیئت مدیره|هيئت مديره|رییس هیئت|رئیس هیئت|نایب|نائب|نایب‌رئیس|"
    r"بازرس|عضو هیئت|عضو اصلی|عضو علی‌البدل|قائم مقام|قائم‌مقام|سهامدار|"
    r"صاحب امضا|رئیس|رییس|ناظر خاص)"
)

_COLON_RE = re.compile(r"^[:\-–—|،,]*$")


def _aria_label_value(soup: BeautifulSoup, wanted: dict[str, str]) -> dict[str, str]:
    """First pass: read values from aria-label attributes shaped 'label: value'.

    The basic-info tiles on rasmio.com carry aria-labels like
    'شماره ثبت: 527349'. Returns {field_name: value}.
    """
    found: dict[str, str] = {}
    taken_labels = set()
    for el in soup.find_all(attrs={"aria-label": True}):
        raw = normalize_digits(el.get("aria-label", ""))
        if ":" not in raw:
            continue
        label, _, value = raw.partition(":")
        key = match_key(label)
        if key in wanted and key not in taken_labels:
            value = clean_text(value)
            if value:
                found[wanted[key]] = value
                taken_labels.add(key)
    return found


_LABEL_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "span")


def _next_text_value(node: Tag, max_levels_up: int = 4) -> str | None:
    """Return the next meaningful text after `node` in document order.

    Walks following siblings, climbing up to `max_levels_up` ancestors —
    the value on rasmio.com is usually a sibling <p> (possibly inside a
    sibling wrapper div), sometimes after a stray ':' text node.
    """
    current: Tag | None = node
    for _ in range(max_levels_up):
        if current is None:
            return None
        sibling = current.next_sibling
        while sibling is not None:
            if isinstance(sibling, Tag):
                text = clean_text(sibling.get_text(" "))
            else:
                text = clean_text(str(sibling))
            if text and not _COLON_RE.match(text):
                return text
            sibling = sibling.next_sibling
        current = current.parent
    return None


def _parse_basic_info(soup: BeautifulSoup) -> dict[str, str | None]:
    """Basic registration info, keyed by output field name."""
    wanted = {match_key(label): field for label, field in _BASIC_INFO_LABELS.items()}
    info: dict[str, str | None] = _aria_label_value(soup, wanted)

    # Second pass: find heading/paragraph/span elements whose text IS a known
    # label (e.g. 'موضوع فعالیت' is an <h3>, 'کد پستی:' a <p> with an inline
    # colon), then take the next meaningful text as the value. First
    # occurrence in DOM order wins (the basic-info card precedes other
    # sections).
    remaining = {key for key in wanted if wanted[key] not in info}
    if not remaining:
        return info

    for el in soup.find_all(_LABEL_TAGS):
        key = match_key(el.get_text())
        if key not in remaining:
            continue
        value = _next_text_value(el)
        if value:
            info[wanted[key]] = value
            remaining.discard(key)
            if not remaining:
                break
    return info


def _page_title(soup: BeautifulSoup) -> str | None:
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" "))
        if title:
            return title
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = clean_text(og["content"])
        if title:
            return title
    if soup.title and soup.title.string:
        return clean_text(re.sub(r"\s*[-|]\s*رسم.*$", "", soup.title.string))
    return None


def _capital_rial(display: str | None) -> int | None:
    """Extract a plain integer from a capital string like '۱۳٬۱۳۷٬۰۰۰٬۰۰۰ ریال'."""
    if not display:
        return None
    digits = re.sub(r"\D", "", normalize_digits(display))
    return int(digits) if digits else None


# --------------------------------------------------------------------------
# Members / executives
# --------------------------------------------------------------------------

def _id_from_href(href: str) -> str | None:
    """Second path segment of an entity link, e.g. '/person/3D67DE5/...' ->
    '3D67DE5', '/newspaper/1125842/' -> '1125842'."""
    parts = [p for p in href.split("/") if p]
    return parts[1] if len(parts) >= 2 else None


def _parse_person_cards(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Cards like <a href="/person/{id}/{name}/" aria-label="مشاهده پروفایل X">
    containing a role <p> ('مدیرعامل', 'عضو هیئت مدیره', ...) and the name."""
    members: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/person/" not in href:
            continue
        paragraphs = anchor.find_all("p")
        roles: list[str] = []
        name_from_p = None
        for p in paragraphs:
            text = clean_text(p.get_text(" "))
            if not text:
                continue
            if _ROLE_PATTERN.search(text):
                if text not in roles:
                    roles.append(text)
            elif name_from_p is None:
                name_from_p = text

        img = anchor.find("img", alt=True)
        aria = anchor.get("aria-label", "")
        name = clean_text(getattr(img, "alt", None)) or None
        if not name and aria.startswith("مشاهده پروفایل"):
            name = clean_text(aria.replace("مشاهده پروفایل", ""))
        if not name:
            name = name_from_p
        if not name:
            continue

        # Require a role keyword so unrelated person links (e.g. in other
        # sections) are not mistaken for executives.
        if not roles:
            continue

        key = (name, tuple(roles), href)
        if key in seen:
            continue
        seen.add(key)
        members.append({
            "name": name,
            "roles": roles,
            "id": _id_from_href(href.strip("/")),
            "profile_url": urljoin(BASE_URL, href),
            "entity": "person",
        })
    return members


def _parse_table_roles(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Table rows (e.g. the بازرسین / auditors table) where a cell holds the
    role and another cell links the entity (audit firm or person)."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        # Roles come from short <p> texts inside cells (the whole cell text
        # would glue the representative onto the role).
        roles: list[str] = []
        for cell in cells:
            for p in cell.find_all("p"):
                text = clean_text(p.get_text(" "))
                if text and _ROLE_PATTERN.search(text) and len(text) <= 40 and text not in roles:
                    roles.append(text)
        if not roles:
            continue
        texts = [clean_text(c.get_text(" ")) for c in cells]
        link = tr.find("a", href=True)
        name = None
        if link is not None:
            name = clean_text(link.get_text(" "))
            img = link.find("img", alt=True)
            if img and img.get("alt"):
                name = clean_text(img["alt"])
        if not name:
            name = next((t for t in texts if t and t not in roles), None)
        if not name:
            continue

        representative = None
        for cell in cells:
            cell_text = clean_text(cell.get_text(" ")) or ""
            if "نماینده" in cell_text:
                rep_link = cell.find("a", href=True)
                if rep_link is not None and rep_link is not link:
                    representative = clean_text(rep_link.get_text(" "))

        href = link["href"] if link is not None else ""
        key = (name, tuple(roles), href)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": name,
            "roles": roles,
            "id": _id_from_href(href.strip("/")) if href else None,
            "profile_url": urljoin(BASE_URL, href) if href else None,
            "entity": "company" if "/company/" in href else "person",
            "representative": representative,
        })
    return rows


def _parse_members(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Person cards + table rows, merged per identity (the page repeats the
    same executive in several card groups, e.g. CEO + board member)."""
    merged: dict[tuple, dict[str, Any]] = {}
    ordered_keys: list[tuple] = []

    def add(entry: dict[str, Any]) -> None:
        identity = (entry.get("name"), entry.get("id"))
        existing = merged.get(identity)
        if existing is None:
            merged[identity] = entry
            ordered_keys.append(identity)
            return
        existing_keys = {match_key(r) for r in existing["roles"]}
        existing["roles"].extend(
            r for r in entry["roles"] if match_key(r) not in existing_keys
        )
        if not existing.get("representative") and entry.get("representative"):
            existing["representative"] = entry["representative"]

    for card in _parse_person_cards(soup):
        add(card)
    for row in _parse_table_roles(soup):
        add(row)

    return [merged[k] for k in ordered_keys]


# --------------------------------------------------------------------------
# Official gazette notices (آگهی‌های روزنامه رسمی)
# --------------------------------------------------------------------------

def _parse_gazettes(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Gazette cards each contain one /newspaper/{id}/ link, a print-date
    ('تاریخ چاپ'), a letter-date ('تاریخ نامه ثبت'), a title and a body."""
    gazettes: list[dict[str, Any]] = []
    seen_newspapers: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/newspaper/" not in href:
            continue
        newspaper_id = _id_from_href(href.strip("/"))
        if newspaper_id in seen_newspapers:
            continue

        # Walk up to the card that also carries the date labels.
        card: Tag | None = anchor
        for _ in range(5):
            if card is None or card.name in ("body", "html", "[document]"):
                card = None
                break
            if "تاریخ چاپ" in card.get_text(" "):
                break
            card = card.parent
        if card is None:
            continue
        seen_newspapers.add(newspaper_id)

        title_p = anchor.find("p")
        title = clean_text(title_p.get_text(" ")) if title_p else clean_text(anchor.get_text(" "))

        print_date = letter_date = None
        for p in card.find_all("p"):
            text = clean_text(p.get_text(" ")) or ""
            if text.endswith(":") and match_key(text[:-1]) in ("تاریخ چاپ", "تاریخ نامه ثبت"):
                value = _next_text_value(p)
                if match_key(text[:-1]) == "تاریخ چاپ":
                    print_date = value
                else:
                    letter_date = value

        # Body = longest paragraph in the card that is not the title.
        body, distribution = None, None
        for p in card.find_all("p"):
            text = clean_text(p.get_text(" ")) or ""
            if not text or text == title:
                continue
            if len(text) <= 25 and not re.search(r"\d", text) and ":" not in text:
                distribution = distribution or text
            elif body is None or len(text) > len(body):
                body = text
        if body and len(body) > MAX_GAZETTE_BODY_CHARS:
            body = body[:MAX_GAZETTE_BODY_CHARS].rstrip() + " …"

        gazettes.append({
            "newspaper_id": newspaper_id,
            "title": title,
            "print_date": print_date,
            "letter_date": letter_date,
            "distribution": distribution,
            "body": body,
            "url": urljoin(BASE_URL, href),
        })
    return gazettes


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class RasmioClient:
    """httpx-based client with browser-like headers, friendly errors, a
    per-company parsed-page cache and pacing to stay under rasmio.com's
    page-fetch throttling."""

    # Backoff waits (seconds) before retrying when the quota shell appears.
    _THROTTLE_BACKOFF = (15.0, 45.0, 90.0)

    def __init__(self, timeout: float | None = None, max_retries: int | None = None,
                 cache_ttl: float | None = None, min_interval: float | None = None):
        timeout = timeout if timeout is not None else _env_float("RASMIO_TIMEOUT", 30.0)
        max_retries = max_retries if max_retries is not None else _env_int("RASMIO_THROTTLE_RETRIES", 1)
        cache_ttl = cache_ttl if cache_ttl is not None else _env_float("RASMIO_COMPANY_TTL", 21600.0)
        min_interval = min_interval if min_interval is not None else _env_float("RASMIO_MIN_INTERVAL", 4.0)

        self._http = httpx.Client(
            base_url=BASE_URL,
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        self._max_retries = max(0, int(max_retries))
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._min_interval = max(0.0, float(min_interval))
        self._company_cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._last_page_fetch = 0.0
        self._lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    # -- low level ---------------------------------------------------------

    def _get(self, path: str, *, params: dict | None = None, referer: str | None = None,
             accept: str | None = None) -> httpx.Response:
        headers = {"Accept": accept} if accept else {}
        if referer:
            headers["Referer"] = urljoin(BASE_URL, referer)

        attempt = 0
        while True:
            try:
                response = self._http.get(path, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                raise RasmioError(
                    f"rasmio.com request timed out after {self._http.timeout.read}s "
                    f"({exc.__class__.__name__}). Try again in a moment."
                ) from exc
            except httpx.HTTPError as exc:
                raise RasmioError(
                    f"Could not reach rasmio.com: {exc.__class__.__name__}: {exc}"
                ) from exc

            if response.status_code == 429:
                if attempt < self._max_retries:
                    attempt += 1
                    retry_after = response.headers.get("Retry-After")
                    delay = min(float(retry_after) if (retry_after or "").isdigit() else 2.0, 5.0)
                    time.sleep(delay)
                    continue
                raise RateLimitedError(
                    "rasmio.com is rate limiting this client (HTTP 429). "
                    "Wait a bit before issuing more requests."
                )
            if response.status_code == 404:
                raise NotFoundError(f"Not found on rasmio.com: {path} (HTTP 404)")
            if response.status_code >= 400:
                raise RasmioError(
                    f"rasmio.com returned HTTP {response.status_code} for {path}"
                )
            return response

    # -- company page: fetch once, parse once, cache ------------------------

    def _fetch_company(self, national_id: str) -> dict[str, Any]:
        """Fetch + fully parse a company page, cached per national ID.

        Returns {"national_id", "page_url", "title", "info", "members",
        "gazettes"}. All three public tools are served from this single
        fetch so one company costs at most one page request per TTL window.
        """
        nid = normalize_national_id(national_id)
        with self._lock:
            cached = self._company_cache.get(nid)
            now = time.monotonic()
            if cached and now - cached[1] < self._cache_ttl:
                return cached[0]

            # Politeness pacing between company page fetches.
            since_last = now - self._last_page_fetch
            if self._last_page_fetch and since_last < self._min_interval:
                time.sleep(self._min_interval - since_last)

            path = f"/company/{nid}/"
            attempts = self._max_retries + 1
            last_error: RateLimitedError | None = None
            for attempt in range(attempts):
                self._last_page_fetch = time.monotonic()
                response = self._get(
                    path,
                    referer=f"/search/all/?q={nid}",
                    accept=HTML_ACCEPT,
                )
                soup = BeautifulSoup(response.text, "html.parser")
                page_title = soup.title.get_text() if (soup.title and soup.title.get_text()) else ""
                if any(marker in page_title for marker in ("یافت نشد", "پیدا نشد", "404")):
                    raise NotFoundError(
                        f"No company found on rasmio.com for national id {nid}"
                    )

                info = _parse_basic_info(soup)
                members = _parse_members(soup)
                gazettes = _parse_gazettes(soup)
                title = _page_title(soup)

                if info.get("national_id") or (title and (members or gazettes)):
                    data = {
                        "national_id": info.get("national_id") or nid,
                        "page_url": str(response.url),
                        "title": title,
                        "info": info,
                        "members": members,
                        "gazettes": gazettes,
                    }
                    self._company_cache[nid] = (data, time.monotonic())
                    return data

                # Data-less response: either the throttle shell (redirect to
                # /direct/) or an unexpected layout. Retry, then report.
                throttled = "/direct" in str(response.url)
                if attempt + 1 < attempts:
                    last_error = RateLimitedError(self._throttle_message(nid, throttled))
                    wait = self._THROTTLE_BACKOFF[min(attempt, len(self._THROTTLE_BACKOFF) - 1)]
                    time.sleep(wait)
                    continue

            raise last_error or RateLimitedError(self._throttle_message(nid, throttled=True))

    @staticmethod
    def _throttle_message(nid: str, throttled: bool) -> str:
        return (
            f"rasmio.com served its data-less quota page instead of the profile for "
            f"national id {nid}"
            if throttled else
            f"rasmio.com returned a page without registration data for national id {nid} "
            "(the site layout may have changed, or the free quota is exhausted)"
        ) + (
            ". Anonymous access on rasmio.com is limited to about 5 company-page views "
            "per 4 days per IP; when it is exhausted every profile request is redirected "
            "to a data-less shell until the window resets. Results for companies already "
            "fetched stay cached (RASMIO_COMPANY_TTL) — retry later or use a different IP."
        )

    # -- public API --------------------------------------------------------

    def search(self, query: str, page: int = 0, page_size: int = 10) -> dict[str, Any]:
        """Search companies / persons / brands via the free JSON search API."""
        if not isinstance(page, int) or page < 0:
            raise ValueError("page must be a non-negative integer")
        page_size = max(1, min(int(page_size or 10), 50))

        q = normalize_query(query)
        if not q:
            raise ValueError("query must not be empty")

        response = self._get(
            SEARCH_PATH,
            params={"q": q, "page": page, "pageSize": page_size},
            referer=f"/search/all/?q={quote(q)}",
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise RasmioError("rasmio.com search returned a non-JSON response") from exc

        hits: list[dict[str, Any]] = []
        for hit in data.get("hits") or []:
            link = (hit.get("link") or "").strip()
            kind = link.strip("/").split("/")[0] if link else "unknown"
            is_company = kind == "company"
            hits.append({
                "title": clean_text(hit.get("title")),
                "kind": kind,
                "national_id": hit.get("id") if is_company else None,
                "id": hit.get("id"),
                "summary": clean_text(hit.get("subTitle")),
                "information": clean_text(hit.get("information")),
                "link": urljoin(BASE_URL, link) if link else None,
                "type": hit.get("type"),
            })

        total = data.get("totalCount")
        return {
            "query": q,
            "page": page,
            "page_size": page_size,
            "total_count": int(normalize_digits(str(total))) if total is not None else None,
            "hits": hits,
            "source": "rasmio.com (free web search API)",
        }

    def get_company_info(self, national_id: str) -> dict[str, Any]:
        """Fetch and parse the company profile page into structured metadata."""
        page = self._fetch_company(national_id)
        info = page["info"]
        capital_display = info.get("registered_capital")
        return {
            "source": "rasmio.com (free company page)",
            "page_url": page["page_url"],
            "national_id": page["national_id"],
            "title": page["title"],
            "registration_number": info.get("registration_number"),
            "economic_code": info.get("economic_code"),
            "legal_type": info.get("legal_type"),
            "status": info.get("status"),
            "establishment_date": info.get("establishment_date"),
            "last_gazette_date": info.get("last_gazette_date"),
            "registered_capital": capital_display,
            "registered_capital_rial": _capital_rial(capital_display),
            "activity_subject": info.get("activity_subject"),
            "registered_address": info.get("registered_address"),
            "postal_code": info.get("postal_code"),
        }

    def get_company_members(self, national_id: str) -> dict[str, Any]:
        """Board members, CEO and auditors (shareholders are not published on
        the free rasmio.com page)."""
        page = self._fetch_company(national_id)
        members = page["members"]
        return {
            "source": "rasmio.com (free company page)",
            "page_url": page["page_url"],
            "national_id": page["national_id"],
            "title": page["title"],
            "count": len(members),
            "members": members,
            "note": (
                "Includes board members, CEO and auditors as published on the free "
                "page; detailed shareholder data is not exposed there."
            ),
        }

    def get_company_gazettes(self, national_id: str) -> dict[str, Any]:
        """Recent official-gazette notices embedded on the company page."""
        page = self._fetch_company(national_id)
        gazettes = page["gazettes"]
        return {
            "source": "rasmio.com (free company page)",
            "page_url": page["page_url"],
            "national_id": page["national_id"],
            "title": page["title"],
            "last_gazette_date": page["info"].get("last_gazette_date"),
            "count": len(gazettes),
            "gazettes": gazettes,
            "note": "Shows the most recent notices embedded on the page (newest first).",
        }
