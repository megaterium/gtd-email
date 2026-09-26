#!/usr/bin/env python3
"""Minimal GTD email triage: Jev decides the bucket, IMAP applies it.

Standard library only. Read SKILL.md for the reasoning behind each rule; this
file is the smallest thing that implements all of them correctly.

    export TYPESAFE_API_KEY=... IMAP_USER=you@gmail.com IMAP_PASSWORD=...
    python gtd.py labels
    python gtd.py backfill --limit 25 --dry-run
    python gtd.py backfill
    python gtd.py watch

No persistence, so no undo: add a decision log before trusting it with a real
mailbox.
"""

import argparse
import email
import email.policy
import imaplib
import json
import os
import re
import select
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
HOST = "imap.gmail.com"
ALL_MAIL = "[Gmail]/All Mail"

# `Unsure` is deliberately absent: it is what code decides when Jev's answer is
# not usable, never an option Jev can pick.
BUCKETS = {
    "action": "A next action is required from the owner personally: a question to answer, a task, a decision, a reply someone is waiting on.",
    "waiting": "The owner is blocked on someone else. He has delegated or asked, and the next move belongs to the other party.",
    "scheduled": "Tied to a specific date or time: a calendar invite, a booking, a flight, an appointment, a dated reminder.",
    "read": "Worth reading but nothing to do: a newsletter he chose, an article, an industry update with real substance.",
    "reference": "Keep for lookup, no action and no reading: a receipt, an invoice, a statement, a confirmation, a shipping notice, credentials.",
    "someday": "An idea or opportunity with no commitment and no deadline. He might want it one day, nothing is owed now.",
    "noise": "Bulk or automated mail with no lasting value: marketing, promotions, app notifications, social digests, cold outreach.",
}
UNSURE = "unsure"
LABELS = [f"GTD/{b.capitalize()}" for b in list(BUCKETS) + [UNSURE]]

# Jev is cheap; a generous excerpt is what makes the answer good.
BODY_FETCH = 16384
BODY_CHARS = 2400
# Below this much probability mass on a single bucket the answer is not usable.
MIN_MASS = 0.45
IDLE_REARM = 9 * 60


# --------------------------------------------------------------------------- mail


def connect():
    user, password = os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"]
    conn = imaplib.IMAP4_SSL(HOST, 993)
    # App passwords are shown in four groups; the spaces are not part of it.
    conn.login(user, password.replace(" ", ""))
    return conn


def ensure_labels(conn):
    existing = set()
    for line in conn.list()[1] or []:
        existing.add(line.decode(errors="replace").rsplit(' "/" ', 1)[-1].strip('"'))
    for label in LABELS:
        if label not in existing:
            conn.create(f'"{label}"')
            print(f"created {label}")
    return existing


def known_correspondents(conn):
    """Addresses the owner has written to. The strongest person-vs-feed signal."""
    out = set()
    try:
        conn.select('"[Gmail]/Sent Mail"', readonly=True)
        uids = (conn.uid("SEARCH", None, "ALL")[1][0] or b"").split()
        if not uids:
            return out
        chunk = b",".join(uids[-2000:])
        for item in conn.uid("FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (TO)])")[1]:
            if isinstance(item, tuple):
                for addr in re.findall(rb"[\w.+-]+@[\w.-]+", item[1]):
                    out.add(addr.decode(errors="replace").lower())
    except Exception as exc:  # a missing Sent folder must not stop triage
        print(f"warning: sender history unavailable ({exc})", file=sys.stderr)
    return out


def condense(msg):
    """Plain text with quoted chains, signatures and long URLs removed."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_content()
                break
    else:
        body = msg.get_content() if msg.get_content_type().startswith("text") else ""
    lines = []
    for line in str(body).splitlines():
        s = line.strip()
        if s.startswith(">") or s in ("--", "-- "):
            continue
        if re.match(r"^(On .{0,80}wrote:|El .{0,80}escribió:|_{5,}|-{5,})", s):
            break
        lines.append(re.sub(r"https?://\S{30,}", "[link]", s))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:BODY_CHARS]


def fetch(conn, uids):
    """Headers plus a bounded body prefix. PEEK so nothing is marked read."""
    out = []
    for i in range(0, len(uids), 50):
        chunk = b",".join(str(u).encode() for u in uids[i : i + 50])
        spec = f"(BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.{BODY_FETCH}>)"
        typ, data = conn.uid("FETCH", chunk, spec)
        if typ != "OK":
            continue
        uid, raw = None, b""
        for item in data:
            if isinstance(item, tuple):
                found = re.search(rb"UID (\d+)", item[0])
                if found:
                    uid = int(found.group(1))
                raw += item[1]
            elif uid is not None:
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                out.append((uid, msg))
                uid, raw = None, b""
    return out


# --------------------------------------------------------------------------- jev


def ask(state):
    body = json.dumps(
        {
            "model": MODEL,
            "state": state,
            "questions": {
                "bucket": {
                    "type": "choice",
                    "instructions": "Which Getting Things Done list does this email belong on for its owner?",
                    "criteria": BUCKETS,
                },
                "needs_me": {
                    "type": "noul",
                    "instructions": "Does this need the owner personally, rather than anyone else or nobody?",
                },
                "has_deadline": {
                    "type": "noul",
                    "instructions": "Is there a date or deadline attached to this email?",
                },
                "still_open": {
                    "type": "noul",
                    "instructions": "Is there a live open loop here today?",
                    "criteria": {
                        "true": "Something is still outstanding: a reply owed, a task undone, a decision pending, an event ahead.",
                        "false": "Nothing is left to do with it now; it is finished, expired, or purely informational.",
                    },
                },
            },
        }
    ).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def classify(uid, msg, correspondents):
    sender = str(msg.get("From", ""))
    addr = (re.findall(r"[\w.+-]+@[\w.-]+", sender) or [""])[0].lower()
    answers = ask(
        {
            "owner_address": os.environ["IMAP_USER"],
            "from": sender,
            "to": str(msg.get("To", "")),
            "subject": str(msg.get("Subject", "")),
            "date": str(msg.get("Date", "")),
            "known_correspondent": addr in correspondents,
            "body": condense(msg),
        }
    )["answers"]
    probs = answers["bucket"]["probabilities"]
    top = answers["bucket"]["choice"]
    # Take Jev's answer as given; the only code policy is "is it usable at all".
    bucket = top if probs.get(top, 0) >= MIN_MASS else UNSURE
    return uid, bucket, probs


# --------------------------------------------------------------------------- apply


def apply(conn, decided, dry_run=False):
    """Batch by bucket: three IMAP commands per set, not per message."""
    by_bucket = {}
    for uid, bucket, _ in decided:
        by_bucket.setdefault(bucket, []).append(uid)

    for bucket, uids in sorted(by_bucket.items()):
        label = f"GTD/{bucket.capitalize()}"
        print(f"{label}: {len(uids)}")
        if dry_run:
            continue
        stale = " ".join(f'"{other}"' for other in LABELS if other != label)
        for i in range(0, len(uids), 200):
            uid_set = ",".join(str(u) for u in uids[i : i + 200])
            # .SILENT: without it the server emits untagged FETCH lines that
            # desynchronize the client's tag counter on the next command.
            conn.uid("STORE", uid_set, "-X-GM-LABELS.SILENT", f"({stale})")
            conn.uid("STORE", uid_set, "+X-GM-LABELS.SILENT", f'("{label}")')
            # Removing \Inbox via labels returns OK and archives nothing.
            # Archiving on Gmail is a move into All Mail. Unsure stays put:
            # it is the queue the owner still owes a decision to.
            if bucket != UNSURE:
                conn.uid("MOVE", uid_set, f'"{ALL_MAIL}"')


def run(conn, uids, correspondents, dry_run=False):
    messages = fetch(conn, uids)
    if not messages:
        return []
    with ThreadPoolExecutor(max_workers=5) as pool:
        decided = list(
            pool.map(lambda m: classify(m[0], m[1], correspondents), messages)
        )
    apply(conn, decided, dry_run)
    return decided


# --------------------------------------------------------------------------- idle


def idle_wait(conn, timeout):
    """IMAP IDLE by hand: imaplib has no idle() before Python 3.14."""
    tag = conn._new_tag()
    conn.send(b"%s IDLE\r\n" % tag)
    conn.readline()  # "+ idling"
    woke = bool(select.select([conn.sock], [], [], timeout)[0])
    conn.send(b"DONE\r\n")
    while not conn.readline().startswith(tag):
        pass
    return woke


def new_uids(conn, since):
    typ, data = conn.uid("SEARCH", None, f"UID {since + 1}:*")
    if typ != "OK":
        return []
    # `UID n:*` also matches the highest UID when it is below n, so filter.
    return sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > since)


def watch(correspondents):
    backoff = 2
    high_water = None
    while True:
        try:
            conn = connect()
            conn.select("INBOX")
            # On a reconnect, sweep before arming: anything that landed while
            # we were away was never pushed.
            if high_water is not None:
                missed = new_uids(conn, high_water)
                if missed:
                    high_water = max(missed)
                    run(conn, missed, correspondents)
            else:
                uids = (conn.uid("SEARCH", None, "ALL")[1][0] or b"").split()
                high_water = max((int(u) for u in uids), default=0)
            print(f"watching INBOX from uid {high_water}", flush=True)
            backoff = 2
            while True:
                idle_wait(conn, IDLE_REARM)
                # Sweep on every wake, push or timeout alike, so a missed push
                # costs latency rather than a lost message.
                arrived = new_uids(conn, high_water)
                if arrived:
                    high_water = max(arrived)
                    for uid, bucket, _ in run(conn, arrived, correspondents):
                        print(f"  uid {uid} -> {bucket}", flush=True)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f"connection failed: {exc}; retrying in {backoff}s", file=sys.stderr)
            __import__("time").sleep(backoff)
            backoff = min(backoff * 2, 300)


# --------------------------------------------------------------------------- cli


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["labels", "backfill", "watch"])
    parser.add_argument("--limit", type=int, help="only the newest N messages")
    parser.add_argument("--dry-run", action="store_true", help="classify, do not mutate")
    args = parser.parse_args()

    conn = connect()
    ensure_labels(conn)
    if args.command == "labels":
        return

    correspondents = known_correspondents(conn)
    print(f"{len(correspondents)} known correspondents")

    if args.command == "watch":
        conn.logout()
        return watch(correspondents)

    conn.select("INBOX")
    uids = [int(u) for u in (conn.uid("SEARCH", None, "ALL")[1][0] or b"").split()]
    if args.limit:
        uids = uids[-args.limit :]
    print(f"{len(uids)} messages to triage")
    # Page so an IMAP failure costs one page, not the run.
    for i in range(0, len(uids), 200):
        run(conn, uids[i : i + 200], correspondents, args.dry_run)
        print(f"  {min(i + 200, len(uids))}/{len(uids)}", flush=True)
    conn.logout()


if __name__ == "__main__":
    main()
