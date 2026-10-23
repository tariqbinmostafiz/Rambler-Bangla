#!/usr/bin/env python3
"""
dex_patch.py - fail-closed, fingerprint-guarded smali patcher for the
Rambler Bangla - guarded Gboard Rambler Lite Bengali script patch.

GPL-3.0. Derived work of PixelBoard (https://github.com/Akshayykadam/PixelBoard).

This is the reproducible local route used while the credentialed Morphe Gradle
plugin cannot resolve. It never edits bytes blindly: it disassembles the target
DEX with baksmali, verifies exact method anchors and SHA-256 string
fingerprints from a profile, derives the enabled-languages register by local
dataflow on the disassembly, inserts two runtime-hook calls in smali, and
reassembles. Any anchor or fingerprint mismatch aborts with no output.

Requires: a JDK on PATH (or JAVA_HOME), baksmali + smali fat jars.
No Google binaries are included in this repository; the user supplies a
lawfully obtained Gboard APK.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

EXT_DEFAULT = ("Lcom/akshaykadam/pixelboard/extension/ramblerlite/"
               "GboardRamblerLiteScriptRuntime;")
RULE_HOOK = "selectHinglishOverrideRule"
PROMPT_HOOK = "rewriteCleanupPromptScriptGate"
STRING_REPLACE = ("Ljava/lang/String;->replace(Ljava/lang/CharSequence;"
                  "Ljava/lang/CharSequence;)Ljava/lang/String;")


class PatchError(Exception):
    pass


# ---------------------------------------------------------------- smali utils

def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PatchError("command failed: %s\n%s%s" % (
            " ".join(cmd), proc.stdout, proc.stderr))
    return proc.stdout


def java_cmd():
    home = os.environ.get("JAVA_HOME")
    return os.path.join(home, "bin", "java") if home else "java"


def smali_prefix(kind, jar):
    """Command prefix for smali/baksmali: fat classpath via SMALI_CP if set,
    else a main-class classpath of the two thin jars is NOT assumed; fall back
    to -jar with the given jar (works for the official fat jars)."""
    cp = os.environ.get("SMALI_CP")
    main = "org.jf.baksmali.Main" if kind == "baksmali" else "org.jf.smali.Main"
    if cp:
        return [java_cmd(), "-cp", cp, main]
    return [java_cmd(), "-jar", jar]


def smali_class_path(class_type):
    """Lcom/foo/Bar; -> com/foo/Bar.smali ; Lkfd; -> kfd.smali"""
    return class_type.strip()[1:-1] + ".smali"


def smali_unescape(s):
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(s):
            raise PatchError("trailing backslash in smali string")
        e = s[i]
        i += 1
        table = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
                 "\\": "\\", '"': '"', "'": "'"}
        if e == "u":
            out.append(chr(int(s[i:i + 4], 16)))
            i += 4
        elif e in table:
            out.append(table[e])
        else:
            raise PatchError("unsupported smali escape: \\%s" % e)
    return "".join(out)


CONST_RE = re.compile(
    r'^(?P<indent>\s*)const-string(?:/jumbo)?\s+(?P<reg>[vp]\d+),\s*"(?P<body>(?:[^"\\]|\\.)*)"\s*$')


def parse_const_string(line):
    m = CONST_RE.match(line)
    if not m:
        return None
    return m.group("reg"), smali_unescape(m.group("body"))


INVOKE_RE = re.compile(
    r'^\s*invoke-\w+(?:/range)?\s*\{(?P<regs>[^}]*)\},\s*(?P<ref>\S+)\s*$')


def parse_invoke(line):
    m = INVOKE_RE.match(line)
    if not m:
        return None
    regs = [r.strip() for r in m.group("regs").split(",") if r.strip()]
    if ".." in m.group("regs"):
        base, last = [r.strip() for r in m.group("regs").split("..")]
        n0, n1 = int(base[1:]), int(last[1:])
        regs = ["v%d" % n for n in range(n0, n1 + 1)]
    return regs, m.group("ref")


def sha256_text(s):
    """Hash logical Unicode text, joining UTF-16 surrogate pairs from smali escapes."""
    normalized = s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def method_bounds(lines, name, proto):
    start = None
    for i, line in enumerate(lines):
        if line.startswith(".method"):
            sig = line.split(None, 1)[1].strip()
            if sig.endswith(name + proto):
                if start is not None:
                    raise PatchError("duplicate method %s%s" % (name, proto))
                start = i
        elif start is not None and line.strip() == ".end method":
            return start, i
    raise PatchError("method %s%s not found" % (name, proto))


def param_slots(proto, is_static):
    params = proto[1:proto.index(")")]
    slots = 0 if is_static else 1
    i = 0
    while i < len(params):
        c = params[i]
        if c == "L":
            i = params.index(";", i) + 1
            slots += 1
        elif c == "[":
            # Arrays are one reference slot, whatever the element type.
            while params[i] == "[":
                i += 1
            if params[i] == "L":
                i = params.index(";", i) + 1
            else:
                i += 1
            slots += 1
        elif c in "JD":
            slots += 2
            i += 1
        else:
            slots += 1
            i += 1
    return slots


def bump_locals(lines, mstart, extra, proto, is_static):
    """Raise the method's local register window; returns first temp vN."""
    for i in range(mstart, len(lines)):
        s = lines[i].strip()
        if s.startswith(".locals"):
            n = int(s.split()[1])
            lines[i] = lines[i].replace(".locals %d" % n, ".locals %d" % (n + extra))
            return n
        if s.startswith(".registers"):
            n = int(s.split()[1])
            locals_n = n - param_slots(proto, is_static)
            lines[i] = lines[i][:len(lines[i]) - len(lines[i].lstrip())] \
                + ".locals %d" % (locals_n + extra)
            return locals_n
        if s.startswith(".end method"):
            break
    raise PatchError("no .locals/.registers in method")


# ------------------------------------------------------------------ profiling

class Profile:
    def __init__(self, path):
        with open(path, encoding="utf-8") as fh:
            self.raw = json.load(fh)
        self.methods = self.raw["methods"]
        self.fingerprints = self.raw["fingerprints"]
        self.anchors = self.raw["anchors"]
        self.ext_class = self.raw.get("extension_class", EXT_DEFAULT)

    def method(self, key):
        m = self.methods[key]
        return m["class"], m["name"], m["signature"]


# ------------------------------------------------------------------- analysis

def disassemble_all(apk, workdir, baksmali_jar):
    dexes = []
    with zipfile.ZipFile(apk) as zf:
        names = [n for n in zf.namelist()
                 if re.fullmatch(r"classes(\d*)\.dex", n)]
        names.sort(key=lambda n: (len(n), n))
        for name in names:
            out = os.path.join(workdir, name.replace(".dex", ""))
            data = zf.read(name)
            tmp = os.path.join(workdir, name)
            with open(tmp, "wb") as fh:
                fh.write(data)
            run(smali_prefix("baksmali", baksmali_jar) + ["d", tmp, "-o", out])
            dexes.append((name, out))
    if not dexes:
        raise PatchError("no classes*.dex entries in %s" % apk)
    return dexes


def scan_fingerprints(dexes, fingerprints):
    """Locate every const-string whose sha256 matches a known fingerprint."""
    hits = {k: [] for k in fingerprints}
    for dex_name, ddir in dexes:
        for root, _dirs, files in os.walk(ddir):
            for fn in files:
                if not fn.endswith(".smali"):
                    continue
                path = os.path.join(root, fn)
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
                mstart = None
                msig = None
                for idx, line in enumerate(lines):
                    if line.startswith(".method"):
                        mstart = idx
                        msig = line.split(None, 1)[1].strip()
                    elif line.strip() == ".end method":
                        mstart = None
                    parsed = parse_const_string(line)
                    if not parsed:
                        continue
                    _reg, value = parsed
                    digest = sha256_text(value)
                    for key, fp in fingerprints.items():
                        if digest == fp["sha256"]:
                            hits[key].append({
                                "dex": dex_name, "file": path, "line": idx + 1,
                                "method": msig, "method_line": mstart,
                            })
    return hits


def _method_header_parts(header):
    """Return (method_name, proto) from a smali .method header."""
    if not header.startswith(".method") or "(" not in header:
        return None
    tail = header.split(None, 1)[1].strip()
    left, right = tail.split("(", 1)
    name = left.split()[-1]
    return name, "(" + right


def _iter_smali_methods(lines):
    """Yield (start, end, name, proto) for all methods in a smali file."""
    i = 0
    while i < len(lines):
        if not lines[i].startswith(".method"):
            i += 1
            continue
        parts = _method_header_parts(lines[i])
        if parts is None:
            i += 1
            continue
        name, proto = parts
        end = i + 1
        while end < len(lines) and lines[end].strip() != ".end method":
            end += 1
        if end >= len(lines):
            raise PatchError("unterminated method %s%s" % (name, proto))
        yield i, end, name, proto
        i = end + 1


def _write_register(line):
    """Best-effort destination register for instructions that overwrite one."""
    m = re.match(
        r"^\s*(?:const(?:-[\w/-]+)?|move(?:-[\w/-]+)?|move-result(?:-[\w/-]+)?|check-cast|new-instance|new-array|iget(?:-[\w/-]+)?|sget(?:-[\w/-]+)?)\s+([vp]\d+)\b",
        line)
    return m.group(1) if m else None


def find_hinglish_rule_site(lines, mstart, mend, placeholder, begins):
    """Find the stock rule by a stable opening fragment and prove it's used
    as the third argument when replacing {HINGLISH_OVERRIDE_RULE}.

    This is a structural compatibility check, not proof that arbitrary APKs
    have compatible control flow. It intentionally refuses ambiguous sites.
    """
    rules = []
    placeholders = []
    for i in range(mstart, mend + 1):
        parsed = parse_const_string(lines[i])
        if not parsed:
            continue
        reg, value = parsed
        if value.startswith(begins):
            rules.append((i, reg, value))
        if value == placeholder:
            placeholders.append((i, reg))
    candidates = []
    for rule_idx, rule_reg, _value in rules:
        for ph_idx, ph_reg in placeholders:
            if ph_reg == rule_reg:
                continue
            for invoke_idx in range(max(rule_idx, ph_idx) + 1, mend + 1):
                inv = parse_invoke(lines[invoke_idx])
                if not inv:
                    continue
                regs, ref = inv
                if ref != STRING_REPLACE or len(regs) != 3:
                    continue
                if regs[1] != ph_reg or regs[2] != rule_reg:
                    continue
                # Reject a reused register if another instruction overwrote it
                # after its matching const-string and before this replace.
                clobbered = False
                for j in range(min(rule_idx, ph_idx) + 1, invoke_idx):
                    dest = _write_register(lines[j])
                    if dest == rule_reg and j != rule_idx:
                        clobbered = True
                    if dest == ph_reg and j != ph_idx:
                        clobbered = True
                if not clobbered:
                    candidates.append((rule_idx, rule_reg))
                break
    unique = sorted(set(candidates))
    if len(unique) != 1:
        if not unique:
            raise PatchError("adaptive mode could not identify the Hinglish rule + placeholder replacement structurally")
        raise PatchError("adaptive mode found multiple plausible Hinglish rule sites; refusing ambiguous patch")
    return unique[0]


def discover_adaptive_builder(dexes, profile):
    """Discover compatible cleanup-builder methods without class/method hashes.

    Requires the enabled-languages placeholder -> String.replace dataflow and
    a recognisable Hinglish override string used to replace its placeholder.
    The exact prompt/rule hashes are advisory in adaptive mode; instruction
    structure remains a mandatory safety gate.
    """
    enabled_placeholder = profile.anchors["enabled_languages_placeholder"]
    hinglish_placeholder = profile.anchors["hinglish_placeholder"]
    begins = profile.fingerprints["hinglish_override_rule"].get(
        "begins", "3. Hinglish Override")
    found = []
    for dex_name, ddir in dexes:
        for root, _dirs, files in os.walk(ddir):
            for fn in files:
                if not fn.endswith(".smali"):
                    continue
                path = os.path.join(root, fn)
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
                cls = None
                for line in lines:
                    if line.startswith(".class"):
                        parts = line.split()
                        if parts:
                            cls = parts[-1]
                        break
                if not cls:
                    continue
                for start, end, name, proto in _iter_smali_methods(lines):
                    method_lines = lines[start:end + 1]
                    # Already-patched methods aren't candidates.
                    if any(profile.ext_class in line for line in method_lines):
                        continue
                    has_enabled = any(
                        (p := parse_const_string(line)) is not None and p[1] == enabled_placeholder
                        for line in method_lines)
                    if not has_enabled:
                        continue
                    try:
                        _langs_reg, _result_reg, replace_end = find_enabled_languages_site(
                            lines, start, end, enabled_placeholder)
                        rule_idx, _rule_reg = find_hinglish_rule_site(
                            lines, start, end, hinglish_placeholder, begins)
                    except PatchError:
                        continue
                    if rule_idx <= replace_end:
                        continue
                    header = lines[start]
                    is_static = bool(re.search(r"\bstatic\b", header))
                    found.append({
                        "dex": dex_name, "file": path, "class": cls,
                        "name": name, "signature": proto,
                        "is_static": is_static, "start": start, "end": end,
                    })
    if len(found) != 1:
        if not found:
            raise PatchError(
                "adaptive mode found no compatible cleanup builder. This APK may use a different prompt model or placeholder flow; no patch was written.")
        details = ["%s->%s%s (%s)" % (x["class"], x["name"], x["signature"], x["dex"])
                   for x in found]
        raise PatchError("adaptive mode found multiple candidate builders; refusing ambiguous patch: " + "; ".join(details))
    return found[0]


def apply_adaptive_target(profile, target):
    """Use the discovered builder anchor for downstream existing patch helpers."""
    profile.methods["cleanup_builder"] = {
        "class": target["class"], "name": target["name"],
        "signature": target["signature"], "dex": target["dex"],
        "evidence": "discovered by structural placeholders and String.replace dataflow",
    }


def analyze(apk, profile, workdir, baksmali_jar, adaptive=False):
    dexes = disassemble_all(apk, workdir, baksmali_jar)
    adaptive_target = None
    if adaptive:
        adaptive_target = discover_adaptive_builder(dexes, profile)
        apply_adaptive_target(profile, adaptive_target)
    hits = scan_fingerprints(dexes, profile.fingerprints)
    report = {"apk": apk, "dexes": [d for d, _ in dexes],
              "fingerprint_hits": hits, "methods": {},
              "mode": "adaptive" if adaptive else "profiled"}
    if adaptive_target:
        report["adaptive_target"] = adaptive_target
    for key in profile.methods:
        cls, name, proto = profile.method(key)
        found = None
        for dex_name, ddir in dexes:
            path = os.path.join(ddir, smali_class_path(cls))
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            try:
                start, end = method_bounds(lines, name, proto)
            except PatchError:
                continue
            consts = []
            for i in range(start, end + 1):
                p = parse_const_string(lines[i])
                if p:
                    consts.append({"line": i + 1, "reg": p[0],
                                   "sha256": sha256_text(p[1]),
                                   "preview": p[1][:60]})
            found = {"dex": dex_name, "file": path,
                     "lines": [start + 1, end + 1], "const_strings": consts}
            break
        report["methods"][key] = found or {"error": "not found"}
    return report


# ------------------------------------------------------------------- patching

def find_enabled_languages_site(lines, mstart, mend, placeholder):
    """Locate .replace("{ENABLED_LANGUAGES}", langs); returns (langs_reg,
    result_reg, insert_after_index)."""
    const_idx = None
    const_reg = None
    for i in range(mstart, mend + 1):
        p = parse_const_string(lines[i])
        if p and p[1] == placeholder:
            const_idx, const_reg = i, p[0]
            break
    if const_idx is None:
        raise PatchError("placeholder const-string %r not found in method" % placeholder)
    for i in range(const_idx + 1, mend + 1):
        inv = parse_invoke(lines[i])
        if not inv:
            continue
        regs, ref = inv
        if ref == STRING_REPLACE and len(regs) == 3 and regs[1] == const_reg:
            langs_reg = regs[2]
            for j in range(i + 1, min(i + 4, mend + 1)):
                m = re.match(r"^\s*move-result-object\s+([vp]\d+)\s*$", lines[j])
                if m:
                    return langs_reg, m.group(1), j
            raise PatchError("no move-result-object after String.replace")
    raise PatchError("no String.replace using %s after placeholder load" % const_reg)


def hook_snippet(temp0, src_reg, langs_reg, ext, hook):
    return [
        "    move-object/from16 %s, %s" % (temp0, src_reg),
        "    move-object/from16 %s, %s" % ("v%d" % (int(temp0[1:]) + 1), langs_reg),
        "    invoke-static/range {%s .. %s}, %s->%s"
        "(Ljava/lang/String;Ljava/lang/String;)Ljava/lang/String;" % (
            temp0, "v%d" % (int(temp0[1:]) + 1), ext, hook),
        "    move-result-object %s" % temp0,
        "    move-object/from16 %s, %s" % (src_reg, temp0),
    ]


def patch_method(lines, profile, fingerprints_ok, adaptive=False):
    cls, name, proto = profile.method("cleanup_builder")
    is_static = " static " in (" " + lines[0])  # caller passes method lines only
    mstart, mend = method_bounds(lines, name, proto)
    header = lines[mstart]
    is_static = bool(re.search(r"\bstatic\b", header))
    body = lines[mstart:mend + 1]
    ext = profile.ext_class
    if any(ext in l for l in body):
        raise PatchError("method already references the extension runtime "
                         "(already patched?)")

    placeholder = profile.anchors["enabled_languages_placeholder"]
    langs_reg, result_reg, replace_end = find_enabled_languages_site(
        lines, mstart, mend, placeholder)

    if adaptive:
        rule_idx, rule_reg = find_hinglish_rule_site(
            lines, mstart, mend, profile.anchors["hinglish_placeholder"],
            profile.fingerprints["hinglish_override_rule"].get(
                "begins", "3. Hinglish Override"))
    else:
        rule_fp = profile.fingerprints["hinglish_override_rule"]["sha256"]
        rule_idx = None
        rule_reg = None
        for i in range(mstart, mend + 1):
            p = parse_const_string(lines[i])
            if p and sha256_text(p[1]) == rule_fp:
                rule_idx, rule_reg = i, p[0]
                break
        if rule_idx is None:
            raise PatchError("stock Hinglish override const-string not found in "
                             "cleanup-builder method (fingerprint %s)" % rule_fp)
    if rule_idx < replace_end:
        raise PatchError("stock override rule loads BEFORE the "
                         "{ENABLED_LANGUAGES} substitution; this build's "
                         "control flow needs a different anchor order. "
                         "Aborting fail-closed; run analyze and adjust the "
                         "profile.")

    # Three temps: one dedicated save register for the enabled-languages
    # value (captured immediately after the String.replace so a later
    # const-string cannot clobber it), plus a contiguous invoke pair.
    base = bump_locals(lines, mstart, 3, proto, is_static)
    if base + 2 > 255:
        raise PatchError("method uses too many registers for 8-bit temps")
    saved_langs = "v%d" % base
    temp0 = "v%d" % (base + 1)

    # Insert Hook B first (later line index), then Hook A, so indices stay
    # valid. Hook B reads the SAVED languages register, not the (possibly
    # clobbered) original.
    snippet_b = hook_snippet(temp0, rule_reg, saved_langs, ext, RULE_HOOK)
    lines[rule_idx + 1:rule_idx + 1] = snippet_b
    snippet_a = (["    move-object/from16 %s, %s" % (saved_langs, langs_reg)]
                 + hook_snippet(temp0, result_reg, langs_reg, ext, PROMPT_HOOK))
    lines[replace_end + 1:replace_end + 1] = snippet_a
    return lines


def verify_prompt_owner(lines_by_class, profile):
    cls, name, proto = profile.method("prompt_owner")
    path_lines = lines_by_class.get(cls)
    if path_lines is None:
        raise PatchError("prompt owner class %s not found" % cls)
    mstart, mend = method_bounds(path_lines, name, proto)
    fp = profile.fingerprints["lite_cleanup_prompt"]["sha256"]
    for i in range(mstart, mend + 1):
        p = parse_const_string(path_lines[i])
        if p and sha256_text(p[1]) == fp:
            return True
    raise PatchError("Lite cleanup prompt fingerprint %s not found in %s->%s"
                     % (fp, cls, name))


def patch(apk, out_apk, profile, workdir, baksmali_jar, smali_jar,
          extension_dex=None, adaptive=False):
    dexes = disassemble_all(apk, workdir, baksmali_jar)

    # Exact hashes are mandatory in the stable profile mode. Adaptive mode
    # treats them as diagnostics and requires unambiguous structural anchors.
    hits = scan_fingerprints(dexes, profile.fingerprints)
    if adaptive:
        discovered = discover_adaptive_builder(dexes, profile)
        apply_adaptive_target(profile, discovered)
    else:
        for key, fp in profile.fingerprints.items():
            if not hits[key]:
                raise PatchError("fingerprint %s (%s) not found anywhere in the "
                                 "APK; refusing to patch an unrecognized build"
                                 % (fp["sha256"], key))

    # 2. Locate the cleanup-builder method's dex.
    cls, name, proto = profile.method("cleanup_builder")
    target = None
    for dex_name, ddir in dexes:
        path = os.path.join(ddir, smali_class_path(cls))
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            try:
                method_bounds(lines, name, proto)
            except PatchError:
                continue
            target = (dex_name, ddir, path, lines)
            break
    if target is None:
        raise PatchError("cleanup builder %s->%s%s not found" % (cls, name, proto))

    dex_name, ddir, path, lines = target

    # 3. In profile mode, require the exact prompt owner. Adaptive mode
    # deliberately does not require the whole prompt hash; it instead requires
    # the builder's placeholder/replacement structure and unique target.
    if not adaptive:
        pcls, pname, pproto = profile.method("prompt_owner")
        plines = None
        for _dn2, dd2 in dexes:
            ppath = os.path.join(dd2, smali_class_path(pcls))
            if os.path.exists(ppath):
                with open(ppath, encoding="utf-8") as fh:
                    cand = fh.read().splitlines()
                try:
                    method_bounds(cand, pname, pproto)
                    plines = cand
                except PatchError:
                    continue
        verify_prompt_owner({pcls: plines} if plines else {}, profile)

    # 4. Rewrite the cleanup builder.
    new_lines = patch_method(lines, profile, hits, adaptive=adaptive)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(new_lines) + "\n")

    # 5. Reassemble only the touched dex.
    new_dex = os.path.join(workdir, "patched-" + dex_name)
    run(smali_prefix("smali", smali_jar) + ["a", ddir, "-o", new_dex])

    # 6. Repack: copy every entry, swap the patched dex, append extension dex.
    with zipfile.ZipFile(apk) as zin, \
            zipfile.ZipFile(out_apk, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == dex_name:
                with open(new_dex, "rb") as fh:
                    zout.writestr(item.filename, fh.read())
            else:
                zout.writestr(item, zin.read(item.filename))
        if extension_dex:
            existing = [d for d, _ in dexes]
            nxt = "classes%d.dex" % (len(existing) + 1)
            with open(extension_dex, "rb") as fh:
                zout.writestr(nxt, fh.read())
    return {"patched_dex": dex_name, "out": out_apk,
            "extension_dex": extension_dex}


# ----------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["analyze", "patch"])
    ap.add_argument("--apk", required=True)
    ap.add_argument("--out")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--workdir", default="/tmp/dex-patch-work")
    ap.add_argument("--baksmali-jar", default=os.environ.get(
        "BAKSMALI_JAR", "tools/baksmali-2.5.2.jar"))
    ap.add_argument("--smali-jar", default=os.environ.get(
        "SMALI_JAR", "tools/smali-2.5.2.jar"))
    ap.add_argument("--extension-dex")
    ap.add_argument("--adaptive", action="store_true",
                    help="discover a uniquely matching cleanup builder by placeholders/dataflow; hashes are advisory")
    args = ap.parse_args(argv)

    profile = Profile(args.profile)
    if os.path.isdir(args.workdir):
        shutil.rmtree(args.workdir)
    os.makedirs(args.workdir)

    try:
        if args.command == "analyze":
            report = analyze(args.apk, profile, args.workdir,
                             args.baksmali_jar, adaptive=args.adaptive)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        if not args.out:
            ap.error("patch requires --out")
        result = patch(args.apk, args.out, profile, args.workdir,
                       args.baksmali_jar, args.smali_jar,
                       args.extension_dex, adaptive=args.adaptive)
        print(json.dumps(result, indent=2))
        return 0
    except PatchError as exc:
        print("FAIL-CLOSED ABORT: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
