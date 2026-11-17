from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from xml.sax.saxutils import escape

import pytest

from kdpw_rze.xml_reader import NAMESPACE


@pytest.fixture
def clock():
    class Clock:
        instant = datetime(2026, 11, 17, 12, tzinfo=UTC)

        def __call__(self):
            return self.instant

    return Clock()


def payment(*, amount="123.4500", event="INTR", currency="EUR"):
    return f"""<PmtDtls><EvtCd>{event}</EvtCd><RcrdDt>2026-10-01</RcrdDt>
    <PmtDt>2026-10-15</PmtDt><IntrstRate>0.0000</IntrstRate>
    <ExpctdPmtVal Ccy="{currency}">{amount}</ExpctdPmtVal><ExpctQty>0</ExpctQty>
    <SttldPmtDt>2026-10-16</SttldPmtDt><SttldPmtVal Ccy="{currency}">0.00</SttldPmtVal>
    <SttldQty>0</SttldQty><UpdtDt>2026-10-20</UpdtDt><OnTmInd>N</OnTmInd></PmtDtls>"""


def instrument(*, identifier="SERIA-A", kind="BOND", status="ACTV", payments=None, extra=""):
    if payments is None:
        payments = payment() * 2
    return f"""<FinInstrmRpt><Scty><SctyId>{identifier}</SctyId><ShrtNm>Test</ShrtNm>
    <SctyTp>{kind}</SctyTp><SctyFrm>OKAZ</SctyFrm><SctySts>{status}</SctySts>
    <CFI>DBFTFB</CFI><FISN>TEST/BOND</FISN><BndTp>OBIC</BndTp></Scty>
    <RgstrtnTp>RPRT</RgstrtnTp><IssncYear>2020</IssncYear>
    <MtrtyDt><Dt>0001-01-01</Dt></MtrtyDt><NmnlVal Ccy="EUR">12345678901234.56</NmnlVal>
    <Qty>123</Qty><OblgtnAmt Ccy="EUR">123.4500</OblgtnAmt>
    <MnyPdgOblgtn Ccy="EUR">0.00</MnyPdgOblgtn><FixRate>0.0000</FixRate>
    <PmtCal>{payments}</PmtCal>{extra}</FinInstrmRpt>"""


def issuer(instruments=None, *, name="Żółć Spółka Testowa", identifiers=""):
    if instruments is None:
        instruments = instrument()
    return f"""<IssrDbtRpt>{identifiers}<FullNm>{escape(name)}</FullNm><SctyGrp>DEBT</SctyGrp>
    <DbtRgstrdAmt>0.00</DbtRgstrdAmt><DbtRprtdAmt>1.00</DbtRprtdAmt>
    <TotDbtAmt>1.00</TotDbtAmt><ReqrdAmt>0.00</ReqrdAmt><NotPdAmt>0.00</NotPdAmt>
    {instruments}</IssrDbtRpt>"""


def document(issuers=None, *, report_day="2026-11-16", namespace=NAMESPACE):
    if issuers is None:
        issuers = issuer()
    report = f"<RptDtTm><DtTm>{report_day}T00:00:00</DtTm></RptDtTm>" if report_day else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <Document xmlns="{namespace}"><DbtRpt><GnlInf><SndrMsgRef>REDA.DBT.001.02</SndrMsgRef>
    <CreDtTm><DtTm>2026-11-16T19:58:49.0455314+01:00</DtTm></CreDtTm>
    {report}</GnlInf>{issuers}</DbtRpt></Document>"""


@pytest.fixture
def make_zip(tmp_path):
    counter = 0

    def create(xml=None, *, compression=zipfile.ZIP_DEFLATED):
        nonlocal counter
        counter += 1
        path = tmp_path / f"fixture-{counter}.zip"
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            archive.writestr("report.xml", (xml if xml is not None else document()).encode("utf-8"))
        return path

    return create
