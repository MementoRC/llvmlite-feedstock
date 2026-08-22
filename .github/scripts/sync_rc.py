#!/usr/bin/env python3
"""Sync conda-forge:main into the rc branch while preserving rc identity.

Companion to bump_rc.py. Where bump_rc.py tracks new prereleases on PyPI and
bumps rc's version, this keeps rc's *framework* -- build script, pins, CI
scaffolding -- in step with conda-forge's main branch, while preserving the
handful of fields that make rc the rc branch.

Two subcommands, driven by .github/workflows/rc-sync.yml:

  capture   Read rc's recipe.yaml BEFORE the merge and record identity fields
            (version, sha256, build_number) plus any --preserve blocks
            (default source.patches) to a JSON file.

            Blocks matter because recipe.yaml is resolved per-hunk "theirs":
            rc-only content such as its patches list lives INSIDE those hunks
            and would otherwise be silently reverted to main's on every sync.
  resolve   After `git merge --no-commit upstream/main` has left conflicts,
            resolve the known-safe set and report anything else as unresolved.

Resolution table:

  <recipe>                        theirs, then re-inject captured identity
  --theirs paths (default         theirs, PER HUNK. README.md is regenerated
  README.md,conda-forge.yml)      by a rerender anyway; conda-forge.yml is a
                                  rerender INPUT, so it must be merged rather
                                  than taken whole-file.
  --regenerated globs             theirs, WHOLE FILE (conda-smithy output;
                                  the rerender overwrites it immediately)
  --ours paths (default           ours   (channel_targets: conda-forge
  recipe/conda_build_config.yaml) llvmlite_rc is rc's entire purpose)
  anything else                   left conflicted and reported

Conflicts are resolved PER HUNK, not per file. `git checkout --theirs <path>`
would take their whole file and silently discard hunks that auto-merged
cleanly -- which would drop rc-only recipe fixes. Content outside conflict
markers is preserved verbatim.
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# bump_rc.py lives beside this file; reuse its tested recipe-editing helpers
# rather than maintaining a second implementation.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bump_rc import (  # noqa: E402
    patch_context_scalar,
    patch_inline_sha256,
    read_current_version,
    _context_lines,
    _emit,
)

DEFAULT_RECIPE = "recipe/recipe.yaml"
DEFAULT_THEIRS = "README.md,conda-forge.yml"
DEFAULT_OURS = "recipe/conda_build_config.yaml"
DEFAULT_REGENERATED = ".ci_support/*,.azure-pipelines/*,azure-pipelines.yml,.scripts/*,.github/workflows/conda-build.yml,pixi.toml,.gitattributes,.gitignore,LICENSE.txt,README.md"
DEFAULT_PRESERVE = "source.patches"

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


def matches(path, patterns):
    """Exact match or fnmatch glob. fnmatch's `*` spans `/`, which is what we
    want for prefixes like `.ci_support/*`."""
    return any(path == p or fnmatch.fnmatch(path, p) for p in patterns)


def _find_block(lines, dotted):
    """Locate a nested block by dotted key path, e.g. "source.patches".

    Returns (start, end) as a half-open line range covering the key line and
    every following line indented deeper than it, or None if not found.
    Line-based on purpose: see the module docstring.
    """
    keys = dotted.split(".")
    lo, hi = 0, len(lines)
    indent = -1
    start = None
    for key in keys:
        start = None
        for i in range(lo, hi):
            line = lines[i]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cur = len(line) - len(line.lstrip())
            m = re.match(r"^(\s*)" + re.escape(key) + r":", line)
            if m and len(m.group(1)) > indent:
                start = i
                indent = len(m.group(1))
                break
        if start is None:
            return None
        end = hi
        for j in range(start + 1, hi):
            line = lines[j]
            if not line.strip():
                continue
            cur = len(line) - len(line.lstrip())
            if cur <= indent:
                end = j
                break
        else:
            end = hi
        lo, hi = start + 1, end
    return (start, end)


def replace_block(lines, dotted, block):
    """Replace the block at `dotted` with `block` (a list of raw lines).

    Returns True on success. If the key is absent but its PARENT exists, the
    block is inserted at the end of the parent instead -- upstream dropping the
    key entirely must not silently discard rc's version of it.
    """
    span = _find_block(lines, dotted)
    if span is not None:
        lines[span[0]:span[1]] = block
        return True
    parent = dotted.rsplit(".", 1)[0]
    if parent == dotted:
        return False
    pspan = _find_block(lines, parent)
    if pspan is None:
        return False
    lines[pspan[1]:pspan[1]] = block
    return True


def take_whole_file(path, side):
    """Resolve a conflict by taking one side's WHOLE file.

    Used for conda-smithy generated files, where per-hunk resolution is
    pointless because a rerender overwrites the file wholesale. Also the only
    way to handle modify/delete conflicts: `git checkout --<side>` fails when
    that side deleted the file, so fall back to removing it.

    Returns True if the file was REMOVED rather than checked out, so the
    caller knows not to `git add` a path that no longer exists.
    """
    try:
        git("checkout", f"--{side}", "--", path)
        git("add", "--", path)
        return False
    except subprocess.CalledProcessError:
        git("rm", "-f", "--", path)
        return True


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

    # build_number is looked up in the context: block ONLY, so capture and
    # patch_context_scalar agree on scope. A build_number declared elsewhere
    # would otherwise be captured but un-patchable, firing the resolve-time
    # guard spuriously.
    build_number = None
    for _, line in _context_lines(lines):
        m = re.match(r'^\s*build_number:\s*"?([0-9]+)"?\s*$', line)
        if m:
            build_number = m.group(1)
            break

    sha256 = None
    for line in lines:
        m = re.match(r'^\s*sha256:\s*"?([0-9a-f]{64})"?\s*$', line)
        if m:
            sha256 = m.group(1)
            break

    preserve = [p.strip() for p in args.preserve.split(",") if p.strip()]
    blocks = {}
    for dotted in preserve:
        span = _find_block(lines, dotted)
        if span is not None:
            blocks[dotted] = lines[span[0]:span[1]]

    identity = {
        "version": version,
        "sha256": sha256,
        "build_number": build_number,
        "blocks": blocks,
    }
    Path(args.out).write_text(json.dumps(identity, indent=2))
    summary = {k: v for k, v in identity.items() if k != "blocks"}
    summary["blocks"] = {k: len(v) for k, v in blocks.items()}
    print(f"captured rc identity: {summary}")
    return 0


def cmd_resolve(args):
    identity = json.loads(Path(args.identity).read_text())
    recipe_path = args.recipe
    take_theirs = [p.strip() for p in args.theirs.split(",") if p.strip()]
    take_ours = [p.strip() for p in args.ours.split(",") if p.strip()]
    regenerated = [p.strip() for p in args.regenerated.split(",") if p.strip()]

    conflicts = conflicted_paths()
    resolved, unresolved, removed = [], [], []

    for path in conflicts:
        # Generated files first: a rerender overwrites them, so take upstream's
        # whole file rather than merging hunks nobody will read.
        if path != recipe_path and matches(path, regenerated):
            if take_whole_file(path, "theirs"):
                removed.append(path)
            resolved.append(path)
            continue
        if path == recipe_path or matches(path, take_theirs):
            side = "theirs"
        elif matches(path, take_ours):
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
        failed = []

        if identity.get("version"):
            if patch_context_scalar(lines, "version", identity["version"]):
                notes.append("version")
            else:
                failed.append("version")

        if identity.get("build_number"):
            if patch_context_scalar(lines, "build_number", identity["build_number"]):
                notes.append("build_number")
            else:
                failed.append("build_number")

        if identity.get("sha256"):
            # numba carries sha256 as a context: key, llvmlite inline under
            # source:. Exactly one applies per feedstock, so require one hit.
            ctx = patch_context_scalar(lines, "sha256", identity["sha256"])
            inline = patch_inline_sha256(
                lines, ["pypi.org/packages/source", ".tar.gz"], identity["sha256"]
            )
            if ctx:
                notes.append("sha256(context)")
            if inline:
                notes.append("sha256(inline)")
            if not (ctx or inline):
                failed.append("sha256")

        if template_distinfo_glob(lines, args.package):
            notes.append("dist-info glob")

        for dotted, block in (identity.get("blocks") or {}).items():
            if replace_block(lines, dotted, block):
                n = sum(1 for line in block if re.match(r"^\s*-\s+\S+\.patch\s*$", line))
                notes.append(f"{dotted}({n})" if n else dotted)
            else:
                failed.append(dotted)

        if failed:
            # Fail loudly rather than shipping main's identity onto rc: the
            # result would be a buildable recipe publishing the wrong version
            # under the rc label. Bail BEFORE writing the file.
            print(
                "error: captured rc identity could not be re-injected: "
                + ", ".join(failed)
                + " -- refusing to continue",
                file=sys.stderr,
            )
            return 1

        recipe.write_text("".join(lines))
        print(f"re-injected rc identity: {', '.join(notes) or 'nothing to do'}")
        if recipe_path not in resolved:
            resolved.append(recipe_path)

    for path in resolved:
        if path in removed:
            # `git rm` already staged the deletion. `git add` on a path that
            # exists in neither the worktree nor the index is a fatal
            # pathspec error, and git add has no --ignore-unmatch.
            continue
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
    p_cap.add_argument("--preserve", default=DEFAULT_PRESERVE)
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
    p_res.add_argument(
        "--regenerated",
        default=DEFAULT_REGENERATED,
        help="comma-separated globs for conda-smithy generated files: resolved "
        "whole-file to upstream, then overwritten by the rerender",
    )
    p_res.set_defaults(func=cmd_resolve)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
