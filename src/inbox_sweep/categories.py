"""Content categories. Each category gets a retention period in sweep.toml.

Order matters: the first match wins, and the "keep for decades" categories are
checked before the junk ones. Promotional mail is never allowed to claim the
documents category ("Open an RRSP today!" is not a tax record).
"""

from __future__ import annotations

import re

from inbox_sweep.headers import is_bulk, is_machine
from inbox_sweep.model import CATEGORY_PROMOTIONS, CATEGORY_SOCIAL, Message

CATEGORIES = [
    "documents", "person_payment", "job_process", "receipt", "booking", "security",
    "reminder", "account_notice", "job_automated", "newsletter", "promotion", "social",
    "other_automated", "human_unknown",
]

CATEGORY_HELP = {
    "documents": "Tax, government, visas, contracts, offers, transcripts, insurance, pensions",
    "person_payment": "Money from or to people (e-transfers, 'X paid you')",
    "job_process": "Interviews, assessments, next steps",
    "receipt": "Receipts, invoices, orders, refunds",
    "booking": "Flights, hotels, tickets, reservations",
    "security": "Codes, sign-in alerts, password resets",
    "reminder": "Bills due, renewals, appointments",
    "account_notice": "Statements ready, terms and privacy updates, account notices",
    "job_automated": "Application received, rejections, job alerts",
    "newsletter": "Mailing lists and newsletters",
    "promotion": "Marketing and sales",
    "social": "Social network notifications",
    "other_automated": "Other machine-sent mail",
    "human_unknown": "Written by a person you never wrote to",
}

_I = re.IGNORECASE

FORM_CODES = re.compile(r"\b(T4A?|T5\d{0,3}|T1|T2202|W-?2|W-?9|1099(-[A-Z]+)?|1098(-[A-Z])?|P60|P45|K-1)\b")
DOCUMENTS = re.compile(
    r"tax (return|slip|form|document|statement|receipt|assessment|filing)|notice of (re)?assessment|"
    r"\be-?visa\b|visa (application|approval|approved|grant(ed)?|decision|issued)|"
    r"(work|study|residence) permit|permanent resid|passport|immigration|citizenship|"
    r"offer letter|offer of employment|job offer|employment (agreement|contract|verification)|"
    r"exit letter|termination letter|severance|reference letter|recommendation letter|"
    r"\btranscript\b|diploma|degree (certificate|conferred)|offer of admission|admission (offer|decision)|"
    r"acceptance letter|certificate (of completion|awarded)|"
    r"insurance policy|policy document|lease agreement|tenancy agreement|rental agreement|mortgage|"
    r"closing documents|\bdeed\b|pension|retirement (plan|savings)|401\(?k\)?|\bRRSP\b|\bTFSA\b|\bIRA\b|\bMPF\b|"
    r"class action|settlement (notice|claim|payment)|consumer claim|court (date|notice|order)|legal notice|"
    r"pay ?slip|pay stub|salary statement|record of employment|"
    r"vaccin(e|ation) (certificate|record)|immuni[sz]ation record|medical record|"
    r"(birth|marriage|death) certificate",
    _I,
)
ESIGN_SENDER = re.compile(r"docusign|hellosign|dropboxsign|adobesign|echosign|eversign|pandadoc|signnow|esign", _I)
SIGNED = re.compile(r"completed|signed|document (is )?ready|fully executed", _I)

PERSON_PAYMENT = re.compile(
    r"e-?transfer|sent you (money|\$|£|€|₹|[0-9])|paid you|has sent you|money (request|transfer)|"
    r"you('ve)? received (a payment|money|\$)|\bzelle\b|\bvenmo\b|\binterac\b|\bpayme\b",
    _I,
)
JOB_PROCESS = re.compile(
    r"\binterview|next steps?\b|take[- ]home|assessment|invitation to complete|"
    r"\bcase study\b|hiring manager|\bonsite\b|\bsuperday\b",
    _I,
)
JOB_AUTOMATED = re.compile(
    r"thank(s| you) for (applying|your (interest|application))|application (received|submitted|status|update)|"
    r"(update|regarding) (on|to) your application|your application (to|for|with)|we('ve| have) received your application|"
    r"not (be )?moving forward|unfortunately|other candidates|job alert|jobs? (for you|matching)|new jobs|"
    r"recommended jobs|is hiring",
    _I,
)
ATS_SENDER = re.compile(
    r"greenhouse|lever\.co|myworkday|workday|icims|smartrecruiters|ashbyhq|jobvite|taleo|workable|"
    r"bamboohr|teamtailor|breezy|recruitee|successfactors|pinpointhq|applytojob|\.ats\.|"
    r"indeed\.com|ziprecruiter|glassdoor|monster\.|jobs-noreply|jobalerts",
    _I,
)
BOOKING = re.compile(
    r"booking|reservation|itinerary|e-?ticket|boarding pass|check[- ]?in|your (trip|flight|stay)|"
    r"flight (confirmation|reminder|change)|hotel",
    _I,
)
RECEIPT = re.compile(
    r"receipt|invoice|order (#|no\.|number|confirm|has shipped|shipped|delivered)|your order|"
    r"payment (confirmation|received|successful)|purchase|has shipped|delivered|refund",
    _I,
)
SECURITY = re.compile(
    r"verification code|security code|one[- ]time (pass)?code|\bOTP\b|2-step|two-factor|\b2FA\b|"
    r"\bsign[- ]?in\b|log[- ]?in (alert|attempt)|new (device|login)|password (reset|changed|change)|"
    r"reset your password|security alert|verify your (email|account|identity)|confirm your email|"
    r"account (locked|unlocked)|is your .{0,20}code",
    _I,
)
REMINDER = re.compile(
    r"reminder|overdue|past due|due (date|soon|today|tomorrow)|payment due|renew(al)?\b|"
    r"expir(es|ing|ed|y)|appointment",
    _I,
)
ACCOUNT_NOTICE = re.compile(
    r"statement (is )?(ready|available)|e-?statement|terms (of service|of use|and conditions)|"
    r"privacy (policy|notice)|updates? to (our|your)|account (update|notice|alert|activity)|^alert\b|notice:",
    _I,
)
SOCIAL_SENDER = re.compile(
    r"facebookmail\.com|linkedin\.com|twitter\.com|@x\.com|instagram|tiktok|pinterest|redditmail|"
    r"reddit\.com|meetup\.com|quora|nextdoor|snapchat",
    _I,
)
PROMO = re.compile(
    r"\d+\s?% off|\bsale\b|\bdeals?\b|free shipping|limited time|last chance|exclusive (offer|code|access)|"
    r"new arrivals|coupon|promo code|black friday|cyber monday|don't miss|shop now",
    _I,
)
NEWSLETTER = re.compile(r"newsletter|digest|weekly|monthly|edition|issue #|roundup|insights", _I)


def categorize(msg: Message) -> str:
    subject = msg.subject or ""
    sender = msg.from_email or ""
    promo_label = CATEGORY_PROMOTIONS in msg.labels
    bulk = is_bulk(msg)
    marketing = promo_label or (bulk and bool(PROMO.search(subject)))

    if not marketing and not msg.list_id:
        if FORM_CODES.search(subject) or DOCUMENTS.search(subject):
            return "documents"
        if ESIGN_SENDER.search(sender) and SIGNED.search(subject):
            return "documents"
        if PERSON_PAYMENT.search(subject):
            return "person_payment"
    if SECURITY.search(subject):
        return "security"
    if not bulk and JOB_PROCESS.search(subject):
        return "job_process"
    if JOB_AUTOMATED.search(subject) or (ATS_SENDER.search(sender) and is_machine(msg)):
        return "job_automated"
    if not marketing:
        if BOOKING.search(subject):
            return "booking"
        if RECEIPT.search(subject):
            return "receipt"
        if REMINDER.search(subject):
            return "reminder"
        if ACCOUNT_NOTICE.search(subject):
            return "account_notice"
    if CATEGORY_SOCIAL in msg.labels or SOCIAL_SENDER.search(sender):
        return "social"
    if marketing:
        return "promotion"
    if msg.list_id or msg.precedence.lower() == "list" or (bulk and NEWSLETTER.search(subject)) or bulk:
        return "newsletter"
    if is_machine(msg):
        return "other_automated"
    return "human_unknown"
