"""
importgate.py -- prove that the headless path never acquires a UI dependency.

WHY THIS IS A MODULE AND NOT A SCRATCH SCRIPT
    It was written as a throwaway three separate times while landing #2, #3 and
    #4, and rebuilt from scratch on each one because nothing carried it forward.
    It spans modules rather than belonging to any one of them, so a module of
    its own is the natural home -- and now every later slice inherits it the way
    `view.py --check` is inherited.

WHAT IT DEFENDS
    Pulling data and writing workbooks must keep working on a machine that has
    never installed matplotlib or opened a display. That is not a preference:
    `exporter` importing `view` even once, or `identity` reaching for a colour
    from a plotting backend, would break the export path on a headless box and
    nothing else in the repo would notice.

    `view` is the exception that proves it. It must still IMPORT with matplotlib
    absent, because `compare.py` imports it unconditionally at startup and has
    to be able to explain the problem rather than fail to start. What it must
    not do is claim to work: `available()` reports False.

WHY SUBPROCESSES
    Blocking is per-interpreter and imports are cached. Once one case has
    imported matplotlib, every later case in the same process would pass
    vacuously against a module that is already in `sys.modules`. One clean
    interpreter per case is the only version of this that means anything.

    This module therefore imports nothing it needs to block, at its own scope
    or anywhere else -- it only ever spawns children.

THE CONTROLS ARE THE POINT
    Blocking is done with a meta-path finder, and a finder that silently does
    nothing turns every line of the report into a vacuous pass. The first
    version of this in #2 would have reported total success against a gate that
    was not blocking anything at all. So two cases exist purely to prove the
    mechanism works:

        * importing `tkinter` while `tkinter` is blocked must FAIL
        * `view.available()` must be True when matplotlib is NOT blocked

    If either of those flips, nothing else in the report can be believed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# The child programs. Kept as source text handed to `python -c` so that each
# runs in a genuinely fresh interpreter with nothing pre-imported.
# ---------------------------------------------------------------------------

_CHILD_BLOCKED = r'''
import json, sys

blocked = set(json.loads(sys.argv[1]))
target, expr = sys.argv[2], sys.argv[3]


class _Blocker:
    """Refuse to find anything under a blocked top-level name."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise ImportError(f"{fullname} is blocked by importgate")
        return None


# Anything already resident would be found in sys.modules without ever
# consulting the finder, so the cache is purged before the finder goes in.
for _name in list(sys.modules):
    if _name.split(".")[0] in blocked:
        del sys.modules[_name]
sys.meta_path.insert(0, _Blocker())

out = {"imported": False, "error": "", "expr": None}
try:
    mod = __import__(target)
    out["imported"] = True
    if expr:
        out["expr"] = bool(eval(expr, {target: mod}))
except BaseException as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"
print("GATE" + json.dumps(out))
'''

_CHILD_PURE = r'''
import json, sys

target = sys.argv[1]
before = set(sys.modules)
out = {"imported": False, "error": "", "foreign": []}
try:
    __import__(target)
    out["imported"] = True
except BaseException as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"

# Measured rather than blocked: a whitelist of permitted imports would have to
# be maintained, whereas the sys.modules delta IS the property being claimed.
fresh = {n.split(".")[0] for n in set(sys.modules) - before}
std = set(sys.stdlib_module_names)
out["foreign"] = sorted(n for n in fresh if n not in std and n != target)
print("GATE" + json.dumps(out))
'''


@dataclass
class Case:
    module: str
    blocked: tuple = ()
    expect_import: bool = True
    expr: str = ""              # evaluated in the child; must come back True
    expr_note: str = ""
    control: bool = False


# The cumulative table. Rows are added by the slice that creates the module.
CASES = [
    Case("identity", ("tkinter", "matplotlib")),
    Case("annotations", ("tkinter", "matplotlib")),
    Case("sensorkit", ("tkinter",)),
    Case("exporter", ("tkinter",)),
    Case("exporter", ("matplotlib",)),
    Case("view", ("matplotlib",), expr="not view.available()",
         expr_note="available() == False, reports itself off"),

    # ---- controls: without these the rows above can pass vacuously ----------
    Case("tkinter", ("tkinter",), expect_import=False, control=True),
    Case("view", (), expr="view.available()",
         expr_note="available() == True with matplotlib present", control=True),
]

# Modules claiming to import nothing outside the standard library. `identity`
# is pure by design; `annotations` is deliberately light enough that the store
# can be exercised anywhere, which is a claim worth checking rather than
# trusting.
PURE = ["identity", "annotations"]


def _run(program: str, *args) -> dict:
    proc = subprocess.run([sys.executable, "-c", program, *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=180)
    for line in proc.stdout.splitlines():
        if line.startswith("GATE"):
            return json.loads(line[4:])
    return {"imported": False, "error":
            f"child produced no verdict (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"}


def check_case(case: Case) -> tuple[bool, str]:
    got = _run(_CHILD_BLOCKED, json.dumps(list(case.blocked)), case.module,
               case.expr)

    if got["imported"] != case.expect_import:
        if case.expect_import:
            return False, f"import failed: {got['error']}"
        return False, "imported anyway -- the finder is not blocking"

    if not case.expect_import:
        return True, got["error"]

    if case.expr and got.get("expr") is not True:
        return False, f"{case.expr!r} was not True ({got.get('expr')!r})"
    return True, case.expr_note


def check_pure(module: str) -> tuple[bool, str]:
    got = _run(_CHILD_PURE, module)
    if not got["imported"]:
        return False, f"import failed: {got['error']}"
    if got["foreign"]:
        return False, f"imports outside the standard library: {got['foreign']}"
    return True, "nothing outside the standard library"


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Assert the headless path never acquires a UI dependency.")
    ap.add_argument("--check", action="store_true",
                    help="run the gate and exit non-zero on failure")
    args = ap.parse_args(argv)
    if not args.check:
        ap.print_help()
        return 0

    rows = []
    for case in CASES:
        good, note = check_case(case)
        blocked = ", ".join(case.blocked) if case.blocked else "nothing"
        verb = "imports" if case.expect_import else "FAILS to import"
        what = (f"{'control: ' if case.control else ''}"
                f"{case.module:<12} {verb:<15} with {blocked:<22} blocked")
        rows.append((what, good, note))

    for module in PURE:
        good, note = check_pure(module)
        rows.append((f"{module:<12} imports only the standard library", good,
                     note if not good else ""))

    print("\nimport gate:")
    for what, good, note in rows:
        print(f"  {'PASS' if good else 'FAIL'}  {what}")
        if note:
            print(f"          [{note}]")
    passed = sum(1 for _w, g, _n in rows if g)
    print(f"\n{passed}/{len(rows)} gates passed")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
