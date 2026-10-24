import json
import queue
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_app_server import (
    AppServerError, AppServerRejected, AppServerTransportError, CodexAppServerClient,
)


class FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        value = self.lines.get()
        if value is None:
            raise StopIteration
        return value

    def reply(self, value):
        self.lines.put(json.dumps(value) + "\n")

    def close(self):
        self.lines.put(None)


class FakeProcess:
    """JSONL stdio peer; exercises the actual reader, RPC and lifecycle code."""
    def __init__(self, *, die_on=None, ignore=None, reject=None, initialize_gate=None):
        self.stdout = FakeOutput()
        self.stdin = self
        self.returncode = None
        self.requests = []
        self.die_on, self.ignore, self.reject = die_on, ignore, reject
        self.initialize_gate = initialize_gate
        self.initializing = threading.Event()
        self.exited = threading.Event()

    def write(self, line):
        if self.returncode is not None:
            raise BrokenPipeError()
        message = json.loads(line)
        self.requests.append(message)
        method = message.get("method")
        if method == "initialize":
            self.initializing.set()
            if self.initialize_gate is not None and not self.initialize_gate.wait(3):
                raise AssertionError("initialization gate was not released")
        if method == self.die_on:
            self.finish(1)
        elif "id" in message and "method" in message and method != self.ignore:
            response = {"error": {"code": -32601, "message": "unsupported"}} if method == self.reject else {
                "result": {"data": [], "generationFixture": id(self)},
            }
            self.stdout.reply({"id": message["id"], **response})
        return len(line)

    def flush(self):
        pass

    def poll(self):
        return self.returncode

    def finish(self, returncode=0):
        if self.returncode is None:
            self.returncode = returncode
            self.exited.set()
            self.stdout.close()

    def close(self):
        self.finish()

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)

    def wait(self, timeout=None):
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired("fake-codex", timeout)
        return self.returncode


class CatalogRecoveryTest(unittest.TestCase):
    def client(self, *, dedicated=False):
        client = CodexAppServerClient(Path("fake-codex"), dedicated=dedicated)
        self.addCleanup(client.close)
        return client

    def test_dead_catalog_recovers_without_closing_live_background_writer(self):
        first, second = FakeProcess(), FakeProcess()
        client = self.client()
        writer = Mock()
        writer.managed_run.return_value = {"status": "inProgress"}
        client._background_clients["active"] = writer
        with patch("codex_app_server.subprocess.Popen", side_effect=[first, second]) as spawn:
            client.start()
            first.finish(1)
            result = client.list_threads()
            self.assertEqual(result["generationFixture"], id(second))
            self.assertEqual(spawn.call_count, 2)
            self.assertTrue(client.is_ready())
            self.assertEqual(client.managed_run("active")["status"], "inProgress")
            writer.close.assert_not_called()

    def test_initial_start_failure_remains_recoverable_after_backoff(self):
        client = self.client()
        with patch("codex_app_server.subprocess.Popen", side_effect=[OSError("unavailable"), FakeProcess()]) as spawn:
            with self.assertRaises(AppServerError):
                client.start()
            self.assertFalse(client._closed)
            self.assertEqual(client.transport_status()["state"], "unavailable")
            with self.assertRaises(AppServerError):
                client.list_threads()
            self.assertEqual(spawn.call_count, 1)
            with patch("codex_app_server.time.monotonic", return_value=client._catalog_retry_at + 1):
                self.assertEqual(client.list_threads()["data"], [])
            self.assertEqual(spawn.call_count, 2)

    def test_stdout_eof_is_recoverable_even_while_process_is_still_alive(self):
        first, second = FakeProcess(ignore="thread/list"), FakeProcess()
        original_write = first.write
        def close_stream(line):
            result = original_write(line)
            if json.loads(line).get("method") == "thread/list":
                first.stdout.close()
            return result
        first.write = close_stream
        client = self.client()
        with patch("codex_app_server.subprocess.Popen", side_effect=[first, second]) as spawn:
            client.start()
            self.assertEqual(client.list_threads()["generationFixture"], id(second))
            self.assertEqual(spawn.call_count, 2)
            self.assertTrue(client.is_ready())

    def test_broken_stdin_recovers_even_with_live_child_and_open_stdout(self):
        first, second = FakeProcess(), FakeProcess()
        original_write = first.write
        def broken_stdin(line):
            if json.loads(line).get("method") == "thread/list":
                raise BrokenPipeError("child closed stdin only")
            return original_write(line)
        first.write = broken_stdin
        client = self.client()
        with patch("codex_app_server.subprocess.Popen", side_effect=[first, second]) as spawn:
            client.start()
            self.assertIsNone(first.poll())
            self.assertEqual(client.list_threads()["generationFixture"], id(second))
            self.assertEqual(spawn.call_count, 2)
            self.assertEqual(first.returncode, -15)
            self.assertTrue(client.is_ready())

    def test_late_write_failure_does_not_mark_new_generation_failed(self):
        first, second = FakeProcess(), FakeProcess()
        client = self.client()
        with patch("codex_app_server.subprocess.Popen", return_value=first):
            client.start()
        previous_generation = client._generation
        def superseded_write(line):
            with client._transport_state_lock:
                client._process = second
                client._generation += 1
                client._ready.set()
                client._transport_failed.clear()
            first.finish(1)
            raise BrokenPipeError("old generation only")
        first.write = superseded_write
        with self.assertRaises(AppServerTransportError):
            client._write({"method": "initialized", "params": {}}, generation=previous_generation)
        self.assertTrue(client.is_ready())
        self.assertFalse(client._transport_failed.is_set())

    def test_concurrent_recovery_starts_one_process_and_status_never_waits_for_it(self):
        gate = threading.Event()
        first, replacement = FakeProcess(), FakeProcess(initialize_gate=gate)
        client = self.client()
        results, failures = [], []
        def read():
            try:
                results.append(client.list_threads())
            except Exception as error:
                failures.append(error)
        with patch("codex_app_server.subprocess.Popen", side_effect=[first, replacement]) as spawn:
            client.start()
            first.finish(1)
            workers = [threading.Thread(target=read) for _ in range(8)]
            for worker in workers:
                worker.start()
            try:
                self.assertTrue(replacement.initializing.wait(2))
                self.assertEqual(client.transport_status()["state"], "recovering")
                self.assertFalse(client.is_ready())
            finally:
                gate.set()
                for worker in workers:
                    worker.join(3)
            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(failures, [])
            self.assertEqual(len(results), 8)
            self.assertEqual(spawn.call_count, 2)

    def test_old_reader_eof_and_reply_cannot_fail_or_complete_new_generation(self):
        current = FakeProcess()
        client = self.client()
        with patch("codex_app_server.subprocess.Popen", return_value=current):
            client.start()
        old = FakeProcess()
        old_generation = client._generation - 1
        previous, new = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
        client._pending.update({100: previous, 101: new})
        client._pending_generations.update({100: old_generation, 101: client._generation})
        old.stdout.reply({"id": 101, "result": {"wrong": "old generation"}})
        old.finish(1)
        client._read_loop(old, old_generation)
        self.assertIn("transportError", previous.get_nowait())
        self.assertTrue(new.empty())
        self.assertTrue(client.is_ready())

    def test_stale_outgoing_request_is_not_written_to_new_process(self):
        process = FakeProcess()
        client = self.client()
        with patch("codex_app_server.subprocess.Popen", return_value=process):
            client.start()
        client._pending_generations[100] = client._generation - 1
        before = len(process.requests)
        with self.assertRaises(AppServerTransportError):
            client._write({"id": 100, "method": "thread/list", "params": {}})
        self.assertEqual(len(process.requests), before)

    def test_transport_failure_retries_only_once(self):
        processes = [FakeProcess(die_on="thread/list"), FakeProcess(die_on="thread/list")]
        client = self.client()
        with patch("codex_app_server.subprocess.Popen", side_effect=processes) as spawn:
            client.start()
            with self.assertRaises(AppServerTransportError):
                client.list_threads()
            self.assertEqual(spawn.call_count, 2)

    def test_write_with_lost_response_is_never_replayed(self):
        process = FakeProcess(die_on="turn/start")
        client = self.client()
        with patch("codex_app_server.subprocess.Popen", return_value=process) as spawn:
            client.start()
            with self.assertRaises(AppServerTransportError):
                client.request("turn/start", {"threadId": "thread", "input": []})
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(sum(message.get("method") == "turn/start" for message in process.requests), 1)

    def test_dedicated_writer_does_not_restart_even_for_read_method(self):
        process = FakeProcess()
        client = self.client(dedicated=True)
        with patch("codex_app_server.subprocess.Popen", return_value=process) as spawn:
            client.start()
            process.finish(1)
            with self.assertRaises(AppServerTransportError):
                client.list_threads()
            self.assertEqual(spawn.call_count, 1)

    def test_live_timeout_and_explicit_rejection_do_not_restart_or_replay(self):
        for process, error in [(FakeProcess(ignore="thread/list"), AppServerError),
                               (FakeProcess(reject="thread/list"), AppServerRejected)]:
            with self.subTest(error=error):
                client = self.client()
                with patch("codex_app_server.subprocess.Popen", return_value=process) as spawn:
                    client.start()
                    with self.assertRaises(error):
                        client.request("thread/list", {}, timeout=0.01)
                    self.assertTrue(client.is_ready())
                    self.assertEqual(spawn.call_count, 1)
                    self.assertEqual(sum(message.get("method") == "thread/list" for message in process.requests), 1)

    def test_failed_recovery_is_backed_off_and_never_closes_other_writers(self):
        process = FakeProcess()
        client = self.client()
        writer = Mock()
        client._background_clients["active"] = writer
        with patch("codex_app_server.subprocess.Popen", side_effect=[process, OSError("offline")]) as spawn:
            client.start()
            process.finish(1)
            for _ in range(3):
                with self.assertRaises(AppServerError):
                    client.list_threads()
            self.assertEqual(spawn.call_count, 2)
            self.assertGreater(client.transport_status()["retryAfterSeconds"], 0)
            writer.close.assert_not_called()

    def test_explicit_close_never_resurrects_client(self):
        client = self.client()
        client.close()
        with patch("codex_app_server.subprocess.Popen") as spawn:
            with self.assertRaises(AppServerError):
                client.start()
            with self.assertRaises(AppServerError):
                client.list_threads()
            spawn.assert_not_called()
        self.assertEqual(client.transport_status()["state"], "closed")
