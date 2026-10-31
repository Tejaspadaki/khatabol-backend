# Market Trust Score: Design (draft for approval)

**Status:** superseded on 2026-10-31. The owner chose plan.md §22 (Trust Score + UPI Udhaar Check) for Feature 4, and plan.md keeps this cross-merchant score as future work (§22 Level 5). Kept for reference only; nothing here is built.
**Feature:** BUILD_CONTEXT.md §11.4, Feature 4.

> **Compliance flag.** This feature shares information about customers' payment behaviour between different shops. It needs a review by a lawyer or compliance owner before launch, at least against India's Digital Personal Data Protection Act, 2023 (consent, purpose limitation, retention, the right to withdraw and to erasure) and against any rules on sharing credit information. This document is an engineering design. It is not legal advice, and none of its choices should be read as a legal opinion.

---

## 1. What it does, and what it never does

A shop can see a simple grade, **A, B or C**, for one of its own customers. The grade says how reliably that customer has repaid udhaar at *other* shops in the same market. It appears only if:

- the shop has opted in,
- the customer has consented,
- at least 3 other shops have contributed data about that customer.

Otherwise the shop sees "insufficient data".

**It never:**
- places a hold, blocks udhaar, or changes a credit limit. Scores are read-only. No code in the ledger, hold, or limit paths reads them (§9).
- shares a name, phone number, UPI ID, amount, item, date, or note with another shop.
- is offered to the voice agent as a tool, so the LLM cannot use it to make or suggest a decision (§9).
- shows a grade for a customer the shop does not already have in its own khata.

---

## 2. Definitions

| Term | Meaning |
|---|---|
| **Market** | Shops that share the same `market_code`. Proposal: the shop's 6-digit PIN code, set by the shop when it opts in. |
| **Contribution** | One row per (shop, customer pseudonym, month): how many udhaar entries were repaid on time and how many late. |
| **Pseudonym** | A keyed hash of the customer's phone number (§4). The same customer gets the same pseudonym at every shop in the market. Without the server's key, it can't be turned back into a phone number. |
| **On time** | An udhaar entry with a due date, cleared in full (oldest-first allocation, as today) on or before its due date. |
| **Late** | An udhaar entry with a due date, cleared after its due date, or still unpaid when its month is counted. |

Udhaar without a due date and reversed entries are never counted.

---

## 3. Consent

Both sides opt in, and either can withdraw at any time.

### 3.1 Shop (merchant) opt-in

Set in the app's settings screen, not by voice, so the wording is read on screen. The opt-in is recorded with a time, and the shop chooses its `market_code`.

Proposed wording (English, with a Hinglish version alongside; the final text is for legal review):

> **Share repayment grades with nearby shops?**
> If you turn this on, Khata Voice will share, for customers who agree:
> - how many of their udhaar entries were paid on time or late,
> - under a code instead of their name or phone number.
>
> Other shops in your PIN code who also take part will see only a grade (A, B or C) for customers they already have. You will see the same for yours.
>
> No names, phone numbers, UPI IDs or amounts are shared. Grades never block or limit anyone's udhaar automatically. Data is kept for 12 months. You can turn this off at any time, and your data stops counting immediately.

### 3.2 Customer consent

The customer must agree themselves. The shop can't do it for them.

- **Proposal:** the shop asks for consent from the customer screen. The customer gets a WhatsApp/SMS message with the wording and a one-time link (valid for 7 days). Tapping "I agree" records the consent with `actor = customer`.
- In this prototype, messaging is mocked. The link opens a simple consent page served by the backend.
- **Rejected alternative:** the merchant records "customer said yes" by voice. It's faster at the counter, but the customer never sees the wording and there's nothing to prove they agreed. Not recommended.

Proposed customer wording (Hinglish, with an English version alongside; the final text is for legal review):

> **{Shop} aapse poochh raha hai:** kya aap chahte hain ki aapka udhaar *time par chukane ka record* aas-paas ki dukaanon ke saath share ho?
> - Sirf yeh share hoga: kitni baar time par diya, kitni baar der se. Aapke naam ya phone number ki jagah ek code ke saath.
> - Aapka naam, phone, UPI ID ya rakam kisi aur dukaan ko nahi dikhegi.
> - Doosri dukaanein sirf ek grade (A, B, C) dekh sakti hain, aur sirf tab jab aap unke bhi customer hon.
> - Yeh grade apne aap kisi ka udhaar band nahi karta.
> - Data 12 mahine rakha jaata hai. Aap kabhi bhi mana kar sakte hain: "{Shop}" ko boliye, ya is link par "Withdraw" dabaiye.
>
> [Main sehmat hoon] [Nahi]

### 3.3 Withdrawal

- **Customer withdraws** (by link, or through the shop by voice with `withdraw_market_consent(customer_id)`, which goes through the "haan" gate like every write):
  - `market_consents.withdrawn_at` is set.
  - A `MARKET_CONSENT_WITHDRAWN` event is appended to `customer_events`, with the actor.
  - From that moment, the shop's contributions about this customer no longer count in any score.
  - The shop no longer sees a grade for them.
- **Shop withdraws:** none of its contributions count any more, it sees no grades, and the withdrawal is logged.
- **Deletion:** contributions are append-only, so withdrawal first *excludes* them from scores. They are then deleted at the next nightly purge. A deletion is the only change ever made to these rows, and it is logged as a count per shop, not per customer. Whether deletion must happen immediately, rather than at the next purge, is a question for legal review.

---

## 4. Pseudonym scheme and key management

```
pseudonym_id = "v{key_version}:" + base32( HMAC-SHA256( K[key_version], "khata-market|" + market_code + "|" + phone_e164 ) )[:26]
```

- **Phone normalization.** The phone is normalized to E.164 (`+91XXXXXXXXXX`) first. Customers without a valid phone number are never contributed or scored. The phone is the only identifier two shops reliably share: names vary too much, and many customers pay in cash with no UPI ID.
- **Why a keyed hash and not a plain hash.** There are only about 10¹⁰ Indian mobile numbers, so anyone could brute-force a plain or salted hash of a phone in minutes. HMAC with a secret key means a leak of the market tables alone reveals nothing.
- **Why one key per market, not a salt per shop.** Every shop in a market must compute the same pseudonym for the same customer, so a per-shop salt cannot work. The `market_code` inside the HMAC means the same person gets unrelated pseudonyms in different markets.
- **Where the key lives.** `MARKET_PSEUDONYM_KEYS` holds versioned 32-byte random keys, in the server's secret store (an environment variable in the prototype). It is never stored in the database, logged, or sent to the app, and only the backend's market module reads it.
- **Rotation.** A new key version every 12 months, matching the retention period.
  - New contributions use the new version.
  - Rows under the old version age out through retention and are deleted with it.
  - Old keys are destroyed once their last row is purged.
  - Rotation also stops anyone linking a customer across years.
- **If the key leaks:** generate a new version, re-contribute the last 12 months from each shop's own ledger under it, and delete every row under the leaked version. The shops' ledgers are the source of truth, so nothing is lost.

**Prototype honesty:** in this build, the market tables sit in the same database as the shops' ledgers. Separation is logical: the market module stores only pseudonyms and counts, and nothing joins market tables to customers. In production the market store should be a separate service and database, receiving only `(market_code, pseudonym_id, period, counts)` from each shop.

---

## 5. Data model

The tables approved in BUILD_CONTEXT §4.1, plus three proposed additions marked **(new)** that need approval:

```
market_contributions (append-only)
  merchant_id, period ('2026-10'), pseudonym_id, on_time_count, late_count, created_at
  UNIQUE (merchant_id, period, pseudonym_id)          (new: makes the monthly job safe to re-run)

market_consents
  merchant_id, customer_id, consented_at, withdrawn_at NULL

merchants
  market_code TEXT NULL        (new: the shop's market; NULL = not taking part)
  market_opt_in_at, market_withdrawn_at TIMESTAMP NULL   (new: shop-level consent)
```

Shop-level consent could instead be stored as a `market_consents` row with `customer_id NULL`. I recommend separate columns, so that a missing customer can never be mistaken for shop-wide consent.

`merchant_id` stays on contributions so that withdrawals can be applied and distinct shops counted. It is never shown to other shops.

---

## 6. Contribution job (monthly)

On the 1st of each month, for each opted-in shop, and for each of its customers with active consent and a valid phone number:

1. Take that customer's udhaar entries with a due date in the previous month.
2. Replay the ledger oldest-first, the same FIFO rule as today, to find when each entry was fully cleared.
3. Count each entry as on time (cleared ≤ due date) or late (cleared later, or still open).
4. Insert one row if the counts are not both zero. The unique key makes a re-run a no-op.

Only counts leave the computation. Amounts are used inside the shop's own ledger to decide "cleared", but are never stored in market tables.

---

## 7. Computing the grade

When a shop opens one of its own customers who has active consent (no lookup by phone, so a shop can't probe unknown numbers):

1. Compute the customer's pseudonym.
2. Take contributions for that pseudonym from the last 12 months, from **other** shops only, and only those whose shop and customer consent are both still active.
3. If fewer than **3 distinct other shops** contributed, or there are fewer than **6 counted entries** in total, return `insufficient_data`.
4. Otherwise compute on-time share `r = on_time / (on_time + late)`:
   - **A** if r ≥ 0.90,
   - **B** if 0.70 ≤ r < 0.90,
   - **C** if r < 0.70.

The shop's own data is left out because it already knows its own history. Leaving it out also means the grade reflects only what others saw.

**Reading of the threshold rule.** BUILD_CONTEXT says "at least 3 other merchants in the market have contributed". This design reads that as 3 other shops that contributed data *about this customer*, not 3 shops anywhere in the market. With fewer than 3 per customer, a grade could reveal what one specific neighbour experienced. Please confirm.

---

## 8. Retention

- Grades use the last 12 months only.
- A nightly purge deletes contributions older than 12 months, and contributions whose shop or customer consent was withdrawn (§3.3).
- Consent records are kept for the life of the shop's account plus 12 months, as proof of consent, and then deleted.
- The 12-month period and the consent-record period need legal review.

---

## 9. How "read-only" is enforced in code

- All of this lives in one module, `app/market/`. Nothing in `app/ledger`, the hold job, the limit check, or the agent tools imports it. A test fails the build if any of them ever does.
- Tests check that setting up a C grade for a customer changes nothing for them: no hold, no limit change, and an udhaar proposal is the same as without the grade.
- The grade is not in `TOOL_SCHEMAS`, so the LLM never sees it. It only appears as a badge on the customer screen in the app.
- `withdraw_market_consent` is the only market tool the agent has, and it goes through the confirmation gate.

---

## 10. Risks and limits (for the reviewer)

| Risk | Mitigation in this design | Remaining gap |
|---|---|---|
| Re-identification in small markets | Per-customer threshold of 3 other shops, at least 6 entries, only three grades | A very small PIN code could still narrow down who a customer is |
| Fake shops created to reach the threshold | Only verified Paytm merchants count (production) | The prototype has no merchant verification |
| A shop probing phone numbers | Grades only for the shop's own consented customers; no lookup by phone | A shop could add a customer just to look them up; audit logging of views is proposed |
| Unfair grades (disputes, family emergencies) | Grades never act automatically; the shop decides | No dispute process for the customer yet; needs a product and legal decision |
| Shared family phone | None | Two people sharing one phone share one grade |
| Phone number changes | The new number starts with no history | History doesn't follow the person; acceptable for privacy |
| Key leak | Versioned keys, re-contribution, purge (§4) | Depends on how the secret store is run |

---

## 11. Decisions needed from you

1. **Market = 6-digit PIN code**, chosen by the shop? Or something else, such as a Paytm-defined area?
2. **Customer consent through a link the customer taps** (recommended), instead of the merchant recording it by voice?
3. **Threshold read as 3 other shops per customer** (§7), plus at least 6 counted entries?
4. **Grade cut-offs:** A ≥ 90%, B 70–90%, C < 70% on time?
5. **Retention: 12 months**, with yearly key rotation and the nightly purge?
6. **The three new columns and the unique key** in §5?
7. **Consent wording** in §3.1 and §3.2, as a draft for legal review?
8. **Legal or compliance review** before launch: who does it? Should the feature stay behind a flag (`ENABLE_MARKET_SCORE=false`) until it's done?

---

## 12. Build plan once approved

Same steps as Features 1–3:

- migration `0005` (the tables, plus the new columns if approved),
- tests written first for the rules above,
- the `app/market/` module, the monthly and nightly jobs, the consent page, and `withdraw_market_consent`,
- a bucket badge on the customer screen,
- the full suite kept green.

The feature ships turned off by default (`ENABLE_MARKET_SCORE=false`).
