# 08 — Working with the owner

Read this early. It changes how you should respond, not just what you build. These are
observed, durable preferences — treat them as standing instructions.

## Standing rules (non-negotiable)

1. **Never guess about strategies — read the source first.** Verbatim from the owner:
   *"DO NOT GUESS, Search the directory …/i-want-to-build-an-algo and analyse the
   strategyV2 py file for the strategies, always."* Before answering anything about
   strategy behavior, read the research repo's `strategies_v2.py` and `engine_v2.py`.
   This is the owner's #1 rule. Violating it erodes trust fast.

2. **Only commit or push when explicitly asked.** Not proactively, not "to be safe." The
   owner says "commit" / "commit and push" when they want it. Until then, stage the change,
   show the diff, and hand over a runbook. (This repo pushes toward a production deploy, so
   the rule has teeth.)

3. **Real money demands caution.** The live bot is real. The owner expects a parity-gated,
   shadow-validated migration discipline and will not flip real money to an unproven
   strategy. When a change is near real money, name the risk and the safeguard explicitly.

## How the owner works

- **Runs all VPS/infra commands themselves.** No SSH from Claude. The workflow is: Claude
  writes a precise runbook (copy-paste `bash` blocks, one command each), the owner runs it
  on the VPS and pastes the raw output back, Claude interprets. Design every deploy
  interaction around this loop.
- **Iterates in explicit phases.** Big work is broken into numbered phases with a clear
  gate between them ("commit and continue to phase 2"). The owner approves transitions,
  sometimes via a multiple-choice question. Keep the phase structure visible.
- **Shares debug bundles.** Periodically drops a `paper-trading-debug-*.md` bundle and asks
  a pointed question about it. Answer from the bundle's actual contents, not assumptions.
- **Technical and hands-on.** Comfortable with SQL, `psql`, PM2, and reading tracebacks.
  You don't need to over-explain basics — but do explain *root causes*.
- **Runs a parallel research project.** The `i-want-to-build-an-algo` backtester is the
  owner's; strategy work happens there and is vendored here. The owner thinks in terms of
  strategy iterations (S404, S455, S505, Round-59, walk-forward validation).

## How the owner wants answers

- **Root cause over quick patch.** When something looks wrong (e.g. the DRREDDY "28 buys"),
  the owner wants the actual mechanism explained — verified against the code — not a
  hand-wave or an immediate patch. Show the evidence chain. Distinguish "real bug" from
  "cosmetic artifact" clearly, and prove which it is.
- **Grounded, not guessed.** Verify claims against the code/DB before asserting them. If
  you're inferring, say so. The owner notices the difference.
- **Concrete and copy-pasteable.** Prefer exact commands, exact SQL, exact file:line
  references over prose. One command per fenced `bash` block (the UI adds a Run button).
- **Direct and concise.** The owner asks short, specific questions and wants precise
  answers, not padding. Lead with the answer.
- **Safety called out.** For anything touching real money or production, state what's safe,
  what's gated, and what you're deliberately *not* doing.

## Communication defaults

- The owner's pronouns haven't been stated — use **they/them**.
- Timezone is **IST** (India). Convert and label times in IST; remember the DB stores UTC
  (IST = UTC+5:30).
- Currency is **₹ (INR)**; "1 lakh" = 100,000.
- Dev machine is **Windows** (PowerShell); the VPS is **Ubuntu**. Give Windows-appropriate
  local commands and Ubuntu-appropriate VPS commands.

## A good interaction, in shape

1. Owner asks a pointed question or shares output.
2. Claude verifies against the actual code/DB/bundle (reading the research repo first if
   it's a strategy question).
3. Claude leads with the answer, shows the evidence, distinguishes real vs cosmetic, and
   names any real-money risk.
4. If action follows, Claude stages it, shows the diff, and provides a runbook — without
   committing or pushing unless asked.
