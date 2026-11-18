# How inbox-sweep decides

Every message gets exactly one decision, `keep` or `trash`, with a reason you
can read on the review page and in `plan.json`. Rules are evaluated in this
order. The first that applies wins.

## 1. Absolute protection (nothing overrides these)

- Mail you sent, and **every thread you replied in**. Your replies are the
  strongest signal there is.
- Starred mail.
- Mail in any label listed in `[protect] labels`.
- Mail from or to an address in `[protect] people`.

## 2. Your review decisions

On the review page each sender can be "Keep all" or "Trash all". Keys can be an
address (`news@shop.com`) or a whole domain (`@updates.university.edu`). "Trash
all" still can't touch anything in step 1.

## 3. Soft protection

Skipped for senders listed in `[never_protect] senders`.

- A display name containing a protected name (`[protect] people` entries without `@`).
- **Someone you've written to** (any address on the To/Cc of mail you sent), unless the message is bulk mail.
- A **protected domain** (`[protect] domains`), unless the message is bulk mail.
  This is how an employer's or school's people stay protected while its
  newsletters go.
- **A person on a personal address**: a webmail or ISP domain, and nothing about
  the message looks machine-sent.
- A protected keyword in the subject (`[protect] keywords`), unless bulk.
- Anything from the last `recent_days` (30). A review "Trash all" can override this.

"Bulk mail" means a `List-Unsubscribe` or `List-Id` header, `Precedence: bulk/list`,
or `Auto-Submitted` other than `no`.

## 4. AI verdict (optional)

With `classify --ai`, senders whose remaining mail is `other_automated` or
`human_unknown` go to a language model in groups, with the 20-year test as the
brief. By default that's a **local** model (Ollama, LM Studio, llama.cpp or vLLM
on this machine); hosted providers are opt-in (`[ai] provider`). Each group
carries name, address, count, date range and up to six subjects. Verdicts are
cached in the index. Promotions, newsletters and the rest never need the AI: the
rules already handle them, and the whole tool works with the AI step off.

## 5. Retention by category

`categories.py` assigns each message one category from its subject, sender and
headers. Promotions can never claim the documents category. `sweep.toml
[retention]` says how many days each category is worth keeping (`-1` forever,
`0` trash now).

## 6. Threads stay whole

If any message in a thread is kept, the whole thread is kept. A plan therefore
never splits a conversation.

## The plan check

`plan` and `apply` both re-derive step 1 from the index and refuse any plan that
would trash absolutely protected mail, or a thread that also has kept messages.
This guards against bugs, edited plans and stale indexes.
