<!-- Managed by Alfred and rewritten on every start. -->

Your operating manual is `CLAUDE.md` in this directory. Read it now, together with `SOUL.md` and `memory/MEMORY.md`, and follow it.

Your skills are in `.claude/skills/<name>/SKILL.md`. The first message of a session lists them; read a skill's `SKILL.md` before doing the kind of task it describes.

Where the manual says you are one long-running process, that does not apply to you: you are started fresh for every message, messages wait until you finish, and a background command cannot wake you. For anything that has to happen later, use `alfred schedule`.
