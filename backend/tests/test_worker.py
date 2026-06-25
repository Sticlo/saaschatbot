from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.application.workers.worker_runtime import acquire_ai_slot, release_ai_slot


def _mock_redis():
    store: dict[str, str] = {}

    client = MagicMock()

    def set_(key, val, nx=False, ex=None):
        if nx:
            if key in store:
                return False
            store[key] = val
            return True
        store[key] = val
        return True

    def delete(key):
        store.pop(key, None)

    def exists(key):
        return key in store

    client.set = set_
    client.delete = delete
    client.exists = exists
    client.expire = MagicMock(return_value=True)
    return client, store


@patch("app.application.workers.worker_runtime.get_redis")
@patch("app.application.workers.worker_runtime.settings")
def test_acquire_and_release_ai_slot(mock_settings, mock_get_redis):
    mock_settings.ai_max_parallel_jobs = 3
    client, _store = _mock_redis()
    mock_get_redis.return_value = client

    slot = acquire_ai_slot(wait_seconds=1.0)
    assert slot is not None
    assert 0 <= slot < 3
    release_ai_slot(slot)

    slot2 = acquire_ai_slot(wait_seconds=1.0)
    assert slot2 is not None
    release_ai_slot(slot2)


@patch("app.worker._run_webhook")
def test_worker_cli_webhook(mock_run):
    from app.worker import main

    assert main(["webhook"]) == 0
    mock_run.assert_called_once()
