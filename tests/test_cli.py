import json

from kdpw_rze.cli import main


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


def test_cli_reports_invalid_archive_as_failure(tmp_path, capsys):
    assert main(["import", str(tmp_path / "missing.zip"), "--data-dir", str(tmp_path / "data")]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "error"
