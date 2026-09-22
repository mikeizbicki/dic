#!/usr/bin/env python3
"""git-apply-fuzzy - apply a unified diff with fuzzy matching.

A drop-in replacement for `git apply` for when the model wrote context
lines that are *almost* right.  `git apply` needs the whole hunk to match
exactly, so one paraphrased word anywhere in the context kills it; this
tries, in order:

    1. an exact match,
    2. a match ignoring trailing whitespace,
    3. a difflib similarity match at or above --threshold (default 0.8).

Level 3 survives a drifted context line, and it is the only level that
can be *wrong*: at 0.8, one line in five may differ.  The window is the
one nearest the hunk's own line number, which is what a human reading
the diff would pick.

When the match is fuzzy, only the lines the hunk actually changed are
rewritten -- each context line keeps the file's own version of itself,
so a fuzzy match cannot "correct" a line of the file into the model's
guess at it.

    fuzzy-apply                     # .git/.geni-patchfile
    fuzzy-apply --dry-run -v
    fuzzy-apply -t 0.9 patch.diff
"""
import argparse, difflib, os, re, subprocess, sys

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_RE = re.compile(r"^diff --git ")


def note(message):
    """One diagnostic line on stderr; git apply is silent, this is not."""
    sys.stderr.write(message + "\n")


def stage(path):
    """Stage path with `git add`, the way `git apply --index` does.

    Run after the write, so a failure here only means the user has to
    stage the file by hand; the file on disk is already correct.
    """
    result = subprocess.run(["git", "add", "-A", "--", path],
                            capture_output=True, text=True)
    if result.returncode != 0:
        note(f"{path}: git add failed: {result.stderr.strip()}")
        return False
    return True


def _path(text):
    """A ---/+++ path, or None for /dev/null."""
    text = text.strip()
    return None if text == "/dev/null" else text


def strip_path(path, n):
    """Remove n leading path components, as `git apply -p` does.

    >>> strip_path("a/dic/tty.py", 1), strip_path("a/b/c", 2)
    ('dic/tty.py', 'c')
    """
    return "/".join(path.split("/")[n:]) if path else path


def parse(text):
    """A unified diff as a list of {old, new, hunks}.

    Anything before the first `diff --git` (geni's commit message) is
    ignored, exactly as `git apply` ignores it.  A hunk is its old-file
    start line and a list of (tag, line) pairs, the line keeping its
    trailing newline and the tag being ' ', '-' or '+'.

    >>> parse("diff --git a/x b/x\\n--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-a\\n+b\\n")
    [{'old': 'a/x', 'new': 'b/x', 'hunks': [(1, [('-', 'a\\n'), ('+', 'b\\n')])]}]
    """
    files = []
    for chunk in re.split(r"(?m)^(?=diff --git )", text):
        lines = chunk.splitlines(keepends=True)
        if not lines or not DIFF_RE.match(lines[0]):
            continue
        entry = {"old": None, "new": None, "hunks": []}
        i = 1
        while i < len(lines) and not HUNK_RE.match(lines[i]):
            if lines[i].startswith("--- "):
                entry["old"] = _path(lines[i][4:])
            elif lines[i].startswith("+++ "):
                entry["new"] = _path(lines[i][4:])
            i += 1
        while i < len(lines):
            match = HUNK_RE.match(lines[i])
            if not match:
                i += 1
                continue
            start = int(match.group(1))
            i += 1
            body = []
            while i < len(lines) and not HUNK_RE.match(lines[i]):
                line = lines[i]
                if line[:1] == "\\":              # \ No newline at end of file
                    i += 1
                    continue
                if line[:1] in (" ", "-", "+"):
                    body.append((line[0], line[1:]))
                elif line == "\n":                # a bare blank context line
                    body.append((" ", "\n"))
                else:
                    break
                i += 1
            entry["hunks"].append((start, body))
        files.append(entry)
    return files


def sides(hunk):
    """The old side (context and '-') and new side (context and '+') of a hunk.

    >>> sides([(' ', 'c\\n'), ('-', 'old\\n'), ('+', 'new\\n')])
    (['c\\n', 'old\\n'], ['c\\n', 'new\\n'])
    """
    return ([line for tag, line in hunk if tag in (" ", "-")],
            [line for tag, line in hunk if tag in (" ", "+")])


def locate(lines, search, hint, threshold):
    """The best window in lines matching search, as (start, ratio).

    Exact first, then ignoring trailing whitespace, then difflib; every
    level prefers the window nearest the hunk's own line number, so a
    block that occurs twice lands where the diff meant it to.  Returns
    (None, 0.0) when nothing reaches the threshold.

    >>> locate(["a\\n", "b\\n", "c\\n"], ["b\\n"], 0, 0.8)
    (1, 1.0)
    >>> locate(["a\\n", "b\\n", "c\\n"], ["b\\n", "x\\n"], 0, 0.8)
    (None, 0.0)
    """
    size = len(search)
    if size == 0:
        return max(0, min(hint, len(lines))), 1.0
    if size > len(lines):
        return None, 0.0
    count = len(lines) - size + 1
    hint = max(0, min(hint, count - 1))
    order = sorted(range(count), key=lambda s: abs(s - hint))
    for start in order:
        if lines[start:start + size] == search:
            return start, 1.0
    bare = [line.rstrip() for line in search]
    for start in order:
        if [line.rstrip() for line in lines[start:start + size]] == bare:
            return start, 1.0
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(search)
    best, best_ratio = None, 0.0
    for start in order:
        matcher.set_seq1(lines[start:start + size])
        if matcher.quick_ratio() < threshold:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best, best_ratio = start, ratio
    return (best, best_ratio) if best_ratio >= threshold else (None, 0.0)


def splice(lines, at, hunk):
    """Apply one hunk's body to lines at position at, keeping the file's context.

    A clean match is a plain slice assignment.  Otherwise the hunk's old
    side and the file's window have the same length by construction, so
    each context line can keep the file's own version of itself and only
    the lines the hunk actually changed are rewritten: a fuzzy match must
    never "correct" a line of the file into the model's guess at it.

    >>> lines = ["ctx\\n", "old\\n", "ctx\\n"]
    >>> splice(lines, 0, [
    ...     (' ', 'ctx\\n'), ('-', 'old\\n'), ('+', 'new\\n'), (' ', 'ctx\\n')])
    >>> lines
    ['ctx\\n', 'new\\n', 'ctx\\n']
    """
    old = [line for tag, line in hunk if tag in (" ", "-")]
    if lines[at:at + len(old)] == old:
        lines[at:at + len(old)] = [line for tag, line in hunk if tag in (" ", "+")]
        return
    result, i = [], 0
    for tag, line in hunk:
        if tag == " ":
            result.append(lines[at + i])
            i += 1
        elif tag == "-":
            i += 1
        else:
            result.append(line)
    lines[at:at + len(old)] = result


def apply_hunks(lines, hunks, threshold):
    """Apply hunks bottom-up; return (lines, matches, failures).

    Bottom-up so that one application does not shift the line numbers the
    next (earlier) hunk is still being located against.  matches is
    (hunk number, line applied at, ratio); failures is (hunk number, the
    line the diff expected to find).

    >>> apply_hunks(["a\\n", "b\\n"], [(1, [('-', 'a\\n'), ('+', 'z\\n')])], 0.8)
    (['z\\n', 'b\\n'], [(1, 1, 1.0)], [])
    >>> apply_hunks(["a\\n"], [(1, [('-', 'q\\n'), ('+', 'z\\n')])], 0.8)
    (['a\\n'], [], [(1, 1)])
    """
    placed, matches, failures = [], [], []
    for index, (start, hunk) in enumerate(hunks, 1):
        old, _ = sides(hunk)
        at, ratio = locate(lines, old, start - 1, threshold)
        if at is None:
            failures.append((index, start))
        else:
            matches.append((index, at + 1, ratio))
            placed.append((at, hunk))
    for at, hunk in sorted(placed, key=lambda p: -p[0]):
        splice(lines, at, hunk)
    return lines, matches, failures


def main():
    parser = argparse.ArgumentParser(
        prog="fuzzy-apply",
        description="Apply a unified diff, matching context fuzzily.")
    parser.add_argument("patch", nargs="?", default=".git/.geni-patchfile",
                        help="the patch to apply (default: %(default)s)")
    parser.add_argument("-t", "--threshold", type=float, default=0.8, metavar="R",
                        help="minimum difflib ratio for a fuzzy match"
                             " (default: %(default)s)")
    parser.add_argument("-p", "--strip", type=int, default=1, metavar="N",
                        help="strip N leading path components (default: %(default)s)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="report what would happen without writing")
    parser.add_argument("-f", "--force", action="store_true",
                        help="write the hunks that applied even if others did not")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="report every hunk's match ratio")
    args = parser.parse_args()

    try:
        with open(args.patch) as handle:
            text = handle.read()
    except OSError as error:
        sys.exit(f"fuzzy-apply: {args.patch}: {error}")

    files = parse(text)
    if not files:
        sys.exit(f"fuzzy-apply: {args.patch}: no diff found")

    failed = False
    applied = []                            # paths written, staged at the end
    for entry in files:
        old = strip_path(entry["old"], args.strip)
        new = strip_path(entry["new"], args.strip)
        path = new or old

        if old is None:                     # a new file: every hunk is an addition
            body = [line for _, hunk in entry["hunks"]
                         for tag, line in hunk if tag == "+"]
            try:
                if not args.dry_run:
                    with open(path, "w") as handle:
                        handle.writelines(body)
                note(f"{path}: created ({len(body)} lines)")
                applied.append(path)
            except OSError as error:
                note(f"{path}: {error}")
                failed = True
            continue

        if new is None:                     # a deletion: every hunk is a removal
            try:
                if not args.dry_run:
                    os.remove(path)
                note(f"{path}: deleted")
                applied.append(path)
            except OSError as error:
                note(f"{path}: {error}")
                failed = True
            continue

        try:
            with open(path) as handle:
                lines = handle.readlines()
        except OSError as error:
            note(f"{path}: {error}")
            failed = True
            continue

        lines, matches, failures = apply_hunks(lines, entry["hunks"], args.threshold)
        if args.verbose:
            for index, line, ratio in matches:
                note(f"  hunk {index}: matched at line {line} (ratio {ratio:.2f})")
        for index, line in failures:
            note(f"  hunk {index}: no match at or above {args.threshold:.2f}"
                 f" near line {line}")
        if failures:
            failed = True
            if not args.force:
                note(f"{path}: {len(failures)}/{len(entry['hunks'])} hunks did not match")
                continue
        try:
            if not args.dry_run:
                with open(path, "w") as handle:
                    handle.writelines(lines)
        except OSError as error:
            note(f"{path}: {error}")
            failed = True
            continue
        note(f"{path}: {len(matches)}/{len(entry['hunks'])} hunks applied")
        applied.append(path)

    if not args.dry_run:
        for path in applied:
            if not stage(path):
                failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
