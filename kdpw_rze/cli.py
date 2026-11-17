from __future__ import annotations

import argparse
import http.client
import io
import json
import sqlite3
import sys
import urllib.error
import zipfile
from pathlib import Path
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException

from . import __version__
from .collector import Collector
from .common import CollectorError


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Archiwum i historia obligacji KDPW RZE.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="Katalog archiwum i SQLite (domyślnie ./data).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("fetch", "Pobierz eksport: maksymalnie jedna próba na dzień."),
        ("import", "Importuj lokalny ZIP bez internetu."),
        ("status", "Pokaż stan pobierania, ostatni raport i błędy."),
        ("resume", "Usuń ręczne wstrzymanie po HTTP 401/403; zachowaj limity."),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS)
        if name == "import":
            command.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    collector = Collector(args.data_dir, progress=lambda message: print(message, file=sys.stderr, flush=True))
    try:
        if args.command == "fetch":
            result = collector.fetch()
        elif args.command == "import":
            result = collector.import_local(args.archive)
        elif args.command == "resume":
            result = collector.resume()
        else:
            result = collector.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") in {"paused", "cooldown"} else 0
    except KeyboardInterrupt:
        print("Przerwano. Limit pobierania pozostaje zachowany; sprawdź status.", file=sys.stderr)
        return 130
    except (CollectorError, OSError, ValueError, sqlite3.Error, urllib.error.URLError, http.client.HTTPException,
            zipfile.BadZipFile, ParseError, DefusedXmlException) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
