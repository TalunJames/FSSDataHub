"""Bounded, robots-aware crawl of official tax sources.

Start from the catalog (state DOR, recorded URLs). If that is not enough,
search the web for the jurisdiction's own site and tax PDFs, open those
pages, follow tax-looking links, and archive every byte — including PDFs —
before anyone extracts a number.

This is still not an open-web vacuum: results must look like that county or
city (or a .gov/.us host). Social and tracker hosts are blocked.
"""

import hashlib
import io
import re
from collections import deque
from urllib.parse import parse_qs, urljoin, urlparse, unquote
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from taxdb import archive, db
from taxdb.sources import STATE_AGENCIES
from . import store

RELEVANT = re.compile(
    r"tax|levy|levies|millage|ordinance|lodging|hotel|motel|occupancy|"
    r"sales.?tax|property.?tax|excise|assessment|revenue|franchise|"
    r"transient|meals|rate.?table|local.?option|withholding|earnings.?tax|"
    r"statute|municipal.?code|charter|"
    # Election documents: where ballot_measure data actually lives.
    r"ballot|measure|canvass|election|proposition|referend|abstract.?of.?votes|"
    r"official.?results|certif",
    re.I,
)

# Election-results pages are often titled with none of the tax words above,
# so the elections pass gets its own follow test.
ELECTION_RELEVANT = re.compile(
    r"ballot|measure|canvass|election|proposition|referend|levy|bond|"
    r"abstract|official.?results|certif|precinct|sample.?ballot|issue",
    re.I,
)

SKIP_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".woff", ".woff2", ".ttf", ".mp4", ".mp3", ".zip", ".exe", ".dmg",
}

SKIP_HOST_PARTS = (
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "youtube.com",
    "instagram.com", "tiktok.com", "doubleclick.net", "google.com",
    "googletagmanager.com", "apple.com", "bing.com", "microsoft.com",
    "duckduckgo.com", "wikipedia.org", "wikiwand.com", "yelp.com",
    "yellowpages.com",
)

KIND_WORD = {
    "county": "county",
    "place": "city",
    "mcd": "township",
    "state": "state",
    "school": "school district",
}

GOV_HOST = re.compile(r"(^|\.)(gov|us|mil)$", re.I)

_robots_cache = {}


class FetchError(Exception):
    pass


def client_for(settings):
    timeout = httpx.Timeout(45.0, connect=15.0)
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": settings.get("user_agent") or store.DEFAULTS["user_agent"]},
        verify=True,
    )


def seeds_for(conn, geoid, state_usps, settings, category=None):
    """Official starting URLs for one jurisdiction."""
    j = conn.execute("SELECT * FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    urls = []
    seen = set()

    def add(url):
        if not url:
            return
        url = url.strip()
        if url not in seen:
            seen.add(url)
            urls.append(url)

    if category == "framework":
        # Statute text and the agency of record, not this year's rate table.
        row = conn.execute(
            "SELECT statute_root_url, revenue_agency_url FROM state_profile "
            "WHERE state_usps=?", ((state_usps or "").upper(),)).fetchone()
        if row:
            add(row["statute_root_url"])
            add(row["revenue_agency_url"])

    if j:
        for row in conn.execute(
                "SELECT url FROM source WHERE scope_geoid IN (?,?,?) "
                "ORDER BY authority_tier",
                (geoid, j["state_fips"], j["county_fips"] or "")):
            add(row["url"])

    agency = STATE_AGENCIES.get((state_usps or "").upper())
    if agency:
        add(agency[1])

    return urls


def search_queries(name, state, category, kind=None):
    """Queries that find the jurisdiction site, then the document on it.

    Query wording is category-specific because the three passes look for
    different documents: rate tables, statute chapters, and canvasses are not
    found by the same words.
    """
    if category == "framework":
        return [
            "%s state code local sales tax authority maximum rate" % state,
            "%s statute two-thirds vote local tax measure" % state,
            "%s constitution property tax levy limit local government" % state,
            "%s secretary of state local ballot measure requirements" % state,
            "%s code lodging tax county municipality maximum" % state,
            "%s legislature revenue title local option taxes" % state,
        ]
    if category == "elections":
        return [
            '"%s" %s board of elections official results canvass' % (name, state),
            '"%s" %s election results levy bond measure' % (name, state),
            '"%s" %s abstract of votes filetype:pdf' % (name, state),
            '"%s" %s county clerk elections past results' % (name, state),
            '"%s" %s ballot measure results site:.gov' % (name, state),
        ]
    cat = (category or "tax").replace("_", " ")
    kind_word = KIND_WORD.get(kind or "", "government")
    return [
        '"%s" %s %s official website' % (name, state, kind_word),
        "%s %s %s tax rate" % (name, state, cat),
        '"%s" %s %s tax filetype:pdf' % (name, state, cat),
        "%s %s treasurer auditor %s" % (name, state, cat),
        "%s %s %s tax site:.gov" % (name, state, cat),
    ]


def search_gov(client, name, state, category, kind=None, limit=12, settings=None,
               diag=None):
    """Web search for official pages and PDFs."""
    return search_web(client, name, state, category, kind=kind, limit=limit,
                      settings=settings, diag=diag)


def new_diag():
    """Counters for one item's searching, so a blocked engine is visible.

    Without this, 'searched and found nothing' and 'search engine refused to
    answer' produce the same empty page list, and a throttled night looks the
    same as a thorough one.
    """
    return {"queries": 0, "answered": 0, "blocked": 0, "hits": 0,
            "kept": 0, "provider": None}


def search_web(client, name, state, category, kind=None, limit=12, settings=None,
               diag=None):
    """Find official pages and documents. Failures are counted, not silent."""
    settings = settings or {}
    diag = diag if diag is not None else new_diag()
    api_key = (settings.get("search_api_key") or "").strip()
    provider = (settings.get("search_provider") or "auto").strip().lower()
    if provider == "auto":
        provider = "brave" if api_key else "scrape"
    diag["provider"] = provider

    out = []
    seen = set()
    for q in search_queries(name, state, category, kind):
        diag["queries"] += 1
        engines = []
        if provider == "brave" and api_key:
            engines.append(lambda: _brave_search(client, api_key, q, limit=8))
        engines.append(lambda: _ddg_search(client, q, limit=8))
        engines.append(lambda: _bing_search(client, q, limit=8))

        hits, refusals = [], 0
        for engine in engines:
            got, refused = engine()
            refusals += 1 if refused else 0
            if got:
                hits = got
                break
        # Only a query that every engine refused counts as blocked. One engine
        # answering with a genuine zero is a real (if unhelpful) answer.
        blocked = not hits and refusals == len(engines)
        if hits:
            diag["answered"] += 1
        elif blocked:
            diag["blocked"] += 1
        diag["hits"] += len(hits)
        for url in hits:
            if url in seen:
                continue
            host = urlparse(url).hostname or ""
            if not looks_official_result(host, url, name, state):
                continue
            seen.add(url)
            out.append(url)
            diag["kept"] += 1
            if len(out) >= limit:
                return out
    return out


def search_note(diag):
    """One line for the run log, or None when searching went fine."""
    if not diag or not diag.get("queries"):
        return None
    if diag["blocked"] and not diag["answered"]:
        return ("web search blocked: %d of %d queries refused by %s — findings "
                "for this item were made without search"
                % (diag["blocked"], diag["queries"], diag.get("provider") or "search"))
    if diag["blocked"]:
        return "web search partly blocked: %d of %d queries refused" % (
            diag["blocked"], diag["queries"])
    if not diag["hits"]:
        return "web search returned no results for %d queries" % diag["queries"]
    return None


def _brave_search(client, api_key, query, limit=8):
    """Brave Search API. Returns (urls, blocked)."""
    try:
        r = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": limit},
            headers={"Accept": "application/json",
                     "X-Subscription-Token": api_key},
            timeout=20.0,
        )
    except httpx.HTTPError:
        return [], True
    if r.status_code in (401, 403):
        return [], True
    if r.status_code == 429:
        return [], True
    if r.status_code >= 400:
        return [], True
    try:
        data = r.json()
    except ValueError:
        return [], True
    out = []
    for hit in ((data.get("web") or {}).get("results") or []):
        url = hit.get("url")
        if url and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out, False


def _ddg_search(client, query, limit=8):
    """Returns (urls, blocked). A scraped SERP that answers with a challenge
    page looks like success at the HTTP layer, so an empty parse counts as
    blocked rather than as a genuine zero."""
    try:
        r = client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=20.0,
        )
        r.raise_for_status()
    except httpx.HTTPError:
        return [], True
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for a in soup.select("a.result__a, a.result-link, a"):
        href = a.get("href") or ""
        target = _unwrap_ddg(href)
        if not target:
            continue
        host = (urlparse(target).hostname or "").lower()
        if "duckduckgo" in host:
            continue
        if target not in out:
            out.append(target)
        if len(out) >= limit:
            break
    return out, not out


def _bing_search(client, query, limit=8):
    """Returns (urls, blocked)."""
    try:
        r = client.get(
            "https://www.bing.com/search",
            params={"q": query},
            timeout=20.0,
        )
        r.raise_for_status()
    except httpx.HTTPError:
        return [], True
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for a in soup.select("li.b_algo h2 a, a[href]"):
        href = (a.get("href") or "").strip()
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").lower()
        if any(x in host for x in ("bing.com", "microsoft.com", "msn.com", "aka.ms")):
            continue
        if href not in out:
            out.append(href)
        if len(out) >= limit:
            break
    return out, not out


def _unwrap_ddg(href):
    if not href:
        return None
    parsed = urlparse(urljoin("https://duckduckgo.com/", href))
    qs = parse_qs(parsed.query)
    if "uddg" in qs:
        return unquote(qs["uddg"][0])
    if parsed.scheme in ("http", "https") and "duckduckgo" not in (parsed.hostname or ""):
        return href
    return None


def _is_gov_host(host):
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    parts = host.split(".")
    tld = parts[-1]
    return tld in ("gov", "mil") or (tld == "us" and len(parts) >= 2)


def _blocked_host(host):
    host = (host or "").lower()
    return any(part in host for part in SKIP_HOST_PARTS)


def name_slug(name):
    slug = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return re.sub(
        r"(county|parish|borough|township|village|city|town)$", "", slug)


def looks_like_jurisdiction_host(host, name, state=None):
    """franklincountyohio.org, co.franklin.oh.us, cityofcolumbus.gov."""
    host = (host or "").lower().rstrip(".")
    if not host or _blocked_host(host):
        return False
    if any(bad in host for bad in (
            "news", "times", "tribune", "herald", "gazette", "blog",
            "wikipedia", "facebook", "yelp", "tripadvisor")):
        return False
    tld = host.rsplit(".", 1)[-1]
    civic = ("county", "city", "township", "village", "parish", "borough",
             "municip", "cityof", "townof", "countyof", "ci.", "co.")
    official_tld = tld in ("gov", "us", "mil", "org")
    civic_com = any(tok in host for tok in ("cityof", "townof", "countyof", "ci.", "co."))
    if not official_tld and not civic_com:
        return False
    host_alnum = re.sub(r"[^a-z0-9]", "", host)
    core = name_slug(name)
    if len(core) < 4 or core not in host_alnum:
        return False
    if any(tok in host for tok in civic) or any(tok in host_alnum for tok in
                                                ("county", "city", "township", "village",
                                                 "parish", "borough", "municip", "cityof",
                                                 "townof", "countyof")):
        return True
    if state and state.lower() in host.replace("-", ""):
        return True
    return _is_gov_host(host)


def looks_official_result(host, url, name, state=None):
    host = (host or "").lower().rstrip(".")
    if not host or _blocked_host(host):
        return False
    if _is_gov_host(host):
        return True
    return looks_like_jurisdiction_host(host, name, state)


def allowed_host(host, seed_hosts, name=None, state=None):
    host = (host or "").lower().rstrip(".")
    if not host or _blocked_host(host):
        return False
    if host in seed_hosts or any(host.endswith("." + h) for h in seed_hosts):
        return True
    if _is_gov_host(host):
        return True
    if name:
        return looks_like_jurisdiction_host(host, name, state)
    return False


def is_document_url(url):
    return os_ext(urlparse(url).path or "") in (".pdf", ".csv", ".xls", ".xlsx")


def robots_allowed(client, url, user_agent, strict):
    parsed = urlparse(url)
    robots_url = "%s://%s/robots.txt" % (parsed.scheme, parsed.netloc)
    rp = _robots_cache.get(robots_url)
    if rp is None:
        rp = RobotFileParser()
        rp.set_url(robots_url)
        try:
            resp = client.get(robots_url, timeout=10.0)
            if resp.status_code == 200 and resp.text:
                rp.parse(resp.text.splitlines())
            else:
                rp.parse([])
        except httpx.HTTPError:
            if strict:
                _robots_cache[robots_url] = False
                return False
            rp.parse([])
        _robots_cache[robots_url] = rp
    if rp is False:
        return False
    try:
        return rp.can_fetch(user_agent, url)
    except Exception:
        return not strict


def looks_relevant(url, anchor=""):
    path = urlparse(url).path or ""
    ext = os_ext(path)
    if ext in SKIP_EXT:
        return False
    blob = "%s %s" % (url, anchor or "")
    return bool(RELEVANT.search(blob)) or ext in (".pdf", ".csv", ".xls", ".xlsx")


def os_ext(path):
    base = path.rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[-1].lower()


def extract_text(url, content_type, blob):
    ctype = (content_type or "").split(";")[0].strip().lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        return _pdf_text(blob)
    if "html" in ctype or not ctype or "xml" in ctype or "text/" in ctype:
        return _html_text(blob)
    try:
        return blob.decode("utf-8", errors="replace")[:500000]
    except Exception:
        return ""


def _pdf_text(blob):
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(blob)) as pdf:
            for page in pdf.pages[:40]:
                tables = page.extract_tables() or []
                for table in tables[:8]:
                    rows = [" | ".join((c or "").replace("\n", " ") for c in row)
                            for row in table if row]
                    if rows:
                        parts.append("TABLE:\n" + "\n".join(rows))
                text = page.extract_text() or ""
                if text:
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)[:200000]
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(blob))
        parts = []
        for page in reader.pages[:40]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception as exc:
        return "[pdf extract failed: %s]" % exc


def _html_text(blob):
    html = blob if isinstance(blob, str) else blob.decode("utf-8", errors="replace")
    try:
        import trafilatura
        extracted = trafilatura.extract(
            html, include_tables=True, favor_precision=True,
            output_format="txt")
        if extracted and len(extracted.strip()) > 80:
            title = ""
            try:
                soup = BeautifulSoup(blob, "lxml")
                if soup.title and soup.title.string:
                    title = soup.title.string.strip()
            except Exception:
                pass
            text = extracted.strip()
            if title:
                return ("# %s\n\n%s" % (title, text[:80000])), title
            return text[:80000], title
    except Exception:
        pass
    try:
        soup = BeautifulSoup(blob, "lxml")
    except Exception:
        return html[:80000]
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "nav",
                     "footer", "header", "form", "aside", "button"]):
        tag.decompose()
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    tables = []
    for table in soup.find_all("table")[:12]:
        rows = []
        for tr in table.find_all("tr")[:80]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            tables.append("\n".join(rows))
    body = soup.get_text("\n", strip=True)
    body = re.sub(r"\n{3,}", "\n\n", body)
    chunks = []
    if title:
        chunks.append("# " + title)
    if tables:
        chunks.append("TABLES:\n" + "\n\n".join(tables))
    chunks.append(body[:80000])
    return "\n\n".join(chunks), title


def html_links(base_url, blob):
    try:
        soup = BeautifulSoup(blob, "lxml")
    except Exception:
        return []
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        href = parsed._replace(fragment="").geturl()
        out.append((href, a.get_text(" ", strip=True)[:200]))
    return out


def fetch_one(client, url, settings):
    max_bytes = store.as_int(settings.get("max_bytes"), 8000000)
    ua = settings.get("user_agent") or store.DEFAULTS["user_agent"]
    strict = store.as_bool(settings.get("strict_robots"))
    if not robots_allowed(client, url, ua, strict):
        return {
            "url": url, "final_url": url, "http_status": None, "content_type": None,
            "blob": b"", "sha256": None, "robots_allowed": 0, "title": "",
            "text": "", "error": "robots.txt disallows this URL",
        }
    try:
        with client.stream("GET", url) as resp:
            ctype = resp.headers.get("content-type", "")
            chunks = []
            size = 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise FetchError("response larger than %d bytes" % max_bytes)
                chunks.append(chunk)
            blob = b"".join(chunks)
            status = resp.status_code
            final = str(resp.url)
    except httpx.HTTPError as exc:
        return {
            "url": url, "final_url": url, "http_status": None, "content_type": None,
            "blob": b"", "sha256": None, "robots_allowed": 1, "title": "",
            "text": "", "error": str(exc)[:400],
        }
    except FetchError as exc:
        return {
            "url": url, "final_url": url, "http_status": None, "content_type": None,
            "blob": b"", "sha256": None, "robots_allowed": 1, "title": "",
            "text": "", "error": str(exc),
        }

    text_out = extract_text(final, ctype, blob)
    title = ""
    if isinstance(text_out, tuple):
        text, title = text_out
    else:
        text = text_out
        if "html" in (ctype or ""):
            try:
                soup = BeautifulSoup(blob, "lxml")
                if soup.title and soup.title.string:
                    title = soup.title.string.strip()
            except Exception:
                pass
    return {
        "url": url,
        "final_url": final,
        "http_status": status,
        "content_type": ctype,
        "blob": blob,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "robots_allowed": 1,
        "title": title,
        "text": text or "",
        "error": None if 200 <= status < 400 else "HTTP %s" % status,
    }


def record_page(conn, run_id, geoid, category, page, archive_id=None):
    cur = conn.execute(
        "INSERT INTO crawl_page (run_id, geoid, category, url, final_url, http_status, "
        "content_type, sha256, byte_size, archive_file_id, robots_allowed, title, "
        "text_chars, error, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, geoid, category, page["url"], page.get("final_url"),
         page.get("http_status"), page.get("content_type"), page.get("sha256"),
         len(page.get("blob") or b""), archive_id, page.get("robots_allowed", 1),
         (page.get("title") or "")[:300], len(page.get("text") or ""),
         page.get("error"), db.now()))
    conn.commit()
    return cur.lastrowid


def archive_page(conn, page, period_label="crawl"):
    if not page.get("blob"):
        return None
    source_id = db.get_or_create_source(
        conn, page.get("final_url") or page["url"],
        page.get("title") or (page.get("final_url") or page["url"]),
        source_type="portal", authority_tier=4)
    aid, _, _, _ = archive.put(
        conn, "crawl", page.get("final_url") or page["url"], page["blob"],
        period_label, source_id=source_id,
        content_type=page.get("content_type"),
        filename=page.get("final_url") or page["url"])
    conn.execute(
        "INSERT OR IGNORE INTO raw_document (source_id, url, sha256, content_type, "
        "byte_size, retrieved_at) VALUES (?,?,?,?,?,?)",
        (source_id, page.get("final_url") or page["url"], page.get("sha256"),
         page.get("content_type"), len(page["blob"]), db.now()))
    conn.commit()
    return aid


DEPT = re.compile(
    r"treasur|auditor|finance|revenue|tax|commission|clerk|ordinance|"
    r"municipal.?code|budget|assessor|collector",
    re.I,
)


def should_follow(url, anchor, depth, category=None):
    if is_document_url(url):
        return True
    if looks_relevant(url, anchor):
        return True
    if category == "elections" and ELECTION_RELEVANT.search("%s %s" % (url, anchor or "")):
        return True
    if depth == 0 and DEPT.search("%s %s" % (url, anchor or "")):
        return True
    return False


def _has_signal(pages):
    """True if we already pulled a tax document or substantial tax text."""
    for p in pages:
        url = p.get("final_url") or p.get("url") or ""
        ctype = p.get("content_type") or ""
        if is_document_url(url) or "pdf" in ctype:
            if p.get("blob") and not p.get("error"):
                return True
        text = p.get("text") or ""
        if len(text) > 400 and RELEVANT.search(text):
            return True
    return False


def _enqueue(queue, seen, url, depth, cap, prefer=False):
    key = _norm(url)
    if key in seen or len(seen) > cap:
        return False
    seen.add(key)
    item = (url, depth)
    if prefer or is_document_url(url):
        queue.appendleft(item)
    else:
        queue.append(item)
    return True


def crawl_item(conn, client, settings, run_id, geoid, category, name, state,
               diag=None):
    """Catalog first, then search for the county/city site and its documents.

    diag, when passed, collects search counters so the caller can tell a
    genuine empty result from a blocked search engine.
    """
    import time

    if diag is None:
        diag = new_diag()
    delay = store.as_float(settings.get("delay_seconds"), 2.0)
    max_pages = store.as_int(settings.get("max_pages_per_item"), 16)
    max_depth = store.as_int(settings.get("max_depth"), 3)
    max_chars = store.as_int(settings.get("max_text_chars"), 80000)
    do_search = store.as_bool(settings.get("web_search"))
    cap = max_pages * 10

    j = conn.execute("SELECT kind FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    kind = j["kind"] if j else None

    seed_urls = seeds_for(conn, geoid, state, settings, category=category)
    if do_search:
        seed_urls.extend(search_web(client, name, state, category, kind=kind,
                                    settings=settings, diag=diag))

    seed_hosts = set()
    for u in seed_urls:
        host = (urlparse(u).hostname or "").lower()
        if host:
            seed_hosts.add(host)

    queue = deque()
    seen = set()
    for u in seed_urls:
        _enqueue(queue, seen, u, 0, cap, prefer=is_document_url(u))

    pages = []
    texts = []
    fetched = 0
    extra_search = False

    while fetched < max_pages:
        if not queue:
            if extra_search or not do_search or _has_signal(pages):
                break
            extra_search = True
            for u in search_web(client, name, state, category, kind=kind, limit=16,
                                settings=settings, diag=diag):
                host = (urlparse(u).hostname or "").lower()
                if host:
                    seed_hosts.add(host)
                _enqueue(queue, seen, u, 0, cap, prefer=is_document_url(u))
            if not queue:
                break

        url, depth = queue.popleft()
        host = (urlparse(url).hostname or "").lower()
        if not allowed_host(host, seed_hosts, name=name, state=state):
            continue
        if delay and fetched:
            time.sleep(delay)
        page = fetch_one(client, url, settings)
        fetched += 1
        aid = None
        if page.get("blob") and page.get("robots_allowed"):
            try:
                aid = archive_page(conn, page)
            except Exception as exc:
                page["error"] = (page.get("error") or "") + "; archive: %s" % exc
        record_page(conn, run_id, geoid, category, page, aid)
        pages.append(page)
        if page.get("text") and not page.get("error"):
            header = "URL: %s\nTITLE: %s\n" % (page.get("final_url") or url, page.get("title") or "")
            texts.append(header + page["text"])

        final = page.get("final_url") or url
        final_host = (urlparse(final).hostname or "").lower()
        if final_host and allowed_host(final_host, seed_hosts, name=name, state=state):
            seed_hosts.add(final_host)

        if depth < max_depth and page.get("blob") and "html" in (page.get("content_type") or ""):
            for href, anchor in html_links(final, page["blob"]):
                if not allowed_host(urlparse(href).hostname, seed_hosts,
                                    name=name, state=state):
                    continue
                if should_follow(href, anchor, depth, category) or looks_relevant(url):
                    _enqueue(queue, seen, href, depth + 1, cap,
                             prefer=is_document_url(href) or looks_relevant(href, anchor))

    combined = "\n\n-----\n\n".join(texts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n\n[truncated]"
    return pages, combined


def _norm(url):
    p = urlparse(url)
    return "%s://%s%s" % (p.scheme, (p.hostname or "").lower(), p.path.rstrip("/") or "/")
