#!/usr/bin/env python3
"""Detect a new prerelease (RC) of a PyPI package and apply it to recipe/recipe.yaml.

Queries the PyPI JSON API, finds the newest PRErelease version (rcN/aN/bN/devN),
and if it is strictly newer than the recipe's current context.version, rewrites
the version, resets context.build_number to 0, and rewrites the sdist sha256
in place -- line-anchored, never a file-wide regex, so jinja templating
elsewhere in the recipe is untouched.

Handles two recipe layouts (numba vs llvmlite differ):
  * sha256 as a context: key   (numba:    `sha256:` under context:, source
    references it via ${{ sha256 }})
  * sha256 inline under source: (llvmlite: `sha256:` on the line right after
    the PyPI `url:`)
It patches whichever form(s) are present and requires at least one to succeed.

Prerelease-only: a final/stable PyPI release is ignored (those flow to `main`
via conda-forge's autotick-bot); this tracks only the rc branch. Because a
final compares GREATER than its prereleases under PEP 440, the strict
old->new comparison also guarantees we never downgrade a manually-bumped
final back to an rc.
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from packaging.version import parse as parse_version, InvalidVersion

PYPI_JSON = "https://pypi.org/pypi/{pkg}/json"


def fetch_json(pkg):
    with urllib.request.urlopen(PYPI_JSON.format(pkg=pkg), timeout=30) as resp:
        return json.load(resp)


def latest_prerelease(data):
    """Newest prerelease that ships an (un-yanked) sdist.

    Returns (version_str, sdist_sha256, sdist_url) or None.
    """
    best = None
    best_ver = None
    for ver_str, files in data["releases"].items():
        try:
            v = parse_version(ver_str)
        except InvalidVersion:
            continue
        if not v.is_prerelease:
            continue
        sdist = next(
            (
                f
                for f in files
                if f.get("packagetype") == "sdist" and not f.get("yanked", False)
            ),
            None,
        )
        if sdist is None:
            continue
        if best_ver is None or v > best_ver:
            best_ver = v
            best = (ver_str, sdist["digests"]["sha256"], sdist["url"])
    return best


def _context_lines(lines):
    """Yield (index, line) for lines inside the top-level `context:` block."""
    in_context = False
    for i, line in enumerate(lines):
        if re.match(r"^context:\s*$", line):
            in_context = True
            continue
        if in_context and re.match(r"^\S", line):
            return
        if in_context:
            yield i, line


def read_current_version(lines):
    for _, line in _context_lines(lines):
        m = re.match(r'^\s*version:\s*"?([^"\s]+)"?\s*$', line)
        if m:
            return m.group(1)
    return None


def patch_context_scalar(lines, key, new_value):
    """Patch `key: <value>` inside context:, preserving any quote style. """
    pattern = re.compile(rf'^(\s*{key}:\s*)(")?([^"\s]*)(")?(\s*)$')
    for i, line in _context_lines(lines):
        m = pattern.match(line)
        if m:
            open_q = m.group(2) or ""
            close_q = m.group(4) or ""
            lines[i] = f"{m.group(1)}{open_q}{new_value}{close_q}{m.group(5)}"
            return True
    return False


def patch_inline_sha256(lines, url_substrings, new_sha256):
    """Patch the 64-hex sha256 on the line right after the first matching
    source `url:` line. Non-destructive: returns False if the following line
    is not a bare sha256 (e.g. numba's `sha256: ${{ sha256 }}` jinja ref)."""
    for i, line in enumerate(lines):
        if "url:" in line and all(s in line for s in url_substrings):
            if i + 1 >= len(lines):
                return False
            m = re.match(r'^(\s*sha256:\s*)"?[0-9a-f]{64}"?(\s*)$', lines[i + 1])
            if not m:
                return False
            lines[i + 1] = f"{m.group(1)}{new_sha256}{m.group(2)}"
            return True
    return False


def _emit(kv):
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with open(gh_out, "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def main():
    args = sys.argv[1:]
    if not args or "--package" not in args:
        print("usage: bump_rc.py <path/to/recipe.yaml> --package <pypi_name>", file=sys.stderr)
        return 2
    recipe_path = Path(args[0])
    pkg = args[args.index("--package") + 1]

    text = recipe_path.read_text()
    lines = text.splitlines(keepends=True)

    old_version = read_current_version(lines)
    if old_version is None:
        print("error: could not find version in context: block", file=sys.stderr)
        return 2

    data = fetch_json(pkg)
    target = latest_prerelease(data)
    if target is None:
        print(f"no prerelease with an sdist found on PyPI for {pkg}")
        _emit({"changed": "false"})
        return 0
    new_version, sha256, tarball_url = target

    changed = parse_version(new_version) > parse_version(old_version)
    print(f"package:           {pkg}")
    print(f"current:           {old_version}")
    print(f"latest prerelease: {new_version}")
    print(f"changed:           {changed}")

    if not changed:
        _emit({"changed": "false"})
        return 0

    if not patch_context_scalar(lines, "version", new_version):
        print("error: failed to patch version in context: block", file=sys.stderr)
        return 1

    reset_build = patch_context_scalar(lines, "build_number", "0")
    if reset_build:
        print("reset build_number to 0 (version changed)")
    else:
        print("warning: no build_number key in context: block to reset", file=sys.stderr)

    patched_ctx = patch_context_scalar(lines, "sha256", sha256)
    patched_inline = patch_inline_sha256(
        lines, ["pypi.org/packages/source", ".tar.gz"], sha256
    )
    if not (patched_ctx or patched_inline):
        print("error: failed to patch sha256 (neither context key nor inline source)", file=sys.stderr)
        return 1

    recipe_path.write_text("".join(lines))
    print(f"updated {recipe_path}: {old_version} -> {new_version}")
    _emit(
        {
            "changed": "true",
            "old_version": old_version,
            "new_version": new_version,
            "sha256": sha256,
            "tarball_url": tarball_url,
            "build_number_reset": "true" if reset_build else "false",
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
