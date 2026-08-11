import asyncio
import base64
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..integrations import llm

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
}

ARXIV_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?"
    r"|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)",
    re.I,
)

HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"}
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}

MAX_IMAGE = 3 * 1024 * 1024  # max image file size: 3 MB
IMG_LIMIT = asyncio.Semaphore(6)  # max concurrent image downloads

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
    """
    if kind == "arxiv":
        return await fetch_arxiv(url)
    if kind == "huggingface":
        return await fetch_huggingface(url)
    return await fetch_web(url)

async def fetch_arxiv(url: str) -> dict:
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
    try:
        r = await _http_get(f"http://export.arxiv.org/api/query?id_list={aid}", timeout=60.0)
        if r.status_code < 300:
            try:
                root = ET.fromstring(r.content)
                entry = root.find("atom:entry", ARXIV_NS)
                if entry is not None:
                    title = (entry.findtext("atom:title", default="", namespaces=ARXIV_NS)
                             or "").strip().replace("\n", " ")
                    summary = (entry.findtext("atom:summary", default="", namespaces=ARXIV_NS)
                               or "").strip()
                    authors = [a.findtext("atom:name", default="", namespaces=ARXIV_NS) or ""
                               for a in entry.findall("atom:author", ARXIV_NS)]
                    if title:
                        meta["title"] = title
                    if authors:
                        meta["seed"] = "**Authors:** " + ", ".join(a for a in authors if a) + "\n\n"
                    if summary:
                        meta["seed"] += "**Abstract:**\n" + summary
            except Exception:
                pass
    except Exception:
        pass

    r = await _http_get(f"https://arxiv.org/pdf/{aid}", timeout=120.0)
    if r.status_code >= 300:
        raise ValueError(f"arXiv PDF download failed with HTTP {r.status_code}")
    meta["content"] = r.content
    return meta

async def fetch_huggingface(url: str) -> dict:
    path = (urlparse(url).path or "").strip("/")
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
    if len(text) < 200:
        try:
            raw = soup.get_text("\n", strip=True)[:12000]
            md = await llm.html_to_markdown(raw)
            if md and len(md) > len(text):
                return {"source_type": "md", "content": f"# {title}\n\n{md}",
                        "title": title, "seed": "", "url": url}
        except Exception:
            pass
        return {"source_type": "md", "content": _naive_md(soup, url, title),
                "title": title, "seed": "", "url": url}

    return {"source_type": "html+css", "content": str(soup),
            "title": title, "seed": "", "url": url}
