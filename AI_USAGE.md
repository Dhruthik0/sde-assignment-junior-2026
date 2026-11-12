# AI Usage Disclosure

> DRAFT: review and rewrite in your own words before submitting. Everything below is factual
> about how this solution was produced; add your own review and changes.

## Tools used
Claude Code (Anthropic, Claude Opus model) in the terminal.

## What I used them for
- Reading the assignment and the mock provider source, and choosing Assignment B.
- Designing and writing the dispatcher (`solution/dispatcher/`), the tests, `chaos.sh`,
  `compare.py`, and the first draft of `SUBMISSION.md`.
- Running the end-to-end chaos runs against the mock and reading the results.

## What I wrote or decided myself
_Fill in honestly: e.g. picked Assignment B, reviewed the design, changes you made after reading
the code, anything you rewrote._

## Where the AI was wrong or unhelpful
- It edited the repo's original `.gitignore`, although the README says to keep original files
  unmodified. Caught while reviewing `git status`; reverted and moved the ignore rule to
  `solution/.gitignore`.
- `chaos.sh` first appended to the previous run's log, so counting "fresh start" lines showed 2
  fresh starts in a single run. Fixed by clearing logs at the start of each run.
- The first version of the lost-webhook polling throttle was a convoluted condition; it was
  rewritten into a simpler one.

## How I verified AI-generated code
- `pytest`: 26 unit tests on the scheduling rules (fake clock) and the state machine.
- `solution/chaos.sh` end-to-end runs against the real mock, including unseen seeds, 1.5x
  injected failures and repeated `kill -9`: checked `violation_count` in `/admin/report` and diffed
  `final_state.csv` against `/admin/leads` (results in `SUBMISSION.md`).
- _Add: reading the code line by line, any changes you made yourself._
