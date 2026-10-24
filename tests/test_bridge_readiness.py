import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

import tests.test_mac_bridge as fixtures


class BridgeReadinessTest(unittest.TestCase):
    request = fixtures.BridgeApiTest.request
    setUp = fixtures.BridgeApiTest.setUp
    tearDown = fixtures.BridgeApiTest.tearDown

    def test_readiness_distinguishes_live_process_from_unavailable_transport(self):
        self.app_server.transport_status = Mock(return_value={
            "ready": False, "state": "unavailable", "retryAfterSeconds": 2,
        })
        self.app_server.start = Mock(side_effect=AssertionError("readiness must not start transport"))
        self.controller.status = Mock(side_effect=AssertionError("readiness must not probe Desktop"))
        self.assertEqual(self.request("GET", "/health/live", authorized=False)[0], 200)
        status, result = self.request("GET", "/health/ready", authorized=False)
        self.assertEqual(status, 503)
        self.assertFalse(result["ok"])
        self.assertEqual(result["appServer"]["retryAfterSeconds"], 2)
        self.app_server.start.assert_not_called()
        self.controller.status.assert_not_called()
        self.app_server.transport_status.return_value = {
            "ready": True, "state": "ready", "retryAfterSeconds": 0,
        }
        self.assertEqual(self.request("GET", "/health/ready", authorized=False)[0], 200)

    def test_status_timeout_returns_structured_error(self):
        with patch.object(self.controller, "status", side_effect=subprocess.TimeoutExpired("helper", 25)):
            status, result = self.request("GET", "/api/desktop/interrupt/status")
        self.assertEqual(status, 502)
        self.assertEqual(result, {"ok": False, "error": "accessibility_probe_failed", "reason": "probe_timeout"})

    def test_display_probes_are_singleflight_and_explicitly_invalidated(self):
        entered, release = threading.Event(), threading.Event()
        count = 0
        def probe():
            nonlocal count
            count += 1
            entered.set()
            self.assertTrue(release.wait(2))
            return {"taskTitle": "task", "stopCandidates": 0, "request": None}
        self.controller.status = probe
        results = []
        workers = [threading.Thread(target=lambda: results.append(self.server.desktop_status()))
                   for _ in range(8)]
        for worker in workers:
            worker.start()
        self.assertTrue(entered.wait(1))
        release.set()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 8)
        self.assertEqual(count, 1)
        self.server.desktop_status()
        self.assertEqual(count, 1)
        self.server.invalidate_desktop_status()
        self.server.desktop_status()
        self.assertEqual(count, 2)

    def test_invalidation_during_probe_does_not_cache_an_old_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        calls = 0
        def probe():
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                self.assertTrue(release.wait(2))
                return {"taskTitle": "old", "stopCandidates": 0}
            return {"taskTitle": "new", "stopCandidates": 1}
        self.controller.status = probe
        results = []
        worker = threading.Thread(target=lambda: results.append(self.server.desktop_status()))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.server.invalidate_desktop_status()
        finally:
            release.set()
            worker.join(2)
        self.assertEqual(results[0]["taskTitle"], "old")
        self.assertEqual(self.server.desktop_status()["taskTitle"], "new")
        self.assertEqual(calls, 2)

    def test_mac_receipt_capability_does_not_change_native_ui_mode(self):
        _, result = self.request("GET", "/health")
        self.assertTrue(result["capabilities"]["deliveryReceipts"])
        self.assertNotIn("nativeTextSend", result["capabilities"])
