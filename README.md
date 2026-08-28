# LocalJudgeAgent

LocalJudgeAgent 是一个本地 AI 算法解题实验系统。

当前 Phase 1：

```text
题面 → GPT-OSS → C++17 → 编译 → 样例测试 → 错误反馈 → 自动修复
```

- Model: `gpt-oss:20b`
- Ollama: `http://127.0.0.1:11434`
- Context: `32768`

推荐运行方式：

```bash
python3 agent.py problems/P1001.json
python3 agent.py P1090
```

题目文件统一放在 `problems/`。不传路径时，`python3 agent.py` 仍默认读取根目录的 `problem.json`。

传入洛谷题号时，程序会从公开题目网页获取完整题面和样例，缓存为 `problems/<题号>.json`，然后进入同一套本地求解流程。以后运行相同题号时直接使用缓存。也可以只抓取而不调用模型：

```bash
python3 luogu.py P1030
```

抓题依赖洛谷公开网页中的内部 JSON 数据结构，并非洛谷承诺稳定的官方 API；若页面结构变化，程序会明确报告获取或解析错误。

程序保存每一版 prompt、模型响应、源代码、编译和样例结果到唯一的 `runs/<题号>_<时间>/` 目录，并最多自动修复三次。查看全部实验汇总：

```bash
python3 summarize.py
```

默认模式只完成本地样例评测。`SAMPLE_AC` 只表示通过题目文件中的本地样例，**不等于洛谷 AC**。

## Online Judge

LocalJudgeAgent can optionally use the official Luogu Open Platform for real judge evaluation after local samples pass.

```bash
export LUOGU_OPEN_TOKEN='username:password'
python3 agent.py P1001 --submit
```

The token must never be committed to Git. Online judging uses GNU C++17 (`cxx/17/gcc`) and does not fall back to automated submission on the main Luogu website. `SAMPLE_AC != OJ_AC`.

### Manual OJ Feedback

Without an Open Platform token, run normally and manually submit the generated `submission.cpp`:

```bash
python3 agent.py P1090
python3 agent.py --resume runs/P1090_xxx --oj-result WA --oj-score 0 --oj-record-id 123456789
```

After a failed real result, GPT-OSS repairs the code and locally verifies it before replacing `submission.cpp`. Report final acceptance without another model call:

```bash
python3 agent.py --resume runs/P1090_xxx --oj-result AC --oj-score 100
```

Only `AC`, `WA`, `TLE`, `MLE`, `RE`, `CE`, and `PC` are accepted. In this manual workflow, the user remains responsible for browser submission; no account, Cookie, or CAPTCHA automation is used.

## Luogu main-site browser mode

This optional mode requires Playwright with Chromium and an active desktop `DISPLAY` or `WAYLAND_DISPLAY`. It uses a headed browser and a persistent profile under `~/.local/share/LocalJudgeAgent/luogu-profile/`. Complete the first login yourself in the browser:

```bash
python3 luogu_main.py --login
python3 luogu_main.py --check-login
```

Then a new problem can be solved and submitted through the normal browser UI:

```bash
python3 agent.py Pxxxx --submit-main
```

CAPTCHA, when presented, must be completed manually. The browser profile contains an authenticated session and must never be committed. This mode never reads account passwords, exports Cookies, or calls private submission APIs.

The adapter uses the normal Luogu browser submission UI with the persistent authenticated profile and manual CAPTCHA handling. Main-site UI changes may require locator updates. A non-submitting compatibility check is available with `luogu_main.py --inspect Pxxxx --code submission.cpp`; add `--dry-fill` to verify C++17 selection and editor content without clicking the submit button.

## Codeforces

Codeforces problemset/archive problems use the `CF` prefix and reuse the same solver and local judge:

```bash
python3 agent.py CF4A
python3 agent.py CF4A --submit-cf
python3 agent.py --resume runs/CF4A_xxx --submit-cf
python3 agent.py --resume runs/CF4A_xxx --refresh-cf-verdict
```

The official Codeforces API supplies metadata and verdicts; the normal headed browser UI is used for submission with a separate persistent profile. The browser adapter connects to the existing direct-network Chromium CDP endpoint at `http://127.0.0.1:9222`; it does not launch another browser. Codeforces anti-bot verification rejects automated final submission, so LocalJudgeAgent prepares and verifies the official form while the user performs the final Submit/verification step. The agent then finds a newer matching problem submission through `user.status`, verifies its displayed source (including Codeforces blank-line `U+00A0` normalization), and waits for a known terminal verdict; `SUBMITTED`, `TESTING`, null, and unknown future states continue polling. `oj.submitted` is true only after the new ID and source are confirmed. The submission resume form verifies the saved SHA-256 and never calls the model. The verdict-refresh form only reconciles an already confirmed submission ID and never opens a submission page. Active contests and virtual participation are explicitly unsupported. Use `codeforces_main.py --login`, `--check-login`, and `--inspect 4A --code submission.cpp [--dry-fill]` to prepare the browser adapter without submitting.

## Batch benchmark

Run a benchmark sequentially with the existing solver and local sample judge. Batch mode never enables any online-judge submission option, and Codeforces rating/tags are retained only in records rather than exposed to the model prompt.

```bash
python3 batch.py benchmarks/cf_stage1.json --dry-run
python3 batch.py benchmarks/cf_stage1.json
python3 batch.py benchmarks/cf_stage1.json --resume batch_runs/cf_stage1_800_1200_xxx
```

`batch_record.json` is atomically checkpointed after every problem. Completed `SAMPLE_AC` and `FAILED` entries are skipped on resume; an interrupted active problem is rerun. Use `--limit 1` for a single-problem workflow test. Detailed model artifacts remain in `runs/`, while `batch_runs/` stores only the batch record, summary, and logs.

可用 `python3 agent.py --inject-ce` 在 v0 注入一个编译错误，真实验证错误反馈与修复闭环；该选项仅用于测试，不改变正常生成逻辑。

未来目标：题目自动获取 → 本地增强测试 → OJ Adapter → 获取真实评测反馈 → 自动修复 → 批量成功率实验。
