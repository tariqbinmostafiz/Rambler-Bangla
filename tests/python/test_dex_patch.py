#!/usr/bin/env python3
"""Unit tests for the pure helpers in standalone-patcher/dex_patch.py.

Needs only Python 3 (stdlib unittest): no JDK, toolchain or APK.
    python3 -m unittest discover -s tests/python -v
"""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PATCHER = os.path.join(ROOT, "standalone-patcher", "dex_patch.py")

_spec = importlib.util.spec_from_file_location("dex_patch", PATCHER)
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)

CLEANUP_PROTO = ("(Lhrl;Ljava/lang/String;ILqqi;Laebb;Ljava/lang/String;"
                 "Laugd;Ljava/util/concurrent/atomic/AtomicBoolean;)V")


def read_lines(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


class SmaliStringTests(unittest.TestCase):
    def test_unescape_basic_escapes(self):
        self.assertEqual(dp.smali_unescape(r"a\nb\tc\\d\"e\'f"), "a\nb\tc\\d\"e'f")

    def test_unescape_unicode(self):
        self.assertEqual(dp.smali_unescape(r"\u09ac\u09be"), "বা")

    def test_unescape_rejects_trailing_backslash(self):
        with self.assertRaises(dp.PatchError):
            dp.smali_unescape("abc\\")

    def test_unescape_rejects_unknown_escape(self):
        with self.assertRaises(dp.PatchError):
            dp.smali_unescape(r"\q")

    def test_parse_const_string(self):
        self.assertEqual(dp.parse_const_string('    const-string v1, "{ENABLED_LANGUAGES}"'),
                         ("v1", "{ENABLED_LANGUAGES}"))
        self.assertEqual(dp.parse_const_string('    const-string/jumbo p0, "x\\ny"'),
                         ("p0", "x\ny"))
        self.assertIsNone(dp.parse_const_string("    const/4 v0, 0x1"))

    def test_sha256_text_is_utf8(self):
        self.assertEqual(dp.sha256_text("বাংলা"),
                         hashlib.sha256("বাংলা".encode("utf-8")).hexdigest())


class InvokeParseTests(unittest.TestCase):
    def test_parse_invoke_list(self):
        regs, ref = dp.parse_invoke("    invoke-virtual {p1, v1, v2}, " + dp.STRING_REPLACE)
        self.assertEqual(regs, ["p1", "v1", "v2"])
        self.assertEqual(ref, dp.STRING_REPLACE)

    def test_parse_invoke_range(self):
        regs, _ = dp.parse_invoke("    invoke-static/range {v3 .. v5}, Lx;->y()V")
        self.assertEqual(regs, ["v3", "v4", "v5"])

    def test_parse_invoke_empty_and_non_invoke(self):
        self.assertEqual(dp.parse_invoke("    invoke-static {}, Lx;->y()V"), ([], "Lx;->y()V"))
        self.assertIsNone(dp.parse_invoke("    move-result-object v0"))


class RegisterMathTests(unittest.TestCase):
    def test_param_slots_static_and_instance(self):
        self.assertEqual(dp.param_slots("()V", True), 0)
        self.assertEqual(dp.param_slots("()V", False), 1)
        self.assertEqual(dp.param_slots("(ILjava/lang/String;Z)V", True), 3)

    def test_param_slots_wide_types(self):
        self.assertEqual(dp.param_slots("(JD)V", True), 4)

    def test_param_slots_arrays_are_one_slot(self):
        self.assertEqual(dp.param_slots("([J)V", True), 1)
        self.assertEqual(dp.param_slots("([[D)V", True), 1)
        self.assertEqual(dp.param_slots("([Ljava/lang/String;I)V", True), 2)

    def test_param_slots_cleanup_builder(self):
        self.assertEqual(dp.param_slots(CLEANUP_PROTO, True), 8)

    def test_bump_locals_from_locals(self):
        lines = [".method public static f()V", "    .locals 3", ".end method"]
        self.assertEqual(dp.bump_locals(lines, 0, 3, "()V", True), 3)
        self.assertEqual(lines[1], "    .locals 6")

    def test_bump_locals_from_registers(self):
        lines = [".method public f(J)V", "    .registers 6", ".end method"]
        # 6 registers - (this + long) = 3 locals
        self.assertEqual(dp.bump_locals(lines, 0, 2, "(J)V", False), 3)
        self.assertEqual(lines[1], "    .locals 5")

    def test_bump_locals_missing_directive(self):
        with self.assertRaises(dp.PatchError):
            dp.bump_locals([".method f()V", ".end method"], 0, 1, "()V", True)


class FixtureSmaliTests(unittest.TestCase):
    """Runs the dataflow helpers on the synthetic fixture's cleanup builder."""

    def setUp(self):
        self.lines = read_lines(os.path.join(ROOT, "fixtures", "src", "kfd.smali"))

    def test_method_bounds(self):
        start, end = dp.method_bounds(self.lines, "d", CLEANUP_PROTO)
        self.assertTrue(self.lines[start].startswith(".method"))
        self.assertEqual(self.lines[end].strip(), ".end method")

    def test_method_bounds_missing(self):
        with self.assertRaises(dp.PatchError):
            dp.method_bounds(self.lines, "nope", "()V")

    def test_find_enabled_languages_site(self):
        start, end = dp.method_bounds(self.lines, "d", CLEANUP_PROTO)
        langs_reg, result_reg, idx = dp.find_enabled_languages_site(
            self.lines, start, end, "{ENABLED_LANGUAGES}")
        self.assertEqual((langs_reg, result_reg), ("v2", "v0"))
        self.assertIn("move-result-object", self.lines[idx])

    def test_find_enabled_languages_site_fails_closed(self):
        start, end = dp.method_bounds(self.lines, "d", CLEANUP_PROTO)
        with self.assertRaises(dp.PatchError):
            dp.find_enabled_languages_site(self.lines, start, end, "{NOT_THERE}")

    def test_hook_snippet_uses_consecutive_temps(self):
        snip = dp.hook_snippet("v3", "v0", "v2", dp.EXT_DEFAULT, dp.RULE_HOOK)
        self.assertEqual(len(snip), 5)
        self.assertIn("invoke-static/range {v3 .. v4}", snip[2])
        self.assertTrue(snip[2].endswith(dp.RULE_HOOK +
                        "(Ljava/lang/String;Ljava/lang/String;)Ljava/lang/String;"))
        self.assertEqual(snip[-1].split(), ["move-object/from16", "v0,", "v3"])


class AdaptiveDiscoveryTests(unittest.TestCase):
    """Adaptive mode tolerates hash/name changes only when dataflow is clear."""

    def test_adaptive_finds_renamed_builder_and_changed_rule_hash(self):
        import tempfile
        from pathlib import Path

        profile = dp.Profile(os.path.join(ROOT, "fixtures", "fixture-profile.json"))
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "classes"
            d.mkdir()
            kfd = read_lines(os.path.join(ROOT, "fixtures", "src", "kfd.smali"))
            # Emulate an app revision: obfuscated class/method/proto changes and
            # the policy string changes while retaining its recognizable heading.
            kfd = [line.replace("Lkfd;", "Lrevision/Builder;")
                       .replace(".method public static d(Lhrl;Ljava/lang/String;ILqqi;Laebb;Ljava/lang/String;Laugd;Ljava/util/concurrent/atomic/AtomicBoolean;)V",
                                ".method public static buildRevision()V")
                       .replace("3. Hinglish Override:", "3. Hinglish Override revision:")
                   for line in kfd]
            (d / "Builder.smali").write_text("\n".join(kfd) + "\n", encoding="utf-8")
            kew = read_lines(os.path.join(ROOT, "fixtures", "src", "kew.smali"))
            kew = [line.replace("Lkew;", "Lrevision/Prompt;")
                       .replace("<fixture_system_role>", "<different_prompt_model>")
                   for line in kew]
            (d / "Prompt.smali").write_text("\n".join(kew) + "\n", encoding="utf-8")
            target = dp.discover_adaptive_builder([("classes3.dex", td)], profile)
            self.assertEqual(target["class"], "Lrevision/Builder;")
            self.assertEqual(target["name"], "buildRevision")
            self.assertEqual(target["signature"], "()V")
            dp.apply_adaptive_target(profile, target)
            lines = read_lines(str(d / "Builder.smali"))
            patched = dp.patch_method(lines, profile, {}, adaptive=True)
            joined = "\n".join(patched)
            self.assertIn("selectHinglishOverrideRule", joined)
            self.assertIn("rewriteCleanupPromptScriptGate", joined)

    def test_adaptive_refuses_ambiguous_builders(self):
        import tempfile
        from pathlib import Path

        profile = dp.Profile(os.path.join(ROOT, "fixtures", "fixture-profile.json"))
        with tempfile.TemporaryDirectory() as td:
            for folder, class_name in (("one", "Lrevision/BuilderOne;"),
                                       ("two", "Lrevision/BuilderTwo;")):
                d = Path(td) / folder
                d.mkdir()
                kfd = read_lines(os.path.join(ROOT, "fixtures", "src", "kfd.smali"))
                kfd = [line.replace("Lkfd;", class_name) for line in kfd]
                (d / "Builder.smali").write_text("\n".join(kfd) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(dp.PatchError, "multiple candidate builders"):
                dp.discover_adaptive_builder([("classes3.dex", td)], profile)

    def test_sha256_handles_smali_utf16_surrogate_pair(self):
        # The same glyph as a literal Unicode character and a UTF-16 surrogate
        # pair should hash to the same logical UTF-8 text.
        literal = "A😀B"
        escaped_pair = "A\ud83d\ude00B"
        self.assertEqual(dp.sha256_text(literal), dp.sha256_text(escaped_pair))


class ProfileTests(unittest.TestCase):
    PROFILES = [os.path.join(ROOT, "fingerprints", "gboard-18.3.1.json"),
                os.path.join(ROOT, "fixtures", "fixture-profile.json")]

    def test_profiles_load_and_are_well_formed(self):
        for path in self.PROFILES:
            with self.subTest(profile=os.path.basename(path)):
                prof = dp.Profile(path)
                for key in ("cleanup_builder", "prompt_owner"):
                    cls, name, sig = prof.method(key)
                    self.assertRegex(cls, r"^L[\w/$]+;$")
                    self.assertTrue(sig.startswith("(") and ")" in sig)
                self.assertEqual(set(prof.anchors), {"enabled_languages_placeholder",
                                                     "hinglish_placeholder"})
                for fp in prof.fingerprints.values():
                    self.assertRegex(fp["sha256"], r"^[0-9a-f]{64}$")
                    self.assertGreater(fp["bytes"], 0)
                self.assertEqual(prof.ext_class, dp.EXT_DEFAULT)

    def test_profile_stores_hashes_not_strings(self):
        with open(self.PROFILES[0], encoding="utf-8") as fh:
            raw = json.load(fh)
        for fp in raw["fingerprints"].values():
            self.assertNotIn("text", fp)
            self.assertNotIn("value", fp)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, PATCHER, *args],
                              capture_output=True, text=True, timeout=60)

    def test_help(self):
        proc = self.run_cli("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("analyze", proc.stdout)
        self.assertIn("--profile", proc.stdout)

    def test_missing_required_args(self):
        self.assertNotEqual(self.run_cli("analyze").returncode, 0)


class LoanspellDataTests(unittest.TestCase):
    """The OVERRIDE block in the Java runtime must match data/loanspell/overrides.tsv."""

    def test_overrides_in_sync_with_java(self):
        tsv = os.path.join(ROOT, "data", "loanspell", "overrides.tsv")
        java = os.path.join(ROOT, "extension-src", "com", "akshaykadam", "pixelboard",
                            "extension", "rambler", "GboardRamblerLoanSpell.java")
        with open(tsv, encoding="utf-8") as fh:
            rows = [l.rstrip("\n").split("\t") for l in fh if l.strip()]
        with open(java, encoding="utf-8") as fh:
            src = fh.read()
        block = re.search(r"OVERRIDE = map\(\n(.*?)\);", src, re.S).group(1)
        pairs = re.findall(r'"((?:[^"\\]|\\.)*)",\s*"((?:[^"\\]|\\.)*)"', block)
        decoded = [(w, b.encode("ascii").decode("unicode_escape")) for w, b in pairs]
        self.assertEqual(decoded, [(r[0], r[1]) for r in rows])


if __name__ == "__main__":
    unittest.main()
