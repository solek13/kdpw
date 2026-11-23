from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from defusedxml.ElementTree import iterparse

from .common import CollectorError

NAMESPACE = "urn:kdpw:xsd:reda.dbt.001.02"
MAX_ZIP_BYTES = 128 * 1024 * 1024
MAX_XML_BYTES = 2 * 1024 * 1024 * 1024


def local_name(tag: str) -> str:
    prefix = "{" + NAMESPACE + "}"
    if not tag.startswith(prefix):
        raise CollectorError(f"Nieobsługiwana przestrzeń nazw XML: {tag[:160]}")
    return tag[len(prefix):]


def payload(element, *, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Keep attributes, repeated children and literal leaf values losslessly."""
    result: dict[str, Any] = {}
    if element.attrib:
        result["@"] = dict(element.attrib)
    children = list(element)
    if not children:
        result["value"] = element.text or ""
    else:
        for child in children:
            name = local_name(child.tag)
            if name not in exclude:
                result.setdefault(name, []).append(payload(child))
    return result


def node(data: dict, *path: str) -> dict:
    for name in path:
        children = data.get(name, [])
        if not children:
            return {}
        if len(children) != 1:
            raise CollectorError(f"Powtórzone pole pojedyncze XML: {'/'.join(path)}")
        data = children[0]
    return data


def value(data: dict, *path: str) -> str | None:
    return node(data, *path).get("value")


def currency(data: dict, *path: str) -> str | None:
    return node(data, *path).get("@", {}).get("Ccy")


def required(data: dict, *path: str) -> str:
    result = value(data, *path)
    if result is None or not result.strip():
        raise CollectorError(f"Brak wymaganego pola XML: {'/'.join(path)}")
    return result


def report_date(header: dict) -> str | None:
    block = node(header, "RptDtTm")
    literal_date = value(block, "Dt")
    literal_time = value(block, "DtTm")
    if literal_date is not None and literal_time is not None:
        raise CollectorError("RptDtTm zawiera równocześnie Dt i DtTm.")
    try:
        if literal_date is not None:
            return date.fromisoformat(literal_date).isoformat()
        if literal_time is not None:
            return datetime.fromisoformat(literal_time).date().isoformat()
    except ValueError as exc:
        raise CollectorError("Niepoprawna data raportu KDPW.") from exc
    return None


@dataclass
class Instrument:
    issuer_ordinal: int
    instrument_ordinal: int
    issuer: dict
    data: dict
    payments: list[dict]


@dataclass
class Statistics:
    issuers: int = 0
    instruments: int = 0
    payments: int = 0
    bond_issuers: int = 0
    bonds: int = 0
    bond_payments: int = 0


class HashingReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size=-1):
        block = self.stream.read(size)
        self.size += len(block)
        if self.size > MAX_XML_BYTES:
            raise CollectorError("XML przekracza limit 2 GiB.")
        self.digest.update(block)
        return block


@dataclass
class ReportReader:
    path: Path
    include_payments: bool = True
    header: dict = field(default_factory=dict, init=False)
    stats: Statistics = field(default_factory=Statistics, init=False)
    xml_sha256: str | None = field(default=None, init=False)
    xml_bytes: int = field(default=0, init=False)

    def __iter__(self) -> Iterator[Instrument]:
        if self.path.stat().st_size > MAX_ZIP_BYTES:
            raise CollectorError("ZIP przekracza limit 128 MiB.")
        with zipfile.ZipFile(self.path) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            if len(members) != 1 or not members[0].filename.lower().endswith(".xml"):
                raise CollectorError("Oczekiwano dokładnie jednego XML-a w ZIP-ie.")
            member = members[0]
            if member.flag_bits & 1 or member.file_size > MAX_XML_BYTES:
                raise CollectorError("Zaszyfrowany lub zbyt duży XML w ZIP-ie.")
            with archive.open(member) as stream:
                reader = HashingReader(stream)
                yield from self._read(reader)
                # Reading through EOF also verifies the member's ZIP CRC.
                if reader.size != member.file_size:
                    raise CollectorError("Niepełny odczyt pliku XML.")
                self.xml_sha256 = reader.digest.hexdigest()
                self.xml_bytes = reader.size

    def _read(self, stream) -> Iterator[Instrument]:
        stack = []
        header_seen = False
        report_seen = False
        instrument_ordinal = 0
        issuer_data = None
        issuer_has_bonds = False
        for event, element in iterparse(
            stream, events=("start", "end"), forbid_dtd=True,
            forbid_entities=True, forbid_external=True,
        ):
            name = local_name(element.tag)
            if event == "start":
                stack.append(element)
                if len(stack) > 64:
                    raise CollectorError("Przekroczona głębokość XML.")
                if len(stack) == 1 and name != "Document":
                    raise CollectorError("Niepoprawny element główny XML.")
                if name == "DbtRpt":
                    if len(stack) != 2 or report_seen:
                        raise CollectorError("Niepoprawny blok DbtRpt.")
                    report_seen = True
                if len(stack) == 2 and name != "DbtRpt":
                    raise CollectorError("Nieznany blok dokumentu XML.")
                if name == "IssrDbtRpt":
                    if len(stack) != 3 or not header_seen:
                        raise CollectorError("Niepoprawna kolejność bloków raportu.")
                    self.stats.issuers += 1
                    instrument_ordinal = 0
                    issuer_data = None
                    issuer_has_bonds = False
                if name == "FinInstrmRpt":
                    if len(stack) != 4 or local_name(stack[-2].tag) != "IssrDbtRpt":
                        raise CollectorError("Instrument poza blokiem emitenta.")
                    instrument_ordinal += 1
                    if issuer_data is None:
                        issuer_data = payload(stack[-2], exclude=frozenset({"FinInstrmRpt"}))
                        required(issuer_data, "FullNm")
                        required(issuer_data, "SctyGrp")
                continue

            if name == "GnlInf":
                if len(stack) != 3 or header_seen or self.stats.issuers:
                    raise CollectorError("Niepoprawny blok informacji ogólnych.")
                self.header = payload(element)
                # The namespace identifies the schema. SndrMsgRef is the sender's
                # message reference, which is not constrained to a fixed value.
                required(self.header, "SndrMsgRef")
                report_date(self.header)
                header_seen = True
                stack[-2].remove(element)
                element.clear()
            elif name == "FinInstrmRpt":
                data = payload(element, exclude=frozenset({"PmtCal"}))
                security_type = required(data, "Scty", "SctyTp")
                if security_type not in {"BOND", "MRBD", "CRIN"}:
                    raise CollectorError(f"Nieznany typ instrumentu: {security_type}")
                required(data, "Scty", "SctyId")
                required(data, "Scty", "SctySts")
                required(data, "RgstrtnTp")
                calendars = element.findall("{" + NAMESPACE + "}PmtCal")
                if len(calendars) > 1:
                    raise CollectorError("Powtórzony kalendarz instrumentu.")
                payment_nodes = list(calendars[0]) if calendars else []
                if any(local_name(p.tag) != "PmtDtls" for p in payment_nodes):
                    raise CollectorError("Nieznany element kalendarza świadczeń.")
                self.stats.instruments += 1
                self.stats.payments += len(payment_nodes)
                payments = []
                for payment_node in payment_nodes:
                    payment = payload(payment_node)
                    required(payment, "EvtCd")
                    required(payment, "RcrdDt")
                    required(payment, "PmtDt")
                    if self.include_payments:
                        payments.append(payment)
                if security_type == "BOND":
                    if not issuer_has_bonds:
                        self.stats.bond_issuers += 1
                        issuer_has_bonds = True
                    self.stats.bonds += 1
                    self.stats.bond_payments += len(payment_nodes)
                yield Instrument(self.stats.issuers, instrument_ordinal, issuer_data, data, payments)
                stack[-2].remove(element)
                element.clear()
            elif name == "IssrDbtRpt":
                if not instrument_ordinal:
                    raise CollectorError("Emitent bez instrumentów w raporcie.")
                stack[-2].remove(element)
                element.clear()
            elif len(stack) == 3 and name not in {"GnlInf", "IssrDbtRpt"}:
                raise CollectorError(f"Nieznany blok raportu: {name}")
            stack.pop()
        if not report_seen or not header_seen or not self.stats.instruments:
            raise CollectorError("Niekompletny lub pusty raport instrumentów.")
