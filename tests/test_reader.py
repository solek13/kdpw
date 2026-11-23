import hashlib
import zipfile

import pytest
from defusedxml.common import DefusedXmlException

from pykdpw.common import CollectorError
from pykdpw.xml_reader import ReportReader, report_date, value

from conftest import document, instrument, issuer, payment


def test_full_fields_types_duplicates_and_source_hash(make_zip):
    xml = document(issuer(
        instrument(kind="MRBD") + instrument(kind="CRIN") + instrument(status="NACT", extra="""
        <Srs><SrsNm>A</SrsNm><Sbscrptn><Qty>42</Qty><MaxPric Ccy="PLN">12.34</MaxPric></Sbscrptn></Srs>
        <Srs><SrsNm>B</SrsNm></Srs>""")
    ))
    reader = ReportReader(make_zip(xml))
    records = list(reader)
    assert [value(record.data, "Scty", "SctyTp") for record in records] == ["MRBD", "CRIN", "BOND"]
    record = records[2]
    assert record.instrument_ordinal == 3
    assert value(record.issuer, "FullNm") == "Żółć Spółka Testowa"
    assert value(record.issuer, "IssrLEI") is None
    assert value(record.data, "Scty", "SctySts") == "NACT"
    assert value(record.data, "NmnlVal") == "12345678901234.56"
    assert value(record.data, "MtrtyDt", "Dt") == "0001-01-01"
    assert len(record.data["Srs"]) == 2
    assert len(record.payments) == 2
    assert record.payments[0] == record.payments[1]
    assert reader.stats.instruments == 3
    assert reader.stats.bonds == 1
    assert reader.stats.payments == 6
    assert reader.stats.bond_payments == 2
    assert reader.xml_sha256 == hashlib.sha256(xml.encode("utf-8")).hexdigest()
    assert report_date(reader.header) == "2026-11-16"


def test_missing_optional_fields_and_no_date_code(make_zip):
    xml = document(report_day=None).replace("<NmnlVal Ccy=\"EUR\">12345678901234.56</NmnlVal>", "")
    xml = xml.replace("<Dt>0001-01-01</Dt>", "<DtCd>NOAP</DtCd>")
    reader = ReportReader(make_zip(xml))
    record = list(reader)[0]
    assert value(record.data, "NmnlVal") is None
    assert value(record.data, "MtrtyDt", "DtCd") == "NOAP"
    assert report_date(reader.header) is None


@pytest.mark.parametrize("xml", [
    document(namespace="urn:kdpw:xsd:reda.dbt.001.99"),
    document(issuers=""),
    document(issuer(instrument(kind="OTHER"))),
    document().replace("<SctyId>SERIA-A</SctyId>", ""),
    document().replace("<PmtDt>2026-10-15</PmtDt>", ""),
    document().replace("2026-11-16T00:00:00", "not-a-date"),
    document().replace("</DbtRpt>", "<Unknown/></DbtRpt>"),
])
def test_invalid_report_is_rejected(make_zip, xml):
    with pytest.raises(CollectorError):
        list(ReportReader(make_zip(xml)))


def test_dtd_entities_rejected(make_zip):
    xml = document().replace("<Document", '<!DOCTYPE Document [<!ENTITY e "secret">]><Document', 1)
    with pytest.raises(DefusedXmlException):
        list(ReportReader(make_zip(xml)))


def test_sender_reference_is_not_a_fixed_message_type(make_zip):
    xml = document().replace("<SndrMsgRef>REDA.DBT.001.02", "<SndrMsgRef>EXAMPLE-123")
    reader = ReportReader(make_zip(xml))
    assert len(list(reader)) == 1
    assert value(reader.header, "SndrMsgRef") == "EXAMPLE-123"


def test_zip_crc_error_is_detected(make_zip):
    path = make_zip(compression=zipfile.ZIP_STORED)
    raw = path.read_bytes()
    assert b"SERIA-A" in raw
    path.write_bytes(raw.replace(b"SERIA-A", b"SERIA-B", 1))
    with pytest.raises(zipfile.BadZipFile):
        list(ReportReader(path))


def test_zip_extra_members_rejected(make_zip):
    path = make_zip()
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("../../outside.txt", "not extracted")
    with pytest.raises(CollectorError):
        list(ReportReader(path))
