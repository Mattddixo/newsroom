"""Funding adapters. Each turns one public identifier into funding records.

- SEC EDGAR (public companies, by CIK): links to the latest annual report and the
  filing index. No amounts: a company's revenue is not "funding", and summarising
  a filing would be interpretation.
- ProPublica Nonprofit Explorer (US nonprofits, by EIN): total revenue and
  contributions/grants received, per tax year, from Form 990 data.
- CRA (Canadian registered charities, by business number): a link to the charity's
  public listing, which carries its T3010 financial returns. CRA has no stable API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from newsroom.net.http import ApiClient, ApiError

SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANY = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=&dateb=&owner=include&count=40"
SEC_DOCUMENT = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
ANNUAL_FORMS = ("10-K", "20-F", "40-F")

PROPUBLICA_API = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
PROPUBLICA_PAGE = "https://projects.propublica.org/nonprofits/organizations/{ein}"
PROPUBLICA_YEARS = 3

CRA_LISTING = (
    "https://apps.cra-arc.gc.ca/ebci/hacc/srch/pub/dsplyRprtngPrd?selectedCharityBn={bn}&dsrdPg=1"
)

CIK_RE = re.compile(r"^\d{1,10}$")
EIN_RE = re.compile(r"^\d{2}-?\d{7}$")
BN_RE = re.compile(r"^\d{9}RR\d{4}$")


@dataclass(frozen=True)
class FundingRecord:
    kind: str
    label: str
    source: str
    source_url: str
    amount: float | None = None
    currency: str | None = None
    period: str | None = None
    funder: str | None = None


def normalize_id(scheme: str, value: str) -> str:
    """Validate and normalise an identifier; raises ValueError."""
    value = value.strip().upper().replace(" ", "")
    if scheme == "sec_cik" and CIK_RE.match(value):
        return value.zfill(10)
    if scheme == "us_ein" and EIN_RE.match(value):
        digits = value.replace("-", "")
        return f"{digits[:2]}-{digits[2:]}"
    if scheme == "ca_bn" and BN_RE.match(value):
        return value
    raise ValueError(f"invalid {scheme}: {value!r}")


def registry_url(scheme: str, value: str) -> str:
    """The public page for an identifier (used as the source of manually set IDs)."""
    value = normalize_id(scheme, value)
    if scheme == "sec_cik":
        return SEC_COMPANY.format(cik=value)
    if scheme == "us_ein":
        return PROPUBLICA_PAGE.format(ein=value.replace("-", ""))
    return CRA_LISTING.format(bn=value)


# ---------------------------------------------------------------- SEC EDGAR


def parse_sec_submissions(data: dict, cik: str) -> list[FundingRecord]:
    cik = normalize_id("sec_cik", cik)
    records = [
        FundingRecord(
            kind="public_filing",
            label="SEC filings (all)",
            source="sec_edgar",
            source_url=SEC_COMPANY.format(cik=cik),
        )
    ]
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    for i, form in enumerate(forms):
        if form not in ANNUAL_FORMS:
            continue
        try:
            accession = recent["accessionNumber"][i]
            document = recent["primaryDocument"][i]
            filed = recent["filingDate"][i]
            period = (recent.get("reportDate") or [""] * len(forms))[i] or filed
        except (KeyError, IndexError):
            break
        if not re.fullmatch(r"[\d-]+", accession) or not re.fullmatch(r"[\w.\-]+", document):
            break
        records.insert(
            0,
            FundingRecord(
                kind="public_filing",
                label=f"Latest annual report (Form {form}), filed {filed}",
                source="sec_edgar",
                source_url=SEC_DOCUMENT.format(
                    cik=int(cik), accession=accession.replace("-", ""), document=document
                ),
                period=period,
            ),
        )
        break  # "recent" is newest first
    return records


def fetch_sec(client: ApiClient, cik: str) -> list[FundingRecord]:
    cik = normalize_id("sec_cik", cik)
    response = client.get(SEC_SUBMISSIONS.format(cik=cik))
    try:
        data = response.json()
    except ValueError as exc:
        raise ApiError("SEC returned non-JSON") from exc
    return parse_sec_submissions(data if isinstance(data, dict) else {}, cik)


# ---------------------------------------------------------------- ProPublica


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value >= 0 else None


def parse_propublica(data: dict, ein: str) -> list[FundingRecord]:
    ein = normalize_id("us_ein", ein)
    page = PROPUBLICA_PAGE.format(ein=ein.replace("-", ""))
    filings = [f for f in data.get("filings_with_data") or [] if isinstance(f, dict)]
    filings = [f for f in filings if isinstance(f.get("tax_prd_yr"), int)]
    filings.sort(key=lambda f: f["tax_prd_yr"], reverse=True)
    records: list[FundingRecord] = []
    for f in filings[:PROPUBLICA_YEARS]:
        period = f"Tax year {f['tax_prd_yr']}"
        revenue = _amount(f.get("totrevenue"))
        if revenue is not None:
            records.append(
                FundingRecord(
                    "nonprofit_revenue",
                    "Total revenue (IRS Form 990)",
                    "propublica",
                    page,
                    revenue,
                    "USD",
                    period,
                )
            )
        grants = _amount(f.get("totcntrbgftgrnt"))
        if grants is not None:
            records.append(
                FundingRecord(
                    "grant",
                    "Contributions, gifts and grants received (Form 990)",
                    "propublica",
                    page,
                    grants,
                    "USD",
                    period,
                )
            )
    return records


def fetch_propublica(client: ApiClient, ein: str) -> list[FundingRecord]:
    ein = normalize_id("us_ein", ein)
    try:
        response = client.get(PROPUBLICA_API.format(ein=ein.replace("-", "")))
    except ApiError as exc:
        if "HTTP 404" in str(exc):
            return []  # not in Nonprofit Explorer
        raise
    try:
        data = response.json()
    except ValueError as exc:
        raise ApiError("ProPublica returned non-JSON") from exc
    return parse_propublica(data if isinstance(data, dict) else {}, ein)


# ---------------------------------------------------------------- CRA


def cra_records(bn: str) -> list[FundingRecord]:
    bn = normalize_id("ca_bn", bn)
    return [
        FundingRecord(
            kind="charity_registration",
            label=f"Registered charity {bn}: CRA listing and T3010 returns",
            source="cra",
            source_url=CRA_LISTING.format(bn=bn),
        )
    ]
