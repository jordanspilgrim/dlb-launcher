# dlb-launcher — wake idle AI-agent CLIs on DLB mail

[![CI](https://github.com/jordanspilgrim/dlb-launcher/actions/workflows/ci.yml/badge.svg)](https://github.com/jordanspilgrim/dlb-launcher/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A tiny PTY wrapper that lets two independent Claude Code / Codex CLI / Gemini CLI sessions actually collaborate via [DLB](https://github.com/jordanspilgrim/dlb-mcp). When mail arrives in the bound thread's inbox, `dlb-launcher` injects a synthetic wake prompt into the wrapped CLI so the LLM notices — without you having to context-switch or type anything.

## Why this exists

[DLB](https://github.com/jordanspilgrim/dlb-mcp) gives two agent sessions a shared mailbox. But MCP is request-response — the server can't push, and an idle LLM CLI sits forever waiting for stdin. Sending mail to a session that nobody is typing into goes unnoticed.

`dlb-launcher` solves that by owning the wrapped CLI's PTY. It transparently relays your keystrokes to the child and the child's output to your terminal, while a background thread watches the DLB SQLite store for new mail addressed to this session. When mail arrives AND the CLI has been idle for ~1s, the launcher writes a synthetic prompt directly into the child's stdin — same effect as if you'd typed it. The LLM wakes, reads its inbox, processes the message, and replies. The OTHER thread (also wrapped with `dlb-launcher`) catches that reply and wakes ITS LLM. Two sessions collaborate with zero manual intervention.

This is the same mechanism `tmux send-keys` uses, packaged as a single-purpose tool with no tmux requirement.

## What it works with

| CLI | PTY wrapping | DLB tool access | Wake prompt recognition | Status |
|---|---|---|---|---|
| **Claude Code** | ✅ | ✅ via MCP (`~/.claude.json`) | ✅ recognized via `CLAUDE.md` instruction | Tested end-to-end |
| **Codex CLI** | ✅ (PTY is OS-level) | ✅ via MCP (`~/.codex/config.toml`) | Add a recognition rule in `AGENTS.md` | Validated, needs your testing |
| **Gemini CLI** | ✅ (PTY is OS-level) | ✅ via MCP (`~/.gemini/settings.json`) | Add a recognition rule in `GEMINI.md` | Validated, needs your testing |

The PTY mechanism is OS-level and CLI-agnostic. The CLI-specific work is just (a) wiring DLB as an MCP server in each CLI's config and (b) telling each LLM (via its instruction file) how to react to a `🔔 DLB-WAKE` prompt.

## Install

```bash
uv tool install dlb-launcher
# or zero-install:
uvx dlb-launcher --name alpha claude
```

Python 3.11+. Stdlib only — zero runtime dependencies.

## Usage

```bash
# Terminal 1
dlb-launcher --name alpha claude

# Terminal 2
dlb-launcher --name bravo claude        # or codex, or gemini
```

Then, inside each session, tell the LLM to register with DLB using the **same name** you passed:

> In alpha's session: "Register me with DLB as `alpha`."
> In bravo's session: "Register me with DLB as `bravo`."

From this point on, when alpha sends a message to bravo (via `mcp__dlb__send`), `dlb-launcher` in bravo's terminal sees the new mail, waits for bravo to be idle, and injects a synthetic prompt. Bravo's LLM reads the inbox and processes the message. Replies travel the same path in reverse.

## How the wake prompt looks

When mail arrives, the wrapped CLI sees this as if you typed it:

```
🔔 DLB-WAKE [bravo]: 1 unread from alpha. Call mcp__dlb__read with name='bravo' and your session_token to fetch them, then surface to the user.
```

The LLM should respond by calling `mcp__dlb__read` and surfacing the message contents BEFORE doing anything else. Make sure your global instructions file (CLAUDE.md / AGENTS.md / GEMINI.md) tells the LLM to react this way — see the [DLB README](https://github.com/jordanspilgrim/dlb-mcp) for the recommended snippet.

## Configuration

| Flag | What |
|---|---|
| `--name <inbox>`, `-n <inbox>` | DLB inbox name to watch. Must match what the LLM inside registers as. If omitted, the launcher is a pure transparent PTY relay (no wake mechanism). |
| `--version` | Print version and exit. |
| `<cli> [args...]` | The CLI to wrap, followed by its own arguments. Everything after `--name` (or the first non-`--name` positional) is forwarded to the child. |

Env vars:
| Var | Default | What |
|---|---|---|
| `DLB_STORE` | `~/.dlb/store.sqlite3` | Path to DLB's SQLite store. Inherited from DLB's own convention. |

## Setup notes per CLI

### Claude Code
Already-configured DLB MCP entry in `~/.claude.json`? You're ready. The `~/.claude/hooks/dlb-inbox-check.sh` hook (if installed) gives you a secondary belt-and-suspenders notification on every user prompt.

### Codex CLI
Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.dlb]
command = "uvx"
args = ["dlb-mcp"]
```

Then add to your `AGENTS.md`:

> If you see a `🔔 DLB-WAKE` prompt, immediately call `mcp__dlb__read` with the name in brackets and your session_token. Surface the messages to the user before doing anything else.

### Gemini CLI
Add this to `~/.gemini/settings.json` (under `mcpServers`):

```json
"dlb": {
  "command": "uvx",
  "args": ["dlb-mcp"]
}
```

Then add the same `🔔 DLB-WAKE` recognition rule to your `GEMINI.md`.

## Known limitations (be honest)

- **Synthetic prompts are visible in the transcript.** Each wake injects a `🔔 DLB-WAKE...` line that looks like a user turn. Cosmetic but unavoidable — there's no Claude Code / Codex / Gemini API for "inject as system message" at this layer.
- **Idle detection is timing-based**, not semantic. If the CLI happens to pause output for >1s mid-turn (rare but possible during long thinking), an injection could land at a slightly awkward moment. We err on the side of waiting (1s threshold) rather than injecting aggressively.
- **`--name` must match the LLM's registered name.** If you launch with `--name alpha` but the LLM registers as `alphabet`, the wake never fires for that session. There's no auto-binding — the launcher trusts what you tell it.
- **Per-CLI brittleness.** PTY wrapping is robust, but each CLI is a moving target — a UI change in Claude Code / Codex / Gemini could alter how output renders, which could break our idle detection. The mechanism is correct; the constants might drift.
- **Not for headless / non-interactive use.** This is for running interactive CLIs in your real terminal. If you want autonomous agents talking to each other with no human anywhere, you want a different architecture (e.g., a daemon that uses the Anthropic / OpenAI / Google SDKs directly).

## License

MIT.
