"""Client for the official Iranian gazette (روزنامه رسمی کشور, rrk.ir).

rkk.ir's current portal is an Oracle APEX app (workspace ``rrs``, app 200).
Its "open data" search page (alias داده-باز, page 199) searches the official
gazette notices by free text, company name, national ID and registration
number — the same data rasmio.com republishes, straight from the source and
without rasmio's anonymous page-view quota.

The flow replicated here (all plain HTTP, no browser):

1. ``GET /ords/f?p=200:103:0::NO::P103_ITEM_SEARCH:<term>`` — the search hub
   renders a checksummed (cs) link to page 199 with the term pre-set.
2. ``GET`` that link — page 199 renders with the term in session state.
3. ``POST wwv_flow.accept?p_context=...`` — the APEX page submit. APEX 23
   requires the pageItems protection context: an outer urlencoded body whose
   ``p_json`` member carries ``pageItems.itemsToSubmit`` (each item with its
   per-item ``ck`` checksum from ``input[data-for=...]``), the base64
   ``protected`` list, ``rowVersion``, ``formRegionChecksums`` and ``salt``.
   The response is ``{"redirectURL": ...}`` (post-redirect-get).
4. ``GET`` the redirect — the post-submit render embeds the Interactive Grid
   configuration for the notices region (AGAHI_MATN): reportId, region id,
   ajaxIdentifier, ajaxColumns and fresh protection tokens.
5. ``POST wwv_flow.ajax?p_context=...`` — the IG data request, again with a
   ``p_json`` payload (``regions[].fetchData``) — returns the notice rows as
   JSON (``regions[].fetchedData.values``).

TLS note: rrk.ir serves an incomplete certificate chain (leaf only). The
client therefore loads the Certum intermediate (which signs rrk.ir's leaf)
on top of the system store so verification stays on.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import tempfile
import warnings
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://rrk.ir"
ORDS = BASE_URL + "/ords"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ACCEPT_JSON = "application/json, text/javascript, */*; q=0.01"

INFRA_FIELDS = {
    "p_flow_id", "p_flow_step_id", "p_instance", "p_debug", "p_trace",
    "p_page_submission_id", "p_request", "p_reload_on_submit",
    "fmap", "fhdr", "fcud", "fcs", "frowid", "upload",
}

HUB_PATH = "/ords/f"
PAGE_199_SLUG = "%D8%AF%D8%A7%D8%AF%D9%87-%D8%A8%D8%A7%D8%B2"  # داده-باز

# Notices IG model column order as returned by fetchedData.values.
# Position → semantic field (calibrated against the live response; the model
# metadata dict in the last cell is skipped).
_NOTICE_COLUMNS = [
    "record_id",        # internal row id
    "title_html",       # <a href=...f-detail-ad?p28_code=...>title</a>
    "letter_date",      # تاریخ نامه ثبت
    "unknown_3",
    "newspaper_date",   # تاریخ چاپ روزنامه
    "tracking_no",      # شماره نامه (مکانیزه)
    "national_id",      # شناسه ملی شرکت
    "title_text",       # plain-text notice title
    "detail_html",      # action button carrying the detail link
    "status",           # e.g. انتشار یافته امضا شده
    "city",             # {'v': code, 'd': name}
    "insert_date_gregorian",
    "notice_code",      # p28_code of the detail page
    "notice_type",      # e.g. آگهی تاسيس / تغییرات
    "newspaper_no",     # شماره روزنامه
    "_meta",            # {'salt': ..., 'protected': ...} — skipped
]


class RkkError(Exception):
    """Base error for rrk.ir flows with a user-facing message."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class RkkPageError(RkkError):
    """APEX returned an in-page error (e.g. page protection violation)."""


def _build_ssl_context() -> ssl.SSLContext:
    """System verification + the missing rrk.ir intermediate certificate."""
    ctx = ssl.create_default_context()
    intermediate = os.environ.get("RKK_INTERMEDIATE_PEM")
    if not intermediate:
        try:  # fetch it once per process and cache in a temp file
            cached = os.path.join(tempfile.gettempdir(), "certum_dv_tls_g2_r39.pem")
            if not os.path.exists(cached):
                import httpx as _hx
                r = _hx.get(
                    "http://certumdvtlsg2r39ca.repository.certum.pl/certumdvtlsg2r39ca.cer",
                    timeout=15,
                )
                if r.status_code == 200 and len(r.content) > 200:
                    with open(cached, "wb") as f:
                        f.write(r.content)
                    intermediate = cached
            else:
                intermediate = cached
        except Exception:
            intermediate = None
    if intermediate:
        try:
            with open(intermediate, "rb") as f:
                data = f.read()
            if b"-----BEGIN" not in data:  # DER → proper PEM
                b64 = base64.encodebytes(data).decode("ascii")
                pem = "-----BEGIN CERTIFICATE-----\n" + b64 + "-----END CERTIFICATE-----\n"
            else:
                pem = data.decode("ascii", "replace")
            ctx.load_verify_locations(cadata=pem)
        except Exception:
            pass
    return ctx


def _visible_text(raw: str) -> str:
    cleaned = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cleaned)).strip()


def _collect_fields(container: BeautifulSoup) -> dict[str, str]:
    fields: dict[str, str] = {}
    for el in container.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name:
            continue
        if el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            fields[name] = opt.get("value", "") if opt else ""
        elif el.get("type") in ("checkbox", "radio"):
            if el.has_attr("checked"):
                fields[name] = el.get("value", "")
        elif el.get("type") in ("submit", "button", "image", "file"):
            continue
        else:
            fields[name] = el.get("value", "")
    return fields


def _protection_context(soup: BeautifulSoup) -> dict[str, Any]:
    """APEX 23 page-items protection tokens (id-only hidden inputs)."""

    def by_id(dom_id: str) -> str:
        el = soup.find("input", {"id": dom_id})
        return el.get("value", "") if el is not None else ""

    frc = by_id("pPageFormRegionChecksums")
    return {
        "protected": by_id("pPageItemsProtected"),
        "rowVersion": by_id("pPageItemsRowVersion"),
        "formRegionChecksums": json.loads(frc) if frc else [],
        "salt": by_id("pSalt"),
    }


class RkkClient:
    """Searches official gazette notices on rrk.ir (open data, free)."""

    def __init__(self, timeout: float | None = None, verify: bool | ssl.SSLContext | None = None):
        timeout = timeout if timeout is not None else float(os.environ.get("RKK_TIMEOUT", "45"))
        if verify is None:
            verify = _build_ssl_context()
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "fa,en;q=0.9"},
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=15.0),
            verify=verify,
        )

    def close(self) -> None:
        self._http.close()

    # -- internals ---------------------------------------------------------

    def _open_search_page(self, term: str) -> tuple[httpx.Response, BeautifulSoup]:
        """Steps 1-2: hub with the term, then the checksummed page-199 render."""
        r1 = self._http.get(HUB_PATH, params={"p": f"200:103:0::NO::P103_ITEM_SEARCH:{term}"})
        m = re.search(
            r'href="(/ords/r/rrs/rrs-front/' + PAGE_199_SLUG + r'\?p199_single_search[^"]+)"',
            r1.text,
        )
        if not m:
            raise RkkError(
                "rrk.ir search hub did not render the expected open-data link; "
                "the portal layout may have changed."
            )
        r2 = self._http.get(BASE_URL + m.group(1).replace("&amp;", "&"))
        soup = BeautifulSoup(r2.text, "html.parser")
        if soup.find("form", id="wwvFlowForm") is None:
            raise RkkError("rrk.ir open-data page did not render its search form.")
        return r2, soup

    def _submit_search(self, soup: BeautifulSoup, term: str) -> str:
        """Step 3: APEX page submit; returns the post-submit render HTML."""
        form = soup.find("form", id="wwvFlowForm")
        action = urljoin(ORDS + "/", form["action"])
        fields = _collect_fields(soup)
        # The page's JS mirrors SINGLE_SEARCH into SINGLE_SEARCH_1 after load.
        if fields.get("P199_SINGLE_SEARCH") and not fields.get("P199_SINGLE_SEARCH_1"):
            fields["P199_SINGLE_SEARCH_1"] = fields["P199_SINGLE_SEARCH"]

        items = []
        for name, value in fields.items():
            if not re.match(r"^P\d+_", name):
                continue
            item: dict[str, Any] = {"n": name, "v": value}
            ck = soup.find("input", {"data-for": name})
            if ck is not None and ck.get("value"):
                item["ck"] = ck.get("value")
            items.append(item)

        prot = _protection_context(soup)
        p_json = json.dumps(
            {"pageItems": {
                "itemsToSubmit": items,
                "protected": prot["protected"],
                "rowVersion": prot["rowVersion"],
                "formRegionChecksums": prot["formRegionChecksums"],
            }, "salt": prot["salt"]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        body = {
            "p_flow_id": fields.get("p_flow_id", "200"),
            "p_flow_step_id": fields.get("p_flow_step_id", "199"),
            "p_instance": fields.get("p_instance", ""),
            "p_debug": "",
            "p_request": "SEARCH",
            "p_reload_on_submit": fields.get("p_reload_on_submit", "S"),
            "p_page_submission_id": fields.get("p_page_submission_id", ""),
            "p_json": p_json,
        }
        r = self._http.post(
            action,
            data=body,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": ACCEPT_JSON,
                "Referer": str(self._http.base_url.copy_with()).rstrip("/") + "/ords/r/rrs/rrs-front/",
                "Origin": BASE_URL,
            },
        )
        # Success: JSON {"redirectURL": ...}. Failure: JSON {"error": ...}
        if len(r.text) < 2000:
            try:
                j = r.json()
            except ValueError:
                raise RkkError(f"Unexpected response from rrk.ir submit: HTTP {r.status_code}")
            if "error" in j:
                raise RkkPageError(str(j["error"])[:300])
            if "redirectURL" in j:
                url = j["redirectURL"]
                url = url if url.startswith("http") else BASE_URL + url
                r2 = self._http.get(url)
                if "protection violation" in r2.text:
                    raise RkkPageError("rrk.ir rejected the search submission (page protection).")
                return r2.text
        return r.text

    @staticmethod
    def _extract_ig_config(html: str) -> dict[str, Any]:
        """Step 4 helper: find the notices Interactive Grid (AGAHI_MATN) config."""
        soup = BeautifulSoup(html, "html.parser")
        prot = _protection_context(soup)
        anchor = html.find('"regionStaticId":"' + "AGAHI_MATN" + '"')
        if anchor < 0:
            raise RkkError("Notices grid configuration not found on the rrk.ir render.")

        def nearest_before(pattern: str) -> str | None:
            m = None
            for cand in re.finditer(pattern, html[:anchor]):
                m = cand  # nearest match before the anchor
            return m.group(1) if m else None

        def after(pattern: str) -> str | None:
            m = re.search(pattern, html[anchor:anchor + 4000])
            return m.group(1) if m else None

        def nearest(pattern: str) -> str | None:
            """Nearest match to the anchor, either side (config blob ± window)."""
            window = html[max(0, anchor - 8000):anchor + 8000]
            best, best_dist = None, None
            for cand in re.finditer(pattern, window):
                dist = abs((anchor - max(0, anchor - 8000)) - cand.start())
                if best_dist is None or dist < best_dist:
                    best, best_dist = cand, dist
            return best.group(1) if best else None

        report_id = nearest(r'"savedReports":\[\{"id":"(\d+)"')
        region_id = nearest_before(r'"regionId":"(\d+)"')
        ajax_columns = nearest_before(r'"ajaxColumns":"([^"]+)"')
        ajax_identifier = after(r'"ajaxIdentifier":"([^"]+)"')
        if not (report_id and region_id and ajax_columns and ajax_identifier):
            raise RkkError("Notices grid configuration incomplete on the rrk.ir render.")
        # unescape \u002F style sequences coming from the embedded JSON
        ajax_identifier = ajax_identifier.encode().decode("unicode_escape")
        ajax_columns = ajax_columns.encode().decode("unicode_escape")
        return {
            "reportId": report_id,
            "regionId": region_id,
            "ajaxColumns": ajax_columns,
            "ajaxIdentifier": ajax_identifier,
            "protected": prot["protected"],
            "rowVersion": prot["rowVersion"],
            "formRegionChecksums": prot["formRegionChecksums"],
            "salt": prot["salt"],
        }

    def _fetch_rows(self, html: str, first_row: int, max_rows: int) -> dict[str, Any]:
        """Step 5: IG data request → notice rows JSON."""
        session = ""
        m = re.search(r"p_context=rrs-front/[^/]+/(\d+)", html)
        if not m:
            m = re.search(r"session=(\d+)", html)
        if m:
            session = m.group(1)
        cfg = self._extract_ig_config(html)
        p_json = json.dumps({
            "regions": [{
                "reportId": cfg["reportId"],
                "view": "grid",
                "ajaxColumns": cfg["ajaxColumns"],
                "id": cfg["regionId"],
                "ajaxIdentifier": cfg["ajaxIdentifier"],
                "fetchData": {"version": 1, "firstRow": first_row, "maxRows": max_rows},
            }],
            "pageItems": {
                "itemsToSubmit": [],
                "protected": cfg["protected"],
                "rowVersion": cfg["rowVersion"],
                "formRegionChecksums": cfg["formRegionChecksums"],
            },
            "salt": cfg["salt"],
        }, ensure_ascii=False, separators=(",", ":"))
        body = {
            "p_flow_id": "200",
            "p_flow_step_id": "199",
            "p_instance": session,
            "p_debug": "",
            "p_json": p_json,
        }
        r = self._http.post(
            f"{ORDS}/wwv_flow.ajax",
            params={"p_context": f"rrs-front/{PAGE_199_SLUG}/{session}"},
            data=body,
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": ACCEPT_JSON,
                     "Referer": f"{ORDS}/r/rrs/rrs-front/{PAGE_199_SLUG}?session={session}",
                     "Origin": BASE_URL},
        )
        try:
            return r.json()
        except ValueError as exc:
            raise RkkError(f"rrk.ir returned a non-JSON data response (HTTP {r.status_code}).") from exc

    # -- public API ---------------------------------------------------------

    def search_notices(self, query: str, max_rows: int = 20) -> dict[str, Any]:
        """Search official-gazette notices; returns structured notice rows."""
        max_rows = max(1, min(int(max_rows or 20), 100))
        _, soup = self._open_search_page(query)
        html = self._submit_search(soup, query)
        data = self._fetch_rows(html, first_row=1, max_rows=max_rows)

        regions = data.get("regions") or []
        rows: list[dict[str, Any]] = []
        fetched = regions[0].get("fetchedData", {}) if regions else {}
        for raw in fetched.get("values", []):
            row: dict[str, Any] = {}
            for idx, col in enumerate(_NOTICE_COLUMNS):
                value = raw[idx] if idx < len(raw) else None
                if col == "_meta":
                    continue
                if col == "city" and isinstance(value, dict):
                    value = value.get("d")
                row[col] = value
            # Prefer the anchor inside the title cell for the detail link.
            title_html = row.pop("title_html", "") or ""
            detail_html = row.pop("detail_html", "") or ""
            m = re.search(r'href="([^"]+)"', title_html) or re.search(r'href="([^"]+)"', detail_html)
            row["detail_url"] = None
            if m:
                href = m.group(1).replace("&amp;", "&")
                row["detail_url"] = href if href.startswith("http") else BASE_URL + href
            code = re.search(r"p28_code=(\d+)", row.get("detail_url") or "")
            row["notice_code"] = code.group(1) if code else row.get("notice_code")
            if isinstance(row.get("unknown_3"), str) and not row["unknown_3"]:
                row.pop("unknown_3", None)
            rows.append(row)
        rows = [
            {k: (re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else v)
             for k, v in r.items()}
            for r in rows
        ]
        return {
            "source": "rrk.ir (روزنامه رسمی کشور — official gazette, open data)",
            "query": query,
            "count": len(rows),
            "max_rows": max_rows,
            "notices": rows,
            "page_url": f"{ORDS}/r/rrs/rrs-front/{PAGE_199_SLUG}",
        }
