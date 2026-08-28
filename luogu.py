#!/usr/bin/env python3
"""Fetch public Luogu problem statements into LocalJudgeAgent's JSON format."""

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent
PROBLEM_ID_PATTERN = re.compile(r"^P\d+$")
DIFFICULTIES = {
    0: "暂无评定",
    1: "入门",
    2: "普及-",
    3: "普及/提高-",
    4: "普及+/提高",
    5: "提高+/省选-",
    6: "省选/NOI-",
    7: "NOI/NOI+/CTSC",
}
HEADERS = {"User-Agent": "LocalJudgeAgent/0.2 (+https://github.com/u-wyh/LocalJudgeAgent)"}


class LuoguError(Exception):
    """A fetch error with a stable user-facing code."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class ContextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_context = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == "lentille-context":
            self.in_context = True

    def handle_endtag(self, tag):
        if tag == "script" and self.in_context:
            self.in_context = False

    def handle_data(self, data):
        if self.in_context:
            self.parts.append(data)


def validate_problem_id(problem_id):
    if not PROBLEM_ID_PATTERN.fullmatch(problem_id):
        raise LuoguError("INVALID_PROBLEM_ID", f"invalid Luogu problem ID: {problem_id}")


def fetch_problem(problem_id):
    validate_problem_id(problem_id)
    source_url = f"https://www.luogu.com.cn/problem/{problem_id}"
    try:
        response = requests.get(source_url, headers=HEADERS, timeout=(10, 30))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise LuoguError("FETCH_FAILED", str(exc)) from exc

    parser = ContextParser()
    parser.feed(response.text)
    try:
        context = json.loads("".join(parser.parts))
        raw = context["data"]["problem"]
        content = raw["content"]
        if raw["pid"] != problem_id or not raw["name"] or not isinstance(content, dict):
            raise ValueError("unexpected problem data")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise LuoguError("PARSE_FAILED", f"unexpected Luogu page structure: {exc}") from exc

    samples = []
    for sample in raw.get("samples") or []:
        if not isinstance(sample, list) or len(sample) < 2:
            raise LuoguError("PARSE_FAILED", "unexpected sample structure")
        samples.append({"input": str(sample[0]), "output": str(sample[1])})
    if not samples:
        raise LuoguError("NO_SAMPLES", f"{problem_id} has no public samples")

    section_keys = (
        ("题目背景", "background"),
        ("题目描述", "description"),
        ("输入格式", "formatI"),
        ("输出格式", "formatO"),
        ("说明/提示", "hint"),
    )
    sections = []
    for heading, key in section_keys:
        value = content.get(key)
        if value and str(value).strip():
            sections.append(f"## {heading}\n\n{str(value).strip()}")
    if not sections:
        raise LuoguError("PARSE_FAILED", "problem statement is empty")

    return {
        "problem_id": problem_id,
        "title": str(raw["name"]),
        "difficulty": DIFFICULTIES.get(raw.get("difficulty"), "unknown"),
        "source_url": source_url,
        "statement": "\n\n".join(sections) + "\n",
        "samples": samples,
    }


def cache_path(problem_id):
    validate_problem_id(problem_id)
    return ROOT / "problems" / f"{problem_id}.json"


def save_problem(problem, path=None):
    destination = path or cache_path(problem["problem_id"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(problem, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


def load_or_fetch(problem_id):
    path = cache_path(problem_id)
    if path.exists():
        print(f"[Problem] cache hit: {path.relative_to(ROOT)}")
        try:
            problem = json.loads(path.read_text(encoding="utf-8"))
            if problem["problem_id"] != problem_id or not problem["samples"]:
                raise ValueError("invalid cached problem")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise LuoguError("PARSE_FAILED", f"invalid cache {path}: {exc}") from exc
        return problem, path
    print(f"[Problem] fetching: https://www.luogu.com.cn/problem/{problem_id}")
    problem = fetch_problem(problem_id)
    save_problem(problem, path)
    print(f"[Problem] cached: {path.relative_to(ROOT)}")
    return problem, path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("problem_id", help="Luogu problem ID, for example P1030")
    args = parser.parse_args()
    try:
        problem, path = load_or_fetch(args.problem_id)
    except LuoguError as exc:
        print(f"[Problem] {exc.code}: {exc}")
        return 1
    print(f"[Problem] {problem['problem_id']} {problem['title']}")
    print(f"[Difficulty] {problem['difficulty']}")
    print(f"[Samples] {len(problem['samples'])}")
    print(f"[File] {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
