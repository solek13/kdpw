import json
import sqlite3

from pykdpw.cli import main


def test_cli_status_does_not_create_data(tmp_path, capsys):
    target = tmp_path / "absent"
    assert main(["status", "--data-dir", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "empty"
    assert not target.exists()


def test_cli_import_and_status_with_data_dir_before_or_after_command(tmp_path, make_zip, capsys):
    target = tmp_path / "data"
    assert main(["--data-dir", str(target), "import", str(make_zip())]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"
    assert main(["status", "--data-dir", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["last_successful_run"]["counts"]["bonds"] == 1
    assert main(["nominal", "--data-dir", str(target), "--run-id", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["by_currency"][0]["nominal_amount"] == "12345678901234.56"


def test_cli_reports_invalid_archive_as_failure(tmp_path, capsys):
    assert main(["import", str(tmp_path / "missing.zip"), "--data-dir", str(tmp_path / "data")]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "error"


def test_cli_payments_opt_in_reimports_same_archive(tmp_path, make_zip, capsys):
    target = tmp_path / "data"
    args = ["import", str(make_zip()), "--data-dir", str(target)]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["report"]["counts"]["payments"] == 2
    with sqlite3.connect(target / "rze.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM payment_history").fetchone() == (0,)
    assert main([*args, "--with-payments"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["report_id"] != second["report_id"]
    with sqlite3.connect(target / "rze.sqlite3") as db:
        assert db.execute("SELECT run_id FROM daily_bonds JOIN payments USING (instrument_version_id)").fetchall() == [
            (second["id"],), (second["id"],)
        ]
