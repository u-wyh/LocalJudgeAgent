#!/usr/bin/env python3
"""LocalJudgeAgent Phase 1: generate, compile, sample-test, and repair C++ code."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests

from codeforces import CodeforcesError, load_or_fetch as load_or_fetch_codeforces
from luogu import LuoguError, load_or_fetch
from oj import OpenJudgeError, TokenNotConfigured, get_auth, judge


MODEL = "gpt-oss:20b"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
NUM_CTX = 32768
MODEL_TIMEOUT_SEC = 900
RUN_TIMEOUT_SEC = 5
MAX_REPAIRS = 3
MAX_OJ_REPAIRS = 3
ROOT = Path(__file__).resolve().parent


def normalize_output(text):
    """Ignore trailing spaces per line and extra final newlines only."""
    return "\n".join(line.rstrip(" \t") for line in text.rstrip("\r\n").splitlines())


def format_problem(problem):
    samples = []
    for index, sample in enumerate(problem["samples"], 1):
        samples.append(
            f"Sample {index} input:\n{sample['input']}\n"
            f"Sample {index} output:\n{sample['output']}"
        )
    difficulty = ("" if problem.get("platform") == "codeforces"
                  else f"Difficulty: {problem.get('difficulty', '')}\n")
    return (
        f"Problem ID: {problem['problem_id']}\n"
        f"Title: {problem['title']}\n"
        f"{difficulty}\n"
        f"Full statement:\n{problem['statement']}\n\n" + "\n\n".join(samples)
    )


def initial_prompt(problem):
    return f"""You are solving the following programming problem.

{format_problem(problem)}

Carefully analyze the constraints stated in the problem and choose an appropriate time and memory complexity.
Return a complete, compilable GNU C++17 program.
Your final response must contain exactly one ```cpp code block and no explanation outside it.
"""


def repair_prompt(problem, code, failure, repair_number):
    return f"""Repair attempt {repair_number} of {MAX_REPAIRS}.

{format_problem(problem)}

{failure}

Current code:
```cpp
{code}
```

Recheck the complete algorithm; do not hardcode for the failing sample.
Return the complete repaired GNU C++17 program, not a patch.
Your final response must contain exactly one ```cpp code block and no explanation outside it.
"""


def call_model(prompt):
    started = time.perf_counter()
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"num_ctx": NUM_CTX},
        },
        timeout=(10, MODEL_TIMEOUT_SEC),
    )
    response.raise_for_status()
    data = response.json()
    content = data["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Ollama returned an empty response")
    return content, time.perf_counter() - started


def extract_code(response):
    matches = re.findall(r"```[ \t]*(?:cpp|c\+\+)?[ \t]*\r?\n(.*?)```", response, re.I | re.S)
    if matches:
        return max(matches, key=len).strip() + "\n"
    raw = response.strip()
    if raw and ("#include" in raw or "int main" in raw):
        return raw + "\n"
    raise ValueError("No C++ code found in model response")


def compile_code(main_cpp, binary):
    started = time.perf_counter()
    result = subprocess.run(
        ["g++", "-std=c++17", "-O2", str(main_cpp), "-o", str(binary)],
        text=True, capture_output=True, check=False,
    )
    return {
        "return_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "time_sec": time.perf_counter() - started,
    }


def run_samples(binary, samples):
    results = []
    for index, sample in enumerate(samples, 1):
        started = time.perf_counter()
        try:
            run = subprocess.run(
                [str(binary)], input=sample["input"], text=True,
                capture_output=True, timeout=RUN_TIMEOUT_SEC, check=False,
            )
            elapsed = time.perf_counter() - started
            if run.returncode != 0:
                verdict = "RE"
            elif normalize_output(run.stdout) != normalize_output(sample["output"]):
                verdict = "WA"
            else:
                verdict = "PASS"
            item = {"sample": index, "verdict": verdict, "return_code": run.returncode,
                    "stdout": run.stdout, "stderr": run.stderr, "timeout": False,
                    "time_sec": elapsed}
        except subprocess.TimeoutExpired as exc:
            item = {"sample": index, "verdict": "TLE", "return_code": None,
                    "stdout": exc.stdout or "", "stderr": exc.stderr or "", "timeout": True,
                    "time_sec": time.perf_counter() - started}
        results.append(item)
        print(f"[Sample {index}] {item['verdict']}")
        if item["verdict"] != "PASS":
            break
    return results


def failure_feedback(problem, compile_result, sample_results):
    if compile_result["return_code"] != 0:
        return f"COMPILATION ERROR\n\nCompiler error:\n{compile_result['stderr']}", "CE"
    failed = next(item for item in sample_results if item["verdict"] != "PASS")
    sample = problem["samples"][failed["sample"] - 1]
    if failed["verdict"] == "WA":
        text = (f"WRONG ANSWER\n\nInput:\n{sample['input']}\nExpected output:\n"
                f"{sample['output']}\nActual output:\n{failed['stdout']}")
    elif failed["verdict"] == "RE":
        text = (f"RUNTIME ERROR\n\nInput:\n{sample['input']}\nReturn code: "
                f"{failed['return_code']}\nStderr:\n{failed['stderr']}")
    else:
        text = f"TIME LIMIT EXCEEDED\n\nInput:\n{sample['input']}\nTimeout: {RUN_TIMEOUT_SEC} seconds"
    return text, failed["verdict"]


def unique_run_dir(problem_id):
    base = ROOT / "runs" / f"{problem_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = Path(f"{base}_{suffix}")
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def oj_repair_prompt(problem, code, result, score):
    focus = {
        "WA": "Check algorithm correctness, boundary cases, overflow, and whether the statement was misread.",
        "TLE": "Check time complexity, algorithmic bottlenecks, and the worst-case constraints.",
        "MLE": "Check memory complexity and large arrays or data structures.",
        "RE": "Check array bounds, division by zero, stack overflow, and invalid memory access.",
        "CE": "Check strict GNU C++17 portability and differences between local and OJ compilation.",
        "PC": "Only partial credit was received. Recheck the full constraints and all subtasks.",
    }[result]
    score_text = f"\nScore: {score:g}" if score is not None else ""
    return f"""The program passed all provided samples, but failed the real online judge.

{format_problem(problem)}

Online Judge result: {result}{score_text}

The hidden failing test case is unavailable.
Re-analyze the algorithm from the complete problem statement and constraints.
Do not guess a hidden testcase.
{focus}

Current submitted code:
```cpp
{code}
```

Return a complete corrected GNU C++17 program, not a patch.
Your final response must contain exactly one ```cpp code block and no explanation outside it.
"""


def next_version(record):
    numbers = [int(label[1:]) for label in record.get("code_versions", [])
               if re.fullmatch(r"v\d+", str(label))]
    return max(numbers, default=-1) + 1


def load_resume_problem(run_dir, record):
    candidates = [run_dir / "problem.json"]
    if record.get("platform") == "codeforces":
        candidates.append(ROOT / "problems" / "codeforces" / f"{record['problem_id']}.json")
    else:
        candidates.append(ROOT / "problems" / f"{record['problem_id']}.json")
    if record.get("problem_id") == "P1001":
        candidates.append(ROOT / "problem.json")
    for path in candidates:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"No local problem data found for {record['problem_id']}")


def prepare_submission(run_dir, record):
    version = record.get("final_version")
    source = run_dir / f"main_{version}.cpp"
    if not version or not source.exists():
        raise FileNotFoundError("final code version is missing")
    destination = run_dir / "submission.cpp"
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        shown = destination.relative_to(ROOT)
    except ValueError:
        shown = destination
    print(f"[Submit] Code ready: {shown}")
    return destination


def manual_oj_entry(record, result, score, record_id):
    return {
        "attempt": len(record.get("oj_history", [])) + 1,
        "code_version": record.get("final_version"),
        "status": f"OJ_{result}",
        "score": score,
        "record_id": record_id,
        "reported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def resume_run(run_dir, oj_result, oj_score=None, oj_record_id=None):
    started = time.perf_counter()
    run_dir = run_dir.resolve()
    record_path = run_dir / "record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    problem = load_resume_problem(run_dir, record)
    record.setdefault("oj_history", [])
    record.setdefault("oj_repair_attempts", 0)
    record.setdefault("oj", {"provider": "luogu-manual", "submitted": False})
    if record.get("final_status") == "OJ_AC":
        print("[Result] OJ_AC (already finalized)")
        return 0
    if not record.get("final_sample_passed"):
        print("[Resume] code is not prepared for submission")
        return 1
    submission = run_dir / "submission.cpp"
    if not submission.exists():
        submission = prepare_submission(run_dir, record)
    code = submission.read_text(encoding="utf-8")

    entry = manual_oj_entry(record, oj_result, oj_score, oj_record_id)
    record["oj_history"].append(entry)
    record["oj"] = {"provider": "luogu-manual", "submitted": True,
                    "status": entry["status"], "raw_status": oj_result,
                    "score": oj_score, "record_id": oj_record_id}
    print(f"[OJ] {oj_result}")
    if oj_result == "AC":
        record["final_status"] = "OJ_AC"
        record["total_time_sec"] = round(record.get("total_time_sec", 0) + time.perf_counter() - started, 6)
        record["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        write_json(record_path, record)
        print("[Result] OJ_AC")
        print(f"[Record] {record_path}")
        return 0

    if record["oj_repair_attempts"] >= MAX_OJ_REPAIRS:
        record["final_status"] = entry["status"]
        record["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        write_json(record_path, record)
        print("[OJ Repair] maximum attempts reached")
        return 1

    record["oj_repair_attempts"] += 1
    record["final_sample_passed"] = False
    prompt = oj_repair_prompt(problem, code, oj_result, oj_score)
    version = next_version(record)
    last_reason = None
    try:
        for local_repair in range(MAX_REPAIRS + 1):
            label = f"v{version}"
            (run_dir / f"prompt_{version}.txt").write_text(prompt, encoding="utf-8")
            response, model_time = call_model(prompt)
            record["model_time_sec"] = record.get("model_time_sec", 0) + model_time
            print(f"[Model] {model_time:.2f}s")
            (run_dir / f"response_{version}.txt").write_text(response, encoding="utf-8")
            code = extract_code(response)
            version_path = run_dir / f"main_{label}.cpp"
            version_path.write_text(code, encoding="utf-8")
            (ROOT / "main.cpp").write_text(code, encoding="utf-8")
            record.setdefault("code_versions", []).append(label)
            compile_result = compile_code(ROOT / "main.cpp", run_dir / "main")
            record["compile_attempts"] = record.get("compile_attempts", 0) + 1
            record["compile_time_sec"] = record.get("compile_time_sec", 0) + compile_result["time_sec"]
            write_json(run_dir / f"compile_{version}.txt", compile_result)
            if compile_result["return_code"] != 0:
                print("[Compile] FAIL")
                sample_results = []
            else:
                print("[Compile] PASS")
                sample_results = run_samples(run_dir / "main", problem["samples"])
                record["run_time_sec"] = record.get("run_time_sec", 0) + sum(
                    item["time_sec"] for item in sample_results)
                write_json(run_dir / f"samples_{version}.json", sample_results)
                if all(item["verdict"] == "PASS" for item in sample_results):
                    record["final_sample_passed"] = True
                    record["final_version"] = label
                    record["failure_reason"] = None
                    record["final_status"] = "PREPARED_FOR_SUBMISSION"
                    prepare_submission(run_dir, record)
                    print(f"[OJ Repair] {label} ready for manual submission")
                    return_code = 0
                    break
            failure, last_reason = failure_feedback(problem, compile_result, sample_results)
            if local_repair == MAX_REPAIRS:
                record["failure_reason"] = "MAX_REPAIRS_EXCEEDED"
                record["final_status"] = "FAILED"
                return_code = 1
                break
            record["repair_attempts"] = record.get("repair_attempts", 0) + 1
            prompt = repair_prompt(problem, code, failure, local_repair + 1)
            version += 1
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        record["failure_reason"] = "MODEL_ERROR" if isinstance(exc, requests.RequestException) else "CODE_EXTRACTION_ERROR"
        record["final_status"] = "FAILED"
        (run_dir / "error.txt").write_text(repr(exc) + "\n", encoding="utf-8")
        return_code = 1
    except Exception as exc:
        record["failure_reason"] = "LOCAL_VALIDATION_ERROR"
        record["final_status"] = "FAILED"
        (run_dir / "error.txt").write_text(repr(exc) + "\n", encoding="utf-8")
        return_code = 1
    finally:
        record["total_time_sec"] = record.get("total_time_sec", 0) + time.perf_counter() - started
        record["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        for key in ("total_time_sec", "model_time_sec", "compile_time_sec", "run_time_sec"):
            record[key] = round(record.get(key, 0), 6)
        write_json(record_path, record)
        print(f"[Record] {record_path}")
    return return_code


def resume_cf_submission(run_dir):
    started = time.perf_counter()
    run_dir = run_dir.resolve()
    record_path = run_dir / "record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    problem = load_resume_problem(run_dir, record)
    if record.get("platform") != "codeforces" or problem.get("platform") != "codeforces":
        print("[Resume] --submit-cf requires a Codeforces run")
        return 1
    if not record.get("final_sample_passed"):
        print("[Resume] code is not prepared for submission")
        return 1
    submission = run_dir / "submission.cpp"
    final_source = run_dir / f"main_{record.get('final_version')}.cpp"
    if not submission.is_file() or not final_source.is_file():
        print("[Resume] final submission files are missing")
        return 1
    digest = hashlib.sha256(submission.read_bytes()).hexdigest()
    expected = record.get("oj", {}).get("submission_sha256")
    if not expected or digest != expected or submission.read_bytes() != final_source.read_bytes():
        print("[Resume] SUBMISSION_SHA256_MISMATCH")
        return 1
    print(f"[Resume] submission SHA-256 verified: {digest}")
    from codeforces_main import CodeforcesMainError, submit_and_wait as submit_codeforces
    try:
        result = submit_codeforces(problem, submission)
    except (CodeforcesMainError, CodeforcesError) as exc:
        print(f"[OJ] FAILED: {exc}")
        return 1

    previous = record.get("browser_submit_history", [])
    offset = len(previous)
    current = result.get("browser_submit_history", [])
    for entry in current:
        entry["click"] = entry.get("click", 0) + offset
    merged = previous + current
    result["browser_submit_history"] = merged
    result["browser_submit_clicks"] = len(merged)
    record["browser_submit_history"] = merged
    record["oj"] = result
    if result.get("submission_confirmed") and result.get("submission_id") is not None:
        record.setdefault("oj_history", []).append({
            "attempt": len(record.get("oj_history", [])) + 1,
            "provider": "codeforces-main", "code_version": record.get("final_version"),
            "status": result["status"], "score": None,
            "submission_id": result["submission_id"],
            "submission_sha256": result["submission_sha256"],
            "reported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        record["final_status"] = result["status"]
        record["failure_reason"] = None
        return_code = 0
    else:
        record["final_status"] = result.get("status", "MANUAL_SUBMISSION_TIMEOUT")
        record["failure_reason"] = result.get("failure_reason", "CF_SUBMISSION_NOT_FOUND")
        return_code = 1
    record["total_time_sec"] = round(
        record.get("total_time_sec", 0) + time.perf_counter() - started, 6)
    record["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    write_json(record_path, record)
    print(f"[OJ] {record['final_status']}")
    print(f"[Record] {record_path}")
    return return_code


def refresh_cf_verdict(run_dir):
    run_dir = run_dir.resolve()
    record_path = run_dir / "record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    oj = record.get("oj", {})
    submission_id = oj.get("submission_id")
    if record.get("platform") != "codeforces" or submission_id is None:
        print("[Refresh] confirmed Codeforces submission_id is required")
        return 1
    from codeforces import wait_for_verdict
    from codeforces_main import configured_handle
    result = wait_for_verdict(configured_handle(), submission_id)
    reconciled_at = datetime.now().astimezone().isoformat(timespec="seconds")
    history = list(oj.get("verdict_history", []))
    if not history and oj.get("raw_status") is not None:
        history.append({"status": oj.get("raw_status"),
                        "observed_at": record.get("finished_at", reconciled_at)})
    for observation in result.get("verdict_history", []):
        if not history or history[-1].get("status") != observation.get("status"):
            history.append(observation)
    if oj.get("status") == "OJ_UNKNOWN":
        oj.setdefault("initial_polling_result", {
            "status": oj.get("status"), "raw_status": oj.get("raw_status"),
            "recorded_at": record.get("finished_at"),
        })
    oj.update(result)
    oj["verdict_history"] = history
    oj["reconciled_at"] = reconciled_at
    record["oj"] = oj
    record["final_status"] = result["status"]
    record["failure_reason"] = None if result["status"] != "OJ_UNKNOWN" else "OJ_RESULT_TIMEOUT"
    matching = next((entry for entry in record.get("oj_history", [])
                     if entry.get("submission_id") == submission_id), None)
    if matching:
        matching.setdefault("initial_status", matching.get("status"))
        matching["status"] = result["status"]
        matching["reconciled_at"] = reconciled_at
    else:
        record.setdefault("oj_history", []).append({
            "attempt": len(record.get("oj_history", [])) + 1,
            "provider": "codeforces-main", "code_version": record.get("final_version"),
            "status": result["status"], "submission_id": submission_id,
            "submission_sha256": oj.get("submission_sha256"),
            "reported_at": reconciled_at, "reconciled_at": reconciled_at,
        })
    record["reconciled_at"] = reconciled_at
    write_json(record_path, record)
    print(f"[Codeforces] Submission {submission_id}: {result['raw_status']} -> {result['status']}")
    print(f"[Record] {record_path}")
    return 0 if result["status"] != "OJ_UNKNOWN" else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("problem_path", nargs="?", type=Path,
                        help="problem JSON path (default: problem.json)")
    parser.add_argument("--problem", dest="legacy_problem_path", type=Path,
                        help=argparse.SUPPRESS)
    parser.add_argument("--inject-ce", action="store_true",
                        help="test repair by replacing v0 with a deliberate compile error")
    parser.add_argument("--submit", action="store_true",
                        help="submit SAMPLE_AC code to the Luogu Open Platform")
    parser.add_argument("--submit-main", action="store_true",
                        help="submit SAMPLE_AC code through the Luogu browser UI")
    parser.add_argument("--submit-cf", action="store_true",
                        help="submit SAMPLE_AC code through the Codeforces browser UI")
    parser.add_argument("--refresh-cf-verdict", action="store_true",
                        help="refresh a confirmed Codeforces submission verdict without submitting")
    parser.add_argument("--resume", type=Path, help="resume an existing run directory")
    parser.add_argument("--oj-result", type=str.upper,
                        choices=("AC", "WA", "TLE", "MLE", "RE", "CE", "PC"))
    parser.add_argument("--oj-score", type=float)
    parser.add_argument("--oj-record-id")
    args = parser.parse_args()
    if args.problem_path and args.legacy_problem_path:
        parser.error("provide the problem path either positionally or with --problem, not both")
    if args.resume:
        if (args.problem_path or args.legacy_problem_path or args.submit or args.submit_main
                or args.inject_ce):
            parser.error("--resume cannot be combined with a problem path or submission/generation options")
        if args.submit_cf and args.refresh_cf_verdict:
            parser.error("--submit-cf and --refresh-cf-verdict are mutually exclusive")
        if args.refresh_cf_verdict:
            if args.oj_result or args.oj_score is not None or args.oj_record_id:
                parser.error("--refresh-cf-verdict cannot use manual OJ result options")
            return refresh_cf_verdict(args.resume)
        if args.submit_cf:
            if args.oj_result or args.oj_score is not None or args.oj_record_id:
                parser.error("--submit-cf resume cannot use manual OJ result options")
            return resume_cf_submission(args.resume)
        if not args.oj_result:
            parser.error("--resume requires --oj-result, --submit-cf, or --refresh-cf-verdict")
        return resume_run(args.resume, args.oj_result, args.oj_score, args.oj_record_id)
    if args.refresh_cf_verdict:
        parser.error("--refresh-cf-verdict requires --resume")
    if args.oj_result or args.oj_score is not None or args.oj_record_id:
        parser.error("OJ feedback options require --resume")
    if sum((args.submit, args.submit_main, args.submit_cf)) > 1:
        parser.error("--submit, --submit-main, and --submit-cf are mutually exclusive")
    if args.submit_main:
        from luogu_main import MainSiteError, playwright_api, require_gui
        try:
            require_gui()
            playwright_api()
        except MainSiteError as exc:
            print(f"[Luogu] {exc}")
            return 1
    if args.submit_cf:
        from codeforces_main import CodeforcesMainError, playwright_api, require_gui
        try:
            require_gui()
            playwright_api()
        except CodeforcesMainError as exc:
            print(f"[Codeforces] {exc}")
            return 1
    if args.submit:
        try:
            get_auth()
        except (TokenNotConfigured, OpenJudgeError) as exc:
            print(exc)
            return 1
    problem_argument = args.problem_path or args.legacy_problem_path
    started = time.perf_counter()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        is_bare_luogu_id = (problem_argument and problem_argument.parent == Path(".")
                            and not problem_argument.suffix and problem_argument.name.startswith("P"))
        is_codeforces_id = (problem_argument and problem_argument.parent == Path(".")
                            and re.fullmatch(r"CF\d+[A-Za-z][A-Za-z0-9]*", problem_argument.name, re.I))
        if is_codeforces_id:
            problem, _ = load_or_fetch_codeforces(problem_argument.name)
        elif is_bare_luogu_id:
            problem, _ = load_or_fetch(problem_argument.name)
        else:
            problem_path = problem_argument or ROOT / "problem.json"
            problem = json.loads(problem_path.read_text(encoding="utf-8"))
    except (LuoguError, CodeforcesError) as exc:
        print(f"[Problem] {exc.code}: {exc}")
        return 1
    run_name = f"CF{problem['problem_id']}" if problem.get("platform") == "codeforces" else problem["problem_id"]
    run_dir = unique_run_dir(run_name)
    write_json(run_dir / "problem.json", problem)
    record = {
        "problem_id": problem["problem_id"], "title": problem["title"],
        "difficulty": problem.get("difficulty", ""), "model": MODEL, "context": NUM_CTX,
        "first_generation_success": False, "compile_attempts": 0, "repair_attempts": 0,
        "final_sample_passed": False, "total_time_sec": 0, "model_time_sec": 0,
        "compile_time_sec": 0, "run_time_sec": 0, "code_versions": [],
        "final_version": None, "failure_reason": None,
        "started_at": started_at, "finished_at": None, "final_status": "FAILED",
        "oj": {"provider": "luogu-open", "submitted": False},
        "oj_repair_attempts": 0, "oj_history": [],
        "platform": problem.get("platform", "luogu"),
        "rating": problem.get("rating"), "tags": problem.get("tags", []),
    }
    print(f"[Problem] {problem['problem_id']} {problem['title']}")
    print(f"[Model] {MODEL}")
    prompt = initial_prompt(problem)
    last_reason = None
    try:
        if shutil.which("g++") is None:
            raise FileNotFoundError("g++ not found")
        for version in range(MAX_REPAIRS + 1):
            label = f"v{version}"
            print(f"[{'Generate' if version == 0 else 'Repair'}] {label}" if version == 0
                  else f"[Repair] attempt {version}/{MAX_REPAIRS}")
            (run_dir / f"prompt_{version}.txt").write_text(prompt, encoding="utf-8")
            response, model_time = call_model(prompt)
            record["model_time_sec"] += model_time
            print(f"[Model] {model_time:.2f}s")
            (run_dir / f"response_{version}.txt").write_text(response, encoding="utf-8")
            try:
                code = extract_code(response)
            except ValueError:
                last_reason = "CODE_EXTRACTION_ERROR"
                break
            if version == 0 and args.inject_ce:
                code = code + "\nthis_is_a_deliberate_compile_error\n"
            version_path = run_dir / f"main_v{version}.cpp"
            version_path.write_text(code, encoding="utf-8")
            (ROOT / "main.cpp").write_text(code, encoding="utf-8")
            record["code_versions"].append(label)
            compile_result = compile_code(ROOT / "main.cpp", run_dir / "main")
            record["compile_attempts"] += 1
            record["compile_time_sec"] += compile_result["time_sec"]
            (run_dir / f"compile_{version}.txt").write_text(
                json.dumps(compile_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if compile_result["return_code"] != 0:
                print("[Compile] FAIL")
                sample_results = []
            else:
                print("[Compile] PASS")
                sample_results = run_samples(run_dir / "main", problem["samples"])
                record["run_time_sec"] += sum(item["time_sec"] for item in sample_results)
                write_json(run_dir / f"samples_{version}.json", sample_results)
                if all(item["verdict"] == "PASS" for item in sample_results):
                    record["final_sample_passed"] = True
                    record["final_version"] = label
                    record["first_generation_success"] = version == 0
                    print("[Result] SAMPLE AC")
                    break
            failure, last_reason = failure_feedback(problem, compile_result, sample_results)
            if version == MAX_REPAIRS:
                last_reason = "MAX_REPAIRS_EXCEEDED"
                break
            record["repair_attempts"] += 1
            prompt = repair_prompt(problem, code, failure, version + 1)
        if not record["final_sample_passed"]:
            record["failure_reason"] = last_reason
            print(f"[Result] FAIL ({last_reason})")
        elif record["final_sample_passed"]:
            submission = prepare_submission(run_dir, record)
            final_code = submission.read_text(encoding="utf-8")
            if args.submit:
                try:
                    record["oj"] = judge(problem["problem_id"], final_code)
                    print(f"[OJ] {record['oj']['status']}")
                    record["oj_history"].append({
                        "attempt": 1, "code_version": record["final_version"],
                        "status": record["oj"]["status"], "score": record["oj"].get("score"),
                        "record_id": record["oj"].get("request_id"),
                        "reported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    })
                except (requests.RequestException, OpenJudgeError) as exc:
                    record["oj"].update({"status": "OJ_UNKNOWN", "raw_status": type(exc).__name__})
                    print(f"[OJ] FAILED: {exc}")
            elif args.submit_main:
                from luogu_main import MainSiteError, submit_and_wait
                try:
                    record["oj"] = submit_and_wait(
                        problem["problem_id"], final_code, run_dir / "browser_debug")
                    print(f"[OJ] {record['oj']['status']}")
                    record["oj_history"].append({
                        "attempt": 1, "provider": "luogu-main",
                        "code_version": record["final_version"],
                        "status": record["oj"]["status"], "score": record["oj"].get("score"),
                        "record_id": record["oj"].get("record_id"),
                        "submission_sha256": record["oj"]["submission_sha256"],
                        "reported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    })
                except MainSiteError as exc:
                    record["oj"].update({"provider": "luogu-main", "submitted": False,
                                         "status": "OJ_UNKNOWN", "raw_status": str(exc)})
                    print(f"[OJ] FAILED: {exc}")
            elif args.submit_cf:
                from codeforces_main import CodeforcesMainError, submit_and_wait as submit_codeforces
                try:
                    record["oj"] = submit_codeforces(problem, submission)
                    print(f"[OJ] {record['oj']['status']}")
                    record["browser_submit_history"] = record["oj"].get("browser_submit_history", [])
                    if record["oj"].get("submission_confirmed"):
                        record["oj_history"].append({
                            "attempt": 1, "provider": "codeforces-main",
                            "code_version": record["final_version"],
                            "status": record["oj"]["status"], "score": None,
                            "submission_id": record["oj"].get("submission_id"),
                            "submission_sha256": record["oj"]["submission_sha256"],
                            "reported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        })
                except (CodeforcesMainError, CodeforcesError) as exc:
                    record["oj"].update({"provider": "codeforces-main", "submitted": False,
                                         "status": "OJ_UNKNOWN", "raw_status": str(exc)})
                    print(f"[OJ] FAILED: {exc}")
    except (requests.RequestException, KeyError, TypeError, json.JSONDecodeError) as exc:
        record["failure_reason"] = "MODEL_ERROR"
        (run_dir / "error.txt").write_text(repr(exc) + "\n", encoding="utf-8")
        print(f"[Result] MODEL_ERROR: {exc}")
    except Exception as exc:
        record["failure_reason"] = last_reason or "MODEL_ERROR"
        (run_dir / "error.txt").write_text(repr(exc) + "\n", encoding="utf-8")
        print(f"[Result] ERROR: {exc}")
    finally:
        record["total_time_sec"] = time.perf_counter() - started
        record["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        oj_status = record.get("oj", {}).get("status")
        if record["oj"].get("submitted") and oj_status:
            record["final_status"] = oj_status
        elif record["oj"].get("failure_reason"):
            record["final_status"] = oj_status or "MANUAL_SUBMISSION_TIMEOUT"
            record["failure_reason"] = record["oj"].get("failure_reason")
        elif record["oj"].get("submit_clicked"):
            record["final_status"] = "SUBMISSION_BLOCKED"
            record["failure_reason"] = record["oj"].get("failure_reason")
        elif record["final_sample_passed"]:
            record["final_status"] = "PREPARED_FOR_SUBMISSION"
        else:
            record["final_status"] = "FAILED"
        for key in ("total_time_sec", "model_time_sec", "compile_time_sec", "run_time_sec"):
            record[key] = round(record[key], 6)
        write_json(run_dir / "record.json", record)
        print(f"[Record] {run_dir / 'record.json'}")
    return 0 if record["final_sample_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
