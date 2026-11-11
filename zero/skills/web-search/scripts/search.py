#!/usr/bin/env python3
"""
Web Search tool - Bing search (no API key required).
Usage: python search.py "query" [max_results]
"""
import os
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

# Exotic whitespace / invisible chars found in scraped snippets (e.g. U+2002 EN SPACE)
_WS_RE = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000\ufeff]")


def setup_stdio():
    """Force UTF-8 output so non-GBK characters don't crash on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


def clean(s: str) -> str:
    """Normalize exotic whitespace and drop control chars."""
    s = _WS_RE.sub(" ", s)
    return "".join(ch for ch in s if ch == "\n" or ch >= " ").strip()


def has_cjk(s: str) -> bool:
    """True if the query contains CJK characters (Chinese/Japanese/Korean)."""
    return any(
        "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" or "\u3040" <= ch <= "\u30ff"
        for ch in s
    )


def fetch(query: str, retries: int = 3) -> str:
    """Fetch Bing search results page with retry."""
    # CJK queries must carry a matching market/language pair. Without it Bing
    # redirects to cn.bing.com and serves a generic anti-bot page whose
    # b_algo blocks are unrelated to the query (verified: multi-keyword CJK
    # queries returned bilibili/xinpianchang results).
    if has_cjk(query):
        params = {"q": query, "mkt": "zh-CN", "setlang": "zh-Hans", "form": "QBLH"}
        accept_lang = "zh-CN,zh;q=0.9,en;q=0.8"
    else:
        params = {"q": query, "mkt": "en-US", "setlang": "en", "form": "QBLH"}
        accept_lang = "en-US,en;q=0.9"
    url = "https://www.bing.com/search?" + urllib.parse.urlencode(params)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": accept_lang,
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
    return clean(html_mod.unescape(re.sub(r"<[^>]+>", "", s)))


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


def relevance(results, query: str) -> float:
    """Fraction of query tokens that appear in the result titles/snippets.

    Bing (from a datacenter IP, no JS) tends to answer only the *first* token of a
    multi-token query and fill the rest of the page with generic entity results
    (e.g. "成都天气 今天 天气预报" -> 成都市百科 / 成都市政府). This score lets
    main() detect that degradation and retry with a shorter query.
    """
    tokens = [t for t in query.split() if t]
    if not tokens or not results:
        return 0.0
    text = " ".join(r["title"] + " " + r["snippet"] for r in results)
    return sum(1 for t in tokens if t in text) / len(tokens)


def query_variants(query: str):
    """Progressively shorter versions of the query, longest first."""
    tokens = query.split()
    variants = [query]
    for n in range(len(tokens) - 1, 0, -1):
        cand = " ".join(tokens[:n])
        if cand != query and cand not in variants:
            variants.append(cand)
    return variants


def bing_search(query: str, max_results: int):
    """Search Bing, retrying with shorter queries when results look off-topic."""
    best = ([], 0.0)
    for cand in query_variants(query):
        try:
            results = parse(fetch(cand), max_results)
        except Exception:
            continue
        score = relevance(results, query)
        if score > best[1]:
            best = (results, score)
        if score >= 0.5:  # good enough: most query tokens are present
            break
    return best[0]


def main():
    setup_stdio()
    if len(sys.argv) < 2:
        print('Usage: python search.py "query" [max_results]', file=sys.stderr)
        sys.exit(1)
    query = sys.argv[1]
    max_results = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    try:
        results = bing_search(query, max_results)
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
