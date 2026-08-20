#!/usr/bin/env python3
"""Sync conda-forge:main into the rc branch while preserving rc identity.

Companion to bump_rc.py. Where bump_rc.py tracks new prereleases on PyPI and
bumps rc's version, this keeps rc's *framework* -- build script, pins, CI
scaffolding -- in step with conda-forge's main branch, while preserving the
handful of fields that make rc the rc branch.

Two subcommands, driven by .github/workflows/rc-sync.yml:

  capture   Read rc's recipe.yaml BEFORE the merge and record identity fields
            (version, sha256, build_number) to a JSON file.
  resolve   After `git merge --no-commit upstream/main` has left conflicts,
            resolve the known-safe set and report anything else as unresolved.

Resolution table:

  <recipe>                        theirs, then re-inject captured identity
  --theirs paths (default         theirs (conda-smithy generated; a rerender
  README.md)                      re-injects the rc channel label)
  --ours paths (default           ours   (channel_targets: conda-forge
  recipe/conda_build_config.yaml) llvmlite_rc is rc's entire purpose)
  anything else                   left conflicted and reported

Conflicts are resolved PER HUNK, not per file. `git checkout --theirs <path>`
would take their whole file and silently discard hunks that auto-merged
cleanly -- which would drop rc-only recipe fixes. Content outside conflict
markers is preserved verbatim.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# bump_rc.py lives beside this file; reuse its tested recipe-editing helpers
# rather than maintaining a second implementation.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bump_rc import patch_context_scalar, patch_inline_sha256, read_current_version, _emit  # noqa: E402

DEFAULT_RECIPE = "recipe/recipe.yaml"
DEFAULT_THEIRS = "README.md"
DEFAULT_OURS = "recipe/conda_build_config.yaml"

CONFLICT_START = re.compile(r"^<{7}(?: |$)")
CONFLICT_BASE = re.compile(r"^\|{7}(?: |$)")
CONFLICT_MID = re.compile(r"^={7}(?: |$)")
CONFLICT_END = re.compile(r"^>{7}(?: |$)")


def git(*args):
    """Run a git command, returning stdout. Raises on non-zero exit."""
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def conflicted_paths():
    out = git("diff", "--name-only", "--diff-filter=U")
    return [p for p in out.splitlines() if p.strip()]


def resolve_hunks(text, side):
    """Keep one side of every conflict hunk; preserve everything else.

    `side` is "ours" or "theirs". Handles both the default and diff3/zdiff3
    conflict styles -- the ||||||| base section is always dropped.
    """
    out = []
    state = "clean"
    for line in text.splitlines(keepends=True):
        if CONFLICT_START.match(line):
            state = "ours"
            continue
        if state != "clean" and CONFLICT_BASE.match(line):
            state = "base"
            continue
        if state != "clean" and CONFLICT_MID.match(line):
            state = "theirs"
            continue
        if state != "clean" and CONFLICT_END.match(line):
            state = "clean"
            continue
        if state == "clean" or state == side:
            out.append(line)
    return "".join(out)


def template_distinfo_glob(lines, package):
    """Rewrite a hardcoded dist-info glob back to the version template.

    conda-forge's main branch has shipped `llvmlite-0.49.0.dist-info/*` before.
    On rc that breaks the moment bump_rc.py moves the version, so any literal
    version in the glob is restored to ${{ version }}.
    """
    pattern = re.compile(
        r"(" + re.escape(package) + r"-)(?!\$\{\{)[0-9][^/\s]*?(\.dist-info)"
    )
    changed = False
    for i, line in enumerate(lines):
        new = pattern.sub(r"\1${{ version }}\2", line)
        if new != line:
            lines[i] = new
            changed = True
    return changed


def cmd_capture(args):
    lines = Path(args.recipe).read_text().splitlines(keepends=True)
    version = read_current_version(lines)
    if version is None:
        print("error: could not find version in context: block", file=sys.stderr)
        return 2

    sha256 = None
    build_number = None
    for line in lines:
        m = re.match(r'^\s*sha256:\s*"?([0-9a-f]{64})"?\s*$', line)
        if m and sha256 is None:
            sha256 = m.group(1)
        m = re.match(r'^\s*build_number:\s*"?([0-9]+)"?\s*$', line)
        if m and build_number is None:
            build_number = m.group(1)

    identity = {"version": version, "sha256": sha256, "build_number": build_number}
    Path(args.out).write_text(json.dumps(identity, indent=2))
    print(f"captured rc identity: {identity}")
    return 0


def cmd_resolve(args):
    identity = json.loads(Path(args.identity).read_text())
    recipe_path = args.recipe
    take_theirs = {p for p in args.theirs.split(",") if p.strip()}
    take_ours = {p for p in args.ours.split(",") if p.strip()}

    conflicts = conflicted_paths()
    resolved, unresolved = [], []

    for path in conflicts:
        if path == recipe_path or path in take_theirs:
            side = "theirs"
        elif path in take_ours:
            side = "ours"
        else:
            unresolved.append(path)
            continue
        target = Path(path)
        target.write_text(resolve_hunks(target.read_text(), side))
        resolved.append(path)

    # Re-inject rc identity unconditionally -- the recipe may have merged
    # cleanly while still carrying main's version, and these fields are what
    # make this the rc branch.
    recipe = Path(recipe_path)
    if recipe.exists():
        lines = recipe.read_text().splitlines(keepends=True)
        notes = []
        if identity.get("version") and patch_context_scalar(
            lines, "version", identity["version"]
        ):
            notes.append("version")
        if identity.get("build_number") and patch_context_scalar(
            lines, "build_number", identity["build_number"]
        ):
            notes.append("build_number")
        if identity.get("sha256"):
            # numba carries sha256 as a context: key, llvmlite inline under
            # source:. Try both; at least one applies per feedstock.
            if patch_context_scalar(lines, "sha256", identity["sha256"]):
                notes.append("sha256(context)")
            if patch_inline_sha256(
                lines, ["pypi.org/packages/source", ".tar.gz"], identity["sha256"]
            ):
                notes.append("sha256(inline)")
        if template_distinfo_glob(lines, args.package):
            notes.append("dist-info glob")
        recipe.write_text("".join(lines))
        print(f"re-injected rc identity: {', '.join(notes) or 'nothing to do'}")
        if recipe_path not in resolved:
            resolved.append(recipe_path)

    for path in resolved:
        git("add", "--", path)

    if resolved:
        print("resolved: " + " ".join(resolved))
    if unresolved:
        print("UNRESOLVED: " + " ".join(unresolved), file=sys.stderr)

    _emit(
        {
            "conflicted": "true" if unresolved else "false",
            "unresolved": " ".join(unresolved),
            "resolved": " ".join(resolved),
        }
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_cap = sub.add_parser("capture", help="record rc identity before merging")
    p_cap.add_argument("--recipe", default=DEFAULT_RECIPE)
    p_cap.add_argument("--out", required=True)
    p_cap.set_defaults(func=cmd_capture)

    p_res = sub.add_parser("resolve", help="resolve conflicts after merging")
    p_res.add_argument("--recipe", default=DEFAULT_RECIPE)
    p_res.add_argument("--identity", required=True)
    p_res.add_argument("--package", required=True)
    p_res.add_argument(
        "--theirs",
        default=DEFAULT_THEIRS,
        help="comma-separated paths to resolve in favour of upstream main",
    )
    p_res.add_argument(
        "--ours",
        default=DEFAULT_OURS,
        help="comma-separated paths to resolve in favour of rc",
    )
    p_res.set_defaults(func=cmd_resolve)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
