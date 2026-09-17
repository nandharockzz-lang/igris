#!/usr/bin/env python3
"""Acceptance tests for the Basic/Full UX layer (hotkey toggle, avatar feeds).

Security properties must be unchanged by polish -- several tests assert the
toggle never touches [agents.*] and the daemon still refuses bad combos.
UI state files carry numbers only (no transcripts, audio, secrets, args).

Run with the daemon venv:
    ~/.local/share/jarvis/venv/bin/python -m unittest -v test_jarvis_ux
from this directory. No mic, audio server or model needed.
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from types import SimpleNamespace
from unittest import mock

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))


def load_module(name, filename):
    loader = importlib.machinery.SourceFileLoader(
        name, os.path.join(DAEMON_DIR, filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


jl = load_module("jl_ux", "jarvis-listen.py")
jt = load_module("jt_ux", "jarvis-toggle")
jcfg = load_module("jcfg_ux", "jarvis-config")


def write_config(path, mode="safe", actions=False):
    with open(path, "w") as fh:
        fh.write(f'agent = "opencode-voice"\nmode = "{mode}"\n\n'
                 '[agents.opencode-voice]\n'
                 'command = ["opencode", "run", "--agent", "jarvis-voice"]\n'
                 f'actions = {"true" if actions else "false"}\n'
                 'timeout = 60\n')


class ToggleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-toggle-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.cfg = os.path.join(self.tmp, "config.toml")
        write_config(self.cfg, mode="safe", actions=False)
        self.calls = []
        self.active = False
        self.set_rc = 0

    def _fake_run(self, argv, timeout=30):
        self.calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            out = "active\n" if self.active else "inactive\n"
            return SimpleNamespace(stdout=out, stderr="", returncode=0)
        if argv[:3] == ["systemctl", "--user", "start"]:
            self.active = True
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if argv[:3] == ["systemctl", "--user", "stop"]:
            self.active = False
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if argv[:3] == ["systemctl", "--user", "restart"]:
            self.active = True
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "apply" in argv:
            if self.set_rc != 0:
                return SimpleNamespace(stdout="", stderr="refused",
                                       returncode=1)
            # emulate jarvis-config apply for real: parse pairs, write the
            # mode/actions lines, and -- when asked to restart -- publish
            # fresh daemon state files like a healthy startup would.
            import re
            pairs = [a for a in argv[1:]
                     if "=" in a and not a.startswith("-")]
            text = open(self.cfg).read()
            if any(p.startswith("actions=") for p in pairs) \
                    and "[agents." not in text:
                return SimpleNamespace(stdout="",
                                       stderr="no [agents.x] section",
                                       returncode=1)
            for pair in pairs:
                key, _, value = pair.partition("=")
                if key == "mode":
                    lines = [l if not l.startswith("mode =")
                             else f'mode = "{value}"'
                             for l in text.splitlines()]
                    text = "\n".join(lines) + "\n"
                elif key == "actions":
                    text = re.sub(r"actions = (true|false)",
                                  f"actions = {value}", text, count=1)
            open(self.cfg, "w").write(text)
            if "--restart-unit" in argv:
                self.active = True
                mode = "safe"
                for line in text.splitlines():
                    if line.startswith("mode ="):
                        mode = line.split("=")[1].strip().strip('"')
                with open(os.path.join(self.state, "mode"), "w") as fh:
                    fh.write(mode)
                with open(os.path.join(self.state, "state"), "w") as fh:
                    fh.write("idle")
            return SimpleNamespace(stdout="applied; restarted and ready",
                                   stderr="", returncode=0)
        if "set" in argv:
            if self.set_rc != 0:
                return SimpleNamespace(stdout="", stderr="refused",
                                       returncode=1)
            # emulate jarvis-config: apply the mode write for real
            mode = argv[-1]
            text = open(self.cfg).read()
            lines = [l if not l.startswith("mode =") else f'mode = "{mode}"'
                     for l in text.splitlines()]
            open(self.cfg, "w").write("\n".join(lines) + "\n")
            return SimpleNamespace(stdout="updated", stderr="",
                                   returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    def _patch(self):
        p1 = mock.patch.object(jt, "_run", self._fake_run)
        p2 = mock.patch.object(jt, "find_helper", lambda n: "/bin/" + n)
        p3 = mock.patch.object(jt, "notify", lambda *a: None)
        p4 = mock.patch.object(jt, "announce", lambda *a: self.calls.append(
            ["announce"]))
        p1.start(); p2.start(); p3.start(); p4.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        self.addCleanup(p3.stop); self.addCleanup(p4.stop)

    def _args(self, *extra):
        return ["--unit", "jarvis-test", "--config", self.cfg,
                "--state-dir", self.state, *extra]

    def test_stopped_arms_basic(self):
        self._patch()
        self.assertEqual(jt.main(self._args()), 0)
        self.assertTrue(self.active)
        self.assertIn('mode = "workspace"', open(self.cfg).read())
        # Toggle owns the verbs flag: on when arming...
        self.assertIn("actions = true", open(self.cfg).read())

    def test_basic_steps_up_to_full(self):
        self._patch()
        write_config(self.cfg, mode="workspace", actions=True)
        with open(os.path.join(self.state, "mode"), "w") as fh:
            fh.write("workspace")
        with open(os.path.join(self.state, "armed_until"), "w") as fh:
            fh.write(f"{time.time() + 600:.0f}")
        self.active = True
        self.assertEqual(jt.main(self._args()), 0)
        self.assertIn('mode = "privileged"', open(self.cfg).read())
        self.assertIn(["announce"], self.calls)  # voice may announce Full

    def test_second_press_disarms_full(self):
        self._patch()
        write_config(self.cfg, mode="privileged", actions=True)
        with open(os.path.join(self.state, "mode"), "w") as fh:
            fh.write("privileged")
        arm = os.path.join(self.state, "armed_until")
        with open(arm, "w") as fh:
            fh.write(f"{time.time() + 600:.0f}")
        self.active = True
        set_calls = [c for c in self.calls if "set" in c]
        self.assertEqual(jt.main(self._args()), 0)
        self.assertFalse(self.active)
        self.assertFalse(os.path.exists(arm))
        # Disarm requests nothing: no mode write, no tracked-run kill here
        # (panic owns that); config mode left for the next cycle.
        self.assertEqual([c for c in self.calls if "set" in c], set_calls)
        # ...but the verbs flag returns, so a later Safe arm can start.
        self.assertIn("actions = false", open(self.cfg).read())

    def test_arm_writes_only_mode_and_actions(self):
        self._patch()
        before = open(self.cfg).read()
        self.assertEqual(jt.main(self._args()), 0)
        after = open(self.cfg).read()
        bl, al = before.splitlines(), after.splitlines()
        self.assertEqual(len(bl), len(al))
        changed = [(b, a) for b, a in zip(bl, al) if b != a]
        self.assertEqual(len(changed), 2)
        self.assertTrue(any(b.startswith("mode =") for b, _ in changed))
        self.assertTrue(any("actions" in b for b, _ in changed))

    def test_missing_section_aborts_before_arming(self):
        self._patch()
        with open(self.cfg, "w") as fh:
            fh.write('agent = "ghost"\nmode = "safe"\n')
        self.assertEqual(jt.main(self._args()), 1)
        self.assertFalse(self.active)  # never armed half-configured

    def test_armed_configs_pass_daemon_invariants(self):
        self._patch()
        self.assertEqual(jt.main(self._args()), 0)  # Basic
        cfg = jl.load_config(self.cfg)
        agent = jl.Agent(cfg["agent"], cfg["agents"][cfg["agent"]])
        self.assertEqual(jl.check_mode_invariants("workspace", agent), [])
        # Step up to Full (mocked live Basic state).
        with open(os.path.join(self.state, "mode"), "w") as fh:
            fh.write("workspace")
        with open(os.path.join(self.state, "armed_until"), "w") as fh:
            fh.write(f"{time.time() + 600:.0f}")
        self.assertEqual(jt.main(self._args()), 0)
        cfg = jl.load_config(self.cfg)
        agent = jl.Agent(cfg["agent"], cfg["agents"][cfg["agent"]])
        self.assertEqual(jl.check_mode_invariants("privileged", agent), [])

    def test_daemon_refusal_surfaces(self):
        self._patch()
        self.set_rc = 1  # jarvis-config refuses the transition
        self.assertEqual(jt.main(self._args()), 1)
        self.assertFalse(self.active)  # never armed past the refusal

    def test_status_reports_daemon_state(self):
        self._patch()
        with open(os.path.join(self.state, "mode"), "w") as fh:
            fh.write("privileged")
        with open(os.path.join(self.state, "armed_until"), "w") as fh:
            fh.write(f"{time.time() + 600:.0f}")
        self.active = True
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(jt.main(self._args("--status")), 0)
        status = json.loads(buf.getvalue())
        self.assertTrue(status["running"])
        self.assertEqual(status["mode"], "privileged")


class SafeGuardTests(unittest.TestCase):
    def test_panel_cannot_brick_safe_with_actions(self):
        tmp = tempfile.mkdtemp(prefix="jarvis-guard-")
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = os.path.join(tmp, "config.toml")
        write_config(cfg, mode="workspace", actions=True)
        daemon = jcfg.load_daemon()
        with self.assertRaises(SystemExit):
            jcfg.validate(daemon, "mode", "safe", cfg)
        # ...while a disarmed (actions=false) config switches freely.
        write_config(cfg, mode="workspace", actions=False)
        jcfg.validate(daemon, "mode", "safe", cfg)


class IntentTests(unittest.TestCase):
    def test_music_matches(self):
        self.assertEqual(jl.match_routine_intent("play blue monday"),
                         ("music", "blue monday"))
        self.assertEqual(jl.match_routine_intent("please play some jazz"),
                         ("music", "some jazz"))
        self.assertEqual(jl.match_routine_intent("put on lofi hip hop"),
                         ("music", "lofi hip hop"))
        self.assertEqual(
            jl.match_routine_intent("player dragon bird's theme song"),
            ("music", "dragon bird's theme song"))

    def test_inline_directive_stripped_and_parsed(self):
        spoken, directive = jl.extract_directive(
            "Playing Dragon Bird's theme song on YouTube now. "
            "<<jarvis:music dragon+bird+theme+song>>")
        self.assertEqual(directive, ("music", "dragon+bird+theme+song"))
        self.assertNotIn("jarvis:", spoken)
        self.assertTrue(spoken.startswith("Playing Dragon Bird"))
        self.assertEqual(jl.match_routine_intent("play my liked songs"),
                         ("mymusic", "liked"))
        self.assertEqual(jl.match_routine_intent("play my watch later"),
                         ("mymusic", "watchlater"))
        self.assertEqual(jl.match_routine_intent("play playlist gym"),
                         ("mymusic", "playlist gym"))

    def test_questions_and_chatter_never_match(self):
        for text in ("can you play something?", "how do i play music",
                     "what does play mean", "play", "stop", "quit", "volume",
                     "i like music", "", "   "):
            with self.subTest(text=text):
                self.assertIsNone(jl.match_routine_intent(text))

    def test_media_and_volume_matches(self):
        cases = {"pause": ("media", "pause"),
                 "resume the music": ("media", "play"),
                 "skip": ("media", "next"),
                 "previous track": ("media", "prev"),
                 "stop the music": ("media", "stop"),
                 "quit the music": ("media", "quit"),
                 "close the player": ("media", "quit"),
                 "volume up": ("volume", "up"),
                 "louder": ("volume", "up"),
                 "turn the music down": ("volume", "down"),
                 "mute the music": ("volume", "mute"),
                 "volume 50": ("volume", "50"),
                 "max volume": ("volume", "100"),
                 "open youtube": ("url", "https://www.youtube.com/"),
                 "open firefox": ("app", "firefox")}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(jl.match_routine_intent(text), want)
        self.assertIsNone(jl.match_routine_intent("volume 999"))

    def test_fast_path_skips_model_on_success(self):
        real_ask, real_bounded, real_speak = (jl.ask_agent, jl.run_bounded,
                                              jl.speak)
        spoken = []
        jl.ask_agent = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("model must not be called"))
        jl.run_bounded = lambda *a, **k: jl.BoundedRun(
            returncode=0, stdout="ok", stderr="", overflowed=False)
        jl.speak = lambda text, voice: spoken.append(text)
        try:
            agent = SimpleNamespace(actions=True, name="t")
            reply = jl.respond(agent, "/nonexistent", "play blue monday",
                               False, "workspace", "hotkey")
        finally:
            jl.ask_agent, jl.run_bounded, jl.speak = (real_ask, real_bounded,
                                                      real_speak)
        self.assertEqual(reply, "Playing it now.")
        self.assertEqual(spoken, ["Playing it now."])

    def test_refusal_falls_through_to_model(self):
        real_ask, real_speak = jl.ask_agent, jl.speak
        jl.ask_agent = lambda *a, **k: "hi there"
        jl.speak = lambda *a, **k: None
        try:
            agent = SimpleNamespace(actions=True, name="t")
            # Safe mode refuses the broker verb: the model still answers.
            reply = jl.respond(agent, "/nonexistent", "play blue monday",
                               False, "safe", "hotkey")
        finally:
            jl.ask_agent, jl.speak = real_ask, real_speak
        self.assertEqual(reply, "hi there")


class ModeWindowTests(unittest.TestCase):
    def test_full_window_shorter_than_basic(self):
        cfg = {"workspace": {"auto_disarm_seconds": 1800,
                             "full_disarm_seconds": 300}}
        self.assertEqual(jl.workspace_timeout(cfg, "workspace"), 1800)
        self.assertEqual(jl.workspace_timeout(cfg, "privileged"), 300)

    def test_windows_clamped(self):
        cfg = {"workspace": {"auto_disarm_seconds": 5,
                             "full_disarm_seconds": 99999}}
        self.assertEqual(jl.workspace_timeout(cfg, "workspace"), 60)
        self.assertEqual(jl.workspace_timeout(cfg, "privileged"), 3600)

    def test_privileged_voice_disarmed_blocked(self):
        tmp = tempfile.mkdtemp(prefix="jarvis-priv-")
        self.addCleanup(shutil.rmtree, tmp, True)
        arm = os.path.join(tmp, "armed_until")
        with open(arm, "w") as fh:
            fh.write(f"{time.time() - 5:.0f}")
        self.assertFalse(jl.run_directive(
            ("volume", "50"), mode="privileged", approval_source="voice",
            require_arm=True, arm_file=arm))


class EnvelopeTests(unittest.TestCase):
    def _wav(self, path, bursts):
        with wave.open(path, "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(16000)
            for amp, n in bursts:
                fh.writeframes(struct.pack(f"<{n}h", *([amp] * n)))

    def test_loud_and_silent_buckets(self):
        tmp = tempfile.mkdtemp(prefix="jarvis-env-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "t.wav")
        # 100ms buckets at 16kHz = 1600 frames each.
        self._wav(path, [(12000, 1600), (0, 1600), (12000, 1600)])
        env = jl._wav_envelope(path)
        self.assertEqual(len(env), 3)
        self.assertGreater(env[0], 80)
        self.assertEqual(env[1], 0)
        self.assertGreater(env[2], 80)

    def test_broken_file_gives_no_animation(self):
        tmp = tempfile.mkdtemp(prefix="jarvis-env-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "t.wav")
        with open(path, "wb") as fh:
            fh.write(b"not a wav")
        self.assertEqual(jl._wav_envelope(path), [])


class EqGateTests(unittest.TestCase):
    def test_gate_holds_idle_noise_at_zero(self):
        self.assertEqual(jl.rms_to_100(50.0, 300.0), 0)
        self.assertEqual(jl.rms_to_100(300.0, 300.0), 0)

    def test_speech_scales_bounded(self):
        v = jl.rms_to_100(900.0, 300.0)
        self.assertGreater(v, 0)
        self.assertLessEqual(v, 100)
        self.assertLessEqual(jl.rms_to_100(1e9, 300.0), 100)

    def test_publish_format_is_numbers_only(self):
        tmp = tempfile.mkdtemp(prefix="jarvis-mic-")
        self.addCleanup(shutil.rmtree, tmp, True)
        real_mic, real_tool = jl.MIC_FILE, jl.TOOL_FILE
        real_ux_mic = jl.ux_state.MIC_FILE
        real_ux_tool = jl.ux_state.TOOL_FILE
        real_ux_state = jl.ux_state.STATE_DIR
        mic_path = os.path.join(tmp, "mic")
        tool_path = os.path.join(tmp, "tool")
        jl.MIC_FILE = mic_path
        jl.TOOL_FILE = tool_path
        jl.ux_state.MIC_FILE = mic_path
        jl.ux_state.TOOL_FILE = tool_path
        jl.ux_state.STATE_DIR = tmp
        try:
            jl.publish_mic([10, 20, 30])
            jl.publish_tool("broker:volume")
            mic = open(os.path.join(tmp, "mic")).read().strip().split()
            self.assertEqual(len(mic), 13)  # 12 buckets + epoch
            self.assertTrue(all(p.replace(".", "").isdigit() for p in mic))
            tool = open(os.path.join(tmp, "tool")).read().strip().split()
            self.assertEqual(len(tool), 2)
            self.assertEqual(tool[0], "broker:volume")
        finally:
            jl.MIC_FILE, jl.TOOL_FILE = real_mic, real_tool
            jl.ux_state.MIC_FILE = real_ux_mic
            jl.ux_state.TOOL_FILE = real_ux_tool
            jl.ux_state.STATE_DIR = real_ux_state


class AuditCleanlinessTests(unittest.TestCase):
    def test_directive_audit_carries_no_free_text(self):
        tmp = tempfile.mkdtemp(prefix="jarvis-audit-")
        self.addCleanup(shutil.rmtree, tmp, True)
        real_file, real_tool = jl.AUDIT_FILE, jl.TOOL_FILE
        real_ux_tool = jl.ux_state.TOOL_FILE
        real_ux_state = jl.ux_state.STATE_DIR
        jl.AUDIT_FILE = os.path.join(tmp, "audit.log")
        jl.TOOL_FILE = os.path.join(tmp, "tool")
        jl.ux_state.TOOL_FILE = jl.TOOL_FILE
        jl.ux_state.STATE_DIR = tmp
        real_bounded = jl.run_bounded
        jl.run_bounded = lambda *a, **k: jl.BoundedRun(
            returncode=0, stdout="ok", stderr="", overflowed=False)
        try:
            jl.run_directive(("app", "Super Secret Project X"),
                             mode="workspace", approval_source="hotkey")
            jl.run_directive(("workspace", "my secret room"),
                             mode="workspace", approval_source="hotkey")
        finally:
            jl.run_bounded = real_bounded
            jl.AUDIT_FILE, jl.TOOL_FILE = real_file, real_tool
            jl.ux_state.TOOL_FILE = real_ux_tool
            jl.ux_state.STATE_DIR = real_ux_state
        log = open(os.path.join(tmp, "audit.log")).read()
        self.assertNotIn("Secret", log)
        self.assertNotIn("secret", log)


class LiveAgreementTests(unittest.TestCase):
    def test_show_live_matches_daemon_files(self):
        tmp = tempfile.mkdtemp(prefix="jarvis-live-")
        self.addCleanup(shutil.rmtree, tmp, True)
        daemon = jcfg.load_daemon()
        real_mode, real_arm = daemon.MODE_FILE, daemon.ARM_FILE
        daemon.MODE_FILE = os.path.join(tmp, "mode")
        daemon.ARM_FILE = os.path.join(tmp, "armed_until")
        try:
            with open(daemon.MODE_FILE, "w") as fh:
                fh.write("workspace")
            with open(daemon.ARM_FILE, "w") as fh:
                fh.write(f"{time.time() + 600:.0f}")
            with mock.patch("subprocess.run") as mrun:
                mrun.return_value = SimpleNamespace(stdout="active\n",
                                                    stderr="", returncode=0)
                live = jcfg.live_state(daemon)
        finally:
            daemon.MODE_FILE, daemon.ARM_FILE = real_mode, real_arm
        self.assertEqual(live["mode"], "workspace")
        self.assertTrue(live["armed"])
        self.assertGreater(live["remaining"], 0)

    def test_bindings_toggle_and_panic_distinct(self):
        text = open(os.path.join(os.path.expanduser("~"), ".config",
                                 "hypr", "bindings.lua")).read()
        self.assertIn("SHIFT + J", text)
        self.assertIn("jarvis-toggle", text)
        self.assertIn("SHIFT + K", text)
        self.assertIn("jarvis-rollback --panic", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
