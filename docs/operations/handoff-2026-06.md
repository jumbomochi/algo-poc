# Handoff — 2026-06-10

Pick-up note for the next session. The repo is clean, on `main`, up to date with
`origin/main`. Last 5 commits (all pushed): `fed479f` `a60c162` `635865f`
`96811f4` `22592d0`.

## Orient in 60 seconds

```bash
git log --oneline -10               # what landed recently
cat docs/strategies/portfolio-2026-05.md   # what's running
cat docs/operations/go-live-checklist.md   # promotion gates
cat docs/operations/divergence-monitor.md  # daily monitor (already built)
```

The system runs in **paper mode** today. Go-live has not happened. The
8-gate checklist and the rollback playbook exist; the **Investment Policy
Statement (IPS) is the missing prerequisite** for phase 1.

---

## Active task: write the Investment Policy Statement

The IPS is the **prerequisite for phase 1 go-live**. The operational gates
(`docs/operations/go-live-checklist.md`) and rollback triggers
(`docs/operations/rollback-playbook.md`) already exist — the IPS sits one
level above them and captures the personal-investor framing they don't.

### Context already settled (from 2026-05-27 session)

- System is a **satellite**, not core. Core remains Amundi CW8 (MSCI World).
- Recommended hybrid: **70% CW8 / 30% system**.
- Phased capital deployment plan: paper 3mo → $5K → $25K → $50K → $100K
  (~17 months total).
- Honest alpha vs CW8: ~+7pp CAGR headline, but only ~+2.78pp is durable
  skill alpha; the remaining ~+4pp is US-vs-international concentration
  premium and is regime-dependent.
- The system's real value is **drawdown protection + skill-building +
  optionality**, not pure dollar income. At $30K satellite, durable alpha
  is only ~$834/yr — meaningful but doesn't justify the time-cost as a
  pure return play.
- 6-sleeve config + 9.97-yr backtest results: see
  `docs/strategies/portfolio-2026-05.md`.

### Three open questions blocking the draft

I started to ask these via AskUserQuestion before the handoff. They are
genuinely things the codebase / prior sessions can't answer — the user
needs to commit a number.

1. **Absolute hard ceiling** — what's the most capital this system *ever*
   runs with? Options previously surfaced:
   - $30K (~satellite, matches prior framing)
   - $50K (allow modest scale-up if it beats CW8 risk-adjusted)
   - $100K (the full phase-plan ceiling)
   - Tied to net-worth % (e.g., cap at 5% of liquid net-worth)

2. **Retirement trigger** — at what cumulative $ loss on deployed system
   capital does the user *retire the strategy entirely* (not just rollback
   to paper, shut it down)?
   - -15% of deployed (recommended — symmetric with backtest max DD 10.85% + buffer)
   - -20% of deployed (matches existing `drawdown_circuit_breaker_pct`)
   - -25% of deployed (aggressive — acknowledges regime risk)
   - Fixed-dollar amount (e.g., "-$5K total ever")

3. **Review cadence** — monthly (recommended), quarterly, or event-driven only?

Answers to these three shape the IPS in ways that can't safely be guessed.
Ask the user before drafting.

### Suggested IPS structure (work-in-progress)

A reasonable outline for `docs/operations/investment-policy-statement.md`:

1. **Purpose & role** — system as satellite to CW8 core; explicit
   acknowledgment of skill-alpha vs concentration-alpha distinction
2. **Investor profile** — risk tolerance, time horizon, capital base
3. **Strategic asset allocation** — 70% CW8 / 30% system hybrid, with
   rebalancing rule
4. **Strategy constraints** — what the system is and isn't allowed to do
   (universe, sleeve weights from `portfolio-2026-05.md`, no leverage,
   no shorts beyond defensive sleeve, etc.)
5. **Phased capital deployment** — gates between phases as objective
   metrics (Sharpe ≥ X over Y days live, max DD ≤ Z, divergence-monitor
   OK for N consecutive days)
6. **Risk limits** — pulling from `config/default.yaml`:
   - position entry 5%, sector concentration 20%
   - trailing stop 15%, drawdown pause 10%, circuit breaker 20%
   - soft/hard ceilings 7%/15%
7. **Halt & retirement triggers** — pre-committed numbers so future-self
   can't talk current-self out of pulling the plug
8. **Monitoring & review cadence** — daily divergence monitor (already
   built), plus the cadence answer from question 3
9. **Governance** — who can change what, when changes can be made
   (probably: weights only at quarterly reviews; risk limits never
   loosened during a drawdown; etc.)
10. **Tax & accounting notes** — depends on user's tax residency, may
    need a question
11. **Appendix: revival conditions** — link
    `docs/strategies/mean-reversion-failure-analysis.md` for the
    pre-committed conditions to re-enable dropped sleeves

---

## Other open items from the 2026-05-27 session note

These were on the next-session list before IPS was picked up. Roughly in
priority order:

1. **Root-cause the IB Gateway disconnect on port 7497.** Was observed
   timing out mid-investigation; `--bars-from-json` was added to
   `scripts/run_backtest.py` as a workaround, but the underlying cause
   is unknown. Check the IBC launchd job logs and gateway login behavior.

2. **Build the launchd plist for the daily divergence-monitor run.**
   Suggested wiring lives in `docs/operations/divergence-monitor.md`
   § *Recommended daily wiring* — 04:45 SGT, after the existing 04:15
   SGT `scripts/run_paper.py` job. Should emit Prometheus textfile and
   exit-code-alert on breach.

3. **Audit the daily 04:15 SGT paper-trading job.** Has it been running
   cleanly the past 2 weeks? Check `equity_snapshots` table for daily
   continuity, and IBC / paper-runner launchd logs for errors.

---

## Files that already exist and shouldn't be rewritten

- `docs/strategies/portfolio-2026-05.md` — canonical "what's running"
- `docs/strategies/mean-reversion-failure-analysis.md` — why MR sleeves
  were dropped + revival conditions
- `docs/operations/go-live-checklist.md` — 8 operational gates
- `docs/operations/rollback-playbook.md` — 4 rollback triggers + procedure
- `docs/operations/divergence-monitor.md` — daily monitor runbook
- `scripts/divergence_monitor.py` + `backtest/divergence.py` + 45 tests
- `scripts/run_paper.py` — aligned with 6-sleeve config

The IPS should **link** these, not duplicate them.

---

## Session log location (Obsidian, outside the repo)

`/Users/huiliang/Library/Mobile Documents/iCloud~md~obsidian/Documents/huiliang/Projects/algo-poc.md`

The 2026-05-27 entry captures the full backtest-rerun + divergence-monitor
session. The next account won't have direct iCloud access unless mounted
locally.
