"""Entry point: `python -m runwatch.worker`."""

import logging
import signal
import sys
import threading

from runwatch.worker.client import HttpWorkerApi
from runwatch.worker.runner import Worker
from runwatch.worker.settings import WorkerSettingsError, load_worker_settings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_worker_settings()
    except WorkerSettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    worker = Worker(
        HttpWorkerApi(settings.runwatch_api_url),
        worker_id=settings.worker_id,
        worker_fault=settings.worker_fault,
        stop=stop,
    )
    logging.getLogger(__name__).info("worker started worker_id=%s", settings.worker_id)
    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
