import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import ccs


def write_jsonl(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def user(text, ts, **extra):
    rec = {
        "type": "user",
        "cwd": "/home/x/proj",
        "entrypoint": "cli",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }
    rec.update(extra)
    return rec


class ParseSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "abc-123.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_extracts_basic_fields(self):
        write_jsonl(self.path, [
            user("primera pregunta", "2026-10-01T10:00:00.000Z"),
            {"type": "assistant", "timestamp": "2026-10-01T10:00:05.000Z", "cwd": "/home/x/proj",
             "message": {"content": [{"type": "text", "text": "ok"}]}},
            user("última pregunta", "2026-10-02T12:00:00.000Z"),
            {"type": "ai-title", "aiTitle": "Título IA", "sessionId": "abc-123"},
        ])
        s = ccs.parse_session(self.path)
        self.assertEqual(s["id"], "abc-123")
        self.assertEqual(s["title"], "Título IA")
        self.assertEqual(s["first"], "primera pregunta")
        self.assertEqual(s["last"], "última pregunta")
        self.assertEqual(s["cwd"], "/home/x/proj")
        self.assertEqual(s["created"], "2026-10-01T10:00:00.000Z")
        self.assertEqual(s["updated"], "2026-10-02T12:00:00.000Z")
        self.assertEqual(s["entrypoint"], "cli")
        self.assertEqual(s["prompts"], 2)

    def test_custom_title_wins_over_ai_title_and_last_one_wins(self):
        write_jsonl(self.path, [
            user("hola", "2026-10-01T10:00:00Z"),
            {"type": "ai-title", "aiTitle": "IA"},
            {"type": "custom-title", "customTitle": "viejo"},
            {"type": "custom-title", "customTitle": "nuevo"},
            {"type": "ai-title", "aiTitle": "IA2"},
        ])
        self.assertEqual(ccs.parse_session(self.path)["title"], "nuevo")

    def test_skips_meta_sidechain_tool_results_and_reminders(self):
        write_jsonl(self.path, [
            user("<system-reminder>ruido</system-reminder>", "2026-10-01T09:00:00Z"),
            user("meta", "2026-10-01T09:00:01Z", isMeta=True),
            user("side", "2026-10-01T09:00:02Z", isSidechain=True),
            user([{"type": "tool_result", "content": "x"}], "2026-10-01T09:00:03Z"),
            user([{"type": "text", "text": "pregunta real\ncon salto"}], "2026-10-01T09:00:04Z"),
        ])
        s = ccs.parse_session(self.path)
        self.assertEqual(s["first"], "pregunta real con salto")
        self.assertEqual(s["prompts"], 1)

    def test_slash_command_shown_as_command(self):
        write_jsonl(self.path, [
            user("<command-message>plan</command-message>\n<command-name>/plan</command-name>\n"
                 "<command-args>VPO-1</command-args>", "2026-10-01T09:00:00Z"),
        ])
        self.assertEqual(ccs.parse_session(self.path)["first"], "/plan VPO-1")

    def test_interactive_if_ever_resumed_from_cli(self):
        write_jsonl(self.path, [
            user("tarea", "2026-10-18T13:00:00Z", entrypoint="sdk-cli"),
            user("sigo yo", "2026-10-19T05:00:00Z", entrypoint="cli"),
        ])
        self.assertEqual(ccs.parse_session(self.path)["entrypoint"], "cli")

    def test_clear_is_not_a_description(self):
        write_jsonl(self.path, [
            user("<command-name>/clear</command-name>", "2026-10-18T13:00:00Z"),
            user("de verdad", "2026-10-18T13:01:00Z"),
        ])
        s = ccs.parse_session(self.path)
        self.assertEqual(s["first"], "de verdad")
        self.assertEqual(s["prompts"], 1)

    def test_returns_none_without_real_prompts(self):
        write_jsonl(self.path, [{"type": "ai-title", "aiTitle": "x"}])
        self.assertIsNone(ccs.parse_session(self.path))

    def test_ignores_broken_lines(self):
        with open(self.path, "w") as f:
            f.write("{roto\n")
            f.write(json.dumps(user("vale", "2026-10-01T09:00:00Z")) + "\n")
        self.assertEqual(ccs.parse_session(self.path)["first"], "vale")


class IndexCacheTest(unittest.TestCase):
    def test_reparses_only_changed_files(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "projects", "-home-x")
            os.makedirs(proj)
            p = os.path.join(proj, "s1.jsonl")
            write_jsonl(p, [user("uno", "2026-10-01T09:00:00Z")])
            cache = os.path.join(root, "cache.json")

            first = ccs.load_sessions(os.path.join(root, "projects"), cache)
            self.assertEqual([s["first"] for s in first], ["uno"])

            calls = []
            orig = ccs.parse_session
            ccs.parse_session = lambda path: calls.append(path) or orig(path)
            try:
                ccs.load_sessions(os.path.join(root, "projects"), cache)
                self.assertEqual(calls, [])
                write_jsonl(p, [user("uno", "2026-10-01T09:00:00Z"), user("dos", "2026-10-01T10:00:00Z")])
                again = ccs.load_sessions(os.path.join(root, "projects"), cache)
                self.assertEqual(calls, [p])
                self.assertEqual(again[0]["last"], "dos")
            finally:
                ccs.parse_session = orig


class FilterTest(unittest.TestCase):
    def test_all_terms_must_match_any_field(self):
        rows = [
            {"title": "Pagos wallet", "first": "", "last": "", "cwd": "/p/shop", "id": "1"},
            {"title": "Otra", "first": "wallet", "last": "", "cwd": "/p/cash", "id": "2"},
        ]
        self.assertEqual([r["id"] for r in ccs.filter_rows(rows, "wallet shop")], ["1"])
        self.assertEqual([r["id"] for r in ccs.filter_rows(rows, "WALLET")], ["1", "2"])
        self.assertEqual(len(ccs.filter_rows(rows, "")), 2)


class RelTimeTest(unittest.TestCase):
    def test_relative(self):
        now = ccs.parse_ts("2026-10-20T12:00:00Z")
        self.assertEqual(ccs.rel_time("2026-10-20T11:59:30Z", now), "now")
        self.assertEqual(ccs.rel_time("2026-10-20T11:15:00Z", now), "45m")
        self.assertEqual(ccs.rel_time("2026-10-20T07:00:00Z", now), "5h")
        self.assertEqual(ccs.rel_time("2026-10-17T12:00:00Z", now), "3d")
        self.assertEqual(ccs.rel_time("2026-01-02T12:00:00Z", now), "2026-01-02")


class ProjectTest(unittest.TestCase):
    H = os.path.expanduser("~")

    def setUp(self):
        ccs.apply_config({})

    def tearDown(self):
        ccs.apply_config({})

    def test_default_key_is_basename(self):
        self.assertEqual(ccs.project_key("/srv/code/api"), "api")
        self.assertEqual(ccs.project_key(self.H), "~")
        self.assertEqual(ccs.project_key(""), "?")

    def test_claude_worktrees_group_with_their_repo(self):
        self.assertEqual(ccs.project_key("/srv/code/api/.claude/worktrees/feat-x"), "api")

    def test_workspaces_group_by_first_level(self):
        ccs.apply_config({"workspaces": ["~/work", "/srv/code/"]})
        self.assertEqual(ccs.project_key(self.H + "/work/shop/sub/dir"), "shop")
        self.assertEqual(ccs.project_key("/srv/code/api/x"), "api")
        self.assertEqual(ccs.project_key(self.H + "/work"), "work")
        self.assertEqual(ccs.project_key("/elsewhere/tool"), "tool")

    def test_default_inferred_emoji(self):
        self.assertEqual(ccs.project_emoji("voice_typing"), "🎤")
        self.assertEqual(ccs.project_emoji("weekly-tokens"), "📊")
        self.assertEqual(ccs.project_emoji("~"), "🏠")

    def test_config_emojis_and_rules_take_precedence(self):
        ccs.apply_config({"emojis": {"shop": "🛒", "voice_typing": "🦜"},
                          "rules": [["cust", "🛃"]]})
        self.assertEqual(ccs.project_emoji("shop"), "🛒")
        self.assertEqual(ccs.project_emoji("voice_typing"), "🦜")
        self.assertEqual(ccs.project_emoji("customs-qa"), "🛃")

    def test_unknown_emoji_is_stable_and_from_pool(self):
        e = ccs.project_emoji("zzz-proyecto-nuevo")
        self.assertIn(e, ccs.EMOJI_POOL)
        self.assertEqual(e, ccs.project_emoji("zzz-proyecto-nuevo"))

    def test_builtin_emojis_are_double_width(self):
        emojis = set(ccs.EMOJI_POOL) | set(ccs.DEFAULT_EMOJIS.values()) | {e for _, e in ccs.DEFAULT_RULES}
        for e in emojis:
            self.assertEqual(ccs.disp_width(e), 2, e)


class ArgsTest(unittest.TestCase):
    def setUp(self):
        ccs.apply_config({})

    def test_dot_filters_by_current_project(self):
        self.assertEqual(ccs.initial_filters(["."], "/srv/code/api"), ("", "api"))

    def test_path_args_filter_by_project_and_rest_is_text(self):
        self.assertEqual(ccs.initial_filters(["/srv/code/api/sub", "pago", "wallet"], "/x"),
                         ("pago wallet", "sub"))
        self.assertEqual(ccs.initial_filters(["pago"], "/x"), ("pago", None))
        self.assertEqual(ccs.initial_filters([], "/x"), ("", None))

    def test_dot_inside_worktree_uses_repo(self):
        self.assertEqual(ccs.initial_filters(["."], "/srv/code/api/.claude/worktrees/w1"), ("", "api"))


class ConfigFileTest(unittest.TestCase):
    def test_missing_file_gives_empty_config(self):
        self.assertEqual(ccs.load_config("/no/existe/config.json"), {})

    def test_reads_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w") as f:
                json.dump({"workspaces": ["~/w"]}, f)
            self.assertEqual(ccs.load_config(p), {"workspaces": ["~/w"]})

    def test_broken_json_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w") as f:
                f.write("{roto")
            with self.assertRaises(SystemExit) as cm:
                ccs.load_config(p)
            self.assertIn(p, str(cm.exception))


class FitTest(unittest.TestCase):
    def test_fit_counts_emoji_as_two_columns(self):
        self.assertEqual(ccs.disp_width(ccs.fit("🟢 shop", 10)), 10)
        self.assertEqual(ccs.disp_width(ccs.fit("🟢 shop larguísimo", 6)), 6)
        self.assertEqual(ccs.fit("abcdef", 4), "abc…")


class ProjectFilterTest(unittest.TestCase):
    def test_project_filter_combines_with_text(self):
        rows = [
            {"title": "a", "first": "x", "last": "", "cwd": "", "id": "1", "project": "shop"},
            {"title": "b", "first": "x", "last": "", "cwd": "", "id": "2", "project": "cash"},
            {"title": "c", "first": "y", "last": "", "cwd": "", "id": "3", "project": "shop"},
        ]
        self.assertEqual([r["id"] for r in ccs.filter_rows(rows, "x", "shop")], ["1"])
        self.assertEqual([r["id"] for r in ccs.filter_rows(rows, "", "shop")], ["1", "3"])

    def test_project_list_sorted_by_recency_with_counts(self):
        rows = [
            {"project": "shop", "updated": "2026-10-02"},
            {"project": "cash", "updated": "2026-10-03"},
            {"project": "shop", "updated": "2026-10-01"},
        ]
        self.assertEqual(ccs.project_list(rows), [("cash", 1), ("shop", 2)])


class CliTest(unittest.TestCase):
    def test_version_flag_prints_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ccs.main(["--version"]), 0)
        self.assertEqual(out.getvalue().strip(), f"ccs {ccs.__version__}")

    def test_version_is_semver(self):
        self.assertRegex(ccs.__version__, r"^\d+\.\d+\.\d+$")

    def test_cli_reads_sys_argv(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["ccs", "--version"]), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                ccs.cli()
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(ccs.__version__, out.getvalue())


if __name__ == "__main__":
    unittest.main()
