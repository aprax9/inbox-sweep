from inbox_sweep.classify import classify, summarize
from inbox_sweep.model import CATEGORY_PROMOTIONS, SENT, STARRED

from conftest import NOW, make


def decide(messages, cfg, **kw):
    return {d.message_id: d for d in classify(messages, cfg, now=NOW, **kw)}


def test_replied_thread_is_kept_whole(cfg):
    a = make(sender="recruiter@corp.example", subject="Opportunity", thread="T", days_ago=900)
    b = make(sender="me@example.com", subject="Re: Opportunity", thread="T", days_ago=899, labels=[SENT],
             to=["recruiter@corp.example"])
    d = decide([a, b], cfg)
    assert d[a.id].action == "keep" and d[a.id].protected == "absolute"


def test_old_promo_trashed_recent_promo_kept(cfg):
    old = make(sender="deals@shop.example", subject="40% off", labels=[CATEGORY_PROMOTIONS], bulk=True, days_ago=400)
    new = make(sender="deals@shop.example", subject="40% off", labels=[CATEGORY_PROMOTIONS], bulk=True, days_ago=3)
    d = decide([old, new], cfg)
    assert d[old.id].action == "trash"
    assert d[new.id].action == "keep"


def test_correspondents_protected_but_their_bulk_mail_is_not(cfg):
    sent = make(sender="me@example.com", subject="Hi", labels=[SENT], to=["friend@company.example"], days_ago=2000)
    personal = make(sender="friend@company.example", subject="Photos", days_ago=1500)
    bulk = make(sender="friend@company.example", subject="Our newsletter", bulk=True, days_ago=1500)
    d = decide([sent, personal, bulk], cfg)
    assert d[personal.id].action == "keep"
    assert d[bulk.id].action == "trash"


def test_personal_address_people_are_kept(cfg):
    m = make(sender="grandpa.joe@aol.com", name="Grandpa Joe", subject="Old photos", days_ago=3000)
    assert decide([m], cfg)[m.id].action == "keep"


def test_protected_domain_keeps_people_not_marketing(cfg):
    cfg.protect_domains = ["employer.example"]
    person = make(sender="hr@employer.example", subject="Your T4", days_ago=3000)
    colleague = make(sender="dana@employer.example", subject="Lunch", days_ago=3000)
    marketing = make(sender="news@employer.example", subject="Insights #4", bulk=True, days_ago=300)
    d = decide([person, colleague, marketing], cfg)
    assert d[person.id].action == "keep"
    assert d[colleague.id].action == "keep"
    assert d[marketing.id].action == "trash"


def test_never_protect_overrides_soft_protection(cfg):
    cfg.protect_domains = ["employer.example"]
    cfg.never_protect = ["info@employer.example"]
    m = make(sender="info@employer.example", subject="Company update", days_ago=500, auto_submitted="auto-generated")
    assert decide([m], cfg)[m.id].action == "trash"


def test_protected_people_names_and_starred(cfg):
    cfg.protect_people = ["mom@family.example", "Okonkwo"]
    a = make(sender="mom@family.example", subject="40% off", labels=[CATEGORY_PROMOTIONS], bulk=True, days_ago=900)
    b = make(sender="c.okonkwo@bank.example", name="C Okonkwo", subject="Funds transferred", days_ago=4000)
    c = make(sender="deals@shop.example", subject="code", labels=[STARRED, CATEGORY_PROMOTIONS], bulk=True, days_ago=900)
    d = decide([a, b, c], cfg)
    assert all(d[m.id].action == "keep" for m in (a, b, c))


def test_retention_windows(cfg):
    recent_payment = make(sender="notify@payments.example", subject="e-Transfer: Sam sent you $20", days_ago=500)
    old_payment = make(sender="notify@payments.example", subject="e-Transfer: Sam sent you $20", days_ago=1500)
    tax = make(sender="noreply@tax.gov.example", subject="Your 2009 Notice of Assessment", days_ago=6000)
    d = decide([recent_payment, old_payment, tax], cfg)
    assert d[recent_payment.id].action == "keep"
    assert d[old_payment.id].action == "trash"
    assert d[tax.id].action == "keep"


def test_review_overrides(cfg):
    keepme = make(sender="digest@news.example", subject="Weekly", bulk=True, list_id="<x>", days_ago=200)
    trashme = make(sender="old.colleague@corp.example", subject="hey", days_ago=200)
    replied = make(sender="old.colleague@corp.example", subject="question", thread="R", days_ago=300)
    mine = make(sender="me@example.com", subject="Re: question", thread="R", labels=[SENT], days_ago=299,
                to=["someone.else@corp.example"])
    d = decide([keepme, trashme, replied, mine], cfg,
               overrides={"digest@news.example": "keep", "old.colleague@corp.example": "trash"})
    assert d[keepme.id].action == "keep"
    assert d[trashme.id].action == "trash"
    assert d[replied.id].action == "keep"  # replied threads beat a trash override


def test_domain_override_and_recent_override(cfg):
    m = make(sender="e.news@updates.uni.example", subject="Alumni minute", bulk=True, days_ago=5)
    assert decide([m], cfg)[m.id].action == "keep"  # recent working set
    assert decide([m], cfg, overrides={"@updates.uni.example": "trash"})[m.id].action == "trash"


def test_ai_verdict_applies_only_to_ambiguous(cfg):
    stranger = make(sender="pat@vendor.example", subject="Following up", days_ago=900)
    promo = make(sender="deals@shop.example", subject="Sale", labels=[CATEGORY_PROMOTIONS], bulk=True, days_ago=900)
    ai = {"pat@vendor.example": {"action": "trash", "reason": "cold outreach"},
          "deals@shop.example": {"action": "keep", "reason": "x"}}
    d = decide([stranger, promo], cfg, ai=ai)
    assert d[stranger.id].action == "trash" and d[stranger.id].reason.startswith("AI")
    assert d[promo.id].action == "trash"  # promotions aren't an AI category


def test_summary_counts(cfg):
    msgs = [make(sender="deals@shop.example", subject="Sale", labels=[CATEGORY_PROMOTIONS], bulk=True, days_ago=900)
            for _ in range(3)]
    s = summarize(msgs, classify(msgs, cfg, now=NOW))
    assert s["trash_threads"] == 3 and s["trash_by_category"] == {"promotion": 3}
