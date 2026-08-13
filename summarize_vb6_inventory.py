#!/usr/bin/env python3
"""Create an English Markdown summary from VB6 inventory CSV reports.

Uses only the Python standard library and never reads VB6 source files.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence


EXPECTED_FILES = [
    "01_Projects.csv", "02_Controls_by_Form.csv", "03_OCX_DLL_References.csv",
    "04_DLL_API_by_File.csv", "05_Missing_Files.csv", "06_Control_Summary.csv",
    "07_Runtime_COM.csv", "08_Control_Matrix_by_Form.csv",
    "09_Code_Complexity_by_File.csv", "10_Events_by_Form.csv",
    "11_SQL_Inventory.csv", "12_Integration_Inventory.csv",
    "13_Migration_Estimate.csv",
]

RISK_ORDER = {"Very High": 4, "High": 3, "Medium": 2, "Low": 1, "": 0}


def read_csv(folder: Path, name: str) -> List[Dict[str, str]]:
    path = folder / name
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def number(value: object) -> float:
    try:
        return float(str(value or "0").strip())
    except ValueError:
        return 0.0


def integer(value: object) -> int:
    return int(number(value))


def fmt(value: float, decimals: int = 1) -> str:
    return f"{value:,.{decimals}f}"


def esc(value: object) -> str:
    return str("" if value is None else value).replace("|", "\\|").replace("\n", " ").strip()


def md_table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> List[str]:
    result = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    result.extend("| " + " | ".join(esc(cell) for cell in row) + " |" for row in rows)
    return result


def top_counter(rows: Iterable[Mapping[str, str]], field: str, count_field: str = "Occurrences") -> Counter[str]:
    result: Counter[str] = Counter()
    for row in rows:
        key = row.get(field, "").strip()
        if key:
            result[key] += max(1, integer(row.get(count_field, "1")))
    return result


def generate_report(folder: Path, output: Path, person_day_hours: float, top_n: int) -> None:
    data = {name: read_csv(folder, name) for name in EXPECTED_FILES}
    projects = data["01_Projects.csv"]
    controls = data["02_Controls_by_Form.csv"]
    dependencies = data["03_OCX_DLL_References.csv"]
    missing = data["05_Missing_Files.csv"]
    complexity = data["09_Code_Complexity_by_File.csv"]
    events = data["10_Events_by_Form.csv"]
    sql = data["11_SQL_Inventory.csv"]
    integrations = data["12_Integration_Inventory.csv"]
    estimates = data["13_Migration_Estimate.csv"]

    found_files = [name for name in EXPECTED_FILES if (folder / name).is_file()]
    missing_reports = [name for name in EXPECTED_FILES if name not in found_files]
    lines: List[str] = [
        "# VB6 Modernization Inventory Summary",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Executive summary",
        "",
    ]

    forms = sum(integer(r.get("Forms")) for r in projects)
    modules = sum(integer(r.get("Modules")) for r in projects)
    classes = sum(integer(r.get("Classes")) for r in projects)
    total_controls = sum(1 for r in controls if r.get("ControlType") not in {"VB.Form", "VB.MDIForm", "VB.UserControl", "VB.PropertyPage"})
    total_code_lines = sum(integer(r.get("CodeLines")) for r in complexity)
    total_sql = sum(integer(r.get("Occurrences")) for r in sql)
    total_integrations = sum(integer(r.get("Occurrences")) for r in integrations)

    totals = {
        field: sum(number(r.get(field)) for r in estimates)
        for field in (
            "FrontendReactDays", "BackendSpringBootDays", "DatabaseAzureSQLDays",
            "TestingDays", "MinimumDays", "LikelyDays", "MaximumDays",
        )
    }
    lines.extend(md_table(
        ["Metric", "Value"],
        [
            ("Projects", len(projects)), ("Forms", forms), ("Modules", modules),
            ("Classes", classes), ("Controls inside forms", total_controls),
            ("Analyzed code lines", f"{total_code_lines:,}"),
            ("SQL occurrences", total_sql), ("Integration occurrences", total_integrations),
            ("Missing source files", len(missing)),
            ("Estimated forms", len(estimates)),
        ],
    ))

    lines.extend(["", "## Portfolio effort estimate", ""])
    if estimates:
        lines.extend(md_table(
            ["Workstream", "Person-days", f"Hours ({person_day_hours:g} h/day)"],
            [
                ("React frontend", fmt(totals["FrontendReactDays"]), fmt(totals["FrontendReactDays"] * person_day_hours)),
                ("Spring Boot backend", fmt(totals["BackendSpringBootDays"]), fmt(totals["BackendSpringBootDays"] * person_day_hours)),
                ("Azure SQL conversion", fmt(totals["DatabaseAzureSQLDays"]), fmt(totals["DatabaseAzureSQLDays"] * person_day_hours)),
                ("Testing", fmt(totals["TestingDays"]), fmt(totals["TestingDays"] * person_day_hours)),
                ("Likely total", fmt(totals["LikelyDays"]), fmt(totals["LikelyDays"] * person_day_hours)),
            ],
        ))
        lines.extend([
            "",
            f"Estimated portfolio range: **{fmt(totals['MinimumDays'])} to {fmt(totals['MaximumDays'])} person-days**; "
            f"the central estimate is **{fmt(totals['LikelyDays'])} person-days**.",
            "",
            "> Person-days measure effort, not calendar duration. Modules/classes without a form, shared platform work, "
            "physical data migration, DevOps, security, training, change management, and production cutover require separate estimates.",
        ])
    else:
        lines.append("`13_Migration_Estimate.csv` was not available or contained no rows; no portfolio effort total could be calculated.")

    lines.extend(["", "## Estimate by project", ""])
    by_project: Dict[str, Counter[str]] = defaultdict(Counter)
    for row in estimates:
        p = row.get("Project", "Unknown")
        for field in ("FrontendReactDays", "BackendSpringBootDays", "DatabaseAzureSQLDays", "TestingDays", "MinimumDays", "LikelyDays", "MaximumDays"):
            by_project[p][field] += number(row.get(field))
        by_project[p]["Forms"] += 1
    if by_project:
        lines.extend(md_table(
            ["Project", "Forms", "React", "Spring Boot", "Azure SQL", "Testing", "Minimum", "Likely", "Maximum"],
            [
                (p, integer(v["Forms"]), fmt(v["FrontendReactDays"]), fmt(v["BackendSpringBootDays"]),
                 fmt(v["DatabaseAzureSQLDays"]), fmt(v["TestingDays"]), fmt(v["MinimumDays"]),
                 fmt(v["LikelyDays"]), fmt(v["MaximumDays"]))
                for p, v in sorted(by_project.items(), key=lambda item: item[1]["LikelyDays"], reverse=True)
            ],
        ))
    else:
        lines.append("No project-level estimates were available.")

    lines.extend(["", "## Highest-effort forms", ""])
    top_effort = sorted(estimates, key=lambda r: number(r.get("LikelyDays")), reverse=True)[:top_n]
    if top_effort:
        lines.extend(md_table(
            ["Project/Form", "Likely days", "Maximum days", "Risk", "Confidence", "Primary drivers"],
            [
                (
                    r.get("Project_Form"), fmt(number(r.get("LikelyDays"))), fmt(number(r.get("MaximumDays"))),
                    r.get("RiskBand"), r.get("EstimateConfidence"),
                    f"{integer(r.get('ThirdPartyControls'))} third-party controls; "
                    f"{integer(r.get('BranchPoints'))} branches; {integer(r.get('SQLOccurrences'))} SQL; "
                    f"{integer(r.get('IntegrationOccurrences'))} integrations",
                )
                for r in top_effort
            ],
        ))
    else:
        lines.append("No form estimates were available.")

    lines.extend(["", "## Risk profile", ""])
    risk_counts = Counter(r.get("RiskBand", "Unknown") for r in estimates)
    confidence_counts = Counter(r.get("EstimateConfidence", "Unknown") for r in estimates)
    lines.extend(md_table(
        ["Risk band", "Forms"],
        [(risk, risk_counts.get(risk, 0)) for risk in ("Very High", "High", "Medium", "Low", "Unknown") if risk_counts.get(risk, 0)],
    ))
    lines.extend(["", "Estimate confidence: " + ", ".join(f"**{k}: {v}**" for k, v in sorted(confidence_counts.items())) + "."])

    high_risk = sorted(
        (r for r in estimates if RISK_ORDER.get(r.get("RiskBand", ""), 0) >= 3),
        key=lambda r: (RISK_ORDER.get(r.get("RiskBand", ""), 0), number(r.get("RiskScore"))), reverse=True,
    )[:top_n]
    if high_risk:
        lines.extend(["", "### High-risk forms", ""])
        lines.extend(md_table(
            ["Project/Form", "Risk score", "Risk band", "Likely days", "Confidence"],
            [(r.get("Project_Form"), r.get("RiskScore"), r.get("RiskBand"), r.get("LikelyDays"), r.get("EstimateConfidence")) for r in high_risk],
        ))

    lines.extend(["", "## Technical findings", ""])
    third_party = Counter(r.get("ControlType", "") for r in controls if r.get("Category") == "ActiveX/third-party or user control")
    db_objects = top_counter(sql, "DatabaseObject")
    db2_features = top_counter(sql, "DB2Features")
    technologies = top_counter(integrations, "Technology")
    dependency_files = Counter(r.get("File", "") for r in dependencies if r.get("File"))

    finding_sets = [
        ("Third-party controls", third_party), ("Database objects", db_objects),
        ("DB2-specific features", db2_features), ("Integration technologies", technologies),
        ("COM/OCX/DLL dependencies", dependency_files),
    ]
    for title, counts in finding_sets:
        lines.extend(["", f"### {title}", ""])
        if counts:
            lines.extend(md_table(["Item", "Occurrences"], counts.most_common(top_n)))
        else:
            lines.append("None detected in the available reports.")

    lines.extend(["", "## Source completeness and report coverage", ""])
    lines.extend(md_table(
        ["Coverage item", "Status"],
        [(name, "Available" if name in found_files else "Missing") for name in EXPECTED_FILES],
    ))
    if missing:
        lines.extend(["", "### Missing source files", ""])
        lines.extend(md_table(
            ["Project", "Type", "Missing file"],
            [(r.get("Project"), r.get("Type"), r.get("MissingFile")) for r in missing[: max(top_n, 20)]],
        ))
    if missing_reports:
        lines.extend(["", "> Summary completeness is reduced because these inventory reports were missing: " + ", ".join(f"`{x}`" for x in missing_reports) + "."])

    lines.extend([
        "", "## Recommended next actions", "",
        "1. Validate the highest-effort and highest-risk forms with business owners.",
        "2. Resolve missing source files before committing dates or budget.",
        "3. Prototype representative Low, Medium, High, and Very High forms.",
        "4. Record actual React, Spring Boot, Azure SQL, and testing person-days from the pilot.",
        "5. Calibrate the inventory coefficients and regenerate the estimate.",
        "6. Assess shared modules/classes and project-level dependencies that are not allocated to individual forms.",
        "7. Run a DB2 catalog, data-volume, compatibility, performance, and cutover assessment.",
        "8. Add cross-cutting work for architecture, security, DevOps, observability, data migration, training, and change management.",
        "", "## Estimation notice", "",
        "This is a preliminary parametric estimate based on static metrics and documented initial coefficients. "
        "It is not a contractual commitment or a statistically calibrated cost-estimating relationship. "
        "The coefficients must be calibrated with pilot results and historical delivery data.",
        "", "A person-day is an effort unit. This report uses the conversion configured at execution time "
        f"(**{person_day_hours:g} hours per person-day**) only for informational hour totals.",
    ])

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize VB6 inventory CSV reports into an English Markdown report.")
    parser.add_argument("inventory_folder", type=Path, help="Folder containing the inventory CSV files")
    parser.add_argument("output_markdown", nargs="?", type=Path, default=Path("VB6_Inventory_Summary.md"))
    parser.add_argument("--person-day-hours", type=float, default=8.0, help="Hours per person-day (default: 8)")
    parser.add_argument("--top", type=int, default=10, help="Number of top findings to show (default: 10)")
    args = parser.parse_args(argv)
    if not args.inventory_folder.is_dir():
        parser.error(f"The inventory folder does not exist: {args.inventory_folder}")
    if args.person_day_hours <= 0:
        parser.error("--person-day-hours must be greater than zero")
    if args.top <= 0:
        parser.error("--top must be greater than zero")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    generate_report(args.inventory_folder.resolve(), args.output_markdown.resolve(), args.person_day_hours, args.top)
    print(f"Markdown summary created: {args.output_markdown.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
