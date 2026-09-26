---
name: gtd-email
license: MIT
description: >
  Build an inbox-zero email triage system that files every message into Getting
  Things Done buckets using Jev, TypeSafe's System One model, for the judgment
  and IMAP for the mail. Covers getting a TypeSafe account and API key,
  installing the TypeSafe agent skill, designing the GTD label scheme and the
  questions that drive it, back-processing an existing inbox, and keeping it at
  zero in real time over IMAP IDLE. Use when someone wants automatic email
  triage, inbox zero, GTD labelling, or a worked example of classifying a
  high-volume stream with a System One model.
---

# GTD email triage with Jev

The goal is a mailbox where the inbox is a landing zone that is always empty and
the work lives in labels. Every message gets exactly one GTD label and then
leaves the inbox. One label is the exception and stays, so the human still has
somewhere to look.

Jev supplies the judgment. Code owns the mail, the labels, and the record of what
was done, so every decision is reversible.

Work through the phases in order. Each one is verifiable on its own; do not move
on until the current one is proven against the real mailbox.

## Phase 0 — Set up TypeSafe and Jev

Jev is a System One model: it returns typed judgments and probabilities rather
than text. Read [the docs](https://docs.typesafe.ai/llms.txt) — they are the
source of truth for the API and the primitives.

**Install the official TypeSafe skill** and follow it for anything about
question design, primitives, or the API. This skill covers only the GTD
application; that one covers the model.

```bash
# Claude Code
claude plugin marketplace add typesafe-ai/skills
claude plugin install typesafe@typesafe-ai

# Any other agent (project-local; add -g for global)
npx skills add typesafe-ai/skills --skill typesafe-ai
```

**Get an account and a key.** Log in at
[console.typesafe.ai/playground](https://console.typesafe.ai/playground), which
is also where a new account is created, then create a key at
[console.typesafe.ai/keys](https://console.typesafe.ai/keys).

```bash
export TYPESAFE_API_KEY='...'
curl -s https://api.typesafe.ai/v1/models \
  -H "Authorization: Bearer $TYPESAFE_API_KEY"
```

A model list back means Phase 0 is done. `jev-latest` is the alias to use;
pin the version (for example `jev-1.13.0`) once calibrated, so a model update
cannot silently change how mail is filed.

**Cost.** Jev bills input tokens only, at $0.042 per million. A triage prompt
with a condensed body runs about 1,400 input tokens, so roughly **$0.06 per
1,000 emails** — a 3,600-message backfill costs about 20 cents and a normal
mail volume costs cents per year. Cost is not a constraint here; send Jev
generous context.

## Phase 1 — Get at the mail

Plain IMAP over TLS. For Gmail the endpoint is `imap.gmail.com:993`.

**Gmail app password.** Requires 2-Step Verification on the account, then
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
The result is 16 characters shown in four groups; **strip the spaces** before
use. Two things to tell the user plainly:

- An app password has **no scopes**. It grants full read, write, and delete over
  the whole mailbox and it bypasses the 2SV prompt. Treat it as a root
  credential: environment variable or a real secret store, never a repo, never a
  log line. Redact it in any `Debug`/`repr` implementation.
- It is revocable from the same page, which is the kill switch if it leaks.

**Other providers** work the same way over IMAP. Three things in this skill are
Gmail-specific and need a local equivalent elsewhere: the `X-GM-RAW` search
extension, the `X-GM-LABELS` store extension, and archiving by moving to
`[Gmail]/All Mail`. On a plain IMAP server, labels are folders and "archive" is
a `UID MOVE` into one.

Verify by selecting `INBOX` and counting it. Do not proceed on a credential you
have not used.

## Phase 2 — Design the label scheme

Nested labels under one parent, so Gmail shows a single collapsible `GTD` group
in the sidebar instead of eight loose labels:

| Label | Means |
| --- | --- |
| `GTD/Action` | A next action the owner has to take |
| `GTD/Waiting` | Delegated or blocked on someone else — the GTD "waiting for" list |
| `GTD/Scheduled` | Tied to a date: invite, booking, reminder |
| `GTD/Read` | Worth reading, nothing to do |
| `GTD/Reference` | Keep for lookup: receipt, statement, confirmation |
| `GTD/Someday` | A maybe, no commitment |
| `GTD/Noise` | Marketing, notifications, automated bulk |
| `GTD/Unsure` | Jev could not decide |

Exactly one label per message. Overlapping labels defeat the purpose: the point
is that each list can be worked top to bottom.

**Everything leaves the inbox except `GTD/Unsure`.** That is what inbox zero
means here — a labelled message that stays in the inbox has been filed twice and
still demands attention. `GTD/Unsure` stays precisely because it is the queue of
decisions the human still owes, and it should be small enough to clear by hand.

Adapt the names to the person's vocabulary, but keep the shape: one parent, one
label per message, one bucket that means "I don't know".

## Phase 3 — Write the judgment

**One Jev request per email, several questions in it.** Independent questions
over the same state run in parallel in a single request, so extra questions cost
input tokens but not a round trip.

Four questions carry this well:

- **`bucket`** — a `choice` over the seven real buckets (not `Unsure`; that is a
  code outcome, not an option). Each `criteria` entry describes a concrete
  situation, not a synonym of the label name.
- **`needs_me`** — a `noul`: does this require the owner personally?
- **`has_deadline`** — a `noul`: is there a date or deadline attached?
- **`still_open`** — a `noul`: is there a live open loop here today, or is
  nothing left to do with it?

`bucket` decides the label. The three `noul`s are cheap, orthogonal signals worth
recording for later calibration and for surfacing a shortlist.

**State is where accuracy comes from.** Send named JSON fields, not a blob:

```json
{
  "owner_address": "person@example.com",
  "from": "Stripe <receipts@stripe.com>",
  "to": ["person@example.com"],
  "subject": "Your receipt",
  "date": "2026-09-26T12:19:13-03:00",
  "known_correspondent": false,
  "messages_from_sender_in_inbox": 47,
  "body": "<condensed>"
}
```

Two fields do most of the work. `known_correspondent` — has the owner ever
*sent* mail to this address — is the single strongest separator between a real
person and a feed. `messages_from_sender_in_inbox` catches bulk senders. Compute
both once per run from the Sent folder and the inbox, not per message.

**Send the body, condensed.** Subject-and-sender alone misclassifies badly. Fetch
a bounded prefix (`BODY.PEEK[TEXT]<0.16384>`, note `PEEK` so nothing is marked
read), then strip quoted reply chains, signature blocks, and long URLs, and cap
the result at roughly 2,400 characters. That lands near 1,400 input tokens with
the full meaning intact.

**Resist adding rules.** The temptation is to pre-filter with heuristics — a
sender denylist, an age cutoff, a threshold that overrides a bucket. Every such
rule is a second classifier that disagrees with the first one and is much harder
to inspect. Ask Jev, take the answer, and record the probabilities so
disagreements are diagnosable. If a bucket is wrong, fix the `criteria` or add
state; do not add a rule on top.

Use `GTD/Unsure` when the answer genuinely is not usable — the probability mass
across the plausible options is too diffuse to act on. Evaluate that threshold
on real data. Gating on Choice `confidence` alone tends to hold back far too
much; a meaningful signal is how much mass sits on the buckets that imply work.

## Phase 4 — Apply the decision

Where the sharp edges are. Four rules, each learned the hard way:

**1. Adding a label does not archive. Removing `\Inbox` does not either.**
`-X-GM-LABELS (\Inbox)` returns `OK` and changes nothing. Archiving on Gmail
means `UID MOVE` into `[Gmail]/All Mail`.

**2. Remove the other GTD labels before adding one.** Re-running triage
otherwise accumulates labels and breaks the one-label invariant.

**3. Batch by bucket, not by message.** Group the decisions, build one UID set
per bucket, and issue three commands per set — remove stale labels, add the new
one, move out of the inbox. Per-message sessions turn minutes into half an hour.

**4. Use `.SILENT` on stores.** `UID STORE` without it makes the server emit
untagged `FETCH` lines; an IMAP client that does not drain them desynchronizes
its tag counter and the next command fails in a way that looks unrelated.

**Record every decision before applying it** — message id, UID, bucket, all
probabilities, token counts, and a run id. Two reasons: undo, and calibration.
Key the undo on `Message-ID`, because a `UID MOVE` changes the UID, so a UID
recorded before the move no longer resolves after it.

## Phase 5 — Back-process the existing inbox

Run on 25 messages first. Read all 25 decisions yourself, with the owner, before
touching the rest — this is the only cheap moment to catch a systematically
wrong `criteria`.

Then the full inbox:

- Page through by UID and checkpoint, so an IMAP failure costs one page.
- 4–6 concurrent requests is plenty; Jev's limits are far above this and the
  IMAP side is the bottleneck.
- Classify everything first, apply second. A single batched apply pass at the
  end is seconds, and a clean split means a bad run can be reviewed before it
  mutates the mailbox.
- Record a distinct run id so the whole backfill can be undone as one unit.

Expect a few thousand messages in about ten minutes for well under a dollar.
Verify by counting the inbox (should be the `Unsure` set, or zero if the
backfill archives those too) and confirming every message carries exactly one
GTD label.

## Phase 6 — Keep it at zero in real time

Polling is the wrong default. IMAP IDLE (RFC 2177) holds one connection open and
the server pushes as soon as mail lands.

```
SELECT INBOX
IDLE                        -> "+ idling"
  ... wait for a push or the re-arm deadline ...
DONE
UID SEARCH UID <high_water+1>:*
```

- **Re-arm well inside 29 minutes.** The RFC lets a server drop a longer IDLE,
  and Gmail drops sooner in practice. Nine minutes is comfortable.
- **Track a high-water UID, and sweep on every wake** — both on a push and on
  the re-arm timeout. Then a missed push costs latency, not a lost message.
- **`UID n:*` always matches the highest UID even when it is below `n`.** Filter
  the result to `> high_water` or the newest message is reprocessed forever.
- **On reconnect, sweep before arming.** Anything that arrived while the
  connection was down was never pushed.
- **Reconnect with exponential backoff**, capped at a few minutes.

Latency end to end is under a minute; most of it is the provider's own push
batching, not the loop.

Verify with a real send, and check that `GTD/Unsure` still stays in the inbox on
the live path — that is the one behaviour a backfill-focused implementation
tends to get wrong.

## Phase 7 — Build the view the owner actually works from

Labels in the sidebar are storage, not a workflow. The payoff of triage is a
single screen that shows the lists side by side, so the empty inbox sits next to
the work it was emptied into. In Gmail that is **Multiple Inboxes**.

**Settings** (gear) → **See all settings** → **Inbox** → **Inbox type:
Multiple inboxes**. Then define one section per list:

| Search query | Section name |
| --- | --- |
| `label:GTD/Action` | `⚡ Action` |
| `label:GTD/Waiting` | `⏳ Waiting` |
| `label:GTD/Scheduled` | `📅 Scheduled` |
| `label:GTD/Read` | `📖 Read / Review` |
| `label:GTD/Unsure` | `❓ Unsure` |

Then set **Multiple inbox position** to **Right of the inbox**, set the maximum
page size to about 10 per section, and **Save Changes** at the bottom — Gmail
discards the settings otherwise.

Four things worth knowing:

- **Gmail allows five sections.** That is why the scheme has eight labels but
  the view has five. `Reference`, `Noise`, and `Someday` are archives — reached
  by search when needed, never worked through — so they stay in the sidebar.
  Put `Unsure` on screen: it is the queue the owner owes decisions to, and out
  of sight it grows.
- **Order the sections by urgency**, not alphabetically. The first section is
  the one read every time.
- **Multiple Inboxes replaces the Categories tabs** (Primary/Social/Promotions).
  That is usually a gain once triage is running, since the buckets supersede
  them, but say so before flipping it.
- The section titles are arbitrary text. Emoji make them scannable at a glance
  and are worth the two seconds.

The result is the point of the whole exercise: `Inbox — No new mail!` on the
left, and the live lists on the right.

## Reference implementation

`reference/gtd.py` is a complete working version in about 250 lines of Python
standard library — no dependencies. It implements every rule above:

```bash
export TYPESAFE_API_KEY='...' IMAP_USER='person@example.com' IMAP_PASSWORD='...'

python reference/gtd.py labels            # create the GTD labels
python reference/gtd.py backfill --limit 25 --dry-run   # inspect before mutating
python reference/gtd.py backfill          # empty the inbox
python reference/gtd.py watch             # stay at zero over IDLE
```

Read it as a starting point, not a product: it has no persistence, so it cannot
undo. Adding a decision log is the first thing to do when adapting it.

## Calibrating afterwards

Keep the recorded probabilities and revisit them. The useful checks are the
bucket distribution against expectation, the `Unsure` rate, and any bucket the
owner keeps correcting by hand. Fix those by sharpening `criteria` or adding
state. A single-message live batch gives Jev thinner sender history than a
backfill did, so watch the live path's quality separately from the backfill's.
