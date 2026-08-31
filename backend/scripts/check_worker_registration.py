"""Fails loudly if a live celery-worker doesn't actually have every task
Kerygma expects registered — the exact class of bug the Phase 4 Task 3
live end-to-end test caught by hand: celery-worker/celery-beat build
their OWN separate images from the same Dockerfile as backend (a
docker-compose quirk — see docker-compose.yml), so rebuilding `backend`
and recreating `celery-worker`'s container is NOT enough to give it new
code. The container comes up looking healthy either way; the only
visible symptom is a silently discarded task at runtime, hours or days
later, which is exactly what happened here. Nothing in the test suite
could have caught it — every task test runs the task in-process via
`.apply()`, which bypasses real worker task discovery entirely.

Self-maintaining, not a hardcoded list: imports every module
celery_app.conf.imports names (the same modules the real worker imports
at startup) and takes whatever ends up registered locally, minus
Celery's own built-ins, as "expected" — so a future task file added to
`imports` is covered automatically, with nothing here to remember to
update.

Usage:
    python scripts/check_worker_registration.py

Exit 0: every expected task is registered on at least one live worker.
Exit 1: a worker responded but is missing one or more expected tasks
        (the stale-image bug), OR no worker responded at all within the
        timeout (broker unreachable, or no worker running).
Meant to run as celery-worker's own Docker HEALTHCHECK (see
docker-compose.yml) — CMD-style, not part of the pytest suite, since it
needs a real running worker process to check, which pytest can't spin up.
"""
import importlib
import sys

sys.path.insert(0, ".")  # run from backend/, matches this repo's other scripts/alembic

from app.tasks.celery_app import celery_app  # noqa: E402

_BUILTIN_PREFIX = "celery."


def expected_tasks() -> set[str]:
    for module_name in celery_app.conf.imports:
        importlib.import_module(module_name)
    return {name for name in celery_app.tasks if not name.startswith(_BUILTIN_PREFIX)}


def main() -> int:
    expected = expected_tasks()
    if not expected:
        print("check_worker_registration: no task modules configured in celery_app.conf.imports — nothing to check")
        return 0

    inspector = celery_app.control.inspect(timeout=5)
    registered = inspector.registered() or {}

    if not registered:
        print("check_worker_registration: FAIL — no celery worker responded within 5s (broker down, or no worker running)")
        return 1

    ok = True
    for worker_name, task_names in registered.items():
        missing = expected - set(task_names)
        if missing:
            ok = False
            print(f"check_worker_registration: FAIL — {worker_name} is missing: {sorted(missing)}")
            print("  (this is exactly the stale-image bug: rebuild AND recreate this worker's own image, not just backend's)")

    if ok:
        print(f"check_worker_registration: OK — {len(registered)} worker(s), all {len(expected)} expected task(s) registered")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
