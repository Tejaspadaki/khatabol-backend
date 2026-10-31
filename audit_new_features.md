# Audit: new Khata Bol features against `docs/BUILD_CONTEXT.md`

- **Audited on:** 2026-10-31.
- **Scope:** the four features in BUILD_CONTEXT §11 "Features in progress", and the existing code they touch. The audit prompt calls this section 13, but in the spec it is §11 (with §4.1 for the data model and §6.1 for the tools). No code was changed.
- **Code audited:** `khatabol-backend/` (FastAPI) and `Khatabol-frontend/src/` (Expo).
- **Frontend:** §7 is a full frontend map and gap list for these features, with the files to change. Use it as the developer guide.
- **Code that changed during the audit** (a per-shop Trust Score and payment allocations) is listed in §8. It is outside these four features and was not audited in full.
- **Line numbers:** in §2–§6 they refer to the code at the first run. `app/agent/tools.py` and `app/agent/pending.py` have been edited since, so their line numbers may have moved by a few dozen lines; search for the function names given. §7 uses the current frontend code.

---

## 1. Summary

| Feature | Status | Rules: Done / Partial / Missing |
|---|---|---|
| 1. Credit limit by voice, with near-limit warning | **Done** (V1 fixed during the audit) | 8 / 0 / 0 |
| 2. Hold for overdue customers | **Done** | 7 / 0 / 0 |
| 3. Partial-payment nudge | **Done** | 6 / 0 / 0 |
| 4. Market trust score | **Missing** (gated correctly: design not approved, and no code written) | 0 / 0 / 1 |
| **Total** | | **21 / 0 / 1** |

**Headline findings:**
- **One rule was broken (Feature 1) and is now fixed (V1).** It was fixed by edits made during the audit, and re-verified: the over-limit question is now asked as its own extra step. Originally, an over-limit udhaar could be saved without a separate confirmation that named the overage, when it was waiting alongside another action that needed a second "haan".
- **One write path skips the hold and limit checks:** undoing a *payment*. It raised a held customer's balance (on the latest code: ₹1,000 → ₹1,500 while on hold). The spec doesn't say whether this counts as "new udhaar", so it needs an owner decision (§3, V2).
- **Feature 4 is compliant with its gate.**
  - `docs/MARKET_TRUST_DESIGN.md` exists and is marked "draft, waiting for the owner's approval".
  - BUILD_CONTEXT §11.4 says "No code yet".
  - No market code, tables, tools, or UI exist anywhere.
  - I found no evidence that the design was approved.
- **Tests:**
  - The feature tests pass: 54 passed at the first run, and 57 on the re-run after other people's edits; 0 failed, 0 skipped.
  - The full backend suite passes: 253 at the first run, 289 on the latest re-run.
  - One re-run, made while files were being edited, showed temporary failures. The next run was clean.
  - The frontend typechecks (`tsc --noEmit`, exit 0) and lints (`expo lint`, exit 0) cleanly. There are no frontend tests.
- **Frontend:** all three built features have their UI (limit card, hold badge, nudge button). There are 12 smaller frontend gaps, and none is a safety problem: the backend enforces every rule. The biggest are: no way to set `hold_grace_days` in the app (FE1); the second-confirmation header guesses which question is being asked (FE3, related to V1); and the nudge button disappears even when the nudge wasn't sent (FE7). See §7.
- **Migrations:** each feature is a new Alembic revision (0002, 0003, 0004), and 0001 is unchanged. A fresh database upgrades to head `0004`.

---

## 2. Status table

### Feature 1: Credit limit by voice (§11.1)

| Rule | Status | Evidence | What is missing |
|---|---|---|---|
| A limit set by voice is proposed, and written only after confirmation | Done | `set_credit_limit` is in `WRITE_TOOLS` (`app/agent/tools.py:100-103`). `_set_credit_limit` only calls `_propose` (`tools.py:498-514`). The commit happens in `_commit_one` → `ledger.set_credit_limit` (`app/agent/pending.py:101-105`). | — |
| Udhaar over the limit is blocked in the write path, not only in the agent | Done | `ledger.add_credit` locks the customer row and calls `_check_limit` (`app/ledger/service.py:311-314`, `191-202`). The tool layer pre-checks in `_limit_check` (`tools.py:409-428`). Arguments the model invents are dropped (`tools.py:621`). The REST udhaar endpoint uses the same service (`app/api/customers.py:77`). | — |
| An override needs a separate confirmation that names the overage | Done (fixed during the audit; was Partial) | It works when the over-limit udhaar is the only action waiting: one more confirmation is added (`tools.py:406`), and `_second_prompt` names the overage (`pending.py:121-126`). The service allows only the approved overage (`service.py:199`) and logs a `CREDIT_LIMIT_OVERRIDE` event (`service.py:329-331`). | `confirm()` only prompts for `waiting[0]` (`pending.py:140`). If another action that needs a second "haan" comes first, the overage is never asked about on its own. See V1. |
| A warning appears at 80% of the limit | Done | `NEAR_LIMIT_PCT = 80`, and the check is `projected*100 >= 80*limit` (`service.py:39`, `187`). The warning is added to the card summary (`tools.py:399-400`) and to `credit_warning` on the pending card (`app/agent/orchestrator.py:256`). The UI shows it in `CreditWarningLine` (`Khatabol-frontend/src/app/(tabs)/bolo.tsx:365`). | — |
| A zero limit blocks all new udhaar | Done | The tool refuses at proposal time (`tools.py:418-419`). The service refuses even when an override is set (`service.py:196-197`). | — |
| Negative limits are rejected | Done | Both the tool (`tools.py:506-507`) and the service (`service.py:164-165`) reject them. | — |
| Each limit change appends a `customer_events` row | Done | `set_credit_limit` → `record_event("CREDIT_LIMIT_SET", {old, new})` (`service.py:158-172`). | — |
| Frontend: credit-limit card (limit, balance, percent used) | Done | `CreditLimitCard` (`Khatabol-frontend/src/app/customer/[id].tsx:163-191`), fed by `credit_card()` (`app/api/customers.py` → `tools.py:569-576`). | — |

### Feature 2: Hold for overdue customers (§11.2)

| Rule | Status | Evidence | What is missing |
|---|---|---|---|
| The daily job holds customers overdue beyond `hold_grace_days`, and is idempotent | Done | `apply_overdue_holds` (`service.py:254-276`) uses the FIFO open credits and `overdue_beyond_grace` (`service.py:207-209`; no due date means no hold). It runs from `daily_holds` (`app/scheduler/jobs.py:32-40`) at `HOLD_HOUR` (`jobs.py:79`, `app/config.py:47`). It is idempotent because the query skips customers already on hold (`service.py:256`), and `set_hold` re-checks under a row lock (`service.py:226-228`). | Not concurrency-safe on SQLite, which has no `FOR UPDATE`. Two overlapping runs would make the second one raise `err_already_on_hold` and stop that merchant's loop. This is low risk. |
| A hold blocks new udhaar only. Payments, reminders and history still work | Done | Only `add_credit` checks `on_hold` (`service.py:312`, `tools.py:379`). `record_payment`, `send_reminders`, `get_history` and `list_dues` never read it. | — |
| Manual hold and lift by voice, both confirmed. A lift names the overdue amount | Done | `_set_hold` proposes only (`tools.py:517-535`). A lift adds `sum_hold_lift_overdue` with the overdue amount (`tools.py:532-534`). The commit is in `pending.py:107-111`. | — |
| Holds are `customer_events` with reason and actor. Nothing is deleted or hidden | Done | `set_hold` → `record_event("HOLD_PLACED"/"HOLD_LIFTED", {reason, overdue_paise, ...}, actor)` (`service.py:217-240`). `actor` is `system` from the job (`service.py:274`) or `merchant` from voice (`pending.py:109`). Append-only is enforced by the `before_flush` guard (`app/db/models.py:149-155`). | No API or UI lists past hold events. Only the current `on_hold`/`hold_reason` is shown. Nothing is hidden in the database, but the trail is not viewable in the product. The append-only guard is ORM-only (see §5). |
| The udhaar path checks the hold itself and returns a speakable refusal | Done | `hold_refusal()` → `err_on_hold` in hi/mr/en ("…naya udhaar nahi likh sakte. Payment le sakte hain.") (`service.py:212-214`, `app/i18n.py:180`). It is raised in the tool (`tools.py:380`) and in the service (`service.py:313`). | — |
| Open point: a merchant lift is respected until a *new* entry goes overdue | Done (as proposed in the spec) | The lift stores `overdue_entry_ids` (`service.py:234-237`). The job excuses those entries (`service.py:265-268`). | — |
| Frontend: hold badge on the customer list and customer screen | Done | `CustomerRow.tsx:34` and `customer/[id].tsx:82-90`. The udhaar button is disabled while on hold (`[id].tsx:98`). | — |

### Feature 3: Partial-payment nudge (§11.3)

| Rule | Status | Evidence | What is missing |
|---|---|---|---|
| The read-back after a payment shows the amount paid and the amount due | Done | `did_payment` uses `paid` and `balance` (`pending.py:63-64`). The nudge is offered (`pending.py:59-62`, `159-161`). | — |
| The reminder is sent only after the merchant confirms | Done | `send_partial_nudge` is in `WRITE_TOOLS`. The tool only proposes (`tools.py:552-566`). The app's button goes through `POST /agent/propose`, which also only proposes; it is limited to `DIRECT_TOOLS` and to `Literal["send_partial_nudge"]` (`orchestrator.py:91-122`, `app/api/schemas.py`). The send happens in `_commit_one` (`pending.py:113-116`). | — |
| The amount is read from `get_balance` at send time | Done | **`app/scheduler/reminders.py:74`**: `b = await ledger.get_balance(...)`, and the reminder uses `b["balance_paise"]` (`reminders.py:79`). The pending args store no amount, only `customer_id` and `payment_id` (`tools.py:566`). | — |
| One nudge per payment per day, enforced by a database unique key | Done | `UNIQUE (source_payment_id, nudge_date)` is in the migration (`0004_partial_payment_nudge_key.py:23`) and the model (`models.py:258`). I confirmed it exists in a fresh database. An `IntegrityError` becomes `err_nudge_sent_today` (`reminders.py:77-82`). The pre-check in `nudge_sent` (`tools.py:560`) is only there to give a friendlier error. | — |
| It reuses the `reminders` module and its template rules | Done | `send_payment_nudge` → `send_reminders` → `reminder_message` (`reminders.py:21-59`, `67-83`). Consent: §11.3 records the decision that the module has no consent rules and Feature 3 doesn't add any. I treat this as compliant. | The audit prompt mentions "consent rules", but none exist, by recorded decision. A customer without a phone still uses up the day's nudge: the reminder is stored as `NO_PHONE` (`reminders.py:52`), but the merchant hears "reminder bhej diya" (`pending.py:116`). See V4. |
| Frontend: nudge button on the payment confirmation card | Done | `bolo.tsx:338-349` and `nudge()` → `api.agentPropose` (`bolo.tsx:111-118`). | — |

### Feature 4: Market trust score (§11.4)

| Rule | Status | Evidence | What is missing |
|---|---|---|---|
| A written design exists and was approved before any code | **Missing (approval pending)** | `docs/MARKET_TRUST_DESIGN.md` says "Status: draft, waiting for the owner's approval". §11 lists 8 open decisions. BUILD_CONTEXT §11.4 says "waiting for owner approval. No code yet." Approval: **not found**. | Owner decisions 1–8 in `MARKET_TRUST_DESIGN.md` §11, and a legal or compliance review owner. |
| No Feature 4 code before approval | Done (compliant) | A case-insensitive search for `market`, `pseudonym` and `consent` across `app/`, `tests/` and `Khatabol-frontend/src/` finds nothing. There is no `app/market/`, no `market_contributions` or `market_consents` model or migration, no `withdraw_market_consent` tool (the tool count is 18 = 14 + 4, which matches §6.1), and no bucket badge. | — |
| Pseudonym, no PII, salt handling, ≥3 contributors, withdrawal, read-only score, per-customer consent | Not applicable | No code exists to check. The design covers each of these (§3–§9 of the design doc). | Check once it is built. |

---

## 3. Violations

| # | Severity | File:line | Rule broken | Detail |
|---|---|---|---|---|
| V1 | **High, now fixed** (re-verified: the 3rd turn asks "Ye udhaar limit ₹2,000 se ₹300 zyada hai… Phir bhi save karun?") | `app/agent/pending.py:137-142` (`waiting[0]`) together with `pending.py:121-126` | §11.1: "An override needs a separate confirmation that names the overage". Also §11.1 "Must never… write an over-limit udhaar without confirmation" (of the overage). | Reproduced with the scripted fake LLM. The steps were: big limit ₹1,000; Sunita pays ₹2,000 (needs a second "haan") and Ramesh gets ₹800 udhaar that is ₹300 over his ₹2,000 limit (also needs a second "haan"). The second prompt was *"Bada amount hai: Sunita Devi se ₹2,000 cash mile. Pakka save karun?"*, with no mention of the overage. The next "haan" committed both, and Ramesh ended at ₹2,300. The overage was spoken only inside the first combined read-back. The app does show "Over the credit limit, please confirm again" and the overage line on the card (`bolo.tsx:319-325`), so voice-only use is the affected path. |
| V2 | Medium (needs an owner decision) | `app/ledger/service.py:370-396` (`reverse_entry`); callers `pending.py:84-92` and `app/api/customers.py:104-106` | §11.1 and §11.2, if undoing a payment counts as "new udhaar" | Undoing a PAYMENT raises the balance without checking `on_hold` or `credit_limit_paise`. Reproduced: a held customer with a ₹2,000 limit went from ₹1,800 to ₹2,300. The spec only talks about udhaar, so this may be intended (it is a correction, not new credit), but it is a way past both checks. |
| V3 | Low | `app/db/models.py:149-155` | §4.1 / top rules: `customer_events` is append-only | Append-only is enforced only by an ORM `before_flush` hook. A Core `update(CustomerEvent)` or `delete(CustomerEvent)`, raw SQL, or a manual database edit would get past it. There is no database trigger and no restricted grant; I checked a fresh database and found no triggers. The code has **no** `UPDATE` or `DELETE` on `customer_events`. The only `session.delete(ev)` is in `tests/test_credit_limit.py:70`, which deliberately tests the guard. |
| V4 | Low | `app/scheduler/reminders.py:52` together with `app/agent/pending.py:116` | §11.3 is not broken, but this is misleading. | A nudge to a customer with no phone is stored as `NO_PHONE`, uses up the day's unique slot, and is reported to the merchant as sent. |

**Step 6 safety searches. Each one lists every hit:**
- **A ledger write without a confirmation record.**
  - The agent writes all go through `pending.confirm` → `_commit_one`.
  - Hits outside the gate:
    - the REST manual writes `POST /customers/{c}/credit`, `/payment`, `/upi` and `POST /entries/{e}/reverse` (`app/api/customers.py:68-106`),
    - `app/demo/seed.py:31-41`,
    - the UPI reconciler (payments only).
  - The REST writes are part of the documented design (BUILD_CONTEXT §7, `source=MANUAL`), and their udhaar path enforces the limit and hold. They predate these features and aren't counted as Feature 1–3 violations.
- **A udhaar write that skips the limit or hold check.** None. Every `CREDIT` entry is created only in `service.add_credit` (`service.py:316`), which checks both. See V2 for reversals.
- **A reminder sent without merchant confirmation.** For Feature 3, none. The existing scheduled `morning_reminders` (`jobs.py:43-57`) sends without a per-send confirmation; that predates these features and is out of scope.
- **A market score read by a write path.** None, because no market code exists.
- **Personal data in a market payload.** None, because no market code exists.

---

## 4. Test coverage

**Run:** `uv run pytest tests/test_credit_limit.py tests/test_holds.py tests/test_nudge.py tests/test_migrations.py` gave **54 passed, 0 failed, 0 skipped**:
- `test_credit_limit.py`: 22,
- `test_holds.py`: 18,
- `test_nudge.py`: 12,
- `test_migrations.py`: 2.

The full suite gave 253 passed. No test is skipped or marked xfail. Where tests use fakes, they fake only the boundaries: `FakeLLM` stands in for Groq, and `test_scheduled_job_holds_and_announces` monkeypatches the event bus. **No rule passes because its enforcement point is mocked.** The limit, hold and unique-key checks all run against a real SQLite database.

| Feature | Rule | Tests (file:line) | Gap |
|---|---|---|---|
| 1 | Proposed, then confirmed | `test_credit_limit.py:130` `test_set_limit_by_voice_writes_only_after_haan` | — |
| 1 | Blocked in the write path | `:75` `test_udhaar_over_limit_is_blocked`, `:83`, `:250` `test_agent_cannot_skip_the_limit_check`, `:270` `test_balance_grows_before_commit_then_commit_fails`, `:287` `test_manual_credit_over_limit_is_refused` | — |
| 1 | Override with a separate named confirmation | `:89` `test_override_allows_only_the_approved_overage`, `:190` `test_over_limit_needs_separate_confirmation_naming_overage`, `:210`, `:221` `test_over_limit_and_big_amount_need_three_yeses` | **Gap:** no test with an over-limit udhaar waiting alongside a *different* action that needs a second "haan" (V1). |
| 1 | 80% warning | `:173` `test_near_limit_warning_on_confirmation_card`, `:183` `test_below_80_percent_has_no_warning`, `:114` `test_credit_status_math`, `:261` `test_two_udhaars_in_one_turn_count_together` | — |
| 1 | Zero limit blocks | `:99` `test_zero_limit_blocks_all_udhaar`, `:235` `test_zero_limit_refused_by_tool_not_agent` | — |
| 1 | Negative rejected | `:54` (service), `:147` (voice) | — |
| 1 | Event appended | `:40` `test_limit_change_is_appended_as_event`, `:62` `test_customer_events_are_append_only` (ORM path only) | **Gap:** no test that raw or Core SQL can't change events (V3). |
| 1 | `get_credit_status` | `:159` | — |
| 1 | Frontend card | none | **Gap:** no frontend tests in the repo (typecheck only). |
| 2 | Daily job and idempotency | `test_holds.py:37`, `:49` boundary, `:57` merchant grace, `:63` no due date, `:71` paid overdue, `:78` `test_job_is_idempotent`, `:244` scheduled job | No test for concurrent or overlapping runs. |
| 2 | Blocks udhaar only | `:100` `test_hold_blocks_udhaar_only` (payment, history, reminder), `:185` payment by voice, `:213` REST | — |
| 2 | Hold and lift by voice, confirmed; lift names overdue | `:132`, `:146` `test_lift_while_overdue_names_the_overdue_amount`, `:158` | — |
| 2 | Events with reason and actor, nothing deleted | `:113` `test_merchant_hold_and_lift_are_events`, `:85` (event sequence) | — |
| 2 | Udhaar path refuses with speakable text | `:167` `test_udhaar_on_hold_refused_by_tool`, `:197` `get_balance` reports hold | **Gap:** no test for undoing a payment on a held or limited customer (V2). |
| 2 | Lift respected until a new overdue entry | `:85` | — |
| 2 | Grace days via PATCH | `:234` | — |
| 2 | Frontend badge | none | Gap, same as above. |
| 3 | Read-back paid and due | `test_nudge.py:35`, `:47`, `:55` | — |
| 3 | Send only after confirm | `:66` `test_nudge_sent_only_after_haan`, `:169` one-tap via `/agent/propose`, `:190` propose allows only the nudge | — |
| 3 | Amount read at send time | `:79` `test_amount_is_read_at_send_time`, `:90` | — |
| 3 | One per payment per day, database key | `:100` `test_one_nudge_per_payment_per_day` (calls `send_payment_nudge` directly, so the database constraint, not the pre-check, is what's tested) | — |
| 3 | Reuses module and template | `:66` (asserts template text and UPI link) | No test for the `NO_PHONE` case (V4). |
| 3 | Payment id must be a live payment of that customer | `:121`, `:130` | — |
| 4 | Any | none | Expected: nothing is built. |
| DB | Fresh database to head; models match | `test_migrations.py:23` (`test_fresh_database_upgrades_to_models`), `:33` (legacy database stamped, then upgraded) | — |

---

## 5. Database rules (Step 5)

- **Fresh database:** `alembic upgrade head` on an empty SQLite file reached version `0004`. `customers` has `credit_limit_paise BIGINT`, `on_hold BOOLEAN` and `hold_reason TEXT`. `merchants` has `hold_grace_days INTEGER`. `reminders` has `source_payment_id` and `nudge_date` with `UNIQUE (source_payment_id, nudge_date)`. Postgres was not tested (unverified; no Postgres instance was run).
- **New revisions, old ones untouched:** `0002` (revises 0001), `0003` (revises 0002), `0004` (revises 0003). The modification time of `0001_baseline_schema.py` (12:32:39) matches its `Create Date`, and the later revisions have later times. There is no git history, so "never edited" is inferred from file times plus `test_migrations.py` passing, not proved.
- **`UPDATE`/`DELETE` on `customer_events`, `market_contributions`, `market_consents`:** no hits in `app/`. The market tables don't exist. The only hit is the deliberate test at `tests/test_credit_limit.py:70`. See V3 for how weak the guard is.
- **Money is integer paise:** `credit_limit_paise` is `BigInteger` (`models.py:122`, `0002:38`). The override and limit event payloads store `*_paise` integers (`service.py:172`, `331`). The LLM boundary takes `limit_rupees` and converts it with `rupees_to_paise` (`tools.py:503`), as §6.1 describes. `hold_grace_days` is a count of days, not money.

---

## 6. Next steps (prioritised)

| # | Item | Files to touch | Size |
|---|---|---|---|
| 1 | **Feature 4: get the design approved before any code.** The owner answers the 8 decisions in `MARKET_TRUST_DESIGN.md` §11 and names who does the legal or compliance (DPDP) review. Record the approval (who and when) in BUILD_CONTEXT §11.4. Only then build migration `0005` and the rest. | `docs/MARKET_TRUST_DESIGN.md`, `docs/BUILD_CONTEXT.md` | S (doc) / L (feature) |
| 2 | **V1 is fixed. Add a regression test:** put a big payment and an over-limit udhaar in one turn, and check that a prompt naming the overage comes before anything is saved (the case I reproduced in V1). | `tests/test_credit_limit.py` | S |
| 3 | **Decide V2.** Should undoing a payment respect the hold and limit? If yes, check in `reverse_entry` when the reversal raises the balance (or require a named confirmation, as with an override). Either way, record the decision in §11.1 and §11.2 and add a test. | `app/ledger/service.py` (`reverse_entry`), `app/agent/tools.py` (`_undo_last`), `docs/BUILD_CONTEXT.md`, tests | S–M |
| 4 | **Harden append-only (V3).** Add a database trigger that blocks `UPDATE` and `DELETE` on `customer_events` (and later on `market_contributions`) as a new revision `0005`, or a Postgres `REVOKE`. Add a test that runs raw SQL against it. | new migration, `tests/test_migrations.py` | S |
| 5 | **V4.** Don't report a `NO_PHONE` nudge as sent. Either refuse at proposal time ("phone number nahi hai") or say so in the reply. | `app/agent/tools.py` (`_send_partial_nudge`), `app/agent/pending.py:116`, `app/i18n.py` | S |
| 6 | **Make hold events viewable.** Add a read endpoint for `customer_events`, or merge them into history, so the hold and limit trail is visible in the app, not only in the database. | `app/api/customers.py`, frontend customer screen | M |
| 7 | **Daily hold job robustness.** Catch errors per merchant (as `morning_reminders` does), so one failure doesn't skip the other shops, and tolerate `err_already_on_hold` from an overlapping run. | `app/scheduler/jobs.py:32-40` | S |
| 8 | **Frontend tests** for the credit-limit card, hold badge and nudge button (there are none today). | `Khatabol-frontend/` | M |
| 9 | **Frontend quick fixes:** FE1 (grace-days setting), FE3 (second-confirmation header), FE7 (nudge button state), FE9 (no nudge without a phone), FE12 (hold badge on Home). Details in §7.3. | see §7.3 | S each |
| 10 | **Frontend follow-ups:** FE4 (show the amount over the limit), FE5 (limit headroom in the udhaar sheet), FE6 (warn before undoing a payment), FE2 (`holds_placed` icon). | see §7.3 | S each |
| 11 | **Frontend once the backend exists:** FE10 (hold and limit history, after step 6) and FE8 (nudge after a manual payment). | see §7.3 | M |

---

## 7. Frontend (`Khatabol-frontend/src/`)

The frontend is an Expo Router app. Every feature rule is decided by the backend; the app only shows what the backend returns and sends the merchant's "haan". The UI never computes limits, holds or amounts, and it should stay that way.

**Checks run:**
- `npx tsc --noEmit`: exit 0.
- `npx expo lint`: exit 0.
- There are **no frontend tests**, and I did not run the app, so how it renders is unverified.

### 7.1 Where each feature lives

| Feature | Screen or component | Data it reads | Backend source of that data | i18n keys (en / hi / mr) |
|---|---|---|---|---|
| 1. Limit card | `CreditLimitCard` in `src/app/customer/[id].tsx:171-202` | `CustomerDetail.credit` (`CreditStatus`, `src/api/types.ts:76-81`) | `GET /customers/{c}` → `credit_card()` (`app/agent/tools.py:650-657`) | `creditLimit`, `limitOf`, `limitUsed`, `noLimit`, `limitZero`, `limitByVoice` (`src/lib/i18n.ts:216-221`, `426-431`, `633-638`) |
| 1. Near-limit and over-limit warning on the udhaar card | `CreditWarningLine` in `src/app/(tabs)/bolo.tsx:373-385`, used at `bolo.tsx:325` | `PendingAction.credit_warning` (`types.ts:83-85`, `161-168`) | `Orchestrator._card()` → `args.credit_warning` (`app/agent/orchestrator.py`) | `nearLimitWarn`, `overLimitWarn` (`i18n.ts:222-223`) |
| 1. Second-confirmation header | `bolo.tsx:318-321` | `msg.second` (`outcome === 'awaiting_second_confirm'`, `bolo.tsx:86`) and `credit_warning.kind` | `pending.confirm()` | `overLimitConfirm`, `bigConfirm` (`i18n.ts:107`, `224`) |
| 1. Setting a limit | No UI by design: voice only, through the "haan" gate. The card shows a hint (`limitByVoice`). | — | `set_credit_limit` tool | — |
| 2. Hold badge in the customer list and Dues list | `CustomerRow` (`src/components/CustomerRow.tsx:34`), used by `customers.tsx:71` and `dues.tsx:76` | `Customer.on_hold` (`types.ts:23-24`) | `customer_card()` (`tools.py`) | `onHold` (`i18n.ts:212`) |
| 2. Hold banner on the customer screen | `customer/[id].tsx:83-93`; the udhaar button is disabled at `[id].tsx:105-106` | `on_hold`, `hold_reason` | `GET /customers/{c}` | `holdReason`, `holdHelp` (`i18n.ts:213-214`) |
| 2. Hold or lift | No UI by design: voice only (`set_hold`). `holdHelp` tells the merchant what to say. | — | — | — |
| 2. Daily job announcement | `EventFeed` (`src/components/EventFeed.tsx`); WebSocket in `src/lib/AppContext.tsx:130-142`, which also refreshes data (`setDataVersion`) | `LiveEvent` (`types.ts:215-224`) | `bus.publish("holds_placed", …, customer_ids=…)` (`app/scheduler/jobs.py`) | the text comes from the backend |
| 3. Paid and still-due read-back | Bot bubble in `bolo.tsx:299-314` | `AgentTurn.reply` | `did_payment` (`pending.py`) | from the backend |
| 3. Nudge button | `done` card in `bolo.tsx:345-360`, handled by `nudge()` at `bolo.tsx:111-124` | `CommittedAction.nudge` (`NudgeOffer`, `types.ts:171-183`) | `pending._commit_one` for `record_payment` | `nudgeButton` (`i18n.ts:112`) |
| 3. Nudge send | `api.agentPropose` (`src/api/client.ts:190-192`) → a pending card → "Haan" → `api.agentMessage` with the **same `session_id`** | `AgentTurn` | `POST /agent/propose` → gate | `askSave`, `yesSave` |
| 4. Market bucket badge | **None. This is correct**, because the design isn't approved. | — | — | — |

### 7.2 How the UI flows connect to the backend

- **Voice or typed udhaar with a limit:**
  1. `agentMessage` / `agentVoice` returns `outcome: awaiting_confirmation` and `pending[].credit_warning`.
  2. The app shows a `pending` card with the warning line.
  3. The "Haan" button sends the text `haan` (`bolo.tsx:338-339`).
  4. If the backend needs another yes, it returns `awaiting_second_confirm`, and the card is shown again with `second: true`.
  5. When it returns `committed`, the app calls `invalidate()` (`bolo.tsx:89`), and every screen reloads, including the limit card.
- **Hold:** the app only reads `on_hold`. A refusal on the manual udhaar sheet comes back as an HTTP 400 in the shop's language and is shown with `notify` (`[id].tsx:231-233`).
- **Nudge:**
  1. A payment is committed, and `committed[].nudge` is filled in.
  2. The app shows the button. Tapping it calls `agentPropose`, which returns a pending card.
  3. "Haan" commits the nudge.
  4. Rule for developers: the "haan" **must** use the same `sessionId`, or the gate won't find the pending action.

### 7.3 Frontend gaps

None of these breaks a spec rule, because the backend enforces all of them. They are about usability, or about the UI matching the backend.

| # | Gap | Evidence | Fix | Size |
|---|---|---|---|---|
| FE1 | **No setting for `hold_grace_days`.** The backend supports it (`PATCH /api/merchants/{id}`, 0–365, `app/api/schemas.py:23`), and the type has it (`types.ts:12`), but `api.updateMerchant`'s body type leaves it out (`client.ts:128-137`), and the shop settings form only has the threshold (`src/app/setup.tsx:182-184`). | `client.ts:128-137`, `setup.tsx:22`, `71-80`, `182-184` | Add `hold_grace_days?: number` to `updateMerchant`, a numeric `Field` in `setup.tsx` (whole number from 0 to 365, with validation like `thresholdInvalid`), and i18n keys such as `graceDays` and `graceDaysHint` in en, hi and mr. | S |
| FE2 | The `holds_placed` live event gets the generic bell icon, and `LiveEvent` has no `customer_ids`. | `EventFeed.tsx:10-16`, `types.ts:215-224` | Add `holds_placed: { icon: 'hand-left', color: colors.danger }` to `META`, and `customer_ids?: string[]` to `LiveEvent`. Data already refreshes (`AppContext.tsx:137`). | S |
| FE3 | **The second-confirmation header guesses.** It shows `overLimitConfirm` if *any* item is over the limit (`bolo.tsx:319-320`), but the backend asks about only one action at a time (`pending.py:140`). With a big amount and an over-limit amount on the same udhaar (3 yeses), the first follow-up header says "over the limit" while the voice says "big amount". The header also never shows the amount over. | `bolo.tsx:318-321` | Fix V1 first. Then have the backend say which question it is asking (for example `second_kind: 'over_limit' \| 'big'` and `over_by` in the turn), and show that, instead of working it out in the UI. | S (after V1) |
| FE4 | **The limit card can't show how far over the limit a customer is.** After an approved override, the card shows a red bar capped at 100% and "120% used", but no amount. `CreditStatus` has no `over_by`, and `credit_card()` doesn't return it. | `[id].tsx:173-195`, `types.ts:76-81`, `tools.py:650-657` | Backend: add `over_by` / `over_by_paise` to `credit_card()`. Frontend: add them to the type and show `overLimitWarn` in the card. | S |
| FE5 | The manual udhaar sheet doesn't show the limit room left. The merchant learns about the limit only from the 400 error after pressing Save. There is no override in the manual flow, which is correct, because overrides go through voice. | `CreditSheet`, `[id].tsx:213-252` | Pass `c.credit` and `c.balance_paise` into the sheet. Show "₹X left before the limit", or `limitZero`, or a "Over the limit: say it by voice to confirm" hint. Display only; the backend still decides. | S |
| FE6 | **An undo button is shown for payments even when the customer is on hold or at the limit** (related to V2). | `[id].tsx:133`, `147-151`, and `reverse()` at `35-45` | After V2 is decided: if undoing a payment would raise the balance for a customer who is held or limited, show a warning in the `confirm()` text, or hide the button if the backend will refuse. | S |
| FE7 | **The nudge button disappears too early.** `setNudged` runs *before* `agentPropose` (`bolo.tsx:114`). If the call fails, or the merchant says "nahi" on the next card, the button is gone for that payment until the app restarts (the state is only in memory). | `bolo.tsx:59`, `111-124`, `346` | Mark the payment as nudged only after a turn whose `committed[]` includes `send_partial_nudge`. On an error or a cancel, leave the button visible. | S |
| FE8 | **No nudge after a manual payment.** The customer screen's `PaymentSheet` uses REST, whose response has no `nudge`. Only voice or typed payments in Bolo get the button. The spec only requires the button on "the payment confirmation card", so this is a gap in coverage, not a broken rule. | `[id].tsx:254-290`, `client.ts:151-155` | Backend: return a `nudge` offer from `POST /customers/{c}/payment`. Frontend: show the button, call `agentPropose`, then show a confirm dialog that sends `haan` on the same session. | M |
| FE9 | **A nudge is offered for customers without a phone** (V4). The reminder is stored as `NO_PHONE`, but the merchant is told it was sent. | `bolo.tsx:346-358` | Hide or disable the button when the customer has no phone (`committed[].customer.phone_last4 === null`), with a "no phone number" hint. Fix V4 on the backend as well. | S |
| FE10 | **No hold or limit history.** `customer_events` (holds, lifts, limit changes, overrides) can't be seen anywhere in the app. | — | Backend: add a `GET /customers/{c}/events` endpoint (next step 6). Frontend: an "Activity" section in `[id].tsx` under History. | M |
| FE11 | **No frontend tests.** Only typecheck and lint protect these screens. | `package.json` (scripts: only `typecheck`, `lint`) | Add component tests for `CreditLimitCard` (no limit / zero / near / over), `CustomerRow` (hold badge), the `pending` card (warning lines and the second-confirmation header), and the nudge button state. Check the current Expo testing docs first (`AGENTS.md` requires this). | M |
| FE12 | **The Home "Overdue" list has no hold badge.** It draws its own rows instead of using `CustomerRow`. | `src/app/(tabs)/home.tsx:81-88` | Reuse the `onHold` `Badge` (or `CustomerRow`) there. | S |

### 7.4 Feature 4 frontend: plan for when the design is approved

Don't start this until approval is recorded (next step 1). The pieces needed are:
- **Types** (`src/api/types.ts`): `MarketBucket = 'A' | 'B' | 'C' | 'insufficient_data'` and a `market` field on `CustomerDetail`. It should contain only the bucket, never counts, other shops or identifiers.
- **Component:** a new `src/components/MarketBucketBadge.tsx` on the customer screen, with the label "Market grade" and a short "advice only" note.
  - Keep it **separate from the existing `TrustScoreBadge`.** That badge is the per-shop Trust Score (High / Medium / Low / New, plan.md §22), not Feature 4. Two different scores with similar names will confuse merchants and developers.
- **Shop opt-in:** a settings section in `setup.tsx` with the consent wording from `MARKET_TRUST_DESIGN.md` §3.1, shown on screen and not by voice, plus `market_code`.
- **Customer consent:** a "request consent" action on the customer screen (design §3.2). The customer's own consent page is served by the backend, not the app.
- **Withdrawal:** voice only, through `withdraw_market_consent` and the gate.
- **i18n:** all wording in en, hi and mr. The final text depends on the legal review.
- **Must never:** show the bucket anywhere near the udhaar, limit or hold controls in a way that suggests it decides anything. Show it only on the customer screen. Never send it to the agent.

---

## 8. Changed during the audit (not audited in full)

These files appeared or changed between the first audit run and the frontend pass. They belong to plan.md §20 and §22, not to BUILD_CONTEXT §11:
- **Backend:**
  - `app/risk/trust_score.py`,
  - `app/api/risk.py`,
  - the `check_udhaar_risk` tool and the "Udhaar Guard" in `_add_credit` (`app/agent/tools.py:418-424`),
  - a `PaymentAllocation` model with migration `0005_payment_allocations_and_adjustments`,
  - a wider append-only guard (`APPEND_ONLY = (LedgerEntry, CustomerEvent, PaymentAllocation)` in `app/db/models.py`).
- **Frontend:**
  - `TrustScoreBadge`,
  - the Trust Score card on the customer screen,
  - the UPI check in `customers.tsx`,
  - the `guardLow` line on pending cards.

**Checked against the Feature 4 safety rule ("a score never triggers a hold, block or limit change"):**
- The Udhaar Guard only adds text to the read-back (`sum_trust_low`).
- It does **not** change `confirmations_needed`, which is still big amount + over limit (`tools.py:430`).
- It doesn't touch the hold or limit fields.
- The new `trust_score.py` docstring says a test enforces that the ledger, hold and limit code never import it. I didn't verify that test.

The backend's append-only guard now also covers `ledger_entries`, which partly answers V3. It is still ORM-only.

---

**Unverified, and why:**
- Postgres behaviour of the migrations and of the `UNIQUE` constraint: only SQLite was run.
- That 0001 was never edited: there is no git history.
- Frontend rendering: typechecked, but the app was not run.
