"""URL routing, the LaTeXML text cleaner, and the text sidecar.

Everything here is offline: the network-facing coroutines are stubbed, so the
suite never depends on arXiv being reachable.
"""

import asyncio
import os

import pytest

from backend.content import fetchers, services
from backend.data import store


PAPER_HTML = b"""
<html><body>
  <header class="arxiv-html-header">arXiv banner</header>
  <nav class="ltx_page_navbar"><a href="#S1">Table of contents</a></nav>
  <div class="ltx_page_main">
    <article class="ltx_document">
      <h1 class="ltx_title">A Title</h1>
      <div class="ltx_abstract">
        <p class="ltx_p">We show that <math alttext="x^2 + 1"
           class="ltx_Math"><semantics><mi>x</mi></semantics></math> matters.</p>
      </div>
      <section class="ltx_section">
        <p class="ltx_p">First <span class="ltx_text">inline</span> paragraph.</p>
        <p class="ltx_p">Second paragraph.</p>
      </section>
    </article>
  </div>
  <footer class="arxiv-html-footer">Contact arXiv</footer>
</body></html>
"""


# --- URL routing ----------------------------------------------------------

@pytest.mark.parametrize("url,kind", [
    ("https://arxiv.org/abs/2401.10774", "arxiv"),
    ("https://arxiv.org/pdf/2401.10774v2", "arxiv"),
    ("https://arxiv.org/html/2401.10774", "arxiv"),
    ("https://huggingface.co/papers/2401.10774", "huggingface"),
    ("https://huggingface.co/bert-base-uncased", "huggingface"),
    ("https://example.com/post", "web"),
])
def test_classify_url(url, kind):
    assert fetchers.classify_url(url) == kind


@pytest.mark.parametrize("path,aid", [
    ("papers/2401.10774", "2401.10774"),
    ("papers/2401.10774v3", "2401.10774v3"),
    ("papers/cond-mat/0512345", "cond-mat/0512345"),
    ("papers/2401.10774/extra", "2401.10774"),
])
def test_hf_paper_paths_carry_an_arxiv_id(path, aid):
    assert fetchers.HF_PAPER_RE.match(path).group(1) == aid


@pytest.mark.parametrize("path", ["papers", "papers/month/2024-01", "papers/nope"])
def test_hf_non_paper_paths_are_not_arxiv(path):
    assert fetchers.HF_PAPER_RE.match(path) is None


def test_huggingface_paper_link_imports_the_arxiv_paper(monkeypatch):
    """A HF paper page is a viewer around arXiv, so it must resolve to the
    paper — same blob, same canonical URL, so re-imports still dedup."""
    seen = []

    async def fake_pdf(aid):
        seen.append(aid)
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(fetchers, "_arxiv_pdf", fake_pdf)
    monkeypatch.setattr(fetchers, "_arxiv_api_meta", lambda aid: _done({}))
    monkeypatch.setattr(fetchers, "_arxiv_html_text", lambda aid: _done("body text"))

    info = asyncio.run(fetchers.fetch_by_kind(
        "huggingface", "https://huggingface.co/papers/2401.10774"))

    assert seen == ["2401.10774"]
    assert info["source_type"] == "pdf"
    assert info["url"] == "https://arxiv.org/abs/2401.10774"
    assert info["text"] == "body text"


def test_arxiv_import_survives_a_missing_html_rendition(monkeypatch):
    """PDF-only submissions have no LaTeX source to render — the import still
    has to succeed, just without a sidecar."""
    monkeypatch.setattr(fetchers, "_arxiv_pdf", lambda aid: _done(b"%PDF-1.4"))
    monkeypatch.setattr(fetchers, "_arxiv_api_meta", lambda aid: _done({}))
    monkeypatch.setattr(fetchers, "_arxiv_html_text", lambda aid: _done(""))

    info = asyncio.run(fetchers.fetch_arxiv("https://arxiv.org/abs/2401.10774"))
    assert "text" not in info


def _done(value):
    async def _coro():
        return value
    return _coro()


# --- LaTeXML text ---------------------------------------------------------

def test_latexml_text_keeps_the_paper_and_drops_the_chrome():
    text = fetchers.latexml_text(PAPER_HTML)
    assert "A Title" in text
    assert "Second paragraph." in text
    assert "arXiv banner" not in text
    assert "Table of contents" not in text
    assert "Contact arXiv" not in text


def test_latexml_text_restores_math_as_latex():
    assert "$x^2 + 1$" in fetchers.latexml_text(PAPER_HTML)


def test_latexml_text_keeps_a_paragraph_on_one_line():
    """Inline markup must not shatter sentences across lines."""
    lines = fetchers.latexml_text(PAPER_HTML).splitlines()
    assert "First inline paragraph." in lines


def test_latexml_text_on_junk_is_empty_not_an_error():
    assert fetchers.latexml_text(b"") == ""


# --- the sidecar ----------------------------------------------------------

def test_sidecar_never_collides_with_a_source_blob():
    assert ".txt" not in store.TYPE_EXT.values()
    assert store.text_sidecar("/data/sources/src_ab12.pdf").endswith("src_ab12.txt")


def test_document_text_prefers_the_sidecar(tmp_path):
    blob = tmp_path / "src_x.md"
    blob.write_text("blob text", encoding="utf-8")
    (tmp_path / "src_x.txt").write_text("sidecar text", encoding="utf-8")
    row = {"source_type": "md", "source_path": str(blob)}
    assert asyncio.run(services.document_text(row)) == "sidecar text"


def test_document_text_falls_back_to_the_blob(tmp_path):
    blob = tmp_path / "src_x.md"
    blob.write_text("blob text", encoding="utf-8")
    row = {"source_type": "md", "source_path": str(blob)}
    assert asyncio.run(services.document_text(row)) == "blob text"


def test_document_text_ignores_an_empty_sidecar(tmp_path):
    blob = tmp_path / "src_x.md"
    blob.write_text("blob text", encoding="utf-8")
    (tmp_path / "src_x.txt").write_text("   \n", encoding="utf-8")
    row = {"source_type": "md", "source_path": str(blob)}
    assert asyncio.run(services.document_text(row)) == "blob text"


def test_deleting_a_source_takes_the_sidecar_with_it(client):
    r = client.post("/api/import", data={"url": ""},
                    files={"file": ("doomed.md", b"# doomed", "text/markdown")},
                    headers={"X-KNDB-User": "sidecar-owner"})
    sid = r.json()["id"]
    sidecar = store.absdir(os.path.join(store.SOURCES_DIR, sid + ".txt"))
    with open(sidecar, "w", encoding="utf-8") as f:
        f.write("cached text")

    assert client.delete("/api/source/" + sid).status_code == 200
    assert not os.path.exists(sidecar)
