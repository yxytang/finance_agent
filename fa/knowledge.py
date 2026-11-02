"""用户侧知识文件导入，原件保留在knowledge/uploads，解析完成后才提交。"""

from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import re
import tempfile
import threading

from fa.retrieval.chunk import chunk_markdown
from fa.retrieval.documents import DocumentError, MAX_FILE_BYTES, SUFFIXES, extract_document, source_files
from fa.retrieval.index import corpus_fingerprint, load, load_or_build

_lock = threading.RLock()


def validate_filename(name: str) -> str:
    if (not name or len(name) > 160 or name != name.strip() or name.endswith((".", " "))
            or name.startswith(".") or re.search(r'[\\/:*?"<>|\x00-\x1f]', name)
            or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}):
        raise DocumentError("文件名无效，请使用不含路径和特殊字符的普通文件名")
    if Path(name).suffix.lower() not in SUFFIXES:
        raise DocumentError("支持MD、PDF、PPTX、PPT、PNG、JPG、WEBP、BMP和TIFF文件")
    return name


def import_file(root: Path, name: str, content: bytes) -> dict:
    validate_filename(name)
    if not content:
        raise DocumentError("不能上传空文件")
    if len(content) > MAX_FILE_BYTES:
        raise DocumentError("单个文件不能超过20MB")
    root = root.resolve()
    destination = root / "uploads" / name
    # uploads不能通过软链接跳出知识库。
    if not destination.resolve().is_relative_to(root):
        raise DocumentError("上传目录不在知识库内")
    with _lock:
        if destination.exists():
            raise FileExistsError("同名文件已存在，请重命名后上传")
        staging = root / ".index" / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=staging, suffix=Path(name).suffix, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        try:
            if destination.suffix.lower() == ".md":
                try:
                    text = content.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise DocumentError("Markdown需要UTF-8编码") from exc
                parts = chunk_markdown(text, f"uploads/{name}")
                if not parts:
                    raise DocumentError("Markdown没有足够的正文（至少30个字符）")
                temporary.write_text(text, encoding="utf-8")
            else:
                # 缓存以内容为键，提交原件后不必再次OCR。
                parts = extract_document(temporary, root / ".index" / "extracted")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # 同文件系统原子发布，目标已存在时失败，避免并发导入覆盖原件。
            os.link(temporary, destination)
            try:
                index = load_or_build(root, root / ".index" / "index.json")
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            chunks = [c for c in index.chunks if c.source == destination.relative_to(root).as_posix()]
            return {"source": destination.relative_to(root).as_posix(), "name": name,
                    "parts": len(parts), "chunks": len(chunks), "size": len(content),
                    "locations": list(dict.fromkeys(c.breadcrumb for c in chunks)),
                    "methods": sorted({c.extraction_method or "markdown" for c in chunks})}
        finally:
            temporary.unlink(missing_ok=True)


def inventory(root: Path) -> dict:
    cached = load(root / ".index" / "index.json")
    fresh = cached.fingerprint == corpus_fingerprint(root)
    counts = Counter(c.source for c in cached.chunks) if fresh else Counter()
    files = [{"source": p.relative_to(root).as_posix(), "name": p.name,
              "format": p.suffix.lower().lstrip("."), "size": p.stat().st_size,
              "chunks": counts[p.relative_to(root).as_posix()] if fresh else None}
             for p in source_files(root)]
    return {"files": files, "indexed": fresh, "max_file_bytes": MAX_FILE_BYTES}


def main(argv=None):
    from fa.config import KNOWLEDGE_DIR
    parser = argparse.ArgumentParser(description="将多格式知识文件解析、切块并入库")
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--root", type=Path, default=KNOWLEDGE_DIR)
    parser.add_argument("--ocr", choices=("auto", "always", "off"))
    parser.add_argument("--rebuild", action="store_true", help="扫描知识目录并更新词法索引")
    args = parser.parse_args(argv)
    if args.ocr:
        os.environ["KNOWLEDGE_OCR_MODE"] = args.ocr
    if not args.files and not args.rebuild:
        parser.error("请提供文件或使用--rebuild")
    errors = 0
    for path in args.files:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise DocumentError("单个文件不能超过20MB")
            result = import_file(args.root, path.name, path.read_bytes())
            print(f"已入库 {result['source']}：{result['chunks']}块，方式：{'、'.join(result['methods'])}")
        except (OSError, DocumentError) as exc:
            errors += 1
            print(f"入库失败 {path.name}：{exc}")
    if args.rebuild:
        try:
            index = load_or_build(args.root, args.root / ".index" / "index.json")
            print(f"索引就绪：{len(index.chunks)}块")
        except (OSError, DocumentError) as exc:
            errors += 1
            print(f"重建失败：{exc}")
    return int(errors > 0)


if __name__ == "__main__":
    raise SystemExit(main())
