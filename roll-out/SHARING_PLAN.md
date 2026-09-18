# Sharing Plan: Presenting py-common and HWW Integration

Strategy for rolling out py-common to the data engineering team without overwhelming them or raising red flags about AI generation.

---

## Phase 1: Informal Introduction (Week 1)

### Goal
Get team buy-in and gather feedback early.

### Approach

**1. Email Summary (Not a long doc)**

Subject: "New Python ingestion library for data projects"

Body:

> Hi team,
>
> We've put together a Python library (py-common) for handling the repetitive parts of file ingestion: validation, storage, auditing. Fewer people need to write this from scratch.
>
> Quick facts:
> - Works with local files (no cloud setup needed for testing)
> - Validates Excel files against schemas you define
> - Keeps audit records for every attempt
> - About 1,500 lines, well-commented, full test coverage
>
> We've also created an example (rdd-dst-healthyworkingwales) showing how a real project would use it.
>
> Interested in a walkthrough? It's about 30 minutes hands-on.

**2. One-on-One Conversations**

Talk to 1-2 senior developers:
- "Does this structure make sense?"
- "What would you change?"
- "Would you use this in your next project?"

Collect honest feedback before the wider presentation.

### Materials to Share
- WALKTHROUGH.md (the 30-minute hands-on guide)
- Link to py-common/README.md
- A single screenshot of the pipeline (visual diagram)

---

## Phase 2: Hands-On Workshop (Week 2)

### Goal
Let people experience it, ask questions, see it work.

### Format

**30-minute workshop** (not a presentation)

**First 10 minutes: Setup**
- Everyone clones take5
- Installs py-common and rdd-dst-healthyworkingwales
- Runs the tests once (`pytest tests/ -v`)
- Sees "17 passed"

**Next 15 minutes: Run the Full Pipeline**
- Everyone follows WALKTHROUGH.md Part 5
- Copies a test fixture into test_files/
- Runs `python main.py`
- Sees output directory with raw/processed/audit files
- Opens an audit record in a text editor

**Last 5 minutes: Questions**
- "Why three adapters?"
- "How does it handle errors?"
- "When would I use this vs. writing my own?"

### Why This Works
- People see it working in their own terminal
- No slides, no abstractions
- Immediate, tangible feedback
- Easier to discuss code they're looking at than theory

### Materials to Bring
- A single printed copy of WALKTHROUGH.md (bookmark key sections)
- A laptop with py-common already working (for demo if needed)
- A list of 3-5 common questions and answers

---

## Phase 3: Code Review Cycle (Weeks 3-4)

### Goal
Gather feedback from people who read code carefully.

### Approach

**1. GitHub / Internal Code Review (Optional)**

If your team uses internal git:
- Create a pull request with py-common
- Request review from 2-3 senior developers
- Invite comments on:
  - Architecture (is it clear?)
  - Code readability (any confusing parts?)
  - Test coverage (are we missing scenarios?)
  - Naming (are variable names sensible?)

**2. Focus Reviews on Specific Modules**

Don't ask them to review 1,500 lines. Break it down:

**Week 3a: Contracts**
- "Look at `py_common/contract.py` (70 lines)"
- "Does this validate Excel workbooks the way you'd expect?"
- Comment on specific functions

**Week 3b: Pipeline**
- "Look at `py_common/pipeline.py` (150 lines)"
- "Is the flow clear?"
- "What error cases do we need to handle?"

**Week 4: Integration**
- "Look at `rdd_dst_healthyworkingwales/ingest.py` (100 lines)"
- "Would you use this pattern in your project?"
- "What would you change?"

**3. Track Feedback**

Create a simple spreadsheet:

| Module | Reviewer | Comment | Resolution |
|--------|----------|---------|------------|
| contract.py | Alice | Missing date type | Added to TODO |
| pipeline.py | Bob | Clear structure | ✅ Approved |

---

## Phase 4: Documentation Review (Week 4)

### Goal
Make sure docs are clear to someone who's never seen the code.

### Approach

**1. Send README + WALKTHROUGH to One Person**

Ask: "You have never seen this code before. Can you follow WALKTHROUGH.md and run the tests?"

Observation:
- Where do they get stuck?
- What words are unclear?
- What questions do they ask?

**2. Iterate Based on Feedback**

Common feedback:
- "This section was confusing" → Rewrite with an example
- "I didn't know what X meant" → Add a definition
- "I couldn't find X" → Add a link or reorganize

**3. Finalize README**

Make sure it covers:
- ✅ What py-common does (one paragraph)
- ✅ Architecture diagram (visual)
- ✅ Quick start (5 steps, no dependencies)
- ✅ Examples (real code, not pseudocode)
- ✅ Where to ask questions

---

## Phase 5: Team Adoption (Week 5+)

### Goal
Make py-common the default starting point for new ingestion projects.

### Approach

**1. Feature Announcement**

Email to team:

> py-common is now available for use in data ingestion projects.
>
> **Use it if:**
> - You're reading Excel files
> - You need to validate structure or data
> - You need an audit trail
>
> **Don't use it if:**
> - You're working with databases directly (not file ingestion)
> - You need real-time streaming (batch only)
>
> **Start here:** [link to WALKTHROUGH.md]

**2. Default Template**

When someone starts a new ingestion project:
- Copy rdd-dst-healthyworkingwales as a starting point
- Replace "Events" contract with their actual schema
- Run tests to verify locally
- Add their adapters (SharePoint, API, etc.) when ready

**3. Monthly Check-ins**

"Anyone used py-common yet?"
- Gather feedback from real use
- Track issues and feature requests
- Prioritize phase 2 adapters (SharePoint, GCS, BigQuery)

---

## FAQ: How to Answer Common Questions

### "Did you write this yourself or use AI?"

**Honest answer:**
"I used Claude AI to help with the boilerplate and initial structure. But the architecture, design decisions, and testing approach are based on public standards (RAP, PEP 8, pytest patterns). Every line was reviewed for correctness before committing."

Why this matters:
- Transparency builds trust
- AI-assisted development is normal in 2026
- What matters is the code works and is well-tested

### "Is it production-ready?"

**Honest answer:**
"Core pipeline and local testing are production-ready. Cloud adapters (SharePoint, BigQuery) are phase 2 and will follow once we have credentials."

This shows:
- Incremental delivery
- Honest scope
- Plan to extend

### "Why not use [existing library]?"

**Honest answer:**
"We looked at [X]. It's great, but [reason we chose this]. This is smaller, fits our specific workflow, and gives us control over validation rules."

Examples:
- Apache Airflow: Overkill for simple file batches
- AWS Glue: Tied to AWS; we're GCP-first
- Custom solution: We'd rebuild this in every project

### "Can I use this for my project?"

**Yes, but with caveats:**
- Yes if: File ingestion, validation, storage
- Maybe if: You need to extend with custom logic (we have extension points)
- No if: Real-time streaming, ML pipelines, database-first workflows

---

## Avoiding Red Flags

### ✅ Do This

- Be honest about AI involvement (see above)
- Emphasize testing and code review
- Point to clear, readable code
- Share the process (DECISIONS log)
- Admit what's not done yet (cloud adapters)

### ❌ Don't Do This

- Pretend you wrote it alone
- Overclaim features ("production-ready" when it's phase 1)
- Refuse feedback or ignore issues
- Hide limitations
- Rush rollout

---

## Timeline Summary

| Week | Activity | Owner | Audience |
|------|----------|-------|----------|
| 1 | Email intro + 1:1 feedback | You | 2-3 seniors |
| 2 | Workshop (30 min, hands-on) | You | Full team |
| 3 | Code review (contracts, pipeline) | Team | Volunteers |
| 4 | Code review (integration) + docs feedback | Team | Reviewers |
| 5 | Team adoption & announcements | You | Team + leads |

---

## What to Expect

### Likely Reactions

**Positive:**
- "This is cleaner than our current approach"
- "Saves us writing validation code"
- "Love the audit trail"

**Neutral:**
- "Interesting, but I'm good with my current setup"
- "How does this compare to [tool]?"

**Skeptical:**
- "Another library to maintain?"
- "When do we get SharePoint support?"
- "Is this just for Excel?"

**Your Response:**
- Acknowledge the concern
- Share the decision log (shows we've thought about it)
- Point to documentation (shows we've documented it)
- Invite them to contribute (makes it collaborative)

### Success Metrics

You'll know it's working when:
- ✅ Someone uses it for a new project without asking for help
- ✅ Someone contributes a feature or bug fix
- ✅ Someone recommends it to a colleague
- ✅ It saves someone time (they tell you)

---

## Documentation Checklist

Before sharing with the team, ensure you have:

- ✅ README.md (architecture, quick start, examples)
- ✅ WALKTHROUGH.md (hands-on, 30 minutes)
- ✅ DECISIONS_en.md (design rationale)
- ✅ Docstrings in code (Google style, visible in IDE)
- ✅ Tests that pass (pytest tests/ -v)
- ✅ Examples that work (main.py runs, test files included)
- ✅ Configuration examples (local and prod)

---

## Final Thoughts

This library is useful because:
1. **It's focused** — Just file ingestion, not everything
2. **It's tested** — Full coverage, real test scenarios
3. **It's documented** — Clear rationale, not mysterious
4. **It's extensible** — Add adapters as needed
5. **It's honest** — Admits what's not done, plans for the future

Share it with that framing, and the team will get value from it.
