"""Si el respaldo diario deja de salir, se pierde todo el día que el servidor muera: el vigilante avisa."""
from __future__ import annotations

import os
import time
from pathlib import Path

from app.application.monitoring.health_watchdog import backup_problems

HOUR = 3600


def _write_status(directory: Path, **values: int) -> None:
    (directory / "status.env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def _dump(directory: Path, age_seconds: float, now: float) -> None:
    path = directory / "saaschatbot-x.dump"
    path.write_bytes(b"PGDMP")
    os.utime(path, (now - age_seconds, now - age_seconds))


def test_no_folder_means_nothing_to_watch(tmp_path: Path):
    assert backup_problems(tmp_path / "no-existe") == []


def test_healthy_backups_raise_nothing(tmp_path: Path):
    now = time.time()
    _dump(tmp_path, 2 * HOUR, now)
    fresh = int(now - 2 * HOUR)
    _write_status(
        tmp_path,
        LAST_DUMP_OK=fresh,
        LAST_FILES_OK=fresh,
        LAST_RESTORE_OK=fresh,
        OFFSITE_ENABLED=1,
        LAST_OFFSITE_OK=fresh,
    )
    assert backup_problems(tmp_path, now=now) == []


def test_old_dump_without_status_is_flagged(tmp_path: Path):
    now = time.time()
    _dump(tmp_path, 30 * HOUR, now)
    [problem] = backup_problems(tmp_path, now=now)
    assert "26 horas" in problem


def test_each_failing_step_is_named(tmp_path: Path):
    now = time.time()
    fresh, stale = int(now - HOUR), int(now - 40 * HOUR)
    _write_status(
        tmp_path,
        LAST_DUMP_OK=fresh,
        LAST_FILES_OK=fresh,
        LAST_RESTORE_OK=stale,
        OFFSITE_ENABLED=1,
        LAST_OFFSITE_OK=stale,
    )
    problems = backup_problems(tmp_path, now=now)
    assert any("restauración" in p for p in problems)
    assert any("fuera del servidor" in p for p in problems)
    assert len(problems) == 2

    _write_status(tmp_path, LAST_DUMP_OK=fresh, LAST_FILES_OK=fresh, LAST_RESTORE_OK=fresh, OFFSITE_ENABLED=0)
    assert backup_problems(tmp_path, now=now) == []
