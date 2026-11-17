import pytest

from kdpw_rze.collector import Collector
from kdpw_rze.common import CollectorError
from kdpw_rze.nominal import nominal_report

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
