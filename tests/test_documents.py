"""生成真实二进制文件，验证多格式解析、来源定位与增量失效。"""

import importlib.util
import io
from pathlib import Path

import pytest

from fa.knowledge import import_file, inventory, validate_filename
from fa.retrieval import load_corpus, search
from fa.retrieval.documents import DocumentError, extract_document
from fa.retrieval.index import corpus_fingerprint, load, load_or_build

HAS_DOCS = all(importlib.util.find_spec(m) is not None for m in ("pymupdf", "pptx", "PIL"))
HAS_OCR = importlib.util.find_spec("rapidocr_onnxruntime") is not None
documents = pytest.mark.skipif(not HAS_DOCS, reason="需要documents extra")
ocr = pytest.mark.skipif(not HAS_OCR or not HAS_DOCS, reason="需要本地OCR依赖")


def pdf_bytes(*texts):
    import pymupdf
    doc = pymupdf.open()
    for text in texts:
        doc.new_page().insert_text((50, 80), text)
    raw = doc.tobytes()
    doc.close()
    return raw


def image_bytes():
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1000, 230), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    draw.text((35, 30), "Emergency reserve", font=font, fill="black")
    draw.text((35, 110), "Budget 9000 CNY", font=font, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def pptx_bytes(picture=None):
    from pptx import Presentation
    from pptx.util import Inches
    ppt = Presentation()
    slide = ppt.slides.add_slide(ppt.slide_layouts[5])
    slide.shapes.title.text = "Emergency reserve"
    group = slide.shapes.add_group_shape()
    frame = group.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    frame.text = "Native reserve policy in grouped shape"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    table.cell(0, 0).text = "Category"
    table.cell(0, 1).text = "Amount"
    table.cell(1, 0).text = "Emergency"
    table.cell(1, 1).text = "9000"
    slide.notes_slide.notes_text_frame.text = "Speaker note for auditing"
    if picture:
        slide.shapes.add_picture(io.BytesIO(picture), Inches(1), Inches(4), width=Inches(6))
    output = io.BytesIO()
    ppt.save(output)
    return output.getvalue()


@documents
def test_pdf_text_pages_persist_and_are_retrievable(tmp_path):
    (tmp_path / "policy.PDF").write_bytes(pdf_bytes("First page about insurance.", "Emergency reserve budget 9000."))
    index = load_or_build(tmp_path, tmp_path / ".index" / "index.json")
    hits = search(index, "reserve", top_k=1)
    assert hits[0].chunk.source == "policy.PDF" and hits[0].chunk.page == 2
    assert "第2页" in hits[0].chunk.breadcrumb
    assert hits[0].chunk.extraction_method == "native"
    assert load(tmp_path / ".index" / "index.json").chunks == index.chunks
    before = corpus_fingerprint(tmp_path)
    (tmp_path / "policy.PDF").write_bytes(pdf_bytes("Changed document."))
    assert corpus_fingerprint(tmp_path) != before
    assert "Changed" in load_or_build(tmp_path, tmp_path / ".index" / "index.json").chunks[0].text


@documents
def test_pptx_tables_grouped_text_notes_and_short_title_survive(tmp_path):
    path = tmp_path / "slides.pptx"
    path.write_bytes(pptx_bytes())
    chunks = load_corpus(tmp_path)
    assert any("grouped shape" in c.text for c in chunks)
    assert any("Emergency\t9000" in c.text and c.extraction_method == "table" for c in chunks)
    assert any("Speaker note" in c.text and c.extraction_method == "notes" for c in chunks)
    assert all(c.source == "slides.pptx" and c.slide == 1 for c in chunks)
    assert any("Emergency reserve" in c.text for c in chunks)
    # 同页的标题和各文本框合并，避免检索只召回标题而漏掉同页正文。
    native = [c for c in chunks if c.extraction_method == "native"]
    assert len(native) == 1 and "grouped shape" in native[0].text


@ocr
def test_local_ocr_for_image_scanned_pdf_and_embedded_slide_image(tmp_path):
    import pymupdf
    raw = image_bytes()
    path = tmp_path / "receipt.png"
    path.write_bytes(raw)
    parts = extract_document(path)
    assert "9000" in "".join(p.text for p in parts)
    assert all(p.method == "ocr" for p in parts)
    scan = pymupdf.open()
    scan.new_page(width=1000, height=230).insert_image(pymupdf.Rect(0, 0, 1000, 230), stream=raw)
    pdf_path = tmp_path / "scan.pdf"
    scan.save(pdf_path)
    scan.close()
    assert any("9000" in p.text and p.page == 1 and p.method == "ocr" for p in extract_document(pdf_path))
    deck = tmp_path / "image-slide.pptx"
    deck.write_bytes(pptx_bytes(raw))
    assert any("9000" in p.text and p.slide == 1 and p.method == "ocr" for p in extract_document(deck))


@documents
def test_pdf_mixed_page_combines_native_and_image_text(tmp_path, monkeypatch):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 50), "Native header")
    page.insert_image(pymupdf.Rect(40, 80, 400, 180), stream=image_bytes())
    path = tmp_path / "mixed.pdf"
    doc.save(path)
    doc.close()
    monkeypatch.setattr("fa.retrieval.documents.ocr_image", lambda raw: "Native header\nBudget 9000")
    parts = extract_document(path)
    assert parts[0].text.count("Native header") == 1
    assert "Budget 9000" in parts[0].text and parts[0].method == "native+ocr"


@documents
def test_extraction_cache_reused_but_content_and_ocr_mode_invalidate(tmp_path, monkeypatch):
    path = tmp_path / "p.pdf"
    path.write_bytes(pdf_bytes("Original text"))
    cache = tmp_path / ".index" / "extracted"
    calls = []
    original = __import__("fa.retrieval.documents", fromlist=["_pdf_parts"])._pdf_parts
    def recording(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr("fa.retrieval.documents._pdf_parts", recording)
    extract_document(path, cache)
    extract_document(path, cache)
    assert len(calls) == 1
    path.write_bytes(pdf_bytes("Changed text"))
    extract_document(path, cache)
    assert len(calls) == 2
    fingerprint = corpus_fingerprint(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_OCR_MODE", "off")
    assert corpus_fingerprint(tmp_path) != fingerprint
    extract_document(path, cache)
    assert len(calls) == 3


@documents
def test_corrupt_password_protected_and_legacy_missing_converter_are_explicit(tmp_path, monkeypatch):
    import pymupdf
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"broken pdf")
    with pytest.raises(DocumentError, match="解析失败"):
        extract_document(bad)
    doc = pymupdf.open()
    doc.new_page().insert_text((40, 40), "private")
    locked = tmp_path / "locked.pdf"
    doc.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="test", owner_pw="owner")
    doc.close()
    with pytest.raises(DocumentError, match="加密"):
        extract_document(locked)
    old = tmp_path / "old.ppt"
    old.write_bytes(b"legacy")
    monkeypatch.setattr("fa.retrieval.documents.shutil.which", lambda name: None)
    with pytest.raises(DocumentError, match="LibreOffice"):
        extract_document(old)


@documents
def test_failed_import_leaves_existing_knowledge_unchanged_and_duplicate_rejected(tmp_path):
    imported = import_file(tmp_path, "p.pdf", pdf_bytes("Emergency reserve budget 9000"))
    assert imported["chunks"] == 1
    before = corpus_fingerprint(tmp_path)
    with pytest.raises(DocumentError):
        import_file(tmp_path, "broken.pdf", b"broken")
    assert not (tmp_path / "uploads" / "broken.pdf").exists()
    assert corpus_fingerprint(tmp_path) == before
    with pytest.raises(FileExistsError):
        import_file(tmp_path, "p.pdf", pdf_bytes("Other text"))
    listing = inventory(tmp_path)
    assert listing["indexed"] and listing["files"][0]["chunks"] == 1


@pytest.mark.parametrize("name", ["../x.pdf", "..\\x.pdf", "x:stream.pdf", ".env", "CON.pdf", "file.exe", "x.pdf "])
def test_invalid_filenames_rejected(name):
    with pytest.raises(DocumentError):
        validate_filename(name)


def test_hidden_caches_never_become_sources(tmp_path):
    hidden = tmp_path / ".index"
    hidden.mkdir()
    (hidden / "private.pdf").write_bytes(b"not a source")
    assert load_corpus(tmp_path) == []


@documents
def test_cache_corruption_is_rebuilt_and_source_deletion_removes_hits(tmp_path):
    path = tmp_path / "policy.pdf"
    path.write_bytes(pdf_bytes("Emergency reserve budget 9000"))
    chunks = load_corpus(tmp_path)
    cache = next((tmp_path / ".index" / "extracted").glob("*.json"))
    cache.write_text('{"parts": []}', encoding="utf-8")
    assert load_corpus(tmp_path) == chunks
    index_path = tmp_path / ".index" / "index.json"
    load_or_build(tmp_path, index_path)
    path.unlink()
    assert load_or_build(tmp_path, index_path).chunks == []


@documents
def test_ocr_off_reports_scanned_pages_and_limits_are_checked(tmp_path, monkeypatch):
    from fa.retrieval import documents as module
    path = tmp_path / "image.png"
    path.write_bytes(image_bytes())
    monkeypatch.setenv("KNOWLEDGE_OCR_MODE", "off")
    with pytest.raises(DocumentError, match="需要OCR"):
        extract_document(path)
    monkeypatch.setenv("KNOWLEDGE_OCR_MODE", "auto")
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 1)
    with pytest.raises(DocumentError, match="20MB"):
        extract_document(path)


@documents
def test_upload_api_auth_download_and_path_boundaries(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from web import server
    monkeypatch.setattr(server, "_knowledge_root", lambda: tmp_path)
    monkeypatch.setattr(server, "WEB_TOKEN", "test")
    from backend.db import Database
    from backend.jobs import JobService
    from types import SimpleNamespace
    db = Database(f"sqlite:///{(tmp_path / 'jobs.sqlite3').as_posix()}")
    db.create_schema()
    jobs = JobService(db, staging_dir=tmp_path / "staging")
    monkeypatch.setattr(server, "runtime", SimpleNamespace(jobs=jobs))
    client = TestClient(server.app)
    assert client.get("/api/knowledge").status_code == 401
    assert client.post("/api/knowledge/files/p.pdf", content=pdf_bytes("Reserve 9000")).status_code == 401
    uploaded = client.post("/api/knowledge/files/p.pdf?token=test", content=pdf_bytes("Reserve 9000"))
    assert uploaded.status_code == 202 and uploaded.json()["status"] == "queued"
    assert jobs.run_once()
    result = client.get("/api/jobs/" + uploaded.json()["id"] + "?token=test").json()
    assert result["status"] == "succeeded" and result["result"]["chunks"] == 1
    conflict = client.post("/api/knowledge/files/p.pdf?token=test", content=b"x")
    assert conflict.status_code == 202
    jobs.run_once()
    assert jobs.get(conflict.json()["id"])["status"] == "failed"
    listed = client.get("/api/knowledge?token=test").json()
    assert listed["files"][0]["source"] == "uploads/p.pdf"
    assert client.get("/api/knowledge/source?token=test&path=uploads/p.pdf").content.startswith(b"%PDF")
    assert client.get("/api/knowledge/source?token=test&path=../.env").status_code == 404
    assert client.post("/api/knowledge/files/.env?token=test", content=b"x").status_code == 422
    bad = client.post("/api/knowledge/files/bad.pdf?token=test", content=b"x")
    assert bad.status_code == 202
    while jobs.run_once():
        pass
    assert jobs.get(bad.json()["id"])["status"] == "failed"
    db.dispose()
