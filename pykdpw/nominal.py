from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from decimal import Decimal, localcontext
from itertools import groupby
from operator import itemgetter
from pathlib import Path

from .common import CollectorError

DECIMAL = re.compile(r"[+]?\d+(?:\.\d+)?\Z", re.ASCII)
CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)

ISSUER_NOMINAL_SCHEMA = """
CREATE TABLE latest_issuer_nominals (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    report_date TEXT, issuer_name TEXT NOT NULL, issuer_lei TEXT, issuer_nip TEXT,
    security_type TEXT NOT NULL, nominal_currency TEXT, nominal_amount TEXT,
    included_records INTEGER NOT NULL, excluded_records INTEGER NOT NULL
);
CREATE VIEW issuer_bond_nominal AS SELECT * FROM latest_issuer_nominals WHERE security_type = 'BOND';
CREATE VIEW issuer_covered_bond_nominal AS SELECT * FROM latest_issuer_nominals WHERE security_type = 'MRBD';
"""


def refresh_issuer_nominals(db: sqlite3.Connection, run_id: int):
    """Keep one exact snapshot readable by plain SQLite clients."""
    rows = db.execute("""
        SELECT report_date, issuer_name, issuer_lei, issuer_nip, security_type, nominal_amount,
               COALESCE('lei:' || NULLIF(issuer_lei, ''), 'nip:' || NULLIF(issuer_nip, ''),
                        'name:' || issuer_name) AS issuer_key,
               CASE WHEN nominal_currency GLOB '[A-Z][A-Z][A-Z]' THEN nominal_currency END AS currency
        FROM daily_instruments
        WHERE run_id = ? AND status = 'ACTV' AND security_type IN ('BOND', 'MRBD')
        ORDER BY issuer_key, security_type, currency, issuer_ordinal, instrument_ordinal
    """, (run_id,))
    db.execute("DELETE FROM latest_issuer_nominals")
    with localcontext() as context:
        context.prec = 50
        for (_, kind, currency), group in groupby(rows, itemgetter("issuer_key", "security_type", "currency")):
            total = Decimal(0)
            included = excluded = 0
            for row in group:
                amount = row["nominal_amount"]
                if currency and amount and DECIMAL.fullmatch(amount):
                    total += Decimal(amount)
                    included += 1
                else:
                    excluded += 1
            db.execute("INSERT INTO latest_issuer_nominals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (run_id, row["report_date"], row["issuer_name"], row["issuer_lei"], row["issuer_nip"],
                        kind, currency, format(total, "f") if included else None, included, excluded))


def nominal_report(data_dir: Path, run_id: int | None = None) -> dict:
    """Sum published whole-issue nominal amounts for one observation, offline."""
    path = (data_dir / "rze.sqlite3").resolve()
    if not path.exists():
        raise CollectorError("Brak bazy. Najpierw pobierz lub importuj raport RZE.")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        run = db.execute(
            "SELECT r.id, r.observation_date, p.report_date FROM runs r "
            "JOIN reports p ON p.id = r.report_id WHERE r.status = 'complete' "
            + ("AND r.id = ? " if run_id is not None else "")
            + "ORDER BY r.id DESC LIMIT 1", (run_id,) if run_id is not None else (),
        ).fetchone()
        if run is None:
            raise CollectorError("Nie znaleziono zakończonej obserwacji dla raportu nominału.")
        rows = db.execute(
            "SELECT issuer_lei, issuer_nip, issuer_name, security_id, status, "
            "nominal_amount, nominal_currency FROM daily_bonds WHERE run_id = ?",
            (run["id"],),
        )
        statuses = Counter()
        identities = Counter()
        issues = Counter({"missing_nominal": 0, "invalid_nominal": 0,
                          "missing_or_invalid_currency": 0, "unknown_status": 0})
        totals = defaultdict(Decimal)
        counts = Counter()
        inactive_positive = 0
        # The source's decimal amounts stay decimal throughout the calculation.
        with localcontext() as context:
            context.prec = 50
            for row in rows:
                status = row["status"]
                statuses[status] += 1
                literal = row["nominal_amount"]
                if status == "NACT":
                    if literal is not None and DECIMAL.fullmatch(literal) and Decimal(literal) > 0:
                        inactive_positive += 1
                    continue
                if status != "ACTV":
                    issues["unknown_status"] += 1
                    continue
                # Flag repeated published identities; never guess which row to drop.
                identity = (row["issuer_lei"], row["issuer_nip"], row["issuer_name"], row["security_id"])
                identities[identity] += 1
                if literal is None or not literal.strip():
                    issues["missing_nominal"] += 1
                    continue
                if not DECIMAL.fullmatch(literal):
                    issues["invalid_nominal"] += 1
                    continue
                currency = row["nominal_currency"]
                if currency is None or not CURRENCY.fullmatch(currency):
                    issues["missing_or_invalid_currency"] += 1
                    continue
                totals[currency] += Decimal(literal)
                counts[currency] += 1
        duplicate_counts = [count for count in identities.values() if count > 1]
        quality = {**issues, "duplicate_identity_groups": len(duplicate_counts),
                   "duplicate_extra_records": sum(count - 1 for count in duplicate_counts)}
        return {
            "status": "partial" if any(quality.values()) else "complete",
            "scope": "published_nominal_of_ACTV_BOND_records_in_RZE",
            "run_id": run["id"], "observation_date": run["observation_date"],
            "report_date": run["report_date"],
            "active_records": statuses["ACTV"], "included_records": sum(counts.values()),
            "excluded_inactive_records": statuses["NACT"],
            "inactive_records_with_positive_nominal": inactive_positive,
            "by_currency": [{"currency": currency, "nominal_amount": format(totals[currency], "f"),
                             "included_records": counts[currency]} for currency in sorted(totals)],
            "quality": quality,
        }
