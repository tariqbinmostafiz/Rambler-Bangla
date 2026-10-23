# User guide

This guide walks through applying the Rambler Bangla patch to a Gboard APK you
obtain yourself, checking the result, and installing it on your own device. It
is based on the scripts in this repository: `scripts/bootstrap_tools.sh`,
`standalone-patcher/dex_patch.py`, `scripts/verify_apk.sh` and
`rename/rename_apk.py`.

This repository distributes no APKs. A patched keyboard contains Google's
proprietary Gboard code, which is not licensed for redistribution. Keep the
APKs you build for your own use, and never upload them, your signing key or
diagnostic logs to this repository. See [`LICENSING-AUDIT.md`](LICENSING-AUDIT.md).

## Contents

- [Prerequisites](#prerequisites)
- [1. Set up the toolchain](#1-set-up-the-toolchain)
- [2. Build the policy runtime](#2-build-the-policy-runtime)
- [3. Verify the input APK](#3-verify-the-input-apk)
- [4. Patch](#4-patch)
- [5. Verify the output](#5-verify-the-output)
- [6. Rename, sign and install on your own device](#6-rename-sign-and-install-on-your-own-device)
- [Layouts and settings](#layouts-and-settings)
- [Troubleshooting](#troubleshooting)

## Prerequisites

- Linux x86_64. The pinned aapt2 is the Linux build.
- A JDK, 17 or newer. CI uses Temurin 21. `JAVA_HOME` is honoured if set.
- Python 3 (3.11 is tested in CI) and `pip`.
- `curl`, `unzip`, `zip` and `sha256sum`.
- androguard 4.1.4, used by `verify_apk.sh` to parse APKs:
  `python3 -m pip install androguard==4.1.4`.
- A Gboard APK that matches the profile in
  [`fingerprints/gboard-18.3.1.json`](../fingerprints/gboard-18.3.1.json):

  | Field | Value |
  | --- | --- |
  | Package | `com.google.android.inputmethod.latin` |
  | Version name | `18.3.1.977415014 beta` |
  | Version code | `176004238` |
  | ABI | `arm64-v8a` |

  Any other build is refused by design. You must supply the APK yourself.

## 1. Set up the toolchain

```bash
git clone https://github.com/muhib-karim/Rambler-Bangla.git
cd Rambler-Bangla
bash scripts/bootstrap_tools.sh
source tools/tool-env.sh
```

`bootstrap_tools.sh` downloads smali, baksmali and dexlib2 2.5.2 with their
dependencies, r8/d8 8.3.37, aapt2 8.3.0, apksig 8.3.0 and android.jar 4.1.1.4
from Maven Central and Google's Maven repository. Every file is checked against
[`tools-src/toolchain.sha256`](../tools-src/toolchain.sha256); a mismatch stops
the script. It then builds the small signer/verifier from
[`tools-src/MiniApkSigner.java`](../tools-src/MiniApkSigner.java) and writes
`tools/tool-env.sh`.

Set `TOOLS_DIR` before running it to put the toolchain somewhere other than
`./tools`. Always `source tools/tool-env.sh` in each new shell: it sets
`SMALI_CP`, which the patcher uses to run smali and baksmali.

## 2. Build the policy runtime

The patch inserts calls into `GboardRamblerLiteScriptRuntime`, so the patched
APK needs that class as an extra DEX. Compile it and convert it with d8, the
same way [`fixtures/build_fixture.sh`](../fixtures/build_fixture.sh) does:

```bash
mkdir -p build/ext-classes build/ext-dex
javac -d build/ext-classes \
  extension-src/com/akshaykadam/pixelboard/extension/ramblerlite/GboardRamblerLiteScriptRuntime.java
java -cp tools/r8-8.3.37.jar com.android.tools.r8.D8 --min-api 24 \
  --output build/ext-dex build/ext-classes/com/akshaykadam/pixelboard/extension/ramblerlite/*.class
```

The result is `build/ext-dex/classes.dex`. `.class` and `.dex` files are
ignored by git.

## 3. Verify the input APK

Run the analyzer first. It disassembles every DEX, looks for each SHA-256
fingerprint in the profile and checks the two method anchors, then prints a
JSON report. It does not write an APK.

```bash
python3 standalone-patcher/dex_patch.py analyze \
  --apk /path/to/gboard-18.3.1.apk \
  --profile fingerprints/gboard-18.3.1.json
```

For a human-readable summary, use the verifier with the profile:

```bash
scripts/verify_apk.sh single /path/to/gboard-18.3.1.apk \
  --profile fingerprints/gboard-18.3.1.json
```

Every `fingerprint` line should say `FOUND` and every `method` line should say
`found in`. If anything says `MISSING` or `NOT FOUND`, this is not the profiled
build and patching will refuse it.

## 4. Patch

```bash
python3 standalone-patcher/dex_patch.py patch \
  --apk /path/to/gboard-18.3.1.apk \
  --out /path/to/rambler-unsigned.apk \
  --profile fingerprints/gboard-18.3.1.json \
  --extension-dex build/ext-dex/classes.dex
```

The patcher:

1. sweeps the whole APK for every profile fingerprint,
2. finds the cleanup-builder method named in the profile,
3. confirms the prompt-owner method holds the expected prompt fingerprint,
4. traces the enabled-languages register by dataflow and inserts the two hooks,
5. reassembles only the touched DEX and appends the runtime DEX as the next
   `classesN.dex`.

On success it prints a short JSON summary naming the patched DEX and the output
path. The output is **unsigned** and will not install until you sign it.

### Experimental adaptive mode (compatible revisions only)

If a newer Gboard/PixelBoard build has changed class names, method names, and
prompt/rule hashes but kept the same placeholder/dataflow structure, try
`--adaptive`. This mode locates one unambiguous cleanup builder by requiring
both placeholders, a `String.replace` flow for `{ENABLED_LANGUAGES}`, and a
recognizable `3. Hinglish Override` rule used for the second replacement.
It treats exact SHA-256 fingerprints as diagnostics rather than hard gates and
does not require the old prompt-owner method/hash. It still aborts if no unique
structural candidate can be proved. The runtime's prompt rewrite also requires
its known SCRIPT GATE text, so a successful build does not prove that every
new prompt model will be rewritten at runtime.

```bash
python3 standalone-patcher/dex_patch.py analyze \
  --apk /path/to/base.apk \
  --profile fingerprints/gboard-18.3.1.json \
  --adaptive

python3 standalone-patcher/dex_patch.py patch \
  --apk /path/to/base.apk \
  --out /path/to/rambler-unsigned.apk \
  --profile fingerprints/gboard-18.3.1.json \
  --extension-dex build/ext-dex/classes.dex \
  --adaptive
```

This mode is intended for Gboard/PixelBoard-compatible builds that preserve the
same runtime contract. It cannot safely patch arbitrary keyboard apps or a
model with a different request structure. Always run analyzer first and test
the signed output on a spare install before replacing your working keyboard.

Options:

| Option | Purpose |
| --- | --- |
| `--workdir DIR` | Scratch directory for the disassembly. It is **deleted and recreated** on every run, so point it at a dedicated empty directory. |
| `--baksmali-jar`, `--smali-jar` | Jar paths used only when `SMALI_CP` is not set. Defaults come from `BAKSMALI_JAR` / `SMALI_JAR` or `tools/`. |
| `--extension-dex FILE` | Runtime DEX to append. Without it the hooks point at a class that is not in the APK. |

## 5. Verify the output

```bash
# The patched APK on its own (zip integrity, package, versions, DEX count,
# signature state, and the profile check)
scripts/verify_apk.sh single /path/to/rambler-unsigned.apk \
  --profile fingerprints/gboard-18.3.1.json

# Stock against patched: package, versionCode, versionName and signers
scripts/verify_apk.sh compare /path/to/gboard-18.3.1.apk /path/to/rambler-unsigned.apk
```

Expect one more DEX entry than the stock APK. After signing, `compare` reports a
different signer; that is expected when the build is re-signed with a non-Google
key.

## 6. Rename, sign and install on your own device

### Rename (optional, for side-by-side installs)

The maintainer builds use their own package id so they install next to the
stock keyboard. The rename tooling and its verification record are in
[`SIDE-BY-SIDE.md`](SIDE-BY-SIDE.md). The driver takes:

```bash
python3 rename/rename_apk.py \
  --in-apk  <input.apk> \
  --out-apk <renamed-unsigned.apk> \
  --new-package <your.package.id> \
  --new-label "Rambler Bangla" \
  --workdir <scratch-dir> --smali-cp "$SMALI_CP" \
  --report rename-report.json
```

The upstream builds also bypass signature checks (see `SIDE-BY-SIDE.md`); this
repository's rename tooling documents what it rewrites and what it deliberately
leaves alone.

### Sign with your own key

The patcher never signs, and no key is stored in this repository. Use your own
keystore. `MiniApkSigner`, built by `bootstrap_tools.sh`, signs with APK
Signature Scheme v1 and v2:

```bash
java -cp "$TOOLS_DIR/apksig-8.3.0.jar:$TOOLS_DIR/classes" MiniApkSigner sign \
  <keystore> <storepass> <alias> <keypass> <in-unsigned.apk> <out-signed.apk>

java -cp "$TOOLS_DIR/apksig-8.3.0.jar:$TOOLS_DIR/classes" MiniApkSigner verify <out-signed.apk>
```

Keep the same key for every build you make. Android only installs an update over
an existing app when the signer matches; with a different key you must uninstall
first (this happened between V29 and V29.3, see
[`ledger/v29.3-provenance.md`](../ledger/v29.3-provenance.md)).

### Install

Install the signed APK on your own device with your usual method, for example
`adb install -r <out-signed.apk>`. Then enable the keyboard in Android's
keyboard settings and select it as the input method.

## Layouts and settings

Rambler's behavior depends on the active Gboard layout. Design intent for the
V29.3 test build, not re-verified on a current device:

| Layout | Voice and typing result |
| --- | --- |
| English (US) | English only. Rambler changes nothing. |
| বাংলা (native) | Bangla only. Romanized Bangla and English words become Bangla script. |
| abc → বাংলা | Multilingual. Each sentence is Bangla script or English, decided per sentence. |
| Bangla (Latin) | Treated as Bangla: output is Bangla script. |

Gboard's combined "EN · BN" keyboard reports itself as English (US). For mixed
dictation, use abc → বাংলা.

Two switches live in the keyboard settings, in the Rambler section:

- **Bangla script correction** (on by default). Turn it off to get Gboard's
  own output unchanged, for voice and typing. Useful for telling whether an odd
  result comes from Rambler or from Gboard.
- **Rambler diagnostics** (off by default). Turn it on, reproduce a problem,
  then turn it off. A log named `RamblerBangla-diag-<date>-<time>.txt` is saved
  to Downloads. It records the active layout and the text before and after each
  conversion, and stays on your phone unless you share it. Read it before
  sharing.

## Troubleshooting

### Exit codes

| Tool | Code | Meaning |
| --- | --- | --- |
| `dex_patch.py` | `0` | `analyze` printed its report, or `patch` wrote the output APK. |
| `dex_patch.py` | `2` | Fail-closed abort (`FAIL-CLOSED ABORT: ...` on stderr). No output APK is written. Also returned for invalid arguments, for example `patch` without `--out`. |
| `dex_patch.py` | `1` | Uncaught Python error with a traceback, for example a missing or malformed profile JSON, or an `--apk` path that is not a readable zip. Errors before the final repack write no output APK; an error during the repack (for example a mistyped `--extension-dex` path) can leave a partial `--out` file, so delete it and run again. |
| `verify_apk.sh` | `1` | `single`: the APK failed the zip integrity check (`zip CORRUPT`). |
| `verify_apk.sh` | `2` | Unknown subcommand; the usage line is printed. |
| `bootstrap_tools.sh` | non-zero | A download failed or a file did not match `tools-src/toolchain.sha256`. |

### Common fail-closed messages

| Message (after `FAIL-CLOSED ABORT:`) | What it means |
| --- | --- |
| `fingerprint ... not found anywhere in the APK; refusing to patch an unrecognized build` | The APK is not the profiled Gboard 18.3.1 build. Check version name, version code and ABI. |
| `cleanup builder ... not found` / `prompt owner class ... not found` | A method anchor from the profile is missing. Same cause: a different build. |
| `Lite cleanup prompt fingerprint ... not found in ...` | The prompt-owner method exists but does not hold the expected prompt. |
| `method already references the extension runtime (already patched?)` | You passed an APK that was already patched. Start from the stock APK. |
| `placeholder const-string ... not found in method` / `no String.replace using ...` | The dataflow check failed. The method body differs from what the profile expects. |
| `stock override rule loads BEFORE the {ENABLED_LANGUAGES} substitution ...` | Control flow differs from the profiled build; the patcher refuses rather than guess. |
| `command failed: ...` | smali or baksmali failed. Check that you ran `source tools/tool-env.sh` and that a JDK is on `PATH` or in `JAVA_HOME`. |

### Other problems

- **`ModuleNotFoundError: androguard` or `loguru`** from `verify_apk.sh`:
  install androguard 4.1.4 (`python3 -m pip install androguard==4.1.4`).
- **`(apksig frontend not built; skipping signature verification)`**: run
  `scripts/bootstrap_tools.sh`, or set `TOOLS_DIR` to where you ran it.
- **Install fails with a signature conflict**: the installed app was signed
  with a different key. Uninstall it first, or sign with the original key.
- **Odd conversion on the phone**: turn off Bangla script correction and
  repeat. If the problem remains, it comes from Gboard. If not, open a
  [bug report](https://github.com/muhib-karim/Rambler-Bangla/issues/new/choose)
  with the layout, what you said and what came out. Do not attach APKs,
  keystores or personal data.
