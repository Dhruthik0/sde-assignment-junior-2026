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
- A second, independent AI review of the finished code (reading the spec and the mock, plus runs at
  higher `--speed`) found issues the first pass missed:
  - At 12000x speed, 2 calls reached the provider after the window had closed. Fixed by
    re-checking the window right before sending.
  - A provider rejection made the lead get re-sent every tick. Fixed with a 5-minute hold.
  - A crash in the scheduling loop would have left the process up but idle. It now exits with
    status 1.
  - Several claims in `SUBMISSION.md` didn't match the code. These were corrected.
- Randomised property tests found that `next_open` treated a campaign window lying entirely outside
  09:00–21:00 as open at 09:00. Fixed, and covered by a test.
- At 12000x speed, the fixed end-of-window margin still let 3 calls arrive after closing, due to
  cold-start HTTP latency. The margin now scales with the clock speed.

## How I verified AI-generated code
- `pytest`: 696 test cases on the scheduling rules (fake clock), the state machine, and
  provider/webhook handling against a scripted fake provider, plus randomised property tests
  (brute-force window check, `pick` invariants, event-order and duplicate independence). They also
  pass on Python 3.11.
- `solution/chaos.sh` end-to-end runs against the real mock, including unseen seeds, 1.5x
  injected failures, 300x–12000x clock speeds, up to 15 `kill -9`s, 12 s outages, and 260 crashes
  injected at exact points between DB commits and HTTP calls: checked `violation_count` in `/admin/report` and diffed
  `final_state.csv` against `/admin/leads` (results in `SUBMISSION.md`).
- _Add: reading the code line by line, any changes you made yourself._
