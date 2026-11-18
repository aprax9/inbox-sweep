"""Property tests: for *any* mailbox, the safety invariants hold.

Hypothesis generates random mailboxes (people, bulk senders, your own replies,
stars, labels, shared threads, ages) and checks what must always be true.
"""

import random

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from inbox_sweep.backends.base import Backend
from inbox_sweep.classify import build_context, classify, protection
from inbox_sweep.config import Config
from inbox_sweep.model import CATEGORY_PROMOTIONS, INBOX, SENT, STARRED, TRASH, Message
from inbox_sweep.plan import apply_plan, build_plan, undo, verify_plan

NOW = 1_800_000_000
DAY = 86400
ME = "me@example.com"
SENDERS = [
    (ME, "Me", False), ("mom@family.example", "Mom", False), ("pal@gmail.com", "Pal", False),
    ("boss@employer.example", "Boss", False), ("news@employer.example", "Employer News", True),
    ("deals@shop.example", "Shop", True), ("no-reply@bank.example", "Bank", False),
    ("notifications@code-host.example", "CodeHost", True), ("stranger@outlook.com", "Stranger", False),
    ("cra@revenue.gov.example", "Revenue Agency", False),
]
SUBJECTS = ["Dinner?", "40% off", "Your verification code is 123456", "Notice of Assessment",
            "Re: plans", "Weekly digest", "Your order has shipped", "Offer letter", "[repo] PR #4", "Hello"]
LABELS = [INBOX, STARRED, CATEGORY_PROMOTIONS, "Taxes", "Family", TRASH]

message_spec = st.tuples(
    st.integers(0, len(SENDERS) - 1),           # sender
    st.integers(0, len(SUBJECTS) - 1),          # subject
    st.integers(0, 12),                         # thread bucket (small => shared threads)
    st.floats(0, 7000),                         # age in days
    st.sets(st.sampled_from(LABELS), max_size=3),
    st.booleans(),                              # addressed to a protected person too
)


def build(specs) -> list[Message]:
    out = []
    for i, (s, subj, thread, age, labels, cc_protected) in enumerate(specs):
        email, name, bulk = SENDERS[s]
        labels = set(labels) | ({SENT} if email == ME else set())
        out.append(Message(
            id=f"m{i}", thread_id=f"t{thread}", date=int(NOW - age * DAY), from_email=email, from_name=name,
            to=[ME] if email != ME else ["pal@gmail.com"], cc=["aunt@family.example"] if cc_protected else [],
            subject=SUBJECTS[subj], labels=sorted(labels),
            list_unsubscribe="<https://x.example/u>" if bulk else ""))
    return out


def config() -> Config:
    cfg = Config()
    cfg.me = [ME]
    cfg.protect_people = ["aunt@family.example", "Okonkwo"]
    cfg.protect_labels = ["Taxes"]
    cfg.protect_domains = ["employer.example"]
    return cfg


mailboxes = st.lists(message_spec, min_size=1, max_size=60).map(build)
FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@FAST
@given(mailboxes)
def test_absolutely_protected_mail_is_never_planned(messages):
    cfg = config()
    plan = build_plan(messages, classify(messages, cfg, now=NOW), cfg)
    planned = {r["id"] for e in plan["trash"] for r in e["messages"]}
    ctx = build_context(messages, cfg)
    for m in messages:
        if m.id in planned:
            assert protection(m, cfg, ctx, NOW)[0] != "absolute"
            assert STARRED not in m.labels and m.from_email != ME and "Taxes" not in m.labels
            assert m.thread_id not in ctx.replied_threads
            assert "aunt@family.example" not in m.cc


@FAST
@given(mailboxes)
def test_threads_are_never_split_and_trash_is_never_replanned(messages):
    cfg = config()
    decisions = classify(messages, cfg, now=NOW)
    plan = build_plan(messages, decisions, cfg)
    kept_threads = {d.thread_id for d in decisions if d.action == "keep"}
    planned_threads = {e["thread_id"] for e in plan["trash"]}
    assert not planned_threads & kept_threads
    planned = {r["id"] for e in plan["trash"] for r in e["messages"]}
    for m in messages:
        if m.thread_id in planned_threads and TRASH not in m.labels:
            assert m.id in planned  # whole thread goes, not part of it
        if TRASH in m.labels:
            assert m.id not in planned


@FAST
@given(mailboxes)
def test_every_plan_passes_the_independent_check(messages):
    cfg = config()
    decisions = classify(messages, cfg, now=NOW)
    assert verify_plan(build_plan(messages, decisions, cfg), messages, decisions, cfg, now=NOW) == []


@FAST
@given(mailboxes, st.randoms(use_true_random=False))
def test_decisions_do_not_depend_on_message_order(messages, rng):
    cfg = config()
    before = {d.message_id: (d.action, d.category) for d in classify(messages, cfg, now=NOW)}
    shuffled = list(messages)
    rng.shuffle(shuffled)
    after = {d.message_id: (d.action, d.category) for d in classify(shuffled, cfg, now=NOW)}
    assert before == after


class MemoryBackend(Backend):
    name = "memory"

    def __init__(self, messages):
        self.labels = {m.id: set(m.labels) for m in messages}
        self.thread_of = {m.id: m.thread_id for m in messages}

    def account(self):
        return ""

    def iter_messages(self, query=None, limit=None, progress=None, include_trash=False):
        return iter(())

    def label_messages(self, messages, label, progress=None):
        for m in messages:
            self.labels[m.id].add(label)

    def trash(self, entries, progress=None):
        for e in entries:
            for r in e["messages"]:
                self.labels[r["id"]].add(TRASH)
        return [e["thread_id"] for e in entries]

    def restore(self, entries, progress=None):
        for e in entries:
            for r in e["messages"]:
                self.labels[r["id"]].discard(TRASH)
        return [e["thread_id"] for e in entries]


@settings(max_examples=60, deadline=None)
@given(mailboxes)
def test_apply_then_undo_restores_exactly_the_original_state(tmp_path_factory, messages):
    cfg = config()
    cfg.data_dir = str(tmp_path_factory.mktemp("data"))
    cfg.apply_batch_size = random.Random(len(messages)).randint(1, 7)
    backend = MemoryBackend(messages)
    original = {k: set(v) for k, v in backend.labels.items()}
    plan = build_plan(messages, classify(messages, cfg, now=NOW), cfg)
    result = apply_plan(plan, backend, cfg, messages, label_keep=False, live_check=False, now=NOW)
    planned = {r["id"] for e in plan["trash"] for r in e["messages"]}
    assert {mid for mid, labels in backend.labels.items() if TRASH in labels and TRASH not in original[mid]} == planned
    undo(result["log"], backend, cfg)
    assert backend.labels == original
