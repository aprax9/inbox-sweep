"""A labeled synthetic mailbox for measuring accuracy (`inbox-sweep eval --corpus`).

Every message carries the answer a careful person would give under the 20-year
test, and a scenario tag. Scenarios come from real cleanups: family on a
relative's work address, a school that emails from an unexpected domain,
employer newsletters, non-English tax mail, spam sent from webmail, and so on.
All domains use the reserved .example TLD.
"""

from __future__ import annotations

import random
import time

from inbox_sweep.config import Config
from inbox_sweep.model import CATEGORY_PROMOTIONS, CATEGORY_SOCIAL, CATEGORY_UPDATES, INBOX, SENT, STARRED, Message

DAY = 86400
ME = "me@example.com"


def corpus_config() -> Config:
    """The settings a person would plausibly give the wizard for this mailbox."""
    cfg = Config()
    cfg.me = [ME]
    cfg.protect_people = ["Okonkwo"]          # family surname
    cfg.protect_domains = ["employer.example"]
    cfg.protect_labels = ["Taxes"]
    return cfg


class _Corpus:
    def __init__(self, now: int, seed: int):
        self.now = now
        self.rng = random.Random(seed)
        self.messages: list[Message] = []
        self.truth: dict[str, str] = {}
        self.scenario: dict[str, str] = {}
        self.n = 0

    def add(self, scenario: str, truth: str, *, days: float, sender: str, subject: str, name: str = "",
            thread: str | None = None, labels=None, bulk: bool = False, list_id: str = "", to=None,
            precedence: str = "", auto: str = "") -> Message:
        self.n += 1
        mid = f"c{self.n:06d}"
        msg = Message(
            id=mid, thread_id=thread or f"ct-{mid}", date=int(self.now - days * DAY),
            from_email=sender, from_name=name, to=list(to if to is not None else [ME]), subject=subject,
            labels=list(labels if labels is not None else [INBOX]),
            list_unsubscribe=(f"<https://{sender.split('@')[1]}/u>" if bulk else ""),
            list_unsubscribe_post=("List-Unsubscribe=One-Click" if bulk else ""),
            list_id=list_id, precedence=precedence, auto_submitted=auto,
            message_id_header=f"<{mid}@corpus.example>",
        )
        self.messages.append(msg)
        self.truth[mid] = truth
        self.scenario[mid] = scenario
        return msg

    def thread_with_reply(self, scenario: str, *, days: float, other: str, name: str, subject: str) -> None:
        thread = f"ct-thread-{self.n + 1}"
        self.add(scenario, "keep", days=days, sender=other, name=name, subject=subject, thread=thread)
        self.add(scenario, "keep", days=days - 1, sender=ME, name="Me", subject="Re: " + subject,
                 thread=thread, labels=[SENT], to=[other])

    def ages(self, k: int, lo: float, hi: float) -> list[float]:
        return [self.rng.uniform(lo, hi) for _ in range(k)]


def labeled_corpus(now: int | None = None, seed: int = 11) -> tuple[list[Message], dict[str, str], dict[str, str]]:
    """Returns (messages, truth {id: keep|trash}, scenario {id: tag})."""
    c = _Corpus(int(now or time.time()), seed)
    r = c.rng

    # ---- Must keep -------------------------------------------------------------
    for d in c.ages(6, 60, 6000):
        c.thread_with_reply("friend, replied", days=d, other="sam.friend@gmail.com", name="Sam",
                            subject=r.choice(["Dinner?", "Photos", "Moving update"]))
    for d in c.ages(4, 60, 6000):
        c.add("grandparent on webmail, never replied", "keep", days=d, sender="nana.k@aol.com", name="Nana",
              subject=r.choice(["Recipe", "Old photos of your father", "Thinking of you"]))
    for d in c.ages(5, 60, 5000):
        c.add("family on a work address (surname)", "keep", days=d, sender="a.okonkwo@bigbank.example",
              name="Ada Okonkwo", subject=r.choice(["Funds transferred", "FW: flight details", "Call me"]))
    # A friend you emailed once, who replies from a corporate address.
    c.add("you wrote to them once", "keep", days=3000, sender=ME, subject="Congrats on the new job!",
          labels=[SENT], to=["jo@startup.example"])
    for d in c.ages(3, 100, 2900):
        c.add("friend at a work address you've emailed", "keep", days=d, sender="jo@startup.example",
              name="Jo", subject=r.choice(["Lunch Friday", "Wedding plans", "Long time!"]))
    # A school that emails from a domain unlike its name: the teacher was emailed once.
    c.add("school, odd domain (you emailed the teacher)", "keep", days=4200, sender=ME,
          subject="Re: project deadline", labels=[SENT], to=["ms.lee@k12-district.example"])
    for d in c.ages(3, 4000, 4500):
        c.add("school, odd domain (you emailed the teacher)", "keep", days=d, sender="ms.lee@k12-district.example",
              name="Ms Lee", subject=r.choice(["Homework feedback", "Field trip forms", "Great essay"]))
    for d in c.ages(4, 300, 2500):
        c.add("colleague at employer", "keep", days=d, sender="dana@employer.example", name="Dana",
              subject=r.choice(["Q3 plan", "Notes from today", "Offsite"]))
    c.add("employer HR document", "keep", days=900, sender="payroll@employer.example", subject="Your 2023 T4 slip")
    c.add("employer HR document", "keep", days=400, sender="hr@employer.example", subject="Pay slips: December")
    for year in range(2010, 2025, 2):
        c.add("tax notice (English)", "keep", days=(2026 - year) * 365, sender="noreply@revenue.gov.example",
              subject=f"Your {year} notice of assessment is ready")
    c.add("tax notice (Chinese)", "keep", days=700, sender="e_alert@ird.gov.hk", subject="稅務易 帳戶信息：你的報稅表")
    c.add("tax notice (German)", "keep", days=800, sender="noreply@elster.de", subject="Ihr Steuerbescheid 2023")
    c.add("tax notice (French)", "keep", days=900, sender="ne-pas-repondre@dgfip.finances.gouv.fr",
          subject="Votre avis d'impôt sur le revenu est disponible")
    c.add("visa / immigration", "keep", days=1200, sender="no-reply@evisa.gov.example", subject="E-visa approved")
    c.add("visa / immigration", "keep", days=1900, sender="noreply@immigration.example", subject="Work permit decision")
    c.add("signed document", "keep", days=1500, sender="dse@docusign.net", subject="Completed: Lease agreement")
    c.add("transcript / admission", "keep", days=3300, sender="registrar@university.example",
          subject="Your official transcript")
    c.add("transcript / admission", "keep", days=3600, sender="admissions@university.example",
          subject="Offer of admission")
    c.add("job offer, not replied", "keep", days=1000, sender="talent@acme-hiring.example", subject="Your offer letter")
    for d in c.ages(4, 40, 1000):
        c.add("e-transfer within 3 years", "keep", days=d, sender="notify@payments.example",
              subject=f"e-Transfer: Sam sent you ${r.randint(20, 200)}")
    c.add("starred promotion", "keep", days=1200, sender="deals@shop.example", subject="Your code: 40% off",
          labels=[STARRED, CATEGORY_PROMOTIONS], bulk=True)
    c.add("in a protected label", "keep", days=2500, sender="statements@broker.example",
          subject="Annual statement", labels=["Taxes"])
    for d in c.ages(3, 1, 25):
        c.add("recent, not junk", "keep", days=d, sender="support@utility.example", subject="Your account update")

    # ---- Should go ---------------------------------------------------------------
    for d in c.ages(25, 40, 4000):
        c.add("newsletter", "trash", days=d, sender="digest@weekly.example", name="Weekly",
              subject="This week's best reads", bulk=True, list_id="<weekly.example>")
    for d in c.ages(25, 40, 4000):
        c.add("promotion", "trash", days=d, sender="offers@store.example", subject="Last chance: 30% off",
              labels=[INBOX, CATEGORY_PROMOTIONS], bulk=True)
    for d in c.ages(10, 40, 3000):
        c.add("social notification", "trash", days=d, sender="notification@facebookmail.com",
              subject="You have 3 new notifications", labels=[CATEGORY_SOCIAL], bulk=True)
    for d in c.ages(12, 40, 3000):
        c.add("security code", "trash", days=d, sender="no-reply@accounts.example",
              subject=r.choice(["Your verification code is 123456", "New sign-in to your account"]))
    for d in c.ages(12, 40, 3000):
        c.add("bank notice", "trash", days=d, sender="alerts@bank.example",
              subject=r.choice(["Your e-statement is ready", "Payment reminder"]), labels=[CATEGORY_UPDATES])
    for d in c.ages(12, 60, 3000):
        c.add("job application confirmation/rejection", "trash", days=d, sender="no-reply@globex.greenhouse.example",
              subject=r.choice(["Thank you for applying to Globex", "Update on your application"]))
    for d in c.ages(8, 1200, 4000):
        c.add("old receipt (over 3 years)", "trash", days=d, sender="orders@shop.example",
              subject=f"Your order #{r.randint(1000, 9999)} has shipped", labels=[CATEGORY_UPDATES])
    for d in c.ages(8, 400, 4000):
        c.add("old booking (over 1 year)", "trash", days=d, sender="bookings@airline.example",
              subject="Your e-ticket and itinerary", labels=[CATEGORY_UPDATES])
    for d in c.ages(6, 1200, 4000):
        c.add("old e-transfer (over 3 years)", "trash", days=d, sender="notify@payments.example",
              subject="e-Transfer: Sam sent you $40")
    for d in c.ages(10, 40, 2000):
        c.add("employer marketing newsletter", "trash", days=d, sender="news@employer.example",
              subject="Employer Insights #12", bulk=True, list_id="<insights.employer.example>")
    for d in c.ages(10, 40, 2000):
        c.add("university alumni fundraising", "trash", days=d, sender="alumni@university.example",
              subject="Alumni Minute: give today", bulk=True, list_id="<alumni.university.example>")
    for d in c.ages(10, 40, 1000):
        c.add("code-host notification", "trash", days=d, sender="notifications@codehost.example",
              subject="[org/repo] Fix build (#412)", list_id="<repo.codehost.example>", precedence="list")
    for d in c.ages(8, 100, 2000):
        c.add("calendar notification", "trash", days=d, sender="calendar-notification@calendar.example",
              subject="Notification: Team sync @ Tue 10am")
    for d in c.ages(6, 100, 3000):
        c.add("reminder", "trash", days=d, sender="billing@telco.example", subject="Your bill is due soon")
    # Known hard cases (counted honestly in the report):
    for d in c.ages(6, 200, 3000):
        c.add("spam from a webmail address", "trash", days=d, sender=f"winner{r.randint(1, 99)}@gmail.com",
              name="Prize Desk", subject="You won! Reply for details")
    for d in c.ages(6, 200, 3000):
        c.add("cold outreach from a stranger", "trash", days=d, sender="chris@staffing.example",
              name="Chris", subject="Exciting opportunity for you")
    return c.messages, c.truth, c.scenario
