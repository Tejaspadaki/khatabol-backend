# Khata Voice: Build Context

This is the source of truth for the build. It started as a copy of `PLAN.md` and is derived from `../../plan.md`, which wins where the two conflict. It covers the backend only (FastAPI service, DB, agent, voice, payments, scheduler).

**Rules that every change keeps:**
- money is integer paise only,
- `ledger_entries` and `customer_events` are append-only,
- the LLM proposes and code decides,
- every write goes through the confirmation gate,
- the tool layer is the only write path.

**Stack:**
- FastAPI + async SQLAlchemy,
- **Groq** LLM with tool calling (`qwen/qwen3.8-27b` by default, with reasoning hidden from replies),
- **Sarvam AI** for speech-to-text (`saaras`) and text-to-speech (`bulbul`),
- SQLite or Postgres.

**No live Paytm integration.** Khata Voice is a solution proposal *for* Paytm, so UPI payments are simulated through our own `payments/incoming` API. The reconciler behind it is the part a payment platform would call on each successful UPI credit.

## 1. Scope

| Phase (from plan.md §10) | Backend deliverable | Status |
|---|---|---|
| 1. Ledger core | Schema, ledger service, balance + FIFO due logic, REST API, tests | Built |
| 2. Agent (text first) | Tool schemas, orchestrator, Hinglish numbers/dates, name resolution, confirmation gate | Built |
| 3. Voice | Push-to-talk upload → Sarvam STT → agent → Sarvam TTS; voice clip kept as proof | Built |
| 4. UPI reconciliation | Simulated incoming payments, VPA linking, unknown-payer flow, announcements | Built |
| 5. Collections | Due lists, UPI reminder links, daily summary, scheduler | Built (messaging mocked) |
| 6. Polish and eval | Eval runner over a 100-utterance set | Next |

## 2. Key design decisions

1. **The confirmation gate is plain code, not the LLM.** Write tools never write. They create a `pending_actions` row and the agent reads it back. On the next utterance, a deterministic yes/no classifier ("haan", "sahi hai", "ok" / "nahi", "cancel") commits or cancels without calling the LLM. That makes the "0 wrong writes" metric a code guarantee and makes confirmations instant. Anything else (for example "500 nahi 600") goes to the LLM, which cancels and re-proposes.
2. **Amounts and dates are resolved by code.**
   - The utterance is pre-normalized ("saadhe teen sau" → 350) before the LLM sees it.
   - Tools take `amount_rupees` as a number, and the service converts it to integer paise.
   - Tools take `due` as the spoken phrase ("somvar", "5 tareekh"). `nlp/dates.py` resolves it against today in IST, so the LLM never does calendar math.
3. **Name matching runs in Python** (rapidfuzz + an Indic phonetic key). One merchant has hundreds of customers at most, so scanning in memory is fast. It also works the same on SQLite and Postgres. `pg_trgm` can be added later as an optimization.
4. **Balances.** The ledger is append-only. `customers.balance_paise` is a cache, updated in the same transaction as the ledger insert, and `verify_balances()` re-derives it from the ledger. "Next due date" is computed by FIFO: payments clear the oldest credits first.
5. **Every incoming UPI payment is recorded in `upi_transactions`** (`txn_id` is UNIQUE), including normal sales and unknown payers. This makes payment processing idempotent, and it is where unknown-payer questions are tracked.
6. **One LLM interface.** `agent/llm.py` defines `LLMClient.chat()`, which returns text plus parsed tool calls in the OpenAI-style message format. `GroqLLM` implements it.
   - It uses temperature 0.2 and `tool_choice=auto`.
   - It retries once on Groq's `tool_use_failed` error (a malformed tool call).
   - Invalid tool-argument JSON goes back to the model as an error instead of being executed.
   - Groq errors, or a missing key, become `LLMUnavailable`. The merchant then hears a spoken fallback instead of getting a 500.
   - Tests use a scripted fake LLM, so the test suite needs no API key.
7. **Voice is push-to-talk over plain HTTP.** The app records while the button is held and uploads the clip, so no WebSocket audio streaming or server-side VAD is needed.
   - STT is Sarvam `saaras:v3` in `translit` mode, which returns Roman-script Hinglish, the form the number and name normalizers expect. `saaras:v4` can be swapped in through config.
   - An empty transcript gets "Phir se boliye?" without calling the LLM.
   - TTS is Sarvam `bulbul:v3`. The text is rewritten so that "₹1,500" is spoken as "1,500 rupaye".
   - The clip is saved under `voice_clips/` and linked from `voice_logs.audio_url`. This is the "every entry has proof" feature.
8. **Database.** SQLite (aiosqlite) by default, so it runs with zero setup. Postgres (asyncpg) is used with `docker compose up`. Switch with `DATABASE_URL`.

## 3. Module layout

```
backend/
├── app/
│   ├── main.py              FastAPI app, lifespan (Alembic upgrade to head, scheduler), routers
│   ├── config.py            Settings (env / .env)
│   ├── events.py            In-process pub/sub → merchant WebSocket (announcements)
│   ├── db/                  base.py (engine, session), models.py
│   ├── nlp/                 hinglish_numbers.py, dates.py, names.py, confirm.py (yes/no)
│   ├── ledger/              service.py (ALL DB writes), allocation.py (FIFO), errors.py
│   ├── agent/               tools.py (schemas + executor), pending.py, prompts.py,
│   │                        llm.py (Groq), orchestrator.py
│   ├── voice/               sarvam.py (STT + TTS client)
│   ├── payments/            reconciler.py, upi_links.py
│   ├── scheduler/           jobs.py (reminders, daily summary, balance verify)
│   ├── api/                 merchants.py, customers.py, agent.py, payments.py, collections.py,
│   │                        ws.py, schemas.py
│   └── demo/                seed.py, simulate.html (push-to-talk + simulate UPI payment page)
└── tests/                   numbers, dates, names, ledger, reconciler, agent flow, API, adapters
```

## 4. Data model

The model follows plan.md §6, with these changes:

- `ledger_entries` adds `payment_mode` (CASH/UPI/OTHER) and `spoken_name`. `ADJUSTMENT` is left out because it has no direction; corrections use `REVERSAL`.
- A new table, `upi_transactions(txn_id UNIQUE, payer_vpa, amount_paise, note, status, customer_id, ledger_entry_id)`, has these statuses:
  - `APPLIED`: auto-settled against a customer's dues,
  - `SALE`: a normal sale, so the khata is unchanged,
  - `UNMATCHED`: the merchant is asked whose payment it is,
  - `RESOLVED`: the merchant assigned it to a customer.

  Undoing an auto-applied payment ("ye Ramesh ka nahi tha") reverses the entry, unlinks the VPA, and puts the payment back to `UNMATCHED`.
- `customers` adds `ref_code`, a short code like `K7F3Q` used in the UPI `tn` note. It is the second signal for matching a payment to a customer.
- `pending_actions` adds `session_id`, `voice_log_id`, `confirmations_needed` (2 for large amounts), and `status`.

Balance sign convention: **positive = customer owes the merchant; negative = advance.**

### 4.1 Additions for the features in §11

- A new table, `customer_events(id, customer_id, merchant_id, event_type, payload JSON, actor, created_at)`. `actor` is `system`, `merchant`, or `customer`. It is **append-only**, like `ledger_entries`: rows are never updated or deleted.
- `customers` adds:
  - `credit_limit_paise` (BIGINT, nullable). `NULL` means no limit; `0` blocks all new udhaar.
  - `on_hold` (BOOLEAN, default `false`).
  - `hold_reason` (TEXT, nullable).

  `on_hold` and `hold_reason` are a cache of the latest hold event in `customer_events`, updated in the same transaction, the same way `balance_paise` caches the ledger.
- `merchants` adds `hold_grace_days` (INTEGER, default 15).
- Feature 4 (Trust Score, §11.4) adds no tables. The `market_contributions` and `market_consents` tables planned for the earlier cross-merchant score are dropped with it (plan.md §22 Level 5, future).

Schema changes ship as Alembic revisions in `app/db/migrations/versions/`. `0001_baseline_schema` matches the schema that `create_all` used to build. App start runs `upgrade head`, and a database made before migrations existed is stamped at `0001` first. Each feature adds its own new revision on top. Old revisions are never edited. Tests still build tables with `create_all` for speed, and `tests/test_migrations.py` checks that a fresh database at head matches the models exactly.

## 5. Agent turn

```
POST /api/merchants/{id}/agent/voice  (audio clip) → Sarvam STT → transcript ─┐
POST /api/merchants/{id}/agent/message {text, session_id?, speak?} ───────────┤
  1. pending actions exist?
       yes-word → commit all (amounts > threshold need a 2nd yes) → template reply.  [no LLM]
       no-word  → cancel all → "Theek hai, cancel kar diya."                        [no LLM]
  2. normalize numbers → build user turn (original + normalized + pending list)
  3. LLM loop (max 6 rounds): tool_use → execute tool → tool_result
       reads  → run now
       writes → validate + create pending_action → return a read-back summary
  4. reply text + cards (pending actions, balances) → voice_logs row
  5. voice / speak=true → Sarvam TTS → base64 WAV in the response
```

Conversation history is kept in memory per `(merchant, session)`. It is reset each day or after 40 messages.

## 6. Tools

These are the tools from plan.md §4.2, plus three additions:

- `cancel_pending`: cancels a pending action, for corrections like "500 nahi 600".
- `list_unmatched_payments`: lists UPI payments from unknown payers.
- `assign_upi_payment`: answers "whose payment is this?" by assigning the payment to a customer (a write, so it is confirmed first).

Write tools (`create_customer`, `add_credit`, `record_payment`, `link_upi`, `send_reminder`, `undo_last`, `assign_upi_payment`) all go through the gate.

**Count reconciliation.** plan.md §4.2 lists 11 tools. The code (`app/agent/tools.py`) has those 11 plus the 3 additions above, for **14**. An earlier count of 10 matched neither, so 14 is the baseline.

### 6.1 New tools for §11

| Tool | Args | Returns | Write? | Feature |
|---|---|---|---|---|
| `set_credit_limit` | `customer_id`, `limit_paise` | old and new limit | Yes | 1 |
| `get_credit_status` | `customer_id` | balance, limit, percent used | No | 1 |
| `set_hold` | `customer_id`, `on_hold`, `reason` | hold state | Yes | 2 |
| `send_partial_nudge` | `customer_id`, `payment_id` | reminder sent | Yes (side effect) | 3 |
| `check_udhaar_risk` | `customer` or `vpa` or `recent_payment_rupees`, `name?`, `amount_rupees?` | level, suggested amount, reasons | No | 4 |

With these the count is **19**: 18 after Features 1–3, plus `check_udhaar_risk` (plan.md §22) for Feature 4. The new write tools go through the gate like the others.

The service API takes `limit_paise`. At the LLM boundary, the schema takes `limit_rupees` and the service converts it, the same way the existing tools take `amount_rupees` (§2.2), so the LLM never does paise math.

## 7. REST API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/merchants` | Create merchant |
| GET | `/api/merchants/{m}` | Merchant profile |
| GET/POST | `/api/merchants/{m}/customers` | List / create customers (`?q=` fuzzy search) |
| GET | `/api/merchants/{m}/customers/{c}` | Balance, next due, last payment |
| GET | `/api/merchants/{m}/customers/{c}/history` | Ledger entries |
| POST | `/api/merchants/{m}/customers/{c}/credit` · `/payment` · `/upi` | Manual writes (source=MANUAL) |
| POST | `/api/merchants/{m}/entries/{e}/reverse` | Reverse an entry |
| POST | `/api/merchants/{m}/agent/message` | Text agent turn (`speak=true` adds TTS audio) |
| POST | `/api/merchants/{m}/agent/propose` | One-tap write from the app (only `send_partial_nudge`): creates a pending action, commit with "haan" |
| GET | `/api/merchants/{m}/risk?vpa=&customer_id=&name=&amount_rupees=` | Trust Score + UPI Udhaar Check (advice only) |
| POST | `/api/merchants/{m}/agent/voice` | Voice agent turn (multipart `audio`, `session_id`) |
| POST | `/api/merchants/{m}/tts` | Speak any text (announcements), returns `audio/wav` |
| GET | `/api/merchants/{m}/dues?filter=` | today / overdue / this_week / all |
| POST | `/api/merchants/{m}/reminders` | Send reminders (mock channel) |
| GET | `/api/merchants/{m}/summary?date=` | Daily summary |
| GET | `/api/merchants/{m}/payments/unmatched` | Unknown-payer queue |
| POST | `/api/merchants/{m}/payments/incoming` | Simulated incoming UPI payment → auto-settle (idempotent on `txn_id`) |
| POST | `/api/merchants/{m}/payments/{txn}/assign` | Resolve unknown payer |
| POST | `/api/merchants/{m}/payments/{txn}/undo` | Undo a wrong auto-settlement |
| GET | `/voice-clips/{file}` | Stored voice clip (entry proof) |
| WS | `/ws/merchants/{m}/events` | Live announcements ("Ramesh ne ₹300 diye…") |
| GET | `/demo/simulate` | Demo console: push-to-talk, text, "Customer pays", live announcements |
| POST | `/demo/seed` | Seed demo merchant + customers |

## 8. Next steps

- **Eval (Phase 6):** about 100 Hinglish utterances in `eval/utterances.jsonl`, run through the real Groq model. It scores tool name, customer and amount, and the results go on the metrics slide. Use it to compare `qwen/qwen3.8-27b` against `openai/gpt-oss-120b`.
- **Latency:** time STT, LLM and TTS separately per turn (the total is already in `voice_logs.latency_ms`). If needed, switch to Sarvam streaming STT/TTS over WebSocket.
- **Voice clip retention:** auto-delete clips after a set period (plan.md §16, privacy).

## 9. Testing

- Unit tests:
  - number normalizer (every row of the plan.md §4.4 table),
  - date resolver (fixed "today"),
  - name scoring (honorifics, Devanagari, duplicates),
  - ledger balance/FIFO/undo,
  - reconciler (idempotency, overpayment → advance, unknown payer → resolve).
- Agent flow test: a scripted fake LLM checks that a write creates a pending action, "haan" commits it, "nahi" cancels it, and a large amount needs a double confirm.
- Adapter tests: Groq request/response mapping and the `tool_use_failed` retry, run against a fake client. Sarvam STT/TTS request shapes and error handling, run against `httpx.MockTransport`.
- API tests cover voice turns with a fake Sarvam client.
- The eval set (Phase 6) runs the real LLM over `eval/utterances.jsonl` and scores tool name, customer, and amount.

## 10. Run

```
cd backend
uv sync
cp .env.example .env            # set GROQ_API_KEY and SARVAM_API_KEY
uv run uvicorn app.main:app --reload
uv run pytest
uv run alembic upgrade head     # optional: the app also does this on start
uv run alembic revision --autogenerate -m "..."   # new schema change: review it, never edit old ones
docker compose up               # Postgres variant
```

## 11. Features in progress

They are built in order, one at a time. Each feature gets its own new Alembic revision, tests written first for the rules below, and a frontend change, and the full suite stays green before the next feature starts.

### 11.1 Feature 1: Credit limit by voice, with a near-limit warning

**Status:** built (migration `0002_credit_limit_and_customer_events`).

- "Ramesh ki limit 2000 rakho" proposes a limit of ₹2,000 (`set_credit_limit`). It writes only after confirmation.
- A udhaar that would take the balance over the limit is blocked. An override needs a separate confirmation that names the overage (for example "₹300 limit se zyada").
- When the balance after the udhaar would reach 80% of the limit or more, the udhaar confirmation card shows a warning.
- A limit of zero blocks all new udhaar. Negative limits are rejected.
- The udhaar tool (`add_credit`) checks the limit itself. The agent does not decide this.
- Every limit change is appended to `customer_events`.
- Frontend: a credit-limit card on the customer screen (limit, balance, percent used).

**Must never:** let the LLM decide whether a udhaar is within the limit, or write a limit or an over-limit udhaar without confirmation.

### 11.2 Feature 2: Hold for overdue customers

**Status:** built (migration `0003_customer_holds`). The job runs daily at `HOLD_HOUR` (default 6:00 IST). `hold_grace_days` can be changed with `PATCH /api/merchants/{id}`.

- A daily job places a hold on any customer with an entry overdue beyond `hold_grace_days`. "Overdue" uses the existing FIFO due logic: an unpaid credit whose `due_date` is more than `hold_grace_days` in the past. Credits without a due date never trigger a hold.
- A hold blocks new udhaar only. Payments, reminders, and history keep working.
- The merchant can place or lift a hold by voice (`set_hold`), with confirmation. Lifting a hold while the balance is still overdue names the overdue amount in the confirmation.
- Holds are events in `customer_events` with a reason and an actor (`system` for the daily job, `merchant` for voice). Nothing is deleted or hidden.
- The udhaar tool checks the hold itself and returns a refusal the agent can speak.
- Frontend: a hold badge on the customer list and customer screen.

**Must never:** block payments, reminders, or history, or delete or hide a hold event.

**Open point:** if the merchant lifts a hold while the customer is still overdue, does the next daily run hold them again? The proposal is no: the job skips customers whose latest hold event is a merchant lift, until a new entry goes overdue.

### 11.3 Feature 3: Partial-payment nudge

**Status:** built (migration `0004_partial_payment_nudge_key`: `reminders.source_payment_id` + `nudge_date`, unique together). By voice, `payment_id` may be omitted and defaults to the customer's latest live payment, because the LLM never sees the id of a payment committed by "haan". The app's button calls `POST /agent/propose`, which only creates the pending action; the "haan" still goes through the gate.

- After a payment leaves a balance, the agent reads back the amount paid and the amount still due.
- A one-tap reminder for the remaining balance (`send_partial_nudge`) is sent only after the merchant confirms.
- The amount in the reminder is read from `get_balance` at send time, not from the original payment message.
- Only one nudge per payment per day, enforced by a unique key.
- Reminders reuse the existing reminders module (`app/scheduler/reminders.py`) and its template rules (`reminder_message` via `app/i18n`).
- Frontend: a nudge button on the payment confirmation card.

**Must never:** send without confirmation, use a stale amount, or send twice for the same payment on the same day.

**Open points:**
- **Unique key storage.** §4.1 has no column for this. The proposal is to add nullable `source_payment_id` and `nudge_date` columns to `reminders`, with a unique index on `(source_payment_id, nudge_date)`.
- **Consent.** The reminders module has templates but **no consent rules today**. Decision: Feature 3 reuses the module as it is and does not add consent. A consent check, if one is added later, belongs in the module so that it covers every reminder.

### 11.4 Feature 4: Trust Score + UPI Udhaar Check (plan.md §22)

**Status:** built. No migration: it reads `ledger_entries`, `customer_upi_handles` and `upi_transactions`.

This replaces the earlier cross-merchant "market trust score". That idea is plan.md §22 **Level 5 (Paytm network)**, which plan.md keeps as future work needing consent and regulatory clearance. Its draft design stays in `docs/MARKET_TRUST_DESIGN.md`, marked superseded.

Rules:
- A known customer gets a trust level, **High / Medium / Low**, from transparent rules over their own behaviour at this shop:
  - on-time repayment rate (a late udhaar that was at least half paid by its due date counts as half on time);
  - average days late;
  - overdue right now;
  - current balance against their usual balance;
  - how long they have been a customer;
  - how often their UPI ID has paid this shop.

  Udhaar without a due date is judged against 30 days from the day it was given.
- **`check_udhaar_risk(customer | vpa | recent payment, amount?)`** returns the level, a suggested amount and the reasons. The UPI ID can be spoken ("rahul99@ybl ko udhaar dena hai?"), taken from a recent payment ("jo abhi ₹50 aaye the"), or typed in the app.
  - **Level 1, this shop:** the history of the UPI ID's linked customer.
  - **Level 2, all of the merchant's shops:** not available until shops exist (§23). Today one merchant is one shop, so Level 1 covers it.
  - **Level 3, payment relationship:** how long and how often this UPI ID has paid the shop (`upi_transactions`).
  - **Level 4, name check:** the UPI ID's registered name (the payer name on its payments, standing in for UPI verification) compared with the name the customer gives. A mismatch is a warning and lowers the level.
  - **Level 5, Paytm network:** future (plan.md §22), not built.
- No history at all: "Naya customer hai. ₹300 tak se shuru karein."
- **Udhaar Guard:** when a udhaar for a **Low** customer is above the suggested amount, the read-back adds "Trust Score: Low, ₹500 tak theek rahega" before "Save karun?". It needs no extra confirmation: the merchant decides.
- Uses repayment behaviour only, never names, phone numbers, caste, religion or area. It is shown only to the merchant.
- Promises (§21) and disputes (§18) feed the score once they exist; they are not tracked yet.
- Frontend:
  - a TrustScore badge with reasons on the customer screen;
  - the guard line on the udhaar confirmation card;
  - a "Check UPI ID" sheet on the Khata tab (typed UPI ID; QR scan not built).

**Must never:** block an udhaar, place a hold, or change a limit. The score is advice only. Only `app/risk/`, the tool layer and the read-only API (`GET /risk`, customer detail) read it, and `tests/test_trust_score.py` fails if anything else imports it.
