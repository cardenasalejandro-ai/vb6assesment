#!/usr/bin/env python3
"""Static VB6 repository assessment (Phase A P1-P8).

Uses only Python's standard library. Results are evidence-oriented heuristics;
binary Crystal Reports and runtime/build facts still require manual validation.
"""
from __future__ import annotations

import argparse, csv, hashlib, json, os, re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

EXT_TYPES = {
    ".vbg":"ProjectGroup", ".vbp":"Project", ".frm":"Form", ".frx":"FormResource",
    ".bas":"Module", ".cls":"Class", ".ctl":"UserControl", ".ctx":"ControlResource",
    ".dsr":"Designer", ".dsx":"DesignerResource", ".pag":"PropertyPage", ".pgx":"PropertyResource",
    ".res":"Resource", ".dob":"ActiveXDocument", ".dca":"ActiveXDocumentResource",
    ".rpt":"CrystalReport", ".dll":"Binary", ".ocx":"Binary", ".exe":"Binary",
    ".ini":"Config", ".cfg":"Config", ".sql":"SQL", ".bat":"Batch", ".cmd":"Batch"
}
TEXT_EXTS = {e for e,t in EXT_TYPES.items() if t not in {"Binary","FormResource","ControlResource","DesignerResource","PropertyResource","Resource","CrystalReport"}}
CODE_EXTS = {".frm",".ctl",".dsr",".pag",".bas",".cls",".dob"}
PROC_START = re.compile(r"^\s*(?:(Public|Private|Friend|Static)\s+)?(Sub|Function|Property\s+(?:Get|Let|Set))\s+([A-Za-z_]\w*)\s*(?:\((.*?)\))?", re.I)
PROC_END = re.compile(r"^\s*End\s+(Sub|Function|Property)\b", re.I)

def read_text(p: Path) -> str:
    raw=p.read_bytes()
    for enc in ("utf-8-sig","cp1252","latin-1"):
        try:return raw.decode(enc)
        except UnicodeDecodeError:pass
    return raw.decode("latin-1",errors="replace")

def csv_write(path: Path, fields, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)

def project_refs(vbp: Path):
    refs=[]
    for line in read_text(vbp).splitlines():
        m=re.match(r"\s*(Form|Module|Class|UserControl|PropertyPage|Designer|ResFile32|RelatedDoc)\s*=\s*(.*)",line,re.I)
        if m:
            val=m.group(2).split(";")[-1].strip().strip('"');refs.append(val.replace("\\",os.sep))
    return refs

def regions(path: Path):
    lines=read_text(path).splitlines(); ext=path.suffix.lower()
    if ext in {".bas",".cls"}:
        i=0
        while i<len(lines) and (not lines[i].strip() or lines[i].lstrip().lower().startswith(("version ","attribute "))): i+=1
        return [],lines[i:]
    # Attribute VB_Name is the reliable boundary after nested designer End blocks.
    idx=next((i for i,x in enumerate(lines) if x.lstrip().lower().startswith("attribute vb_name")),len(lines))
    return lines[:idx],lines[idx:]

def effective(lines):
    blank=sum(not x.strip() for x in lines)
    comments=sum(bool(re.match(r"^\s*(?:'|Rem\b)",x,re.I)) for x in lines)
    return len(lines)-blank-comments,blank,comments

def parse_procs(rel, module_type, code):
    out=[]; i=0
    while i<len(code):
        m=PROC_START.match(code[i])
        if not m:i+=1;continue
        start=i;i+=1
        while i<len(code) and not PROC_END.match(code[i]):i+=1
        body=code[start:i+1]; joined="\n".join(body); eff,_,_=effective(body)
        params=[x for x in (m.group(4) or "").split(",") if x.strip()]
        complexity=1+sum(len(re.findall(p,joined,re.I|re.M)) for p in [r"^\s*If\b",r"^\s*ElseIf\b",r"^\s*For(?:\s+Each)?\b",r"^\s*Do\b",r"^\s*While\b",r"^\s*Case\b",r"\bAnd\b",r"\bOr\b",r"\bIIf\s*\(",r"\bOn\s+Error\s+GoTo\b"])
        nesting=0;maxnest=0
        for ln in body:
            if re.match(r"^\s*(If\b.*Then\s*$|For\b|For\s+Each\b|Do\b|While\b|Select\s+Case\b|With\b)",ln,re.I):nesting+=1;maxnest=max(maxnest,nesting)
            if re.match(r"^\s*(End\s+If|Next\b|Loop\b|Wend\b|End\s+Select|End\s+With)",ln,re.I):nesting=max(0,nesting-1)
        on_resume=bool(re.search(r"\bOn\s+Error\s+Resume\s+Next\b",joined,re.I));on_goto=bool(re.search(r"\bOn\s+Error\s+GoTo\b",joined,re.I))
        declared=set(re.findall(r"\b(?:Dim|Private|Public|Static)\s+([A-Za-z_]\w*)",joined,re.I))
        sql_count=len(re.findall(r"(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE|CALL|CREATE\s+(?:TABLE|VIEW))\b",joined,re.I))
        name=m.group(3); kind=m.group(2).title(); event=bool(re.search(r"_(?:Click|Change|Load|Unload|Initialize|Terminate|KeyDown|KeyPress|KeyUp|MouseDown|MouseUp|GotFocus|LostFocus|Timer)$",name,re.I))
        out.append(dict(file=rel,module_name=Path(rel).stem,module_type=module_type,proc_name=name,proc_kind=kind,scope=(m.group(1) or "Public").title(),is_event_handler=str(event).lower(),parameter_count=len(params),has_optional_params=str(any(re.search(r"\bOptional\b",p,re.I) for p in params)).lower(),has_paramarray=str(any(re.search(r"\bParamArray\b",p,re.I) for p in params)).lower(),has_byref_params=str(any(not re.search(r"\bByVal\b",p,re.I) for p in params)).lower(),effective_lines=eff,cyclomatic_complexity=complexity,max_nesting_depth=maxnest,declared_variables=len(declared),undeclared_variables="REQUIRES_SEMANTIC_PARSER",error_handling="Mixed" if on_resume and on_goto else "OnErrorResumeNext" if on_resume else "OnErrorGoto" if on_goto else "None",uses_gosub=str(bool(re.search(r"\bGoSub\b",joined,re.I))).lower(),uses_goto=str(bool(re.search(r"\bGoTo\b",joined,re.I))).lower(),uses_doevents=str(bool(re.search(r"\bDoEvents\b",joined,re.I))).lower(),distinct_procs_called=len(set(re.findall(r"\b([A-Za-z_]\w*)\s*\(",joined))),sql_statements_embedded=sql_count,line=start+1,body=joined))
    return out

CONSTRUCTS={
"Variant / implicit variable":r"\b(?:As\s+Variant|Dim\s+\w+\s*(?:,|$))","As Object late binding":r"\bAs\s+Object\b","Control array":r"\bIndex\s+As\s+Integer\b|\b(?:Load|Unload)\s+\w+\s*\(","Fixed-length string":r"\bString\s*\*\s*\d+","Currency":r"\bAs\s+Currency\b","User-defined Type":r"^\s*(?:Public|Private)?\s*Type\s+\w+","GoSub":r"\bGoSub\b","On Error Resume Next":r"\bOn\s+Error\s+Resume\s+Next\b","DoEvents":r"\bDoEvents\b","Static local":r"^\s*Static\s+\w+","WithEvents":r"\bWithEvents\b","Implements":r"^\s*Implements\b","Conditional compilation":r"^\s*#If\b","Raw memory pointer":r"\b(?:ObjPtr|VarPtr|StrPtr|CopyMemory)\b","PrevInstance":r"\bApp\.PrevInstance\b","Option Base 1 / non-zero bound":r"\bOption\s+Base\s+1\b|\b\d+\s+To\s+\d+\b","ReDim Preserve":r"\bReDim\s+Preserve\b","Custom Err.Raise":r"\bErr\.Raise\b|\bvbObjectError\b"
}
DEPS={"Create/GetObject":r"\b(?:CreateObject|GetObject)\s*\(","Win32 Declare":r"^\s*(?:Public|Private)?\s*Declare\s+(?:Sub|Function)","Registry":r"\b(?:GetSetting|SaveSetting|DeleteSetting|RegOpenKeyEx\w*)\b","File I/O":r"\b(?:Open\s+.+\s+For\s+(?:Input|Output|Append|Random|Binary)|FreeFile|FileCopy|Kill|Dir\$?)\b","INI API":r"\b(?:Get|Write)PrivateProfileString\b","Office automation":r"\b(?:Excel|Word|Outlook)\.Application\b","Messaging/network":r"\b(?:MAPI|CDO|Winsock|MSComm|Inet|MSXML|SOAP|MSMQ|MQSeries)\b","COM+/MTS":r"\b(?:ObjectContext|SetComplete|SetAbort)\b","Printer":r"\bPrinter\.\w+|\bPrinters\b","Subclassing":r"\bSetWindowLong\w*\b|\bGWL_WNDPROC\b"}

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("repo");ap.add_argument("--output",default="assessment");ap.add_argument("--exclude",action="append",default=[".git","assessment","node_modules","vendor"]);a=ap.parse_args()
    root=Path(a.repo).resolve();out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
    if not root.is_dir():raise SystemExit(f"Repository not found: {root}")
    files=[p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in EXT_TYPES and not any(x in p.relative_to(root).parts for x in a.exclude)]
    vbps=[p for p in files if p.suffix.lower()==".vbp"]; ownership=defaultdict(list); referenced=set()
    for v in vbps:
        for r in project_refs(v):
            target=(v.parent/r).resolve();referenced.add(target)
            ownership[target].append(str(v.relative_to(root)))
    inv=[]
    for p in files:
        stat=p.stat(); ext=p.suffix.lower(); lines=len(read_text(p).splitlines()) if ext in TEXT_EXTS else ""
        inv.append(dict(project=";".join(ownership[p.resolve()]) or "UNREFERENCED",file_path=str(p.relative_to(root)),extension=ext,artifact_type=EXT_TYPES[ext],bytes=stat.st_size,total_lines=lines,last_modified=datetime.fromtimestamp(stat.st_mtime,timezone.utc).isoformat()))
    csv_write(out/"01-file-inventory.csv",["project","file_path","extension","artifact_type","bytes","total_lines","last_modified"],inv)
    extc=Counter(r["extension"] for r in inv);extb=Counter();[extb.update({r["extension"]:r["bytes"]}) for r in inv]
    orphans=[r["file_path"] for r in inv if r["extension"] in CODE_EXTS and (root/r["file_path"]).resolve() not in referenced and r["extension"]!=".vbp"]
    ptypes=[]
    for v in vbps:
        typ=next((x.split("=",1)[1].strip() for x in read_text(v).splitlines() if x.lower().startswith("type=")),"UNKNOWN");ptypes.append(f"- {v.relative_to(root)}: {typ}")
    summary=["# File inventory summary","","## Counts and bytes by extension","", "| Extension | Count | Bytes |","|---|---:|---:|"]+[f"| {e} | {extc[e]} | {extb[e]} |" for e in sorted(extc)]+["","## Projects",""]+ptypes+["","## Orphan candidates",""]+([f"- {x}" for x in orphans] or ["- None"])+["","## Binary classification", "", "Classification as build INPUT/OUTPUT requires a successful R1 build; inventory rows are available in 01-file-inventory.csv."]
    (out/"01-file-inventory-summary.md").write_text("\n".join(summary)+"\n",encoding="utf-8")
    loc=[];control_global=defaultdict(lambda:[0,set()]);procs=[]; texts={}
    for p in files:
        if p.suffix.lower() not in CODE_EXTS:continue
        rel=str(p.relative_to(root));designer,code=regions(p);texts[rel]="\n".join(code);eff,blank,comment=effective(code)
        controls=re.findall(r"^\s*Begin\s+([\w.]+)\s+\w+", "\n".join(designer),re.I|re.M);depth=0;maxdepth=0
        for ln in designer:
            if re.match(r"^\s*Begin\s+",ln,re.I):depth+=1;maxdepth=max(maxdepth,depth)
            elif re.match(r"^\s*End\s*$",ln,re.I):depth=max(0,depth-1)
        for c in controls:control_global[c][0]+=1;control_global[c][1].add(rel)
        loc.append(dict(file=rel,designer_lines=len(designer),code_lines_total=len(code),code_lines_blank=blank,code_lines_comment=comment,code_lines_effective=eff,control_count=len(controls),max_nesting_depth=maxdepth))
        procs.extend(parse_procs(rel,EXT_TYPES[p.suffix.lower()],code))
    csv_write(out/"02-loc-split.csv",["file","designer_lines","code_lines_total","code_lines_blank","code_lines_comment","code_lines_effective","control_count","max_nesting_depth"],loc)
    csv_write(out/"02-controls-by-type.csv",["control_type","occurrences","distinct_files"],[dict(control_type=k,occurrences=v[0],distinct_files=len(v[1])) for k,v in sorted(control_global.items())])
    top=sorted(loc,key=lambda x:x["code_lines_effective"],reverse=True)[:20]; complex_forms=[x for x in loc if x["control_count"]>40]; gods=[x for x in loc if x["code_lines_effective"]>2000]
    (out/"02-loc-summary.md").write_text("# LOC summary\n\n"+f"- Effective code lines: {sum(x['code_lines_effective'] for x in loc)}\n- Designer lines: {sum(x['designer_lines'] for x in loc)}\n\n## Top 20 files\n\n"+"\n".join(f"- {x['file']}: {x['code_lines_effective']}" for x in top)+"\n\n## Forms over 40 controls\n\n"+("\n".join(f"- {x['file']}: {x['control_count']}" for x in complex_forms) or "- None")+"\n\n## Files over 2,000 effective LOC\n\n"+("\n".join(f"- {x['file']}: {x['code_lines_effective']}" for x in gods) or "- None")+"\n",encoding="utf-8")
    procfields=[x for x in procs[0].keys() if x!="body"] if procs else ["file","module_name","module_type","proc_name"]
    csv_write(out/"03-procedures.csv",procfields,procs)
    bands=Counter("1-5" if x["cyclomatic_complexity"]<=5 else "6-10" if x["cyclomatic_complexity"]<=10 else "11-20" if x["cyclomatic_complexity"]<=20 else "21-50" if x["cyclomatic_complexity"]<=50 else "51+" for x in procs)
    noexp=sum("option explicit" not in t.lower() for t in texts.values()); n=len(procs) or 1
    (out/"03-complexity-summary.md").write_text("# Procedure complexity summary\n\n"+"\n".join(f"- {b}: {bands[b]}" for b in ["1-5","6-10","11-20","21-50","51+"])+f"\n- Procedures without error handling: {100*sum(x['error_handling']=='None' for x in procs)/n:.1f}%\n- Procedures using On Error Resume Next: {100*sum(x['error_handling'] in ('OnErrorResumeNext','Mixed') for x in procs)/n:.1f}%\n- Modules missing Option Explicit: {noexp}\n\n## Top 30\n\n"+"\n".join(f"- {x['file']}::{x['proc_name']}: {x['cyclomatic_complexity']}" for x in sorted(procs,key=lambda x:x["cyclomatic_complexity"],reverse=True)[:30])+"\n",encoding="utf-8")
    hazards=[]
    for name,pat in CONSTRUCTS.items():
        counts={f:len(re.findall(pat,t,re.I|re.M)) for f,t in texts.items()};counts={k:v for k,v in counts.items() if v}
        hazards.append(dict(construct=name,occurrences=sum(counts.values()),distinct_files=len(counts),worst_files=";".join(f"{k}:{v}" for k,v in sorted(counts.items(),key=lambda x:x[1],reverse=True)[:5])))
    csv_write(out/"04-language-constructs.csv",["construct","occurrences","distinct_files","worst_files"],hazards)
    (out/"04-porting-hazards.md").write_text("# VB6 porting hazards\n\nCounts are lexical candidates and must be reviewed per occurrence.\n\n"+"\n".join(f"- {x['construct']}: {x['occurrences']} in {x['distinct_files']} files" for x in hazards)+"\n",encoding="utf-8")
    com=[]
    for v in vbps:
        for ln in read_text(v).splitlines():
            if ln.startswith("Reference=") or ln.startswith("Object="):
                kind=ln.split("=",1)[0];val=ln.split("=",1)[1];guid=(re.search(r"\{?([0-9A-F-]{36})\}?",val,re.I) or ["",""])[1]; fn=Path(re.split(r"[#;]",val)[-1].strip()).name
                com.append(dict(project=str(v.relative_to(root)),kind=kind,guid=guid,version="",file_name=fn,description=val,is_microsoft_standard="UNKNOWN",is_third_party="UNKNOWN",is_in_house="UNKNOWN",binary_present_in_repo=str(any(p.name.lower()==fn.lower() for p in files)).lower(),source_present_in_repo="UNKNOWN"))
    csv_write(out/"05-com-references.csv",["project","kind","guid","version","file_name","description","is_microsoft_standard","is_third_party","is_in_house","binary_present_in_repo","source_present_in_repo"],com)
    deps=[]
    for name,pat in DEPS.items():
        sites=[];total=0
        for f,t in texts.items():
            for m in re.finditer(pat,t,re.I|re.M):total+=1;sites.append(f"{f}:{t[:m.start()].count(chr(10))+1}")
        deps.append(dict(dependency=name,category=name,occurrences=total,distinct_files=len(set(x.split(':')[0] for x in sites)),example_call_sites=";".join(sites[:10])))
    csv_write(out/"05-external-dependencies.csv",["dependency","category","occurrences","distinct_files","example_call_sites"],deps)
    (out/"05-dependency-risk.md").write_text("# Dependency risk\n\nVendor status and Java/JS replacements require product identification and current vendor research.\n\n"+"\n".join(f"- {d['dependency']}: {d['occurrences']} candidate occurrences" for d in deps)+"\n",encoding="utf-8")
    screens=[]
    for x in loc:
        if Path(x["file"]).suffix.lower() not in {".frm",".ctl"}:continue
        full=read_text(root/x["file"]);name=(re.search(r"Attribute VB_Name\s*=\s*\"([^\"]+)",full,re.I) or ["",Path(x["file"]).stem])[1];caption=(re.search(r"^\s*Caption\s*=\s*\"([^\"]*)",full,re.I|re.M) or ["",""])[1]
        nav=sorted(set(re.findall(r"\b([A-Za-z_]\w*)\.(?:Show|Hide)\b|\bLoad\s+([A-Za-z_]\w*)",full,re.I)));nav=[a or b for a,b in nav]
        screens.append(dict(form_name=name,caption=caption,is_mdi_parent=str("MDIForm" in full).lower(),is_mdi_child=str(bool(re.search(r"^\s*MDIChild\s*=\s*-1",full,re.M))).lower(),is_modal_dialog="heuristic_review",control_count=x["control_count"],distinct_control_types=len(set(re.findall(r"^\s*Begin\s+([\w.]+)",full,re.I|re.M))),grid_controls=len(re.findall(r"\b(?:MSFlexGrid|DataGrid|TrueDBGrid)\b",full,re.I)),tab_controls=len(re.findall(r"\b(?:SSTab|TabStrip)\b",full,re.I)),data_bound_controls=len(re.findall(r"^\s*(?:DataSource|DataField)\s*=",full,re.I|re.M)),menu_item_count=len(re.findall(r"^\s*Begin\s+VB\.Menu\b",full,re.I|re.M)),toolbar_button_count=len(re.findall(r"\bToolbar\b",full,re.I)),event_handler_count=sum(p["is_event_handler"]=="true" for p in procs if p["file"]==x["file"]),effective_code_lines=x["code_lines_effective"],has_frx=str((root/Path(x["file"]).with_suffix(".frx")).exists()).lower(),navigation_targets=";".join(nav)))
    csv_write(out/"06-screens.csv",list(screens[0].keys()) if screens else ["form_name"],screens)
    edges=[]
    for srow in screens:
        for target in filter(None,srow["navigation_targets"].split(";")):edges.append(f'    "{srow["form_name"]}" --> "{target}"')
    (out/"06-screen-graph.md").write_text("# Screen navigation\n\n```mermaid\nflowchart TD\n"+("\n".join(edges) if edges else '    A["No static navigation found"]')+"\n```\n",encoding="utf-8")
    buckets=Counter("complex data-entry screen" if x["control_count"]>40 or x["grid_controls"]>2 else "simple dialog" if x["control_count"]<10 else "standard CRUD form" for x in screens)
    (out/"06-ui-summary.md").write_text("# UI summary\n\n"+"\n".join(f"- {k}: {v}" for k,v in buckets.items())+"\n",encoding="utf-8")
    sqlrows=[]
    for p in procs:
        for ln,line in enumerate(p["body"].splitlines(),p["line"]):
            if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CALL|CREATE|ALTER|DROP)\b",line,re.I):
                kind=(re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CALL|CREATE|ALTER|DROP)\b",line,re.I).group(1).upper()); sqlrows.append(dict(file=p["file"],procedure=p["proc_name"],line=ln,statement_kind="DDL" if kind in {"CREATE","ALTER","DROP"} else kind,is_dynamic=str("&" in line or "+" in line).lower(),is_parameterized=str("Parameters" in p["body"]).lower(),target_objects=";".join(re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE|CALL)\s+([\w.]+)",line,re.I)),statement_text=line.strip(),char_length=len(line.strip()),joins_count=len(re.findall(r"\bJOIN\b",line,re.I)),subqueries_count=max(0,len(re.findall(r"\bSELECT\b",line,re.I))-1),uses_db2_specific_syntax=str(bool(re.search(r"\bWITH\s+(?:UR|CS|RS|RR)\b|SYSIBM|FETCH\s+FIRST",line,re.I))).lower()))
    csv_write(out/"07-sql-inventory.csv",["file","procedure","line","statement_kind","is_dynamic","is_parameterized","target_objects","statement_text","char_length","joins_count","subqueries_count","uses_db2_specific_syntax"],sqlrows)
    api_counts={k:sum(len(re.findall(p,t,re.I)) for t in texts.values()) for k,p in {"ADO":r"\bADODB\.","RDO":r"\brdo\w*", "DAO":r"\bDAO\.","ODBC/CLI":r"\bSQL(?:Connect|ExecDirect|Prepare)\b","Data binding":r"\b(?:Adodc|DataEnvironment)\b"}.items()}
    (out/"07-data-access.md").write_text("# Data access layer\n\n## API candidate counts\n\n"+"\n".join(f"- {k}: {v}" for k,v in api_counts.items())+"\n\nConnection strings and credentials are intentionally not copied into this narrative; inspect source locations under controlled access and redact secrets.\n",encoding="utf-8")
    (out/"07-sql-summary.md").write_text(f"# SQL summary\n\n- Candidate statements: {len(sqlrows)}\n- Dynamic candidates: {sum(x['is_dynamic']=='true' for x in sqlrows)}\n- DB2-specific candidates: {sum(x['uses_db2_specific_syntax']=='true' for x in sqlrows)}\n\nThis lexical extraction must be reconciled with runtime SQL capture (R2).\n",encoding="utf-8")
    reports=[]
    for p in files:
        if p.suffix.lower() in {".rpt",".dsr"}:reports.append(dict(file=str(p.relative_to(root)),report_type="Crystal" if p.suffix.lower()==".rpt" else "DataReport",version="UNDETERMINED",sections="UNDETERMINED",database_fields="UNDETERMINED",formula_fields="UNDETERMINED",subreports="UNDETERMINED",data_source="UNDETERMINED",parameters_from_vb6="UNDETERMINED",parse_status="MANUAL_BINARY_REVIEW" if p.suffix.lower()==".rpt" else "TEXT_AVAILABLE"))
    csv_write(out/"08-reports.csv",["file","report_type","version","sections","database_fields","formula_fields","subreports","data_source","parameters_from_vb6","parse_status"],reports)
    batches=[str(p.relative_to(root)) for p in files if p.suffix.lower() in {".bat",".cmd"}]
    command_sites=[f for f,t in texts.items() if re.search(r"\bCommand\$?\b",t,re.I)]
    (out/"08-batch-and-reports.md").write_text(f"# Batch and reports\n\n- Reports inventoried: {len(reports)}\n- Binary reports requiring manual review: {sum(x['parse_status']=='MANUAL_BINARY_REVIEW' for x in reports)}\n\n## Batch wrappers\n\n"+("\n".join(f"- {x}" for x in batches) or "- None")+"\n\n## Command$ code paths\n\n"+("\n".join(f"- {x}" for x in command_sites) or "- None")+"\n",encoding="utf-8")
    manifest={"generated_at":datetime.now(timezone.utc).isoformat(),"repo":str(root),"file_count":len(files),"sha256":{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    (out/"00-source-scan-manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    print(f"Assessment written to {out}")
if __name__=="__main__":main()
