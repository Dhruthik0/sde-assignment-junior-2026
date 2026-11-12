# AI Usage Disclosure

## Tools used
Claude Code (Anthropic's Claude Opus model), in the terminal.

## What I used them for
Most of the code was written by Claude Code. Specifically:
- Reading the assignment and the mock provider's source, and recommending which assignment to take.
- Designing and writing the dispatcher (`solution/dispatcher/`), the tests, the test scripts
  (`chaos.sh`, `crash_inject.*`, `mutants.py`, `compare.py`) and the first draft of
  `SUBMISSION.md`.
- Running the end-to-end runs against the mock and summarising the results.

## What I wrote or decided myself
- I chose Assignment B, and chose to keep the repo private.
- I didn't accept the first finished version. I asked for it to be rechecked, then tested much
  harder, then checked again against every line of the spec. Each round found real problems
  (listed below) that the first version shipped with.
- I went through the code and the test results, and edited the write-ups.

## Where the AI was wrong or unhelpful
- It edited the repo's original `.gitignore`, although the README says to leave original files
  alone. This showed up in `git status`; the change was reverted and the rule moved to
  `solution/.gitignore`.
- `chaos.sh` appended to the previous run's log at first, so one run seemed to have 2 fresh starts.
  It now clears the logs before each run.
- Its first lost-webhook polling throttle was a convoluted condition and had to be simplified.
- When I asked for a recheck, a second, independent AI review (spec, mock source, runs at higher
  `--speed`) found things the first pass missed:
  - At 12000x, 2 calls reached the provider after the window had closed. The window is now
    re-checked right before sending.
  - A rejected dial was re-sent every tick. There is now a 5-minute hold.
  - If the scheduling loop crashed, the process stayed up doing nothing. It now exits with status 1.
  - Several claims in `SUBMISSION.md` didn't match the code.
- Randomised property tests caught `next_open` treating a window entirely outside 09:00–21:00 as
  open at 09:00.
- At 12000x the fixed end-of-window margin still let 3 calls arrive late (cold-start HTTP latency).
  The margin now scales with clock speed.
- Nothing stopped two dispatchers running on the same DB. A test run that did this got 1
  concurrency violation; a file lock now prevents it.
- A signed webhook whose JSON wasn't an object got a 500 instead of a 400.
- Mutation testing showed the tests never checked the CSV sort order or which call `last_status`
  comes from. There's a test for both now.

## How I verified AI-generated code
- `pytest`: 697 test cases covering the scheduling rules (fake clock), the state machine, and
  provider/webhook handling against a scripted fake provider, plus randomised property tests.
  They also pass on Python 3.11.
- End-to-end runs against the real mock: unseen seeds, up to 3x injected failures, clock speeds
  from 300x to 12000x, up to 15 `kill -9`s in one run, SIGTERM, Ctrl-C, a frozen process,
  12 s outages, a second copy started mid-run, and 260 crashes injected at exact points between DB
  commits and HTTP calls. Each run checked `violation_count` in `/admin/report` and diffed
  `final_state.csv` against `/admin/leads`. All results are in `SUBMISSION.md`.
- `solution/mutants.py` breaks one rule at a time and checks the tests fail (31 of 32 caught; the
  one left can't change behaviour).
- A separate AI review checked each clause of the spec against the code and the mock.
