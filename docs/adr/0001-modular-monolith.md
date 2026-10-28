# ADR 0001 Modular monolith with a PostgreSQL reservation ledger

Status: accepted for initialization, 29 October 2026.

The product needs reliable scheduling during conversational retries. Voice vendors and external calendars fail independently, and their state cannot participate in a PostgreSQL transaction.

Use a FastAPI modular monolith, a PostgreSQL reservation ledger with range exclusion, and a transactional outbox consumed by a separate worker process. Keep hosted Bolna/Sarvam behind adapters. Exclude Redis, microservices, and self-hosted audio orchestration from the initial scope.

This keeps local booking invariants enforceable and recovery inspectable. It requires explicit pending states, an eventual calendar consistency policy, and a durable worker before a pilot. External calendar writers can still create conflicts; document ownership and reconcile instead of claiming global atomicity. The foundation reserves anonymous requests and returns pending until adapters exist.
