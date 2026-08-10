"""
checks.py -- the gate set, as data, with one command that runs it.

WHY THIS EXISTS
    There is no test framework here by design: a gate is a module CLI that
    exits non-zero. That makes the gate set the entire test suite, and until
    now the set existed in exactly one place -- an English sentence in
    `CLAUDE.md` -- with no command that ran it and nothing that enforced it.

    The sentence had drifted in both directions. It named a `+7 h` fixture
    gate that is not a gate (it is one case inside `ingest.clockcheck --check`)
    and a "study validation" gate that is not runnable, while omitting
    `exporter.py --check` and `annotations.py --check`, which between them are
    84% of all the coverage this repo has. Prose cannot be run, so nothing
    noticed.

    The set is therefore a table. Adding a gate is a row, not a sentence, and
    `--list` answers "what does this repo check?" without reading any prose.

WHY SUBPROCESSES, AND NOT ONE PROCESS
    Each gate keeps its own CLI and its own `--check`; this module only spawns
    them and names the set. Folding their bodies in here would couple five
    independent checks into one interpreter, and `importgate.py` specifically
    requires a fresh interpreter per case -- see its own docstring -- which a
    shared process destroys.

    Children are spawned with `sys.executable`, so running this under
    `.venv\\Scripts\\python.exe` runs every gate under the venv too.

MUST-FAIL IS A FIRST-CLASS EXPECTATION
    A gate that is supposed to go red is not a footnote. A row declares
    `expect="fail"`, and a must-fail row that passes is reported red and exits
    non-zero, exactly like a must-pass row that fails. This is the discipline
    `importgate.py` already applies to its two control cases, lifted to the
    set.

THE SELF-TEST IS THE POINT
    A runner that silently does nothing prints all-green forever, and every
    row underneath it becomes a vacuous pass -- the precise failure
    `importgate.py` documents having shipped once already. So `--check` always
    runs the runner's own control first, over a synthetic table that includes
    a deliberately-failing row and a must-fail row that wrongly passes. If the
    runner cannot report red and exit non-zero on those, nothing else in the
    report can be believed.

    It is not behind an opt-in flag, because a control nobody types is not a
    control.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass
class Gate:
    """One row of the set: what it defends, how to run it, what to expect."""

    name: str
    defends: str
    argv: tuple[str, ...]
    expect: str = "pass"            # "pass" or "fail"
    # Gates that cannot run on a bare box. Declared rather than omitted, and
    # reported as SKIP rather than dropped -- an invisible omission is how the
    # documented set drifted in the first place.
    needs: str = ""
    timeout_s: int = 600


# ---------------------------------------------------------------------------
# The set. Counts in the comments are the 2026-08-10 baseline, recorded so a
# silent drop is visible in the diff as well as in the report.
# ---------------------------------------------------------------------------

GATES = [
    Gate("clock check",
         "air temperature peaks near solar noon at the stated longitude, so a "
         "timestamp that lost its zone is caught; includes the +7 h corrupted "
         "fixture, which must stay red",
         ("-m", "ingest.clockcheck", "--check")),                       # 9

    Gate("archive union",
         "merging study snapshots unions overlapping rows instead of summing "
         "them, and a second rebuild changes nothing",
         ("archive.py", "--check")),                                    # 15

    Gate("import gate",
         "the headless pull-and-export path never acquires tkinter or "
         "matplotlib, and `view` still imports without claiming to work",
         ("importgate.py", "--check")),                                 # 10

    Gate("workbook exporter",
         "the exported workbook's sheets, charts, units and provenance say "
         "what the study actually holds, across a DST transition",
         ("exporter.py", "--check")),                                   # 98

    Gate("annotation store",
         "marks round-trip, carry their zone designator, and a malformed set "
         "is rejected with a message that names the problem",
         ("annotations.py", "--check")),                                # 74

    # Declared, not run by default. Established 2026-08-10: `view.py --check`
    # calls `tk.Tk()` unconditionally (view.py:3210) and loads the LATEST REAL
    # STUDY off disk from `../studies/`, so it needs both a display and machine
    # state that no clone of this repo has. It is a real gate -- 254 checks --
    # and it is a human's gate, not an agent's.
    Gate("view window",
         "the chart window is viewable rather than merely constructed, its "
         "axis is tz-aware across a DST transition, and its z-scores match "
         "the workbook's",
         ("view.py", "--check"),
         needs="a display and a study in ../studies/"),                 # 254
]

# The floor. Every commit must leave the runner green AND must not reduce
# coverage: a slice that lowers the count is reporting a mistake, not tidying.
# Raise this deliberately when a slice adds checks; never lower it to go green.
FLOOR = 206


# ---------------------------------------------------------------------------
# Running one row
# ---------------------------------------------------------------------------

_COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+(?:checks|gates)\s+passed")


def parse_count(text: str) -> tuple[int, int] | None:
    """The trailing `N/M checks passed` line, if the gate printed one.

    Every gate here ends with one, but this is read rather than required: a
    gate's exit code is the verdict, and the count is evidence about coverage.
    A gate that stops printing it loses its contribution to the floor, which
    shows up as the floor failing -- not as a silent zero.
    """
    found = _COUNT_RE.findall(text)
    if not found:
        return None
    passed, total = found[-1]
    return int(passed), int(total)


@dataclass
class Result:
    gate: Gate
    returncode: int | None          # None == it never finished
    output: str
    count: tuple[int, int] | None = None
    ok: bool = False
    note: str = ""
    skipped: bool = False
    lines: list[str] = field(default_factory=list)


def target_of(gate: Gate) -> Path:
    """The file a row claims to run, so a typo in the table is catchable."""
    if gate.argv[0] == "-m":
        return ROOT.joinpath(*gate.argv[1].split(".")).with_suffix(".py")
    return ROOT / gate.argv[0]


def run_gate(gate: Gate) -> Result:
    """Spawn one gate and judge it against its declared expectation."""
    # Children print typographic quotes and degree signs. A Windows console
    # defaults to cp1252, which cannot encode them, and a gate that crashes on
    # its way to printing PASS reads as a real failure. The children that know
    # this reconfigure their own stdout; telling every child to encode UTF-8
    # covers the ones that do not, and decoding with `replace` means a stray
    # byte damages one character rather than the whole verdict.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run([sys.executable, *gate.argv], cwd=ROOT,
                              capture_output=True, encoding="utf-8",
                              errors="replace", env=env,
                              timeout=gate.timeout_s)
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or "") + (exc.stderr or "")
        res = Result(gate, None, text)
        res.ok = False
        res.note = f"timed out after {gate.timeout_s}s"
        res.lines = text.splitlines()
        return res
    except OSError as exc:
        res = Result(gate, None, str(exc))
        res.ok = False
        res.note = f"could not be started: {exc}"
        res.lines = [str(exc)]
        return res

    text = (proc.stdout or "") + (proc.stderr or "")
    res = Result(gate, proc.returncode, text)
    res.count = parse_count(proc.stdout or "")
    res.lines = text.splitlines()

    went_green = proc.returncode == 0
    if gate.expect == "fail":
        res.ok = not went_green
        res.note = ("" if res.ok else
                    "exited 0, and this row is declared MUST FAIL -- whatever "
                    "it defends is no longer being defended")
    else:
        res.ok = went_green
        res.note = "" if res.ok else f"exited {proc.returncode}"
    return res


def run_set(gates, *, include_all: bool = False, echo: bool = True) -> tuple[list[Result], int]:
    """Run every row, report it, and return the results and an exit code.

    `echo=False` is what the self-test uses: the same code path, judged rather
    than printed.
    """
    results = []
    for gate in gates:
        if gate.needs and not include_all:
            res = Result(gate, None, "", ok=True, skipped=True,
                         note=f"needs {gate.needs}")
            results.append(res)
            if echo:
                _echo_row(res)
            continue
        res = run_gate(gate)
        results.append(res)
        if echo:
            _echo_row(res)
            if not res.ok:
                _echo_output(res)

    failed = [r for r in results if not r.ok]
    return results, 1 if failed else 0


# ---------------------------------------------------------------------------
# Reporting. The report is meant to be pasted into a PR body, which is what
# CLAUDE.md step 8 asks for -- "the actual output of the gates you ran", not a
# claim that they passed.
# ---------------------------------------------------------------------------

_NAME_W = 20


def _count_text(res: Result) -> str:
    if res.skipped:
        return "--"
    if res.count is None:
        return "?"
    return f"{res.count[0]}/{res.count[1]}"


def _echo_row(res: Result) -> None:
    verdict = "SKIP" if res.skipped else ("PASS" if res.ok else "FAIL")
    must = "  MUST FAIL" if res.gate.expect == "fail" else ""
    cmd = " ".join(("python", *res.gate.argv))
    print(f"  {verdict}  {res.gate.name:<{_NAME_W}} {_count_text(res):>9}  "
          f"{cmd}{must}")
    if res.note:
        print(f"          [{res.note}]")


def _echo_output(res: Result, head: int = 20, tail: int = 100) -> None:
    """The failing gate's own words, indented.

    Truncated only when it is very long, and the omission is stated. A report
    that quietly drops the middle of a failure is the same mistake as a set
    that quietly drops a gate.
    """
    lines = res.lines
    if not lines:
        print("          (the gate produced no output at all)")
        return
    print(f"          ---- {res.gate.name} output ----")
    if len(lines) <= head + tail:
        shown = lines
    else:
        shown = (lines[:head]
                 + [f"... {len(lines) - head - tail} line(s) omitted ..."]
                 + lines[-tail:])
    for line in shown:
        print(f"          {line}")
    print("          ---- end ----")


def _echo_summary(results: list[Result], *, enforce_floor: bool = True) -> int:
    ran = [r for r in results if not r.skipped]
    skipped = [r for r in results if r.skipped]
    green = [r for r in ran if r.ok]
    counted = sum(r.count[1] for r in ran if r.count)
    missing = [r.gate.name for r in ran if r.count is None]

    # FLOOR is a claim about the HEADLESS set, so it is measured against the
    # headless rows only. Counting `--all` rows towards it would let the 254
    # checks in the view gate hide a collapse in the five that every clone of
    # this repo can actually run.
    headless = [r for r in ran if not r.gate.needs]
    headless_count = sum(r.count[1] for r in headless if r.count)

    print()
    print(f"{counted} checks across {len(ran)} gate(s)")
    if missing:
        print(f"  (no count parsed from: {', '.join(missing)} -- "
              f"they contribute nothing to the total above)")
    for r in skipped:
        print(f"  skipped: {r.gate.name} -- {r.note}. Run `python checks.py "
              f"--check --all` where that is available.")

    floor_ok = True
    if enforce_floor:
        print(f"  headless coverage: {headless_count}; floor is {FLOOR}")
        floor_ok = headless_count >= FLOOR
        if not floor_ok:
            print(f"\nFAIL  coverage fell below the floor: {headless_count} "
                  f"< {FLOOR}. A slice that reduces coverage is reporting a "
                  f"mistake; if the drop is intended, say why and lower FLOOR "
                  f"in the same commit.")
    else:
        # Said out loud. A partial run that quietly stopped applying the floor
        # would read exactly like a full green one.
        print(f"  floor NOT checked -- this is a partial run; {FLOOR} is a "
              f"claim about the whole set")

    print(f"\n{len(green)}/{len(ran)} gates passed")
    return 0 if (len(green) == len(ran) and floor_ok) else 1


# ---------------------------------------------------------------------------
# The runner's own control. Synthetic rows with known outcomes, run through
# the SAME run_gate/run_set the real table uses -- a control that exercises a
# copy of the logic proves nothing about the logic.
# ---------------------------------------------------------------------------

def _child(source: str) -> tuple[str, ...]:
    return ("-c", source)


_GREEN = _child("print('2/2 checks passed')")
_RED = _child("import sys; print('1/2 checks passed'); "
              "print('FAIL  the deliberately-failing row'); sys.exit(1)")


def self_test() -> list[tuple[str, bool, str]]:
    """Assert the runner can report red, and exits non-zero when it does."""
    checks: list[tuple[str, bool, str]] = []

    def ok(what, cond, detail=""):
        checks.append((what, bool(cond), detail))

    passing = Gate("control: green", "a child that exits 0", _GREEN)
    failing = Gate("control: red", "a child that exits 1", _RED)
    must_fail_red = Gate("control: must-fail red", "a child that exits 1",
                         _RED, expect="fail")
    must_fail_green = Gate("control: must-fail green", "a child that exits 0",
                           _GREEN, expect="fail")
    broken = Gate("control: broken", "a module that is not there",
                  ("-m", "no_such_module_here", "--check"))

    r = run_gate(passing)
    ok("a passing row is reported green", r.ok, r.note)

    r = run_gate(failing)
    ok("a DELIBERATELY FAILING row is reported red -- without this every row "
       "above is a vacuous pass", not r.ok, r.note)
    ok("and the failing gate's own output survives into the report",
       "the deliberately-failing row" in r.output,
       repr(r.output[:120]))

    r = run_gate(must_fail_red)
    ok("a must-fail row that fails is green", r.ok, r.note)

    r = run_gate(must_fail_green)
    ok("a must-fail row that PASSES is reported red", not r.ok, r.note)
    ok("and the note says so rather than leaving it to be inferred",
       "MUST FAIL" in r.note, r.note)

    r = run_gate(broken)
    ok("a row whose target cannot be run is red, not an exception",
       not r.ok, r.note)

    _res, code = run_set([passing, failing], echo=False)
    ok("a set containing a red row exits non-zero", code == 1, f"exit {code}")

    _res, code = run_set([passing, must_fail_red], echo=False)
    ok("a set whose rows all meet their expectation exits zero", code == 0,
       f"exit {code}")

    got = parse_count("blah\n7/9 checks passed\n")
    ok("the count line is read off a gate's output", got == (7, 9), repr(got))

    # The floor, judged rather than printed. Both of these were wrong in the
    # first version of this module and were caught by running it, not by
    # reading it.
    import contextlib
    import io

    def summary_code(results, **kw) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return _echo_summary(results, **kw)

    thin = Result(Gate("thin", "", _GREEN), 0, "", count=(1, 1), ok=True)
    fat_display = Result(Gate("fat", "", _GREEN, needs="a display"), 0, "",
                         count=(5000, 5000), ok=True)

    ok("the floor is measured on the HEADLESS rows only, so a --all gate "
       "cannot hide a collapse in the set every clone can run",
       summary_code([thin, fat_display]) == 1)
    ok("and a partial run is not judged against a floor that describes the "
       "whole set",
       summary_code([thin], enforce_floor=False) == 0)

    missing = [g.name for g in GATES if not target_of(g).is_file()]
    ok("every declared row names a file that exists", not missing,
       f"missing: {missing}")

    return checks


def _run_self_test(echo: bool = True) -> bool:
    checks = self_test()
    passed = sum(1 for _w, good, _d in checks if good)
    if echo:
        print("runner self-test:")
        for what, good, detail in checks:
            print(f"  {'PASS' if good else 'FAIL'}  {what}")
            if detail and not good:
                print(f"          [{detail}]")
        print(f"  {passed}/{len(checks)} checks passed")
    return passed == len(checks)


# ---------------------------------------------------------------------------

def _list_gates() -> None:
    print("gate set:\n")
    for gate in GATES:
        expect = "MUST FAIL" if gate.expect == "fail" else "must pass"
        print(f"  {gate.name}")
        print(f"      run     : {' '.join(('python', *gate.argv))}")
        print(f"      expect  : {expect}")
        if gate.needs:
            print(f"      needs   : {gate.needs} (skipped unless --all)")
        print(f"      defends : {gate.defends}")
        print()
    print(f"  coverage floor: {FLOOR} checks")


def _main(argv=None):
    import argparse

    # Gate output quotes mark names and station labels, which carry
    # typographic quotes; a Windows console defaults to cp1252 and cannot
    # encode them, which would crash the runner on its way to printing a PASS.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="Run the repo's gate set and exit non-zero if any gate "
                    "fails, or if a must-fail gate passes.")
    ap.add_argument("--check", action="store_true",
                    help="run the self-test and then every headless gate")
    ap.add_argument("--list", action="store_true",
                    help="print the gate set -- what each gate defends and "
                         "how to run it on its own")
    ap.add_argument("--all", action="store_true",
                    help="also run the gates that need a display or a study "
                         "on disk")
    ap.add_argument("--only", action="append", default=None, metavar="NAME",
                    help="run only the named gate(s); substring match")
    args = ap.parse_args(argv)

    if args.list:
        _list_gates()
        return 0
    if not args.check:
        ap.print_help()
        return 0

    gates = GATES
    if args.only:
        wanted = [w.lower() for w in args.only]
        gates = [g for g in GATES
                 if any(w in g.name.lower() for w in wanted)]
        if not gates:
            print(f"no gate matches {args.only}")
            return 1

    # The control runs first and unconditionally. If it is red, the rows below
    # it mean nothing, so say that instead of printing a reassuring report.
    control_ok = _run_self_test()
    print()
    if not control_ok:
        print("the runner cannot report red -- every row below would be a "
              "vacuous pass, so the set was not run")
        return 1

    print(f"gate set ({len(gates)} declared):\n")
    results, code = run_set(gates, include_all=args.all)
    summary_code = _echo_summary(results, enforce_floor=not args.only)
    return max(code, summary_code)


if __name__ == "__main__":
    raise SystemExit(_main())
