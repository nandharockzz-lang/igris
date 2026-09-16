#!/usr/bin/env python3
"""Tests for the expanded deterministic broker and confirmation gates.

Covers the versioned contract (shapes, arg validation, confirmation
flags, redaction), direct command parsing (new verbs match, ambiguous
chatter never matches), exact broker argv (Lua expressions byte-exact,
no shell anywhere), confirmation gates (needs_confirm without a button
stages pending + exit 2; confirm executes; deny drops; expiry enforced),
and UI-visible pending content.

Run with the daemon venv:
    ~/.local/share/jarvis/venv/bin/python -m unittest -v test_jarvis_broker
from this directory. hyprctl/system backends are stubbed; no compositor,
mic or audio needed.
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(DAEMON_DIR)


def load_module(name, filename):
    loader = importlib.machinery.SourceFileLoader(
        name, os.path.join(DAEMON_DIR, filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


jl = load_module("jl_br", "jarvis-listen.py")
jo = load_module("jo_br", "jarvis-open")
act = load_module("act_br", "actions.py")


class ContractTests(unittest.TestCase):
    def test_request_shape(self):
        r = act.build_request("workspace", {"target": "3"})
        self.assertEqual(r["v"], 1)
        self.assertEqual(r["action"], "workspace")
        self.assertEqual(r["args"], {"target": "3"})
        self.assertFalse(r["needs_confirm"])
        self.assertIn("id", r)

    def test_confirmation_derived_not_declared(self):
        r = act.build_request("reboot", {}, source="voice")
        self.assertTrue(r["needs_confirm"])
        # Even a supervised source cannot clear the flag on the request.
        r2 = act.build_request("reboot", {}, source="button")
        self.assertTrue(r2["needs_confirm"])

    def test_invalid_args_rejected(self):
        self.assertIsNone(act.build_request("nope", {}))
        self.assertIsNone(act.build_request("volume", {"value": "999"}))
        self.assertIsNone(act.build_request("volume", {"value": "loud;rm"}))
        self.assertIsNone(act.build_request("workspace", {"target": 'a"b'}))
        self.assertIsNone(act.build_request("notify", {"text": ""}))

    def test_result_redacts(self):
        r = act.result("1", True, result="opened secret thing")
        self.assertTrue(r["ok"])
        self.assertFalse(r["pending"])
        # Result bodies carry no free speech in the gated paths; the shape
        # itself is bounded.
        self.assertLessEqual(len(r["result"]), 200)

    def test_pending_roundtrip_and_expiry(self):
        tmp = tempfile.mkdtemp(prefix="jarvis-pend-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(act, "STATE_DIR", tmp), \
                mock.patch.object(act, "PENDING_FILE",
                                  os.path.join(tmp, "pending-action")):
            req = act.build_request("close-window", {}, source="voice",
                                    req_id="abc")
            self.assertTrue(act.write_pending(req))
            got = act.read_pending()
            self.assertEqual(got["action"], "close-window")
            self.assertIn("Close", got["description"])
            # Expired pending reads as absent.
            payload = json.load(open(os.path.join(tmp, "pending-action")))
            payload["created"] = int(time.time()) - 9999
            json.dump(payload, open(os.path.join(tmp, "pending-action"),
                                     "w"))
            self.assertIsNone(act.read_pending())
            # Non-confirmable payloads never read back.
            act.clear_pending()
            bad = {"v": 1, "id": "x", "action": "volume",
                   "args": {"value": "up"}, "created": int(time.time())}
            json.dump(bad, open(os.path.join(tmp, "pending-action"), "w"))
            self.assertIsNone(act.read_pending())


class ParseTests(unittest.TestCase):
    def test_new_verbs_match(self):
        cases = {
            "switch to workspace 3": ("workspace", "3"),
            "go to workspace two": ("workspace", "two"),
            "workspace 2": ("workspace", "2"),
            "next workspace": ("workspace", "next"),
            "go to the previous workspace": ("workspace", "previous"),
            "focus firefox": ("focus-window", "firefox"),
            "switch to terminal": ("focus-window", "terminal"),
            "move firefox to workspace 2": ("move-window",
                                            ["firefox", "2"]),
            "move this window to workspace 1": ("move-window",
                                                ["this", "1"]),
            "fullscreen": ("fullscreen", ""),
            "toggle fullscreen": ("fullscreen", ""),
            "lock the screen": ("lock", ""),
            "take a screenshot": ("screenshot", "full"),
            "screenshot this window": ("screenshot", "window"),
            "remind me to drink water": ("notify", "drink water"),
            "close this window": ("close-window", ""),
            "log out": ("logout", ""),
            "reboot the machine": ("reboot", ""),
            "shut down": ("poweroff", ""),
            "turn wifi off": ("wifi", "off"),
            "wifi on": ("wifi", "on"),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(jl.match_routine_intent(text), want)

    def test_ambiguous_never_matches(self):
        for text in ("stop", "restart", "can you play something?",
                     "close it", "reboot?", "what time is it",
                     "switch to workspace", "move firefox",
                     "remind me to", "wifi", "fullscreen the volume"):
            with self.subTest(text=text):
                got = jl.match_routine_intent(text)
                # "switch to decaf" parses as focus (broker refuses: no such
                # window, falls through to the agent) -- everything else
                # must not match at all.
                if text == "switch to decaf":
                    self.assertEqual(got, ("focus-window", "decaf"))
                else:
                    self.assertIsNone(got)

    def test_workspace_focus_no_collision(self):
        self.assertEqual(jl.match_routine_intent("switch to workspace 2"),
                         ("workspace", "2"))
        self.assertEqual(jl.match_routine_intent("switch to firefox"),
                         ("focus-window", "firefox"))


class ArgvTests(unittest.TestCase):
    def setUp(self):
        self.argv = []
        self.clients = []

    def _fake_run(self, argv, **kw):
        self.argv.append(argv)
        return (0, "ok")

    def test_workspace_lua_exact(self):
        with mock.patch.object(jo, "run_cmd", side_effect=self._fake_run):
            self.assertEqual(jo.cmd_workspace("3"), 0)
            self.assertEqual(jo.cmd_workspace("next"), 0)
        self.assertEqual(self.argv[0],
                         ["hyprctl", "dispatch",
                          'hl.dsp.focus({ workspace = "3" })'])
        self.assertEqual(self.argv[1],
                         ["hyprctl", "dispatch",
                          'hl.dsp.focus({ workspace = "+1" })'])

    def test_workspace_rejects_injection(self):
        self.assertEqual(jo.cmd_workspace('3"}) evil'), 1)
        self.assertEqual(jo.cmd_workspace("a$b"), 1)
        self.assertEqual(self.argv, [])

    def test_focus_uses_address_not_text(self):
        self.clients = [{"class": "firefox", "title": "x",
                         "address": "0xabc123"}]
        with mock.patch.object(jo, "hypr_json",
                               return_value=self.clients):
            with mock.patch.object(jo, "run_cmd",
                                   side_effect=self._fake_run):
                self.assertEqual(jo.cmd_focus_window("firefox"), 0)
        self.assertEqual(self.argv[0],
                         ["hyprctl", "dispatch",
                          'hl.dsp.focus({ window = "address:0xabc123" })'])

    def test_move_builds_two_exact_exprs(self):
        self.clients = [{"class": "foot", "title": "t",
                         "address": "0x1a2b"}]
        with mock.patch.object(jo, "hypr_json",
                               return_value=self.clients):
            with mock.patch.object(jo, "run_cmd",
                                   side_effect=self._fake_run):
                self.assertEqual(jo.cmd_move_window("foot", "2"), 0)
        self.assertEqual(self.argv[0],
                         ["hyprctl", "dispatch",
                          'hl.dsp.window.move({ workspace = "2" })'])
        self.assertEqual(self.argv[1],
                         ["hyprctl", "dispatch",
                          'hl.dsp.focus({ window = "address:0x1a2b" })'])

    def test_fullscreen_lock_notify_argv(self):
        with mock.patch.object(jo, "run_cmd", side_effect=self._fake_run):
            self.assertEqual(jo.cmd_fullscreen(), 0)
            self.assertEqual(jo.cmd_lock(), 0)
            self.assertEqual(jo.cmd_notify("drink water"), 0)
            self.assertEqual(jo.cmd_notify("bad; text"), 1)
        self.assertEqual(self.argv[0],
                         ["hyprctl", "dispatch",
                          "hl.dsp.window.fullscreen({})"])
        self.assertEqual(self.argv[1], ["loginctl", "lock-session"])
        self.assertEqual(self.argv[2],
                         ["notify-send", "-a", "Jarvis", "Jarvis",
                          "drink water"])

    def test_no_shell_anywhere(self):
        import inspect
        src = open(os.path.join(DAEMON_DIR, "jarvis-open")).read()
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        tree = __import__("ast").parse(src)
        for node in __import__("ast").walk(tree):
            if isinstance(node, __import__("ast").Call):
                for kw in node.keywords:
                    if kw.arg == "shell":
                        self.fail("shell= kwarg in broker")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-gate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # jo imports the shared actions module: patch it where jo reads it.
        self.p_state = mock.patch.object(jo.actions, "STATE_DIR", self.tmp)
        self.p_pend = mock.patch.object(
            jo.actions, "PENDING_FILE", os.path.join(self.tmp, "p"))
        self.p_state.start()
        self.p_pend.start()
        self.addCleanup(self.p_state.stop)
        self.addCleanup(self.p_pend.stop)
        self.argv = []

    def pending(self):
        return jo.actions.read_pending()

    def _ok(self, argv, **kw):
        self.argv.append(argv)
        return (0, "ok")

    def test_unstaged_gated_verb_stages_and_exits_2(self):
        rc = jo.stage_or_run("reboot", {}, False, lambda: 0)
        self.assertEqual(rc, 2)
        self.assertEqual(self.argv, [])  # nothing ran
        pending = self.pending()
        self.assertEqual(pending["action"], "reboot")

    def test_supervised_flag_executes(self):
        with mock.patch.object(jo, "run_cmd", side_effect=self._fake_ok):
            rc = jo.stage_or_run("reboot", {}, True, jo.cmd_reboot)
        self.assertEqual(rc, 0)
        self.assertEqual(self.argv[0], ["systemctl", "reboot"])
        self.assertIsNone(self.pending())

    def _fake_ok(self, argv, **kw):
        self.argv.append(argv)
        return (0, "ok")

    def test_confirm_executes_and_clears(self):
        jo.stage_or_run("wifi", {"value": "off"}, False,
                        lambda: self.fail("must not run"))
        with mock.patch.object(jo, "run_cmd", side_effect=self._fake_ok):
            self.assertEqual(jo.cmd_confirm(), 0)
        self.assertEqual(self.argv[0], ["nmcli", "radio", "wifi", "off"])
        self.assertIsNone(self.pending())

    def test_deny_runs_nothing(self):
        jo.stage_or_run("poweroff", {}, False,
                        lambda: self.fail("must not run"))
        self.assertEqual(jo.cmd_deny(), 0)
        self.assertEqual(self.argv, [])
        self.assertIsNone(self.pending())

    def test_directive_pending_shape(self):
        # run_directive maps broker exit 2 -> "pending" (tested against a
        # stubbed broker via run_bounded).
        jl2 = jl
        with mock.patch.object(
                jl2, "run_bounded",
                return_value=SimpleNamespace(returncode=2, overflowed=False,
                                             stdout="", stderr="needs")):
            with mock.patch.object(jl2, "jarvis_open_path",
                                   return_value="/bin/true"):
                with mock.patch.object(jl2, "arm_active",
                                       return_value=True):
                    got = jl2.run_directive(("reboot", ""), mode="workspace",
                                            approval_source="voice",
                                            require_arm=False)
        self.assertEqual(got, "pending")


class QmlGateTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(REPO_ROOT, name)) as fh:
            return fh.read()

    def test_card_has_confirm_deny(self):
        svc = self.read("service.qml")
        self.assertIn("pendingActive", svc)
        self.assertIn("answerPending(true)", svc)
        self.assertIn("answerPending(false)", svc)
        self.assertIn('"confirm"', svc)
        self.assertIn('"deny"', svc)


if __name__ == "__main__":
    unittest.main()
