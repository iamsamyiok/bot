# musebot — Your self-hosted personal AI butler — inspired by Muse & Grok Bot

[中文](README.md) | **English**

> A personal AI butler that runs on your own computer: Feishu / smart speaker / web / CLI, 11 dedicated workers, memory that compounds into a digital twin of you, **the full pipeline works on free model tiers**.

Same idea as Muse / Grok Bot — but nothing runs on a vendor cloud, no subscription, and your data never leaves your machine.

## What it does

- **One sentence starts the job** — the Router brain classifies intent, dispatches the right worker, and pushes results to Feishu / phone / speaker when done.
- **Memory that compounds** — conversations settle into a three-layer digital twin (daily / digest / procedure), with conflict resolution. "What kind of tools do I like?" gets an answer built from your history.
- **Skills distill themselves** — after 3 successes of the same task type, a skill card is auto-extracted; replay it with one sentence.
- **Approval gate** — 16 classes of risky actions (send email, transfer, delete...) pause the worker and push an approval card to Feishu. Waiting costs zero process and zero tokens.
- **Routines** — "every day at 9am, summarize GitHub trending + my city weather" created in one sentence.
- **Quota-aware (optional)** — reads your LLM plan's 5-hour usage window; full speed when rich, auto-fallback to free chains when low.
- **Self-healing** — a watchdog revives the butler and companion services every minute.

## 4 channels · 11 workers

Feishu (text/image/file/approval cards) · smart speaker (14 MCP tools) · web chat · CLI.
Workers: codex (private tasks), opencode (public tasks, free LLM, auto-fallback to codex), ufo (Windows GUI), search, weather, note, reme (memory), research, review, mail, files, weekly.

## Quick start

```bash
git clone https://github.com/iamsamyiok/musebot.git && cd bot
cp config/bot.example.json config/bot.json   # optional: your base_url/key/model
python butler.py
```

Responsible API design: no vendor is built in. Fill your own OpenAI-compatible
`base_url` + `model` + `api_key_env` in `config/bot.json` (local Ollama works).
Keys live only in your local bot.json — gitignored.

See [docs/CHANNELS.md](docs/CHANNELS.md) for Feishu app setup and speaker binding.

## Requirements

Windows 10/11 recommended (GUI worker + watchdog are Windows-integrated); Linux/macOS run the core. Python 3.11+. Any OpenAI-compatible LLM endpoint — **free tiers work, no GPU needed**.

## Privacy

Memory, task ledger, config: all local (`~/.musebot/`). Keys: local bot.json only, never committed. Your LLM provider sees your chats — use a local model or the private codex worker for sensitive work.

## License

[MIT](LICENSE)
