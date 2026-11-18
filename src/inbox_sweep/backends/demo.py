"""A synthetic mailbox for trying inbox-sweep without connecting a real account.

`inbox-sweep --backend demo index` creates ~15 years of fake mail (family, friends,
work, newsletters, receipts, e-transfers, tax mail, job hunting, security codes)
in .inbox-sweep/demo-mailbox.json. Trash and restore just flip a TRASH label in
that file. All domains use the reserved .example TLD.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from inbox_sweep.backends.base import Backend, LiveProtection, Progress
from inbox_sweep.model import (
    CATEGORY_PROMOTIONS,
    CATEGORY_SOCIAL,
    CATEGORY_UPDATES,
    INBOX,
    SENT,
    STARRED,
    TRASH,
    Message,
)

DAY = 86400


class _Builder:
    def __init__(self, me: str, now: int, seed: int = 7):
        self.me = me
        self.now = now
        self.rng = random.Random(seed)
        self.messages: list[Message] = []
        self.n = 0

    def add(self, *, days_ago: float, sender: str, name: str, subject: str, thread: str | None = None,
            labels=None, bulk: bool = False, one_click: bool = False, to=None, list_id: str = "",
            precedence: str = "") -> Message:
        self.n += 1
        mid = f"demo-{self.n:05d}"
        lu = ""
        lup = ""
        if bulk:
            domain = sender.split("@")[1]
            lu = f"<mailto:unsubscribe@{domain}>, <https://{domain}/unsubscribe?u={self.n}>"
            if one_click:
                lup = "List-Unsubscribe=One-Click"
        msg = Message(
            id=mid,
            thread_id=thread or f"t-{mid}",
            date=int(self.now - days_ago * DAY),
            from_email=sender,
            from_name=name,
            to=to if to is not None else [self.me],
            subject=subject,
            labels=list(labels or [INBOX]),
            list_unsubscribe=lu,
            list_unsubscribe_post=lup,
            list_id=list_id,
            precedence=precedence,
            message_id_header=f"<{mid}@demo.example>",
        )
        self.messages.append(msg)
        return msg

    def conversation(self, *, days_ago: float, other: str, name: str, subject: str, turns: int) -> None:
        thread = f"t-conv-{self.n + 1:05d}"
        for i in range(turns):
            age = days_ago - i * 0.5
            if i % 2 == 0:
                self.add(days_ago=age, sender=other, name=name, subject=subject, thread=thread)
            else:
                self.add(days_ago=age, sender=self.me, name="Me", subject="Re: " + subject,
                         thread=thread, labels=[SENT], to=[other])


def generate_mailbox(me: str, now: int | None = None) -> list[Message]:
    now = int(now or time.time())
    b = _Builder(me, now)
    r = b.rng

    # People you write to: family, friends, colleagues.
    family = [("mom.demo@gmail.com", "Mom"), ("sam.sibling@outlook.com", "Sam")]
    friends = [("alex.rivera@gmail.com", "Alex Rivera"), ("jordan.k@hotmail.com", "Jordan K"),
               ("taylor.m@icloud.com", "Taylor M")]
    for addr, name in family + friends:
        for _ in range(4):
            b.conversation(days_ago=r.uniform(40, 5000), other=addr, name=name,
                           subject=r.choice(["Dinner Sunday?", "Photos from the trip", "Happy birthday!",
                                             "Quick question", "Flight times"]), turns=r.randint(2, 4))
    # A relative who emails but you never replied to by email.
    for _ in range(3):
        b.add(days_ago=r.uniform(300, 4000), sender="grandpa.joe@aol.com", name="Grandpa Joe",
              subject=r.choice(["Old family photos", "Recipe you asked about", "Thinking of you"]))

    # Work: colleagues (replied) and the company's own newsletter.
    for _ in range(6):
        b.conversation(days_ago=r.uniform(400, 2500), other="dana.lee@acme-corp.example", name="Dana Lee",
                       subject=r.choice(["Q3 plan", "Offer letter - signed copy", "Team offsite"]), turns=3)
    for i in range(24):
        b.add(days_ago=60 + i * 30, sender="news@acme-corp.example", name="Acme Corp",
              subject=f"Acme Insights #{100 - i}", bulk=True, one_click=True, list_id="<insights.acme-corp.example>")

    # Newsletters and promotions (bulk, unsubscribe headers).
    for sender, name, subject in [
        ("digest@weekly-reads.example", "Weekly Reads", "This week's 10 best essays"),
        ("hello@startup-news.example", "Startup News", "Funding roundup"),
        ("alumni@state-university.example", "State University Alumni", "Alumni Minute: give today"),
    ]:
        for i in range(30):
            b.add(days_ago=5 + i * 45, sender=sender, name=name, subject=subject, bulk=True,
                  one_click=True, list_id=f"<{sender.split('@')[1]}>")
    for sender, name in [("deals@shoe-shop.example", "Shoe Shop"), ("offers@electro-mart.example", "Electro Mart"),
                         ("promo@coffee-club.example", "Coffee Club")]:
        for i in range(25):
            b.add(days_ago=3 + i * 60, sender=sender, name=name,
                  subject=r.choice(["40% off this weekend", "Last chance: free shipping", "New arrivals"]),
                  labels=[INBOX, CATEGORY_PROMOTIONS], bulk=True, one_click=(i % 2 == 0))
    starred_promo = b.add(days_ago=900, sender="deals@shoe-shop.example", name="Shoe Shop",
                          subject="Your exclusive code", labels=[STARRED, CATEGORY_PROMOTIONS], bulk=True)
    assert starred_promo

    # Social and developer notifications.
    for i in range(20):
        b.add(days_ago=10 + i * 70, sender="notification@social-net.example", name="SocialNet",
              subject="You have 3 new notifications", labels=[CATEGORY_SOCIAL], bulk=True)
        b.add(days_ago=2 + i * 20, sender="notifications@code-host.example", name="CodeHost",
              subject=f"[org/repo] Fix build (#{400 + i})", list_id="<repo.org.code-host.example>",
              precedence="list")

    # Security codes, reminders, account notices.
    for i in range(25):
        b.add(days_ago=1 + i * 90, sender="no-reply@bank.example", name="Example Bank",
              subject=r.choice(["Your verification code is 482913", "New sign-in to your account",
                                "Your e-statement is ready", "Payment reminder: card due"]),
              labels=[CATEGORY_UPDATES])

    # Money from people: e-transfers over 8 years.
    for i in range(16):
        who = r.choice(["Alex Rivera", "Sam", "Jordan K"])
        b.add(days_ago=20 + i * 180, sender="notify@payments.interbank.example", name="Interbank e-Transfer",
              subject=f"e-Transfer: {who} sent you $" + str(r.randint(20, 300)), labels=[CATEGORY_UPDATES])

    # Documents that matter for decades.
    for year in range(2012, 2025, 3):
        b.add(days_ago=(2026 - year) * 365, sender="noreply@tax-agency.gov.example", name="Tax Agency",
              subject=f"Your {year} Notice of Assessment is available")
    b.add(days_ago=1500, sender="dse@esign.example", name="eSign",
          subject="Completed: Lease agreement - 12 Example St", labels=[INBOX])
    b.add(days_ago=2900, sender="registrar@state-university.example", name="Registrar",
          subject="Your official transcript")

    # Job hunting: confirmations, rejections, interviews.
    for i in range(20):
        company = r.choice(["Globex", "Initech", "Umbrella", "Hooli", "Vandelay"])
        b.add(days_ago=30 + i * 40, sender=f"no-reply@{company.lower()}.ats.example", name=f"{company} Careers",
              subject=r.choice([f"Thank you for applying to {company}", f"Update on your application to {company}",
                                f"{company}: application received"]))
    b.add(days_ago=12, sender="recruiter@hooli.example", name="Hooli Recruiting",
          subject="Interview confirmation: Product Manager")
    b.add(days_ago=800, sender="recruiter@globex.example", name="Globex Recruiting",
          subject="Next steps: take-home assessment")

    # Receipts and bookings.
    for i in range(20):
        b.add(days_ago=15 + i * 120, sender="orders@book-store.example", name="Book Store",
              subject=f"Your order #{1000 + i} has shipped", labels=[CATEGORY_UPDATES])
        b.add(days_ago=40 + i * 150, sender="bookings@air-line.example", name="Air Line",
              subject="Your e-ticket receipt and itinerary", labels=[CATEGORY_UPDATES])

    # Strangers who wrote once (kept by default, flagged for review).
    for name, addr, subject in [("Chris Wong", "chris@staffing-agency.example", "Exciting opportunity for you"),
                                ("Pat Lee", "pat.lee@vendor.example", "Following up on our demo"),
                                ("Rene", "rene@listing-site.example", "Re: your apartment ad")]:
        b.add(days_ago=r.uniform(500, 3000), sender=addr, name=name, subject=subject)

    return b.messages


class DemoBackend(Backend):
    name = "demo"

    def __init__(self, path: Path, me: str = "you@example.com"):
        self.path = Path(path)
        self.me = me
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._save(generate_mailbox(me))

    def _load(self) -> list[Message]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [Message(**m) for m in data["messages"]]

    def _save(self, messages: list[Message]) -> None:
        payload = {"account": self.me, "messages": [asdict(m) for m in messages]}
        self.path.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    def account(self) -> str:
        return self.me

    def iter_messages(self, query=None, limit=None, progress: Progress = None,
                      include_trash: bool = False) -> Iterator[Message]:
        count = 0
        for msg in self._load():
            if TRASH in msg.labels and not include_trash:
                continue
            yield msg
            count += 1
            if limit and count >= limit:
                return

    def protected_thread_ids(self, protect: LiveProtection) -> set[str] | None:
        out = set()
        for m in self._load():
            if TRASH in m.labels:
                continue
            if ((protect.starred and STARRED in m.labels)
                    or (protect.sent and (SENT in m.labels or m.from_email in protect.me))
                    or any(m.in_label(wanted) for wanted in protect.labels)
                    or m.from_email in protect.people or any(a in protect.people for a in m.to + m.cc)):
                out.add(m.thread_id)
        return out

    # Test helpers: simulate changes made in the mail app after indexing.
    def set_labels(self, message_id: str, add: list[str] = (), remove: list[str] = ()) -> None:
        msgs = self._load()
        for m in msgs:
            if m.id == message_id:
                m.labels = sorted((set(m.labels) | set(add)) - set(remove))
        self._save(msgs)

    def add_message(self, msg: Message) -> None:
        msgs = self._load()
        msgs.append(msg)
        self._save(msgs)

    def label_messages(self, messages, label, progress: Progress = None) -> None:
        wanted = {m.id for m in messages}
        all_msgs = self._load()
        for m in all_msgs:
            if m.id in wanted and label not in m.labels:
                m.labels.append(label)
        self._save(all_msgs)

    def _set_trash(self, entries, trashed: bool) -> list[str]:
        threads = {e["thread_id"] for e in entries}
        all_msgs = self._load()
        for m in all_msgs:
            if m.thread_id in threads:
                if trashed and TRASH not in m.labels:
                    m.labels.append(TRASH)
                if not trashed and TRASH in m.labels:
                    m.labels.remove(TRASH)
        self._save(all_msgs)
        return [e["thread_id"] for e in entries]

    def trash(self, entries, progress: Progress = None) -> list[str]:
        return self._set_trash(entries, True)

    def restore(self, entries, progress: Progress = None) -> list[str]:
        return self._set_trash(entries, False)

    # Test helper
    def trashed_thread_ids(self) -> set[str]:
        return {m.thread_id for m in self._load() if TRASH in m.labels}
