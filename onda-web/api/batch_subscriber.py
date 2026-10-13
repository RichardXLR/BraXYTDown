"""Private Vercel Queues entrypoint; separate from the public FastAPI function."""
import asyncio
import os
import time
from vercel.queue import subscribe
from .batch_store import BatchStore, Conflict, HEX32, HEX64, probe_path

TOPIC = 'onda-media-batches-v1'

@subscribe(topic=TOPIC, consumer_group='onda-media-batches-v1', max_concurrency=1, max_attempts=5, retry_after=120)
async def consume(payload: dict[str, str]):
    if set(payload) == {'probe', 'owner', 'deployment'}:
        if payload['deployment'] != os.environ.get('VERCEL_DEPLOYMENT_ID'):
            return
        def complete():
            path = probe_path(payload['deployment'], payload['owner'], payload['probe'])
            store = BatchStore()
            value, etag = store.read(path)
            if not value or value.get('expires', 0) < time.time() or value.get('completed'):
                return
            value['completed'] = True
            value['completedAt'] = time.time()
            try:
                store.write(path, value, etag)
            except Conflict:
                return
        await asyncio.to_thread(complete)
        return
    if set(payload)=={'owner','batch'} and HEX64.fullmatch(payload['owner']) and HEX32.fullmatch(payload['batch']):
        from .batch_worker import process
        await process(payload['owner'],payload['batch'])
