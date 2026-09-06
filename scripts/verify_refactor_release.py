"""Execute the frozen offline refactor matrix and inventory real-resource gaps."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MODULE = re.compile(r"tests\.test_[a-z0-9_]+\Z")
TOKEN = re.compile(r"[a-z0-9][a-z0-9.-]{1,95}\Z")
GATE_FIELDS = {"server_identity","maintenance_window","gpu_uuids","instance_ids","release_digests",
               "asset_revisions","legal_input_fixtures","task_count","phase_timeouts","total_timeout",
               "disk_limit","ram_limit","vram_limit","download_budget","rollback_actions"}


class VerificationError(ValueError): pass


def require(condition, code):
    if not condition: raise VerificationError(code)


def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)


def load_plan(path):
    value=json.loads(path.read_text(encoding="utf-8"))
    require(type(value) is dict and set(value)=={"schema","release_scope","candidate_manifest","offline_suites","model_reports","real_resource_gate"}
            and value["schema"]==1 and value["release_scope"]=="MC-026..MC-044","release_plan_invalid")
    require(value["candidate_manifest"]=="docs/project-control/evidence/refactor/mc043-final-candidate-sha256.json",
            "candidate_manifest_invalid")
    suites=value["offline_suites"]
    require(type(suites) is list and [item.get("id") for item in suites]==["faults","security","multiserver"],"offline_suites_invalid")
    modules=[]
    for item in suites:
        require(type(item) is dict and set(item)=={"id","modules","timeout_seconds"}
                and type(item["modules"]) is list and item["modules"]
                and type(item["timeout_seconds"]) is int and 10<=item["timeout_seconds"]<=1800,"offline_suite_invalid")
        require(all(type(module) is str and MODULE.fullmatch(module) for module in item["modules"]),"offline_module_invalid")
        modules.extend(item["modules"])
    require(len(modules)==len(set(modules)),"offline_module_duplicate")
    reports=value["model_reports"]
    require(type(reports) is list and len(reports)==14 and len(reports)==len(set(reports))
            and all(type(item) is str and TOKEN.fullmatch(item) for item in reports),"model_report_matrix_invalid")
    gate=value["real_resource_gate"]
    require(type(gate) is dict and set(gate)=={"status","requires","forbids"} and gate["status"]=="unprepared"
            and set(gate["requires"])==GATE_FIELDS and set(gate["forbids"])=={"stop_external_process","disconnect_shared_redis","infer_gpu_ownership"},
            "real_resource_gate_invalid")
    return value


def model_inventory(plan, governance_root):
    items=[]
    for model in plan["model_reports"]:
        path=(governance_root/"docs/project-control/evidence/refactor"/(model+".json")).resolve()
        require(path.is_relative_to(governance_root.resolve()),"model_report_path_invalid")
        if not path.is_file(): items.append({"model":model,"state":"missing"});continue
        try: report=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,UnicodeError,json.JSONDecodeError): items.append({"model":model,"state":"invalid"});continue
        results=report.get("results") if type(report) is dict else None
        state="documented" if type(results) is list and results else "invalid"
        items.append({"model":model,"state":state,"has_not_run":state=="documented" and any(x.get("result")=="not_run" for x in results if type(x) is dict)})
    return items


def write_exclusive(root, name, value):
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=(root/name).resolve();require(path.parent==root.resolve(),"evidence_path_invalid")
    with path.open("x",encoding="utf-8",newline="\n") as stream:
        stream.write(canonical(value)+"\n");stream.flush();os.fsync(stream.fileno())


def execute(plan, governance_root, evidence):
    manifest=governance_root/plan["candidate_manifest"]
    require(manifest.is_file(),"candidate_manifest_missing")
    candidate=json.loads(manifest.read_text(encoding="utf-8"))
    require(type(candidate) is dict and re.fullmatch(r"[0-9a-f]{64}",candidate.get("candidate_digest",""))
            and not candidate.get("errors"),"candidate_manifest_invalid")
    results=[]
    for suite in plan["offline_suites"]:
        started=time.monotonic()
        command=[sys.executable,"-B","-m","unittest",*suite["modules"]]
        run=subprocess.run(command,cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,
                           timeout=suite["timeout_seconds"],shell=False)
        summary=re.search(r"Ran (\d+) tests? in [0-9.]+s",run.stderr)
        skipped=re.search(r"OK \(skipped=(\d+)\)",run.stderr)
        item={"id":suite["id"],"exit_code":run.returncode,"seconds":round(time.monotonic()-started,3),
              "ran":int(summary.group(1)) if summary else None,"skipped":int(skipped.group(1)) if skipped else 0,
              "stdout_bytes":len(run.stdout.encode()),"stderr_bytes":len(run.stderr.encode()),
              "stdout_sha256":hashlib.sha256(run.stdout.encode()).hexdigest(),
              "stderr_sha256":hashlib.sha256(run.stderr.encode()).hexdigest()}
        write_exclusive(evidence,suite["id"]+".json",item);results.append(item)
        require(run.returncode==0,"offline_suite_failed_"+suite["id"])
    models=model_inventory(plan,governance_root)
    result={"schema":1,"candidate_digest":candidate["candidate_digest"],"offline_suites":results,"model_reports":models,
            "model_documents_complete":all(x["state"]=="documented" for x in models),
            "real_resource_checks":"not_run","real_resource_gate":plan["real_resource_gate"]}
    write_exclusive(evidence,"result.json",result)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--plan",type=Path,required=True)
    parser.add_argument("--governance-root",type=Path);parser.add_argument("--evidence",type=Path)
    mode=parser.add_mutually_exclusive_group(required=True);mode.add_argument("--verify-only",action="store_true");mode.add_argument("--execute",action="store_true")
    args=parser.parse_args(argv);plan=load_plan(args.plan.resolve())
    if args.verify_only:
        print(canonical({"status":"plan_valid","offline_execution":"not_run","real_resource_checks":"not_run"}));return 0
    require(args.governance_root is not None and args.evidence is not None,"execution_inputs_missing")
    print(canonical(execute(plan,args.governance_root.resolve(),args.evidence.resolve())));return 0


if __name__=="__main__": raise SystemExit(main())
