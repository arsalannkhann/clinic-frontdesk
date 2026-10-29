# Voice agent assets

These are product conversation assets, separate from coding-agent instructions in AGENTS.md and .claude/agents/. No provider-validated deployment JSON exists yet: `clinic-voice bolna compile-agent` compiles `prompts/receptionist.md` into a documentation-derived payload (token redacted) that has never been sent to Bolna. Do not upload these prompts as a complete Bolna agent configuration or make paid calls without integration work and owner authorization.

The receptionist flow covers administrative tasks only. The reminder prompt requires consent and server-verified booking context. Synthetic evaluation scenarios describe expected behavior; no speech-quality results are asserted. Validate current graph/tool/provider schemas before compiling an agent payload. Keep clinic settings in trusted backend configuration, never accept phone or tenant identity chosen by the LLM.
