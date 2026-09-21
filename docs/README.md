# py-common Documentation

Three documents, each answering a different question. Start at the
top; go deeper only as your question demands it.

| Doc | Answers | Read this if... |
|---|---|---|
| [`GETTING_STARTED.md`](GETTING_STARTED.md) | How do I install this, run it, and see it actually process a file? | You just cloned the repo. |
| [`USAGE_GUIDE.md`](USAGE_GUIDE.md) | Is py-common the right tool for my new source? How do I add an adapter? | You're about to build a new ingestion feed. |
| [`INGESTION_FRAMEWORK_GUIDE.md`](INGESTION_FRAMEWORK_GUIDE.md) | What, precisely, works today versus what's stubbed or missing — with a file and line number for every claim? Also: the ingestion vocabulary, A–Z. | You need to know exactly what you can rely on, or you're deciding what to build next. |

Two other files live outside `docs/` and are referenced from all three
above rather than repeated here:

- **`CONTRIBUTING.md`** (repo root) — dev workflow, TDD/PR process,
  test conventions.
- **`DECISIONS_en.md`** (repo root) — the design-decision log: why the
  codebase looks the way it does.
