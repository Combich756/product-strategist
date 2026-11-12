"""Bounded ingestion. Source text is evidence, never executable instructions."""

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
from bisect import bisect_right
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit, urlunsplit

from . import documents
from .state import UserError, utc_now

MAX_BYTES = 1_000_000
MAX_CHARS = 300_000
TABLE_FORMATS = {"csv", "tsv", "xlsx"}
LOCAL_FORMATS = {"txt", "md", "html", "htm"} | TABLE_FORMATS | documents.FORMATS
BLOCKS = {"article", "section", "p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "main"}
HIDDEN = {"style", "script", "template", "noscript", "head", "svg"}


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in HIDDEN:
            self.hidden.append(tag)
        elif not self.hidden and tag in BLOCKS:
            self.fragments.append("\n")
        elif not self.hidden and tag in {"td", "th"}:
            self.fragments.append(" | ")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in BLOCKS:
            self.fragments.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.fragments.append(data.replace("\n", " "))

    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.fragments).splitlines())
        return "\n".join(line for line in lines if line)


def normalize_url(url):
    try:
        parts = urlsplit(url)
        hostname = parts.hostname or ""
        if (
            parts.scheme != "https" or not re.fullmatch(r"[a-z0-9.-]+", hostname)
            or "." not in hostname or parts.netloc.lower() != hostname
            or parts.fragment or any(ord(char) < 33 for char in url)
        ):
            raise ValueError
    except ValueError:
        raise UserError("Нужен публичный HTTPS URL с доменным именем, без порта, логина и фрагмента.") from None
    return urlunsplit(("https", hostname, parts.path or "/", parts.query, ""))


def parse_html(raw, encoding):
    parser = VisibleText()
    parser.feed(raw.decode(encoding))
    parser.close()
    return parser.text()


def limited_file(path, limit=MAX_BYTES):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise UserError(f"Размер источника превышает {limit} байт.")
    return raw


def make_source(raw, text, *, origin, format_, kind, acquisition):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip() or len(text) > MAX_CHARS:
        raise UserError(f"Источник пуст или превышает {MAX_CHARS} символов. Разбейте материал.")
    return {
        "origin": origin, "format": format_, "kind": kind, "acquisition": acquisition,
        "captured_at": utc_now(), "sha256": hashlib.sha256(raw).hexdigest(),
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "status": "active", "text": text,
    }


def read_local(folder, name, kind, origin=None, *, sheet=None):
    inbox = (folder / "materials").resolve()
    path = (inbox / name).resolve()
    if not path.is_relative_to(inbox) or not path.is_file():
        raise UserError("Положите разрешённый файл в materials/ текущего кейса.")
    format_ = path.suffix.lower().lstrip(".")
    if format_ not in LOCAL_FORMATS:
        raise UserError("Поддерживаются MD, TXT, CSV, TSV, HTML, PDF, DOCX и XLSX.")
    if sheet is not None and format_ != "xlsx":
        raise UserError("--sheet допустим только для локального XLSX.")
    if kind == "public" and not origin:
        raise UserError("Для открытой копии укажите --origin с разрешённым URL.")
    if origin and kind != "public":
        raise UserError("--origin допустим только для открытой копии.")
    details = {}
    if format_ in documents.FORMATS:
        raw = limited_file(path, documents.MAX_BYTES)
        text, details = documents.extract(raw, format_, sheet=sheet, limit=MAX_CHARS)
    else:
        raw = limited_file(path)
        text = parse_html(raw, "utf-8-sig") if format_ in {"html", "htm"} else raw.decode("utf-8-sig")
    source = make_source(
        raw, text, origin=origin or f"materials/{path.relative_to(inbox).as_posix()}",
        format_=format_, kind=kind, acquisition="local_public_copy" if origin else "local_file",
    )
    return {**source, "file": path.relative_to(inbox).as_posix(), **details}


def public_addresses(host):
    addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(
        host, 443, type=socket.SOCK_STREAM,
    )))
    if not addresses:
        raise UserError("DNS не вернул адресов.")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise UserError("Сайт разрешается в непубличный IP-адрес.")
    return addresses


def fetch_web(url):
    parts = urlsplit(url)
    host = parts.hostname
    addresses = public_addresses(host)
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        path += "?" + quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")
    connection = http.client.HTTPSConnection(host, timeout=8)
    last_error = None
    context = ssl.create_default_context()
    try:
        for ip in addresses[:2]:
            try:
                with socket.create_connection((ip, 443), timeout=8) as raw_socket:
                    connection.sock = context.wrap_socket(raw_socket, server_hostname=host)
                break
            except OSError as error:
                last_error = error
        else:
            raise UserError(f"HTTPS недоступен: {last_error}")
        connection.request("GET", path, headers={"Accept": "text/html,text/plain", "User-Agent": "ProductStrategist/0.1"})
        response = connection.getresponse()
        if response.status != 200:
            raise UserError(f"Источник вернул HTTP {response.status}. Редиректы требуют отдельного разрешения URL.")
        mime = response.headers.get_content_type()
        encoding = response.headers.get_content_charset() or "utf-8"
        if mime not in {"text/plain", "text/html", "application/xhtml+xml"}:
            raise UserError(f"Формат сайта не поддерживается: {mime}")
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise UserError(f"Ответ превышает {MAX_BYTES} байт.")
        text = raw.decode(encoding) if mime == "text/plain" else parse_html(raw, encoding)
        return make_source(raw, text, origin=url, format_="txt", kind="public", acquisition="https")
    finally:
        connection.close()


def source_lines(source):
    return source["text"].split("\n")


def line_location(source, line):
    if "page_starts" in source:
        return {"page": bisect_right(source["page_starts"], line)}
    if "continuation_lines" in source:
        return {"row": line - bisect_right(source["continuation_lines"], line)}
    return {}


def metadata(source):
    return {key: value for key, value in source.items()
            if key not in {"text", "page_starts", "continuation_lines"}}
