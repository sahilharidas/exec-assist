# exec-assist

A task-closing assistant that lives in a Telegram chat. An LLM agent handles the
conversation; a small deterministic Python engine, exposed to the agent as MCP
tools, decides everything that has to be right.

I built it for myself and have run it daily since September 2026 against my own
Obsidian vault. This repository is a cleaned copy of that system: same code,
with my paths, bot names, notes and a few personal references removed, and a
made-up sample vault for the tests.

## The idea: the model talks, Python decides

```
   you  <-->  agent gateway (Telegram, speech-to-text, LLM)
                   |
          MCP over SSE (private docker network)
                   |
              mcp_server  ---->  ea/  (the engine)
                   |               |
                   +----> vault (markdown) + state/tasks.json
                                   ^
                 cron jobs --------+  (deterministic, send-only)
```

The agent is [Hermes Agent](https://github.com/NousResearch/hermes-agent), an
open-source framework I run in Docker; I did not write it. What I wrote is
everything below it: the engine, the MCP server, the scheduled jobs and the
gateway plugins.

The split exists because some questions cannot be answered by a model reading
notes. "What have I been putting off?" depends on when each item was first seen,
how often it has already been nagged about and whether another copy of it was
ticked somewhere else. That lives in a state store, so it is computed in Python
and handed to the model as a finished answer.

## What is in here

| Path | What it is |
|---|---|
| `ea/` | The engine: markdown task parser, task identity and de-duplication, the state store (ages, completions, nag cooldowns), shortlist scoring, fuzzy matching, Telegram transport, local speech-to-text, OpenRouter client |
| `mcp_server/` | FastMCP server exposing the engine as 12 tools over SSE, with its Dockerfile and compose file |
| `bin/` | Scheduled jobs (morning carry-over, midday nudge, evening prompt, weekly review, nightly tidy) and the original standalone bot |
| `hermes_plugins/` | Two gateway plugins: inline Done / Drop buttons on task nudges, and one-off reminders with Done / Snooze buttons. Button taps call the MCP server directly; no model runs |
| `tests/` | 25 tests over the engine and the tool boundary, run against `tests/sample_vault` |

### MCP tools

`today`, `snapshot_note`, `capture`, `add_task`, `complete`, `close_task`,
`stale`, `waiting`, `frog`, `task_context`, `log_focus`, `log_shutdown`.

All of them are deterministic: no LLM calls and no network inside the server.

## Failure modes I hit, and what catches each one now

- **The model tidied what it was asked to store.** "chase the ER claim thing
  before fri" came back as a neat sentence with the useful detail gone. The
  `capture` tool description now demands the user's words verbatim and says why,
  and the server stores exactly the string it receives
  (`tests/test_tools.py::Capture`).
- **The model ticked the wrong task.** A one-word query matched confidently.
  `complete` now refuses to act unless the match is exact or substantial and
  clearly ahead of the runner-up; otherwise it returns the candidates so the
  agent has to ask (`ea/match.py`, `tests/test_tools.py::Complete`).
- **Two processes polled one Telegram bot token.** Telegram allows one
  `getUpdates` consumer per token, so the agent and my own bot silently stole
  each other's messages. Inbound and outbound are now separate concerns: the
  poller requires a token of its own, takes an exclusive lock, and refuses to
  fall back to the shared token; scheduled jobs only ever send.
- **The first run "completed" 246 tasks at once.** Items that were already
  ticked when tracking began were stamped as finished that night, and the weekly
  review confidently reported a Monday-night work habit. A completion time is now
  recorded only on an observed open-to-done transition
  (`ea/store.py`, `tests/test_engine.py::Store`).
- **Ticked items came back.** The same item often sits in several notes, and it
  stayed open while any copy was open. A tick in one note now closes the other
  copies on the next scan, but only for items the store has counted before, so a
  first scan never rewrites anything.
- **A model must not paraphrase a reminder.** One-off reminders are stored as
  text and sent verbatim by a loop in the gateway; no model runs at send time.
- **An age that is a lower bound must stay one.** Ages recovered from old
  backups are shown as `9+d`, and the tool description tells the model not to
  round that down.

## Running the tests

Standard library only; no install needed.

```bash
python3 -m unittest discover -s tests -t .
```

The tool tests import the real server module. Where `fastmcp` is not installed
they use a stand-in decorator, so the tool bodies under test are still the real
code. They also pass inside the server image with `fastmcp` installed.

These are tests of the deterministic side and of the tool boundary. There is no
scored evaluation set for the model's own behaviour yet (for example, how often
it paraphrases before calling `capture`); that is the next thing to build.

## Running the server

```bash
cp .env.example .env            # then fill it in
docker network create hermes_mcp
cd mcp_server && docker compose --env-file ../.env up -d --build
```

The agent connects with:

```yaml
mcp_servers:
  exec_assist:
    url: http://ea_mcp:8000/sse
    transport: sse
```

Nothing is published on the host; the server is reachable only from the agent's
docker network. It runs as an unprivileged user and its only write access is the
vault and the state directory.

The scheduled jobs are plain scripts; `crontab.example` shows the schedule.

## Vault conventions the parser reads

- `- [ ] thing` open, `[x]` done, `[-]` or `[~]` cancelled, `[>]` carried
  forward to a later note.
- `[WAIT]` blocked on someone else, `[FROG]` the one task being avoided,
  `[LOW]` fine for a low-energy day.
- Due dates: `📅 2026-09-20`, `due:2026-09-20` or `@2026-09-20`.
- `### DOMAIN: WORK` headings and `**Bold**` sub-headings are kept as context.

## Data handling

The vault holds personal notes, so the design keeps it local: voice notes are
transcribed on the box with `faster-whisper`, the MCP server makes no outbound
calls, secrets are read from an env file and never logged, and error paths log
exception types where a URL could contain a token. Only the weekly review and
the standalone bot's rewrites send text to a hosted model, through OpenRouter.

## Known gaps

- Capture quality depends on the model obeying the tool description; it is
  spot-checked by hand, not measured.
- Due dates are opt-in, so overdue detection is only as good as the notes.
- Folder names (`00 📅 Daily`, `TODO.md`) follow my vault and are set in
  `ea/config.py`.
