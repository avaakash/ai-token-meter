"""Session naming. Run with python3 -B -m unittest test_titles."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


def user(content, **extra):
    return {"type": "user", "sessionId": "s1", "message": {"content": content}, **extra}


class FirstPromptTests(unittest.TestCase):
    def test_plain_and_text_block_prompts(self):
        self.assertEqual(server.claude.first_prompt_text(user("  fix the\n flaky test ")), "fix the flaky test")
        self.assertEqual(server.claude.first_prompt_text(user([{"type": "text", "text": "hello"}])), "hello")

    def test_injected_and_tool_records_are_skipped(self):
        for record in (
            user([{"type": "tool_result", "content": "ok"}]),
            user("Base directory for this skill: /x", isMeta=True),
            user("sub-agent task", isSidechain=True),
            user("<local-command-caveat>Caveat: ...</local-command-caveat>"),
            user("/code-review"),
            user("<command-message>clear</command-message>\n<command-name>/clear</command-name>"),
        ):
            with self.subTest(record=record):
                self.assertIsNone(server.claude.first_prompt_text(record))

    def test_slash_command_with_arguments_reads_as_typed(self):
        text = ("<command-message>work-issue</command-message>\n<command-name>/work-issue</command-name>\n"
                "<command-args>5364</command-args>")
        self.assertEqual(server.claude.first_prompt_text(user(text)), "/work-issue 5364")


class TitleSourceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        db_patch = patch.object(server, "DB_PATH", str(self.dir / "usage.sqlite3"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        server.init_db()

    def titles(self):
        with server.get_conn() as conn:
            for sid in ("a", "b", "c", "d"):
                conn.execute("INSERT INTO messages (message_id, source, session_id, timestamp, cost) VALUES (?, 'claude', ?, '2026-10-16T00:00:00.000Z', 1)", (sid, sid))
            conn.commit()
        with patch.object(server.claude, "LOG_GLOBS", []), patch.object(server.codex, "LOG_GLOBS", []):
            return {r["session_id"]: (r["title"], r["first_prompt"]) for r in server.list_all_sessions("all")}

    def test_precedence(self):
        with server.get_conn() as conn:
            conn.executemany(
                "INSERT INTO session_titles (session_id, ai_title, custom_title, app_title, app_title_source, first_prompt) VALUES (?, ?, ?, ?, ?, ?)",
                [("a", "ai", "custom", "app rename", "user", "p"),
                 ("b", "ai", "custom", "app auto", "auto", None),
                 ("c", "ai", None, "app auto", "auto", None),
                 ("d", None, None, None, None, "first prompt")],
            )
            conn.commit()
        with patch.object(server.claude, "APP_SESSIONS_GLOB", str(self.dir / "none" / "*.json")), \
             patch.object(server.codex, "SESSION_INDEX_PATH", str(self.dir / "none.jsonl")):
            titles = self.titles()
        self.assertEqual(titles["a"][0], "app rename")
        self.assertEqual(titles["b"][0], "custom")
        self.assertEqual(titles["c"][0], "app auto")
        self.assertEqual(titles["d"], (None, "first prompt"))

    def test_external_title_sources(self):
        app_dir = self.dir / "app" / "x"
        app_dir.mkdir(parents=True)
        (app_dir / "local_1.json").write_text(json.dumps({"cliSessionId": "a", "title": "Renamed in app", "titleSource": "user"}))
        (app_dir / "local_2.json").write_text("not json")
        index = self.dir / "session_index.jsonl"
        index.write_text(
            json.dumps({"id": "b", "thread_name": "Old name", "updated_at": "2026-10-01T00:00:00Z"}) + "\n"
            + json.dumps({"id": "b", "thread_name": "New name", "updated_at": "2026-10-02T00:00:00Z"}) + "\n"
        )
        with patch.object(server.claude, "APP_SESSIONS_GLOB", str(self.dir / "app" / "**" / "*.json")), \
             patch.object(server.codex, "SESSION_INDEX_PATH", str(index)):
            titles = self.titles()
        self.assertEqual(titles["a"][0], "Renamed in app")
        self.assertEqual(titles["b"][0], "New name")


if __name__ == "__main__":
    unittest.main()
