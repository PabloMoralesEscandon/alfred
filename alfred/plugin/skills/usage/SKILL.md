---
name: usage
description: Ask whether the Claude plan limits allow work right now, and obey the answer. Use it when you schedule non-urgent work (put --skip-if-low on the item) or wake up for a scheduled or non-urgent task (check running_low first), whenever your memory, SOUL or the user's instructions contain a rule about usage ("don't do this if the weekly limit is over 90%", "skip it when the 5-hour window is nearly used up"), and to get woken when weekly quota would otherwise go unused. Needs the usage permission.
---

# Plan usage: ask, get a verdict, obey it

The monitor answers questions about the Claude plan limits with a **verdict**: `true` or `false` and a
short reason on one line. You act on the verdict. You do not read percentages and decide for yourself,
and you do not recompute anything: the rules and thresholds are evaluated by the monitor.

```
$ alfred usage running_low
true: week 90% used (resets in 3d 0h)
```

Asking is free: it reads `/usage` without calling the model, and answers from a reading a few minutes
old when there is one. Ask as often as you need. It needs the usage permission; if you don't have it,
the command says so.

| Exit code | Meaning |
| --- | --- |
| 0 | Answered: the line starts with `true:` or `false:` |
| 1 | Refused: no usage permission |
| 2 | The condition can't be read: fix it (the error says why) |
| exit code 3 | The usage could not be read (see "When the check fails") |

`--json` gives the full breakdown (each limit's percent used and left, when it resets, the forecast).
Use it only to write a log or to tell the user the numbers; never to make the decision yourself.

## The questions

```bash
alfred usage running_low                     # is any limit (5-hour window or week) at 90% or more?
alfred usage running_low --limit week        # only the weekly limit; --limit session for the 5-hour window
alfred usage running_low --threshold 80      # another threshold (the default is set by the user, 90%)
alfred usage running_low --projected         # also true if the recent pace would use a limit up before it resets
alfred usage check 'week.used >= 90'         # any condition, in the usage-alert language
alfred usage check 'session.left < 1h and week.surplus > 0' --hours 9-20
alfred usage forecast --hours 9-20           # true: weekly quota will go unused even using every window fully
```

Values for `check` (and `usage-alert add`):

| Value | What it is |
| --- | --- |
| `session.used`, `week.used` | percent used of the 5-hour window and of the week (all models) |
| `session.left`, `week.left` | time until it resets (`45m`, `2h`, `3d`) |
| `week.windows_left` | useful 5-hour windows before the weekly reset, inside the active hours |
| `week.max_spend` | the most of the week those windows can still spend |
| `week.surplus` | weekly quota that will be lost even using every window fully (positive = lost) |
| `session.projected`, `week.projected` | the percent it reaches at its reset at the recent pace |

Operators `< <= > >= = !=`, joined with `and`, `or`, `not` and parentheses. `--hours 9-20`,
`--days mon-fri` and `--tz Europe/Madrid` set the hours the user works, for the forecast values; without
them the user's default applies (every hour, unless they set one).

## Scheduling work that should wait for the limits

When you schedule a non-urgent reminder or task, put the condition on the item itself. Just before each
delivery the service asks the same question; if the item is held back you are not woken at all, so it
costs no usage. A recurring item moves on to its next time; a one-off ends as `skipped`.

```bash
alfred schedule add --at 2026-06-08T17:00 --every 1d --task "Daily report" --skip-if-low             # 5-hour window or week at 90%
alfred schedule add --in 2h --task "Re-index the notes" --skip-if-low week --threshold 80
alfred schedule add --in 2h --task "..." --skip-if-low --projected                         # also at a pace that runs out
alfred schedule add --in 1d --task "..." --skip-if 'week.left < 1d and week.used > 80'
alfred schedule add --at 2026-06-08T18:00 --every 1d --task "Work on the backlog" --only-if-waste --hours 9-20 [--margin 5]
alfred schedule add --in 1h --task "..." --only-if 'session.left < 1h and session.used < 30'
```

- `--skip-if` / `--skip-if-low`: not delivered when it holds. `--only-if` / `--only-if-waste`: delivered
  only when it holds. You can give one of each; `--hours`, `--days`, `--tz` set the hours for the forecast.
- If the usage can't be read at that moment, a `--skip-if` item is delivered and an `--only-if` item is
  skipped; `--if-unread run|skip` chooses otherwise. Follow the user's rule ("if you can't check, don't"
  is `--if-unread skip`).
- `alfred schedule list` shows each item's condition, how many deliveries it held back and the last
  reason. Tell the user about skips when they ask, or when a skipped task was one they were waiting for.
- Turn a user rule into the item's condition the same way as below (the table), when you schedule it.

## When you wake up for a non-urgent task

A scheduled task without a condition, a note to yourself, an event: before doing the work, ask. (A task
with `--skip-if-low` was already checked just now: do it.)

1. Run `alfred usage running_low`.
2. `true`: do not do the task. End with one line that says why, using the reason, for example:
   `Skipped the 17:00 report: the weekly limit is at 90% (resets in 3 days).`
   If the task was a quiet background job nobody waits for, end with `NO_REPLY` instead.
3. `false`: do the task as usual. Say nothing about usage.

Urgent tasks (the user or the task itself marks them urgent) skip this check, unless a user rule
says otherwise (next section).

## Rules from the user about usage

If your memory, your SOUL, or the user's instructions say something like "don't run when usage is
high", do not judge it by reading the usage yourself. Translate the rule into a condition, ask, and obey
the verdict.

| The user's rule | The question | Then |
| --- | --- | --- |
| "Don't do this task if the weekly limit is over 90%" | `alfred usage check 'week.used > 90'` | `true`: skip it |
| "Don't start anything once 90% of anything is used" | `alfred usage running_low` | `true`: skip it |
| "Don't run it when the 5-hour window is almost used up" | `alfred usage running_low --limit session` | `true`: skip it |
| "Pause the heavy jobs in the last day if the week is over 80%" | `alfred usage check 'week.left < 1d and week.used > 80'` | `true`: skip them |
| "Stop if we'd run out before the reset at this pace" | `alfred usage running_low --projected` | `true`: skip it |
| "Only run the backlog when weekly quota would be wasted anyway" (I work 9 to 20) | `alfred usage forecast --hours 9-20` | `true`: run it |
| "Use the rest of a 5-hour window that is about to reset unused" | `alfred usage check 'session.left < 1h and session.used < 30'` | `true`: run it |

- Write the condition from the user's words; `week.used >= 90` and `week.used > 90` differ at exactly 90.
  Use the one that matches what they said, and the threshold they said.
- Some rules can't be expressed: another model's own limit ("Fable's weekly limit"), money ("over $5"),
  tokens, or a limit the questions don't know. Then say so to the user in one line and ask how to handle
  it. Do not act blindly, and do not estimate it yourself.
- Urgent tasks ignore a user rule only if the rule allows it ("unless it's urgent"). If the rule says
  nothing about urgency, it applies to urgent tasks too; if in doubt, ask.

## When the check fails

- Exit code 2: your condition is wrong. Fix it from the error. If the rule can't be written in the
  language, treat it as one that can't be expressed (above).
- Exit code 1: you lack the usage permission. Tell the user the rule can't be checked until they grant it
  (`/bot perm <bot> usage on` in Alfred's panel), and ask whether to go ahead.
- Exit code 3 (the usage could not be read; it waits a couple of minutes before trying again):
  - If a user rule says to hold off when unsure ("if you can't check, don't"), don't do the task, and say so.
  - Otherwise, if the task is one the user asked for or is important, do it and add one line saying the
    usage could not be checked.
  - If it is optional work you would do on your own initiative, skip it this time.

Always say in your reply when you skipped something because of a check, with the reason it gave.

## Be woken when weekly quota would be lost

```bash
alfred usage-alert waste --hours 9-20 [--days mon-fri] [--margin 5] [--repeat] [note for yourself]
alfred usage-alert add 'week.surplus > 0' --hours 9-20
```

`waste` wakes you when the useful 5-hour windows left before the weekly reset (inside those hours) can no
longer spend what is left of the week. It fires once per quota week, again that week only if the loss
grows by about one more window, and with `--repeat` every week. `--margin 5` ignores losses under 5
points. The event you get gives the verdict and its numbers: act on it (for example, offer the user tasks
from their backlog). Every wake-up costs some plan usage; questions do not. `alfred usage-alert list` and
`alfred usage-alert remove <id>` manage your alerts.
