# LocalJudgeAgent

LocalJudgeAgent 是一个本地 AI 算法解题实验系统。

当前 Phase 1：

```text
题面 → GPT-OSS → C++17 → 编译 → 样例测试 → 错误反馈 → 自动修复
```

- Model: `gpt-oss:20b`
- Ollama: `http://127.0.0.1:11434`
- Context: `32768`

运行：

```bash
python3 agent.py
```

程序读取 `problem.json`，保存每一版 prompt、模型响应、源代码、编译和样例结果到唯一的 `runs/<题号>_<时间>/` 目录，并最多自动修复三次。

可用 `python3 agent.py --inject-ce` 在 v0 注入一个编译错误，真实验证错误反馈与修复闭环；该选项仅用于测试，不改变正常生成逻辑。

未来目标：题目自动获取 → 本地增强测试 → OJ Adapter → 获取真实评测反馈 → 自动修复 → 批量成功率实验。
