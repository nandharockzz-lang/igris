#!/usr/bin/env python3
"""Tests for the atomic mode/restart transition (jarvis-config apply).

Covers: validate-all-before-writing (a bad pair aborts with the file
byte-identical), single-write multi-key updates, the actions flag writer
(including refusal to invent sections), readiness gating (stale daemon
files never count, fresh refusal fails fast), rollback (config bytes and
unit state restored, fresh startup error dropped), the no-pairs restart
path, show carrying startup_error/stt for the panel, and static guards on
the QML side (panel routes through apply, card surface matches the card).

Run with the daemon venv:
    ~/.local/share/jarvis/venv/bin/python -m unittest -v test_jarvis_transition
from this directory. systemctl is stubbed; no daemon, mic or audio needed.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
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


jl = load_module("jl_tr", "jarvis-listen.py")
jcfg = load_module("jcfg_tr", "jarvis-config")


def write_config(path, mode="safe", actions=False, agent="opencode-voice"):
    with open(path, "w") as fh:
        fh.write(f'agent = "{agent}"\nmode = "{mode}"\n\n'
                 f'[agents.{agent}]\n'
                 'command = ["opencode", "run", "--agent", "jarvis-voice"]\n'
                 f'actions = {"true" if actions else "false"}\n'
                 'timeout = 60\n')


def file_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


class ApplyValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-apply-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = os.path.join(self.tmp, "config.toml")
        write_config(self.cfg)

    def args(self, *pairs, restart=None, wait=25):
        return SimpleNamespace(config=self.cfg, pairs=list(pairs),
                               restart_unit=restart, wait_ready=wait)

    def test_bad_pair_aborts_before_any_write(self):
        before = file_bytes(self.cfg)
        with self.assertRaises(SystemExit):
            jcfg.cmd_apply(jl, self.args("mode=workspace",
                                         "listen.silence_tail=banana"))
        self.assertEqual(file_bytes(self.cfg), before)

    def test_unknown_key_aborts_before_any_write(self):
        before = file_bytes(self.cfg)
        with self.assertRaises(SystemExit):
            jcfg.cmd_apply(jl, self.args("mode=workspace", "frobnicate=1"))
        self.assertEqual(file_bytes(self.cfg), before)

    def test_actions_refuses_missing_section(self):
        with open(self.cfg, "w") as fh:
            fh.write('agent = "ghost"\nmode = "safe"\n')
        with self.assertRaises(SystemExit):
            jcfg.cmd_apply(jl, self.args("actions=true"))

    def test_multi_key_single_write(self):
        writes = []
        real_write = jcfg.write

        def counting(path, lines):
            writes.append(path)
            return real_write(path, lines)

        with mock.patch.object(jcfg, "write", side_effect=counting):
            self.assertEqual(jcfg.cmd_apply(
                jl, self.args("mode=workspace", "actions=true")), 0)
        self.assertEqual(writes, [self.cfg])
        text = open(self.cfg).read()
        self.assertIn('mode = "workspace"', text)
        self.assertIn("actions = true", text)


class ApplyRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-apr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = os.path.join(self.tmp, "config.toml")
        write_config(self.cfg)
        self.state = os.path.join(self.tmp, "run")
        os.makedirs(self.state)
        self.calls = []
        self.active = False
        self.p_run = mock.patch.object(jcfg, "_run", self._fake_run)
        self.p_state = mock.patch.object(jl, "STATE_DIR", self.state)
        self.p_sfile = mock.patch.object(jl, "STATE_FILE",
                                          os.path.join(self.state, "state"))
        self.p_mfile = mock.patch.object(jl, "MODE_FILE",
                                          os.path.join(self.state, "mode"))
        self.p_err = mock.patch.object(jl, "STARTUP_ERROR_FILE",
                                        os.path.join(self.state,
                                                     "startup_error"))
        for p in (self.p_run, self.p_state, self.p_sfile, self.p_mfile,
                  self.p_err):
            p.start()
            self.addCleanup(p.stop)

    def _fake_run(self, argv, timeout=30):
        self.calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            out = "active\n" if self.active else "inactive\n"
            return SimpleNamespace(stdout=out, stderr="", returncode=0)
        if argv[:3] == ["systemctl", "--user", "restart"]:
            self.active = True
            # Emulate a healthy daemon startup publishing fresh files.
            with open(os.path.join(self.state, "state"), "w") as fh:
                fh.write("idle")
            mode = "workspace"
            try:
                text = open(self.cfg).read()
                for line in text.splitlines():
                    if line.startswith("mode ="):
                        mode = line.split("=")[1].strip().strip('"')
            except OSError:
                pass
            with open(os.path.join(self.state, "mode"), "w") as fh:
                fh.write(mode)
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if argv[:3] == ["systemctl", "--user", "stop"]:
            self.active = False
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    def args(self, *pairs, restart="jarvis-test", wait=5):
        return SimpleNamespace(config=self.cfg, pairs=list(pairs),
                               restart_unit=restart, wait_ready=wait)

    def test_happy_path_restarts_once_and_reports_ready(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(jcfg.cmd_apply(
                jl, self.args("mode=workspace", "actions=true")), 0)
        restarts = [c for c in self.calls
                    if c[:3] == ["systemctl", "--user", "restart"]]
        self.assertEqual(len(restarts), 1)
        self.assertIn("ready", buf.getvalue())
        text = open(self.cfg).read()
        self.assertIn('mode = "workspace"', text)
        self.assertIn("actions = true", text)

    def test_no_pairs_restart_only(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(jcfg.cmd_apply(jl, self.args()), 0)
        self.assertIn("restarted and ready", buf.getvalue())

    def test_unready_rolls_back_config_and_stops(self):
        # No fresh state files: the daemon never proves it is up.
        for name in ("state", "mode"):
            try:
                os.unlink(os.path.join(self.state, name))
            except OSError:
                pass
        real_fake, self.active = self._fake_run, False

        def no_publish(argv, timeout=30):
            if argv[:3] == ["systemctl", "--user", "restart"]:
                self.calls.append(argv)
                self.active = True  # unit up, daemon silent: stale files
                return SimpleNamespace(stdout="", stderr="", returncode=0)
            return real_fake(argv, timeout)

        before = file_bytes(self.cfg)
        with mock.patch.object(jcfg, "_run", side_effect=no_publish):
            with self.assertRaises(SystemExit) as ctx:
                jcfg.cmd_apply(jl, self.args("mode=workspace",
                                             "actions=true", wait=0.1))
        self.assertIn("rolled back", str(ctx.exception))
        self.assertEqual(file_bytes(self.cfg), before)
        stops = [c for c in self.calls
                 if c[:3] == ["systemctl", "--user", "stop"]]
        self.assertTrue(stops)  # was inactive: restored to stopped
        self.assertFalse(self.active)

    def test_fresh_refusal_fails_fast_with_reason(self):
        err = os.path.join(self.state, "startup_error")
        with open(err, "w") as fh:
            fh.write("[jarvis] wake_word 'nope' is not installed.")
        future = time.time() + 60
        os.utime(err, (future, future))
        before = file_bytes(self.cfg)
        with self.assertRaises(SystemExit) as ctx:
            jcfg.cmd_apply(jl, self.args("mode=workspace", wait=20))
        self.assertIn("nope", str(ctx.exception))
        self.assertEqual(file_bytes(self.cfg), before)
        # The fresh refusal belonged to the failed attempt: dropped.
        self.assertFalse(os.path.exists(err))

    def test_stale_files_never_count_as_ready(self):
        with open(os.path.join(self.state, "state"), "w") as fh:
            fh.write("idle")
        with open(os.path.join(self.state, "mode"), "w") as fh:
            fh.write("workspace")
        old = time.time() - 600
        for name in ("state", "mode"):
            os.utime(os.path.join(self.state, name), (old, old))

        def no_publish(argv, timeout=30):
            if argv[:3] == ["systemctl", "--user", "restart"]:
                self.calls.append(argv)
                self.active = True
                return SimpleNamespace(stdout="", stderr="", returncode=0)
            return self._fake_run(argv, timeout)

        with mock.patch.object(jcfg, "_run", side_effect=no_publish):
            with self.assertRaises(SystemExit):
                jcfg.cmd_apply(jl, self.args("mode=workspace", wait=0.1))


class ShowTests(unittest.TestCase):
    def test_show_carries_startup_error_and_stt(self):
        tmp = tempfile.mkdtemp(prefix="jarvis-show-")
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = os.path.join(tmp, "config.toml")
        write_config(cfg)
        with mock.patch.object(jcfg, "get_available_models", return_value=[]):
            args = SimpleNamespace(config=cfg)
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(jcfg.cmd_show(jl, args), 0)
        d = json.loads(buf.getvalue())
        self.assertIn("startup_error", d)
        self.assertEqual(d["startup_error"], "")
        self.assertEqual(d["stt"]["engine"], "whisper")
        self.assertTrue(any(isinstance(w, dict) and "installed" in w
                            for w in d["wake_words"]))


class QmlStaticTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(REPO_ROOT, name)) as fh:
            return fh.read()

    def test_panel_routes_armed_writes_through_apply(self):
        panel = self.read("Panel.qml")
        self.assertIn('"apply"', panel)
        self.assertIn('"--restart-unit"', panel)
        self.assertNotIn("restartProc", panel)

    def test_panel_wake_dropdown_offers_installed_only(self):
        panel = self.read("Panel.qml")
        self.assertIn("if (!inst) continue", panel)

    def test_panel_surfaces_diagnostics(self):
        panel = self.read("Panel.qml")
        self.assertIn("startupError", panel)
        self.assertIn("sttEngine", panel)
        self.assertIn("Listener: ", panel)

    def test_card_surface_matches_card(self):
        # Console surface lives in ConsoleCard.qml (hosted by DraggableAvatar);
        # the service owns pending-confirm polling and answerPending.
        card = self.read("ConsoleCard.qml")
        self.assertIn("id: card", card)
        avatar = self.read("DraggableAvatar.qml")
        self.assertIn("ConsoleCard {", avatar)
        svc = self.read("service.qml")
        self.assertIn("pendingActive", svc)
        self.assertIn("answerPending", svc)

    def test_avatar_falls_back_to_canvas(self):
        goku = self.read("GokuAvatar.qml")
        self.assertIn("ChibiAvatar {", goku)
        self.assertIn("visible: !root.spriteReady", goku)
        self.assertIn("visible: root.spriteReady", goku)


if __name__ == "__main__":
    unittest.main()
