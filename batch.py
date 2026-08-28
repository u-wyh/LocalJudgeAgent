#!/usr/bin/env python3
"""Sequential, resumable LocalJudgeAgent benchmark runner."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = "gpt-oss:20b"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
MIN_FREE_BYTES = 512 * 1024 * 1024
FINAL_SAMPLE = {"SAMPLE_AC", "FAILED"}
FINAL_REAL = {"OJ_AC", "FAILED", "CAPTCHA_SKIPPED"}
REPAIRABLE = {"OJ_WA", "OJ_TLE", "OJ_RE", "OJ_CE", "OJ_MLE", "OJ_PC"}
MAX_OJ_REPAIRS = 3


class BatchSystemError(RuntimeError):
    pass


class LoginRequired(RuntimeError):
    pass


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path, value):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def relative(path):
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def load_benchmark(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    platform = data.get("platform")
    if platform not in {"codeforces", "luogu"} or not isinstance(data.get("problems"), list):
        raise ValueError("benchmark must contain a supported platform and problems list")
    pattern = r"CF\d+[A-Za-z][A-Za-z0-9]*" if platform == "codeforces" else r"P\d+"
    seen = set()
    for index, item in enumerate(data["problems"], 1):
        problem_id = item.get("id", "")
        if not re.fullmatch(pattern, problem_id, re.I):
            raise ValueError(f"invalid {platform} ID at position {index}: {problem_id}")
        normalized = problem_id.upper()
        if normalized in seen:
            raise ValueError(f"duplicate problem ID: {problem_id}")
        seen.add(normalized)
        if platform == "codeforces" and not isinstance(item.get("expected_rating"), int):
            raise ValueError(f"missing expected_rating for {problem_id}")
    return data


def ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=5) as response:
            return {x.get("name") or x.get("model") for x in json.load(response).get("models", [])}
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise BatchSystemError(f"Ollama is unavailable: {exc}") from exc


def system_preflight(real_oj=False):
    if MODEL not in ollama_models():
        raise BatchSystemError(f"required model is not installed: {MODEL}")
    if shutil.which("g++") is None:
        raise BatchSystemError("g++ is unavailable")
    if shutil.disk_usage(ROOT).free < MIN_FREE_BYTES:
        raise BatchSystemError("less than 512 MiB disk space remains")
    if real_oj:
        while True:
            checked = subprocess.run([sys.executable, "luogu_main.py", "--check-login"], cwd=ROOT,
                                     capture_output=True, text=True, check=False)
            if not checked.returncode and "Persistent login session confirmed" in checked.stdout:
                break
            print("=" * 60)
            print("LUOGU LOGIN REQUIRED")
            print("Log in using the persistent Luogu browser, then press Enter.")
            print("=" * 60)
            input()


def unique_batch_dir(name):
    base = ROOT / "batch_runs" / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    path, suffix = base, 1
    while path.exists():
        path, suffix = Path(f"{base}_{suffix}"), suffix + 1
    path.mkdir(parents=True)
    return path


def find_run_dir(output, problem_id, before):
    matches = re.findall(r"^\[Record\]\s+(.+)/record\.json\s*$", output, re.MULTILINE)
    if matches:
        return Path(matches[-1]).resolve()
    after = {p.resolve() for p in (ROOT / "runs").glob(f"{problem_id.upper()}_*") if p.is_dir()}
    created = sorted(after - before, key=lambda p: p.stat().st_mtime)
    return created[-1] if created else None


def invoke(batch_dir, order, problem_id, phase, args, interactive=False):
    stdout_path = batch_dir / f"{order:03d}_{problem_id}_{phase}.stdout.log"
    stderr_path = batch_dir / f"{order:03d}_{problem_id}_{phase}.stderr.log"
    command = [sys.executable, str(ROOT / "agent.py"), *map(str, args)]
    if interactive:
        result = subprocess.run(command, cwd=ROOT, text=True, check=False)
        result.stdout, result.stderr = "", ""
    else:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    stdout_path.write_text(result.stdout or "", encoding="utf-8")
    stderr_path.write_text(result.stderr or "", encoding="utf-8")
    with (batch_dir / "batch.log").open("a", encoding="utf-8") as log:
        log.write(f"\n===== [{order}] {problem_id} {phase} stdout =====\n{result.stdout}")
        log.write(f"\n===== [{order}] {problem_id} {phase} stderr =====\n{result.stderr}")
    return result, stdout_path, stderr_path


def read_agent_record(run_dir):
    return json.loads((run_dir / "record.json").read_text(encoding="utf-8"))


def latest_existing_run(problem_id):
    candidates = sorted((ROOT / "runs").glob(f"{problem_id.upper()}_*"))
    valid = [path for path in candidates if (path / "record.json").is_file()]
    return valid[-1] if valid else None


def verify_existing_run(problem_id):
    run_dir = latest_existing_run(problem_id)
    if not run_dir:
        return None, None, "RUN_MISSING"
    record = read_agent_record(run_dir)
    version = record.get("final_version")
    submission = run_dir / "submission.cpp"
    final_source = run_dir / f"main_{version}.cpp"
    if not version or not submission.is_file() or not final_source.is_file():
        return run_dir, None, "FINAL_SOURCE_MISSING"
    content = submission.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    recorded = record.get("oj", {}).get("submission_sha256") or record.get("submission_sha256")
    if content != final_source.read_bytes() or (recorded and digest != recorded):
        return run_dir, digest, "SUBMISSION_SHA256_MISMATCH"
    return run_dir, digest, None


def verify_existing_cf(benchmark, verbose=True):
    verified = []
    for item in benchmark["problems"]:
        run_dir, digest, error = verify_existing_run(item["id"])
        if verbose:
            print(f"{item['id']} {relative(run_dir) if run_dir else '-'} "
                  f"{'PASS' if not error else error} {digest or ''}")
        if error:
            raise ValueError(f"{item['id']}: {error}")
        verified.append((item, run_dir, digest))
    print(f"Integrity verified: {len(verified)}/{len(benchmark['problems'])}")
    return verified


def clone_captcha_retry(problem_id):
    candidates = sorted((ROOT / "runs").glob(f"{problem_id.upper()}_*"), reverse=True)
    source_run = next((path for path in candidates
                       if (path / "record.json").is_file()
                       and read_agent_record(path).get("final_status") == "CAPTCHA_SKIPPED"), None)
    if not source_run:
        return None
    record = read_agent_record(source_run)
    version = record.get("final_version")
    submission = source_run / "submission.cpp"
    final_source = source_run / f"main_{version}.cpp"
    if not submission.is_file() or not final_source.is_file() or submission.read_bytes() != final_source.read_bytes():
        raise ValueError(f"{problem_id}: SUBMISSION_SHA256_MISMATCH")
    base = ROOT / "runs" / f"{problem_id.upper()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_captcha_retry"
    retry_run, suffix = base, 1
    while retry_run.exists():
        retry_run, suffix = Path(f"{base}_{suffix}"), suffix + 1
    retry_run.mkdir(parents=True)
    for name in ("problem.json", f"main_{version}.cpp", "submission.cpp"):
        shutil.copy2(source_run / name, retry_run / name)
    record.update({"retry_of": relative(source_run), "started_at": now_iso(), "finished_at": None,
                   "final_status": "PREPARED_FOR_SUBMISSION", "failure_reason": None,
                   "oj": {"provider": "luogu-main", "submitted": False,
                          "submission_sha256": hashlib.sha256(submission.read_bytes()).hexdigest()},
                   "oj_history": [], "oj_repair_attempts": 0})
    atomic_json(retry_run / "record.json", record)
    print(f"[Retry] Reusing verified source from {relative(source_run)}")
    return retry_run


def run_existing_cf(path, benchmark, limit=None):
    verified = verify_existing_cf(benchmark)
    checked = subprocess.run([sys.executable, "codeforces_main.py", "--check-login"], cwd=ROOT,
                             capture_output=True, text=True, check=False)
    if checked.returncode:
        raise BatchSystemError(checked.stdout.strip() or checked.stderr.strip())
    selected = verified[:limit] if limit else verified
    batch_dir = unique_batch_dir(f"{benchmark['name']}_existing_submit")
    record = {"benchmark": benchmark["name"], "benchmark_file": relative(path),
              "platform": "codeforces", "mode": "submit_existing_cf",
              "started_at": now_iso(), "finished_at": None, "status": "RUNNING",
              "total": len(selected), "problems": []}
    atomic_json(batch_dir / "batch_record.json", record)
    for order, (item, run_dir, digest) in enumerate(selected, 1):
        print("=" * 60)
        print("CODEFORCES MANUAL SUBMIT REQUIRED")
        print(f"Problem: {item['id']}")
        print(f"Run: {relative(run_dir)}")
        print(f"Source SHA256: {digest}")
        print("Use the browser to complete anti-bot and click final Submit.")
        print("=" * 60)
        return_code = subprocess.call(
            [sys.executable, str(ROOT / "agent.py"), "--resume", str(run_dir), "--submit-cf"],
            cwd=ROOT)
        current = read_agent_record(run_dir)
        oj = current.get("oj", {})
        entry = {"order": order, "problem_id": item["id"],
                 "rating": current.get("rating", item.get("expected_rating")),
                 "run_dir": relative(run_dir), "submission_id": oj.get("submission_id"),
                 "language": oj.get("language"),
                 "source_verified": bool(oj.get("source_match")),
                 "raw_verdict": oj.get("raw_verdict", oj.get("raw_status")),
                 "final_status": current.get("final_status"),
                 "timeConsumedMillis": oj.get("timeConsumedMillis"),
                 "memoryConsumedBytes": oj.get("memoryConsumedBytes"),
                 "submitted": bool(oj.get("submission_confirmed")),
                 "return_code": return_code, "finished_at": now_iso()}
        record["problems"].append(entry)
        atomic_json(batch_dir / "batch_record.json", record)
        print(f"[{order}/{len(selected)}] {item['id']}: {entry['final_status']}")
    record.update({"status": "COMPLETE", "finished_at": now_iso()})
    atomic_json(batch_dir / "batch_record.json", record)
    terminal = record["problems"]
    summary = {"total": len(terminal), "submitted": sum(x["submitted"] for x in terminal),
               "statuses": {status: sum(x["final_status"] == status for x in terminal)
                            for status in ("OJ_AC", "OJ_WA", "OJ_TLE", "OJ_RE", "OJ_CE")},
               "skipped": sum(x["final_status"] == "OJ_SKIPPED" for x in terminal),
               "timeout": sum(x["final_status"] in {"OJ_UNKNOWN", "MANUAL_SUBMISSION_TIMEOUT"}
                              for x in terminal),
               "by_rating": {str(rating): {"problems": len([x for x in terminal if x["rating"] == rating]),
                   "oj_ac": sum(x["final_status"] == "OJ_AC" for x in terminal if x["rating"] == rating)}
                   for rating in (800, 900, 1000, 1100, 1200)}}
    atomic_json(batch_dir / "batch_summary.json", summary)
    print("\nCodeforces existing-run summary")
    print(f"Total: {len(terminal)}")
    print(f"Submitted: {sum(x['submitted'] for x in terminal)}")
    for status in ("OJ_AC", "OJ_WA", "OJ_TLE", "OJ_RE", "OJ_CE"):
        print(f"{status}: {sum(x['final_status'] == status for x in terminal)}")
    print(f"Skipped: {sum(x['final_status'] == 'OJ_SKIPPED' for x in terminal)}")
    print(f"Timeout: {sum(x['final_status'] in {'OJ_UNKNOWN', 'MANUAL_SUBMISSION_TIMEOUT'} for x in terminal)}")
    for rating in (800, 900, 1000, 1100, 1200):
        group = [x for x in terminal if x["rating"] == rating]
        print(f"Rating {rating}: {sum(x['final_status'] == 'OJ_AC' for x in group)} / {len(group)} OJ_AC")
    return batch_dir


def make_result(order, item, run_dir, logs):
    result = {"order": order, "problem_id": item["id"], "status": "FAILED",
              "run_dir": relative(run_dir) if run_dir else None,
              "stdout_paths": [relative(x[0]) for x in logs],
              "stderr_paths": [relative(x[1]) for x in logs]}
    if not run_dir or not (run_dir / "record.json").is_file():
        result["failure_reason"] = "AGENT_RECORD_MISSING"
        return result
    record = read_agent_record(run_dir)
    result.update({
        "difficulty": record.get("difficulty") or "unknown",
        "rating": record.get("rating"),
        "first_generation_success": bool(record.get("first_generation_success")),
        "local_repairs": int(record.get("repair_attempts", 0)),
        "repair_attempts": int(record.get("repair_attempts", 0)),
        "oj_attempts": len(record.get("oj_history", [])),
        "oj_repairs": int(record.get("oj_repair_attempts", 0)),
        "oj_history": record.get("oj_history", []),
        "sample_ac": bool(record.get("final_sample_passed")),
        "SAMPLE_AC": bool(record.get("final_sample_passed")),
        "model_time_sec": float(record.get("model_time_sec", 0)),
        "total_time_sec": float(record.get("total_time_sec", 0)),
        "status": record.get("final_status", "FAILED"),
        "final_status": record.get("final_status", "FAILED"),
        "failure_reason": record.get("failure_reason"),
    })
    return result


def run_one(batch_dir, order, item, real_oj, existing=None):
    logs = []
    run_dir = ROOT / existing["run_dir"] if existing and existing.get("run_dir") else None
    if not run_dir or not (run_dir / "record.json").is_file():
        before = {p.resolve() for p in (ROOT / "runs").glob(f"{item['id'].upper()}_*")}
        args = [item["id"]] + (["--submit-main"] if real_oj else [])
        proc, out, err = invoke(batch_dir, order, item["id"], "initial", args,
                                interactive=real_oj)
        logs.append((out, err))
        run_dir = find_run_dir(proc.stdout, item["id"], before)
    if not real_oj:
        result = make_result(order, item, run_dir, logs)
        result["status"] = "SAMPLE_AC" if result.get("sample_ac") else "FAILED"
        result["final_status"] = result["status"]
        return result

    while run_dir and (run_dir / "record.json").is_file():
        record = read_agent_record(run_dir)
        status = record.get("final_status")
        if status in FINAL_REAL:
            return make_result(order, item, run_dir, logs)
        if status in REPAIRABLE:
            if int(record.get("oj_repair_attempts", 0)) >= MAX_OJ_REPAIRS:
                record["final_status"] = "FAILED"
                record["failure_reason"] = "MAX_OJ_REPAIRS_EXCEEDED"
                atomic_json(run_dir / "record.json", record)
                return make_result(order, item, run_dir, logs)
            number = int(record.get("oj_repair_attempts", 0)) + 1
            proc, out, err = invoke(batch_dir, order, item["id"], f"oj_repair_{number}",
                                    ["--resume", run_dir, "--repair-current-oj"])
            logs.append((out, err))
            if proc.returncode:
                return make_result(order, item, run_dir, logs)
            proc, out, err = invoke(batch_dir, order, item["id"], f"submit_{number + 1}",
                                    ["--resume", run_dir, "--submit-main"], interactive=True)
            logs.append((out, err))
            continue
        if status in {"PREPARED_FOR_SUBMISSION", "LOGIN_REQUIRED"}:
            proc, out, err = invoke(batch_dir, order, item["id"], "resume_submit",
                                    ["--resume", run_dir, "--submit-main"], interactive=True)
            logs.append((out, err))
            if proc.returncode == 3:
                raise LoginRequired(relative(run_dir))
            continue
        return make_result(order, item, run_dir, logs)
    return make_result(order, item, run_dir, logs)


def summary_for(record):
    final = FINAL_REAL if record.get("real_oj") else FINAL_SAMPLE
    items = [x for x in record["problems"] if x.get("status") in final]
    if not record.get("real_oj"):
        count = len(items)
        sample_ac = sum(x["status"] == "SAMPLE_AC" for x in items)
        ratings = {}
        for item in items:
            ratings.setdefault(str(item.get("rating")), []).append(item)
        by_rating = []
        for rating, group in sorted(ratings.items(), key=lambda pair: (
                pair[0] == "None", int(pair[0]) if pair[0] != "None" else 0)):
            by_rating.append({"rating": None if rating == "None" else int(rating),
                "problems": len(group),
                "first_pass": sum(x.get("first_generation_success", False) for x in group),
                "sample_ac": sum(x["status"] == "SAMPLE_AC" for x in group),
                "average_repairs": sum(x.get("repair_attempts", 0) for x in group) / len(group),
                "average_model_time_sec": sum(x.get("model_time_sec", 0) for x in group) / len(group)})
        average = lambda key: sum(float(x.get(key, 0)) for x in items) / count if count else 0
        return {"benchmark": record["benchmark"], "generated_at": now_iso(), "overall": {
            "total": count, "sample_ac": sample_ac, "failed": count - sample_ac,
            "first_generation_success": sum(x.get("first_generation_success", False) for x in items),
            "final_sample_success_rate": sample_ac / count if count else 0,
            "average_repairs": average("repair_attempts"),
            "average_model_time_sec": average("model_time_sec"),
            "average_total_time_sec": average("total_time_sec"),
            "total_wall_time_sec": float(record.get("wall_time_sec", 0))},
            "by_rating": by_rating}
    evaluated = [x for x in items if x.get("oj_attempts", 0) > 0]
    difficulties = {}
    for item in items:
        difficulties.setdefault(item.get("difficulty") or "unknown", []).append(item)
    by_difficulty = []
    for difficulty, group in difficulties.items():
        judged = [x for x in group if x.get("oj_attempts", 0) > 0]
        by_difficulty.append({"difficulty": difficulty, "problems": len(group),
            "oj_evaluated": len(judged),
            "first_oj_ac": sum(x.get("oj_history", [{}])[0].get("status") == "OJ_AC" for x in judged),
            "final_oj_ac": sum(x.get("status") == "OJ_AC" for x in judged),
            "average_oj_repairs": sum(x.get("oj_repairs", 0) for x in judged) / len(judged) if judged else 0,
            "average_model_time_sec": sum(x.get("model_time_sec", 0) for x in group) / len(group)})
    return {"benchmark": record["benchmark"], "generated_at": now_iso(), "overall": {
        "total": len(items), "oj_evaluated": len(evaluated),
        "oj_ac": sum(x.get("status") == "OJ_AC" for x in evaluated),
        "first_oj_ac": sum(x.get("oj_history", [{}])[0].get("status") == "OJ_AC" for x in evaluated),
        "final_oj_ac": sum(x.get("status") == "OJ_AC" for x in evaluated),
        "captcha_skipped": sum(x.get("status") == "CAPTCHA_SKIPPED" for x in items),
        "failed": sum(x.get("status") == "FAILED" for x in items)},
        "by_difficulty": by_difficulty}


def print_summary(summary):
    overall = summary["overall"]
    print("\nBatch summary")
    for key, label in (("total", "Total problems"), ("oj_evaluated", "OJ evaluated"),
                       ("oj_ac", "OJ AC"), ("first_oj_ac", "First OJ AC"),
                       ("final_oj_ac", "Final OJ AC"), ("captcha_skipped", "CAPTCHA skipped"),
                       ("sample_ac", "SAMPLE_AC"), ("failed", "Failed")):
        if key in overall:
            print(f"{label}: {overall[key]}")
    if "final_sample_success_rate" in overall:
        print(f"First-generation success: {overall['first_generation_success']}/{overall['total']}")
        print(f"Final sample success rate: {overall['final_sample_success_rate']:.1%}")
        print(f"Average repairs: {overall['average_repairs']:.2f}")
        print(f"Average model time: {overall['average_model_time_sec']:.1f}s")
        print(f"Average total time: {overall['average_total_time_sec']:.1f}s")
        print(f"Total wall time: {overall['total_wall_time_sec']:.1f}s")
        print("\nRating  Problems  FirstPass  SampleAC  AvgRepairs  AvgModelTime")
        for row in summary["by_rating"]:
            print(f"{str(row['rating']):<7} {row['problems']:<9} {row['first_pass']:<10} "
                  f"{row['sample_ac']:<9} {row['average_repairs']:<11.2f} "
                  f"{row['average_model_time_sec']:.1f}s")
    if "by_difficulty" in summary:
        print("\nDifficulty  Problems  OJEvaluated  FirstOJAC  FinalOJAC  AvgOJRepairs  AvgModelTime")
        for row in summary["by_difficulty"]:
            print(f"{row['difficulty']:<11} {row['problems']:<9} {row['oj_evaluated']:<12} "
                  f"{row['first_oj_ac']:<10} {row['final_oj_ac']:<10} "
                  f"{row['average_oj_repairs']:<13.2f} {row['average_model_time_sec']:.1f}s")


def checkpoint(batch_dir, record, started, base_wall):
    record["wall_time_sec"] = round(base_wall + time.perf_counter() - started, 6)
    atomic_json(batch_dir / "batch_record.json", record)


def run_batch(path, benchmark, resume_dir=None, limit=None, real_oj=False,
              retry_captcha_skipped=False):
    started = time.perf_counter()
    if resume_dir:
        batch_dir = resume_dir.resolve()
        record = json.loads((batch_dir / "batch_record.json").read_text(encoding="utf-8"))
        if record.get("benchmark") != benchmark["name"] or record.get("real_oj") != real_oj:
            raise ValueError("resume directory does not match benchmark mode")
        if limit is not None and record.get("limit") != limit:
            raise ValueError(f"resume limit must remain {record.get('limit')}")
        limit = record.get("limit") if limit is None else limit
        record.update({"status": "RUNNING", "resumed_at": now_iso()})
    else:
        batch_dir = unique_batch_dir(benchmark["name"])
        record = {"benchmark": benchmark["name"], "benchmark_file": relative(path),
                  "platform": benchmark["platform"], "real_oj": real_oj,
                  "problem_ids": [x["id"] for x in benchmark["problems"]],
                  "started_at": now_iso(), "finished_at": None, "status": "RUNNING",
                  "current_index": 0, "total": len(benchmark["problems"]),
                  "limit": limit, "problems": [], "wall_time_sec": 0}
    base_wall = float(record.get("wall_time_sec", 0))
    selected = benchmark["problems"][:limit] if limit else benchmark["problems"]
    checkpoint(batch_dir, record, started, base_wall)
    final = FINAL_REAL if real_oj else FINAL_SAMPLE
    try:
        system_preflight(real_oj)
    except LoginRequired:
        record["status"] = "BATCH_PAUSED_LOGIN_REQUIRED"
        checkpoint(batch_dir, record, started, base_wall)
        print("[Batch] BATCH_PAUSED_LOGIN_REQUIRED")
        return batch_dir
    for order, item in enumerate(selected, 1):
        existing = next((x for x in record["problems"] if x["order"] == order), None)
        if existing and existing.get("status") in final:
            print(f"[{order}/{len(selected)}] {item['id']} already completed: {existing['status']}")
            continue
        print(f"[{order}/{len(selected)}] {item['id']}")
        running = existing or {"order": order, "problem_id": item["id"], "status": "RUNNING",
                               "started_at": now_iso()}
        if retry_captcha_skipped and not existing:
            retry_run = clone_captcha_retry(item["id"])
            if retry_run:
                running["run_dir"] = relative(retry_run)
        if not existing:
            record["problems"].append(running)
        record["current_index"] = order
        checkpoint(batch_dir, record, started, base_wall)
        try:
            result = run_one(batch_dir, order, item, real_oj, running)
        except LoginRequired as exc:
            running.update({"status": "LOGIN_REQUIRED", "run_dir": str(exc),
                            "failure_reason": "LUOGU_LOGIN_REQUIRED"})
            record["status"] = "BATCH_PAUSED_LOGIN_REQUIRED"
            checkpoint(batch_dir, record, started, base_wall)
            print("[Batch] BATCH_PAUSED_LOGIN_REQUIRED")
            return batch_dir
        result.update({"started_at": running.get("started_at"), "finished_at": now_iso()})
        record["problems"][record["problems"].index(running)] = result
        checkpoint(batch_dir, record, started, base_wall)
        print(f"[Result] {result['status']}")
    record.update({"status": "COMPLETE", "current_index": len(selected), "finished_at": now_iso()})
    checkpoint(batch_dir, record, started, base_wall)
    summary = summary_for(record)
    atomic_json(batch_dir / "batch_summary.json", summary)
    print_summary(summary)
    print(f"\nBatch record: {relative(batch_dir / 'batch_record.json')}")
    return batch_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--real-oj", action="store_true")
    parser.add_argument("--submit-existing-cf", action="store_true")
    parser.add_argument("--check-existing-cf", action="store_true")
    parser.add_argument("--retry-captcha-skipped", action="store_true")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.dry_run and args.resume:
        parser.error("--dry-run cannot be combined with --resume")
    try:
        path = args.benchmark.resolve()
        benchmark = load_benchmark(path)
        if (args.submit_existing_cf or args.check_existing_cf) and benchmark["platform"] != "codeforces":
            parser.error("existing-CF modes require a Codeforces benchmark")
        if args.retry_captcha_skipped and (not args.real_oj or benchmark["platform"] != "luogu"):
            parser.error("--retry-captcha-skipped requires a Luogu --real-oj benchmark")
        if args.real_oj and benchmark["platform"] != "luogu":
            parser.error("--real-oj currently supports Luogu benchmarks only")
        if args.limit and args.limit > len(benchmark["problems"]):
            parser.error("--limit exceeds benchmark size")
        if args.dry_run:
            print(f"Benchmark: {benchmark['name']}")
            print(f"Platform: {benchmark['platform']}")
            print(f"Problems: {len(benchmark['problems'])}")
            for order, item in enumerate(benchmark["problems"], 1):
                suffix = f" expected_rating={item['expected_rating']}" if "expected_rating" in item else ""
                print(f"{order:2d}. {item['id']}{suffix}")
            return 0
        if args.check_existing_cf:
            verify_existing_cf(benchmark)
            return 0
        if args.submit_existing_cf:
            run_existing_cf(path, benchmark, args.limit)
            return 0
        run_batch(path, benchmark, args.resume, args.limit, args.real_oj,
                  retry_captcha_skipped=args.retry_captcha_skipped)
        return 0
    except KeyboardInterrupt:
        return 130
    except (BatchSystemError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[Batch] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
