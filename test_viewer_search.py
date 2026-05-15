from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
import os
import sys

# Ensure the webui and core modules are importable
sys.path.append(os.getcwd())

from webui.backend.app import create_app

class FakeCore:
    def __init__(self):
        self.config = MagicMock()
        self.config.get.return_value = {}
        self.config.get_raw.return_value = {}
        self.redis = MagicMock()
        self.queue = MagicMock()
        self.workers = MagicMock()
        self.envid_registry = MagicMock()

def test_viewer_search():
    fake_core = FakeCore()
    
    # Mocking session/auth. Note: monkeypatching _get_session in the webui.backend.app module
    # or wherever it's used. Since we import create_app from webui.backend.app,
    # we patch it there.
    with patch('webui.backend.app._get_session', return_value={'permissions': ['all'], 'user': 'test_user'}):
        app = create_app(core=fake_core)
        client = TestClient(app)

        urls = [
            "/api/tasks/viewer/search",
            "/api/tasks/viewer/search?status=completed",
            "/api/tasks/viewer/search?envid=test-12345"
        ]

        # We also need to mock the core's task store or whatever the route uses.
        # Minimal FakeCore might need search_tasks or similar.
        # Let's see if it works or which attribute is missing.
        fake_core.search_tasks = MagicMock(return_value=[])

        for url in urls:
            response = client.get(url)
            print(f"URL: {url}")
            print(f"Status: {response.status_code}")
            try:
                data = response.json()
                if isinstance(data, dict):
                    count = data.get('total', data.get('count', len(data.get('tasks', []))))
                else:
                    count = len(data)
                print(f"Count: {count}")
            except:
                print("JSON parsing failed")
            print("-" * 10)

if __name__ == "__main__":
    try:
        test_viewer_search()
    except Exception as e:
        print(f"Execution failed: {e}")
        import traceback
        traceback.print_exc()
