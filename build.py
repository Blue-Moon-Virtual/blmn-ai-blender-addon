#!/usr/bin/env python3
"""Build the installable add-on zip: dist/blmn-ai-blender-addon.zip

The archive contains the `blmn_ai/` package folder at its root, so Blender
installs it as the module `blmn_ai` (Install from Disk) and the bundled CGCookie
updater can replace it in place. This is the exact binary attached to GitHub
releases and served from blmn.ai/blender.

Pure stdlib (zipfile) so it runs identically on Windows, macOS, Linux and CI
with no `zip` binary required.
"""
import ast
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = "blmn_ai"
OUT_DIR = os.path.join(ROOT, "dist")
OUT = os.path.join(OUT_DIR, "blmn-ai-blender-addon.zip")

EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_SUFFIXES = (".pyc",)


def bl_info_version():
    src = open(os.path.join(ROOT, PKG, "__init__.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "bl_info" for t in node.targets
        ):
            return ".".join(map(str, ast.literal_eval(node.value)["version"]))
    raise SystemExit("error: bl_info version not found in blmn_ai/__init__.py")


def main():
    pkg_dir = os.path.join(ROOT, PKG)
    if not os.path.isfile(os.path.join(pkg_dir, "__init__.py")):
        raise SystemExit(f"error: {PKG}/__init__.py not found — run from the repo root")

    version = bl_info_version()
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(OUT):
        os.remove(OUT)

    count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(pkg_dir):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS and not d.endswith("_updater"))
            for name in sorted(filenames):
                if name.endswith(EXCLUDE_SUFFIXES):
                    continue
                abspath = os.path.join(dirpath, name)
                # Archive path keeps the blmn_ai/ prefix (relative to repo root).
                arcname = os.path.relpath(abspath, ROOT).replace(os.sep, "/")
                zf.write(abspath, arcname)
                count += 1

    size_kb = round(os.path.getsize(OUT) / 1024)
    print(f"Built {os.path.relpath(OUT, ROOT)} ({size_kb} KB, {count} files) — v{version}")


if __name__ == "__main__":
    main()
