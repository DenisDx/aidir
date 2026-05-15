import asyncio
import redis.asyncio as aioredis
from core.config import config
import json

async def get_task_data(r, key):
    data = await r.hgetall(key)
    if not data:
        return None
    
    external_str = data.get('external', '{}')
    envid = None
    if external_str:
        try:
            external = json.loads(external_str)
            if isinstance(external, dict):
                envid = external.get('envid')
        except:
            pass
    
    status = data.get('status')
    return {
        'id': key.split(':task:')[-1],
        'status': status,
        'envid': envid,
        'key': key
    }

async def main():
    rcfg = config.get('redis') or {}
    host = rcfg.get('host', '127.0.0.1')
    port = int(rcfg.get('port', 6379))
    pwd = rcfg.get('password')
    ns = config.get('instance', 'aidir')
    url = f"redis://:{pwd}@{host}:{port}" if pwd else f"redis://{host}:{port}"
    r = aioredis.from_url(url, decode_responses=True)

    try:
        await r.ping()
    except Exception as e:
        print(f"Connection error: {e}")
        return

    pattern = f"{ns}:task:*"
    tasks = []
    async for key in r.scan_iter(match=pattern, count=500):
        t = await get_task_data(r, key)
        if t:
            tasks.append(t)

    def filter_tasks(status=None, envid=None):
        res = tasks
        if status:
            res = [t for t in res if t['status'] == status]
        if envid:
            res = [t for t in res if t['envid'] == envid]
        return res

    cases = [
        ("no filters", {}),
        ("status=completed", {"status": "completed"}),
        ("status=running", {"status": "running"}),
        ("envid=test-12345", {"envid": "test-12345"}),
        ("status=completed + envid=test-12345", {"status": "completed", "envid": "test-12345"}),
    ]

    print(f"INSTANCE: {ns}")
    print(f"Total tasks scanned: {len(tasks)}")
    for name, filters in cases:
        filtered = filter_tasks(**filters)
        print(f"Count for {name}: {len(filtered)}")
        if "envid" in filters and len(filtered) == 0:
            # Check if any envid exists to see if the filter was just non-matching
            envids = {t['envid'] for t in tasks if t['envid']}
            if envids and name == "envid=test-12345":
                print(f"Available envids sample: {list(envids)[:3]}")

    await r.aclose()

if __name__ == "__main__":
    asyncio.run(main())
