"""Phase 3D task API tests — endpoints, validation, security, durability."""

import json
import unittest
import urllib.error
import urllib.request

import db
from test_chat_api import ChatAPITestCase


def request(url, payload=None, content_type="application/json", raw=None,
            method="POST"):
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if content_type is not None:
        req.add_header("Content-Type", content_type)
    return do_task_request(req)


def do_task_request(req):
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


class TaskAPITestCase(ChatAPITestCase):
    def create_task(self, payload=None, **kwargs):
        payload = payload if payload is not None else {"title": "Ship it"}
        code, body = request(self.base + "/api/tasks", payload, **kwargs)
        try:
            return code, json.loads(body)
        except ValueError:
            return code, None

    def patch_task(self, task_id, payload, method="PATCH", **kwargs):
        code, body = request(
            f"{self.base}/api/tasks/{task_id}", payload, method=method,
            **kwargs
        )
        try:
            return code, json.loads(body)
        except ValueError:
            return code, None

    def delete_task(self, task_id, method="DELETE"):
        req = urllib.request.Request(
            f"{self.base}/api/tasks/{task_id}", method=method
        )
        code, body = do_task_request(req)
        try:
            return code, json.loads(body)
        except ValueError:
            return code, None

    def list_tasks(self, query="", method="GET"):
        req = urllib.request.Request(self.base + f"/api/tasks{query}",
                                     method=method)
        code, body = do_task_request(req)
        try:
            return code, json.loads(body)
        except ValueError:
            return code, None

    def raw_list_tasks(self, query=""):
        req = urllib.request.Request(self.base + f"/api/tasks{query}",
                                     method="GET")
        return do_task_request(req)


class TestTaskCreate(TaskAPITestCase):
    def test_create_defaults(self):
        code, task = self.create_task({"title": "Draft checklist"})
        self.assertEqual(code, 201)
        self.assertEqual(len(task["id"]), 32)
        self.assertTrue(all(ch in "0123456789abcdef" for ch in task["id"]))
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["priority"], "std")
        self.assertEqual(task["day"], "today")
        self.assertEqual(task["description"], "")
        self.assertEqual(task["time"], "")
        self.assertEqual(task["title"], "Draft checklist")
        self.assertTrue(task["created_at"])
        self.assertTrue(task["updated_at"])

    def test_create_full_input(self):
        code, task = self.create_task({
            "title": "Prep briefing",
            "description": "collect numbers",
            "status": "planned",
            "priority": "high",
            "time": "09:30",
            "day": "tomorrow",
        })
        self.assertEqual(code, 201)
        self.assertEqual(task["status"], "planned")
        self.assertEqual(task["priority"], "high")
        self.assertEqual(task["time"], "09:30")
        self.assertEqual(task["day"], "tomorrow")

    def test_missing_title(self):
        code, payload = self.create_task({"description": "no title"})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_task")

    def test_blank_title(self):
        code, payload = self.create_task({"title": "   "})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_task")

    def test_overlong_title(self):
        code, payload = self.create_task({"title": "x" * 201})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_task")

    def test_unsupported_field(self):
        code, payload = self.create_task({"title": "T", "color": "red"})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_invalid_status(self):
        code, payload = self.create_task({"title": "T", "status": "zapped"})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_task")
        self.assertIn("status", payload["error"]["message"])

    def test_invalid_priority(self):
        code, payload = self.create_task({"title": "T", "priority": "urgent"})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_task")

    def test_invalid_day(self):
        code, payload = self.create_task({"title": "T", "day": "yesterday"})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_task")

    def test_overlong_time(self):
        code, payload = self.create_task({"title": "T", "time": "x" * 21})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_task")

    def test_malformed_json(self):
        code, payload = self.create_task(raw=b"{nope")
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

    def test_non_object_json(self):
        code, payload = self.create_task(raw=b"[1,2,3]")
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_wrong_content_type(self):
        code, payload = self.create_task(
            raw=b'{"title":"T"}', content_type="text/plain"
        )
        self.assertEqual(code, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

    def test_body_size_limit(self):
        code, payload = self.create_task(
            raw=b'{"title":"' + b"x" * (17 * 1024) + b'"}'
        )
        self.assertEqual(code, 413)
        self.assertEqual(payload["error"]["code"], "payload_too_large")

    def test_create_persists_before_response(self):
        code, task = self.create_task({"title": "Durable"})
        conn = db.open_conn(self.db_path, readonly=True)
        row = db.get_task(conn, task["id"])
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Durable")


class TestTaskList(TaskAPITestCase):
    def test_empty_list(self):
        code, payload = self.list_tasks()
        self.assertEqual(code, 200)
        self.assertEqual(payload, {
            "tasks": [], "count": 0, "truncated": False,
            "status_counts": {"queued": 0, "warn": 0, "done": 0,
                              "planned": 0},
            "day_counts": {"today": 0, "tomorrow": 0},
        })

    def test_ordering_newest_updated_first(self):
        import time
        _, t1 = self.create_task({"title": "T1"})
        _, t2 = self.create_task({"title": "T2"})
        time.sleep(0.02)  # timestamps are ms-granular: pin the patch later
        self.patch_task(t1["id"], {"status": "done"})  # moves t1 to top
        code, payload = self.list_tasks()
        self.assertEqual(code, 200)
        self.assertEqual([t["id"] for t in payload["tasks"]],
                         [t1["id"], t2["id"]])

    def test_limit_bounds_and_truncation(self):
        for i in range(3):
            self.create_task({"title": f"T{i}"})
        code, payload = self.list_tasks("?limit=2")
        self.assertEqual(code, 200)
        self.assertEqual(len(payload["tasks"]), 2)
        self.assertTrue(payload["truncated"])
        for bad in ("0", "-1", "201", "abc", "1.5"):
            code, _ = self.list_tasks(f"?limit={bad}")
            self.assertEqual(code, 400)

    def test_singleton_tie_break_deterministic(self):
        import time
        ids = []
        for i in range(2):
            _, t = self.create_task({"title": "Same title"})
            ids.append(t["id"])
            time.sleep(0.02)  # distinct timestamp: creation order wins
        # force a genuine tie: both rows share one updated_at value
        conn = db.open_conn(self.db_path)
        try:
            conn.execute(
                "UPDATE tasks SET updated_at='2026-10-19T20:00:00.000Z'")
            conn.commit()
        finally:
            conn.close()
        _, payload = self.list_tasks()
        listed = [t["id"] for t in payload["tasks"]]
        self.assertEqual(listed, sorted(ids, reverse=True))  # id DESC tie
        again = self.list_tasks()[1]["tasks"]
        self.assertEqual([t["id"] for t in again], listed)  # deterministic


class TestTaskCounts(TaskAPITestCase):
    def test_counts_are_global_not_page_counts(self):
        # more tasks than the page can return; counts must cover all
        for i in range(3):
            self.create_task({"title": f"W{i}"})
        self.create_task({"title": "D1", "status": "done"})
        self.create_task({"title": "P1", "status": "planned",
                          "day": "tomorrow"})
        code, payload = self.list_tasks("?limit=2")
        self.assertEqual(code, 200)
        self.assertEqual(len(payload["tasks"]), 2)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["status_counts"], {
            "queued": 3, "warn": 0, "done": 1, "planned": 1,
        })
        self.assertEqual(sum(payload["status_counts"].values()), 5)

    def test_counts_update_after_patch_and_delete(self):
        _, task = self.create_task({"title": "A"})
        _, payload = self.list_tasks()
        self.assertEqual(payload["status_counts"]["queued"], 1)
        self.patch_task(task["id"], {"status": "warn"})
        code, payload = self.list_tasks()
        self.assertEqual(code, 200)
        self.assertEqual(payload["status_counts"], {
            "queued": 0, "warn": 1, "done": 0, "planned": 0,
        })
        self.delete_task(task["id"])
        _, payload = self.list_tasks()
        self.assertEqual(payload["status_counts"], {
            "queued": 0, "warn": 0, "done": 0, "planned": 0,
        })

    def test_counts_aggregate_matches_rows(self):
        self.create_task({"title": "A", "status": "queued"})
        self.create_task({"title": "B", "status": "queued"})
        conn = db.open_conn(self.db_path, readonly=True)
        try:
            self.assertEqual(db.task_status_counts(conn), {
                "queued": 2, "warn": 0, "done": 0, "planned": 0,
            })
        finally:
            conn.close()


class TestTaskPatch(TaskAPITestCase):
    def test_patch_status_and_timestamp(self):
        _, task = self.create_task({"title": "T"})
        code, updated = self.patch_task(task["id"], {"status": "done"})
        self.assertEqual(code, 200)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["id"], task["id"])
        self.assertGreaterEqual(updated["updated_at"], task["updated_at"])

    def test_patch_title_and_description(self):
        _, task = self.create_task({"title": "T"})
        code, updated = self.patch_task(
            task["id"], {"title": "New", "description": "d"}
        )
        self.assertEqual(code, 200)
        self.assertEqual(updated["title"], "New")
        self.assertEqual(updated["description"], "d")
        self.assertIn("created_at", updated)

    def test_empty_patch(self):
        _, task = self.create_task({"title": "T"})
        code, payload = self.patch_task(task["id"], {})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "empty_update")

    def test_unexpected_patch_field(self):
        _, task = self.create_task({"title": "T"})
        code, payload = self.patch_task(task["id"],
                                        {"description": "ok", "day": "today"})
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_patch_does_not_change_id(self):
        _, task = self.create_task({"title": "T"})
        old_created = task["created_at"]
        code, updated = self.patch_task(
            task["id"], {"title": "N", "description": "D", "status": "warn"}
        )
        self.assertEqual(updated["id"], task["id"])
        self.assertEqual(updated["created_at"], old_created)

    def test_unknown_id_404(self):
        code, payload = self.patch_task("0" * 32, {"status": "done"})
        self.assertEqual(code, 404)
        self.assertEqual(payload["error"]["code"], "task_not_found")

    def test_invalid_id_400(self):
        for bad in ("short", "XYZ0".ljust(32, "0"), "g" * 32, ""):
            code, payload = self.patch_task(bad, {"status": "done"})
            self.assertEqual(code, 400)
            self.assertEqual(payload["error"]["code"], "invalid_task")

    def test_repeated_updates_deterministic(self):
        _, task = self.create_task({"title": "T", "status": "queued"})
        previous = None
        for status in ("warn", "done", "planned", "queued"):
            code, updated = self.patch_task(task["id"], {"status": status})
            self.assertEqual(code, 200)
            self.assertEqual(updated["status"], status)
            if previous:
                self.assertGreaterEqual(updated["updated_at"], previous)
            previous = updated["updated_at"]


class TestTaskDelete(TaskAPITestCase):
    def test_delete_works_then_404(self):
        _, task = self.create_task({"title": "T"})
        code, payload = self.delete_task(task["id"])
        self.assertEqual(code, 200)
        self.assertTrue(payload["deleted"])
        self.assertEqual(payload["id"], task["id"])
        code, payload = self.delete_task(task["id"])
        self.assertEqual(code, 404)
        self.assertEqual(payload["error"]["code"], "task_not_found")

    def test_delete_unknown_404(self):
        code, payload = self.delete_task("a" * 32)
        self.assertEqual(code, 404)
        self.assertEqual(payload["error"]["code"], "task_not_found")

    def test_delete_invalid_id_400(self):
        code, payload = self.delete_task("nope")
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_task")


class TestTaskSecurityAndMethods(TaskAPITestCase):
    def test_cross_origin_rejected(self):
        req = urllib.request.Request(
            self.base + "/api/tasks", data=b'{"title":"T"}', method="POST"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", "http://evil.test")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("cross-origin request should be rejected")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 403)
            payload = json.loads(err.read())
            self.assertEqual(payload["error"]["code"],
                             "cross_origin_forbidden")

    def test_unsupported_methods_405(self):
        cases = [
            ("PATCH", "/api/tasks", b'{"title":"T"}'),
            ("DELETE", "/api/tasks", b""),
            ("PUT", "/api/tasks", b'{"title":"T"}'),
            ("GET", "/api/tasks/0" + "0" * 31, None),
            ("POST", "/api/tasks/" + "0" * 32, b'{"status":"done"}'),
            ("PUT", "/api/tasks/" + "0" * 32, b'{"status":"done"}'),
            ("OPTIONS", "/api/tasks/" + "0" * 32, None),
        ]
        for method, path, raw in cases:
            code, _ = request(self.base + path, raw=raw, method=method,
                              content_type="application/json")
            self.assertEqual(code, 405, f"{method} {path}")

    def test_no_secrets_in_responses(self):
        code, body = self.raw_list_tasks()
        self.assertEqual(code, 200)
        text = body.decode("utf-8")
        self.assertNotIn("test-key-123", text)
        _, task = self.create_task({"title": "Secret scan"})
        _, patched = self.patch_task(task["id"], {"status": "done"})
        self.assertNotIn("test-key-123", json.dumps(patched))

    def test_error_responses_leak_no_internals(self):
        req = urllib.request.Request(
            self.base + "/api/tasks",
            data=b"{nope", method="POST",
        )
        req.add_header("Content-Type", "application/json")
        code, body = do_task_request(req)
        self.assertEqual(code, 400)
        text = body.decode("utf-8")
        for leak in ("Traceback", "sqlite", "/home/", ".py", ".db"):
            self.assertNotIn(leak, text)


class TestTaskPersistenceWorkflow(TaskAPITestCase):
    """The mission's 8-step durable workflow with conversational isolation."""

    def test_full_workflow_survives_reopen(self):
        seed_code, seed = self.chat({"message": "Seed conversation"})  # noqa
        # 1. create tasks
        _, a = self.create_task({"title": "Alpha", "priority": "high",
                                 "time": "09:30"})
        _, b = self.create_task({"title": "Beta", "day": "tomorrow"})
        # 2. persisted in the database
        conn = db.open_conn(self.db_path)
        conversations_before = conn.execute(
            "SELECT id FROM conversations ORDER BY created_at"
        ).fetchall()
        self.assertTrue(db.get_task(conn, a["id"]))
        conn.close()
        # 3. update status
        code, updated = self.patch_task(a["id"], {"status": "done"})
        self.assertEqual(code, 200)
        self.assertEqual(updated["status"], "done")
        # 4. close and reinitialize the server/database
        self.stop_jarvis()
        db.init_db(self.db_path)
        self.start_jarvis(self.cfg)
        # 5. updated state remains
        _, listed = self.list_tasks()
        by_id = {t["id"]: t for t in listed["tasks"]}
        self.assertEqual(by_id[a["id"]]["status"], "done")
        self.assertEqual(by_id[a["id"]]["priority"], "high")
        self.assertEqual(by_id[b["id"]]["day"], "tomorrow")
        self.assertTrue(listed["truncated"] is False)
        # 6. delete one task
        code, _ = self.delete_task(b["id"])
        self.assertEqual(code, 200)
        # 7. absence is durable
        self.stop_jarvis()
        db.init_db(self.db_path)
        self.start_jarvis(self.cfg)
        code, listed = self.list_tasks()
        ids = [t["id"] for t in listed["tasks"]]
        self.assertNotIn(b["id"], ids)
        self.assertIn(a["id"], ids)
        # 8. conversations unchanged throughout
        conn = db.open_conn(self.db_path, readonly=True)
        conversations_after = conn.execute(
            "SELECT id FROM conversations ORDER BY created_at"
        ).fetchall()
        self.assertEqual(
            [r["id"] for r in conversations_after],
            [r["id"] for r in conversations_before],
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
