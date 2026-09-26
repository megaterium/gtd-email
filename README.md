# gtd-email

An agent skill for building an inbox-zero email triage system.

Every message gets exactly one Getting Things Done label and then leaves the
inbox, so the inbox is a landing zone that is always empty and the work lives in
the lists. [Jev](https://docs.typesafe.ai), TypeSafe's System One model, makes
the judgment; code owns the mail, the labels, and the record of what was done,
so every decision is reversible.

Costs about **$0.06 per 1,000 emails**. A 3,600-message backfill is roughly 20
cents and ten minutes.

## Install

Works in Claude Code, Codex, and the ~75 other agents the
[skills CLI](https://github.com/vercel-labs/skills) supports:

```bash
npx skills add megaterium/gtd-email -a claude-code -a codex
```

Drop the `-a` flags to choose agents interactively. Skills are plain
directories, so manual installation is a copy: put `skills/gtd-email/` into
`~/.agents/skills/` (read by Codex, and by Claude Code via a symlink) or into
`.claude/skills/` in a project.

Then just ask your agent to set up GTD email triage.

## What it covers

Eight phases, each verifiable on its own:

0. Getting a TypeSafe account and API key, and installing the official
   TypeSafe skill for the model itself
1. Reaching the mailbox over IMAP, and what an app password really grants
2. The GTD label scheme, and why exactly one bucket stays in the inbox
3. Designing the judgment: the questions, the state, and why not to add rules
4. Applying it — the four IMAP behaviours that quietly do nothing if you get
   them wrong
5. Back-processing an existing inbox safely
6. Staying at zero in real time over IMAP IDLE
7. Building the Gmail view you actually work from

## Reference implementation

[`skills/gtd-email/reference/gtd.py`](skills/gtd-email/reference/gtd.py) is a
complete working version in ~250 lines of Python standard library, no
dependencies:

```bash
export TYPESAFE_API_KEY='...' IMAP_USER='you@gmail.com' IMAP_PASSWORD='...'

python gtd.py labels                     # create the GTD labels
python gtd.py backfill --limit 25 --dry-run   # inspect before mutating
python gtd.py backfill                   # empty the inbox
python gtd.py watch                      # stay at zero over IDLE
```

It is a starting point, not a product: it has no persistence, so it cannot undo.
Adding a decision log is the first thing to do when adapting it.

## Scope

The skill is written for any IMAP mailbox. Three things in it are Gmail-specific
and need a local equivalent elsewhere: the `X-GM-RAW` search extension, the
`X-GM-LABELS` store extension, and archiving by moving to `[Gmail]/All Mail`. On
a plain IMAP server, labels are folders and archiving is a move.

The bucket names are a starting vocabulary, not doctrine. Keep the shape — one
parent label, one label per message, one bucket meaning "I don't know" — and
rename the rest to match how you actually think about your mail.

## License

MIT. See [LICENSE](LICENSE).
