# Khata Voice: Backend Plan

This plan is derived from `../plan.md`. It covers the backend only (FastAPI service, DB, agent, voice, payments, scheduler).

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
│   ├── main.py              FastAPI app, lifespan (create tables, scheduler), routers
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
- **Migrations:** Alembic, before any real data.

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
docker compose up               # Postgres variant
```
