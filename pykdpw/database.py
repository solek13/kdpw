from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .common import CollectorError, canonical_json, content_hash, day, timestamp
from .nominal import ISSUER_NOMINAL_SCHEMA, refresh_issuer_nominals
from .xml_reader import ReportReader, currency, report_date, value

SCHEMA_VERSION = 3
REPORTS_SCHEMA = """
CREATE TABLE reports (
    id INTEGER PRIMARY KEY,
    xml_sha256 TEXT,
    report_date TEXT, created_at_source TEXT,
    header_json TEXT NOT NULL DEFAULT '{}',
    counts_json TEXT NOT NULL DEFAULT '{}',
    xml_bytes INTEGER,
    payments_included INTEGER NOT NULL CHECK (payments_included IN (0, 1)),
    all_instruments INTEGER NOT NULL DEFAULT 1 CHECK (all_instruments IN (0, 1)),
    UNIQUE (xml_sha256, payments_included, all_instruments)
);
CREATE INDEX reports_date ON reports(report_date);
"""
SCHEMA = """
CREATE TABLE schema_info (version INTEGER NOT NULL);
INSERT INTO schema_info VALUES (3);
CREATE TABLE control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_attempt_at TEXT, last_attempt_day TEXT,
    paused_reason TEXT, blocked_until TEXT
);
INSERT INTO control (id) VALUES (1);
""" + REPORTS_SCHEMA + """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL, observation_date TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('network', 'local')),
    source_location TEXT NOT NULL,
    status TEXT NOT NULL,
    archive_path TEXT, zip_sha256 TEXT, zip_bytes INTEGER,
    http_status INTEGER, response_headers_json TEXT,
    report_id INTEGER REFERENCES reports(id),
    finished_at TEXT, error TEXT
);
CREATE INDEX runs_observation_date ON runs(observation_date);
CREATE INDEX runs_zip_hash ON runs(zip_sha256);
CREATE TABLE issuer_versions (
    id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL UNIQUE,
    lei TEXT, nip TEXT, name TEXT NOT NULL, security_group TEXT,
    registered_debt_pln TEXT, reported_debt_pln TEXT, total_debt_pln TEXT,
    required_amount_pln TEXT, unpaid_amount_pln TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX issuers_lei ON issuer_versions(lei);
CREATE INDEX issuers_nip ON issuer_versions(nip);
CREATE TABLE instrument_versions (
    id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL UNIQUE,
    security_id TEXT NOT NULL, short_name TEXT, security_type TEXT NOT NULL,
    status TEXT, form TEXT, cfi TEXT, fisn TEXT, bond_type TEXT,
    registration_type TEXT, issue_year TEXT, maturity_date TEXT, maturity_code TEXT,
    nominal_amount TEXT, nominal_currency TEXT, quantity TEXT,
    required_amount TEXT, required_currency TEXT,
    unpaid_amount TEXT, unpaid_currency TEXT, fixed_rate TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX instruments_security_id ON instrument_versions(security_id);
"""

HISTORY_SCHEMA = """
CREATE TABLE payment_history (
    security_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    first_id INTEGER NOT NULL REFERENCES instrument_versions(id),
    end_id INTEGER REFERENCES instrument_versions(id),
    event_code TEXT, record_date TEXT, payment_date TEXT, interest_rate TEXT,
    expected_amount TEXT, expected_currency TEXT, expected_quantity TEXT,
    settled_date TEXT, settled_amount TEXT, settled_currency TEXT, settled_quantity TEXT,
    updated_date TEXT, timely_update TEXT, payload_json TEXT NOT NULL,
    PRIMARY KEY (security_id, ordinal, first_id)
) WITHOUT ROWID;
CREATE UNIQUE INDEX current_payments ON payment_history(security_id, ordinal) WHERE end_id IS NULL;
CREATE INDEX payments_date ON payment_history(payment_date);
CREATE VIEW payments AS
SELECT v.id AS instrument_version_id, h.ordinal,
       h.event_code, h.record_date, h.payment_date, h.interest_rate,
       h.expected_amount, h.expected_currency, h.expected_quantity, h.settled_date,
       h.settled_amount, h.settled_currency, h.settled_quantity, h.updated_date,
       h.timely_update, h.payload_json
FROM instrument_versions v JOIN payment_history h ON h.security_id = v.security_id
 AND h.first_id <= v.id AND (h.end_id IS NULL OR v.id < h.end_id);

CREATE TABLE issuer_history (
    ordinal INTEGER NOT NULL, first_id INTEGER NOT NULL REFERENCES reports(id),
    end_id INTEGER REFERENCES reports(id), issuer_version_id INTEGER NOT NULL REFERENCES issuer_versions(id),
    PRIMARY KEY (ordinal, first_id)
) WITHOUT ROWID;
CREATE UNIQUE INDEX current_issuers ON issuer_history(ordinal) WHERE end_id IS NULL;
CREATE VIEW report_issuers AS
SELECT r.id AS report_id, h.ordinal, h.issuer_version_id
FROM reports r JOIN issuer_history h ON h.first_id <= r.id AND (h.end_id IS NULL OR r.id < h.end_id);

CREATE TABLE instrument_history (
    issuer_ordinal INTEGER NOT NULL, instrument_ordinal INTEGER NOT NULL,
    first_id INTEGER NOT NULL REFERENCES reports(id), end_id INTEGER REFERENCES reports(id),
    instrument_version_id INTEGER NOT NULL REFERENCES instrument_versions(id),
    PRIMARY KEY (issuer_ordinal, instrument_ordinal, first_id)
) WITHOUT ROWID;
CREATE UNIQUE INDEX current_instruments ON instrument_history(issuer_ordinal, instrument_ordinal) WHERE end_id IS NULL;
CREATE VIEW report_instruments AS
SELECT r.id AS report_id, h.issuer_ordinal, h.instrument_ordinal, h.instrument_version_id
FROM reports r JOIN instrument_history h ON h.first_id <= r.id AND (h.end_id IS NULL OR r.id < h.end_id);
"""

DAILY_VIEWS = """
CREATE VIEW daily_instruments AS
SELECT r.id AS run_id, r.observation_date, r.started_at AS observed_at,
       p.id AS report_id, p.report_date,
       ri.issuer_ordinal, ri.instrument_ordinal,
       i.id AS issuer_version_id, i.lei AS issuer_lei,
       i.nip AS issuer_nip, i.name AS issuer_name,
       v.id AS instrument_version_id, v.*
FROM runs r
JOIN reports p ON p.id = r.report_id
JOIN report_instruments ri ON ri.report_id = p.id
JOIN report_issuers ir ON ir.report_id = ri.report_id AND ir.ordinal = ri.issuer_ordinal
JOIN issuer_versions i ON i.id = ir.issuer_version_id
JOIN instrument_versions v ON v.id = ri.instrument_version_id
WHERE r.status = 'complete';
CREATE VIEW daily_bonds AS SELECT * FROM daily_instruments WHERE security_type = 'BOND';
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        try:
            tables = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if not tables:
                self.connection.executescript("BEGIN;\n" + SCHEMA + HISTORY_SCHEMA + DAILY_VIEWS
                                              + ISSUER_NOMINAL_SCHEMA + "\nCOMMIT;")
            version = self.connection.execute("SELECT version FROM schema_info").fetchone()
            if version and version[0] == 1:
                self._migrate_history()
            if version and version[0] in (1, 2):
                self._migrate_reports()
            elif version is None or version[0] != SCHEMA_VERSION:
                raise CollectorError("Nieobsługiwana wersja bazy SQLite.")
        except Exception:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def recover_interrupted(self, now: datetime) -> list[int]:
        rows = self.connection.execute(
            "SELECT id FROM runs WHERE status IN ('started', 'downloaded', 'importing')"
        ).fetchall()
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?, "
                "error = 'Poprzedni proces zakończył się przed ukończeniem importu.' "
                "WHERE status IN ('started', 'downloaded', 'importing')", (timestamp(now),)
            )
        return [row[0] for row in rows]

    def control(self) -> dict:
        return dict(self.connection.execute("SELECT * FROM control WHERE id = 1").fetchone())

    def set_control(self, **fields):
        allowed = {"last_attempt_at", "last_attempt_day", "paused_reason", "blocked_until"}
        if not fields.keys() <= allowed:
            raise ValueError("Unknown control field")
        with self.connection:
            self.connection.execute(
                "UPDATE control SET " + ", ".join(f"{key} = ?" for key in fields) + " WHERE id = 1",
                tuple(fields.values()),
            )

    def new_run(self, now: datetime, source: str, location: str) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO runs (started_at, observation_date, source, source_location, status) "
                "VALUES (?, ?, ?, ?, 'started')", (timestamp(now), day(now), source, location)
            )
        return cursor.lastrowid

    def update_run(self, run_id: int, **fields):
        allowed = {"status", "archive_path", "zip_sha256", "zip_bytes", "http_status",
                   "response_headers_json", "report_id", "finished_at", "error"}
        if not fields.keys() <= allowed:
            raise ValueError("Unknown run field")
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET " + ", ".join(f"{key} = ?" for key in fields) + " WHERE id = ?",
                (*fields.values(), run_id),
            )

    def run(self, run_id: int) -> dict:
        row = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        result = dict(row)
        result["response_headers"] = json.loads(result.pop("response_headers_json") or "{}")
        if result["report_id"] is not None:
            report = dict(self.connection.execute(
                "SELECT * FROM reports WHERE id = ?", (result["report_id"],)
            ).fetchone())
            report["counts"] = json.loads(report.pop("counts_json"))
            report["header"] = json.loads(report.pop("header_json"))
            result["report"] = report
        return result

    def _version(self, table: str, digest: str, columns: dict) -> tuple[int, bool]:
        row = self.connection.execute(
            f"SELECT id FROM {table} WHERE content_hash = ?", (digest,)
        ).fetchone()
        if row:
            return row[0], False
        fields = {"content_hash": digest, **columns}
        cursor = self.connection.execute(
            f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            tuple(fields.values()),
        )
        return cursor.lastrowid, True

    def _record_membership(self, report_id: int, issuers: dict, instruments: dict):
        # Positions identify slots in the published XML, including duplicate securities.
        for table, positions, version, incoming in (
            ("issuer_history", ("ordinal",), "issuer_version_id", issuers),
            ("instrument_history", ("issuer_ordinal", "instrument_ordinal"), "instrument_version_id", instruments),
        ):
            columns = ", ".join(positions)
            current = {tuple(row[:-1]): row[-1] for row in self.connection.execute(
                f"SELECT {columns}, {version} FROM {table} WHERE end_id IS NULL"
            )}
            match = " AND ".join(f"{key} = ?" for key in positions)
            self.connection.executemany(
                f"UPDATE {table} SET end_id = ? WHERE {match} AND end_id IS NULL",
                ((report_id, *key) for key, old in current.items() if incoming.get(key) != old),
            )
            placeholders = ", ".join("?" for _ in range(len(positions) + 2))
            self.connection.executemany(
                f"INSERT INTO {table} ({columns}, first_id, {version}) VALUES ({placeholders})",
                ((*key, report_id, new) for key, new in incoming.items() if current.get(key) != new),
            )

    def _record_payments(self, instrument_id: int, security_id: str, payments: list):
        # End any omitted slots before publishing a shorter or calendar-free version.
        self.connection.execute(
            "UPDATE payment_history SET end_id = ? WHERE security_id = ? AND ordinal > ? AND end_id IS NULL",
            (instrument_id, security_id, len(payments)),
        )
        if not payments:
            return
        current = dict(self.connection.execute(
            "SELECT ordinal, payload_json FROM payment_history WHERE security_id = ? AND end_id IS NULL",
            (security_id,),
        ))
        for ordinal, p in enumerate(payments, 1):
            payload = canonical_json(p)
            old = current.get(ordinal)
            if old == payload:
                continue
            if old is not None:
                self.connection.execute(
                    "UPDATE payment_history SET end_id = ? WHERE security_id = ? AND ordinal = ? AND end_id IS NULL",
                    (instrument_id, security_id, ordinal),
                )
            self.connection.execute(
                "INSERT INTO payment_history VALUES (" + ",".join("?" for _ in range(18)) + ")",
                (security_id, ordinal, instrument_id, None,
                 value(p, "EvtCd"), value(p, "RcrdDt"), value(p, "PmtDt"), value(p, "IntrstRate"),
                 value(p, "ExpctdPmtVal"), currency(p, "ExpctdPmtVal"), value(p, "ExpctQty"),
                 value(p, "SttldPmtDt"), value(p, "SttldPmtVal"), currency(p, "SttldPmtVal"), value(p, "SttldQty"),
                 value(p, "UpdtDt"), value(p, "OnTmInd"), payload),
            )

    def _migrate_history(self):
        # Keep the old tables until all their snapshots have been replayed. The
        # schema switch and the data conversion commit together under the collector lock.
        self.connection.executescript("""
            BEGIN IMMEDIATE;
            DROP VIEW daily_bonds;
            DROP INDEX payments_date;
            ALTER TABLE payments RENAME TO old_payments;
            ALTER TABLE report_issuers RENAME TO old_issuers;
            ALTER TABLE report_instruments RENAME TO old_instruments;
        """ + HISTORY_SCHEMA + DAILY_VIEWS)
        for report_id, in self.connection.execute("SELECT id FROM reports ORDER BY id"):
            issuers = {(slot,): version for slot, version in self.connection.execute(
                "SELECT ordinal, issuer_version_id FROM old_issuers WHERE report_id = ?", (report_id,),
            )}
            instruments = {(issuer, slot): version for issuer, slot, version in self.connection.execute(
                "SELECT issuer_ordinal, instrument_ordinal, instrument_version_id FROM old_instruments "
                "WHERE report_id = ?", (report_id,),
            )}
            self._record_membership(report_id, issuers, instruments)
        for instrument_id, security_id in self.connection.execute(
            "SELECT id, security_id FROM instrument_versions ORDER BY id"
        ):
            payments = [json.loads(row[0]) for row in self.connection.execute(
                "SELECT payload_json FROM old_payments WHERE instrument_version_id = ? ORDER BY ordinal",
                (instrument_id,),
            )]
            self._record_payments(instrument_id, security_id, payments)
        for table in ("old_payments", "old_instruments", "old_issuers"):
            self.connection.execute(f"DROP TABLE {table}")
        self.connection.execute("UPDATE schema_info SET version = 2")
        self.connection.commit()

    def _migrate_reports(self):
        # Keep report IDs and their references while replacing the old unique key.
        self.connection.executescript("""
            PRAGMA foreign_keys = OFF;
            BEGIN IMMEDIATE;
            DROP VIEW daily_bonds;
            DROP VIEW IF EXISTS daily_instruments;
            CREATE TEMP TABLE old_reports AS SELECT * FROM reports;
            DROP TABLE reports;
        """ + REPORTS_SCHEMA + DAILY_VIEWS + ISSUER_NOMINAL_SCHEMA + """
            INSERT INTO reports (id, xml_sha256, report_date, created_at_source, header_json,
                                 counts_json, xml_bytes, payments_included, all_instruments)
            SELECT id, xml_sha256, report_date, created_at_source, header_json,
                   counts_json, xml_bytes, 1, 0 FROM old_reports;
            DROP TABLE old_reports;
            UPDATE schema_info SET version = 3;
        """)
        latest = self.connection.execute("SELECT MAX(id) FROM runs WHERE status = 'complete'").fetchone()[0]
        if latest is not None:
            refresh_issuer_nominals(self.connection, latest)
        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys = ON")

    def _complete_import(self, run_id: int, report_id: int, now: datetime):
        self.connection.execute(
            "UPDATE runs SET status = 'complete', report_id = ?, finished_at = ?, error = NULL WHERE id = ?",
            (report_id, timestamp(now), run_id),
        )
        refresh_issuer_nominals(self.connection, run_id)

    def import_report(self, path: Path, zip_sha256: str, run_id: int, now_fn, *, include_payments=False) -> int:
        """Publish all report rows and the successful run in a single transaction."""
        previous = self.connection.execute(
            "SELECT r.report_id FROM runs r JOIN reports p ON p.id = r.report_id "
            "WHERE r.zip_sha256 = ? AND r.status = 'complete' AND p.payments_included = ? "
            "AND p.all_instruments = 1 LIMIT 1",
            (zip_sha256, include_payments),
        ).fetchone()
        if previous:
            with self.connection:
                self._complete_import(run_id, previous[0], now_fn())
            return previous[0]

        reader = ReportReader(path, include_payments=include_payments)
        self.connection.execute("BEGIN")
        try:
            report_id = self.connection.execute(
                "INSERT INTO reports (payments_included) VALUES (?)", (include_payments,)
            ).lastrowid
            issuers = {}
            instruments = {}
            last_issuer_ordinal = None
            for instrument in reader:
                if instrument.issuer_ordinal != last_issuer_ordinal:
                    issuer = instrument.issuer
                    issuer_id, _ = self._version("issuer_versions", content_hash(issuer), {
                        "lei": value(issuer, "IssrLEI"), "nip": value(issuer, "NIP"),
                        "name": value(issuer, "FullNm"), "security_group": value(issuer, "SctyGrp"),
                        "registered_debt_pln": value(issuer, "DbtRgstrdAmt"),
                        "reported_debt_pln": value(issuer, "DbtRprtdAmt"),
                        "total_debt_pln": value(issuer, "TotDbtAmt"),
                        "required_amount_pln": value(issuer, "ReqrdAmt"),
                        "unpaid_amount_pln": value(issuer, "NotPdAmt"),
                        "payload_json": canonical_json(issuer),
                    })
                    issuers[(instrument.issuer_ordinal,)] = issuer_id
                    last_issuer_ordinal = instrument.issuer_ordinal
                data = instrument.data
                digest = content_hash({"data": data, "payments": instrument.payments})
                instrument_id, new = self._version("instrument_versions", digest, {
                    "security_id": value(data, "Scty", "SctyId"),
                    "short_name": value(data, "Scty", "ShrtNm"),
                    "security_type": value(data, "Scty", "SctyTp"),
                    "status": value(data, "Scty", "SctySts"),
                    "form": value(data, "Scty", "SctyFrm"),
                    "cfi": value(data, "Scty", "CFI"), "fisn": value(data, "Scty", "FISN"),
                    "bond_type": value(data, "Scty", "BndTp"),
                    "registration_type": value(data, "RgstrtnTp"),
                    "issue_year": value(data, "IssncYear"),
                    "maturity_date": value(data, "MtrtyDt", "Dt"),
                    "maturity_code": value(data, "MtrtyDt", "DtCd"),
                    "nominal_amount": value(data, "NmnlVal"), "nominal_currency": currency(data, "NmnlVal"),
                    "quantity": value(data, "Qty"),
                    "required_amount": value(data, "OblgtnAmt"), "required_currency": currency(data, "OblgtnAmt"),
                    "unpaid_amount": value(data, "MnyPdgOblgtn"), "unpaid_currency": currency(data, "MnyPdgOblgtn"),
                    "fixed_rate": value(data, "FixRate"), "payload_json": canonical_json(data),
                })
                if new:
                    self._record_payments(instrument_id, value(data, "Scty", "SctyId"), instrument.payments)
                instruments[(instrument.issuer_ordinal, instrument.instrument_ordinal)] = instrument_id

            previous = self.connection.execute(
                "SELECT id FROM reports WHERE xml_sha256 = ? AND payments_included = ? AND all_instruments = 1",
                (reader.xml_sha256, include_payments),
            ).fetchone()
            if previous:
                self.connection.rollback()
                report_id = previous[0]
                self.connection.execute("BEGIN")
            else:
                self._record_membership(report_id, issuers, instruments)
                created_at = value(reader.header, "CreDtTm", "DtTm")
                if created_at is None:
                    created_at = value(reader.header, "CreDtTm", "Dt")
                self.connection.execute(
                    "UPDATE reports SET xml_sha256 = ?, report_date = ?, created_at_source = ?, "
                    "header_json = ?, counts_json = ?, xml_bytes = ? WHERE id = ?",
                    (reader.xml_sha256, report_date(reader.header), created_at,
                     canonical_json(reader.header), canonical_json(asdict(reader.stats)), reader.xml_bytes, report_id),
                )
            self._complete_import(run_id, report_id, now_fn())
            self.connection.commit()
            return report_id
        except BaseException:
            self.connection.rollback()
            raise
