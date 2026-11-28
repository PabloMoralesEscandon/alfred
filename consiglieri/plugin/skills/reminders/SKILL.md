---
name: reminders
description: Schedule something for later in this Telegram chat. Covers reminders and timed messages ("remind me at 6", "in 20 minutes tell me to..."), recurring ones ("every morning"), and tasks you should carry out yourself at a set time ("check the price tomorrow at 9 and tell me"). Also how you wake yourself up later: a note to yourself for a set day and time, to check on something or pick a job back up. Also lists and cancels them.
---

# Reminders and scheduled tasks

Everything goes through `consiglieri schedule`, which prints JSON. The chat is taken from your environment, so you never pass a chat id.

## Add

```bash
# A fixed message, delivered as-is
consiglieri schedule add --at 2026-06-08T09:00:00+02:00 --message "Call Sam"
consiglieri schedule add --in 20m --message "Take the pizza out"

# A task: at that time you are woken up with this prompt and your reply is sent to the chat
consiglieri schedule add --at 2026-06-08T08:00 --task "Check today's weather in Madrid and tell me if I need an umbrella"

# Recurring: add --every to either kind
consiglieri schedule add --at 2026-06-08T08:00 --every 1d --task "Morning briefing: calendar and unread mail"
```

- `--at` takes an ISO-8601 time. Without an offset it is read as the machine's local time, which is the time shown in the `<context>` line.
- `--in` and `--every` take a number and a unit: `s`, `m`, `h`, `d`, `w` (for example `90s`, `45m`, `2h`, `1d`, `1w`).
- Use `--message` when the text is already known. Use `--task` when something has to be looked up or done at that moment. Write the task prompt so it makes sense with no memory of this conversation.

## Wake yourself up

A task is also how you leave a note for your future self. At that time you receive `[Scheduled task] <your note>` in this conversation, exactly as if a message had arrived, and you act on it. Only you see the note.

```bash
consiglieri schedule add --in 30m --task "Note to self: check whether the export to ~/exports finished. Tell the user only if it failed."
consiglieri schedule add --at 2026-06-12T07:30 --task "Note to self: the user flies at 11:40. Check the flight status and tell them the gate."
```

- Use it on your own initiative whenever something needs a later look: a job that will take hours, an answer you are waiting for, a follow-up the user will expect.
- Write the note for someone who has forgotten the details: what to check, what to do about each outcome, and when the user should hear about it.
- When you wake up and there is nothing worth telling the user, end with `NO_REPLY`.
- A wake-up that repeats (`--every`) keeps using the user's plan limits. Set one up when they asked for it; ask first when it was your idea.

## Only when the plan limits allow

Only with the user's express permission (they asked for it, gave you a usage rule, or said yes when you
asked; see the `consiglieri:usage` skill), a reminder or task can carry a usage condition, checked just before each
delivery. When it holds the item back, nothing is sent and you are not woken (a recurring item moves on to
its next time):

```bash
consiglieri schedule add --at 2026-06-08T17:00 --every 1d --task "Daily report" --skip-if-low        # not when the 5-hour window or the week is at 90%
consiglieri schedule add --at 2026-06-08T18:00 --every 1d --task "Backlog" --only-if-waste --hours 9-20  # only when weekly quota would go unused
```

Use it for work that can wait; never for something the user needs at that time. The options are in the
`consiglieri:usage` skill.

## List and cancel

```bash
consiglieri schedule list           # pending items for this chat
consiglieri schedule list --all     # include sent, cancelled, failed, and skipped
consiglieri schedule cancel <id>    # the id, or its first characters
```

## Behaviour

- If the time is missing or vague ("later", "soon"), ask instead of guessing.
- `"ok": false` means nothing was scheduled. Read `error`, fix the command, or tell the user what is wrong.
- After adding, confirm in one line with the local time it will fire.
