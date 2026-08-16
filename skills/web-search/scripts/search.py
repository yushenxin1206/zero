#!/usr/bin/env python3
"""
Web Search tool - Bing search (no API key required).
Usage: python search.py "query" [max_results]
"""
import sys
import re
import html as html_mod
import time
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def fetch(query: str, retries: int = 3) -> str:
    """Fetch Bing search results page with retry."""
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query) + "&setlang=en"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            last_err = e
            time.sleep(1.5 * attempt)
    raise last_err


def fetch_duckduckgo(query: str) -> str:
    """Fallback engine: DuckDuckGo HTML endpoint."""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def parse_ddg(html_text: str, max_results: int):
    """Parse DuckDuckGo HTML results (.result__a / .result__snippet)."""
    results = []
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html_text,
        re.DOTALL,
    ):
        url = m.group(1)
        if "uddg=" in url:
            url = urllib.parse.parse_qs(
                urllib.parse.urlparse(url).query
            ).get("uddg", [url])[0]
        results.append(
            {
                "title": strip_tags(m.group(2)),
                "url": html_mod.unescape(url),
                "snippet": strip_tags(m.group(3)),
            }
        )
        if len(results) >= max_results:
            break
    return results


def strip_tags(s: str) -> str:
    return html_mod.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def decode_url(u: str) -> str:
    """Decode Bing redirect URLs (bing.com/ck/a?u=...) back to the real URL."""
    u = html_mod.unescape(u)
    if "bing.com/ck/a" in u:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        if q.get("u"):
            return urllib.parse.unquote(q["u"][0])
    return u


def parse(html_text: str, max_results: int):
    results = []
    # Each Bing organic result is in <li class="b_algo"> ... </li>
    for block in re.findall(r'<li class="b_algo".*?</li>', html_text, re.DOTALL):
        # Title link
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not m:
            continue
        url = decode_url(m.group(1))
        title = strip_tags(m.group(2))
        # Snippet
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        snippet = strip_tags(sm.group(1)) if sm else ""
        if not url.startswith("http"):
            continue
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def main():
    if len(sys.argv) < 2:
        print('Usage: python search.py "query" [max_results]', file=sys.stderr)
        sys.exit(1)
    query = sys.argv[1]
    max_results = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    try:
        page = fetch(query)
        results = parse(page, max_results)
        if not results:
            # Fallback to DuckDuckGo if Bing returned no usable results
            page = fetch_duckduckgo(query)
            results = parse_ddg(page, max_results)
            if results:
                print("(via DuckDuckGo)")
    except Exception as e:
        print(f"[ERROR] Search failed: {e}", file=sys.stderr)
        sys.exit(2)

    if not results:
        print("[INFO] No results found.")
        return

    for i, r in enumerate(results, 1):
        print(f"{i}. {r['title']}")
        print(f"   URL: {r['url']}")
        print(f"   {r['snippet']}")
        print()


if __name__ == "__main__":
    main()
