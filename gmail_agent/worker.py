"""The same cycle powers one-shot runs and the local 30-minute daemon."""

import json
import logging
import random
import signal
import threading
import time

from gmail_agent.locking import worker_lock
from gmail_agent.replies import process_pending
from gmail_agent.sync import sync_mailbox

log = logging.getLogger(__name__)


def run_cycle(store, client, account_id, generator, *, limit=20):
    with store.db:
        store.start_cycle(account_id)
    try:
        result = sync_mailbox(store, client, account_id)
        result.update(process_pending(store, client, account_id, generator, limit=limit))
    except Exception as error:
        with store.db:
            store.finish_cycle(account_id, error)
        raise
    finally:
        generator.close()
    with store.db:
        store.finish_cycle(account_id)
    return result


def run_forever(store, client, account_id, generator, *, interval=1800, limit=20, stop=None):
    stop = stop or threading.Event()
    while not stop.is_set():
        started = time.monotonic()
        model_busy = False
        try:
            result = run_cycle(store, client, account_id, generator, limit=limit)
            model_busy = result["model_busy"]
            log.info("Cycle complete: %s", json.dumps(result, sort_keys=True))
        except Exception as error:  # noqa: BLE001 -- a failed cycle must not terminate the daemon
            # Provider errors can contain sensitive request data. Log only the class.
            log.error("Cycle failed (%s); retained cursor and queued work", type(error).__name__)
        delay = max(1, interval - (time.monotonic() - started))
        if model_busy:
            # Jitter breaks the shared login schedule so one account cannot
            # repeatedly lose the model lock to the same worker every cycle.
            delay = min(delay, random.uniform(60, 300))
        stop.wait(delay)


def run_locked(store, client, account_id, generator, *, once=False, interval=1800, limit=20):
    with worker_lock(store.path, account_id):
        try:
            if once:
                return run_cycle(store, client, account_id, generator, limit=limit)
            stop = threading.Event()
            previous = {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            try:
                run_forever(
                    store, client, account_id, generator, interval=interval, limit=limit, stop=stop
                )
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        finally:
            generator.close()
