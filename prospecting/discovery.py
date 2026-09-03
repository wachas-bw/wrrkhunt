"""Fresh candidate and LinkedIn-post discovery with source budgets and provenance."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

from .audit import audit_pending
from .config import META_PROFILE, REPO_ROOT
from .db import Database
from .util import evidence_excerpt, iso, normalize_domain, normalize_linkedin, registrable_domain, text_hash, utcnow

BLOCKED_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com", "youtu.be", "x.com",
    "twitter.com", "crunchbase.com", "tracxn.com", "wellfound.com", "glassdoor.com",
    "google.com", "bing.com", "exa.ai", "wikipedia.org",
}
MARKET_NAMES = {"IN": "India", "AE": "UAE", "SG": "Singapore", "GB": "United Kingdom", "US": "United States"}
SERVICE_VERTICALS = {
    "IN": "interior design dental clinic education consultant real estate",
    "AE": "interior fit-out clinic real estate business setup",
    "SG": "tuition centre renovation clinic professional services",
    "GB": "home services clinic estate agent education consultant",
    "US": "home services dental clinic med spa real estate",
}
SERVICE_QUERY_VERTICALS = {
    "IN": ["dental clinic", "interior design studio", "real estate consultancy",
           "education consultant", "business services firm"],
    "AE": ["interior fit-out company", "clinic setup consultancy", "real estate agency",
           "business setup consultancy", "dental clinic"],
    "SG": ["renovation contractor", "tuition centre", "dental clinic",
           "home services company", "professional services firm"],
    "GB": ["estate agency", "dental clinic", "home services company",
           "education consultancy", "business consultancy"],
    "US": ["real estate agency", "dental clinic", "med spa",
           "home services company", "business consultancy"],
}
AGENCY_QUERY_VERTICALS = ["digital marketing agency", "web design agency", "creative agency"]
STARTUP_QUERY_VARIANTS = ["raised pre-seed or seed", "announced early-stage funding"]
EXPANSION_SERVICE_QUERY_VERTICALS = {
    "IN": ["aesthetic clinic", "immigration consultant", "travel agency",
           "event management company", "accounting firm"],
    "AE": ["aesthetic clinic", "property management company", "immigration consultancy",
           "facility management company", "travel agency"],
    "SG": ["aesthetic clinic", "enrichment centre", "maid agency",
           "accounting firm", "interior design firm"],
    "GB": ["care agency", "cosmetic clinic", "property management company",
           "immigration law firm", "home renovation company"],
    "US": ["HVAC company", "plumbing company", "property management company",
           "immigration law firm", "home remodeling company"],
}
EXPANSION_AGENCY_QUERY_VERTICALS = [
    "SEO agency", "performance marketing agency", "branding agency",
]
EXPANSION_STARTUP_QUERY_VARIANTS = [
    "Y Combinator 2025 or 2026 batch", "raised Series A in 2025 or 2026",
]
FRESH_SERVICE_QUERY_VERTICALS = {
    "IN": ["physiotherapy clinic", "coworking space", "recruitment consultancy",
           "logistics company", "solar installer"],
    "AE": ["cleaning company", "maintenance company", "business centre",
           "recruitment agency", "logistics company"],
    "SG": ["corporate services firm", "recruitment agency", "cleaning company",
           "aircon service company", "physiotherapy clinic"],
    "GB": ["accountancy firm", "recruitment agency", "care home",
           "property maintenance company", "private clinic"],
    "US": ["roofing company", "landscaping company", "pest control company",
           "staffing agency", "physical therapy clinic"],
}
FRESH_AGENCY_QUERY_VERTICALS = [
    "ecommerce agency", "social media agency", "public relations agency",
]
FRESH_STARTUP_QUERY_VARIANTS = [
    "Techstars 2025 or 2026 cohort", "raised a seed round in 2026",
]
CONVERSION_SERVICE_QUERY_VERTICALS = {
    "IN": ["diagnostic centre", "architecture studio", "coaching institute",
           "salon and spa", "legal services firm"],
    "AE": ["car rental company", "home healthcare company", "accounting firm",
           "professional training institute", "relocation company"],
    "SG": ["childcare centre", "wellness clinic", "corporate training company",
           "immigration consultancy", "event company"],
    "GB": ["veterinary clinic", "mortgage broker", "architecture practice",
           "professional training provider", "commercial cleaning company"],
    "US": ["veterinary clinic", "small law firm", "accounting firm",
           "property restoration company", "solar installation company"],
}
CONVERSION_AGENCY_QUERY_VERTICALS = [
    "video production agency", "lead generation agency", "product design agency",
]
CONVERSION_STARTUP_QUERY_VARIANTS = [
    "announced venture funding in 2026", "joined an accelerator in 2026",
]
META_VERTICALS = {
    "IN": ["interior design", "dental clinic", "education consultant"],
    "AE": ["interior fit out", "clinic", "business setup"],
    "SG": ["tuition centre", "renovation", "clinic"],
}
MARKET_TERMS = {
    "IN": (" india", "bengaluru", "bangalore", "delhi", "mumbai", "pune", "hyderabad", "chennai", "gurugram", "noida"),
    "AE": (" uae", "united arab emirates", "dubai", "abu dhabi", "sharjah"),
    "SG": ("singapore",),
    "GB": ("united kingdom", " london", "manchester", "birmingham", "edinburgh"),
    "US": ("united states", " usa", "new york", "california", "texas", "seattle", "boston"),
}


@dataclass
class Candidate:
    domain: str
    company: str
    website: str
    market: str
    pool: str
    source_url: str
    excerpt: str
    vertical: str = ""
    linkedin_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    title: str
    url: str
    excerpt: str
    published: str = ""


def _walk_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _walk_text(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_text(item)


def parse_exa_output(payload: str) -> list[SearchResult]:
    """Parse mcporter JSON and Exa's stable Title/URL/Highlights text format."""
    try:
        value = json.loads(payload)
        text = "\n".join(_walk_text(value))
    except json.JSONDecodeError:
        text = payload
    blocks = re.split(r"\n\s*---\s*\n", text)
    results: list[SearchResult] = []
    seen = set()
    for block in blocks:
        title = re.search(r"(?:^|\n)Title:\s*(.+)", block)
        url = re.search(r"(?:^|\n)URL:\s*(https?://\S+)", block)
        if not url:
            continue
        clean_url = url.group(1).strip().rstrip(".,)")
        if clean_url in seen:
            continue
        seen.add(clean_url)
        published = re.search(r"(?:^|\n)Published:\s*(.+)", block)
        highlights = block.split("Highlights:", 1)[-1]
        highlights = re.sub(r"\n\.\.\.\s*", " ", highlights)
        results.append(SearchResult(
            title=title.group(1).strip() if title else normalize_domain(clean_url),
            url=clean_url,
            excerpt=evidence_excerpt(highlights, 700),
            published=(published.group(1).strip() if published else ""),
        ))
    return results


def resolve_mcporter_binary() -> str:
    """Resolve mcporter for launchd as well as an interactive NVM shell."""
    configured = os.environ.get("WRRKHUNT_MCPORTER_BIN", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise RuntimeError(f"WRRKHUNT_MCPORTER_BIN is not executable: {path}")
    found = shutil.which("mcporter")
    if found:
        return str(Path(found).resolve())
    nvm_root = Path(os.environ.get("NVM_DIR", str(Path.home() / ".nvm"))).expanduser()
    candidates = sorted(
        nvm_root.glob("versions/node/*/bin/mcporter"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    raise RuntimeError(
        "mcporter was not found; set WRRKHUNT_MCPORTER_BIN or install it in PATH/NVM"
    )


def resolve_mcporter_config() -> Path:
    """Resolve a self-contained Exa configuration, with legacy-workspace fallback."""
    configured = os.environ.get("WRRKHUNT_MCPORTER_CONFIG", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path.resolve()
        raise RuntimeError(f"WRRKHUNT_MCPORTER_CONFIG was not found: {path}")

    candidates = (
        REPO_ROOT / "config" / "mcporter.json",
        # Backward compatibility for the original jhunt/wrrkhunt checkout.
        REPO_ROOT.parent / "config" / "mcporter.json",
    )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise RuntimeError(
        "Exa mcporter configuration was not found; restore config/mcporter.json "
        "or set WRRKHUNT_MCPORTER_CONFIG"
    )


def exa_search(query: str, num_results: int = 8, timeout: int = 75) -> list[SearchResult]:
    mcporter_config = resolve_mcporter_config()
    command = [
        resolve_mcporter_binary(), "--config", str(mcporter_config), "call", "exa.web_search_exa",
        "--args", json.dumps({"query": query, "numResults": num_results}),
        "--output", "json", "--timeout", str(timeout * 1000),
    ]
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True,
                            timeout=timeout + 10, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:800])
    return parse_exa_output(result.stdout)


def _candidate_from_result(result: SearchResult, market: str, pool: str,
                           query: str) -> Candidate | None:
    host = normalize_domain(result.url)
    root = registrable_domain(host)
    if not root or root in BLOCKED_HOSTS or any(root.endswith("." + x) for x in BLOCKED_HOSTS):
        return None
    if not result.excerpt:
        return None
    company = re.split(r"\s+[|—-]\s+", result.title, maxsplit=1)[0].strip()
    return Candidate(
        domain=root, company=company[:160] or root, website=result.url, market=market,
        pool=pool, source_url=result.url, excerpt=result.excerpt,
        metadata={"query": query, "published": result.published},
    )


def exa_queries(markets: list[str], *, expansion: bool = False,
                fresh_wave: bool = False,
                conversion_wave: bool = False) -> list[tuple[str, str, str, int]]:
    """Return market, pool, query in the requested 50/30/20 source mix."""
    if sum(bool(value) for value in (expansion, fresh_wave, conversion_wave)) > 1:
        raise ValueError("choose one Exa query wave")
    queries = []
    if conversion_wave:
        service_verticals = CONVERSION_SERVICE_QUERY_VERTICALS
        agency_verticals = CONVERSION_AGENCY_QUERY_VERTICALS
        startup_variants = CONVERSION_STARTUP_QUERY_VARIANTS
    elif fresh_wave:
        service_verticals = FRESH_SERVICE_QUERY_VERTICALS
        agency_verticals = FRESH_AGENCY_QUERY_VERTICALS
        startup_variants = FRESH_STARTUP_QUERY_VARIANTS
    elif expansion:
        service_verticals = EXPANSION_SERVICE_QUERY_VERTICALS
        agency_verticals = EXPANSION_AGENCY_QUERY_VERTICALS
        startup_variants = EXPANSION_STARTUP_QUERY_VARIANTS
    else:
        service_verticals = SERVICE_QUERY_VERTICALS
        agency_verticals = AGENCY_QUERY_VERTICALS
        startup_variants = STARTUP_QUERY_VARIANTS
    for market in markets:
        country = MARKET_NAMES[market]
        # Ten query units per market preserve the campaign's 50/30/20 source mix.
        # Granular verticals produce materially fresher result sets than one broad
        # parenthesized query, while every result is still audited on its own site.
        for vertical in service_verticals[market]:
            queries.append((
                market,
                "service_smb",
                f'{country} {vertical} "WhatsApp" contact email official website -directory',
                10,
            ))
        for vertical in agency_verticals:
            queries.append((
                market,
                "agency_directory",
                f'{country} {vertical} 10 70 employees official website contact email',
                10,
            ))
        for funding_phrase in startup_variants:
            queries.append((
                market,
                "funded_startup",
                f'{country} startup {funding_phrase} 2025 2026 official website contact',
                8,
            ))
    return queries


def ingest_candidates(db: Database, campaign_id: int, run_id: int,
                      candidates: Iterable[Candidate]) -> tuple[int, int]:
    inserted = 0
    seen = 0
    for candidate in candidates:
        seen += 1
        root = registrable_domain(candidate.domain)
        # Canonical deduplication spans campaigns, including the held legacy backlog.
        if db.row("SELECT 1 FROM prospects WHERE registrable_domain=? LIMIT 1", (root,)):
            continue
        if db.is_suppressed("domain", root):
            continue
        try:
            prospect_id, created = db.upsert_prospect(
                campaign_id, domain=root, company=candidate.company, market=candidate.market,
                pool=candidate.pool, vertical=candidate.vertical, website=candidate.website,
                linkedin_url=candidate.linkedin_url, metadata=candidate.metadata,
            )
        except Exception:
            continue
        if not created:
            continue
        inserted += 1
        db.add_evidence(
            prospect_id, "discovery", candidate.source_url, candidate.excerpt, "medium",
            candidate.pool, source_run_id=run_id, metadata=candidate.metadata,
        )
    return seen, inserted


def discover_exa(db: Database, campaign_id: int, markets: list[str],
                 target: int = 60, *, expansion: bool = False,
                 fresh_wave: bool = False,
                 conversion_wave: bool = False) -> dict[str, int]:
    total_seen = total_inserted = 0
    for market, pool, query, num_results in exa_queries(
            markets, expansion=expansion, fresh_wave=fresh_wave,
            conversion_wave=conversion_wave):
        run_id = db.create_source_run(campaign_id, "exa", query, market, pool)
        try:
            results = exa_search(query, num_results=num_results)
            candidates = [c for r in results if (c := _candidate_from_result(r, market, pool, query))]
            seen, inserted = ingest_candidates(db, campaign_id, run_id, candidates)
            total_seen += seen
            total_inserted += inserted
            db.finish_source_run(run_id, status="completed", candidates=inserted,
                                 budget_note="Exa via existing mcporter configuration")
        except Exception as exc:
            db.finish_source_run(run_id, status="failed", error=str(exc)[:1000])
    return {"seen": total_seen, "inserted": total_inserted}


def _external_url(href: str) -> str:
    if not href:
        return ""
    if "l.facebook.com/l.php" in href:
        return parse_qs(urlsplit(href).query).get("u", [""])[0]
    return href if href.startswith("http") else ""


class MetaNetworkUnavailable(RuntimeError):
    """The browser could not reach Meta after bounded retries."""


def _goto_meta_with_retry(page: Any, url: str, attempts: int = 3) -> None:
    """Retry only transient browser-network failures; all other failures fail closed."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            return
        except Exception as exc:
            detail = str(exc)
            if not re.search(r"ERR_(?:INTERNET_DISCONNECTED|NETWORK_CHANGED)", detail, re.I):
                raise
            last_error = exc
            if attempt < attempts:
                page.wait_for_timeout(attempt * 5_000)
    raise MetaNetworkUnavailable(
        f"Meta browser network unavailable after {attempts} attempts: {last_error}"
    ) from last_error


def discover_meta(db: Database, campaign_id: int, markets: list[str],
                  max_per_query: int = 10, headed: bool = False) -> dict[str, int]:
    """Harvest public Meta Ad Library cards in a real browser; challenges fail closed."""
    eligible = [m for m in markets if m in {"IN", "AE", "SG"}]
    if not eligible:
        return {"seen": 0, "inserted": 0}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed; run automation setup") from exc
    total_seen = total_inserted = 0
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(META_PROFILE), headless=not headed, viewport={"width": 1400, "height": 1000},
        )
        try:
            context.set_offline(False)
            page = context.pages[0] if context.pages else context.new_page()
            for market in eligible:
                for vertical in META_VERTICALS[market]:
                    query = f'"whatsapp" {vertical}'
                    run_id = db.create_source_run(
                        campaign_id, "meta_ad_library", query, market, "service_smb")
                    url = "https://www.facebook.com/ads/library/?" + urllib.parse.urlencode({
                        "active_status": "active", "ad_type": "all", "country": market,
                        "q": query, "search_type": "keyword_unordered", "media_type": "all",
                    })
                    try:
                        _goto_meta_with_retry(page, url)
                        page.wait_for_timeout(5_000)
                        page_text = page.locator("body").inner_text(timeout=10_000)
                        if re.search(r"captcha|security check|temporarily blocked|log in to continue", page_text, re.I):
                            raise RuntimeError("Meta login/challenge detected")
                        candidates = []
                        for anchor in page.locator("a[href]").all():
                            href = _external_url(anchor.get_attribute("href") or "")
                            domain = registrable_domain(href)
                            if not domain or domain in BLOCKED_HOSTS:
                                continue
                            excerpt = evidence_excerpt(anchor.inner_text() or page_text[:500])
                            candidates.append(Candidate(
                                domain, anchor.inner_text().strip() or domain, href, market,
                                "service_smb", url, excerpt, vertical=vertical,
                                metadata={"query": query},
                            ))
                            if len(candidates) >= max_per_query:
                                break
                        seen, inserted = ingest_candidates(db, campaign_id, run_id, candidates)
                        total_seen += seen
                        total_inserted += inserted
                        db.finish_source_run(run_id, status="completed", candidates=inserted,
                                             budget_note="Public Meta Ad Library; no paid credits")
                    except MetaNetworkUnavailable as exc:
                        db.finish_source_run(run_id, status="failed", error=str(exc)[:1000])
                        raise
                    except Exception as exc:
                        db.finish_source_run(run_id, status="failed", error=str(exc)[:1000])
        finally:
            context.close()
    return {"seen": total_seen, "inserted": total_inserted}


def _apify_token() -> str:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    candidates = [REPO_ROOT.parent / "jobhunt" / ".apify_token", REPO_ROOT / ".apify_token"]
    for path in candidates:
        if not token and path.exists():
            token = path.read_text().strip()
    return token


def _apify_remaining(db: Database) -> float:
    budget = float(db.setting("apify_monthly_budget_usd", 5.0))
    month = iso()[:7]
    row = db.row("SELECT COALESCE(SUM(cost_usd),0) AS spent FROM source_runs "
                 "WHERE source='apify_linkedin' AND substr(started_at,1,7)=?", (month,))
    return max(0.0, budget - float(row["spent"] if row else 0))


def parse_apify_date(value: Any):
    from datetime import UTC, datetime
    if isinstance(value, dict):
        if value.get("date"):
            value = value["date"]
        elif value.get("timestamp"):
            return datetime.fromtimestamp(float(value["timestamp"]) / 1000, tz=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC)


def infer_market(*values: str) -> str:
    text = " " + " ".join(str(value or "").lower() for value in values) + " "
    for market, terms in MARKET_TERMS.items():
        if any(re.search(r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", text)
               for term in terms):
            return market
    for value in values:
        domain = normalize_domain(str(value or ""))
        if domain.endswith(".in"):
            return "IN"
        if domain.endswith(".ae"):
            return "AE"
        if domain.endswith(".sg"):
            return "SG"
        if domain.endswith(".uk"):
            return "GB"
    return ""


def infer_post_market(post_text: str) -> str:
    """Infer a post's operating market only from an explicit location statement."""
    cues = re.compile(
        r"(?:\blocation\b|\bbased in\b|\bheadquartered\b|\boffice in\b|\bonsite\b|"
        r"\bjoin our team in\b|\bestablished (?:a |the )?company\b|\bcompany in\b|📍)",
        re.I,
    )
    for segment in re.split(r"[\n.!?]+", str(post_text or "")):
        if cues.search(segment):
            market = infer_market(segment)
            if market:
                return market
    return ""


def discover_apify_posts(db: Database, campaign_id: int) -> dict[str, int | float]:
    token = _apify_token()
    if not token:
        return {"seen": 0, "inserted": 0, "cost_usd": 0.0}
    cfg = json.loads((REPO_ROOT / "sources" / "intent_config.json").read_text())
    actor = cfg["apify"]["actor"].replace("/", "~")
    queries = (cfg["linkedin_queries_ask"][:3] + cfg["linkedin_queries_early"][:2])
    max_posts = min(10, int(cfg["apify"].get("max_posts_per_query", 20)))
    estimate = len(queries) * max_posts * 1.5 / 1000
    per_run = float(db.setting("apify_run_budget_usd", 0.50))
    if estimate > per_run or estimate > _apify_remaining(db):
        return {"seen": 0, "inserted": 0, "cost_usd": 0.0}
    run_id = db.create_source_run(campaign_id, "apify_linkedin", " | ".join(queries), "", "posts")
    url = (f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?"
           + urllib.parse.urlencode({"token": token, "timeout": 300}))
    payload = {
        "searchQueries": queries, "maxPosts": max_posts, "postedLimit": "week",
        "sortBy": "date", "scrapePages": 1, "scrapeComments": False,
    }
    try:
        request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=340) as response:
            items = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 402:
            db.finish_source_run(run_id, status="budget_exhausted", cost_usd=0,
                                 error="Apify returned 402; free credit exhausted")
            return {"seen": 0, "inserted": 0, "cost_usd": 0.0}
        db.finish_source_run(run_id, status="failed", error=f"HTTP {exc.code}")
        return {"seen": 0, "inserted": 0, "cost_usd": 0.0}
    inserted = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        post_url = normalize_linkedin(item.get("linkedinUrl") or item.get("url") or "")
        raw_post_text = item.get("content") or item.get("text") or ""
        post_text = raw_post_text.strip() if isinstance(raw_post_text, str) else ""
        if not post_url or not post_text:
            continue
        published = item.get("postedAt") or item.get("publishedAt") or item.get("date")
        try:
            parsed_published = parse_apify_date(published)
            if parsed_published < utcnow() - timedelta(hours=int(db.setting("post_max_age_hours", 48))):
                continue
            published = parsed_published.isoformat()
        except (TypeError, ValueError):
            continue
        author_url = normalize_linkedin(author.get("linkedinUrl") or author.get("url") or "")
        headline = str(author.get("headline") or author.get("info") or "")
        location = str(author.get("location") or item.get("location") or "")
        company_website = (author.get("companyWebsite") or author.get("website") or
                           item.get("companyWebsite") or item.get("website") or "")
        market = infer_market(location, headline, company_website) or infer_post_market(post_text)
        role = "influencer" if any(x in headline.lower()
                                   for x in ("investor", "advisor", "creator", "mentor")) else "prospect"
        prospect_id = None
        if role == "prospect":
            company_domain = registrable_domain(company_website)
            raw_company_name = (author.get("companyName") or author.get("company") or
                                item.get("companyName") or "")
            company_name = raw_company_name.strip() if isinstance(raw_company_name, str) else ""
            if not company_name:
                company_match = re.search(r"(?:founder|co-?founder|ceo)\s*@\s*([^|,]+)", headline, re.I)
                company_name = company_match.group(1).strip() if company_match else ""
            prospect = None
            if company_domain:
                prospect = db.row(
                    "SELECT p.* FROM prospects p JOIN campaigns c ON c.id=p.campaign_id "
                    "WHERE c.name='fresh' AND p.registrable_domain=?", (company_domain,))
            if not prospect and company_name:
                prospect = db.row(
                    "SELECT p.* FROM prospects p JOIN campaigns c ON c.id=p.campaign_id "
                    "WHERE c.name='fresh' AND lower(p.company)=lower(?) LIMIT 1", (company_name,))
            if not prospect and company_domain and market and not db.row(
                    "SELECT 1 FROM prospects WHERE registrable_domain=?", (company_domain,)):
                prospect_id, created = db.upsert_prospect(
                    campaign_id, domain=company_domain, company=company_name or company_domain,
                    market=market, pool="funded_startup", website=company_website,
                    linkedin_url=author_url,
                    metadata={"apify_post": post_url, "headline": headline},
                )
                if created:
                    db.add_evidence(prospect_id, "founder_post", post_url,
                                    evidence_excerpt(post_text, 700), "medium", "fresh founder post",
                                    source_run_id=run_id, detected_at=published)
            elif prospect:
                prospect_id = int(prospect["id"])
        try:
            with db.transaction(immediate=True) as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO posts(prospect_id,source_run_id,author_name,author_url,post_url,text,text_hash,"
                    "published_at,role,status,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (prospect_id, run_id, author.get("name") or author.get("fullName") or "", author_url,
                     post_url, post_text, text_hash(post_text), published, role, "discovered",
                     json.dumps({"headline": headline, "location": location,
                                 "company_website": company_website}), iso()),
                )
                if cur.rowcount > 0:
                    conn.execute("UPDATE posts SET market=? WHERE id=?", (market, cur.lastrowid))
                inserted += int(cur.rowcount > 0)
        except Exception:
            continue
    db.finish_source_run(run_id, status="completed", candidates=inserted, cost_usd=estimate,
                         budget_note=f"local hard cap; remaining before run ${_apify_remaining(db):.2f}")
    return {"seen": len(items), "inserted": inserted, "cost_usd": estimate}


def discover_exa_posts(db: Database, campaign_id: int, markets: list[str]) -> dict[str, int]:
    inserted = seen = 0
    cutoff = utcnow() - timedelta(hours=int(db.setting("post_max_age_hours", 48)))
    after = cutoff.date().isoformat()
    searches = []
    for market in markets:
        country = MARKET_NAMES[market]
        searches.extend([
            (market, "prospect", f"site:linkedin.com/posts {country} founder small team CRM WhatsApp operations after:{after}"),
            (market, "influencer", f"site:linkedin.com/posts {country} startup operations small business customer experience after:{after}"),
        ])
    for market, role, query in searches:
        run_id = db.create_source_run(campaign_id, "exa_linkedin_posts", query, "", role)
        run_inserted = 0
        try:
            results = exa_search(query, num_results=10)
            for result in results:
                seen += 1
                if "linkedin.com/posts/" not in result.url and "linkedin.com/feed/update/" not in result.url:
                    continue
                # Unknown dates are rejected: freshness must be provable.
                try:
                    published = result.published.replace("Z", "+00:00")
                    from datetime import datetime
                    parsed = datetime.fromisoformat(published)
                    if parsed.tzinfo is None:
                        from datetime import UTC
                        parsed = parsed.replace(tzinfo=UTC)
                    if parsed < cutoff:
                        continue
                except (TypeError, ValueError):
                    continue
                post_url = normalize_linkedin(result.url)
                with db.transaction(immediate=True) as conn:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO posts(source_run_id,author_name,post_url,text,text_hash,published_at,"
                        "role,status,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (run_id, result.title[:160], post_url, result.excerpt, text_hash(result.excerpt),
                         parsed.isoformat(), role, "discovered", json.dumps({"market": market}), iso()),
                    )
                    if cur.rowcount > 0:
                        conn.execute("UPDATE posts SET market=? WHERE id=?", (market, cur.lastrowid))
                    created = int(cur.rowcount > 0)
                    inserted += created
                    run_inserted += created
            db.finish_source_run(run_id, status="completed", candidates=run_inserted,
                                 budget_note="Exa post discovery; freshness required")
        except Exception as exc:
            db.finish_source_run(run_id, status="failed", error=str(exc)[:1000])
    return {"seen": seen, "inserted": inserted}


def discover(db: Database, markets: list[str], target: int = 60, *, use_exa: bool = True,
             use_meta: bool = True, use_apify: bool = True, audit: bool = True,
             expansion: bool = False, fresh_wave: bool = False,
             conversion_wave: bool = False) -> dict[str, Any]:
    db.initialize()
    markets = [m.upper() for m in markets]
    unknown = sorted(set(markets) - set(MARKET_NAMES))
    if unknown:
        raise ValueError(f"unsupported markets: {', '.join(unknown)}")
    campaign_id = db.ensure_campaign("fresh", "fresh", "active")
    result: dict[str, Any] = {"markets": markets, "target": target}
    linkedin_manual = db.setting("linkedin_post_discovery_mode", "manual") == "manual"
    if use_exa:
        result["exa"] = discover_exa(
            db, campaign_id, markets, target, expansion=expansion, fresh_wave=fresh_wave,
            conversion_wave=conversion_wave,
        )
        result["exa"]["query_variant"] = (
            "conversion_wave" if conversion_wave else (
                "fresh_wave" if fresh_wave else "expansion" if expansion else "default"
            )
        )
        if linkedin_manual:
            result["exa_posts"] = {
                "seen": 0, "inserted": 0,
                "disabled": "LinkedIn post discovery is manual-browser only",
            }
        else:
            result["exa_posts"] = discover_exa_posts(db, campaign_id, markets)
    if use_meta:
        try:
            result["meta"] = discover_meta(db, campaign_id, markets)
        except Exception as exc:
            result["meta"] = {"seen": 0, "inserted": 0, "error": str(exc)}
    if use_apify:
        if linkedin_manual:
            result["apify"] = {
                "seen": 0, "inserted": 0, "cost_usd": 0.0,
                "disabled": "third-party LinkedIn scraping is disabled in safe mode",
            }
        else:
            try:
                result["apify"] = discover_apify_posts(db, campaign_id)
            except Exception as exc:
                active = db.row(
                    "SELECT id FROM source_runs WHERE campaign_id=? AND source='apify_linkedin' "
                    "AND status='running' ORDER BY id DESC LIMIT 1", (campaign_id,))
                if active:
                    cfg = json.loads((REPO_ROOT / "sources" / "intent_config.json").read_text())
                    max_posts = min(10, int(cfg["apify"].get("max_posts_per_query", 20)))
                    estimate = 5 * max_posts * 1.5 / 1000
                    db.finish_source_run(active["id"], status="failed", cost_usd=estimate,
                                         error=str(exc)[:1000])
                result["apify"] = {"seen": 0, "inserted": 0, "error": str(exc)[:1000]}
    if audit:
        audited = audit_pending(db, "fresh", target)
        result["audited"] = len(audited)
        result["qualified"] = sum(x.get("status") == "qualified" for x in audited)
        result["audit_results"] = audited
    return result
