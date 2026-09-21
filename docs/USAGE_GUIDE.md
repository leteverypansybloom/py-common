# Usage Guide: Is py-common Right for This Source, and How Do I Extend It?

**Audience:** a developer who has already worked through
[`GETTING_STARTED.md`](GETTING_STARTED.md) and is now deciding whether
to build a new ingestion feed on top of `py-common`, or add a new
adapter to it.

**Where this content comes from:** this guide replaces a set of
earlier planning documents (`roll-out/README.md`, `QUICK_REFERENCE.md`,
`SHARING_PLAN.md`, `WALKTHROUGH.md` and `WALKTHROUGH_COMPARISON.md`,
still visible in git history at commit `732f7e6`) that were written
during the initial team roll-out and covered a lot of the same ground
across five separate files — a "Use it when / Don't use it when" list
appeared, slightly differently worded, in three of them. The useful
judgement calls from those documents are consolidated here, once, and
corrected against the code as it stands today rather than as it was
planned in 2026-09.

---

## What py-common actually does

`py-common` is a small **framework** (see
[`INGESTION_FRAMEWORK_GUIDE.md`, Section 3](INGESTION_FRAMEWORK_GUIDE.md#3-framework-vs-solution)
for the framework-vs-solution distinction) for one specific shape of
work: discover Excel workbooks at a source, validate them against a
declared schema, land the raw and processed copies immutably, load
them into a warehouse table, and keep an audit trail of every attempt.
A project that depends on it — a "Healthy Working Wales SharePoint
ingestion" repo, for example — should mostly be a `config.yaml`, a
`Contract` per source, and a thin `main.py` wiring adapters together.

## Use py-common when

- Your source produces **discrete files** — Excel workbooks today,
  specifically, since `Contract.validate()` calls
  `openpyxl.load_workbook()` directly (`contract.py:80-85`).
- You need the file's **structure and data checked** before it's
  trusted anywhere downstream — required worksheets, required and
  unexpected columns, nullability, basic types, uniqueness
  (`contract.py:92-185`).
- You need a **defensible audit trail** — who processed what, when,
  with what checksum and outcome — without extra work
  (`model.py:75-92`, written by `pipeline.py:215-247`).
- You want **adding a new source to be mostly configuration**: a new
  `Contract`, a new adapter satisfying `Source`, and reuse of the
  same `Pipeline`, `ObjectStore` and `Warehouse` — see
  [Adding a new adapter](#adding-a-new-adapter) below.
- You're doing **batch, not streaming**, work — a daily or weekly
  extract, not continuous events.

## Not a fit today — but check "future path" before ruling it out

**Streaming or near-real-time ingestion.** `py-common` is batch-only
by design: `Source.items()` lists everything present at call time,
there is no event/webhook entry point, and nothing in `Pipeline` is
written for long-lived processing. If you have a genuine low-latency
requirement, this isn't the tool, full stop — building streaming
support into a framework designed around "list, then download" would
be a rewrite, not an extension.

**Reading from a database directly, as a source.** This is the one
item worth being careful about, because an earlier planning document
(`roll-out/README.md`, commit `732f7e6`) listed "reading from
databases directly" as a flat "don't use py-common" case. That's true
of the code *today*, but it's an accident of what's been built so
far, not a structural limit of the design — treat it as **not yet
supported**, not as permanently out of scope:

- `Source` (`ports.py:14-45`) only requires `items()` (list what's
  available) and `download()` (fetch one item's bytes). Nothing in
  the protocol assumes a file — a `DatabaseSource` that lists
  "changed since last run" query results and returns each batch as
  bytes would satisfy it structurally, the same way `LocalSource` and
  the (stubbed) `SharePointSource` do.
- The actual blocker is one layer up: `Contract.validate()`
  hard-codes `openpyxl.load_workbook()` (`contract.py:80`), so
  today's contract only knows how to check an Excel workbook. Reading
  from a database would need either a contract variant that accepts a
  `pandas.DataFrame` or CSV/Parquet bytes instead of an Excel
  workbook, or a database source that serialises its query result as
  an in-memory workbook before handing it to the existing contract
  (workable for small extracts, wasteful for large ones).
- **If this becomes a real requirement**, it's an additive piece of
  work — a new `Source` implementation plus a contract-input
  extension — not a redesign of `py-common`. Raise it as a proposed
  adapter (see [Adding a new adapter](#adding-a-new-adapter)) rather
  than writing a one-off database script outside the framework; that
  one-off script is exactly the kind of duplicated, unaudited logic
  `py-common` exists to avoid. The priority list in
  [`INGESTION_FRAMEWORK_GUIDE.md`, Section 9](INGESTION_FRAMEWORK_GUIDE.md#9-priority-order-for-closing-the-gaps)
  has the currently-agreed order of what gets built next; a database
  source isn't on it yet because no project has needed one, not
  because it was rejected.

**Building an ML pipeline.** Not what this is for — use a proper
ML/feature-engineering framework and, if you need governed input data
for it, treat `py-common` as an upstream ingestion step, not the
pipeline itself.

## Adding a new adapter

The pattern is the same for any new `Source`, `ObjectStore` or
`Warehouse` — including a hypothetical `DatabaseSource`:

1. Look at the protocol it must satisfy in `ports.py` and at an
   existing implementation of the same protocol (`LocalSource` is the
   simplest; `SharePointSource` shows the shape of a not-yet-built
   one).
2. Write the test first, against the protocol's documented behaviour
   — see `CONTRIBUTING.md`, "Write Tests First (TDD)".
3. Implement the class. It does **not** need to inherit from
   anything — `Protocol` in `ports.py` uses structural typing, so a
   class satisfies `Source` by having the right methods with the
   right signatures (`CONTRIBUTING.md`, "Protocols vs Implementation"
   has a minimal worked example).
4. If it's a `Source` for a new *kind* of data (not Excel), check
   whether `Contract.validate()` needs a matching extension before
   the new source is actually usable end to end — don't assume "the
   adapter exists" means "the pipeline works," per the database
   example above.
5. Add unit tests with the external service mocked (see
   `tests/unit/test_bigquery_adapter.py` for the pattern using
   `unittest.mock.patch`), and an `integration`-marked test that only
   runs against the real service when credentials are present.
6. Update `INGESTION_FRAMEWORK_GUIDE.md`'s Section 5 status table and
   Section 9 priority list if the new adapter changes either.

## Deciding what belongs in py-common vs. your project

Restated briefly from
[`INGESTION_FRAMEWORK_GUIDE.md`, Section 3](INGESTION_FRAMEWORK_GUIDE.md#3-framework-vs-solution) —
read that section for the full reasoning:

- Put it in `py-common` if more than one feed will need it, or it
  closes a recurring operational risk (retry logic, the audit shape,
  the lock implementation).
- Keep it in your project's own repo if it's about the *meaning* of
  one source — field mappings, what counts as a duplicate event, the
  exact destination table layout.
- Don't add platform complexity — an orchestrator, CDC, a connector
  catalogue — until a real project actually needs it. See
  [`INGESTION_FRAMEWORK_GUIDE.md`, Section 7](INGESTION_FRAMEWORK_GUIDE.md#7-choosing-features-for-your-scenario)
  for a scenario-by-scenario breakdown of what to prioritise.

## What sits outside py-common entirely

Employer/organisation identity resolution, SILVER → GOLD
transformation, and orchestrating multiple dependent jobs are
downstream-of-ingestion concerns that this library deliberately
doesn't take on. See
[`INGESTION_FRAMEWORK_GUIDE.md`, Section 8](INGESTION_FRAMEWORK_GUIDE.md#8-what-sits-outside-py-common)
for the reasoning and the specific tools recommended for each (Splink,
Dataform, Dagster/Prefect).

## Where to go next

| Question | Read |
|---|---|
| "How do I run something for the first time?" | [`GETTING_STARTED.md`](GETTING_STARTED.md) |
| "Is py-common right for my new source? How do I extend it?" | This document |
| "What exactly works today, with evidence?" | [`INGESTION_FRAMEWORK_GUIDE.md`](INGESTION_FRAMEWORK_GUIDE.md) |
| "What's the dev workflow, PR process, test conventions?" | `CONTRIBUTING.md` |
| "Why was it built this way?" | `DECISIONS_en.md` |
