"""Entry point: `python -m runwatch.worker`."""

import logging
import signal
import sys
import threading

from runwatch.logs import LogSettingsError, configure_logging
from runwatch.telemetry import (
    WORKER_SERVICE,
    build_logger_provider,
    build_tracer_provider,
    set_tracer_provider,
)
from runwatch.worker.client import HttpWorkerApi
from runwatch.worker.runner import Worker
from runwatch.worker.settings import WorkerSettingsError, load_worker_settings


def main() -> int:
    logs = build_logger_provider(WORKER_SERVICE)
    try:
        configure_logging(WORKER_SERVICE, logs)
        settings = load_worker_settings()
    except (LogSettingsError, WorkerSettingsError) as exc:
        print(exc, file=sys.stderr)
        return 2
    provider = build_tracer_provider(WORKER_SERVICE)
    set_tracer_provider(provider)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    worker = Worker(
        HttpWorkerApi(settings.runwatch_api_url),
        worker_id=settings.worker_id,
        worker_fault=settings.worker_fault,
        stop=stop,
    )
    logging.getLogger(__name__).info(
        "worker started worker_id=%s",
        settings.worker_id,
        extra={"worker_id": settings.worker_id},
    )
    try:
        worker.run()
    finally:
        provider.shutdown()
        logs.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
