"""A database file that disappears is not silently recreated, and readiness
says why in the log (H16, OPS-8).

Before: ``/readyz`` failed with no reason logged, and a connection to a
missing database file created an empty one, so every request after that
failed on a missing table while the instance looked alive and the original
loss was hidden behind a new, empty file.
"""

from __future__ import annotations

import logging
import os
import shutil

from fastapi.testclient import TestClient

from support import ADMIN, bootstrap, make_app

from athena.core import db


def _remove(db_file) -> None:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(f"{db_file}{suffix}"):
            os.unlink(f"{db_file}{suffix}")


def _lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "database" in record.getMessage()
    ]


def test_readiness_logs_why_once_and_does_not_recreate_the_file(tmp_path, caplog):
    app, db_file = make_app(tmp_path)
    kept = tmp_path / "kept.db"
    with TestClient(app) as client, caplog.at_level(logging.WARNING, logger="athena"):
        bootstrap(client)
        assert client.get("/readyz").status_code == 200
        # A consistent copy to bring back later.
        conn = db.connect(db_file)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        shutil.copy(db_file, kept)
        _remove(db_file)
        caplog.clear()
        probes = [client.get("/readyz") for _ in range(3)]
        gone = db_file.exists()
        while_missing = _lines(caplog)
        shutil.copy(kept, db_file)
        caplog.clear()
        back = [client.get("/readyz") for _ in range(2)]
        after = _lines(caplog)

    assert [probe.status_code for probe in probes] == [503, 503, 503]
    # The probe's caller learns nothing more than before.
    assert probes[0].json() == {
        "status": "error",
        "database": "unavailable",
        "code": "unavailable",
    }
    assert gone is False
    # Once, with the reason and the path, however often it is probed.
    assert len(while_missing) == 1
    assert "database unavailable" in while_missing[0]
    assert "database file is missing" in while_missing[0]
    assert str(db_file) in while_missing[0]
    assert [probe.status_code for probe in back] == [200, 200]
    assert len(after) == 1 and "available again" in after[0]


def test_a_request_is_answered_unavailable_and_nothing_is_created(tmp_path, caplog):
    app, db_file = make_app(tmp_path)
    with TestClient(app) as client, caplog.at_level(logging.WARNING, logger="athena"):
        bootstrap(client)
        _remove(db_file)
        caplog.clear()
        answers = [client.get("/issues", headers=ADMIN) for _ in range(2)]
        logged = _lines(caplog)

    for answer in answers:
        assert answer.status_code == 503, answer.text
        assert answer.json() == {
            "detail": "the database is unavailable",
            "code": "unavailable",
        }
    assert not db_file.exists()
    assert len(logged) == 1 and str(db_file) in logged[0]


def test_a_first_start_still_creates_its_database(tmp_path):
    app, db_file = make_app(tmp_path)
    assert not db_file.exists()
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        assert db_file.exists()
        served = os.path.abspath(db_file) in db._SERVED
    # Once the app has stopped the path is no longer served: a command or a
    # next start may create it again.
    assert served and os.path.abspath(db_file) not in db._SERVED
    _remove(db_file)
    conn = db.connect(db_file)
    conn.close()
    assert db_file.exists()


def test_two_apps_on_one_file_do_not_end_each_others_claim(tmp_path):
    path = tmp_path / "shared.db"
    db.connect(path).close()
    db.begin_serving(path)
    db.begin_serving(path)
    db.end_serving(path)
    try:
        assert os.path.abspath(path) in db._SERVED
    finally:
        db.end_serving(path)
    assert os.path.abspath(path) not in db._SERVED
    db.end_serving(path)  # one too many is harmless
    assert db._SERVED.get(os.path.abspath(path)) is None
