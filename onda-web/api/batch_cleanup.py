"""Private delayed cleanup: never dependent on an open browser or cron accuracy."""
from vercel.queue import subscribe
from .batch_store import HEX32, HEX64

@subscribe(topic='onda-batch-cleanup-v1',consumer_group='onda-batch-cleanup-v1',max_concurrency=1,max_attempts=3,retry_after=60)
async def garbage_collect(payload:dict[str,str]):
    if set(payload)!={'owner','batch'} or not HEX64.fullmatch(payload['owner']) or not HEX32.fullmatch(payload['batch']):return
    from .batch_worker import cleanup
    await cleanup(payload['owner'],payload['batch'])
