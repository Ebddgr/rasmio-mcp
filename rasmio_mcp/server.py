"""Rasmio MCP server — free access to Iranian company registry data.

Exposes four tools over MCP stdio:

- search_rasmio(query, page, page_size)
- get_company_info(national_id)
- get_company_members(national_id)
- get_company_gazettes(national_id)

No API key is needed: the server uses rasmio.com's unauthenticated web
search API and parses the public server-rendered company pages.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .client import (
    RasmioClient,
    RasmioError,
    normalize_national_id,
    normalize_query,
)
from .rkk_client import RkkClient, RkkError

mcp = FastMCP("rasmio")

_client: RasmioClient | None = None
_rkk_client: RkkClient | None = None


def _get_client() -> RasmioClient:
    global _client
    if _client is None:
        _client = RasmioClient()
    return _client


def _get_rkk_client() -> RkkClient:
    global _rkk_client
    if _rkk_client is None:
        _rkk_client = RkkClient()
    return _rkk_client


@mcp.tool()
def search_rasmio(query: str, page: int = 0, page_size: int = 10) -> dict:
    """Search Iranian companies, persons and registered brands on rasmio.com.

    Args:
        query: Company/person name or a national ID (شناسه ملی). Persian
            digits (۰-۹) and Arabic ي/ك are normalized automatically.
        page: Zero-based result page.
        page_size: Results per page (1-50, default 10).

    Returns:
        {"query", "page", "page_size", "total_count", "hits": [...]}
        where each hit has title, kind (company/person/brand/...),
        national_id (only for companies), id, summary, information and link.
        Use get_company_info with a company hit's national_id for details.
    """
    query = normalize_query(query)
    if not query:
        raise ValueError("query must not be empty")
    return _get_client().search(query, page=page, page_size=page_size)


@mcp.tool()
def get_company_info(national_id: str) -> dict:
    """Fetch the full public profile of an Iranian company by national ID.

    Args:
        national_id: 10- or 11-digit national ID (شناسه ملی), e.g.
            "14007650912" (Digikala). Persian digits are accepted.

    Returns:
        Structured metadata: title, registration_number (شماره ثبت),
        economic_code (کد اقتصادی), legal_type (نوع شرکت), status (وضعیت),
        establishment_date (تاریخ تاسیس), last_gazette_date,
        registered_capital (سرمایه ثبتی, display string + rial integer),
        activity_subject (موضوع فعالیت), registered_address (نشانی),
        postal_code (کد پستی), plus the source page_url.
    """
    return _get_client().get_company_info(national_id)


@mcp.tool()
def get_company_members(national_id: str) -> dict:
    """List the executives of an Iranian company: board members, CEO and auditors.

    Args:
        national_id: 10- or 11-digit national ID (شناسه ملی) of the company.

    Returns:
        {"members": [...]} where each member has name, roles (e.g. مدیرعامل,
        رئیس هیئت مدیره, نایب رئیس, عضو هیئت مدیره, بازرس اصلی), profile_url
        and id. Note: granular shareholder data is not published on the free
        rasmio.com page; shareholders appear only inside gazette notice texts.
    """
    return _get_client().get_company_members(national_id)


@mcp.tool()
def get_company_gazettes(national_id: str) -> dict:
    """List recent official-gazette notices (آگهی‌های روزنامه رسمی) of a company.

    Args:
        national_id: 10- or 11-digit national ID (شناسه ملی) of the company.

    Returns:
        {"gazettes": [...]} with each notice's title, print_date (تاریخ چاپ),
        letter_date (تاریخ نامه ثبت), distribution (e.g. کثیرالانتشار), body
        text and the rasmio.com newspaper URL. These notices cover capital
        changes, board changes, assembly invitations and other registry
        changes.
    """
    return _get_client().get_company_gazettes(national_id)


@mcp.tool()
def search_official_gazette(query: str, max_rows: int = 20) -> dict:
    """Search official-gazette notices (آگهی‌های روزنامه رسمی) directly on rrk.ir.

    Queries the gazette's own open-data search (روزنامه رسمی کشور) — the
    primary source rasmio.com aggregates — with no page-view quota.

    Args:
        query: Free text, a company name, or an 10/11-digit national ID
            (شناسه ملی). Persian digits and Arabic ي/ك are normalized.
        max_rows: Max notices to return (1-100, default 20).

    Returns:
        {"notices": [...]} — each notice has title_text, notice_type,
        national_id, letter_date (تاریخ نامه), newspaper_date (تاریخ چاپ),
        newspaper_no, tracking_no, status, city, notice_code and detail_url
        on rrk.ir.
    """
    query = normalize_query(query)
    if not query:
        raise ValueError("query must not be empty")
    return _get_rkk_client().search_notices(query, max_rows=max_rows)


def main() -> None:
    """Run the MCP server over stdio (the default for MCP hosts)."""
    mcp.run()


if __name__ == "__main__":
    main()
