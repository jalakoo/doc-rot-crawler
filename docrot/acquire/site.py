"""Docs-site acquisition ladder.

Probe in order, stop at the first hit:
  1. /llms-full.txt     - whole corpus inlined, one GET
  2. /llms.txt          - page index, then per-page .md
  3. per-page .md       - Mintlify / VitePress convention
  4. /sitemap.xml       - universal fallback
  5. framework search   - Next.js / Fumadocs /api/search
  6. HTML crawl         - last resort, trafilatura
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from ..models import Page
from .http import Fetcher
from .pins import find_pins

MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+\.md)\)")
LOC = re.compile(r"<loc>([^<]+)</loc>")
SECTION_URL = re.compile(r"https?://[^\s)\]\"'<>]+")


def _root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _index_urls(root: str, docs_url: str, name: str) -> list[str]:
    """Where an index file may live: under the docs path first, then host root.

    momentic.ai/docs serves its real llms.txt / llms-full.txt / sitemap.xml
    under /docs; the host-root copies describe the marketing site and blog.
    A docs URL at the host root yields one candidate, so nothing is fetched twice.
    """
    base = docs_url.rstrip("/")
    urls = [f"{base}/{name}", f"{root}/{name}"]
    return list(dict.fromkeys(urls))


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            # llms-full.txt headings are often links: `# [Fields](https://...)`
            return re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s[2:]).strip()
    return fallback


def _page(url: str, text: str) -> Page:
    path = urlparse(url).path or "/"
    return Page(
        url=url,
        path=path,
        title=_title(text, path.rstrip("/").split("/")[-1] or "index"),
        source="site",
        text=text,
        pinned_versions=find_pins(text),
    )


async def acquire_site(docs_url: str, fetcher: Fetcher, max_pages: int,
                       log=print) -> tuple[list[Page], str]:
    """Returns (pages, which-strategy-worked)."""
    root = _root(docs_url)
    prefix = urlparse(docs_url).path.rstrip("/")

    for probe in (_llms_full, _llms_txt, _per_page_md, _sitemap, _search_index):
        pages, how = await probe(root, docs_url, prefix, fetcher, max_pages, log)
        if pages:
            return _tag(pages[:max_pages], docs_url), how

    pages = await _html_crawl(root, docs_url, prefix, fetcher, max_pages, log)
    return _tag(pages[:max_pages], docs_url), ("html crawl" if pages else "none")


def _tag(pages: list[Page], origin: str) -> list[Page]:
    for p in pages:
        p.origin = origin
    return pages


# ----------------------------------------------------------------- strategies

async def _llms_full(root, docs_url, prefix, f, max_pages, log):
    for index in _index_urls(root, docs_url, "llms-full.txt"):
        code, text = await f.get(index)
        if code != 200 or len(text) < 2000:
            continue
        pages = _sections(index, text, prefix)
        note = f"{len(pages)} pages" if pages else f"0 sections under {prefix or '/'}"
        log(f"  llms-full.txt 200 ({len(text)//1024} KB) - {note}")
        if pages:
            return pages, "llms-full.txt"
    return [], ""


def _is_index(url: str) -> bool:
    return urlparse(url).path.rsplit("/", 1)[-1] in ("llms.txt", "llms-full.txt")


def _sections(index: str, text: str, prefix: str) -> list[Page]:
    chunks = re.split(r"\n(?=#\s+\S)", text)
    pages = []
    for i, chunk in enumerate(chunks):
        if len(chunk.strip()) < 80:
            continue
        url = f"{index}#section-{i}"
        # A section names its page in its heading (`# [Title](url)`) or just
        # under it (`Source: url`). A URL further down belongs to the page's
        # content - a code sample's `https://localhost:5001` - not its address.
        m = SECTION_URL.search("\n".join(chunk.lstrip().splitlines()[:4]))
        if m:
            url = m.group(0).rstrip(").,")
        if prefix and prefix not in url:
            continue
        if _is_index(url):
            # The file's own preamble ("this file is very large - see llms.txt")
            # names the index as its address. It is no page; its prose and the
            # commands in it are instructions for agents reading the file.
            continue
        pages.append(_page(url, chunk))
    return pages


async def _llms_txt(root, docs_url, prefix, f, max_pages, log):
    for index in _index_urls(root, docs_url, "llms.txt"):
        code, text = await f.get(index)
        if code != 200:
            continue
        links = [(t, u) for t, u in MD_LINK.findall(text)]
        if prefix:
            links = [(t, u) for t, u in links if prefix in u]
        if not links:
            log(f"  llms.txt 200 at {index} - 0 .md links under {prefix or '/'}")
            continue
        log(f"  llms.txt 200 - {len(links)} pages indexed")
        pages: list[Page] = []
        for title, url in links[:max_pages]:
            c, body = await f.get(url)
            if c != 200:
                continue
            p = _page(url.removesuffix(".md"), body)
            p.title = title or p.title
            pages.append(p)
        return pages, "llms.txt + per-page .md"
    return [], ""


async def _per_page_md(root, docs_url, prefix, f, max_pages, log):
    if not await f.ok(docs_url.rstrip("/") + ".md"):
        return [], ""
    log("  per-page .md served")
    seen: set[str] = set()
    queue: list[str] = [docs_url.rstrip("/")]
    pages: list[Page] = []
    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        c, body = await f.get(url + ".md")
        if c != 200:
            continue
        pages.append(_page(url, body))
        # Links are root-relative or absolute: momentic.ai/docs.md links to its
        # 23 children absolutely, so a root-relative-only match stopped at 1 page.
        for href in re.findall(r"\]\(((?:/|" + re.escape(root) + r"/)[^)\s#]*)\)", body):
            nxt = (root + href if href.startswith("/") else href).removesuffix(".md")
            if nxt not in seen and (not prefix or prefix in nxt):
                queue.append(nxt)
    return pages, "per-page .md"


async def _sitemap(root, docs_url, prefix, f, max_pages, log):
    for index in _index_urls(root, docs_url, "sitemap.xml"):
        code, text = await f.get(index)
        if code != 200 or "<loc>" not in text:
            continue
        urls = [u for u in LOC.findall(text) if not prefix or prefix in u]
        if not urls:
            log(f"  sitemap.xml 200 at {index} - 0 URLs under {prefix or '/'}")
            continue
        log(f"  sitemap.xml 200 - {len(urls)} URLs")
        return await _fetch_html(urls[:max_pages], f), "sitemap.xml"
    return [], ""


async def _search_index(root, docs_url, prefix, f, max_pages, log):
    """Next.js / Fumadocs expose /api/search?query=. An empty query returns
    nothing, so union a few high-frequency stopwords to enumerate the site.
    """
    urls: set[str] = set()
    for term in ("the", "a", "to", "of"):
        code, text = await f.get(f"{root}/api/search?query={term}")
        if code != 200 or not text.strip().startswith("["):
            continue
        for m in re.finditer(r'"url"\s*:\s*"([^"]+)"', text):
            u = m.group(1).split("#")[0]
            if u.startswith("/"):
                u = root + u
            if not prefix or prefix in u:
                urls.add(u)
    if not urls:
        return [], ""
    log(f"  /api/search 200 - {len(urls)} pages enumerated")
    return await _fetch_html(sorted(urls)[:max_pages], f), "framework search index"


async def _html_crawl(root, docs_url, prefix, f, max_pages, log):
    log("  falling back to HTML crawl")
    seen: set[str] = set()
    queue: list[str] = [docs_url]
    urls: list[str] = []
    while queue and len(urls) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        code, html = await f.get(url)
        if code != 200:
            continue
        urls.append(url)
        for href in re.findall(r'href="(/[^"#]*)"', html):
            nxt = urljoin(root, href)
            if nxt not in seen and (not prefix or prefix in nxt):
                queue.append(nxt)
    return await _fetch_html(urls, f)


# ------------------------------------------------------------------- helpers

async def _fetch_html(urls: list[str], f: Fetcher) -> list[Page]:
    pages = []
    for url in urls:
        code, html = await f.get(url)
        if code != 200:
            continue
        pages.append(_page(url, _to_text(html)))
    return pages


def _to_text(html: str) -> str:
    """HTML to markdown-ish text, preserving <pre> blocks as fences.

    Fences are pulled out first and re-inserted afterwards. They are wrapped in
    <p> before extraction because trafilatura discards bare text nodes, and any
    that still go missing are appended rather than lost - a dropped fence is a
    missed finding, silently.
    """
    import html as htmllib

    blocks: list[str] = []

    def grab(m):
        inner = re.sub(r"<[^>]+>", "", m.group(1))
        blocks.append(htmllib.unescape(inner).strip())
        return f"<p>@@FENCE{len(blocks)-1}@@</p>"

    stripped = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    stripped = re.sub(r"<style.*?</style>", " ", stripped, flags=re.S | re.I)
    stripped = re.sub(r"<pre[^>]*>(.*?)</pre>", grab, stripped, flags=re.S | re.I)

    title = ""
    tm = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
    if tm:
        title = htmllib.unescape(re.sub(r"<[^>]+>", "", tm.group(1))).strip()

    text = ""
    try:
        import trafilatura
        text = trafilatura.extract(stripped, include_comments=False) or ""
    except Exception:
        text = ""
    if not text:
        text = htmllib.unescape(re.sub(r"<[^>]+>", " ", stripped))
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)

    def fence(b: str) -> str:
        lang = "python" if re.search(r"\b(import |from \w+ import|def |print\()", b) \
               else "bash" if re.search(r"^\s*(pip|npm|uv|export|curl|git)\b", b, re.M) \
               else ""
        return f"```{lang}\n{b}\n```"

    for i, b in enumerate(blocks):
        token = f"@@FENCE{i}@@"
        if token in text:
            text = text.replace(token, fence(b))
        else:
            text += "\n\n" + fence(b)     # never drop a fence

    return (f"# {title}\n\n{text}" if title else text).strip()
