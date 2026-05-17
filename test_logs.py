import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

# Mocking core.config before importing core.logger and core.cron
import types
mock_config = types.ModuleType('core.config')
mock_config.config = types.SimpleNamespace(
    logging=types.SimpleNamespace(
        level='info',
        timezone='UTC'
    )
)
sys.modules['core.config'] = mock_config

from core.logger import logger
from core.cron import _trim_log_file

logs = Path('logs')
logs.mkdir(exist_ok=True)

log_path = logs / '__wipe_test.log'
jsonl_path = logs / '__wipe_test.jsonl'
trim_path = logs / '__trim_test.jsonl'

now = datetime.now(timezone.utc)
old = (now - timedelta(hours=2)).isoformat(timespec='milliseconds')
fresh = (now - timedelta(seconds=10)).isoformat(timespec='milliseconds')

log_path.write_text(f"{old} [INFO] [system] old line\n{fresh} [INFO] [system] fresh line\n", encoding='utf-8')
jsonl_path.write_text(
    json.dumps({"ts": old, "k": "old"}, ensure_ascii=False) + "\n" +
    json.dumps({"ts": fresh, "k": "fresh"}, ensure_ascii=False) + "\n",
    encoding='utf-8'
)

logger.wipe_logs(60)

log_text = log_path.read_text(encoding='utf-8')
jsonl_text = jsonl_path.read_text(encoding='utf-8')
wipe_ok = ('old line' not in log_text and 'fresh line' in log_text and '"k": "old"' not in jsonl_text and '"k": "fresh"' in jsonl_text)
print('WIPE_TEST', 'PASS' if wipe_ok else 'FAIL')

trim_lines = [json.dumps({"ts": fresh, "i": i, "payload": "x"*30}, ensure_ascii=False) for i in range(20)]
trim_path.write_text("\n".join(trim_lines) + "\n", encoding='utf-8')
removed = _trim_log_file(trim_path, 300)
size = trim_path.stat().st_size
trim_ok = removed > 0 and size <= 300
print('TRIM_TEST', 'PASS' if trim_ok else 'FAIL', 'removed=', removed, 'size=', size)

for p in (log_path, jsonl_path, trim_path):
    p.unlink(missing_ok=True)
