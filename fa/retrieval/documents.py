"""多格式知识解析。只做文本提取/OCR，不生成未经原文支持的图片描述。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
from importlib.metadata import PackageNotFoundError, version
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import zipfile

SCHEME = "document-extraction-v2-slide-context"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SUFFIXES = {".md", ".pdf", ".pptx", ".ppt", *IMAGE_SUFFIXES}
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100
MAX_PIXELS = 20_000_000
_ocr_lock = threading.RLock()


class DocumentError(ValueError):
    """可直接展示给上传者的解析错误；不包含凭证或客户端状态。"""


@dataclass(frozen=True)
class Part:
    text: str
    location: str
    method: str
    page: int | None = None
    slide: int | None = None


def source_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUFFIXES
                  and not any(part.startswith(".") for part in p.relative_to(root).parts))


def parser_signature() -> str:
    mode = os.environ.get("KNOWLEDGE_OCR_MODE", "auto").strip().lower()
    if mode not in {"auto", "always", "off"}:
        raise DocumentError("KNOWLEDGE_OCR_MODE须为auto、always或off")
    packages = {}
    for package in ("PyMuPDF", "python-pptx", "Pillow", "rapidocr-onnxruntime"):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = "missing"
    return json.dumps({"scheme": SCHEME, "ocr": mode, "packages": packages}, sort_keys=True)


def _require(module: str):
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise DocumentError('缺少文档解析依赖，请执行：pip install -e ".[documents]"') from exc


@lru_cache(maxsize=1)
def _ocr_engine():
    return _require("rapidocr_onnxruntime").RapidOCR(
        intra_op_num_threads=1, inter_op_num_threads=1,
    )


def ocr_image(raw: bytes) -> str:
    """解码后再交给OCR，防止远程URL/文件路径被OCR库当成输入。"""
    Image = _require("PIL.Image")
    ImageOps = _require("PIL.ImageOps")
    numpy = _require("numpy")
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > MAX_PIXELS:
            raise DocumentError("图片超过2000万像素，请缩小后上传")
        image = ImageOps.exif_transpose(image).convert("RGB")
        with _ocr_lock:
            result, _ = _ocr_engine()(numpy.asarray(image))
    return "\n".join(str(item[1]).strip() for item in result or [] if str(item[1]).strip())


def _image_parts(raw: bytes, mode: str) -> list[Part]:
    if mode == "off":
        raise DocumentError("图片需要OCR，当前KNOWLEDGE_OCR_MODE=off")
    Image = _require("PIL.Image")
    output = []
    with Image.open(io.BytesIO(raw)) as image:
        frames = getattr(image, "n_frames", 1)
        if frames > MAX_PAGES:
            raise DocumentError("图片页数超过100页")
        for i in range(frames):
            image.seek(i)
            if image.width * image.height > MAX_PIXELS:
                raise DocumentError("图片超过2000万像素，请缩小后上传")
            frame = io.BytesIO()
            image.save(frame, format="PNG")
            text = ocr_image(frame.getvalue())
            if text.strip():
                output.append(Part(text, f"图片第{i + 1}页", "ocr", page=i + 1))
    return output


def _merge_text(native: str, recognized: str) -> str:
    # 只去除OCR重复识别的原生文本行；不去重原文中的账单/表格重复行。
    existing = {"".join(line.split()) for line in native.splitlines() if line.strip()}
    additional = [line for line in recognized.splitlines() if line.strip() and "".join(line.split()) not in existing]
    return "\n".join([native.strip(), *additional]).strip()


def _pdf_parts(raw: bytes, mode: str) -> list[Part]:
    pdf = _require("pymupdf")
    output = []
    with pdf.open(stream=raw, filetype="pdf") as document:
        if document.needs_pass:
            raise DocumentError("PDF已加密，请先解密再上传")
        if document.page_count > MAX_PAGES:
            raise DocumentError("PDF超过100页，请拆分后上传")
        for i, page in enumerate(document):
            text = page.get_text("text", sort=True).strip()
            # 有图的页面同时OCR，覆盖图文混排；纯文本PDF不跑OCR。
            needs_ocr = mode == "always" or (mode == "auto" and (not text or page.get_images()))
            method = "native"
            if needs_ocr:
                if page.rect.width * page.rect.height * (150 / 72) ** 2 > MAX_PIXELS:
                    raise DocumentError(f"PDF第{i + 1}页尺寸过大，请缩小后上传")
                pixmap = page.get_pixmap(dpi=150, alpha=False)
                recognized = ocr_image(pixmap.tobytes("png"))
                method = "native+ocr" if text else "ocr"
                text = _merge_text(text, recognized)
            if not text and mode == "off":
                raise DocumentError(f"PDF第{i + 1}页没有文字层，需启用OCR")
            if text:
                output.append(Part(text, f"第{i + 1}页", method, page=i + 1))
    return output


def _pptx_parts(path: Path, mode: str) -> list[Part]:
    pptx = _require("pptx")
    # 解压上限在打开Presentation前检查。
    with zipfile.ZipFile(path) as archive:
        if sum(info.file_size for info in archive.infolist()) > 100 * 1024 * 1024:
            raise DocumentError("PPTX解压后超过100MB，请拆分后上传")
    presentation = pptx.Presentation(str(path))
    if len(presentation.slides) > MAX_PAGES:
        raise DocumentError("PPT超过100页，请拆分后上传")
    output = []

    def walk(shapes):
        for shape in shapes:
            if hasattr(shape, "shapes"):
                yield from walk(shape.shapes)
            else:
                yield shape

    for i, slide in enumerate(presentation.slides, 1):
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None else ""
        location = f"幻灯片{i}" + (f"：{title}" if title else "")
        native_text = []
        extras = []
        for shape in walk(slide.shapes):
            if shape.has_text_frame and shape.text.strip():
                # 一个文本框常只有“预算”或一个数字；按页合并保留标签与数值上下文。
                native_text.append(shape.text.strip())
            if shape.has_table:
                text = "\n".join("\t".join(cell.text for cell in row.cells) for row in shape.table.rows)
                if text.strip():
                    extras.append(Part(text, location + " > 表格", "table", slide=i))
            if hasattr(shape, "image") and mode != "off":
                text = ocr_image(shape.image.blob)
                if text.strip():
                    extras.append(Part(text, location + " > 图片", "ocr", slide=i))
        if native_text:
            output.append(Part("\n".join(native_text), location, "native", slide=i))
        output.extend(extras)
        if slide.has_notes_slide:
            frame = slide.notes_slide.notes_text_frame
            if frame is not None and frame.text.strip():
                output.append(Part(frame.text, location + " > 备注", "notes", slide=i))
    return output


def _legacy_ppt_parts(path: Path, mode: str) -> list[Part]:
    office = shutil.which("soffice") or shutil.which("libreoffice")
    if not office:
        raise DocumentError("旧版.ppt需要LibreOffice（soffice）；也可以另存为.pptx后上传")
    with tempfile.TemporaryDirectory(prefix="finance-ppt-") as directory:
        output = Path(directory)
        # 独立配置目录避免复用用户已打开的Office进程。
        profile = (output / "profile").as_uri()
        completed = subprocess.run(
            [office, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pptx",
             "--outdir", str(output), str(path.resolve())],
            capture_output=True, timeout=90, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        target = output / (path.stem + ".pptx")
        if completed.returncode or not target.is_file():
            raise DocumentError("旧版PPT转换失败，请另存为.pptx后上传")
        return _pptx_parts(target, mode)


def extract_document(path: Path, cache_dir: Path | None = None) -> list[Part]:
    """按文件内容、解析器版本和OCR模式缓存；缓存不包含文件路径。"""
    if path.suffix.lower() not in SUFFIXES - {".md"}:
        raise DocumentError(f"不支持的文件格式：{path.suffix}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DocumentError("单个文件不能超过20MB")
    raw = path.read_bytes()
    signature = parser_signature()
    digest = hashlib.sha256(raw + path.suffix.lower().encode() + signature.encode()).hexdigest()
    cache = cache_dir / f"{digest}.json" if cache_dir else None
    if cache and cache.is_file():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if payload["key"] == digest:
                parts = [Part(**part) for part in payload["parts"]]
                if parts and all(isinstance(p.text, str) and p.text.strip() and p.location for p in parts):
                    return parts
        except (OSError, ValueError, KeyError, TypeError):
            pass
    mode = json.loads(signature)["ocr"]
    try:
        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            parts = _image_parts(raw, mode)
        elif suffix == ".pdf":
            parts = _pdf_parts(raw, mode)
        elif suffix == ".pptx":
            parts = _pptx_parts(path, mode)
        else:
            parts = _legacy_ppt_parts(path, mode)
        if not parts:
            raise DocumentError("没有提取到可检索文字；请检查图片清晰度或文档内容")
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"{path.name}解析失败（{type(exc).__name__}），请检查文件是否损坏") from exc
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"key": digest, "parts": [asdict(p) for p in parts]}, handle, ensure_ascii=False)
        try:
            temporary.replace(cache)
        finally:
            temporary.unlink(missing_ok=True)
    return parts
