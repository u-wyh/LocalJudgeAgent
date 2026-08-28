#!/usr/bin/env python3
"""Codeforces browser adapter with a separate persistent Playwright profile."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path


HOME_URL = "https://codeforces.com/"
LOGIN_URL = "https://codeforces.com/enter"
PROFILE_DIR = Path.home() / ".local" / "share" / "LocalJudgeAgent" / "codeforces-profile"
CONFIG_PATH = Path.home() / ".config" / "LocalJudgeAgent" / "codeforces.json"
COMPILER_PRIORITY = ("GNU G++23", "GNU G++20", "GNU G++17")


class CodeforcesMainError(Exception):
    pass


def require_gui():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise CodeforcesMainError("GUI session unavailable")


def playwright_api():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CodeforcesMainError("Playwright is not installed.") from exc
    return sync_playwright


def open_context(playwright):
    try:
        browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
    except Exception as exc:
        raise CodeforcesMainError(
            "Could not connect to Codeforces Chromium on 127.0.0.1:9222."
        ) from exc

    if not browser.contexts:
        raise CodeforcesMainError(
            "Connected Chromium has no browser context."
        )

    return browser.contexts[0]


def visible(locator):
    return locator.count() > 0 and locator.first.is_visible()


def logged_in_handle(page):
    logout = page.locator('a[href*="/logout"]')
    profiles = page.locator('header a[href^="/profile/"], #header a[href^="/profile/"]')
    if not visible(logout):
        return None
    for index in range(profiles.count()):
        item = profiles.nth(index)
        if item.is_visible():
            match = re.search(r"/profile/([^/?#]+)", item.get_attribute("href") or "")
            if match:
                return match.group(1)
    return None


def save_handle(handle):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"handle": handle}, indent=2) + "\n", encoding="utf-8")


def configured_handle():
    env_handle = os.environ.get("CF_HANDLE")
    if env_handle:
        return env_handle
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["handle"]
    except (OSError, ValueError, KeyError, TypeError):
        raise CodeforcesMainError("Codeforces handle is not configured.")


def login():
    require_gui()
    with playwright_api()() as playwright:
        context = open_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            handle = logged_in_handle(page)
            if not handle:
                print("[Codeforces] Complete login/human verification in the opened browser.")
                input("Press Enter after login is completed...")
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
                handle = logged_in_handle(page)
            if not handle:
                raise CodeforcesMainError("Login could not be confirmed.")
            save_handle(handle)
            print("[Codeforces] Login session saved.")
        finally:
            pass  # External CDP browser must remain running


def check_login():
    require_gui()
    with playwright_api()() as playwright:
        context = open_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            handle = logged_in_handle(page)
            if not handle:
                raise CodeforcesMainError("Codeforces login session is not available.")
            save_handle(handle)
            print("[Codeforces] Persistent login session confirmed.")
        finally:
            pass  # External CDP browser must remain running


def fetch_problem_statement_data(contest_id, index):
    require_gui()
    url = f"https://codeforces.com/problemset/problem/{contest_id}/{index}"
    with playwright_api()() as playwright:
        context = open_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            root = page.locator(".problem-statement")
            if not visible(root):
                raise CodeforcesMainError("Problem statement is unavailable; complete any browser challenge first.")
            return root.first.evaluate(r"""root => {
                const text = element => element ? element.innerText.trim() : '';
                const known = ['header','input-specification','output-specification','sample-tests','note'];
                const descriptions = [...root.children]
                    .filter(e => !known.some(c => e.classList.contains(c)))
                    .map(text).filter(Boolean);
                const input = text(root.querySelector('.input-specification'));
                const output = text(root.querySelector('.output-specification'));
                const note = text(root.querySelector('.note'));
                const samples = [...root.querySelectorAll('.sample-test')].map(group => {
                    const inputs = [...group.querySelectorAll('.input pre')];
                    const outputs = [...group.querySelectorAll('.output pre')];
                    return inputs.map((input, i) => ({input: input.innerText, output: outputs[i]?.innerText || ''}));
                }).flat();
                return {
                    time_limit: text(root.querySelector('.time-limit')),
                    memory_limit: text(root.querySelector('.memory-limit')),
                    description: descriptions.join('\n\n'), input, output, note, samples
                };
            }""")
        finally:
            pass  # External CDP browser must remain running


def statement_to_problem(data):
    sections = []
    for heading, key in (("Description", "description"), ("Input", "input"),
                         ("Output", "output"), ("Notes", "note")):
        if data.get(key):
            sections.append(f"## {heading}\n\n{data[key]}")
    return {"time_limit": data.get("time_limit"), "memory_limit": data.get("memory_limit"),
            "statement": "\n\n".join(sections) + "\n", "samples": data.get("samples", [])}


def fetch_problem_statement(contest_id, index):
    return statement_to_problem(fetch_problem_statement_data(contest_id, index))


def open_submit_page(page, contest_id, index):
    url = f"https://codeforces.com/problemset/problem/{contest_id}/{index}"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    if not re.search(rf"/problem/{contest_id}/{re.escape(index)}(?:$|[?#])", page.url):
        raise CodeforcesMainError("PROBLEM_ID_MISMATCH")
    entry = page.get_by_role("link", name="Submit", exact=True)
    if not visible(entry):
        raise CodeforcesMainError("Could not locate Submit entry.")
    entry.first.click()
    page.wait_for_load_state("domcontentloaded")
    return page.url


def locate_form(page, contest_id, index):
    problem_select = page.locator('select[name="submittedProblemIndex"]')
    problem_input = page.locator('input[name="submittedProblemCode"]')
    language = page.locator('select[name="programTypeId"]')
    file_input = page.locator('input[type="file"][name="sourceFile"]')
    text_input = page.locator('textarea[name="source"]')
    submit = page.locator('input[type="submit"], button[type="submit"]')
    visible_submits = [submit.nth(i) for i in range(submit.count()) if submit.nth(i).is_visible()]
    problem = problem_select if visible(problem_select) else problem_input
    if not visible(problem) or not visible(language):
        raise CodeforcesMainError("Problem or language selector is unavailable.")
    expected_problem = f"{contest_id}{index}"
    if problem.evaluate("element => element.tagName") == "SELECT":
        selected_text = problem.locator("option:checked").inner_text().strip()
    else:
        problem.fill(expected_problem)
        selected_text = problem.input_value().strip()
    if expected_problem.lower() not in selected_text.lower():
        raise CodeforcesMainError("PROBLEM_ID_MISMATCH")
    source_kind = "file" if file_input.count() else ("text" if visible(text_input) else None)
    if not source_kind:
        raise CodeforcesMainError("Source input is unavailable.")
    if len(visible_submits) != 1:
        raise CodeforcesMainError("Submit button is not uniquely identified.")
    options = [language.locator("option").nth(i).inner_text().strip()
               for i in range(language.locator("option").count())]
    available = [name for name in COMPILER_PRIORITY if any(name in option for option in options)]
    if not available:
        raise CodeforcesMainError("No supported GNU G++ compiler is available.")
    return {"problem": problem, "language": language, "file": file_input,
            "text": text_input, "source_kind": source_kind, "submit": visible_submits[0],
            "compiler": available[0], "available": available, "problem_text": selected_text.strip()}


def select_compiler(form):
    options = form["language"].locator("option")
    for index in range(options.count()):
        option = options.nth(index)
        if form["compiler"] in option.inner_text():
            value = option.get_attribute("value")
            form["language"].select_option(value=value)
            if form["compiler"] not in form["language"].locator("option:checked").inner_text():
                raise CodeforcesMainError("LANGUAGE_SELECTION_FAILED")
            return
    raise CodeforcesMainError("LANGUAGE_SELECTION_FAILED")


def fill_source(page, form, code_path):
    expected = hashlib.sha256(code_path.read_bytes()).hexdigest()
    if form["source_kind"] == "file":
        form["file"].set_input_files(str(code_path.resolve()))
        name = form["file"].input_value().replace("C:\\fakepath\\", "")
        if name != code_path.name:
            raise CodeforcesMainError("SOURCE_FILL_FAILED")
        actual = form["file"].evaluate("""async input => {
            const bytes = await input.files[0].arrayBuffer();
            const hash = await crypto.subtle.digest('SHA-256', bytes);
            return [...new Uint8Array(hash)].map(x => x.toString(16).padStart(2,'0')).join('');
        }""")
    else:
        code = code_path.read_text(encoding="utf-8")
        form["text"].fill(code)
        actual = hashlib.sha256(form["text"].input_value().encode("utf-8")).hexdigest()
    if actual != expected:
        raise CodeforcesMainError("SOURCE_VERIFICATION_FAILED")
    return expected


def inspect(contest_id, index, code_path, dry_fill=False):
    require_gui()
    with playwright_api()() as playwright:
        context = open_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            if not logged_in_handle(page):
                raise CodeforcesMainError("Codeforces login session is not available.")
            open_submit_page(page, contest_id, index)
            form = locate_form(page, contest_id, index)
            print("[Codeforces] Problem page: OK")
            print("[Codeforces] Submit entry: OK")
            print(f"[Codeforces] Problem selection: {contest_id}{index}")
            print("[Codeforces] Language control: OK")
            print(f"[Codeforces] Available C++ compilers: {', '.join(form['available'])}")
            print(f"[Codeforces] C++ compiler: {form['compiler']}")
            print(f"[Codeforces] Source input: {form['source_kind']}")
            print("[Codeforces] Submit button: OK")
            if dry_fill:
                select_compiler(form)
                fill_source(page, form, code_path)
                print("[Codeforces] Dry fill compiler: PASS")
                print("[Codeforces] Source verification: PASS")
            print("[Codeforces] Inspection PASS")
            print("[Codeforces] No submission performed.")
        finally:
            pass  # External CDP browser must remain running


def submit_and_wait(problem, code_path):
    if problem.get("contest_phase") != "FINISHED":
        raise CodeforcesMainError("ACTIVE_CONTEST_NOT_SUPPORTED")
    require_gui()
    from codeforces import get_user_submissions, wait_for_verdict
    handle = configured_handle()
    before = get_user_submissions(handle)
    before_id = max((item.get("id", 0) for item in before), default=0)
    with playwright_api()() as playwright:
        context = open_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            if logged_in_handle(page) != handle:
                raise CodeforcesMainError("Codeforces login handle mismatch.")
            open_submit_page(page, problem["contest_id"], problem["index"])
            form = locate_form(page, problem["contest_id"], problem["index"])
            select_compiler(form)
            digest = fill_source(page, form, code_path)
            form["submit"].click()
            result = wait_for_verdict(handle, before_id, problem["contest_id"], problem["index"])
            result.update({"provider": "codeforces-main", "submitted": True,
                           "submission_sha256": digest})
            return result
        finally:
            pass  # External CDP browser must remain running


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--login", action="store_true")
    modes.add_argument("--check-login", action="store_true")
    modes.add_argument("--inspect", metavar="PROBLEM_ID")
    parser.add_argument("--code", type=Path)
    parser.add_argument("--dry-fill", action="store_true")
    args = parser.parse_args()
    if args.dry_fill and not args.inspect:
        parser.error("--dry-fill requires --inspect")
    try:
        if args.login:
            login()
        elif args.check_login:
            check_login()
        else:
            from codeforces import parse_problem_id
            if not args.code or not args.code.is_file():
                parser.error("--inspect requires an existing --code file")
            contest_id, index = parse_problem_id(args.inspect)
            inspect(contest_id, index, args.code, args.dry_fill)
    except CodeforcesMainError as exc:
        print(f"[Codeforces] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
