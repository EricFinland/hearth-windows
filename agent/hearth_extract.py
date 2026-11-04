#!/usr/bin/env python3
"""hearth extract: turn a file the user attached into text a model can read.

The desktop chat lets a person attach files to a message (see
desktop/server/attachments.py). The model only ever sees text, so every
attached file goes through extract() here first, and what comes back is one
of two things: text that is honestly the file's content, or a status that says
plainly why there is none. The second half matters as much as the first. A
scanned PDF run through a naive extractor produces a page of glyph ids that
looks like text to every check except reading it, and a model handed that
will confidently summarise noise. So every extractor here ends in a judgement
("does this look like writing?") and a refusal is a first-class result, never
an exception and never junk passed along.

What is understood, deliberately a small set:

  * Text-like files (source code, markdown, csv, json, logs, xml, html, ...),
    by extension or by sniffing. Encoding is decided in this order: a byte
    order mark (UTF-8, UTF-16 LE/BE, UTF-32 LE/BE), then strict UTF-8, then
    cp1252 (what Windows Notepad wrote for decades). A file that decodes but
    is mostly control characters is binary wearing a .txt, and is refused.
  * .docx, via zipfile and xml.etree: the paragraphs of word/document.xml,
    with tabs and line breaks kept. Encrypted documents are recognised and
    named as such.
  * PDF, best effort: FlateDecode (or unfiltered) content streams are
    inflated with zlib and the text-showing operators (Tj, TJ, ' and ") are
    read out, with newlines on the text-positioning operators. PDFs whose
    fonts are subset or CID-encoded produce strings that are glyph numbers,
    not characters; the garbage check catches most of those and says "no
    extractable text" rather than passing them on.
  * Images and other binaries are recognised and NOT read. There is no vision
    support, and pretending otherwise by inlining base64 would only burn the
    context window.

Every extractor is bounded, because the input is a file from anywhere: a zip
bomb inside a .docx, a PDF whose streams inflate to gigabytes, or a content
stream engineered to make a tokenizer crawl. Decompression is capped in bytes
(and read in chunks, since a zip header's declared size can lie), the total
inflated PDF content is capped, and the PDF walk also stops at a wall-clock
budget. Hitting a cap is reported as truncation, not as a crash.

Standard library only. No I/O beyond reading the one file asked for.
"""

import argparse
import codecs
import io
import os
import re
import sys
import time
import unicodedata
import zipfile
import zlib
from xml.etree import ElementTree

# The largest file this module will read at all. Matches the per-file upload
# cap in desktop/server/attachments.py; a caller that hands over more gets the
# head of it and a truncation flag.
MAX_INPUT_BYTES = 20 * 1024 * 1024

# Extracted text is kept up to this many characters. The prompt budget is far
# smaller than this (see attachments.py), and the full file stays on disk for
# the agent's own read tools, so holding megabytes of text in memory to then
# cut it down to a few thousand characters would buy nothing.
MAX_TEXT_CHARS = 1_000_000

# word/document.xml is read in chunks up to this many decompressed bytes. A
# genuine 300-page manuscript is a few megabytes of XML; anything near this
# is a bomb or not a document.
MAX_DOCX_XML_BYTES = 48 * 1024 * 1024

# Total inflated bytes across every PDF content stream, and the number of
# streams looked at. Together with PDF_TIME_BUDGET these keep a hostile PDF
# from pinning a CPU or exhausting memory.
MAX_PDF_INFLATED_BYTES = 24 * 1024 * 1024
MAX_PDF_STREAMS = 4000
PDF_TIME_BUDGET = 8.0  # seconds

# How many leading bytes are sniffed to decide "text or binary" for a file
# whose extension says nothing.
SNIFF_BYTES = 8192

KIND_TEXT = "text"
KIND_DOCX = "docx"
KIND_PDF = "pdf"
KIND_IMAGE = "image"
KIND_BINARY = "binary"

STATUS_OK = "ok"              # text extracted and it looks like writing
STATUS_EMPTY = "empty"        # a readable format with nothing in it
STATUS_NO_TEXT = "no_text"    # e.g. a scanned PDF: nothing honest to show
STATUS_BINARY = "binary"      # not a text format
STATUS_IMAGE = "image"        # stored, not readable without vision support
STATUS_ENCRYPTED = "encrypted"
STATUS_ERROR = "error"        # the file is damaged or not what it claims

TEXT_EXTENSIONS = frozenset("""
txt text md markdown rst adoc org csv tsv json jsonl ndjson log xml html htm
xhtml svg yaml yml toml ini cfg conf config env properties py pyi pyw js mjs
cjs jsx ts tsx rs go java kt kts scala c h cc cpp cxx hpp hh cs fs fsx vb sh
bash zsh fish ps1 psm1 psd1 bat cmd sql rb php pl pm lua r jl swift m mm dart
ex exs erl hs ml mli clj cljs el lisp scm vue svelte css scss sass less graphql
gql proto tf hcl dockerfile makefile mk cmake gradle sbt nix diff patch tex bib
srt vtt rtf
""".split())

IMAGE_EXTENSIONS = frozenset("png jpg jpeg gif bmp webp ico tif tiff heic heif avif psd".split())

# Magic numbers for the image formats above, for files with a misleading or
# missing extension.
_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM",
    b"II*\x00", b"MM\x00*", b"\x00\x00\x01\x00", b"8BPS",
)

# An OLE compound file. Modern Office writes a password-protected .docx as one
# of these instead of a zip, so this is how "encrypted" is recognised.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _result(kind, status, text="", note="", encoding=None, truncated=False):
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        truncated = True
    return {
        "kind": kind,
        "status": status,
        "text": text,
        "chars": len(text),
        "encoding": encoding,
        "truncated": bool(truncated),
        "note": note,
    }


# --------------------------------------------------------------------------
# plain text
# --------------------------------------------------------------------------

_BOMS = (
    # UTF-32 before UTF-16: FF FE 00 00 starts with the UTF-16 LE mark too.
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _control_ratio(text):
    """Fraction of characters that are control characters other than the
    whitespace every text file legitimately contains. Real text is close to
    zero; a binary decoded through cp1252 lands near 0.1 or above."""
    if not text:
        return 0.0
    sample = text[:200_000]
    bad = 0
    for ch in sample:
        o = ord(ch)
        if (o < 32 and ch not in "\t\n\r\f\v") or o == 0x7F or o == 0xFFFD:
            bad += 1
    return bad / len(sample)


def decode_text(data):
    """Decode bytes that are supposed to be text. Returns (text, encoding),
    or (None, reason) when the bytes are not text in any encoding tried.

    BOM first, because a BOM is the file telling us. Then strict UTF-8,
    because valid UTF-8 that is not meant as UTF-8 is vanishingly rare. Then
    cp1252, which accepts nearly anything, which is exactly why the result is
    checked for control characters afterwards: a fallback that cannot fail
    has to be judged on what it produced.
    """
    for bom, enc in _BOMS:
        if data.startswith(bom):
            try:
                text = data[len(bom):].decode(enc.replace("-sig", ""))
            except UnicodeDecodeError:
                return None, "the file starts with a {} byte order mark but is not valid {}".format(
                    enc.upper(), enc.upper())
            if _control_ratio(text) > 0.02:
                return None, "the file decodes but is mostly control characters"
            return text, enc
    if b"\x00" in data[:SNIFF_BYTES * 4]:
        # NUL never appears in UTF-8 or cp1252 text. (BOM-less UTF-16 does
        # contain them; it is rare enough on disk that refusing it with an
        # honest reason beats guessing a byte order.)
        return None, "the file contains NUL bytes, so it is binary (or BOM-less UTF-16)"
    try:
        text = data.decode("utf-8")
        enc = "utf-8"
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
        enc = "cp1252"
    if _control_ratio(text) > 0.02:
        return None, "the file is mostly control characters, so it is binary"
    return text, enc


def _extract_text(data, truncated):
    text, enc = decode_text(data)
    if text is None:
        return _result(KIND_BINARY, STATUS_BINARY, note=enc)
    if not text.strip():
        return _result(KIND_TEXT, STATUS_EMPTY, note="the file is empty", encoding=enc)
    return _result(KIND_TEXT, STATUS_OK, text=text, encoding=enc, truncated=truncated)


# --------------------------------------------------------------------------
# .docx
# --------------------------------------------------------------------------

def _read_capped(zf, info, cap):
    """Read one zip member, refusing to go past `cap` decompressed bytes. The
    header's declared file_size is checked first as a cheap early out, but it
    is the attacker's to write, so the read itself is also bounded."""
    if info.file_size > cap:
        return None
    out = bytearray()
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(256 * 1024)
            if not chunk:
                break
            out += chunk
            if len(out) > cap:
                return None
    return bytes(out)


def _docx_text(xml_bytes):
    """Paragraph text from word/document.xml, in document order. iterparse
    rather than a recursive walk: nesting depth is the document's to choose,
    and a recursive walk over a hostile document hits the recursion limit."""
    pieces = []
    for _event, elem in ElementTree.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        tag = elem.tag
        if tag == _W + "t":
            if elem.text:
                pieces.append(elem.text)
        elif tag == _W + "tab":
            pieces.append("\t")
        elif tag in (_W + "br", _W + "cr"):
            pieces.append("\n")
        elif tag == _W + "p":
            pieces.append("\n")
            elem.clear()
    return "".join(pieces)


def _extract_docx(data):
    if data.startswith(_OLE_MAGIC):
        return _result(KIND_DOCX, STATUS_ENCRYPTED,
                       note="this document is password protected (or an old binary .doc), "
                            "so its text cannot be read")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        return _result(KIND_DOCX, STATUS_ERROR, note="this is not a valid .docx file (not a zip archive)")
    try:
        with zf:
            try:
                info = zf.getinfo("word/document.xml")
            except KeyError:
                return _result(KIND_DOCX, STATUS_ERROR,
                               note="this .docx has no document body (word/document.xml is missing)")
            if info.flag_bits & 0x1:
                return _result(KIND_DOCX, STATUS_ENCRYPTED, note="this document is encrypted")
            xml_bytes = _read_capped(zf, info, MAX_DOCX_XML_BYTES)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, zlib.error, EOFError) as exc:
        return _result(KIND_DOCX, STATUS_ERROR,
                       note="this .docx could not be unpacked ({})".format(type(exc).__name__))
    if xml_bytes is None:
        return _result(KIND_DOCX, STATUS_ERROR,
                       note="this .docx expands to more than {} MB of XML, which is not a "
                            "real document; it was not read".format(MAX_DOCX_XML_BYTES // (1024 * 1024)))
    # A document body never carries a DTD. Refusing one outright closes the
    # entity-expansion family of attacks before the parser is involved at all.
    head = xml_bytes[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in xml_bytes.upper():
        return _result(KIND_DOCX, STATUS_ERROR, note="this .docx contains a DTD, which Word never writes; it was not read")
    try:
        text = _docx_text(xml_bytes)
    except ElementTree.ParseError:
        return _result(KIND_DOCX, STATUS_ERROR, note="this .docx's document XML is damaged")
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    if not text.strip():
        return _result(KIND_DOCX, STATUS_EMPTY, note="the document has no text (it may be all images)")
    return _result(KIND_DOCX, STATUS_OK, text=text)


# --------------------------------------------------------------------------
# PDF, best effort
# --------------------------------------------------------------------------

_STREAM_RE = re.compile(rb"stream(?:\r\n|\n|\r)")
_LENGTH_RE = re.compile(rb"/Length\s+(\d+)(?!\s+\d+\s+R)")
_SKIP_DICT_RE = re.compile(
    rb"/Subtype\s*/(?:Image|Type1C|CIDFontType0C|OpenType|XML|Form)\b"
    rb"|/Type\s*/(?:XRef|ObjStm|Metadata|EmbeddedFile|XObject)\b"
    rb"|/Length[123]\b|/Predictor\b")
_FILTER_RE = re.compile(rb"/Filter\s*(\[[^\]]*\]|/[A-Za-z0-9]+)")

_TOKEN_RE = re.compile(
    rb"(?P<str>\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\))"
    rb"|(?P<hex><[0-9A-Fa-f\s]*>)"
    rb"|(?P<dict><<|>>)"
    rb"|(?P<open>\[)|(?P<close>\])"
    rb"|(?P<num>[+-]?(?:\d+\.?\d*|\.\d+))"
    rb"|(?P<name>/[^\s/\[\]()<>{}%]*)"
    rb"|(?P<op>[A-Za-z'\"*][A-Za-z0-9'\"*]*)"
    rb"|(?P<comment>%[^\r\n]*)",
    re.S)

_ESCAPE_RE = re.compile(rb"\\([nrtbf()\\]|[0-7]{1,3}|\r\n|\r|\n)")
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
            b"(": b"(", b")": b")", b"\\": b"\\"}


def _unescape_literal(raw):
    def sub(m):
        g = m.group(1)
        if g in _ESCAPES:
            return _ESCAPES[g]
        if g[:1] in (b"\r", b"\n"):
            return b""  # a backslash before a line break continues the string
        return bytes([int(g, 8) & 0xFF])
    return _ESCAPE_RE.sub(sub, raw)


def _pdf_string_text(raw_bytes):
    if raw_bytes.startswith(b"\xfe\xff"):
        return raw_bytes[2:].decode("utf-16-be", errors="replace")
    return raw_bytes.decode("latin-1")


def _string_token(tok):
    if tok.startswith(b"("):
        return _pdf_string_text(_unescape_literal(tok[1:-1]))
    hexdigits = re.sub(rb"\s+", b"", tok[1:-1])
    if len(hexdigits) % 2:
        hexdigits += b"0"
    try:
        return _pdf_string_text(bytes.fromhex(hexdigits.decode("ascii")))
    except ValueError:
        return ""


def _content_text(buf, deadline):
    """Text shown by one content stream's operators. A flat token loop with
    an operand stack, not a parser: only the handful of operators that show
    or move text matter, and everything else is skipped."""
    out = []
    operands = []
    in_array = None  # a list of pieces while inside [ ... ] for TJ
    pos = 0
    n = len(buf)
    count = 0
    while pos < n:
        m = _TOKEN_RE.search(buf, pos)
        if m is None:
            break
        pos = m.end()
        count += 1
        if count & 0x3FFF == 0 and time.monotonic() > deadline:
            break
        kind = m.lastgroup
        tok = m.group(kind)
        if kind in ("str", "hex"):
            s = _string_token(tok)
            if in_array is not None:
                in_array.append(s)
            else:
                operands.append(s)
        elif kind == "num":
            if in_array is not None:
                try:
                    # A large negative kern inside TJ is how most producers
                    # write a word gap.
                    if float(tok) < -180:
                        in_array.append(" ")
                except ValueError:
                    pass
            else:
                operands.append(tok)
        elif kind == "open":
            in_array = []
        elif kind == "close":
            if in_array is not None:
                operands.append(in_array)
            in_array = None
        elif kind == "op":
            op = tok
            if op == b"Tj" or op == b"'" or op == b'"':
                if op != b"Tj":
                    out.append("\n")
                strs = [o for o in operands if isinstance(o, str)]
                if strs:
                    out.append(strs[-1])
            elif op == b"TJ":
                arrays = [o for o in operands if isinstance(o, list)]
                if arrays:
                    out.append("".join(arrays[-1]))
            elif op in (b"Td", b"TD"):
                nums = [o for o in operands if isinstance(o, bytes)]
                try:
                    ty = float(nums[-1]) if nums else 0.0
                except ValueError:
                    ty = 0.0
                out.append("\n" if ty != 0 else " ")
            elif op in (b"T*", b"ET"):
                out.append("\n")
            elif op == b"ID":
                # Inline image data is raw bytes up to EI. Skip it rather than
                # tokenizing pixels into fake strings.
                end = buf.find(b"EI", pos)
                pos = n if end < 0 else end + 2
            operands = []
        # names, dict markers and comments carry no text
    return "".join(out)


def _inflate(raw, cap):
    """zlib-inflate at most `cap` bytes. Returns (data, hit_cap). A damaged
    stream yields whatever inflated before the damage, which is the most
    useful thing a best-effort reader can do with it."""
    d = zlib.decompressobj()
    try:
        data = d.decompress(raw, cap)
    except zlib.error:
        return b"", False
    return data, bool(d.unconsumed_tail)


def _looks_like_writing(text):
    """Does this read as human text rather than glyph numbers or noise?

    Deliberately simple and deliberately strict: a false "no extractable
    text" costs the user a sentence saying so and the file is still there
    for the agent's tools, while a false "looks fine" puts nonsense in front
    of a model that will treat it as the document.
    """
    body = "".join(text.split())
    if len(body) < 12:
        return False
    printable = sum(1 for ch in body if ch.isprintable() and unicodedata.category(ch)[0] != "C")
    if printable / len(body) < 0.95:
        return False
    letters = sum(1 for ch in body if ch.isalpha())
    if letters / len(body) < 0.5:
        return False
    words = re.findall(r"[^\W\d_]{2,}", text)
    if sum(len(w) for w in words) / len(body) < 0.5:
        return False
    ascii_letters = [ch for ch in body if "a" <= ch.lower() <= "z"]
    if len(ascii_letters) > len(body) * 0.6:
        vowels = sum(1 for ch in ascii_letters if ch.lower() in "aeiouy")
        share = vowels / len(ascii_letters)
        if share < 0.18 or share > 0.65:
            return False
    return True


def _extract_pdf(data, truncated_input):
    if not data.lstrip()[:5] == b"%PDF-":
        return _result(KIND_PDF, STATUS_ERROR, note="this file does not start with a PDF header")
    if re.search(rb"/Encrypt\s*(?:\d+\s+\d+\s+R|<<)", data):
        return _result(KIND_PDF, STATUS_ENCRYPTED,
                       note="this PDF is encrypted, so its text cannot be read")
    deadline = time.monotonic() + PDF_TIME_BUDGET
    budget = MAX_PDF_INFLATED_BYTES
    pieces = []
    truncated = truncated_input
    streams = 0
    for m in _STREAM_RE.finditer(data):
        if data[max(0, m.start() - 3):m.start()] == b"end":
            continue  # the tail of "endstream", not the start of a stream
        start = m.end()
        # The stream's dictionary is what sits between the object header and
        # the "stream" keyword. Bounded look-back, so a hostile file cannot
        # make each stream cost a scan of everything before it.
        head_from = max(0, m.start() - 4096)
        obj_at = data.rfind(b"obj", head_from, m.start())
        dict_bytes = data[obj_at if obj_at >= 0 else head_from:m.start()]
        if b"endobj" in dict_bytes:
            continue  # this "stream" was not a stream keyword after an object header
        streams += 1
        if streams > MAX_PDF_STREAMS or budget <= 0 or time.monotonic() > deadline:
            truncated = True
            break
        length = None
        lm = _LENGTH_RE.search(dict_bytes)
        if lm:
            length = int(lm.group(1))
            if data[start + length:start + length + 32].lstrip()[:9] != b"endstream":
                length = None
        if length is None:
            end = data.find(b"endstream", start)
            if end < 0:
                continue
            length = end - start
        if _SKIP_DICT_RE.search(dict_bytes):
            continue
        raw = data[start:start + length]
        fm = _FILTER_RE.search(dict_bytes)
        if fm:
            filters = re.findall(rb"/([A-Za-z0-9]+)", fm.group(1))
            if filters != [b"FlateDecode"]:
                continue  # images, fonts and exotic chains: not text
            content, hit = _inflate(raw, budget)
            if hit:
                truncated = True
        else:
            content, hit = raw[:budget], len(raw) > budget
        budget -= len(content)
        if b"begincmap" in content[:4096]:
            continue  # a ToUnicode map, not page content
        text = _content_text(content, deadline)
        if text.strip():
            pieces.append(text)
        if time.monotonic() > deadline:
            truncated = True
            break
    text = "\n".join(pieces)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return _result(KIND_PDF, STATUS_NO_TEXT,
                       note="no extractable text (a scanned or image-only PDF, or one whose "
                            "text is stored in a form this reader does not understand)",
                       truncated=truncated)
    if not _looks_like_writing(text):
        return _result(KIND_PDF, STATUS_NO_TEXT,
                       note="no extractable text (scanned or encoded PDF): what came out "
                            "was glyph codes rather than words, so it was not passed on",
                       truncated=truncated)
    return _result(KIND_PDF, STATUS_OK, text=text, truncated=truncated,
                   note="extracted best effort; layout, tables and columns may be scrambled")


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def _extension(name):
    base = os.path.basename(name or "").lower()
    if base in ("dockerfile", "makefile", "license", "readme", "changelog"):
        return base
    _root, ext = os.path.splitext(base)
    return ext[1:]


def _is_image(data, ext):
    if ext in IMAGE_EXTENSIONS:
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return any(data.startswith(m) for m in _IMAGE_MAGIC) and ext not in TEXT_EXTENSIONS


def extract_bytes(data, name="", truncated=False):
    """Classify and extract `data`, which came from a file called `name`.

    Returns a dict: kind, status, text, chars, encoding, truncated, note.
    Never raises on hostile input: every failure is a status with a note.
    """
    if len(data) > MAX_INPUT_BYTES:
        data = data[:MAX_INPUT_BYTES]
        truncated = True
    ext = _extension(name)
    try:
        if ext == "pdf" or (not ext and data[:5] == b"%PDF-"):
            return _extract_pdf(data, truncated)
        if ext in ("docx", "docm", "dotx"):
            return _extract_docx(data)
        if _is_image(data, ext):
            return _result(KIND_IMAGE, STATUS_IMAGE,
                           note="images are not readable by the model yet (no vision support)")
        if ext in TEXT_EXTENSIONS or ext in ("dockerfile", "makefile", "license", "readme", "changelog"):
            return _extract_text(data, truncated)
        # Unknown extension: sniff. A file that decodes cleanly is text no
        # matter what it is called; anything else is stored, not read.
        if data[:4] == b"PK\x03\x04":
            return _result(KIND_BINARY, STATUS_BINARY,
                           note="this is an archive or an Office format Hearth does not read "
                                "(only .docx is extracted)")
        sniff_text, _enc = decode_text(data[:SNIFF_BYTES])
        if sniff_text is not None:
            return _extract_text(data, truncated)
        return _result(KIND_BINARY, STATUS_BINARY, note="this is a binary file, so it is not shown to the model")
    except (MemoryError, RecursionError) as exc:
        return _result(KIND_BINARY, STATUS_ERROR,
                       note="the file could not be read safely ({})".format(type(exc).__name__))


def extract(path, name=None):
    """extract_bytes() on the file at `path`, reading at most MAX_INPUT_BYTES."""
    with open(path, "rb") as fh:
        data = fh.read(MAX_INPUT_BYTES + 1)
    truncated = len(data) > MAX_INPUT_BYTES
    return extract_bytes(data[:MAX_INPUT_BYTES], name or os.path.basename(path), truncated)


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

def _make_docx(body_xml, encrypted_flag=False, extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body>' + body_xml + '</w:body></w:document>')
        zf.writestr("word/document.xml", doc)
        for k, v in (extra or {}).items():
            zf.writestr(k, v)
    data = buf.getvalue()
    if encrypted_flag:
        # Flip the "encrypted" general-purpose bit on the document member in
        # both the local header and the central directory, which is all the
        # reader inspects before refusing.
        raw = bytearray(data)
        for sig in (b"PK\x03\x04", b"PK\x01\x02"):
            i = 0
            while True:
                i = raw.find(sig, i)
                if i < 0:
                    break
                flag_at = i + (6 if sig == b"PK\x03\x04" else 8)
                raw[flag_at] |= 0x1
                i += 4
        data = bytes(raw)
    return data


def _make_pdf(content, compress=True, extra_objects=b""):
    stream = zlib.compress(content) if compress else content
    filt = b"/Filter /FlateDecode " if compress else b""
    return (b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>\nendobj\n"
            b"4 0 obj\n<< " + filt + b"/Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream\nendobj\n" + extra_objects
            + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n")


def _self_test():
    # --- plain text and encodings ---
    r = extract_bytes("héllo wörld\n".encode("utf-8"), "a.txt")
    assert r["status"] == STATUS_OK and r["encoding"] == "utf-8" and r["text"] == "héllo wörld\n", r
    r = extract_bytes(codecs.BOM_UTF8 + "naïve".encode("utf-8"), "a.md")
    assert r["encoding"] == "utf-8-sig" and r["text"] == "naïve", r
    r = extract_bytes(codecs.BOM_UTF16_LE + "zürich, ok".encode("utf-16-le"), "a.csv")
    assert r["encoding"] == "utf-16-le" and r["text"] == "zürich, ok", r
    r = extract_bytes(codecs.BOM_UTF16_BE + "big endian".encode("utf-16-be"), "a.log")
    assert r["encoding"] == "utf-16-be" and r["text"] == "big endian", r
    r = extract_bytes(codecs.BOM_UTF32_LE + "thirty two".encode("utf-32-le"), "a.txt")
    assert r["encoding"] == "utf-32-le" and r["text"] == "thirty two", r
    # Not valid UTF-8, so cp1252: 0x93/0x94 are curly quotes, 0xE9 is e-acute.
    r = extract_bytes(b"\x93caf\xe9\x94 menu", "notes.txt")
    assert r["encoding"] == "cp1252" and r["text"] == "“café” menu", r
    # Unknown extension, sniffed as text.
    r = extract_bytes(b"key = value\n", "settings.weird")
    assert r["status"] == STATUS_OK and r["kind"] == KIND_TEXT, r
    # Binary refusal: NULs, and a control-character soup without NULs.
    r = extract_bytes(b"MZ\x90\x00\x03\x00\x00\x00" * 50, "tool.exe")
    assert r["status"] == STATUS_BINARY and r["text"] == "", r
    r = extract_bytes(bytes(range(1, 32)) * 40, "data.txt")
    assert r["status"] == STATUS_BINARY, r
    r = extract_bytes(b"   \n\n", "blank.txt")
    assert r["status"] == STATUS_EMPTY, r
    # Images are recognised by magic even with a misleading name, and never read.
    r = extract_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "photo.dat")
    assert r["kind"] == KIND_IMAGE and r["status"] == STATUS_IMAGE and "vision" in r["note"], r
    r = extract_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "x")
    assert r["kind"] == KIND_IMAGE, r
    # Output is capped.
    big = ("word " * (MAX_TEXT_CHARS // 4)).encode()
    r = extract_bytes(big, "big.txt")
    assert r["chars"] == MAX_TEXT_CHARS and r["truncated"] is True, (r["chars"], r["truncated"])

    # --- .docx ---
    body = ('<w:p><w:r><w:t>Quarterly</w:t></w:r><w:r><w:tab/><w:t xml:space="preserve">report </w:t></w:r></w:p>'
            '<w:p><w:r><w:t>line one</w:t><w:br/><w:t>line two</w:t></w:r></w:p>'
            '<w:p><w:r><w:delText>deleted words</w:delText></w:r></w:p>')
    r = extract_bytes(_make_docx(body), "q.docx")
    assert r["status"] == STATUS_OK and r["kind"] == KIND_DOCX, r
    assert r["text"] == "Quarterly\treport \nline one\nline two", repr(r["text"])
    r = extract_bytes(_make_docx(body, encrypted_flag=True), "q.docx")
    assert r["status"] == STATUS_ENCRYPTED, r
    r = extract_bytes(_OLE_MAGIC + b"\x00" * 600, "locked.docx")
    assert r["status"] == STATUS_ENCRYPTED, r
    r = extract_bytes(b"not a zip", "x.docx")
    assert r["status"] == STATUS_ERROR, r
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("other.xml", "<a/>")
    r = extract_bytes(buf.getvalue(), "x.docx")
    assert r["status"] == STATUS_ERROR and "document.xml" in r["note"], r
    r = extract_bytes(_make_docx(""), "empty.docx")
    assert r["status"] == STATUS_EMPTY, r
    # A DTD (the billion-laughs carrier) is refused before parsing.
    bomb_xml = ('<?xml version="1.0"?><!DOCTYPE w [<!ENTITY a "aaaaaaaaaa">]>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", bomb_xml)
    r = extract_bytes(buf.getvalue(), "laughs.docx")
    assert r["status"] == STATUS_ERROR and "DTD" in r["note"], r
    # Zip bomb: a member that inflates past the cap is refused, both when the
    # header admits its size and when it lies about it.
    global MAX_DOCX_XML_BYTES
    saved_cap = MAX_DOCX_XML_BYTES
    MAX_DOCX_XML_BYTES = 64 * 1024
    try:
        huge_body = "<w:p><w:r><w:t>" + ("A" * (400 * 1024)) + "</w:t></w:r></w:p>"
        bomb = _make_docx(huge_body)
        assert len(bomb) < 8 * 1024, len(bomb)  # it really is a bomb: tiny on disk
        r = extract_bytes(bomb, "bomb.docx")
        assert r["status"] == STATUS_ERROR and "expands" in r["note"], r
        # Patch the declared uncompressed size down to 10 bytes in both headers.
        raw = bytearray(bomb)
        for sig, off in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
            i = 0
            while True:
                i = raw.find(sig, i)
                if i < 0:
                    break
                name_len_at = i + (26 if sig == b"PK\x03\x04" else 28)
                name_len = int.from_bytes(raw[name_len_at:name_len_at + 2], "little")
                name_at = i + (30 if sig == b"PK\x03\x04" else 46)
                if bytes(raw[name_at:name_at + name_len]) == b"word/document.xml":
                    raw[i + off:i + off + 4] = (10).to_bytes(4, "little")
                i += 4
        r = extract_bytes(bytes(raw), "liar.docx")
        assert r["status"] == STATUS_ERROR, r
    finally:
        MAX_DOCX_XML_BYTES = saved_cap

    # --- PDF ---
    content = (b"BT /F1 12 Tf 72 720 Td (Hello, attached world.) Tj 0 -14 Td "
               b"[(The quick) -250 (brown fox) 120 (es)] TJ T* (jumps \\(over\\) the lazy dog) Tj "
               b"0 -14 Td <48656c6c6f20686578> Tj ET")
    r = extract_bytes(_make_pdf(content), "doc.pdf")
    assert r["status"] == STATUS_OK and r["kind"] == KIND_PDF, r
    assert "Hello, attached world." in r["text"], r["text"]
    assert "The quick brown foxes" in r["text"], r["text"]
    assert "jumps (over) the lazy dog" in r["text"], r["text"]
    assert "Hello hex" in r["text"], r["text"]
    assert r["text"].index("Hello, attached") < r["text"].index("The quick"), r["text"]
    # Unfiltered content streams are read too.
    r = extract_bytes(_make_pdf(b"BT (Plain stream text is here) Tj ET", compress=False), "p.pdf")
    assert r["status"] == STATUS_OK and "Plain stream text is here" in r["text"], r
    # Glyph-code garbage (what a CID-keyed font produces) is refused, not passed on.
    garbage = b"BT " + b" ".join(b"<" + bytes([0x30 + (i % 7), 0x41 + (i % 3)]).hex().encode() + b"> Tj"
                                 for i in range(200)) + b" ET"
    r = extract_bytes(_make_pdf(garbage), "scan.pdf")
    assert r["status"] == STATUS_NO_TEXT and r["text"] == "", r
    r = extract_bytes(_make_pdf(b"BT (\x01\x02\x03\x04\x05\x06\x07\x08\x0e\x0f\x10\x11\x12) Tj ET"), "c.pdf")
    assert r["status"] == STATUS_NO_TEXT, r
    # An image-only PDF has no text operators at all.
    r = extract_bytes(_make_pdf(b"q 612 0 0 792 0 0 cm /Im0 Do Q"), "image-only.pdf")
    assert r["status"] == STATUS_NO_TEXT and "scanned" in r["note"], r
    # Encrypted PDFs are named as such.
    enc = _make_pdf(content).replace(b"trailer\n<< /Root 1 0 R >>",
                                     b"trailer\n<< /Root 1 0 R /Encrypt 9 0 R >>")
    r = extract_bytes(enc, "locked.pdf")
    assert r["status"] == STATUS_ENCRYPTED, r
    r = extract_bytes(b"<html>not a pdf</html>", "fake.pdf")
    assert r["status"] == STATUS_ERROR, r
    # Two unfiltered streams: each is read once, and "endstream" is never
    # mistaken for the start of another stream.
    second = (b"5 0 obj\n<< /Length 29 >>\nstream\nBT (Second stream text) Tj ET\n"
              b"endstream\nendobj\n")
    r = extract_bytes(_make_pdf(b"BT (First stream words) Tj ET", compress=False, extra_objects=second), "two.pdf")
    assert r["text"].count("First stream words") == 1 and r["text"].count("Second stream text") == 1, r["text"]
    # Image and font streams are skipped even when Flate-compressed.
    img = (b"5 0 obj\n<< /Type /XObject /Subtype /Image /Filter /FlateDecode /Length 20 >>\nstream\n"
           + zlib.compress(b"(not text) Tj")[:20] + b"\nendstream\nendobj\n")
    r = extract_bytes(_make_pdf(content, extra_objects=img), "mixed.pdf")
    assert "not text" not in r["text"], r["text"]
    # Inline image data is skipped rather than tokenized.
    inline = b"BT (Before the picture) Tj ET BI /W 4 /H 4 ID (fake) Tj \x00\xff EI BT (After it) Tj ET"
    r = extract_bytes(_make_pdf(inline), "inline.pdf")
    assert "Before the picture" in r["text"] and "After it" in r["text"] and "fake" not in r["text"], r
    # Inflation is capped: a stream that inflates past the budget is cut off
    # and reported as truncated rather than swallowing memory.
    global MAX_PDF_INFLATED_BYTES
    saved_pdf = MAX_PDF_INFLATED_BYTES
    MAX_PDF_INFLATED_BYTES = 32 * 1024
    try:
        long_content = b"BT " + b"(Readable words keep coming here) Tj T* " * 20000 + b"ET"
        r = extract_bytes(_make_pdf(long_content), "long.pdf")
        assert r["truncated"] is True and r["status"] == STATUS_OK, (r["status"], r["truncated"])
        assert r["chars"] < 40 * 1024, r["chars"]
    finally:
        MAX_PDF_INFLATED_BYTES = saved_pdf
    # A pathological token soup finishes promptly (bounded by the budget).
    t0 = time.monotonic()
    extract_bytes(_make_pdf(b"(" * 200000 + b"a" * 200000), "soup.pdf")
    assert time.monotonic() - t0 < PDF_TIME_BUDGET + 5, "the PDF reader is not bounded"

    # --- extract() reads from disk ---
    import tempfile
    d = tempfile.mkdtemp(prefix="hearth-extract-")
    try:
        p = os.path.join(d, "readme")
        with open(p, "wb") as fh:
            fh.write(b"from disk")
        r = extract(p)
        assert r["status"] == STATUS_OK and r["text"] == "from disk", r
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    print("hearth-extract self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="hearth-extract",
                                description="Show what Hearth would read out of a file.")
    p.add_argument("path", nargs="?")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.path:
        p.error("a path is required")
    r = extract(a.path)
    print("kind={kind} status={status} chars={chars} encoding={encoding} truncated={truncated}".format(**r))
    if r["note"]:
        print("note: " + r["note"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
