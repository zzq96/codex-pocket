import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from delivery_journal import DeliveryJournal


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "manage-delivery-receipts.py"
SPEC = importlib.util.spec_from_file_location("receipt_cli", SCRIPT)
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class DeliveryReceiptCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "delivery.sqlite"
        self.journal = DeliveryJournal(self.path)
        self.payload = {"requestId": "11111111-1111-4111-8111-111111111111",
                        "message": "private prompt excluded from CLI output"}
        self.journal.claim("device-a", "thread-a", self.payload)

    def invoke(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = CLI.main(["--journal", str(self.path), *args])
        return code, output.getvalue()

    def resolution_args(self):
        return ["resolve", "--owner", "device-a", "--request-id", self.payload["requestId"],
                "--outcome", "refused"]

    def test_list_exposes_only_receipt_metadata(self):
        code, output = self.invoke("list")
        self.assertEqual(code, 0)
        rows = json.loads(output)
        self.assertEqual(rows[0]["state"], "claimed")
        self.assertEqual(set(rows[0]), set(CLI.PUBLIC_FIELDS))
        self.assertNotIn(self.payload["message"], output)
        self.assertNotIn("fingerprint", output)

    def test_resolution_requires_explicit_manual_acknowledgement(self):
        with patch.object(CLI, "require_stopped_bridge") as probe:
            with self.assertRaises(SystemExit) as caught:
                self.invoke(*self.resolution_args())
        self.assertEqual(caught.exception.code, 2)
        probe.assert_not_called()
        self.assertEqual(self.journal.get("device-a", self.payload["requestId"])["state"], "claimed")

    def test_running_bridge_prevents_resolution(self):
        with patch.object(CLI.socket, "create_connection") as connect:
            with self.assertRaises(SystemExit):
                self.invoke(*self.resolution_args(), "--confirm-stopped-and-reviewed")
        connect.return_value.close.assert_called_once()
        self.assertEqual(self.journal.get("device-a", self.payload["requestId"])["state"], "claimed")

    def test_manual_resolution_keeps_old_id_read_only_and_allows_a_new_id(self):
        with patch.object(CLI.socket, "create_connection", side_effect=ConnectionRefusedError):
            code, output = self.invoke(*self.resolution_args(), "--confirm-stopped-and-reviewed")
        self.assertEqual(code, 0)
        self.assertIn("refused", output)
        replay = self.journal.claim("device-a", "thread-a", self.payload)
        self.assertFalse(replay["dispatch"])
        self.assertEqual(replay["state"], "refused")
        next_payload = dict(self.payload, requestId="22222222-2222-4222-8222-222222222222")
        self.assertTrue(self.journal.claim("device-a", "thread-a", next_payload)["dispatch"])

    def test_missing_database_is_not_created(self):
        missing = Path(self.temp.name) / "does-not-exist.sqlite"
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            CLI.main(["--journal", str(missing), "list"])
        self.assertFalse(missing.exists())

    def test_abandon_unseen_retires_late_original_request(self):
        unseen = dict(self.payload, requestId="33333333-3333-4333-8333-333333333333")
        with patch.object(CLI.socket, "create_connection", side_effect=ConnectionRefusedError):
            code, _ = self.invoke("abandon-unseen", "--owner", "device-b",
                                  "--request-id", unseen["requestId"], "--scope", "thread-b",
                                  "--confirm-stopped-and-reviewed")
        self.assertEqual(code, 0)
        delayed = self.journal.claim("device-b", "thread-b", unseen)
        self.assertEqual(delayed["state"], "refused")
        self.assertFalse(delayed["dispatch"])
