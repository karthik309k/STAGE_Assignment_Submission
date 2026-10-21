# AI collaboration log

## How AI was used
* **Tool:** Claude (Anthropic), in a single long working session that I directed. I supplied the brief, the study guide and the data pack, and I asked for the whole system to be built.
* Claude read the brief and the data pack, explored the data with scripts, proposed the architecture, and wrote the code, tests and documents.
* **Nothing was accepted on trust.** Claude ran the loader against the real pack, a full replay, the provided simulator, and the test suite, and the output of those runs drove most of the corrections below. I then ran everything myself on my own Windows machine (see "What I ran myself").
* Design decisions I am responsible for and can defend: a deterministic rule-based core with an advisory-only model, the goal priority order, my interpretations of the vague goal rules, which goals may send a message, and intention-to-treat with shadow goals for the holdout.
* Privacy: the data pack was only read by scripts inside the build session and by my local commands. It was not pasted into any chat or uploaded to a third-party service.

## Things the AI got wrong, and how each was caught
1. **Shell brace expansion created literal directories** (`{src`, `{src/oua,tests,sample_run}`) and wrote files to the wrong place. Caught by listing the tree; cleaned up and re-created.
2. **Persisted state turned a re-run into 1,131 "duplicates".** A crashed first replay had saved viewer state, and the next run correctly treated every trigger as already processed. Idempotency worked, but a fresh run must not inherit state, so the replay now wipes `viewer_state/` unless `--resume` is passed.
3. **Flagged (prompt-injection) titles leaked into decisions.** 83 proposals were rejected by the verifier for `injected_content`. Root cause: the G07 goal context pointed at the viewer's own series title, which can be a flagged record, and the repair step cleaned the page but not the message. Fixed at the source (flagged records are never a goal target) and in repair (messages with bad titles are dropped).
4. **Over-messaging.** The first version messaged viewers on G01/G07 goals: 309 pushes for about 870 agent viewers. The brief warns against messaging everyone just because you can, and those viewers were in the app at that moment with the same title already in the hero slot. Messaging is now limited to goals that need reach outside the session (G04, G03, G06): 35 pushes.
5. **A model answer of "no goal" was logged as an unsupported claim.** Abstaining is legitimate; only a claim of eligibility that the rules contradict is unsupported. Fixed in the validator.
6. **Model calls were double-counted** on malformed output. Found by `test_malformed_model_json_is_retried_once_then_accepted`.
7. **The first "served vs everyone else" demonstration was degenerate.** The planner only assigns goals to viewers it acts on, so on the goal metric every goal-bearing agent viewer counts as "served". It was recomputed on all triggered viewers with a goal-free metric, where the selection bias shows (+15.5 pts "served vs rest" against +1.5 pts randomised).
8. **Wrong test expectations, not wrong code.** A test expected G08 after a purchase, but G07 correctly outranks it; a "lapsed" fixture viewer was not actually lapsed. The tests were fixed and the code left alone.
9. **A design weakness the tests exposed.** Measurability inferred from the event stream would disable goals in a pack that happens to contain no purchases. Kept (it is what finds the missing `tv_app_open` for G11), documented in KNOWN_LIMITATIONS.md, and the test fixture made realistic.
10. **A single lucky seed looked like a result.** The first run showed +1.2 pts; a fresh run with another seed showed -1.9 pts. Instead of keeping the better-looking number, a multi-seed robustness table was added and the headline now says "no detectable uplift".
11. **The code was written and tested on Linux and failed on my Windows machine** (found when I ran it, not by the AI's own runs):
    * `PermissionError: [WinError 5] Access is denied` when saving viewer state. The atomic save (write a temp file, then rename it) can fail on Windows when an editor file-watcher or antivirus briefly locks the new file. Fixed by retrying the rename with short waits and falling back to a direct write. The fix was tested by simulating the failure.
    * `'gzip' is not recognized`: the final step called the Linux `gzip` command to compress the simulator output. Replaced with Python's built-in `gzip` module so it is portable.
    Lesson recorded: AI-generated code that has only been run on one operating system has not been tested on the other.

## Suggestions deliberately NOT accepted
* **Let the model choose goals and write push copy.** Rejected: unverifiable, and a prompt-injection path. The model only ranks goals the rules already found eligible; message copy comes from templates and catalogue names.
* **Use `wa_new_episode_v2`** ("a new episode is out"). The pack has no episode release times, so the claim cannot be evidenced.
* **Fall back to the profile dialect when the app dialect is invalid** (d7 / NA / null). That is a guess about what the app will request; no page is safer.
* **Tune pages to the simulator's formula** (always five rows, padding `next_episode` with unrelated titles). That would inflate the number without meaning. Rows below three valid titles are dropped, not padded.
* **Report the uplift against "everyone else", or round the ITT result up.** We report +1.2 pts with a confidence interval that includes zero.
* **Treat lapsed profiles without a lapse date as "recently lapsed".** They are ineligible instead of guessed.

## What I ran myself
* Unzipped the project on Windows and ran `python -m pytest tests -q`: **44 passed**.
* Ran the full replay on my own copy of the data pack (`python -m oua replay ... --seed oua-practice`). It reproduced the AI session's figures exactly: 2,769 decisions, 895 published, 0 actions to holdout viewers, 0 simulator policy problems, ITT goal success 11.7% (agent) vs 10.5% (holdout), +1.2 pts with a 95% CI of -2.1 to +4.5.
* Hit and fixed the two Windows problems in item 11, then re-ran the replay cleanly.


## Checks I did myself

* **Decision verified by hand: viewer U-04621, goal G06 (win back a recently lapsed viewer).**
  I printed the full decision with `python -m oua audit U-04621 --run sample_run`, then checked its evidence against the original data pack:
  * the trigger event `df918504-087e-4344-a122-3a8387e6ce93` exists in `replay/triggers.jsonl` (an `app_open` for U-04621 on dialect d2);
  * a `subscription_lapsed` event for U-04621 exists in `data/events.jsonl` (event id: `367e3d64-a396-493d-a54a-26419d0daefe`), received in September 2026, which supports "lapsed within 30 days";
  * the profile row cited as evidence (`updated_at` 2026-09-29T16:35:57.406Z, status `lapsed`) exists in `data/profiles_snapshot.csv`.
  The decision's evidence was real. The goal was later judged **failed** (no purchase within the 14-day window), and the agent recorded that in the viewer's `learnings`. One failed goal says nothing about whether the agent works; only the agent-versus-holdout comparison does.
* ran `tools/check_run.py`, which re-checks actions against the data pack without using the agent code: 18 of 18 checks passed.
* deliberately disabled the quiet-hours check in `verifier.py`. `test_verifier_rejects_message_inside_quiet_hours` failed as expected (1 failed, 43 passed); I restored the code and all 44 tests passed again.

## Changes I made myself
* **Fixed the two Windows failures by hand-editing the code.** I added the retry-and-fallback `save` method (and the missing `import time`) in `src/oua/state.py`, and replaced the Linux-only `gzip` call with Python's built-in `gzip` module in `src/oua/cli.py`. I then re-ran the full replay on Windows and it completed cleanly.
* **Kept the data pack out of the project and out of OneDrive.** I moved it to `C:\work\pack` and pass its location with `--pack`, so it is neither in the submitted zip nor synced to a cloud service, as the brief's privacy rule requires.
* **Re-ran the replay from my own copy of the code** and got the same results as the AI session (2,769 decisions, 895 published, 0 holdout actions, 0 policy problems, +1.2 pts with a 95% CI of -2.1 to +4.5).