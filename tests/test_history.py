import json
import sqlite3
import zipfile
from datetime import timedelta
from xml.etree.ElementTree import ParseError

import pytest

from pykdpw.collector import Collector
from pykdpw.common import CollectorError
from pykdpw.database import Database

from conftest import document, instrument, issuer, payment


def query(directory, sql, args=()):
    with sqlite3.connect(directory / "rze.sqlite3") as connection:
        return connection.execute(sql, args).fetchall()


def test_daily_snapshot_versions_and_payment_changes(tmp_path, make_zip, clock):
    directory = tmp_path / "data"
    collector = Collector(directory, now_fn=clock)
    original = make_zip()
    first = collector.import_local(original)
    clock.instant += timedelta(days=1)
    repeat = collector.import_local(original)
    assert first["report_id"] == repeat["report_id"]
    assert repeat["observation_date"] == "2026-11-18"
    assert repeat["report"]["report_date"] == "2026-11-16"
    assert query(directory, "SELECT COUNT(*) FROM payments") == [(2,)]

    # Same XML, different ZIP bytes is the same source report.
    repacked = collector.import_local(make_zip(compression=zipfile.ZIP_STORED))
    assert repacked["report_id"] == first["report_id"]
    # New report date, unchanged instrument and payments: share versions.
    next_report = collector.import_local(make_zip(document(report_day="2026-11-17")))
    assert next_report["report_id"] != first["report_id"]
    assert query(directory, "SELECT COUNT(*) FROM instrument_versions") == [(1,)]
    # Correction of a previously seen date creates a new immutable report.
    correction = collector.import_local(make_zip(document(issuer(instrument(payments=payment(amount="777.00"))))))
    assert correction["report_id"] != first["report_id"]
    assert query(directory, "SELECT COUNT(*) FROM instrument_versions") == [(2,)]
    assert query(directory, "SELECT COUNT(*) FROM reports") == [(3,)]
    assert query(directory, "SELECT COUNT(*) FROM payments") == [(3,)]
    assert query(directory, "SELECT expected_amount FROM payments ORDER BY instrument_version_id, ordinal") == [
        ("123.4500",), ("123.4500",), ("777.00",),
    ]
    assert query(directory, "SELECT COUNT(*) FROM daily_bonds") == [(5,)]
    manifest = directory / correction["archive_path"]
    assert json.loads(manifest.with_name("manifest.json").read_text(encoding="utf-8"))["status"] == "complete"
    assert collector.status()["report_versions"] == 3
    assert collector.status()["control"]["last_attempt_at"] is None


def test_source_occurrences_not_merged_by_security_id(tmp_path, make_zip, clock):
    directory = tmp_path / "data"
    xml = document(issuer(instrument() * 2, name="Emitent A") + issuer(name="Emitent B"))
    result = Collector(directory, now_fn=clock).import_local(make_zip(xml))
    assert result["report"]["counts"]["bonds"] == 3
    assert query(directory, "SELECT COUNT(*) FROM report_instruments") == [(3,)]
    assert query(directory, "SELECT COUNT(*) FROM instrument_versions") == [(1,)]
    assert query(directory, "SELECT issuer_name FROM daily_bonds ORDER BY issuer_ordinal, instrument_ordinal") == [
        ("Emitent A",), ("Emitent A",), ("Emitent B",),
    ]


def test_only_changed_positions_are_stored(tmp_path, make_zip, clock):
    directory = tmp_path / "data"
    collector = Collector(directory, now_fn=clock)
    a = instrument()
    b = issuer(instrument(identifier="B"), name="B")
    original = make_zip(document(issuer(a) + b))
    first = collector.import_local(original)
    for day in range(17, 27):
        collector.import_local(make_zip(document(issuer(a) + b, report_day=f"2026-11-{day}")))

    def history_sizes():
        return tuple(query(directory, f"SELECT COUNT(*) FROM {table}")[0][0]
                     for table in ("issuer_history", "instrument_history", "payment_history"))

    assert history_sizes() == (2, 2, 4)
    assert query(directory, "SELECT COUNT(*) FROM daily_bonds") == [(22,)]
    changed = instrument(payments=payment() + payment(amount="777.00"))
    corrected = collector.import_local(make_zip(document(issuer(changed))))
    assert history_sizes() == (2, 3, 5)
    replay = collector.import_local(original)
    assert replay["report_id"] == first["report_id"]
    # A previously seen report replayed between new imports must not reset the baseline.
    restored = collector.import_local(make_zip(document(issuer(changed) + b, report_day="2026-11-28")))
    assert history_sizes() == (3, 4, 5)
    for run, expected in ((first, ["SERIA-A", "B"]), (corrected, ["SERIA-A"]),
                          (replay, ["SERIA-A", "B"]), (restored, ["SERIA-A", "B"])):
        rows = query(directory, "SELECT security_id FROM daily_bonds WHERE run_id = ? ORDER BY issuer_ordinal",
                     (run["id"],))
        assert [row[0] for row in rows] == expected
    amounts = ("SELECT p.expected_amount FROM daily_bonds b JOIN payments p USING (instrument_version_id) "
               "WHERE b.run_id = ? AND b.security_id = 'SERIA-A' ORDER BY p.ordinal")
    assert query(directory, amounts, (first["id"],)) == [("123.4500",), ("123.4500",)]
    assert query(directory, amounts, (restored["id"],)) == [("123.4500",), ("777.00",)]


def test_partial_import_rolls_back_and_archive_can_be_replayed(tmp_path, make_zip, clock, monkeypatch):
    directory = tmp_path / "data"
    collector = Collector(directory, now_fn=clock)
    path = make_zip()
    original = Database._version

    def fail_after_issuer(self, table, *args):
        if table == "instrument_versions":
            raise RuntimeError("simulated interruption")
        return original(self, table, *args)

    monkeypatch.setattr(Database, "_version", fail_after_issuer)
    with pytest.raises(RuntimeError):
        collector.import_local(path)
    assert query(directory, "SELECT COUNT(*) FROM issuer_versions") == [(0,)]
    assert query(directory, "SELECT COUNT(*) FROM reports") == [(0,)]
    assert query(directory, "SELECT status FROM runs") == [("failed",)]
    archived = directory / collector.status()["last_run"]["archive_path"]
    assert archived.read_bytes() == path.read_bytes()
    monkeypatch.setattr(Database, "_version", original)
    assert collector.import_local(archived)["status"] == "complete"


def test_malformed_tail_does_not_publish_partial_report(tmp_path, make_zip, clock):
    directory = tmp_path / "data"
    collector = Collector(directory, now_fn=clock)
    collector.import_local(make_zip())
    damaged = document(issuer(instrument(identifier="SECOND")))[:-15]
    with pytest.raises(ParseError):
        collector.import_local(make_zip(damaged))
    assert query(directory, "SELECT COUNT(*) FROM reports") == [(1,)]
    assert query(directory, "SELECT security_id FROM instrument_versions") == [("SERIA-A",)]
    assert collector.status()["last_successful_run"]["status"] == "complete"


def test_recover_crashed_process_without_publishing_report(tmp_path, clock):
    directory = tmp_path / "data"
    directory.mkdir()
    database = Database(directory / "rze.sqlite3")
    run_id = database.new_run(clock(), "network", "https://example.invalid")
    database.set_control(last_attempt_day="2026-11-17", last_attempt_at=clock().isoformat())
    database.close()
    collector = Collector(directory, now_fn=clock)
    assert collector.fetch()["status"] == "already_attempted"
    assert collector.status()["last_run"]["status"] == "interrupted"
    assert query(directory, "SELECT COUNT(*) FROM reports") == [(0,)]


def test_corrupt_zip_never_completes(tmp_path, clock):
    path = tmp_path / "broken.zip"
    path.write_bytes(b"not a zip")
    collector = Collector(tmp_path / "data", now_fn=clock)
    with pytest.raises(zipfile.BadZipFile):
        collector.import_local(path)
    assert collector.status()["last_run"]["status"] == "failed"
