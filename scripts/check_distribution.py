"""Check an actual built wheel in a clean venv outside the source checkout.

Only dependency installation uses the network. The data fixture is synthetic;
no test in this script downloads anything from KDPW.
"""

from __future__ import annotations

import argparse
import email
import json
import os
import subprocess
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path


FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<Document xmlns="urn:kdpw:xsd:reda.dbt.001.02"><DbtRpt>
<GnlInf><SndrMsgRef>PACKAGE-TEST</SndrMsgRef><RptDtTm><Dt>2026-11-16</Dt></RptDtTm></GnlInf>
<IssrDbtRpt><FullNm>Testowy emitent Żółć</FullNm><NIP>0012345678</NIP><SctyGrp>DEBT</SctyGrp>
<DbtRgstrdAmt>0.00</DbtRgstrdAmt><DbtRprtdAmt>123.45</DbtRprtdAmt><TotDbtAmt>123.45</TotDbtAmt>
<ReqrdAmt>0.00</ReqrdAmt><NotPdAmt>0.00</NotPdAmt>
<FinInstrmRpt><Scty><SctyId>TEST-A</SctyId><ShrtNm>TEST</ShrtNm><SctyTp>BOND</SctyTp>
<SctyFrm>OKAZ</SctyFrm><SctySts>ACTV</SctySts></Scty><RgstrtnTp>RPRT</RgstrtnTp>
<NmnlVal Ccy="PLN">123.45</NmnlVal><Qty>1</Qty><PmtCal>
<PmtDtls><EvtCd>INTR</EvtCd><RcrdDt>2026-11-01</RcrdDt><PmtDt>2026-11-15</PmtDt>
<ExpctdPmtVal Ccy="PLN">1.23</ExpctdPmtVal></PmtDtls>
</PmtCal></FinInstrmRpt></IssrDbtRpt></DbtRpt></Document>
"""


def check_members(names: list[str]):
    for name in names:
        parts = Path(name).parts
        assert not {"data", ".venv", ".git", "__pycache__"}.intersection(parts), name
        assert "kdpw_rze" not in parts, name
        assert not name.endswith((".sqlite3", ".sqlite3-wal", ".zip", ".log", ".pyc")), name


def check_distribution(dist_dir: Path):
    wheels = list(dist_dir.glob("*.whl"))
    sdists = list(dist_dir.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "Expected one wheel and one source distribution"
    wheel = wheels[0].resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        check_members(names)
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
        assert metadata["Name"] == "pykdpw"
        assert metadata["License-Expression"] == "MIT"
        assert metadata["Requires-Python"] == ">=3.12"
        assert any(name.endswith("/licenses/LICENSE") for name in names)
        assert "pykdpw/nominal.py" in names
        version = metadata["Version"]
    with tarfile.open(sdists[0]) as archive:
        names = archive.getnames()
        check_members(names)
        for required in ("LICENSE", "README.md", "pyproject.toml", "tests/conftest.py",
                         "scripts/check_distribution.py", "docs/publishing.md"):
            assert any(name.endswith("/" + required) for name in names), required

    # TemporaryDirectory owns exactly the generated directory under the system temp root.
    with tempfile.TemporaryDirectory(prefix="pykdpw-package-") as temporary:
        root = Path(temporary).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        command = scripts / ("pykdpw.exe" if os.name == "nt" else "pykdpw")
        subprocess.run([str(python), "-m", "pip", "install", str(wheel)], cwd=root, check=True)

        def run(*args):
            result = subprocess.run([str(command), *args], cwd=root, check=True,
                                    capture_output=True, text=True, encoding="utf-8")
            return result.stdout

        assert run("--version").strip() == version
        assert "fetch" in run("--help")
        assert json.loads(run("status"))["status"] == "empty"
        assert not (root / "data").exists()
        fixture = root / "fixture.zip"
        with zipfile.ZipFile(fixture, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.xml", FIXTURE.encode("utf-8"))
        result = json.loads(run("import", str(fixture)))
        assert result["status"] == "complete"
        assert result["report"]["counts"]["bonds"] == 1
        assert result["report"]["counts"]["bond_payments"] == 1
        nominal = json.loads(run("nominal"))
        assert nominal["by_currency"] == [
            {"currency": "PLN", "nominal_amount": "123.45", "included_records": 1}
        ]
        api = """import json, pathlib, pykdpw
from pykdpw import Collector, CollectorError, nominal_report
assert nominal_report(pathlib.Path('data'))['included_records'] == 1
assert Collector(pathlib.Path('data')).status()['last_successful_run']['status'] == 'complete'
assert issubclass(CollectorError, Exception)
print(json.dumps({'module': pykdpw.__file__}))
"""
        result = subprocess.run([str(python), "-I", "-c", api], cwd=root, check=True,
                                capture_output=True, text=True, encoding="utf-8")
        installed_path = Path(json.loads(result.stdout)["module"]).resolve()
        assert installed_path.is_relative_to(environment), installed_path
    print(f"Distribution {version}: metadata, contents, clean install, CLI, import and Python API OK.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    check_distribution(parser.parse_args().dist_dir)
