import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from codex_app_server import AppServerError, ManagedRequestError
from delivery_journal import DeliveryJournal, user_receipt_ids
import tests.test_mac_bridge as fixtures


RID = "00000000-0000-0000-0000-000000000001"
RID2 = "00000000-0000-0000-0000-000000000002"


def message_turn(message="hello", *, item="new-user", turn="new-turn"):
    return {"id": turn, "status": "completed", "items": [
        {"id": item, "type": "userMessage", "content": [{"type": "text", "text": message}]},
    ]}


class DeliveryJournalTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "delivery.sqlite"
        self.journal = DeliveryJournal(self.path)
        self.payload = {"requestId": RID, "message": "private fixture prompt"}

    def test_committed_claim_survives_restart_and_never_dispatches_twice(self):
        self.assertTrue(self.journal.claim("phone", "thread-1", self.payload)["dispatch"])
        restarted = DeliveryJournal(self.path)
        self.assertFalse(restarted.claim("phone", "thread-1", self.payload)["dispatch"])
        self.assertNotIn(b"private fixture prompt", self.path.read_bytes())

    def test_receipt_only_missing_request_never_claims(self):
        self.assertIsNone(self.journal.claim("phone", "thread-1", self.payload, receipt_only=True))
        self.assertTrue(self.journal.claim("phone", "thread-1", self.payload)["dispatch"])

    def test_unseen_request_can_be_abandoned_without_prompt_or_dispatch(self):
        self.assertIsNone(self.journal.claim("phone", "create", self.payload, receipt_only=True))
        record = self.journal.abandon_unseen("phone", RID, "create")
        self.assertEqual(record["state"], "refused")
        self.assertEqual(record["phase"], "abandoned_unseen")
        self.assertEqual(record["fingerprint"], "")
        self.assertIsNone(record["thread_id"])
        restarted = DeliveryJournal(self.path)
        receipt = restarted.claim("phone", "create", self.payload, receipt_only=True)
        self.assertFalse(receipt["dispatch"])
        self.assertTrue(receipt["result"]["retryAllowed"])
        self.assertFalse(restarted.claim("phone", "create", self.payload)["dispatch"])
        self.assertNotIn(b"private fixture prompt", self.path.read_bytes())
        self.assertTrue(restarted.claim("phone", "create", {**self.payload, "requestId": RID2})["dispatch"])

    def test_unseen_abandonment_cannot_overwrite_claim_or_accept_invalid_scope(self):
        self.journal.claim("phone", "thread-1", self.payload)
        before = self.journal.get("phone", RID)
        with self.assertRaisesRegex(ValueError, "delivery_already_exists"):
            self.journal.abandon_unseen("phone", RID, "thread-1")
        self.assertEqual(self.journal.get("phone", RID), before)
        for scope in ("", " ", " a", "a/b", "a\\b", "a\n", "a" * 129):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, "invalid_delivery_scope"):
                self.journal.abandon_unseen("phone", RID2, scope)

    def test_concurrent_claim_has_one_dispatcher(self):
        barrier = threading.Barrier(8)
        results = []
        def claim():
            barrier.wait(2)
            results.append(self.journal.claim("phone", "thread-1", self.payload)["dispatch"])
        workers = [threading.Thread(target=claim) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(results), 1)

    def test_identity_reuse_rejected_and_pending_blocks_fresh_ids(self):
        self.journal.claim("phone", "thread-1", self.payload)
        with self.assertRaisesRegex(ValueError, "different_content"):
            self.journal.claim("phone", "thread-1", {**self.payload, "message": "different"})
        with self.assertRaisesRegex(ValueError, "confirmation_pending"):
            self.journal.claim("phone", "thread-1", {**self.payload, "requestId": RID2})

    def test_device_identity_scopes_receipt_reads(self):
        self.journal.claim("phone", "thread-1", self.payload)
        self.assertIsNone(self.journal.claim("other-phone", "thread-1", self.payload, receipt_only=True))

    def test_creation_receipt_can_be_read_from_its_thread_without_reallocation(self):
        created = {**self.payload, "projectId": "project-a"}
        self.journal.claim("phone", "create", created)
        self.journal.advance("phone", RID, "created", thread_id="thread-created")
        self.assertFalse(self.journal.claim("phone", "thread-created", self.payload)["dispatch"])
        with self.assertRaisesRegex(ValueError, "different_content"):
            self.journal.claim("phone", "thread-other", self.payload)

    def test_confirmed_receipt_cannot_be_downgraded_by_late_timeout(self):
        self.journal.claim("phone", "thread-1", self.payload)
        self.journal.finish("phone", RID, "confirmed", 202, {"ok": True})
        result = self.journal.finish("phone", RID, "uncertain", 409, {"ok": False})
        self.assertEqual(result["state"], "confirmed")
        self.assertTrue(result["result"]["ok"])

    def test_uncertain_delivery_needs_explicit_local_resolution(self):
        self.journal.claim("phone", "thread-1", self.payload)
        self.journal.finish("phone", RID, "uncertain", 409, {"ok": False})
        with self.assertRaisesRegex(ValueError, "operator_resolution"):
            self.journal.finish("phone", RID, "refused", 409, {"ok": False})
        resolved = self.journal.resolve_pending("phone", RID, "refused")
        self.assertTrue(resolved["result"]["retryAllowed"])
        self.assertFalse(self.journal.claim("phone", "thread-1", self.payload)["dispatch"])
        self.assertTrue(self.journal.claim("phone", "thread-1", {**self.payload, "requestId": RID2})["dispatch"])

    def test_local_confirmation_requires_exact_owner_and_known_thread_identity(self):
        self.journal.claim("phone", "thread-1", self.payload)
        with self.assertRaisesRegex(ValueError, "unknown_delivery_id"):
            self.journal.resolve_pending("other-phone", RID, "confirmed")
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            self.journal.resolve_pending("phone", RID, "confirmed", thread_id="thread-other")
        result = self.journal.resolve_pending("phone", RID, "confirmed")
        self.assertEqual(result["result"]["desktop"]["confirmedBy"], "operator")
        with self.assertRaisesRegex(ValueError, "already_resolved"):
            self.journal.resolve_pending("phone", RID, "refused")

    def test_unknown_creation_can_be_resolved_without_reallocating(self):
        self.journal.claim("phone", "create", self.payload)
        self.journal.advance("phone", RID, "creating")
        with self.assertRaisesRegex(ValueError, "requires_thread_id"):
            self.journal.resolve_pending("phone", RID, "confirmed")
        for thread_id in ("", "a/b", "a" * 129, "a\n"):
            with self.subTest(thread_id=thread_id), self.assertRaisesRegex(ValueError, "invalid_thread_id"):
                self.journal.resolve_pending("phone", RID, "confirmed", thread_id=thread_id)
        result = self.journal.resolve_pending("phone", RID, "confirmed", thread_id="thread-created")
        self.assertEqual(result["thread_id"], "thread-created")
        self.assertFalse(self.journal.claim("phone", "create", self.payload)["dispatch"])

    def test_receipts_require_exact_text_thread_and_stable_item_ids(self):
        thread = {"id": "thread-1", "turns": [message_turn("hello")]}
        self.assertEqual(len(user_receipt_ids(thread, "thread-1", "hello")), 1)
        self.assertEqual(user_receipt_ids(thread, "other", "hello"), set())
        self.assertEqual(user_receipt_ids(thread, "thread-1", "hell"), set())
        thread["turns"][0]["items"][0].pop("id")
        self.assertEqual(user_receipt_ids(thread, "thread-1", "hello"), set())


class MacDeliveryApiTest(unittest.TestCase):
    request = fixtures.BridgeApiTest.request
    raw_request = fixtures.BridgeApiTest.raw_request
    enroll_device = fixtures.BridgeApiTest.enroll_device
    upload_attachment = fixtures.BridgeApiTest.upload_attachment

    def setUp(self):
        fixtures.BridgeApiTest.setUp(self)
        self.history = {"thread-1": self.app_server.read_thread("thread-1")["thread"]}
        original_create = self.app_server.create_thread
        def create(**kwargs):
            created = original_create(**kwargs)
            self.history[created["thread"]["id"]] = {**created["thread"], "turns": []}
            return created
        self.app_server.create_thread = create
        self.app_server.read_thread = lambda thread_id: {"thread": self.history[thread_id]}

    def tearDown(self):
        fixtures.BridgeApiTest.tearDown(self)

    def send(self, **extra):
        return self.request("POST", "/api/codex/threads/thread-1/turn",
                            {"requestId": RID, "message": "hello", **extra})

    def test_existing_turn_reply_is_durable_and_duplicate_never_sends(self):
        status, first = self.send()
        self.assertEqual(status, 202)
        self.assertFalse(first["delivery"]["replayed"])
        self.server.delivery_journal = DeliveryJournal(self.server.delivery_journal.path)
        status, second = self.send()
        self.assertEqual(status, 202)
        self.assertTrue(second["delivery"]["replayed"])
        self.assertEqual(second["delivery"]["requestId"], RID)
        self.assertEqual(len(self.controller.desktop_sent), 1)

    def test_receipt_only_never_dispatches_missing_request(self):
        status, result = self.send(receiptOnly=True)
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "delivery_receipt_pending")
        self.assertEqual(self.controller.desktop_sent, [])

    def test_late_original_post_after_local_abandonment_cannot_send_or_create(self):
        for request_id, scope, path in (
            (RID, "thread-1", "/api/codex/threads/thread-1/turn"),
            (RID2, "create", "/api/codex/threads"),
        ):
            with self.subTest(scope=scope):
                self.server.delivery_journal.abandon_unseen("legacy-master", request_id, scope)
                status, result = self.request("POST", path, {"requestId": request_id, "message": "hello"})
                self.assertEqual(status, 409)
                self.assertEqual(result["error"], "delivery_manually_refused")
                self.assertEqual(result["delivery"]["requestId"], request_id)
                self.assertTrue(result["delivery"]["replayed"])
                self.assertTrue(result["retryAllowed"])
        self.assertEqual(self.controller.desktop_sent, [])
        self.assertEqual(self.app_server.created_threads, [])

    def test_invalid_input_can_be_corrected_without_an_unresolvable_claim(self):
        status, result = self.send(message="")
        self.assertEqual(status, 400)
        self.assertTrue(result["retryAllowed"])
        self.assertEqual(self.send()[0], 202)
        self.assertEqual(len(self.controller.desktop_sent), 1)

    def test_unknown_send_is_reconciled_by_exact_new_history_without_second_click(self):
        self.controller.dispatch_error = "desktop_send_unconfirmed"
        with patch("mac_bridge.desktop_message_landed", return_value=False):
            status, result = self.send()
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "delivery_uncertain")
        self.assertFalse(result["retryAllowed"])
        self.history["thread-1"]["turns"].append(message_turn())
        self.controller.send_to_desktop = Mock(side_effect=AssertionError("must not dispatch"))
        status, result = self.send(receiptOnly=True)
        self.assertEqual(status, 202)
        self.assertEqual(result["desktop"]["confirmedBy"], "threadHistory")
        self.assertTrue(result["delivery"]["receiptOnly"])
        self.controller.send_to_desktop.assert_not_called()

    def test_old_identical_message_cannot_confirm_an_uncertain_send(self):
        self.history["thread-1"]["turns"].append(message_turn())
        self.controller.dispatch_error = "desktop_send_unconfirmed"
        with patch("mac_bridge.desktop_message_landed", return_value=False):
            self.send()
        status, result = self.send(receiptOnly=True)
        self.assertEqual(status, 409)
        self.assertFalse(result["retryAllowed"])

    def test_rejected_preflight_can_use_a_new_id_but_old_id_never_dispatches(self):
        self.controller.dispatch_error = "desktop_draft_present"
        status, result = self.send()
        self.assertEqual(status, 409)
        self.assertTrue(result["retryAllowed"])
        self.controller.dispatch_error = None
        self.assertEqual(self.send()[0], 409)
        self.assertEqual(self.controller.desktop_sent, [])
        self.assertEqual(self.send(requestId=RID2)[0], 202)
        self.assertEqual(len(self.controller.desktop_sent), 1)

    def test_new_task_replays_same_created_thread_and_supports_turn_receipt(self):
        body = {"requestId": RID, "projectId": "project-id", "message": "hello"}
        status, first = self.request("POST", "/api/codex/threads", body)
        self.assertEqual(status, 202)
        thread_id = first["thread"]["id"]
        status, second = self.request("POST", "/api/codex/threads", body)
        self.assertEqual(status, 202)
        self.assertEqual(second["thread"]["id"], thread_id)
        status, receipt = self.request("POST", f"/api/codex/threads/{thread_id}/turn",
                                       {"requestId": RID, "message": "hello", "receiptOnly": True})
        self.assertEqual(status, 202)
        self.assertEqual(receipt["threadId"], thread_id)
        self.assertEqual(len(self.app_server.created_threads), 1)
        self.assertEqual(len(self.controller.desktop_sent), 1)

    def test_allocation_timeout_is_durable_unknown_and_never_allocated_again(self):
        create = Mock(side_effect=AppServerError("transport lost after create"))
        self.app_server.create_thread = create
        body = {"requestId": RID, "projectId": "project-id", "message": "hello"}
        status, result = self.request("POST", "/api/codex/threads", body)
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "delivery_uncertain")
        self.assertEqual(self.request("POST", "/api/codex/threads", body)[0], 409)
        create.assert_called_once()

    def test_late_creation_validation_failure_does_not_enable_reallocation(self):
        create = Mock(side_effect=ManagedRequestError("settings failed after allocation"))
        self.app_server.create_thread = create
        body = {"requestId": RID, "projectId": "project-id", "message": "hello"}
        status, result = self.request("POST", "/api/codex/threads", body)
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "delivery_uncertain")
        self.assertFalse(result["retryAllowed"])
        self.request("POST", "/api/codex/threads", body)
        create.assert_called_once()

    def test_concurrent_duplicate_gets_pending_without_waiting_for_sender(self):
        entered, release = threading.Event(), threading.Event()
        send = self.controller.send_to_desktop
        def blocked(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return send(*args, **kwargs)
        self.controller.send_to_desktop = blocked
        results = []
        def first_request():
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
            try:
                connection.request("POST", "/api/codex/threads/thread-1/turn",
                                   json.dumps({"requestId": RID, "message": "hello"}),
                                   {"Authorization": f"Bearer {fixtures.TOKEN}", "Content-Type": "application/json"})
                response = connection.getresponse()
                results.append((response.status, json.loads(response.read())))
            finally:
                connection.close()
        worker = threading.Thread(target=first_request)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            status, result = self.send()
            self.assertEqual(status, 409)
            self.assertEqual(result["error"], "delivery_receipt_pending")
        finally:
            release.set()
            worker.join(4)
        self.assertEqual(results[0][0], 202)
        self.assertEqual(len(self.controller.desktop_sent), 1)

    def test_background_turn_is_also_claimed_once(self):
        self.server.screen_lock_probe = lambda: True
        self.assertEqual(self.send()[0], 202)
        status, result = self.send()
        self.assertEqual(status, 202)
        self.assertEqual(result["mode"], "background")
        self.assertEqual(len(self.app_server.started), 1)

    def test_legacy_client_without_id_retains_existing_behavior(self):
        body = {"message": "hello"}
        self.assertEqual(self.request("POST", "/api/codex/threads/thread-1/turn", body)[0], 202)
        self.assertEqual(self.request("POST", "/api/codex/threads/thread-1/turn", body)[0], 202)
        self.assertEqual(len(self.controller.desktop_sent), 2)
