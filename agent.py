#!/usr/bin/env python3
"""LocalJudgeAgent Phase 1: generate, compile, sample-test, and repair C++ code."""

import argparse
import json
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests


MODEL = "gpt-oss:20b"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
NUM_CTX = 32768
MODEL_TIMEOUT_SEC = 900
RUN_TIMEOUT_SEC = 5
MAX_REPAIRS = 3
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
    return (
        f"Problem ID: {problem['problem_id']}\n"
        f"Title: {problem['title']}\n"
        f"Difficulty: {problem.get('difficulty', '')}\n\n"
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("problem_path", nargs="?", type=Path,
                        help="problem JSON path (default: problem.json)")
    parser.add_argument("--problem", dest="legacy_problem_path", type=Path,
                        help=argparse.SUPPRESS)
    parser.add_argument("--inject-ce", action="store_true",
                        help="test repair by replacing v0 with a deliberate compile error")
    args = parser.parse_args()
    if args.problem_path and args.legacy_problem_path:
        parser.error("provide the problem path either positionally or with --problem, not both")
    problem_path = args.problem_path or args.legacy_problem_path or ROOT / "problem.json"
    started = time.perf_counter()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    problem = json.loads(problem_path.read_text(encoding="utf-8"))
    run_dir = unique_run_dir(problem["problem_id"])
    record = {
        "problem_id": problem["problem_id"], "title": problem["title"],
        "difficulty": problem.get("difficulty", ""), "model": MODEL, "context": NUM_CTX,
        "first_generation_success": False, "compile_attempts": 0, "repair_attempts": 0,
        "final_sample_passed": False, "total_time_sec": 0, "model_time_sec": 0,
        "compile_time_sec": 0, "run_time_sec": 0, "code_versions": [],
        "final_version": None, "failure_reason": None,
        "started_at": started_at, "finished_at": None, "final_status": "FAILED",
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
        record["final_status"] = "SAMPLE_AC" if record["final_sample_passed"] else "FAILED"
        for key in ("total_time_sec", "model_time_sec", "compile_time_sec", "run_time_sec"):
            record[key] = round(record[key], 6)
        write_json(run_dir / "record.json", record)
        print(f"[Record] {run_dir / 'record.json'}")
    return 0 if record["final_sample_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
