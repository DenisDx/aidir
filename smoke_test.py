import os
import time
import json
import redis
from core.config import Config
from core.queue_manager import QueueManager
from core.task import Task

def test():
    try:
        # 1) Loads config and connects to Redis
        config = Config()
        redis_host = config.get("redis.host", "127.0.0.1")
        redis_port = config.get("redis.port", 6379)
        redis_pass = config.get("redis.password")
        
        r = redis.Redis(host=redis_host, port=redis_port, password=redis_pass, decode_responses=True)
        r.ping()
        print("1) Redis connection: PASS")

        # 2) Creates QueueManager
        qm = QueueManager(config)
        print("2) QueueManager creation: PASS")

        # 3) Create Task and run transitions
        payload = {'envid': 'test-12345'}
        task = Task(type='agent', external=True, payload=payload)
        
        def get_stats(tid):
            return r.hgetall(f"task:{tid}")

        # Transition: add_task
        qm.add_task(task)
        s1 = get_stats(task.id)
        print(f"3.1) add_task: status={s1.get('status')}, created_at={s1.get('created_at')}, updated_at={s1.get('updated_at')}")
        
        time.sleep(1.1)
        # Transition: mark_running
        qm.mark_running(task.id)
        s2 = get_stats(task.id)
        print(f"3.2) mark_running: status={s2.get('status')}, started_at={s2.get('started_at')}, updated_at={s2.get('updated_at')}")

        time.sleep(1.1)
        # Transition: mark_completed
        qm.mark_completed(task.id)
        s3 = get_stats(task.id)
        print(f"3.3) mark_completed: status={s3.get('status')}, finished_at={s3.get('finished_at')}, updated_at={s3.get('updated_at')}")

        # 5) Verifies created_at is set and updated_at changes
        checks = []
        checks.append(s1.get('created_at') is not None)
        checks.append(s2.get('updated_at') != s1.get('updated_at'))
        checks.append(s3.get('updated_at') != s2.get('updated_at'))
        print(f"5) Verifications (created_at exists, updated_at changes): {'PASS' if all(checks) else 'FAIL'}")

        # 6) Calls delete_task and verifies hash still exists (external=True)
        qm.delete_task(task.id)
        exists = r.exists(f"task:{task.id}")
        print(f"6) delete_task (external=True) - Hash still exists: {'PASS' if exists else 'FAIL'}")

        # 7) Cleans up
        r.delete(f"task:{task.id}")
        print("7) Cleanup: PASS")

    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    test()
