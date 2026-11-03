import json
import tempfile
import unittest
from pathlib import Path

import mcp_server


class SharedConnectionCatalogTests(unittest.TestCase):
    def test_only_shared_safe_metadata_is_exposed(self):
        records = [
            {
                "id": "shared",
                "name": "Shared API",
                "target": "https://example.com",
                "detail": "Reachable",
                "kind": "API",
                "state": "Active",
                "tags": ["api", "shared"],
                "isCustom": True,
                "secretName": "SHARED_TOKEN",
            },
            {
                "id": "private",
                "name": "Hermes only",
                "target": "private-command",
                "detail": "",
                "kind": "MCP",
                "state": "Active",
                "tags": [],
                "isCustom": False,
                "agentID": "hermes",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "connections.json"
            path.write_text(json.dumps(records))
            result = mcp_server.shared_connections(path)

        self.assertEqual([item["id"] for item in result], ["shared"])
        self.assertTrue(result[0]["credential_stored"])
        self.assertNotIn("secretName", result[0])
        self.assertNotIn("instructions", result[0])


if __name__ == "__main__":
    unittest.main()
