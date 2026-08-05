"""
mashup.py -- read and write the Power Query M inside an .xlsx.

Excel stores every query in a single `DataMashup` blob in `customXml/item1.xml`:
a UTF-16 XML wrapper whose text is base64 of

    [uint32 version][uint32 package length][package zip]
    [uint32 len][permissions][uint32 len][metadata][uint32 len][permission bindings]

and the package zip holds `Formulas/Section1.m`, which is the actual M source
for every query in the workbook.

WHY THIS EXISTS
    The M is the only part of this project that git could not see. `.gitignore`
    excludes `sources/*.xlsx` because the workbook is 4 MB of cached results,
    so the query logic -- the thing that actually broke, and the thing Phase 1
    of AGENT_TASK.md is entirely about -- had no version history at all.
    `extract` writes it out as plain text so it can be committed and diffed.

WRITING IT BACK
    `inject` rebuilds the blob with a new Section1.m. It is deliberately narrow:
    it rewrites query BODIES only. Adding a query that must LOAD TO A SHEET also
    needs a connection, a sheet, a table and a queryTable part, which is far more
    invasive and is better done by Excel itself. Use `apply_via_com` for that.

    Always work on a copy. `inject` refuses to write to a path under `sources/`.
"""

from __future__ import annotations

import base64
import io
import re
import shutil
import struct
import zipfile
from pathlib import Path

MASHUP_PART = "customXml/item1.xml"
SECTION_PATH = "Formulas/Section1.m"

_B64_RE = re.compile(r">([A-Za-z0-9+/=\s]+)</DataMashup>")


def _read_mashup_xml(xlsx: Path) -> str:
    with zipfile.ZipFile(xlsx) as z:
        if MASHUP_PART not in z.namelist():
            raise ValueError(f"{xlsx.name} has no Power Query blob "
                             f"({MASHUP_PART} missing)")
        raw = z.read(MASHUP_PART)
    # The part is UTF-16 with a BOM.
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("could not decode the DataMashup part")


def _split_blob(blob: bytes) -> tuple[int, bytes, bytes]:
    """-> (version, package zip bytes, everything after the package)."""
    version, pkg_len = struct.unpack("<II", blob[:8])
    pkg = blob[8:8 + pkg_len]
    return version, pkg, blob[8 + pkg_len:]


def extract(xlsx: Path) -> str:
    """Return the full M source of every query in the workbook."""
    xml = _read_mashup_xml(Path(xlsx))
    m = _B64_RE.search(xml)
    if not m:
        raise ValueError("no base64 payload inside <DataMashup>")
    _, pkg, _ = _split_blob(base64.b64decode(m.group(1)))
    with zipfile.ZipFile(io.BytesIO(pkg)) as z:
        return z.read(SECTION_PATH).decode("utf-8")


def query_names(section_m: str) -> list[str]:
    """Every `shared <name> =` in the section, in order."""
    return re.findall(r"^shared\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=",
                      section_m, re.M)


def inject(src_xlsx: Path, dest_xlsx: Path, section_m: str) -> Path:
    """Write `section_m` into a COPY of the workbook. Never edits in place."""
    src_xlsx, dest_xlsx = Path(src_xlsx), Path(dest_xlsx)
    if "sources" in [p.name for p in dest_xlsx.resolve().parents]:
        raise ValueError("refusing to write into sources/ -- it is read-only")
    if src_xlsx.resolve() == dest_xlsx.resolve():
        raise ValueError("refusing to edit the workbook in place; pass a copy")

    xml = _read_mashup_xml(src_xlsx)
    m = _B64_RE.search(xml)
    if not m:
        raise ValueError("no base64 payload inside <DataMashup>")
    version, pkg, tail = _split_blob(base64.b64decode(m.group(1)))

    # Rebuild the package zip with the new Section1.m, keeping every other part.
    out_pkg = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(pkg)) as zin, \
            zipfile.ZipFile(out_pkg, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.namelist():
            data = (section_m.encode("utf-8") if item == SECTION_PATH
                    else zin.read(item))
            zout.writestr(item, data)
    new_pkg = out_pkg.getvalue()

    new_blob = struct.pack("<II", version, len(new_pkg)) + new_pkg + tail
    b64 = base64.b64encode(new_blob).decode("ascii")
    new_xml = xml[:m.start(1)] + b64 + xml[m.end(1):]

    dest_xlsx.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_xlsx, dest_xlsx)

    # Rewrite the one part, preserving everything else byte-for-byte.
    tmp = dest_xlsx.with_suffix(".tmp.xlsx")
    with zipfile.ZipFile(src_xlsx) as zin, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = (new_xml.encode("utf-16") if item.filename == MASHUP_PART
                    else zin.read(item.filename))
            zout.writestr(item, data)
    tmp.replace(dest_xlsx)
    return dest_xlsx


def apply_via_com(xlsx: Path, section_m: str, timeout_s: int = 300) -> None:
    """Push query bodies into a workbook through Excel itself.

    Excel owns the blob and rebuilds every dependent part -- connections, sheet
    tables, queryTables -- which is why this is the right tool when queries are
    ADDED or REMOVED rather than merely edited. Needs Excel + pywin32.
    """
    import win32com.client as win32

    bodies = _split_queries(section_m)
    xl = win32.DispatchEx("Excel.Application")
    xl.Visible = False
    xl.DisplayAlerts = False
    wb = None
    try:
        wb = xl.Workbooks.Open(str(Path(xlsx).resolve()), UpdateLinks=0)
        existing = {q.Name: q for q in wb.Queries}
        for name, formula in bodies.items():
            if name in existing:
                existing[name].Formula = formula
            else:
                wb.Queries.Add(Name=name, Formula=formula)
        for name, q in existing.items():
            if name not in bodies:
                q.Delete()
        wb.Save()
    finally:
        if wb is not None:
            wb.Close(SaveChanges=False)
        xl.Quit()


MASHUP_OLEDB = ('OLEDB;Provider=Microsoft.Mashup.OleDb.1;Data Source=$Workbook$;'
                'Location={name};Extended Properties=""')

XL_SRC_EXTERNAL = 0
XL_CMD_SQL = 2


def configure_loading(xlsx: Path, *, load_to_sheet: list[str] | None = None,
                      unload: list[str] | None = None,
                      drop_sheets: list[str] | None = None,
                      refresh_on_load: bool | None = None,
                      timeout_s: int = 300) -> dict:
    """Fix up which queries load where. Needs Excel + pywin32.

    Editing the M blob directly changes query BODIES only. Whether a query
    materialises onto a sheet lives in connections.xml, the sheet, a table part
    and a queryTable part -- four coordinated pieces that Excel maintains and
    that are not safe to hand-write. So this is done through Excel.

    load_to_sheet   queries that should appear as a sheet (e.g. meta_refresh)
    unload          queries that should become staging-only (e.g. src_NDBC),
                    which is done by deleting the sheet that materialises them
    drop_sheets     leftover sheets whose query no longer exists
    refresh_on_load whether opening the file re-runs every query. Set this True
                    on sources/ only. It MUST stay False in a project snapshot:
                    a snapshot that refreshes itself on open is not immutable,
                    and its manifest would no longer describe its contents.
    """
    import pythoncom
    import win32com.client as win32

    report: dict = {"loaded": [], "unloaded": [], "dropped": [], "errors": []}
    pythoncom.CoInitialize()
    xl = wb = None
    try:
        xl = win32.DispatchEx("Excel.Application")
        xl.Visible = False
        xl.DisplayAlerts = False
        wb = xl.Workbooks.Open(str(Path(xlsx).resolve()), UpdateLinks=0)
        sheets = {ws.Name.lower(): ws for ws in wb.Worksheets}

        for name in (drop_sheets or []) + (unload or []):
            ws = sheets.get(name.lower())
            if ws is None:
                continue
            try:
                ws.Delete()
                bucket = "dropped" if name in (drop_sheets or []) else "unloaded"
                report[bucket].append(name)
                sheets.pop(name.lower(), None)
            except Exception as e:
                report["errors"].append(f"delete {name}: {e}")

        # No pre-flight against wb.Queries: that collection is not reachable
        # through late binding in this Excel build (both .Count and iteration
        # raise). A missing query surfaces as a clear error from ListObjects.Add
        # below, which is enough.
        for name in (load_to_sheet or []):
            if name.lower() in sheets:
                report["loaded"].append(f"{name} (already)")
                continue
            ws, conn, errs = None, None, []
            try:
                ws = wb.Worksheets.Add()
                ws.Name = name
                cs = MASHUP_OLEDB.format(name=name)
                sql = f"SELECT * FROM [{name}]"

                # Two routes, because ListObjects.Add(xlSrcExternal, ...) returns
                # E_INVALIDARG against the Mashup OLE DB provider in some Excel
                # builds. QueryTables.Add takes the same connection string and
                # succeeds where that one does not.
                try:
                    conn = wb.Connections.Add2(
                        f"Query - {name}",
                        f"Connection to the '{name}' query in the workbook.",
                        cs, sql, XL_CMD_SQL)
                    lo = ws.ListObjects.Add(SourceType=XL_SRC_EXTERNAL,
                                            Source=conn,
                                            Destination=ws.Range("$A$1"))
                    lo.QueryTable.BackgroundQuery = False
                    lo.QueryTable.Refresh(BackgroundQuery=False)
                except Exception as e1:
                    errs.append(f"ListObjects.Add: {e1}")
                    if conn is not None:
                        try:
                            conn.Delete()      # no orphan connection left behind
                        except Exception:
                            pass
                        conn = None
                    qt = ws.QueryTables.Add(Connection=cs,
                                            Destination=ws.Range("$A$1"))
                    qt.CommandType = XL_CMD_SQL
                    qt.CommandText = sql
                    qt.BackgroundQuery = False
                    qt.Refresh(BackgroundQuery=False)
                report["loaded"].append(name)
            except Exception as e:
                errs.append(f"QueryTables.Add: {e}")
                report["errors"].append(f"load {name}: " + " | ".join(errs))
                # Leave neither a blank sheet nor a dangling connection: both
                # would look like the query loaded when it did not.
                for cleanup in (lambda: ws.Delete() if ws is not None else None,
                                lambda: conn.Delete() if conn is not None else None):
                    try:
                        cleanup()
                    except Exception:
                        pass

        if refresh_on_load is not None:
            n = 0
            for i in range(1, wb.Connections.Count + 1):
                try:
                    conn = wb.Connections.Item(i)
                    conn.OLEDBConnection.RefreshOnFileOpen = bool(refresh_on_load)
                    n += 1
                except Exception:
                    pass
            report["refresh_on_load"] = f"{refresh_on_load} on {n} connection(s)"

        wb.Save()
    finally:
        try:
            if wb is not None:
                wb.Close(SaveChanges=False)
        except Exception:
            pass
        try:
            if xl is not None:
                xl.Quit()
        except Exception:
            pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
    return report


def _split_queries(section_m: str) -> dict[str, str]:
    """`section Section1; shared a = ...; shared b = ...;` -> {name: body}."""
    out: dict[str, str] = {}
    starts = [(m.group(1), m.start())
              for m in re.finditer(r"^shared\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=",
                                   section_m, re.M)]
    for i, (name, pos) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(section_m)
        body = section_m[pos:end]
        body = body.split("=", 1)[1].strip()
        out[name] = body.rstrip().rstrip(";").rstrip()
    return out


def rebuild_workbook(src_xlsx: Path, dest_xlsx: Path, section_path: Path,
                     *, refresh: bool = True, log=print) -> Path:
    """The whole Phase 1 fix, end to end, onto a COPY of the workbook.

        inject corrected M -> refresh through Excel -> fix up what loads where

    `src_xlsx` is only ever read, so it is safe to run against sources/ even
    though sources/ is read-only by policy.
    """
    from .refresh import refresh_and_snapshot

    src, dest = Path(src_xlsx), Path(dest_xlsx)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = dest.with_name(dest.stem + ".staged.xlsx")

    log(f"injecting {section_path} into a copy of {src.name} ...")
    inject(src, staged, Path(section_path).read_text(encoding="utf-8"))

    if refresh:
        log("refreshing through Excel (tens of seconds; needs Excel closed) ...")
        refresh_and_snapshot(staged, dest)
        staged.unlink(missing_ok=True)
    else:
        staged.replace(dest)

    log("configuring which queries load where ...")
    report = configure_loading(
        dest,
        load_to_sheet=["meta_refresh"],
        unload=["src_NDBC"],          # staging: the 3 station tables carry it
        drop_sheets=["meta_ERDDAP"],  # query deleted; it duplicated meta_COOPS_datums
        # False, deliberately. A snapshot that re-refreshes when opened is not
        # immutable and its manifest would stop describing its own contents.
        # Turn this on for sources/ only, by hand or with a separate call.
        refresh_on_load=False)
    for k, v in report.items():
        log(f"  {k}: {v}")
    return dest


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Read/write Power Query M in xlsx.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("rebuild", help="inject + refresh + configure, onto a copy")
    r.add_argument("xlsx", type=Path)
    r.add_argument("-o", "--out", type=Path, required=True)
    r.add_argument("--section", type=Path, default=Path("sources/Section1.m"))
    r.add_argument("--no-refresh", action="store_true")

    c = sub.add_parser("configure", help="fix up which queries load where")
    c.add_argument("xlsx", type=Path)
    c.add_argument("--load", nargs="*", default=[])
    c.add_argument("--unload", nargs="*", default=[])
    c.add_argument("--drop-sheets", nargs="*", default=[])
    c.add_argument("--refresh-on-load", choices=["true", "false"], default=None)

    e = sub.add_parser("extract", help="write Section1.m to a text file")
    e.add_argument("xlsx", type=Path)
    e.add_argument("-o", "--out", type=Path, default=None)

    n = sub.add_parser("names", help="list query names")
    n.add_argument("xlsx", type=Path)

    i = sub.add_parser("inject", help="write a Section1.m into a COPY")
    i.add_argument("xlsx", type=Path)
    i.add_argument("section", type=Path)
    i.add_argument("-o", "--out", type=Path, required=True)

    args = ap.parse_args(argv)

    if args.cmd == "rebuild":
        out = rebuild_workbook(args.xlsx, args.out, args.section,
                               refresh=not args.no_refresh)
        print(f"\nwrote {out}")
        return 0

    if args.cmd == "configure":
        rol = (None if args.refresh_on_load is None
               else args.refresh_on_load == "true")
        rep = configure_loading(args.xlsx, load_to_sheet=args.load,
                                unload=args.unload,
                                drop_sheets=args.drop_sheets,
                                refresh_on_load=rol)
        for k, v in rep.items():
            print(f"  {k}: {v}")
        return 0 if not rep.get("errors") else 1

    if args.cmd == "names":
        for name in query_names(extract(args.xlsx)):
            print(name)
        return 0

    if args.cmd == "extract":
        text = extract(args.xlsx)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8")
            print(f"wrote {args.out} ({len(text):,} chars, "
                  f"{len(query_names(text))} queries)")
        else:
            print(text)
        return 0

    dest = inject(args.xlsx, args.out,
                  args.section.read_text(encoding="utf-8"))
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
