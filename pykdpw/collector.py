from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
from contextlib import closing, contextmanager
from datetime import UTC, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import transport
from .common import CollectorError, WARSAW, canonical_json, day, timestamp, utc_now, write_json
from .database import Database
from .locking import exclusive_lock
from .xml_reader import MAX_ZIP_BYTES


def cooldown_until(now: datetime, retry_after: str | None) -> str:
    next_day = now.astimezone(WARSAW).date() + timedelta(days=1)
    minimum = datetime.combine(next_day, time.min, WARSAW).astimezone(UTC)
    if retry_after:
        try:
            if retry_after.strip().isdigit():
                requested = now + timedelta(seconds=int(retry_after.strip()))
            else:
                requested = parsedate_to_datetime(retry_after)
                if requested.tzinfo is None:
                    requested = requested.replace(tzinfo=UTC)
            minimum = max(minimum, requested)
        except (ValueError, TypeError, OverflowError):
            # Unparseable Retry-After still stops requests until the next day.
            pass
    return timestamp(minimum)


class Collector:
    def __init__(self, data_dir: Path, *, include_payments: bool = False, now_fn=utc_now, progress=None):
        self.data_dir = data_dir.resolve()
        self.include_payments = include_payments
        self.now = now_fn
        self.progress = progress or (lambda message: None)

    @contextmanager
    def _database(self):
        with exclusive_lock(self.data_dir):
            database = Database(self.data_dir / "rze.sqlite3")
            try:
                for run_id in database.recover_interrupted(self.now()):
                    self._manifest(database, run_id)
                yield database
            finally:
                database.close()

    def _manifest(self, database: Database, run_id: int):
        run = database.run(run_id)
        directory = self.data_dir / "archives" / run["observation_date"] / f"{run_id:08d}"
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "manifest.json", run)

    def _new_run(self, database: Database, source: str, location: str) -> tuple[int, Path]:
        now = self.now()
        run_id = database.new_run(now, source, location)
        relative = Path("archives") / day(now) / f"{run_id:08d}" / "source.zip"
        destination = self.data_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        database.update_run(run_id, archive_path=relative.as_posix())
        self._manifest(database, run_id)
        return run_id, destination

    def _import(self, database: Database, run_id: int, path: Path, metadata: dict):
        database.update_run(
            run_id, status="downloaded", zip_sha256=metadata["zip_sha256"],
            zip_bytes=metadata["zip_bytes"], http_status=metadata.get("http_status"),
            response_headers_json=canonical_json(metadata.get("response_headers", {})),
        )
        self._manifest(database, run_id)
        self.progress("Archiwum zapisane. Trwa import raportu do SQLite.")
        database.update_run(run_id, status="importing")
        self._manifest(database, run_id)
        database.import_report(path, metadata["zip_sha256"], run_id, self.now, include_payments=self.include_payments)
        self._manifest(database, run_id)
        return database.run(run_id)

    def _failure(self, database: Database, run_id: int, exc: BaseException):
        # A successful SQL commit remains successful if a later manifest write fails.
        if database.run(run_id)["status"] != "complete":
            database.update_run(run_id, status="failed", finished_at=timestamp(self.now()),
                                error=f"{type(exc).__name__}: {exc}"[:2000])
            self._manifest(database, run_id)

    def fetch(self) -> dict:
        with self._database() as database:
            now = self.now()
            state = database.control()
            if state["paused_reason"]:
                return {"status": "paused", "reason": state["paused_reason"],
                        "message": "Po sprawdzeniu przyczyny użyj polecenia resume.", "control": state}
            if state["blocked_until"] and now < datetime.fromisoformat(state["blocked_until"]):
                return {"status": "cooldown", "control": state}
            if state["last_attempt_day"] and state["last_attempt_day"] >= day(now):
                return {"status": "already_attempted", "control": state,
                        "message": "Wykorzystano dzienną próbę. Archiwum można importować lokalnie."}
            run_id, destination = self._new_run(database, "network", transport.EXPORT_URL)
            # Commit before any request, including failures and process interruption.
            database.set_control(last_attempt_at=timestamp(now), last_attempt_day=day(now))
            try:
                self.progress("Pobieranie oficjalnego eksportu KDPW: jedno żądanie, bez ponowień.")
                metadata = transport.download(destination)
                return self._import(database, run_id, destination, metadata)
            except urllib.error.HTTPError as exc:
                try:
                    database.update_run(run_id, http_status=exc.code)
                    if exc.code in {401, 403}:
                        database.set_control(paused_reason=f"KDPW odmówił dostępu: HTTP {exc.code}.")
                    elif exc.code == 429:
                        database.set_control(blocked_until=cooldown_until(self.now(), exc.headers.get("Retry-After")))
                    self._failure(database, run_id, exc)
                finally:
                    exc.close()
                raise CollectorError(f"KDPW zwrócił HTTP {exc.code}; nie ponawiam żądania.") from exc
            except BaseException as exc:
                self._failure(database, run_id, exc)
                raise

    def import_local(self, source: Path) -> dict:
        source = source.resolve()
        if not source.is_file():
            raise CollectorError(f"Nie znaleziono archiwum: {source}")
        if source.stat().st_size > MAX_ZIP_BYTES:
            raise CollectorError("ZIP przekracza limit 128 MiB.")
        with self._database() as database:
            run_id, destination = self._new_run(database, "local", str(source))
            temporary = destination.with_suffix(".zip.part")
            try:
                import hashlib

                digest = hashlib.sha256()
                size = 0
                with source.open("rb") as incoming, temporary.open("wb") as outgoing:
                    for block in iter(lambda: incoming.read(1024 * 1024), b""):
                        size += len(block)
                        if size > MAX_ZIP_BYTES:
                            raise CollectorError("ZIP przekracza limit 128 MiB.")
                        outgoing.write(block)
                        digest.update(block)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                temporary.replace(destination)
                return self._import(database, run_id, destination,
                                    {"zip_sha256": digest.hexdigest(), "zip_bytes": size})
            except BaseException as exc:
                self._failure(database, run_id, exc)
                raise
            finally:
                temporary.unlink(missing_ok=True)

    def resume(self) -> dict:
        with self._database() as database:
            previous = database.control()["paused_reason"]
            database.set_control(paused_reason=None)
            return {"status": "resumed" if previous else "not_paused", "control": database.control()}

    def status(self) -> dict:
        path = self.data_dir / "rze.sqlite3"
        if not path.exists():
            return {"status": "empty", "data_dir": str(self.data_dir)}
        # Read-only and without the collector lock: usable during a long import.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            control = dict(connection.execute("SELECT * FROM control WHERE id = 1").fetchone())
            result = {"data_dir": str(self.data_dir), "control": control}
            for key, condition in (("last_run", ""), ("last_successful_run", "WHERE r.status = 'complete'")):
                row = connection.execute(
                    "SELECT r.*, p.report_date, p.counts_json, p.xml_sha256 FROM runs r "
                    "LEFT JOIN reports p ON p.id = r.report_id " + condition + " ORDER BY r.id DESC LIMIT 1"
                ).fetchone()
                result[key] = dict(row) if row else None
                if row:
                    result[key]["counts"] = json.loads(result[key].pop("counts_json") or "{}")
                    result[key]["response_headers"] = json.loads(result[key].pop("response_headers_json") or "{}")
            result["report_versions"] = connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
            return result
