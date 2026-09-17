#!/usr/bin/env python3
"""Tests for runtime-derived wake-word availability and safe selection.

Covers: the scanner (custom dir wins, feature extractors skipped, version
tags stripped), resolve accept/refuse (unknown names and the not-yet-
installed igris refuse with installed lists, never a bare traceback),
startup-error write/clear, jarvis-config set refusal keeping the prior
value, and install-wake-word refusals plus a genuine load validation.

Run with the daemon venv:
    ~/.local/share/jarvis/venv/bin/python -m unittest -v test_jarvis_wake
from this directory. No mic or audio needed; the install happy-path loads
one real openWakeWord model (seconds).
"""

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

DAEMON_DIR = os.path.dirname(os.path.abspath(__file__))


def load_module(name, filename):
    loader = importlib.machinery.SourceFileLoader(
        name, os.path.join(DAEMON_DIR, filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


jl = load_module("jl_wake", "jarvis-listen.py")
jcfg = load_module("jcfg_wake", "jarvis-config")


def fake_openwakeword(models_dir):
    mod = ModuleType("openwakeword")
    mod.__file__ = os.path.join(models_dir, "__init__.py")
    return mod


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-wake-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = os.path.join(self.tmp, "pkg", "resources", "models")
        self.custom = os.path.join(self.tmp, "custom")
        os.makedirs(self.pkg)
        os.makedirs(self.custom)
        for f in ("hey_jarvis_v0.1.onnx", "alexa_v0.1.onnx",
                  "melspectrogram.onnx", "embedding_model.onnx",
                  "silero_vad.onnx", "timer_v0.1.onnx"):
            open(os.path.join(self.pkg, f), "wb").write(b"fake")
        self.p_custom = mock.patch.object(jl, "CUSTOM_WAKE_DIR", self.custom)
        self.p_custom.start()
        self.addCleanup(self.p_custom.stop)
        self.fake = fake_openwakeword(os.path.join(self.tmp, "pkg"))
        self.p_mod = mock.patch.dict(sys.modules, {"openwakeword": self.fake})
        self.p_mod.start()
        self.addCleanup(self.p_mod.stop)

    def test_lists_shipped_models_without_extractors(self):
        models = jl.wake_models()
        self.assertIn("hey_jarvis", models)
        self.assertIn("alexa", models)
        self.assertIn("timer", models)
        for stem in (p for p, _ in models.values()):
            self.assertNotIn(stem, ("melspectrogram", "embedding_model",
                                    "silero_vad"))

    def test_version_tag_stripped_from_name(self):
        models = jl.wake_models()
        path, stem = models["hey_jarvis"]
        self.assertEqual(stem, "hey_jarvis_v0.1")  # score key keeps version
        self.assertTrue(path.endswith("hey_jarvis_v0.1.onnx"))

    def test_custom_dir_wins_over_shipped(self):
        open(os.path.join(self.custom, "hey_jarvis_v0.2.onnx"),
             "wb").write(b"custom")
        models = jl.wake_models()
        self.assertTrue(models["hey_jarvis"][0].startswith(self.custom))

    def test_custom_igris_resolves(self):
        open(os.path.join(self.custom, "igris_v0.1.onnx"), "wb").write(b"x")
        models = jl.wake_models()
        self.assertIn("igris", models)


class ResolveTests(unittest.TestCase):
    def test_unknown_name_refused_with_installed_list(self):
        with mock.patch.object(jl, "wake_models",
                               return_value={"hey_jarvis": ("/p", "s")}):
            with self.assertRaises(SystemExit) as ctx:
                jl.resolve_wake_model({"wake_word": "nope"})
        self.assertIn("hey_jarvis", str(ctx.exception))

    def test_igris_hint_when_missing(self):
        with mock.patch.object(jl, "wake_models", return_value={}):
            with self.assertRaises(SystemExit) as ctx:
                jl.resolve_wake_model({"wake_word": "igris"})
        self.assertIn("install-wake-word", str(ctx.exception))

    def test_installed_name_resolves(self):
        with mock.patch.object(jl, "wake_models",
                               return_value={"alexa": ("/p/a.onnx",
                                                       "alexa_v0.1")}):
            self.assertEqual(jl.resolve_wake_model({"wake_word": "alexa"}),
                             ("/p/a.onnx", "alexa_v0.1"))


class StartupErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-serr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        err = os.path.join(self.tmp, "startup_error")
        self.p_state = mock.patch.object(jl, "STATE_DIR", self.tmp)
        self.p_file = mock.patch.object(jl, "STARTUP_ERROR_FILE", err)
        self.p_ux_state = mock.patch.object(jl.ux_state, "STATE_DIR", self.tmp)
        self.p_ux_file = mock.patch.object(jl.ux_state, "STARTUP_ERROR_FILE",
                                           err)
        for p in (self.p_state, self.p_file, self.p_ux_state, self.p_ux_file):
            p.start()
            self.addCleanup(p.stop)

    def test_note_and_clear_roundtrip(self):
        jl.note_startup_error("[jarvis] wake_word 'nope' is not installed.")
        with open(os.path.join(self.tmp, "startup_error")) as f:
            self.assertIn("nope", f.read())
        jl.clear_startup_error()
        self.assertFalse(os.path.exists(os.path.join(self.tmp,
                                                     "startup_error")))

    def test_clear_missing_is_silent(self):
        jl.clear_startup_error()  # must not raise


class ConfigValidateTests(unittest.TestCase):
    def test_set_refuses_uninstalled_wake_word(self):
        cfg = os.path.join(tempfile.mkdtemp(prefix="jarvis-wcfg-"),
                           "config.toml")
        open(cfg, "w").write('wake_word = "hey_jarvis"\n')
        self.addCleanup(shutil.rmtree, os.path.dirname(cfg), True)
        with mock.patch.object(jl, "wake_models",
                               return_value={"hey_jarvis": ("/p", "s")}):
            with self.assertRaises(SystemExit):
                jcfg.validate(jl, "wake_word", "nope", cfg)
        # Prior value untouched.
        with open(cfg) as f:
            self.assertIn('"hey_jarvis"', f.read())

    def test_set_accepts_installed_wake_word(self):
        with mock.patch.object(jl, "wake_models",
                               return_value={"alexa": ("/p", "s")}):
            jcfg.validate(jl, "wake_word", "alexa", "/nonexistent.toml")

    def test_show_lists_installed_only(self):
        with mock.patch.object(jl, "wake_models",
                               return_value={"alexa": ("/p", "s")}):
            out = jcfg.wake_word_list(jl)
        by_name = {e["name"]: e["installed"] for e in out}
        self.assertTrue(by_name["alexa"])
        self.assertNotIn("igris", by_name)  # uninstalled custom wake hidden
        self.assertNotIn("hey_jarvis", by_name)  # not in this mock install set


class InstallTests(unittest.TestCase):
    def test_missing_source_refused(self):
        args = SimpleNamespace(name="igris", path="/nonexistent/x.onnx")
        with self.assertRaises(SystemExit):
            jcfg.cmd_install_wake(jl, args)

    def test_oversize_refused(self):
        src = os.path.join(tempfile.mkdtemp(prefix="jarvis-wsz-"), "x.onnx")
        self.addCleanup(shutil.rmtree, os.path.dirname(src), True)
        with open(src, "wb") as f:
            f.truncate(10)
        with mock.patch.object(jcfg, "MAX_WAKE_BYTES", 5):
            args = SimpleNamespace(name="igris", path=src)
            with self.assertRaises(SystemExit):
                jcfg.cmd_install_wake(jl, args)

    def test_real_model_installs_and_loads(self):
        import openwakeword
        src = os.path.join(os.path.dirname(openwakeword.__file__),
                           "resources", "models", "hey_jarvis_v0.1.onnx")
        if not os.path.exists(src):
            self.skipTest("shipped hey_jarvis model absent")
        tmp = tempfile.mkdtemp(prefix="jarvis-winst-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(jl, "CUSTOM_WAKE_DIR", tmp):
            args = SimpleNamespace(name="hey_jarvis_copy", path=src)
            self.assertEqual(jcfg.cmd_install_wake(jl, args), 0)
            self.assertTrue(os.path.exists(
                os.path.join(tmp, "hey_jarvis_copy.onnx")))


if __name__ == "__main__":
    unittest.main()
