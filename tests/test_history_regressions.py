import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_app_server as backend
from mac_bridge import incremental_thread_detail, thread_history_cursor


def event(kind, turn="turn"):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn}}


def command():
    return {"type": "response_item", "payload": {"type": "function_call",
            "name": "exec_command", "arguments": json.dumps({"cmd": "python build.py"})}}


def records(*entries):
    return "".join(json.dumps(entry) + "\n" for entry in entries)


class RolloutCacheRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with backend._ROLLOUT_ACTIVITY_LOCK:
            backend._ROLLOUT_ACTIVITY_CACHE.clear()

    def thread(self, identifier, content=None):
        path = self.root / f"rollout-{identifier}.jsonl"
        path.write_text(content if content is not None else records(event("task_started"), command()))
        return {"id": identifier, "path": str(path)}

    def test_unchanged_catalog_working_set_is_not_reread_after_warmup(self):
        original = Path.open
        for count in (9, 50):
            with self.subTest(count=count):
                threads = [self.thread(f"t{i}") for i in range(count)]
                backend._ROLLOUT_ACTIVITY_CACHE.clear()
                reads = []
                def track(path, *args, **kwargs):
                    if args and args[0] == "rb":
                        reads.append(path)
                    return original(path, *args, **kwargs)
                with patch.object(Path, "open", track):
                    for thread in threads:
                        backend.summarize_thread(thread)
                    self.assertEqual(len(reads), count)
                    for _ in range(2):
                        for thread in threads:
                            backend.summarize_thread(thread)
                    self.assertEqual(len(reads), count)

    def test_partial_record_is_committed_only_after_newline(self):
        thread = self.thread("partial", records(event("task_started")))
        path = Path(thread["path"])
        backend._rollout_activity_snapshot(thread)
        with path.open("a") as handle:
            handle.write(json.dumps(event("task_complete")))
        self.assertEqual(backend._rollout_activity_snapshot(thread)["status"], "inProgress")
        with patch.object(Path, "open", side_effect=AssertionError("unchanged partial line reread")):
            self.assertEqual(backend._rollout_activity_snapshot(thread)["status"], "inProgress")
        with path.open("a") as handle:
            handle.write("\n")
        self.assertEqual(backend._rollout_activity_snapshot(thread)["status"], "completed")

    def test_atomic_replacement_and_same_size_rewrite_reset_cached_state(self):
        thread = self.thread("replace", records(event("task_started", "aaaa")))
        path = Path(thread["path"])
        self.assertEqual(backend._rollout_activity_snapshot(thread)["activeTurnId"], "aaaa")
        replacement = self.root / "replacement"
        replacement.write_text(records(event("task_started", "bbbb")))
        os.replace(replacement, path)
        self.assertEqual(backend._rollout_activity_snapshot(thread)["activeTurnId"], "bbbb")
        previous_mtime = path.stat().st_mtime_ns
        path.write_text(records(event("task_started", "cccc")))
        os.utime(path, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
        self.assertEqual(backend._rollout_activity_snapshot(thread)["activeTurnId"], "cccc")

    def test_truncation_resets_old_counts(self):
        thread = self.thread("truncate")
        self.assertEqual(backend._rollout_activity_snapshot(thread)["turns"]["turn"]["ranCommands"], 1)
        Path(thread["path"]).write_text(records(event("task_started", "new")))
        result = backend._rollout_activity_snapshot(thread)
        self.assertEqual(result["activeTurnId"], "new")
        self.assertEqual(result["turns"], {})

    def test_truncation_above_committed_offset_still_resets_state(self):
        thread = self.thread("partial-truncate", records(event("task_started", "old")) + "x" * 500)
        self.assertEqual(backend._rollout_activity_snapshot(thread)["activeTurnId"], "old")
        Path(thread["path"]).write_text(records(event("task_started", "new")) + "x" * 20)
        # The new size is still greater than the last complete-line offset.
        self.assertEqual(backend._rollout_activity_snapshot(thread)["activeTurnId"], "new")

    def test_cache_entries_and_per_file_turn_counts_are_bounded(self):
        with patch.object(backend, "ROLLOUT_ACTIVITY_CACHE_ENTRIES", 3):
            for index in range(10):
                backend._rollout_activity_snapshot(self.thread(str(index)))
            self.assertEqual(len(backend._ROLLOUT_ACTIVITY_CACHE), 3)
        content = []
        for index in range(10):
            content += [event("task_started", str(index)), command(), event("task_complete", str(index))]
        with patch.object(backend, "ROLLOUT_ACTIVITY_MAX_TURNS", 3):
            snapshot = backend._rollout_activity_snapshot(self.thread("counts", records(*content)))
        self.assertEqual(list(snapshot["turns"]), ["7", "8", "9"])

    def test_large_record_does_not_hide_following_status_event(self):
        content = records(event("task_started"), {"payload": "x" * 1000}, [], event("task_complete"))
        with patch.object(backend, "ROLLOUT_ACTIVITY_MAX_RECORD_BYTES", 128):
            self.assertEqual(backend._rollout_activity_snapshot(self.thread("large", content))["status"], "completed")

    def test_slow_file_read_does_not_hold_global_cache_lock(self):
        fast = self.thread("fast")
        slow = self.thread("slow")
        backend._rollout_activity_snapshot(fast)
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        original = Path.open
        failures = []
        def slow_open(path, *args, **kwargs):
            if str(path) == slow["path"] and args and args[0] == "rb":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release slow file")
            return original(path, *args, **kwargs)
        def read(thread, completion=None):
            try:
                backend._rollout_activity_snapshot(thread)
            except Exception as error:
                failures.append(error)
            finally:
                if completion:
                    completion.set()
        with patch.object(Path, "open", slow_open):
            worker = threading.Thread(target=read, args=(slow,))
            worker.start()
            fast_worker = None
            try:
                self.assertTrue(entered.wait(2))
                fast_worker = threading.Thread(target=read, args=(fast, done))
                fast_worker.start()
                self.assertTrue(done.wait(2), "cached task waited for unrelated file I/O")
            finally:
                release.set()
                worker.join(3)
                if fast_worker:
                    fast_worker.join(3)
        self.assertEqual(failures, [])


class LongTurnRegressionTest(unittest.TestCase):
    def base_items(self, tool_count=198):
        return [
            {"id": "user", "type": "userMessage", "content": [{"type": "text", "text": "request"}]},
            {"id": "commentary", "type": "agentMessage", "text": "working", "phase": "commentary"},
        ] + [{"id": f"tool-{index}", "type": "commandExecution", "command": "true",
              "status": "completed", "aggregatedOutput": "ok"} for index in range(tool_count)]

    def detail(self, items, status="inProgress"):
        return backend.summarize_thread_detail({
            "id": "thread", "turns": [{"id": "turn", "status": status, "items": items}],
            "_historyPage": {"nextCursor": None, "older": False},
        })

    def test_final_after_200_items_is_preserved_without_promoting_commentary(self):
        items = self.base_items(250) + [{"id": "final", "type": "agentMessage", "text": "ACTUAL FINAL", "phase": "final_answer"}]
        turn = self.detail(items, "completed")["turns"][0]
        self.assertEqual(len(turn["items"]), backend.HISTORY_MAX_ITEMS)
        by_id = {item["id"]: item for item in turn["items"]}
        self.assertEqual(by_id["final"]["text"], "ACTUAL FINAL")
        self.assertEqual(by_id["commentary"]["phase"], "commentary")
        self.assertIn("user", by_id)
        self.assertTrue(turn["itemsTruncated"])
        self.assertEqual(turn["omittedItemCount"], len(items) - backend.HISTORY_MAX_ITEMS)
        self.assertEqual(turn["sourceItemCount"], len(items))

    def test_new_message_after_200_items_changes_incremental_tail(self):
        before = self.detail(self.base_items())
        after = self.detail(self.base_items() + [{"id": "late", "type": "agentMessage", "text": "new", "phase": "commentary"}])
        cursor = thread_history_cursor(before)
        delta = incremental_thread_detail(after, cursor["turnId"], cursor["revision"])
        self.assertEqual(delta["historyDelta"]["mode"], "replace")
        self.assertEqual(delta["turns"][0]["items"][-1]["id"], "late")

    def test_edit_of_omitted_item_changes_source_revision(self):
        items = self.base_items(250)
        before = self.detail(items)
        changed = copy.deepcopy(items)
        changed[2]["aggregatedOutput"] = "changed hidden tool output"
        after = self.detail(changed)
        self.assertEqual(before["turns"][0]["items"], after["turns"][0]["items"])
        self.assertNotEqual(thread_history_cursor(before), thread_history_cursor(after))

    def test_explicit_commentary_is_not_final_but_legacy_unphased_message_is(self):
        explicit = self.detail(self.base_items(0), "completed")["turns"][0]
        self.assertEqual(explicit["items"][-1]["phase"], "commentary")
        legacy = self.detail([{"id": "old", "type": "agentMessage", "text": "answer"}], "completed")
        self.assertEqual(legacy["turns"][0]["items"][0]["phase"], "final_answer")
        self.assertNotIn("itemsTruncated", legacy["turns"][0])

    def test_message_only_turn_is_bounded_and_retains_user_and_final(self):
        items = self.base_items(0) + [{"id": f"message-{index}", "type": "agentMessage",
                "phase": "commentary", "text": "working"} for index in range(250)]
        items.append({"id": "final", "type": "agentMessage", "phase": "final_answer", "text": "done"})
        turn = self.detail(items, "completed")["turns"][0]
        self.assertEqual(len(turn["items"]), backend.HISTORY_MAX_ITEMS)
        self.assertEqual(turn["items"][0]["id"], "user")
        self.assertEqual(turn["items"][-1]["id"], "final")
