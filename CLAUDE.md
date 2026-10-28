# Claude Code entry point

Follow [AGENTS.md](AGENTS.md) first. Read [docs/CLAUDE_HANDOFF.md](docs/CLAUDE_HANDOFF.md) for the complete continuation prompt and acceptance criteria. Read source before editing; the handoff describes initialization state, not a guarantee that later commits are unchanged.

Start at milestone 1 in docs/ROADMAP.md: production tenant authentication, clinic onboarding, schedule exceptions and durable provider worker. Keep each slice runnable and covered by PostgreSQL tests. Frontend and live calls follow trusted backend boundaries.

Optional role definitions live in .claude/agents/. They are available for explicit delegation; their presence does not authorize parallel agents, production access, phone purchases, or outbound contact.
