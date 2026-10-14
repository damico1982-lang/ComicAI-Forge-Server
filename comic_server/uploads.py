"""Private, bounded file storage and explicit AI reference extraction."""
import hashlib
import io
import json
import warnings
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageOps
from pypdf import PdfReader

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_USER_BYTES = 1024 * 1024 * 1024
TEXT_EXTENSIONS = {"txt", "md", "csv", "json", "xml", "yaml", "yml", "log", "srt"}


def inspect_file(path, name, mime):
    """Return AI-ready data without altering the original upload. Never execute or extract arbitrary archives."""
    suffix = name.rsplit(".", 1)[-1].lower()
    try:
        if mime in {"image/jpeg", "image/png", "image/webp"} or suffix in {"jpg", "jpeg", "png", "webp"}:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as source:
                    if source.width * source.height > 40_000_000:
                        raise ValueError("Photo exceeds 40 megapixels")
                    source.load()
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    image.thumbnail((2048, 2048))
                    out = io.BytesIO(); image.save(out, "JPEG", quality=94)
                    return "image", out.getvalue(), None
        if suffix == "pdf":
            reader = PdfReader(path, strict=False)
            if reader.is_encrypted:
                raise ValueError("Encrypted PDFs cannot be read as story references")
            if len(reader.pages) > 200:
                raise ValueError("Use a PDF with 200 pages or fewer")
            parts = []; length = 0
            for page in reader.pages:
                text = page.extract_text() or ""; length += len(text)
                if length > 60000:
                    raise ValueError("Document text exceeds 60,000 characters")
                parts.append(text)
            text = "\n".join(parts)
        elif suffix == "docx":
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 2 * 1024 * 1024:
                    raise ValueError("Document text is too large")
                raw = archive.read(info)
                if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                    raise ValueError("Unsupported document markup")
                document = ElementTree.fromstring(raw)
                namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                text = "\n".join("".join(t.text or "" for t in p.iter(namespace + "t"))
                    for p in document.iter(namespace + "p"))
        elif suffix in TEXT_EXTENSIONS or mime.startswith("text/"):
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("Use a UTF-8 reference document smaller than 1 MB")
            text = path.read_text(encoding="utf-8-sig")
            if "\x00" in text:
                raise ValueError("Binary data is not a text document")
        else:
            return "attachment", None, "Stored as an attachment; this format is not sent to the AI."
        if not text.strip():
            raise ValueError("No readable text found. For scanned PDFs, add page photos or an OCR text file")
        if len(text) > 60000:
            raise ValueError("Document text exceeds 60,000 characters")
        return "text", text, None
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else "This file cannot be decoded as an AI reference"
        return "attachment", None, message[:200] + ". The original file is still stored."


def metadata(row):
    return {"id": row["id"], "name": row["name"], "mimeType": row["mime"], "size": row["size"],
        "sha256": row["sha256"], "kind": row["kind"], "notice": row["notice"]}


def references(store, username, selected):
    output = []; total_text = 0; images = 0
    for ref in selected:
        if not ref.useForAi:
            continue
        row = store.upload(str(ref.id), username)
        if row is None:
            raise ValueError("An attached file is missing")
        kind, content, notice = inspect_file(store.uploads / row["disk_name"], row["name"], row["mime"])
        if kind == "attachment":
            raise ValueError(f"{row['name']}: {notice}")
        item = {"name": row["name"], "label": ref.label, "notes": ref.notes, "role": ref.role, "kind": kind}
        if kind == "image":
            images += 1
            if images > 8:
                raise ValueError("Use up to eight likeness/location photos per generation")
            item["image"] = content
        else:
            total_text += len(content)
            if total_text > 60000:
                raise ValueError("Combined reference document text must fit within 60,000 characters")
            item["text"] = content
        output.append(item)
    return output
