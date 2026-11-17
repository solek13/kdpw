from __future__ import annotations

import hashlib
import os
import time
import urllib.request
from pathlib import Path

from . import __version__
from .common import CollectorError
from .xml_reader import MAX_ZIP_BYTES

EXPORT_URL = "https://data.kdpw.pl/oth/rze/info/api/file/getxml"
USER_AGENT = f"pykdpw/{__version__} (+https://github.com/solek13/kdpw)"
SOCKET_TIMEOUT = 60
TOTAL_TIMEOUT = 300


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(destination: Path) -> dict:
    """One request, no redirect, retry, proxy rotation or session impersonation."""
    request = urllib.request.Request(EXPORT_URL, headers={
        "User-Agent": USER_AGENT, "Accept": "application/zip", "Accept-Encoding": "identity",
    })
    opener = urllib.request.build_opener(NoRedirect())
    temporary = destination.with_suffix(".zip.part")
    deadline = time.monotonic() + TOTAL_TIMEOUT
    digest = hashlib.sha256()
    size = 0
    try:
        with opener.open(request, timeout=SOCKET_TIMEOUT) as response:
            if response.status != 200:
                raise CollectorError(f"Nieoczekiwany status HTTP: {response.status}")
            length_header = response.headers.get("Content-Length")
            expected_length = int(length_header) if length_header is not None else None
            if expected_length is not None and not 0 < expected_length <= MAX_ZIP_BYTES:
                raise CollectorError("Niepoprawny rozmiar eksportu w nagłówku HTTP.")
            # Store only non-sensitive response metadata.
            headers = {key: response.headers[key] for key in (
                "Content-Type", "Content-Length", "Content-Disposition", "Date", "Last-Modified", "ETag"
            ) if key in response.headers}
            with temporary.open("wb") as stream:
                while True:
                    if time.monotonic() > deadline:
                        raise CollectorError("Przekroczono całkowity czas pobierania (300 s).")
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_ZIP_BYTES:
                        raise CollectorError("Eksport przekracza limit 128 MiB.")
                    stream.write(block)
                    digest.update(block)
                stream.flush()
                os.fsync(stream.fileno())
            if expected_length is not None and size != expected_length:
                raise CollectorError("Pobrano niepełny plik eksportu.")
            if size == 0:
                raise CollectorError("Pobrano pusty plik.")
        temporary.replace(destination)
        return {"zip_sha256": digest.hexdigest(), "zip_bytes": size,
                "http_status": 200, "response_headers": headers}
    finally:
        temporary.unlink(missing_ok=True)
