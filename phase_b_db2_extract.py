#!/usr/bin/env python3
"""Phase B COMPLETE v4 (2026-08-13), with per-query DB2 logging.

Requires: pip install ibm_db
Credentials are never stored in output. Connection values come from CLI/env.

Visible verification markers:
- E1 has 3 queries (1-of-3, 2-of-3, 3-of-3).
- E4 has 2 queries (1-of-2, 2-of-2).
- Literal coverage validation runs before any DB2 connection.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    import ibm_db  # type: ignore
except ImportError:  # permit --dry-run without the driver
    ibm_db = None


@dataclass(frozen=True)
class Query:
    phase: str
    slug: str
    sql: str
    optional: bool = False
    note: str = ""


LOG = logging.getLogger("phase_b_db2_extract")

# Literal checklist. The document contains 45 fixed SQL statements plus the P12
# COUNT(*) template. Seven fixed extraction queries are supplemental because
# P10, P11 and P14 describe required inputs without printing all extraction SQL,
# and P12 needs a safe catalog query to select its top 50 tables.
DOCUMENT_FIXED_SLUGS = {
    "E": {
        "E1-query-1-of-3-version-fixpack",
        "E1-query-2-of-3-instance-topology",
        "E1-query-3-of-3-system-platform",
        "E2-query-1-of-4-dbpartitiongroups",
        "E2-query-2-of-4-dbpartitiongroupdef",
        "E2-query-3-of-4-table-partition-groups",
        "E2-query-4-of-4-distribution-keys",
        "E3-query-1-of-1-dbcfg",
        "E4-query-1-of-2-reg-variables-filtered",
        "E4-query-2-of-2-reg-variables-all",
        "E5-query-1-of-5-tablespaces",
        "E5-query-2-of-5-bufferpools",
        "E5-query-3-of-5-mdc-indexes",
        "E5-query-4-of-5-data-partitions",
        "E5-query-5-of-5-table-organization",
    },
    "P9": {
        "09a-object-counts", "09b-table-metrics", "09c-physical-size",
        "09d-routine-counts", "09e-routine-bodies", "09f-trigger-bodies",
        "09g-constraints", "09g-sequences", "09g-generated-columns",
        "09h-periods", "09h-controls", "09h-special-types",
        "09i-wrappers", "09i-servers", "09i-nicknames", "09i-server-options",
        "09i-user-options", "09i-function-mappings", "09i-type-mappings",
        "09i-nickname-dependencies",
    },
    "P12": {"12-lob-inventory"},
    "P13": {
        "13a-read-grants", "13b-write-grants", "13c-routine-grants",
        "13d-packages", "13e-package-statements",
        "13f-cross-schema-table-deps", "13f-cross-schema-routine-deps",
        "13g-current-sql", "13g-connection-summary",
    },
}
SUPPLEMENTAL_FIXED_SLUGS = {
    "P10": {"10-columns"},
    "P11": {"11-routine-bodies", "11-trigger-bodies", "11-views", "11-package-statements"},
    "P12": {"12-top-50-tables"},
    "P14": {"14-dbauth"},
}


def configure_logging(log_file: Path, verbose: bool) -> None:
    """Log progress to console and a UTF-8 file without connection secrets."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)sZ | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = __import__("time").gmtime
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    LOG.addHandler(console)
    LOG.addHandler(file_handler)


def schema_pred(column: str, schemas: list[str]) -> str:
    values = ", ".join("'" + s.replace("'", "''") + "'" for s in schemas)
    return f"{column} IN ({values})"


def queries(schemas: list[str]) -> list[Query]:
    ts = schema_pred("T.TABSCHEMA", schemas)
    s = schema_pred("TABSCHEMA", schemas)
    rs = schema_pred("ROUTINESCHEMA", schemas)
    trgs = schema_pred("TRIGSCHEMA", schemas)
    base = [
        Query("E", "E1-query-1-of-3-version-fixpack", "SELECT SERVICE_LEVEL,FIXPACK_NUM,BLD_LEVEL FROM SYSIBMADM.ENV_INST_INFO"),
        Query("E", "E1-query-2-of-3-instance-topology", "SELECT INST_NAME,IS_INST_PARTITIONABLE,NUM_DBPARTITIONS FROM SYSIBMADM.ENV_INST_INFO"),
        Query("E", "E1-query-3-of-3-system-platform", "SELECT OS_NAME,OS_VERSION,OS_RELEASE,HOST_NAME,TOTAL_CPUS,TOTAL_MEMORY FROM SYSIBMADM.ENV_SYS_INFO"),
        Query("E", "E2-query-1-of-4-dbpartitiongroups", "SELECT * FROM SYSCAT.DBPARTITIONGROUPS"),
        Query("E", "E2-query-2-of-4-dbpartitiongroupdef", "SELECT * FROM SYSCAT.DBPARTITIONGROUPDEF"),
        Query("E", "E2-query-3-of-4-table-partition-groups", f"SELECT S.DBPGNAME,T.TABSCHEMA,T.TABNAME FROM SYSCAT.TABLES T JOIN SYSCAT.TABLESPACES S ON T.TBSPACEID=S.TBSPACEID WHERE {ts}"),
        Query("E", "E2-query-4-of-4-distribution-keys", f"SELECT TABSCHEMA,TABNAME,COLNAME,PARTKEYSEQ FROM SYSCAT.COLUMNS WHERE {s} AND PARTKEYSEQ>0 ORDER BY TABNAME,PARTKEYSEQ"),
        Query("E", "E3-query-1-of-1-dbcfg", "SELECT * FROM SYSIBMADM.DBCFG WHERE NAME IN ('codeset','codepage','territory','collate_info','alt_collate','string_units','date_compat','number_compat','varchar2_compat','cur_commit','decflt_rounding','pagesize')"),
        Query("E", "E4-query-1-of-2-reg-variables-filtered", "SELECT NAME,VALUE,VALUE_FLAGS FROM SYSIBMADM.REG_VARIABLES WHERE NAME IN ('DB2_COMPATIBILITY_VECTOR','DB2_DEFERRED_PREPARE_SEMANTICS','DB2_WORKLOAD','DB2COMPOPT')", True, "Document E4 query 1 of 2; VALUE_FLAGS can vary by DB2 level"),
        Query("E", "E4-query-2-of-2-reg-variables-all", "SELECT NAME,VALUE FROM SYSIBMADM.REG_VARIABLES", True, "Document E4 query 2 of 2"),
        Query("E", "E5-query-1-of-5-tablespaces", "SELECT TBSPACE,TBSPACETYPE,DATATYPE,PAGESIZE,EXTENTSIZE,PREFETCHSIZE,BUFFERPOOLID FROM SYSCAT.TABLESPACES"),
        Query("E", "E5-query-2-of-5-bufferpools", "SELECT BPNAME,NPAGES,PAGESIZE FROM SYSCAT.BUFFERPOOLS"),
        Query("E", "E5-query-3-of-5-mdc-indexes", f"SELECT TABSCHEMA,TABNAME,INDNAME,INDEXTYPE FROM SYSCAT.INDEXES WHERE {s} AND INDEXTYPE IN ('BLOK','DIM')"),
        Query("E", "E5-query-4-of-5-data-partitions", f"SELECT TABNAME,DATAPARTITIONNAME,SEQNO,LOWVALUE,HIGHVALUE FROM SYSCAT.DATAPARTITIONS WHERE {s} ORDER BY TABNAME,SEQNO", True),
        Query("E", "E5-query-5-of-5-table-organization", f"SELECT TABNAME,COMPRESSION,ROWCOMPMODE,TABLEORG FROM SYSCAT.TABLES WHERE {s} AND TYPE='T'"),
        Query("P9", "09a-object-counts", f"SELECT TYPE,COUNT(*) AS CNT FROM SYSCAT.TABLES WHERE {s} GROUP BY TYPE"),
        Query("P9", "09b-table-metrics", f"SELECT T.TABSCHEMA,T.TABNAME,T.CARD AS EST_ROWS,T.COLCOUNT,T.STATS_TIME,(SELECT COUNT(*) FROM SYSCAT.INDEXES I WHERE I.TABSCHEMA=T.TABSCHEMA AND I.TABNAME=T.TABNAME) IDX_CNT,(SELECT COUNT(*) FROM SYSCAT.TRIGGERS G WHERE G.TABSCHEMA=T.TABSCHEMA AND G.TABNAME=T.TABNAME) TRG_CNT,(SELECT COUNT(*) FROM SYSCAT.REFERENCES R WHERE R.TABSCHEMA=T.TABSCHEMA AND R.TABNAME=T.TABNAME) FK_OUT,T.PARTITION_MODE,T.COMPRESSION,T.TEMPORALTYPE FROM SYSCAT.TABLES T WHERE {ts} AND T.TYPE='T' ORDER BY T.CARD DESC"),
        Query("P9", "09c-physical-size", f"SELECT TABSCHEMA,TABNAME,DATA_OBJECT_P_SIZE+INDEX_OBJECT_P_SIZE+LONG_OBJECT_P_SIZE+LOB_OBJECT_P_SIZE+XML_OBJECT_P_SIZE AS TOTAL_KB FROM SYSIBMADM.ADMINTABINFO WHERE {s} ORDER BY TOTAL_KB DESC"),
        Query("P9", "09d-routine-counts", f"SELECT ROUTINETYPE,ORIGIN,LANGUAGE,COUNT(*) CNT FROM SYSCAT.ROUTINES WHERE {rs} GROUP BY ROUTINETYPE,ORIGIN,LANGUAGE"),
        Query("P9", "09e-routine-bodies", f"SELECT ROUTINESCHEMA,ROUTINENAME,ROUTINETYPE,LANGUAGE,PARM_COUNT,RESULT_SETS,LENGTH(TEXT) TEXT_CHARS,TEXT FROM SYSCAT.ROUTINES WHERE {rs} AND ORIGIN='Q' ORDER BY TEXT_CHARS DESC"),
        Query("P9", "09f-trigger-bodies", f"SELECT TRIGSCHEMA,TRIGNAME,TABSCHEMA,TABNAME,TRIGTIME,TRIGEVENT,GRANULARITY,LENGTH(TEXT) TEXT_CHARS,TEXT FROM SYSCAT.TRIGGERS WHERE {trgs} ORDER BY TEXT_CHARS DESC"),
        Query("P9", "09g-constraints", f"SELECT TYPE,COUNT(*) CNT FROM SYSCAT.TABCONST WHERE {s} GROUP BY TYPE"),
        Query("P9", "09g-sequences", f"SELECT SEQSCHEMA,SEQNAME,DATATYPEID,START,INCREMENT,CACHE,CYCLE FROM SYSCAT.SEQUENCES WHERE {schema_pred('SEQSCHEMA', schemas)}"),
        Query("P9", "09g-generated-columns", f"SELECT TABSCHEMA,TABNAME,COLNAME,IDENTITY,GENERATED,TEXT FROM SYSCAT.COLUMNS WHERE {s} AND (IDENTITY='Y' OR GENERATED<>' ' )"),
        Query("P9", "09h-periods", f"SELECT * FROM SYSCAT.PERIODS WHERE {s}", True, "DB2 10.1+"),
        Query("P9", "09h-controls", f"SELECT * FROM SYSCAT.CONTROLS WHERE {s}", True, "DB2 10.1+ / RCAC"),
        Query("P9", "09h-special-types", f"SELECT TABSCHEMA,TABNAME,TYPENAME,COUNT(*) CNT FROM SYSCAT.COLUMNS WHERE {s} AND TYPENAME IN ('GRAPHIC','VARGRAPHIC','DBCLOB','DECFLOAT','XML','ROWID','BLOB','CLOB') GROUP BY TABSCHEMA,TABNAME,TYPENAME"),
        Query("P9", "09i-wrappers", "SELECT WRAPNAME,WRAPTYPE,LIBRARY FROM SYSCAT.WRAPPERS", True),
        Query("P9", "09i-servers", "SELECT SERVERNAME,SERVERTYPE,SERVERVERSION,WRAPNAME FROM SYSCAT.SERVERS", True),
        Query("P9", "09i-nicknames", "SELECT TABSCHEMA,TABNAME AS NICKNAME,SERVERNAME,REMOTE_SCHEMA,REMOTE_TABLE FROM SYSCAT.NICKNAMES ORDER BY SERVERNAME,TABNAME", True),
        Query("P9", "09i-server-options", "SELECT SERVERNAME,OPTION,SETTING FROM SYSCAT.SERVEROPTIONS", True),
        Query("P9", "09i-user-options", "SELECT AUTHID,SERVERNAME,OPTION FROM SYSCAT.USEROPTIONS", True),
        Query("P9", "09i-function-mappings", "SELECT * FROM SYSCAT.FUNCMAPPINGS", True),
        Query("P9", "09i-type-mappings", "SELECT * FROM SYSCAT.TYPEMAPPINGS", True),
        Query("P9", "09i-nickname-dependencies", "SELECT DISTINCT D.TABSCHEMA,D.TABNAME,D.BSCHEMA,D.BNAME FROM SYSCAT.TABDEP D JOIN SYSCAT.NICKNAMES N ON N.TABSCHEMA=D.BSCHEMA AND N.TABNAME=D.BNAME", True),
        Query("P10", "10-columns", f"SELECT TABSCHEMA,TABNAME,COLNAME,TYPENAME,LENGTH,SCALE,NULLS,DEFAULT,CODEPAGE,IDENTITY,GENERATED FROM SYSCAT.COLUMNS WHERE {s} ORDER BY TABSCHEMA,TABNAME,COLNO"),
        Query("P11", "11-routine-bodies", f"SELECT ROUTINESCHEMA,ROUTINENAME,ROUTINETYPE,LANGUAGE,PARM_COUNT,RESULT_SETS,LENGTH(TEXT) TEXT_CHARS,TEXT FROM SYSCAT.ROUTINES WHERE {rs} ORDER BY ROUTINESCHEMA,ROUTINENAME"),
        Query("P11", "11-trigger-bodies", f"SELECT TRIGSCHEMA,TRIGNAME,TABSCHEMA,TABNAME,TRIGTIME,TRIGEVENT,GRANULARITY,LENGTH(TEXT) TEXT_CHARS,TEXT FROM SYSCAT.TRIGGERS WHERE {trgs} ORDER BY TRIGSCHEMA,TRIGNAME"),
        Query("P11", "11-views", f"SELECT VIEWSCHEMA,VIEWNAME,LENGTH(TEXT) TEXT_CHARS,TEXT FROM SYSCAT.VIEWS WHERE {schema_pred('VIEWSCHEMA', schemas)}"),
        Query("P11", "11-package-statements", "SELECT PKGSCHEMA,PKGNAME,SECTNO,TEXT FROM SYSCAT.STATEMENTS ORDER BY PKGSCHEMA,PKGNAME,SECTNO", True, "Catalog availability/authority varies; also used by P13"),
        Query("P12", "12-lob-inventory", f"SELECT TABSCHEMA,TABNAME,COLNAME,TYPENAME,LENGTH FROM SYSCAT.COLUMNS WHERE {s} AND TYPENAME IN ('BLOB','CLOB','DBCLOB')"),
        Query("P12", "12-top-50-tables", f"SELECT TABSCHEMA,TABNAME,CARD AS EST_ROWS FROM SYSCAT.TABLES WHERE {s} AND TYPE='T' ORDER BY CARD DESC FETCH FIRST 50 ROWS ONLY", False, "Drives one actual COUNT(*) per returned table"),
        Query("P13", "13a-read-grants", f"SELECT GRANTEE,GRANTEETYPE,COUNT(*) TABLES_GRANTED FROM SYSCAT.TABAUTH WHERE {s} AND SELECTAUTH IN ('Y','G') GROUP BY GRANTEE,GRANTEETYPE ORDER BY TABLES_GRANTED DESC"),
        Query("P13", "13b-write-grants", f"SELECT GRANTEE,GRANTEETYPE,SUM(CASE WHEN INSERTAUTH IN ('Y','G') THEN 1 ELSE 0 END) CAN_INSERT,SUM(CASE WHEN UPDATEAUTH IN ('Y','G') THEN 1 ELSE 0 END) CAN_UPDATE,SUM(CASE WHEN DELETEAUTH IN ('Y','G') THEN 1 ELSE 0 END) CAN_DELETE FROM SYSCAT.TABAUTH WHERE {s} GROUP BY GRANTEE,GRANTEETYPE ORDER BY CAN_UPDATE DESC"),
        Query("P13", "13c-routine-grants", f"SELECT GRANTEE,GRANTEETYPE,COUNT(*) ROUTINES_GRANTED FROM SYSCAT.ROUTINEAUTH WHERE {schema_pred('SCHEMA', schemas)} AND EXECUTEAUTH IN ('Y','G') GROUP BY GRANTEE,GRANTEETYPE ORDER BY ROUTINES_GRANTED DESC"),
        Query("P13", "13d-packages", "SELECT PKGSCHEMA,PKGNAME,BOUNDBY,PKG_CREATE_TIME,LASTUSED,TOTAL_SECT FROM SYSCAT.PACKAGES ORDER BY LASTUSED DESC"),
        Query("P13", "13e-package-statements", "SELECT PKGSCHEMA,PKGNAME,SECTNO,TEXT FROM SYSCAT.STATEMENTS ORDER BY PKGSCHEMA,PKGNAME,SECTNO", True),
        Query("P13", "13f-cross-schema-table-deps", f"SELECT DISTINCT D.TABSCHEMA DEPENDENT_SCHEMA,D.TABNAME DEPENDENT_OBJECT,D.DTYPE,D.BSCHEMA APP_SCHEMA,D.BNAME APP_OBJECT FROM SYSCAT.TABDEP D WHERE {schema_pred('D.BSCHEMA', schemas)} AND D.TABSCHEMA NOT IN ({', '.join(repr(x) for x in schemas)}) ORDER BY 1,2"),
        Query("P13", "13f-cross-schema-routine-deps", f"SELECT DISTINCT R.ROUTINESCHEMA,R.ROUTINENAME,R.BSCHEMA,R.BNAME FROM SYSCAT.ROUTINEDEP R WHERE {schema_pred('R.BSCHEMA', schemas)} AND R.ROUTINESCHEMA NOT IN ({', '.join(repr(x) for x in schemas)})", True),
        Query("P13", "13g-current-sql", "SELECT APPLICATION_NAME,SESSION_AUTH_ID,CLIENT_IPADDR,CLIENT_APPLNAME FROM SYSIBMADM.MON_CURRENT_SQL", True, "Run repeatedly at peak; monitoring authority required"),
        Query("P13", "13g-connection-summary", "SELECT APPLICATION_NAME,PRIMARY_AUTH_ID,CLIENT_IPADDR,CLIENT_PRDID,CONNECTION_START_TIME FROM SYSIBMADM.MON_CONNECTION_SUMMARY", True, "Run repeatedly at peak"),
        Query("P14", "14-dbauth", "SELECT * FROM SYSCAT.DBAUTH"),
    ]
    return base


def validate_query_pack(all_queries: list[Query]) -> dict:
    """Compare exact identifiers, not only totals, against the manual checklist."""
    phase_counts: dict[str, int] = {}
    for query in all_queries:
        phase_counts[query.phase] = phase_counts.get(query.phase, 0) + 1
    slugs = [query.slug for query in all_queries]
    duplicate_slugs = sorted({slug for slug in slugs if slugs.count(slug) > 1})
    expected_document = set().union(*DOCUMENT_FIXED_SLUGS.values())
    expected_supplemental = set().union(*SUPPLEMENTAL_FIXED_SLUGS.values())
    expected = expected_document | expected_supplemental
    actual = set(slugs)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    expected_by_phase = {}
    for phase in set(DOCUMENT_FIXED_SLUGS) | set(SUPPLEMENTAL_FIXED_SLUGS):
        expected_by_phase[phase] = len(DOCUMENT_FIXED_SLUGS.get(phase, set()) | SUPPLEMENTAL_FIXED_SLUGS.get(phase, set()))
    report = {
        "status": "ok" if not duplicate_slugs and not missing and not unexpected else "error",
        "document_fixed_queries": len(expected_document),
        "supplemental_fixed_queries": len(expected_supplemental),
        "expected_fixed_queries": len(expected),
        "actual_fixed_queries": len(all_queries),
        "expected_by_phase": expected_by_phase,
        "actual_by_phase": phase_counts,
        "duplicate_slugs": duplicate_slugs,
        "missing_query_ids": missing,
        "unexpected_query_ids": unexpected,
        "document_query_ids_by_phase": {k: sorted(v) for k, v in DOCUMENT_FIXED_SLUGS.items()},
        "supplemental_query_ids_by_phase": {k: sorted(v) for k, v in SUPPLEMENTAL_FIXED_SLUGS.items()},
        "dynamic_document_query": {"P12": "SELECT 'TABLE_NAME' AS T, COUNT(*) FROM MYSCHEMA.TABLE_NAME; generated for up to 50 tables"},
    }
    if report["status"] != "ok":
        raise RuntimeError(f"Phase B query-pack coverage validation failed: {json.dumps(report, sort_keys=True)}")
    return report


def connect(args):
    if ibm_db is None:
        raise RuntimeError("Missing dependency 'ibm_db'. Install with: pip install ibm_db")
    dsn = args.dsn or os.getenv("DB2_DSN", "")
    user = args.user or os.getenv("DB2_USER", "<DB2_USER>")
    password = args.password or os.getenv("DB2_PASSWORD", "<DB2_PASSWORD>")
    if not dsn:
        host = os.getenv("DB2_HOST", "<DB2_HOST>")
        port = os.getenv("DB2_PORT", "50000")
        db = os.getenv("DB2_DATABASE", "<DB2_DATABASE>")
        protocol = os.getenv("DB2_PROTOCOL", "TCPIP")
        dsn = f"DATABASE={db};HOSTNAME={host};PORT={port};PROTOCOL={protocol};UID={user};PWD={password};"
        return ibm_db.connect(dsn, "", "")
    return ibm_db.connect(dsn, user, password)


def execute(conn, sql: str) -> tuple[list[str], list[dict]]:
    stmt = ibm_db.exec_immediate(conn, sql)
    cols = [ibm_db.field_name(stmt, i) for i in range(ibm_db.num_fields(stmt))]
    rows = []
    while True:
        row = ibm_db.fetch_assoc(stmt)
        if not row:
            break
        rows.append({c: row.get(c) for c in cols})
    return cols, rows


def quote_identifier(value: str) -> str:
    """Quote a catalog-provided DB2 identifier; never accept user SQL here."""
    return '"' + value.replace('"', '""') + '"'


def execute_actual_counts(conn, tables: list[dict], out: Path) -> tuple[list[dict], list[dict]]:
    """Run the P12 COUNT(*) template for each of the catalog-selected top 50 tables."""
    rows: list[dict] = []
    errors: list[dict] = []
    total = len(tables)
    for index, table in enumerate(tables, 1):
        schema = str(table.get("TABSCHEMA", ""))
        name = str(table.get("TABNAME", ""))
        sql = f"SELECT COUNT(*) AS ACTUAL_ROWS FROM {quote_identifier(schema)}.{quote_identifier(name)}"
        slug = f"12-count-{schema}-{name}"
        LOG.info("START COUNT [%d/%d] %s.%s", index, total, schema, name)
        LOG.debug("SQL [%s]:\n%s;", slug, sql)
        try:
            _, result = execute(conn, sql)
            actual = result[0].get("ACTUAL_ROWS") if result else None
            rows.append({"TABSCHEMA": schema, "TABNAME": name, "EST_ROWS": table.get("EST_ROWS"), "ACTUAL_ROWS": actual, "STATUS": "ok", "ERROR": ""})
            LOG.info("DONE COUNT  [%d/%d] %s.%s | actual_rows=%s", index, total, schema, name, actual)
        except Exception as exc:
            error = str(exc)
            rows.append({"TABSCHEMA": schema, "TABNAME": name, "EST_ROWS": table.get("EST_ROWS"), "ACTUAL_ROWS": "", "STATUS": "error", "ERROR": error})
            errors.append({"slug": slug, "optional": False, "error": error})
            LOG.exception("FAILED COUNT [%d/%d] %s.%s | error=%s", index, total, schema, name, error)
    write_csv(out / "12-actual-row-counts.csv", ["TABSCHEMA", "TABNAME", "EST_ROWS", "ACTUAL_ROWS", "STATUS", "ERROR"], rows)
    return rows, errors


def write_csv(path: Path, cols: list[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: "" if v is None else v for k, v in row.items()})


def build_environment_md(out: Path, results: dict[str, list[dict]], errors: list[dict]) -> None:
    version = (results.get("E1-query-1-of-3-version-fixpack") or [{}])[0]
    topology = (results.get("E1-query-2-of-3-instance-topology") or [{}])[0]
    cfg = {str(r.get("NAME", "")).lower(): r.get("VALUE") for r in results.get("E3-query-1-of-1-dbcfg", [])}
    regs = {str(r.get("NAME", "")): r.get("VALUE") for r in results.get("E4-query-1-of-2-reg-variables-filtered", [])}
    text = ["# DB2 LUW environment profile", "", f"Generated: {datetime.now(timezone.utc).isoformat()}", "",
            f"- Service level: {version.get('SERVICE_LEVEL', 'NOT DETERMINED')}",
            f"- Fixpack: {version.get('FIXPACK_NUM', 'NOT DETERMINED')}",
            f"- Partitions: {topology.get('NUM_DBPARTITIONS', 'NOT DETERMINED')}",
            f"- Codeset/codepage: {cfg.get('codeset', 'NOT DETERMINED')} / {cfg.get('codepage', 'NOT DETERMINED')}",
            f"- Territory/collation: {cfg.get('territory', 'NOT DETERMINED')} / {cfg.get('collate_info', 'NOT DETERMINED')}",
            f"- string_units: {cfg.get('string_units', 'NOT DETERMINED')}",
            f"- cur_commit: {cfg.get('cur_commit', 'NOT DETERMINED')}",
            f"- DB2_COMPATIBILITY_VECTOR: {regs.get('DB2_COMPATIBILITY_VECTOR', 'NOT DETERMINED')}", "",
            "## Version-gated features", "", "Confirm from SERVICE_LEVEL: temporal/RCAC 10.1+, monitoring 9.7+, BOOLEAN 11.1+, BLU 10.5+.", "",
            "## Queries not completed", ""]
    text += [f"- {e['slug']}: {e['error']}" for e in errors] or ["- None"]
    out.write_text("\n".join(text) + "\n", encoding="utf-8")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--schemas", default=os.getenv("DB2_SCHEMAS", "MYSCHEMA"), help="Comma-separated DB2 schemas")
    p.add_argument("--output", default="assessment")
    p.add_argument("--dsn", default="", help="DB2 cataloged DSN; alternatively use DB2_HOST/PORT/DATABASE")
    p.add_argument("--user", default="")
    p.add_argument("--password", default="", help="Prefer DB2_PASSWORD environment variable")
    p.add_argument("--phases", default="E,P9,P10,P11,P12,P13,P14")
    p.add_argument("--dry-run", action="store_true", help="Write rendered SQL only; no DB connection")
    p.add_argument("--log-file", default="", help="Log path; default: <output>/00-extraction.log")
    p.add_argument("--verbose", action="store_true", help="Show full SQL in the console as well as the log file")
    p.add_argument("--skip-actual-counts", action="store_true", help="Skip P12 COUNT(*) for the top 50 tables (may be expensive)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    schemas = [x.strip().upper() for x in args.schemas.split(",") if x.strip()]
    if not schemas or any(not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", x) for x in schemas):
        raise SystemExit("Invalid --schemas; use comma-separated unquoted DB2 identifiers")
    selected = {x.strip().upper() for x in args.phases.split(",")}
    all_queries = queries(schemas)
    coverage = validate_query_pack(all_queries)
    qs = [q for q in all_queries if q.phase.upper() in selected]
    out = Path(args.output).resolve(); out.mkdir(parents=True, exist_ok=True)
    log_file = Path(args.log_file).resolve() if args.log_file else out / "00-extraction.log"
    configure_logging(log_file, args.verbose)
    (out / "00-query-coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    LOG.info("Query-pack coverage verified | fixed_queries=%d | E4_queries=2 | dynamic_P12_counts=up_to_50", coverage["actual_fixed_queries"])
    LOG.info("Starting DB2 Phase B extraction | schemas=%s | phases=%s | dry_run=%s", ",".join(schemas), ",".join(sorted(selected)), args.dry_run)
    (out / "00-query-plan.sql").write_text("\n\n".join(f"-- {q.phase} {q.slug}\n{q.sql};" for q in qs) + "\n", encoding="utf-8")
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "schemas": schemas, "dry_run": args.dry_run, "queries": []}
    if args.dry_run:
        for index, q in enumerate(qs, 1):
            LOG.info("DRY-RUN [%d/%d] %s %s", index, len(qs), q.phase, q.slug)
            LOG.debug("SQL [%s]:\n%s;", q.slug, q.sql)
            manifest["queries"].append({"phase": q.phase, "slug": q.slug, "optional": q.optional, "note": q.note, "status": "planned"})
        if "P12" in selected and not args.skip_actual_counts:
            manifest["queries"].append({"phase": "P12", "slug": "12-actual-row-counts", "status": "planned_dynamic", "detail": "One quoted COUNT(*) per row returned by 12-top-50-tables"})
        (out / "00-extraction-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        LOG.info("Dry run completed | query_plan=%s | queries=%d", out / "00-query-plan.sql", len(qs))
        return 0
    LOG.info("Opening DB2 connection (connection parameters are not logged)")
    conn = connect(args); results: dict[str, list[dict]] = {}; errors = []
    LOG.info("DB2 connection established")
    try:
        for index, q in enumerate(qs, 1):
            item = {"phase": q.phase, "slug": q.slug, "optional": q.optional, "note": q.note}
            LOG.info("START [%d/%d] %s %s | optional=%s", index, len(qs), q.phase, q.slug, q.optional)
            LOG.debug("SQL [%s]:\n%s;", q.slug, q.sql)
            try:
                cols, rows = execute(conn, q.sql)
                write_csv(out / f"{q.slug}.csv", cols, rows)
                results[q.slug] = rows
                item.update(status="ok", rows=len(rows), file=f"{q.slug}.csv")
                LOG.info("DONE  [%d/%d] %s | rows=%d | file=%s", index, len(qs), q.slug, len(rows), out / f"{q.slug}.csv")
            except Exception as exc:
                err = str(exc).replace(args.password, "<REDACTED>") if args.password else str(exc)
                item.update(status="error", error=err); errors.append({"slug": q.slug, "optional": q.optional, "error": err})
                LOG.exception("FAILED [%d/%d] %s | optional=%s | error=%s", index, len(qs), q.slug, q.optional, err)
            manifest["queries"].append(item)
        top_tables = results.get("12-top-50-tables", [])
        if "P12" in selected and top_tables and not args.skip_actual_counts:
            LOG.info("Starting P12 actual row counts for %d catalog-selected tables", len(top_tables))
            count_rows, count_errors = execute_actual_counts(conn, top_tables, out)
            errors.extend(count_errors)
            manifest["queries"].append({
                "phase": "P12", "slug": "12-actual-row-counts", "optional": False,
                "status": "error" if count_errors else "ok", "rows": len(count_rows),
                "failed_tables": len(count_errors), "file": "12-actual-row-counts.csv",
            })
        elif "P12" in selected and args.skip_actual_counts:
            LOG.warning("P12 actual COUNT(*) queries skipped by --skip-actual-counts")
            manifest["queries"].append({"phase": "P12", "slug": "12-actual-row-counts", "status": "skipped", "reason": "--skip-actual-counts"})
    finally:
        ibm_db.close(conn)
        LOG.info("DB2 connection closed")
    build_environment_md(out / "00-environment.md", results, errors)
    (out / "00-extraction-manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    LOG.info("Extraction completed | successful=%d | failed=%d | log=%s", sum(q.get("status") == "ok" for q in manifest["queries"]), len(errors), log_file)
    return 2 if any(not e["optional"] for e in errors) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if os.getenv("ASSESSMENT_DEBUG") == "1": traceback.print_exc()
        raise SystemExit(1)
