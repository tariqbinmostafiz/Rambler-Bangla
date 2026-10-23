# Adaptive patcher update

This working copy adds an **experimental `--adaptive` mode** to `standalone-patcher/dex_patch.py`.

## What changed

- Default behavior remains the original strict fingerprint-locked profile behavior.
- `--adaptive` discovers exactly one cleanup-builder method across DEX files by structural anchors, instead of hard-coding the class, method name, or signature from Gboard 18.3.1.
- The adaptive finder requires `{ENABLED_LANGUAGES}` to flow into a `String.replace` call, and a recognizable `3. Hinglish Override` string to be used as the replacement value for `{HINGLISH_OVERRIDE_RULE}`. Register clobbering and ambiguous/multiple candidate methods are rejected.
- Exact text hashes and the old prompt-owner method are advisory/not hard gates only in adaptive mode.
- UTF-16 surrogate pairs parsed from smali are normalized before UTF-8 hashing to avoid UnicodeEncodeError.
- The Python unit suite includes synthetic revision tests for renamed methods/classes and modified prompt/rule hashes.

## Run

```bash
python3 -m unittest discover -s tests/python -v

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

## Important limits

This is **not** a promise to patch arbitrary apps. The target has to preserve the same request-builder model: both placeholders and the replacement/dataflow pattern must exist, and exactly one candidate must be found. Even if a patch builds, the Java runtime's prompt rewrite still requires its known SCRIPT GATE block, so behavior in a newer prompt model must be verified on-device. The full smali/Dex/APK integration suite requires the pinned toolchain (`bash scripts/bootstrap_tools.sh`); only the Python unit suite was available to run in this environment.
