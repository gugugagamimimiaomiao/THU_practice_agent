from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from route_lookup import query_routes


class RouteLookupTests(unittest.TestCase):
    def test_no_key_returns_manual_map_fallback_without_network(self):
        with patch.dict(os.environ, {"AMAP_WEB_SERVICE_KEY": ""}, clear=False):
            result = query_routes(
                hotel="大理古城测试酒店",
                sites=[{"name": "大理州教育行政部门"}],
                city="大理州",
            )
        self.assertFalse(result["configured"])
        self.assertEqual(result["routes"], [])
        self.assertIn("不会猜测线路", result["message"])


if __name__ == "__main__":
    unittest.main()
