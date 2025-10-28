Scope: /

Purpose: Ensure persistent, human-readable session logs for every agent session and conversation.

Approvals directive:

- /approvals always
- Interpretation: Agents should proceed autonomously within repo scope without prompting for per-command approval. If the harness enforces a different policy, agents should proactively request escalation as needed to fulfill tasks, while avoiding destructive actions beyond repo scope.
  - Do not prompt the user for per-command approvals during normal work within this repo. Operate autonomously and log actions in `docs/session_log.md`.
  - If a required command is blocked by sandboxing or approval policy, request a single escalation for the minimal necessary scope, then continue autonomously. Avoid destructive actions beyond the repo scope.

Startup requirement:

- On startup in this repository (or any directory under its scope), the agent must read `docs/session_log.md` before taking other actions to rehydrate context from prior sessions.
- At the first assistant turn in a new session (before responding to the user), both:
  - Append a brief "Context restored" entry to `docs/session_log.md` summarizing the understood context (project purpose, current behavior/config, notable recent changes). If log write fails, proceed and note failure in the next entry.
  - Echo a one‑line "Context restored" notice in chat (the first assistant message) summarizing the same context at a high level.
  - Include a runtime environment snapshot in both the log entry and the chat line: approvals policy, filesystem sandbox mode, and network access (e.g., "approvals on-request; workspace-write; network restricted").

Session identifiers:

- On the first assistant turn of each new session, generate a short session ID of the form `sess-YYYYMMDDTHHMMSS-xxxxxxxx` (8 lower-hex chars). Any reasonable method is acceptable (e.g., timestamp + random/uuid suffix).
- Include this session ID in:
  - The "Context restored" log entry and the chat line.
  - All subsequent log entries created by that same session.
- Purpose: Disambiguate concurrent sessions writing to the shared log.

Instructions for agents working in this repository:

- Always maintain a running session log at `docs/session_log.md` for every session and every conversation.
- If the file does not exist, create it with a top-level heading and a short intro.
- After each user–assistant exchange and after each meaningful action (planning, edits, patches, refactors, releases), append an entry with:
  - Timestamp (ISO-8601, local if available)
  - Actor (agent or user)
  - Summary of actions, outcomes, and rationale
  - Files touched (paths) or “(conversation only)”
  - Commands run (if any, concise)
  - Commit hash and subject if a commit was made (or requested)
- Keep entries concise and scannable; prefer bullets. Avoid duplicating entire diffs.
- Do not block on failures to write the log; continue the main task and note the failure in the next entry.

Formatting template (append to the end of the file):

```
## [YYYY-MM-DDTHH:mm:ss±ZZ:ZZ] Session entry
- Actor: <agent or user>
- Session: <sess-...> (when available)
- Summary: <what changed and why>
- Files: <a, b, c>
- Commands: <cmds if any>
- Commit: <hash> <subject>
```

Purpose: Read `docs/session_log.md` to establish context for this project.

Fallback policy:

- Do not add fallback behavior or alternate data sources in code changes unless explicitly requested by the user in this conversation.
- Prefer fail-fast behavior with clear, minimal messaging over hidden fallbacks.
- Example: For metadata commands (like `info`), report only what the primary source exposes; do not read EXIF/secondary sources unless asked.

Tone and humor policy:

- Default tone: clear, concise, and friendly.
- Humor level: approximately 70% of baseline — light, occasional quips that never obscure instructions or diagnostics.
- Never let humor reduce clarity, precision, or safety. Technical content and action items take priority.
- Honor user requests to adjust tone (e.g., “humor up/down”) within these constraints.
