#!/usr/bin/env python3
"""Tests for the [stt] speech-engine configuration layer.

Covers: defaults merge (existing configs without [stt] keep working),
fail-fast validation of engine/model names, jarvis-owned voxtype config
materialization (pins model + vocabulary without touching the user's
voxtype setup), transcribe argv (local passes use -c + --engine), and the
opt-in cloud fallback (only on empty local result, only when enabled).

Run with the daemon venv:
    ~/.local/share/jarvis/venv/bin/python -m unittest -v test_jarvis_stt
from this directory. No mic, audio server, model or voxtype needed --
run_bounded is stubbed.
"""

import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
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


jl = load_module("jl_stt", "jarvis-listen.py")


def ok_result(stdout):
    proc = SimpleNamespace(returncode=0, overflowed=False, stdout=stdout)
    return proc


class ResolveTests(unittest.TestCase):
    def test_defaults_apply_without_stt_section(self):
        stt = jl.resolve_stt({})
        self.assertEqual(stt["engine"], "whisper")
        self.assertEqual(stt["model"], "base.en")
        self.assertEqual(stt["language"], "en")
        self.assertFalse(stt["cloud_fallback"])

    def test_unknown_engine_refused(self):
        with self.assertRaises(SystemExit):
            jl.resolve_stt({"stt": {"engine": "dragonspeech"}})

    def test_unknown_model_refused(self):
        with self.assertRaises(SystemExit):
            jl.resolve_stt({"stt": {"engine": "whisper",
                                    "model": "huge.en"}})

    def test_parakeet_model_names_accepted(self):
        stt = jl.resolve_stt({"stt": {"engine": "parakeet",
                                      "model": "parakeet-tdt-0.6b-v3-int8"}})
        self.assertEqual(stt["model"], "parakeet-tdt-0.6b-v3-int8")

    def test_empty_language_refused(self):
        with self.assertRaises(SystemExit):
            jl.resolve_stt({"stt": {"language": "  "}})

    def test_missing_whisper_file_refused(self):
        with mock.patch.object(jl, "VOXTYPE_MODELS_DIR", "/nonexistent"):
            with self.assertRaises(SystemExit):
                jl.resolve_stt({"stt": {"engine": "whisper",
                                        "model": "base.en"}})


class MaterializeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-stt-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.vox = os.path.join(self.tmp, "voxtype.toml")
        self.p_state = mock.patch.object(jl, "STATE_DIR", self.tmp)
        self.p_vox = mock.patch.object(jl, "JARVIS_VOX_CONFIG", self.vox)
        self.p_state.start()
        self.p_vox.start()
        self.addCleanup(self.p_state.stop)
        self.addCleanup(self.p_vox.stop)

    def test_whisper_config_pins_model_and_vocabulary(self):
        stt = {"engine": "whisper", "model": "base.en", "language": "en",
               "vocabulary": "jarvis firefox", "cloud_fallback": False,
               "fallback_engine": "soniox"}
        jl.write_voxtype_config(stt)
        with open(self.vox) as f:
            text = f.read()
        self.assertIn('model = "base.en"', text)
        self.assertIn('language = "en"', text)
        self.assertIn('initial_prompt = "jarvis firefox"', text)
        self.assertNotIn("parakeet", text)


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self.stt = {"engine": "whisper", "model": "base.en", "language": "en",
                    "vocabulary": "", "cloud_fallback": False,
                    "fallback_engine": "soniox"}

    def run_transcribe(self, results):
        """Stub run_bounded with a result queue; return argv list."""
        argv = []
        queue = list(results)

        def fake(cmd, **kw):
            argv.append(cmd)
            return queue.pop(0)

        with mock.patch.object(jl, "run_bounded", side_effect=fake):
            with mock.patch.object(jl, "JARVIS_VOX_CONFIG", "/tmp/vox.toml"):
                text = jl.transcribe("/tmp/cmd.wav", self.stt)
        return text, argv

    def test_local_pass_uses_owned_config_and_engine(self):
        text, argv = self.run_transcribe([ok_result("open firefox\n")])
        self.assertEqual(text, "open firefox")
        self.assertEqual(len(argv), 1)
        self.assertEqual(argv[0][:4],
                         ["voxtype", "-c", "/tmp/vox.toml", "transcribe"])
        self.assertIn("--engine", argv[0])
        self.assertIn("whisper", argv[0])

    def test_no_fallback_when_disabled(self):
        text, argv = self.run_transcribe([ok_result("\n")])
        self.assertEqual(text, "")
        self.assertEqual(len(argv), 1)

    def test_fallback_on_empty_only_when_enabled(self):
        self.stt["cloud_fallback"] = True
        text, argv = self.run_transcribe([ok_result("\n"),
                                          ok_result("open firefox\n")])
        self.assertEqual(text, "open firefox")
        self.assertEqual(len(argv), 2)
        # Fallback goes through the USER config (their keys): no -c flag.
        self.assertNotIn("-c", argv[1])
        self.assertIn("soniox", argv[1])

    def test_no_fallback_when_local_succeeds(self):
        self.stt["cloud_fallback"] = True
        text, argv = self.run_transcribe([ok_result("pause the music\n")])
        self.assertEqual(text, "pause the music")
        self.assertEqual(len(argv), 1)


if __name__ == "__main__":
    unittest.main()
