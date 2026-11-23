import sqlite3

import pytest

from pykdpw.collector import Collector
from pykdpw.common import CollectorError
from pykdpw.nominal import nominal_report

from conftest import document, instrument, issuer


def test_one_snapshot_per_currency_whole_issue_not_quantity_or_payments(tmp_path, make_zip, clock):
    directory = tmp_path / "data"
    collector = Collector(directory, now_fn=clock)
    xml = document(issuer(
        instrument(identifier="EUR")
        + instrument(identifier="PLN").replace('Ccy="EUR"', 'Ccy="PLN"')
        + instrument(identifier="REDEEMED", status="NACT")
        + instrument(identifier="COVERED", kind="MRBD")
    ))
    first = collector.import_local(make_zip(xml))
    result = nominal_report(directory)
    assert result["status"] == "complete"
    assert result["by_currency"] == [
        {"currency": "EUR", "nominal_amount": "12345678901234.56", "included_records": 1},
        {"currency": "PLN", "nominal_amount": "12345678901234.56", "included_records": 1},
    ]
    assert result["active_records"] == 2
    assert result["excluded_inactive_records"] == 1
    assert result["inactive_records_with_positive_nominal"] == 1
    assert result["report_date"] == "2026-11-16"
    # Another successful observation does not multiply outstanding debt.
    collector.import_local(make_zip(xml))
    assert nominal_report(directory)["by_currency"] == result["by_currency"]
    assert nominal_report(directory, first["id"])["by_currency"] == result["by_currency"]
    with pytest.raises(CollectorError):
        nominal_report(directory, 999)


def test_missing_zero_invalid_and_unknown_status_are_reported(tmp_path, make_zip, clock):
    directory = tmp_path / "data"
    original = '<NmnlVal Ccy="EUR">12345678901234.56</NmnlVal>'
    xml = document(issuer(
        instrument(identifier="MISSING").replace(original, "")
        + instrument(identifier="ZERO").replace(original, '<NmnlVal Ccy="EUR">0.00</NmnlVal>')
        + instrument(identifier="INVALID").replace(original, '<NmnlVal Ccy="EUR">NaN</NmnlVal>')
        + instrument(identifier="NO_CURRENCY").replace(original, '<NmnlVal>20.00</NmnlVal>')
        + instrument(identifier="NEGATIVE").replace(original, '<NmnlVal Ccy="EUR">-10.00</NmnlVal>')
        + instrument(identifier="UNKNOWN", status="XXXX")
    ))
    Collector(directory, now_fn=clock).import_local(make_zip(xml))
    result = nominal_report(directory)
    assert result["status"] == "partial"
    assert result["active_records"] == 5
    assert result["included_records"] == 1
    assert result["by_currency"] == [{"currency": "EUR", "nominal_amount": "0.00", "included_records": 1}]
    assert result["quality"] == {
        "missing_nominal": 1, "invalid_nominal": 2, "missing_or_invalid_currency": 1,
        "unknown_status": 1, "duplicate_identity_groups": 0, "duplicate_extra_records": 0,
    }


def test_duplicate_source_identity_marks_sum_as_partial(tmp_path, make_zip, clock):
    directory = tmp_path / "data"
    Collector(directory, now_fn=clock).import_local(make_zip(document(issuer(instrument() * 2))))
    result = nominal_report(directory)
    assert result["status"] == "partial"
    assert result["by_currency"][0]["nominal_amount"] == "24691357802469.12"
    assert result["quality"]["duplicate_identity_groups"] == 1
    assert result["quality"]["duplicate_extra_records"] == 1


def test_no_database_is_not_a_zero_total(tmp_path):
    with pytest.raises(CollectorError):
        nominal_report(tmp_path / "absent")
    assert not (tmp_path / "absent").exists()


def test_latest_issuer_views_by_type_currency_and_observation(tmp_path, make_zip):
    collector = Collector(tmp_path)
    bonds = (instrument(identifier="A") + instrument(identifier="B", status="NACT")
             + instrument(identifier="USD").replace('Ccy="EUR"', 'Ccy="USD"'))
    covered = instrument(kind="MRBD").replace("12345678901234.56", "0.00001")
    bad = covered.replace("0.00001", "bad")
    ident = "<NIP>0012345678</NIP>"
    path = make_zip(document(issuer(bonds, identifiers=ident) + issuer(instrument(), identifiers=ident)
                             + issuer(covered + bad, identifiers=ident) + issuer(covered, name="Bank B")
                             + issuer(instrument(kind="CRIN"), name="Fundusz")))
    first = collector.import_local(path)
    with sqlite3.connect(tmp_path / "rze.sqlite3") as db:
        sql = "SELECT issuer_name, nominal_currency, nominal_amount, included_records, excluded_records FROM "
        def totals(view):
            return db.execute(sql + view + " ORDER BY issuer_name, nominal_currency").fetchall()

        name = "Żółć Spółka Testowa"
        expected = [(name, "EUR", "24691357802469.12", 2, 0), (name, "USD", "12345678901234.56", 1, 0)]
        assert totals("issuer_bond_nominal") == expected
        assert totals("issuer_covered_bond_nominal") == [
            ("Bank B", "EUR", "0.00001", 1, 0), (name, "EUR", "0.00001", 1, 1)]
        assert db.execute("SELECT COUNT(*) FROM daily_instruments WHERE security_type='CRIN'").fetchone() == (1,)
        collector.import_local(make_zip(document(issuer(covered))))
        assert totals("issuer_bond_nominal") == []
        assert totals("issuer_covered_bond_nominal") == [(name, "EUR", "0.00001", 1, 0)]
        replay = collector.import_local(path)
        assert replay["report_id"] == first["report_id"]
        assert totals("issuer_bond_nominal") == expected
        assert db.execute("SELECT DISTINCT run_id FROM issuer_bond_nominal").fetchall() == [(replay["id"],)]
