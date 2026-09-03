@AGENTS.md

# Claude Code entry point

Read `TEAM-PROSPECTING-GUIDE.md` and `AUTOMATION.md` before operating or changing the
pipeline. For email work, also read `SEND-RUNBOOK.md`.

Treat the imported `AGENTS.md` as the shared source of truth for both Claude Code and
Codex. Do not infer current campaign state from historical markdown or CSV files; inspect
the CLI status, health, SQLite-backed dashboard, and relevant tests.

Claude Code project memory is context, not a substitute for code-enforced approval,
suppression, cap, and hash checks. Confirm this file loaded with `/context` when onboarding
a new clone. Reference: https://code.claude.com/docs/en/memory
