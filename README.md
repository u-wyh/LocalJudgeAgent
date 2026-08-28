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

可用 `python3 agent.py --inject-ce` 在 v0 注入一个编译错误，真实验证错误反馈与修复闭环；该选项仅用于测试，不改变正常生成逻辑。

未来目标：题目自动获取 → 本地增强测试 → OJ Adapter → 获取真实评测反馈 → 自动修复 → 批量成功率实验。
