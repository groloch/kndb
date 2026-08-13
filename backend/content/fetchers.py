import asyncio
import base64
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..core import config
from ..integrations import llm

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
}

ARXIV_ID = (r"[0-9]{4}\.[0-9]{4,5}(?:v\d+)?"
            r"|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?")

ARXIV_RE = re.compile(rf"arxiv\.org/(?:abs|pdf|html)/({ARXIV_ID})", re.I)
# huggingface.co/papers/<arxiv id> — a viewer wrapped around the arXiv paper
HF_PAPER_RE = re.compile(rf"^papers/({ARXIV_ID})(?:/|$)", re.I)

HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"}
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}

# arXiv renders LaTeX submissions to HTML; ar5iv backfills what arXiv misses.
ARXIV_HTML_URLS = ("https://arxiv.org/html/{aid}",
                   "https://ar5iv.labs.arxiv.org/html/{aid}")
# page chrome wrapped around the LaTeXML <article>, and the inline tags whose
# text belongs to the enclosing block rather than on a line of its own
LATEXML_DROP = ("script", "style", "noscript", "iframe", "nav", "header",
                "footer", "dialog")
LATEXML_DROP_CLASS = ("ltx_page_navbar", "ltx_TOC", "ltx_page_logo", "ar5iv-footer")
LATEXML_BLOCK = ("p", "div", "section", "h1", "h2", "h3", "h4", "h5", "h6", "li",
                 "tr", "table", "figure", "figcaption", "blockquote", "br",
                 "dt", "dd", "pre")
# shorter than this means we got an error page, not a paper
MIN_HTML_TEXT = config.MIN_HTML_CHARS

MAX_IMAGE = config.IMAGE_MAX_BYTES               # largest image inlined
IMG_LIMIT = asyncio.Semaphore(config.IMAGE_DOWNLOADS)  # concurrent downloads

_CLIENT: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(
            headers=UA,
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=60.0, pool=30.0),
        )
    return _CLIENT

async def aclose() -> None:
    global _CLIENT
    if _CLIENT is not None:
        await _CLIENT.aclose()
        _CLIENT = None

async def _http_get(url: str, timeout: float = 60.0, **kw) -> httpx.Response:
    kw.setdefault("timeout", timeout)
    return await _http().get(url, **kw)

def classify_url(url: str) -> str:
    if ARXIV_RE.search(url or ""):
        return "arxiv"
    host = (urlparse(url).hostname or "").lower()
    for h in HF_HOSTS:
        if host == h or host.endswith("." + h):
            return "huggingface"
    return "web"

async def fetch_by_kind(kind: str, url: str) -> dict:
    """Dispatch. Every fetch returns::

        {"source_type", "content" (str|bytes), "title", "seed", "url"}

    plus an optional ``"text"`` — a clean plain-text rendition of the same
    document, cached alongside the blob and preferred over parsing the blob
    when the LLM needs the document as text.
    """
    if kind == "arxiv":
        return await fetch_arxiv(url)
    if kind == "huggingface":
        return await fetch_huggingface(url)
    return await fetch_web(url)

async def fetch_arxiv(url: str) -> dict:
    """The PDF stays the stored blob — it is what the user reads. The HTML
    rendition rides along as ``text`` because LaTeX-derived markup extracts far
    more cleanly than anything we can recover from the PDF."""
    m = ARXIV_RE.search(url)
    if not m:
        raise ValueError("not a valid arXiv URL")
    aid = m.group(1)
    meta = {
        "source_type": "pdf",
        "title": f"arXiv:{aid}",
        "seed": "",
        "url": f"https://arxiv.org/abs/{aid}",
    }
    api, pdf, text = await asyncio.gather(
        _arxiv_api_meta(aid), _arxiv_pdf(aid), _arxiv_html_text(aid),
    )
    meta.update(api)
    meta["content"] = pdf
    if text:
        meta["text"] = text
    return meta

async def _arxiv_pdf(aid: str) -> bytes:
    r = await _http_get(f"https://arxiv.org/pdf/{aid}", timeout=120.0)
    if r.status_code >= 300:
        raise ValueError(f"arXiv PDF download failed with HTTP {r.status_code}")
    return r.content

async def _arxiv_api_meta(aid: str) -> dict:
    """Title/authors/abstract from the arXiv API. Best effort — the import
    still works off the bare ID when this is unavailable."""
    out = {}
    try:
        r = await _http_get(f"https://export.arxiv.org/api/query?id_list={aid}",
                            timeout=60.0, follow_redirects=True)
        if r.status_code >= 300:
            return out
        root = ET.fromstring(r.content)
        entry = root.find("atom:entry", ARXIV_NS)
        if entry is None:
            return out
        title = (entry.findtext("atom:title", default="", namespaces=ARXIV_NS)
                 or "").strip().replace("\n", " ")
        summary = (entry.findtext("atom:summary", default="", namespaces=ARXIV_NS)
                   or "").strip()
        authors = [a.findtext("atom:name", default="", namespaces=ARXIV_NS) or ""
                   for a in entry.findall("atom:author", ARXIV_NS)]
        seed = ""
        if title:
            out["title"] = title
        if authors:
            seed = "**Authors:** " + ", ".join(a for a in authors if a) + "\n\n"
        if summary:
            seed += "**Abstract:**\n" + summary
        if seed:
            out["seed"] = seed
    except Exception:
        pass
    return out

async def _arxiv_html_text(aid: str) -> str:
    """Clean text from arXiv's HTML rendition, or ``""`` when there is none —
    PDF-only submissions have no LaTeX source to render, and the caller then
    falls back to extracting text from the PDF."""
    for template in ARXIV_HTML_URLS:
        try:
            r = await _http_get(template.format(aid=aid), timeout=90.0,
                                follow_redirects=True)
            if r.status_code >= 300:
                continue
            text = await asyncio.to_thread(latexml_text, r.content)
            if len(text) >= MIN_HTML_TEXT:
                return text
        except Exception:
            continue
    return ""

def latexml_text(html: bytes | str) -> str:
    """Flatten a LaTeXML-generated paper into plain text: page chrome dropped,
    math restored to its LaTeX source, one block element per line."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("article") or soup.find(class_="ltx_page_main") or soup.body
    if root is None:
        return ""
    for tag in root.find_all(LATEXML_DROP):
        tag.decompose()
    for cls in LATEXML_DROP_CLASS:
        for tag in root.find_all(class_=cls):
            tag.decompose()
    for node in root.find_all("math"):
        alt = (node.get("alttext") or "").strip()
        node.replace_with(f" ${alt}$ " if alt else " ")
    for tag in root.find_all(LATEXML_BLOCK):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = re.sub(r"[^\S\n]+", " ", root.get_text(" "))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r" ?\n ?", "\n", text)).strip()

async def fetch_huggingface(url: str) -> dict:
    path = (urlparse(url).path or "").strip("/")
    paper = HF_PAPER_RE.match(path)
    if paper:  # a paper page is a viewer around arXiv — import the paper itself
        return await fetch_arxiv(f"https://arxiv.org/abs/{paper.group(1)}")
    if path and not path.startswith("papers/"):
        for branch in ("main", "master"):
            readme_url = f"https://huggingface.co/{path}/raw/{branch}/README.md"
            try:
                r = await _http_get(readme_url, timeout=45.0)
                if r.status_code < 300 and r.text.strip():
                    title = path.rsplit("/", 1)[-1]
                    return {
                        "source_type": "md",
                        "content": r.text,
                        "title": f"{title} (Hugging Face)",
                        "seed": "",
                        "url": url,
                    }
            except Exception:
                continue
    return await fetch_web(url)

def _page_title(soup: BeautifulSoup, url: str) -> str:
    if soup.title and soup.title.string and soup.title.string.strip():
        return soup.title.string.strip()[:200]
    path = urlparse(url).path.strip("/")
    if path:
        return path.rsplit("/", 1)[-1].replace("_", " ").replace("-", " ").title()[:200]
    return urlparse(url).hostname or "untitled"

def _url_title(url: str) -> str:
    path = urlparse(url).path.strip("/")
    seg = path.rsplit("/", 1)[-1] if path else urlparse(url).hostname or "untitled"
    return (seg.replace("_", " ").replace("-", " ").replace(".pdf", "").strip()
            .title()[:200] or "untitled")

async def _inline_css(soup: BeautifulSoup, base_url: str) -> None:
    targets = []
    for link in list(soup.find_all("link")):
        rel = [str(x).lower() for x in (link.get("rel") or [])]
        href = link.get("href")
        if "stylesheet" not in rel or not href:
            link.decompose()
            continue
        targets.append((link, urljoin(base_url, href)))

    async def one(link, href):
        try:
            rr = await _http_get(href, timeout=20.0)
            ctype = rr.headers.get("content-type", "").lower()
            if (rr.status_code < 300 and 0 < len(rr.content) < 8 * 1024 * 1024
                    and any(t in ctype for t in ("css", "text"))):
                style = soup.new_tag("style")
                style.string = rr.text
                link.replace_with(style)
            else:
                link.decompose()
        except Exception:
            link.decompose()

    await asyncio.gather(*(one(l, u) for l, u in targets))

async def _embed_images(soup: BeautifulSoup, base_url: str) -> None:
    targets = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            img.decompose()
            continue
        if src.startswith("data:"):
            continue
        targets.append((img, urljoin(base_url, src)))

    async def one(img, url):
        try:
            async with IMG_LIMIT:
                rr = await _http_get(url, timeout=20.0)
            ctype = rr.headers.get("content-type", "").lower()
            if rr.status_code >= 300 or "image/" not in ctype:
                img.decompose()
                return
            if not rr.content or len(rr.content) > MAX_IMAGE:
                img.decompose()
                return
            img["src"] = f"data:{ctype};base64," + base64.b64encode(rr.content).decode()
        except Exception:
            img.decompose()

    await asyncio.gather(*(one(img, u) for img, u in targets))

def _naive_md(soup: BeautifulSoup, url: str, title: str) -> str:
    lines = [f"# {title}", "", soup.get_text("\n", strip=True), "", "## Links"]
    seen = set()
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if href.startswith("#"):
            continue
        if not href.startswith(("http://", "https://")):
            href = urljoin(url, href)
        if href in seen:
            continue
        seen.add(href)
        txt = a.get_text(" ", strip=True).strip()
        lines.append(f"- {txt} — <{href}>" if txt else f"- <{href}>")
    return "\n".join(lines)

async def fetch_web(url: str) -> dict:
    r = await _http_get(url)
    if r.status_code >= 300:
        raise ValueError(f"GET {url} -> HTTP {r.status_code}")
    ctype = (r.headers.get("content-type") or "").lower()
    low = url.lower()

    if "html" not in ctype:
        if "pdf" in ctype or low.endswith(".pdf"):
            return {"source_type": "pdf", "content": r.content,
                    "title": _url_title(url), "seed": "", "url": url}
        return {"source_type": "md", "content": r.text,
                "title": _url_title(url), "seed": "", "url": url}

    soup = BeautifulSoup(r.content, "html.parser")
    title = _page_title(soup, url)
    for tag in soup.find_all(["script", "noscript", "iframe", "style"]):
        tag.decompose()
    await _inline_css(soup, url)
    await _embed_images(soup, url)

    text = soup.get_text(" ", strip=True)
    if len(text) < config.MIN_PAGE_CHARS:
        try:
            # html_to_markdown applies limits.web_markdown_chars itself
            md = await llm.html_to_markdown(soup.get_text("\n", strip=True))
            if md and len(md) > len(text):
                return {"source_type": "md", "content": f"# {title}\n\n{md}",
                        "title": title, "seed": "", "url": url}
        except Exception:
            pass
        return {"source_type": "md", "content": _naive_md(soup, url, title),
                "title": title, "seed": "", "url": url}

    return {"source_type": "html+css", "content": str(soup),
            "title": title, "seed": "", "url": url}
