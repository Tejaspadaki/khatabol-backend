# Khata Voice API: test cases and results

This document lists every backend endpoint with its test cases, explains how to run the tests, and records the latest results.

Each case has an ID such as `CUS-08`. The same ID appears in the test function name (`test_CUS_08_...`) and in the live report.

## Results at a glance

| Suite | What it runs against | Cases | Result |
|---|---|---|---|
| API tests (`tests/api/`) | In-process app, temp SQLite, fake Groq + fake Sarvam | 82 | All pass |
| Unit + flow tests (`tests/*.py`) | Numbers, dates, names, ledger, reconciler, agent loop, Groq/Sarvam adapters | 93 | All pass |
| **Total automated** | | **175** | **175 / 175 pass** |
| Live run (`scripts/live_api_test.py`) | Real server, **real Groq + Sarvam** | 37 | **37 / 37 pass** ([LIVE_TEST_RESULTS.md](LIVE_TEST_RESULTS.md)) |

Coverage: all 28 endpoints. That is 25 REST operations, the WebSocket, the voice-clip file route, and the `/` redirect.

**Manual testing:** open `/demo/tester` on a running server. It lists every endpoint as a form, with a customer picker, voice recording, audio playback, a live WebSocket event log, and a history of requests sent. `/demo/tester#9` opens a specific endpoint directly.

## How to run

**Automated tests.** These need no keys and no network access, and take about 11 s.

```bash
cd backend
uv run pytest                 # everything
uv run pytest tests/api -v    # just the API cases, with IDs
uv run pytest -k CUS_08       # one case
```

**Live test.** This needs `GROQ_API_KEY` and `SARVAM_API_KEY` in `.env`. Every run creates a fresh demo shop.

```bash
uv run uvicorn app.main:app --port 8000          # terminal 1
uv run python scripts/live_api_test.py           # terminal 2 -> writes LIVE_TEST_RESULTS.md
uv run python scripts/live_api_test.py --base http://127.0.0.1:8765   # other host/port
```

Cases tagged `[AI]` in the live run depend on the LLM or the speech models, so their wording can differ between runs. They check the structure of the response and its effect on the khata, not the exact sentence.

## Conventions

- Base path: `/api/merchants/{m}`, where `{m}` is the merchant UUID.
- Money in requests is `amount_rupees` (a number). Responses carry both a display string (`"₹1,500"`) and integer `*_paise`.
- Errors return `{"detail": "..."}`. Ledger errors are short Hinglish sentences that can be shown or spoken to the merchant.
- Status codes:
  - `422`: request failed schema validation.
  - `400`: request is valid but breaks a business rule.
  - `404`: unknown resource, or a resource that belongs to a different merchant.
  - `503`: the speech service (Sarvam) is unavailable.

---

## 1. Merchants: `tests/api/test_merchants_api.py`

| Endpoint | Purpose |
|---|---|
| `POST /api/merchants` | Create a shop. Body: `name`, `phone?`, `upi_vpa?`, `language` (`hi`/`mr`/`en`), `large_amount_threshold_rupees` (default 10000) |
| `GET /api/merchants/{m}` | Shop profile |

| ID | Case | Expected |
|---|---|---|
| MER-01 | Create with all fields | 201; threshold shown as `₹5,000`; 36-char UUID |
| MER-02 | Create with only `name` | 201; defaults `hi`, `₹10,000`, no VPA |
| MER-03 | Missing / empty name, language `fr`, threshold 0 | 422 each |
| MER-04 | Get existing | 200 |
| MER-05 | Get unknown UUID | 404 `Merchant nahi mila.` |
| MER-06 | Get with malformed id | 422 |

## 2. Customers and khata: `tests/api/test_customers_api.py`

| Endpoint | Purpose |
|---|---|
| `GET .../customers` | List customers A-Z. `?q=` runs fuzzy/phonetic search and returns `decision`: `match`, `ambiguous` or `no_match` |
| `POST .../customers` | Create. Body: `name`, `phone?`, `nicknames[]` |
| `GET .../customers/{c}` | Balance, next due date and amount, overdue, last payment, UPI IDs, `ref_code` |
| `GET .../customers/{c}/history?limit=` | Entries, newest first (limit 1 to 500) |
| `POST .../customers/{c}/credit` | Udhaar. Body: `amount_rupees`, `due?` (ISO date or spoken phrase like "kal" or "5 tareekh"), `items?`, `note?` |
| `POST .../customers/{c}/payment` | Payment. Body: `amount_rupees`, `mode` (`cash`/`upi`/`other`), `note?` |
| `POST .../customers/{c}/upi` | Link a UPI ID (VPA). Body: `vpa` |
| `POST .../entries/{e}/reverse` | Append a REVERSAL entry (the ledger is append-only) |

| ID | Case | Expected |
|---|---|---|
| CUS-01 | Create `"ramesh  patil"` with phone and nickname | 201; name `Ramesh Patil`; `phone_last4`; balance `₹0` |
| CUS-02 | Create Devanagari `रमेश`, then search `ramesh` | Search decision is `match` |
| CUS-03 | Missing / empty name; name made only of honorifics (`bhaiya ji`) | 422, 422, 400 |
| CUS-04 | Create under an unknown merchant | 404 |
| CUS-05 | List | Sorted by name |
| CUS-06 | Search `ramesh bhaiya` (two Rameshes), `sunitha`, `suresh` | `ambiguous` with 2 candidates; `match` Sunita Devi with score ≥ 0.85; `no_match` |
| CUS-07 | List for unknown merchant | 404 |
| CUS-08 | Detail after ₹700 overdue + ₹300 future credit, ₹200 paid, UPI linked | Balance ₹800; overdue ₹500 (payment cleared the oldest credit first); next due ₹500; VPA lower-cased; last payment ₹200 |
| CUS-09 | Detail for unknown customer | 404 `Ye customer is khata mein nahi hai.` |
| CUS-10 | Read, write or list a customer through another merchant | 404 / empty: data is isolated per shop |
| CUS-11 | History order and `limit=1` | PAYMENT, CREDIT, CREDIT; 1 entry |
| CUS-12 | `limit` 0 or 501 | 422 |
| CUS-13 | Credit with ISO due, then with `due="kal"` and ₹150.50 | Due dates resolved; paise exact (50050) |
| CUS-14 | `due="jab paise honge"` | 400 `...samajh nahi aayi` |
| CUS-15 | Amount missing / 0 / negative / text; amount over ₹1 crore | 422 each; 400 |
| CUS-16 | Credit for unknown customer | 404 |
| CUS-17 | UPI payment, then overpayment in cash | Mode `UPI`; default `CASH`; balance `-₹150` (advance) |
| CUS-18 | Mode `cheque`; amount 0; unknown customer | 422, 422, 404 |
| CUS-19 | Link `"  Ramesh@YBL "` | 201; stored as `ramesh@ybl` |
| CUS-20 | VPA without `@`; with a space; missing | 400, 400, 422 |
| CUS-21 | Link the same VPA to a second customer | VPA moves to the latest customer |
| CUS-22 | Reverse a payment, reverse it again, reverse the reversal | 201 and balance restored; then 400; 400 |
| CUS-23 | Reverse unknown entry; reverse another shop's entry | 404, 404 |
| CUS-24 | Credit, credit, payment, credit, then reverse the last credit | Balance is exactly 100000 + 25075 − 40000 paise |

## 3. Agent, voice and speech: `tests/api/test_agent_api.py`

| Endpoint | Purpose |
|---|---|
| `POST .../agent/message` | Text turn. Body: `text` (1 to 1000 chars), `session_id?`, `speak?`. Returns `reply`, `outcome`, `pending[]`, `committed[]`, `tool_calls[]`, `latency_ms`, plus `audio_base64` when `speak=true` |
| `POST .../agent/voice` | Multipart upload: `audio` (≤ 10 MB) and `session_id?`. Runs Sarvam STT, then the agent, then Sarvam TTS. Also returns `transcript` |
| `POST .../tts` | Body: `text` (≤ 2000 chars). Returns `audio/wav` |

Possible `outcome` values:
- `answered`
- `awaiting_confirmation`
- `awaiting_second_confirm` (amount above the threshold)
- `committed`
- `cancelled`
- `write_failed`
- `low_confidence`
- `llm_unavailable`

| ID | Case | Expected |
|---|---|---|
| AGT-01 | Plain question | `answered`; no `audio_base64` key |
| AGT-02 | "Ramesh Patil ko saadhe teen sau... somvar tak", then "haan" | First `awaiting_confirmation` with nothing written; then `committed`, reply `Ho gaya. Ramesh Patil: ab ₹850 baaki.`, entry source `VOICE`; **1 LLM call in total** |
| AGT-03 | Propose, then "nahi" | `cancelled`; balance unchanged |
| AGT-04 | Propose in session A, say "haan" in session B | B's "haan" goes to the LLM and nothing is committed |
| AGT-05 | Empty body / empty text / 1001 chars / 65-char session id | 422 each |
| AGT-06 | Unknown merchant | 404 |
| AGT-07 | LLM unavailable | 200, `llm_unavailable`, spoken fallback sentence |
| AGT-08 | `speak=true` | WAV audio (`RIFF`) of the reply |
| AGT-09 | `speak=true` with TTS down | Reply still returned; `audio_base64: null`; `tts_error` set |
| AGT-10 | Voice turn | Transcript, reply and audio returned; clip saved to `voice_clips/` as proof |
| AGT-11 | Voice "Ramesh Patil ko sau rupaye", then voice "haan" | Committed; balance ₹100 |
| AGT-12 | Silent clip (empty transcript) | `Phir se boliye?`, `low_confidence`, LLM not called |
| AGT-13 | Empty file; 10 MB + 1 byte; no file | 400, 413, 422 |
| AGT-14 | Sarvam STT down | 503 `Speech-to-text unavailable...` |
| AGT-15 | Voice for unknown merchant | 404 |
| AGT-16 | TTS | 200 `audio/wav` |
| AGT-17 | TTS with empty / 2001-char text; unknown merchant; Sarvam down | 422, 422, 404, 503 |

The agent's internal behavior (name resolution, ambiguous names, multi-intent, corrections, the read-back guard, rate limits, no DB lock during LLM calls) is covered in `tests/test_agent.py`.

## 4. Collections: `tests/api/test_collections_api.py`

| Endpoint | Purpose |
|---|---|
| `GET .../dues?filter=` | `today` (due today or overdue), `overdue`, `this_week`, `all` (default). Sorted by amount due, largest first |
| `POST .../reminders` | Body: `customer_ids[]` or `all_due: true`. Sends a WhatsApp message with a `upi://pay` link (delivery is mocked) |
| `GET .../summary?day=YYYY-MM-DD` | Credit given, collected (and the UPI part), outstanding, plus a spoken sentence |

Seed data used in these tests (relative to today):

| Customer | Owes | Due |
|---|---|---|
| Ramesh Patil | ₹500 | Overdue |
| Sunita Devi | ₹450 | Today |
| Ramesh Sharma | ₹1,200 | In 4 days |
| Mohan Lal | ₹250 | No due date |
| Pappu | ₹0 | |

| ID | Case | Expected |
|---|---|---|
| COL-01 | Each filter | overdue = {Ramesh P}; today = {Ramesh P, Sunita}; this_week adds Ramesh S; all adds Mohan |
| COL-02 | Default filter | `all`, total `₹2,400`, sorted largest first |
| COL-03 | `filter=yesterday` | 422 |
| COL-04 | Shop with no dues | Empty list, `₹0` |
| COL-05 | `all_due` | 2 sent; link has `pa=sharmastore@paytm` and `tn=KHATA-<ref>` |
| COL-06 | One customer with no phone | 1 reminder, status `NO_PHONE`, link `am=250.00` |
| COL-07 | Customer who owes nothing | 0 sent |
| COL-08 | Empty body; bad UUID; unknown customer | 422, 422, 404 |
| COL-09 | Merchant without a UPI ID | 400 (a payment link can't be built) |
| COL-10 | Summary after a ₹300 UPI payment | Collected ₹300 (UPI); outstanding ₹2,100; spoken sentence |
| COL-11 | Past day; `day=31-12-2020` | Zeros; 422 |

## 5. Payments (simulated UPI): `tests/api/test_payments_api.py`

There is no live Paytm integration. Khata Voice is a proposal for Paytm, and these endpoints stand in for the payment platform.

| Endpoint | Purpose |
|---|---|
| `POST .../payments/incoming` | A UPI payment the shop received. Body: `payer_vpa`, `amount_rupees`, `payer_name?`, `note?`, `txn_id?` (generated if missing; a repeated `txn_id` is ignored) |
| `GET .../payments/unmatched` | Payments from unknown payers, waiting for "kiska hai?" |
| `POST .../payments/{txn}/assign` | Body: `customer_id`. Records the payment for that customer and learns their VPA |
| `POST .../payments/{txn}/undo` | "Ye Ramesh ka nahi tha": reverses the entry, unlinks the VPA, and puts the payment back in the queue |

| ID | Case | Expected |
|---|---|---|
| PAY-01 | Known VPA, customer has dues | `APPLIED`; `Ramesh Patil ne ₹300 UPI se diye. Ab ₹200 baaki.`; entry `UPI_AUTO` |
| PAY-02 | Same `txn_id` again | `duplicate: true`; no double count |
| PAY-03 | No `txn_id` given | Unique `SIM...` ids |
| PAY-04 | Unknown VPA, shop has dues | `UNMATCHED`; `₹200 sunita99@ybl se aaye. Kiska hai?`; VPA lower-cased |
| PAY-05 | Unknown VPA, shop has no dues | `SALE` (`₹50 prapt hue.`) |
| PAY-06 | Known VPA, customer owes nothing | `SALE`; khata unchanged |
| PAY-07 | Note carries `KHATA-<ref>` from a reminder link | `APPLIED` to that customer; new VPA learned |
| PAY-08 | Payment larger than the dues | `₹100 advance jama hai` |
| PAY-09 | Amount 0; 2-char VPA; missing fields; unknown merchant | 422 ×4, 404 |
| PAY-10 | Unmatched list | Only `UNMATCHED` payments |
| PAY-11 | Assign, then the same VPA pays again | `RESOLVED`, spoken confirmation; the next payment is `APPLIED` automatically |
| PAY-12 | Assign unknown txn; already applied; unknown customer; empty body | 404, 400, 404, 422 |
| PAY-13 | Undo an auto-match, then assign to the right customer | `UNMATCHED`; Ramesh restored and unlinked; Sunita credited |
| PAY-14 | Undo unknown txn; undo a payment with no entry | 404, 400 |
| PAY-15 | Assign across shops (foreign txn or foreign customer) | 404 both |

## 6. System, demo and WebSocket: `tests/api/test_system_api.py`

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness |
| `GET /` | Redirects to the demo console |
| `GET /voice-clips/{file}` | Stored voice clip (proof for an entry) |
| `POST /demo/seed` | Create the demo shop |
| `GET /demo/merchants` | Shops, newest first (feeds the demo's shop picker) |
| `GET /demo/simulate` | Demo console: push-to-talk, text, "Customer pays", live announcements |
| `GET /demo/tester` | Manual API tester (light theme): every endpoint as a form |
| `WS /ws/merchants/{m}/events` | Live announcements: `upi_applied`, `upi_unmatched`, `upi_sale`, `reminders_sent`, `daily_summary` |

| ID | Case | Expected |
|---|---|---|
| SYS-01 | Health | `{"ok": true}` |
| SYS-02 | Root | 307 to `/demo/simulate` |
| SYS-03 | OpenAPI | 24 paths; `payments/incoming` present; no webhook route |
| SYS-04 | Voice clip file; missing file | Bytes served; 404 |
| DEM-01 | Seed | 201; 5 customers |
| DEM-02 | Merchants list | Newest first |
| DEM-03 | Demo page | HTML with "Hold to talk" and the `payments/incoming` call |
| DEM-04 | Tester page | Light theme only; every REST path is in its catalog |
| WS-01 | Subscribe, then two payments arrive | `upi_applied` event with text, amount and status; then `upi_unmatched` with `payer_vpa` |

---

## 7. Live run against real Groq and Sarvam

Last run: **37 / 37 passed**. The full table with timings and replies is in [LIVE_TEST_RESULTS.md](LIVE_TEST_RESULTS.md). Highlights:

| Case | What happened |
|---|---|
| AGT-01 | "Ramesh Patil ka kitna baaki hai?" → one `get_balance` call → "Ramesh Patil ka ₹500 baaki hai, jo 28 October ko due tha aur ab overdue hai..." (1.7 s) |
| AGT-02 | "Mohan Lal ko 120 rupaye ka saamaan diya, kal tak dega" → "Mohan Lal ko ₹120 udhaar (saamaan), kal tak. Save karun?" (0.6 s); "haan" → "Ho gaya. Mohan Lal: ab ₹370 baaki." (16 ms, no LLM) |
| AGT-03 | Multi-intent ("Ramesh Sharma ko 100..., Sunita Devi ne 50 cash lautaye") → both actions proposed in one reply; "nahi" cancelled both |
| AGT-10 | Voice round trip: Sarvam TTS spoke "Sunita Devi ka kitna baaki hai?", Sarvam STT heard it word for word, and the agent answered with the correct balance and spoken audio |
| PAY-01 / WS-01 | Payment → khata updated → announcement on the WebSocket in 92 ms |

## 8. Bugs found by this testing (all fixed)

| # | Found by | Problem | Fix |
|---|---|---|---|
| 1 | Live run | The default model `llama-3.3-70b-versatile` isn't available to this Groq account (404 `model_not_found`) | Default is now `qwen/qwen3.8-27b` with `reasoning_format=hidden`, picked from the models the key can access. Any `<think>` text that still appears is stripped |
| 2 | Live run | **"database is locked" (500)**: an agent turn kept a SQLite write transaction open while waiting on Groq, so every other write in the shop failed | Commit before every LLM call; SQLite now runs in WAL mode with a busy timeout. Regression test `test_no_db_transaction_held_during_llm_call` fails with the same error if the fix is removed |
| 3 | Live run | Groq free tier allows 7,000 input tokens per minute; each call sent ~2,400 tokens and a turn needed 3 calls, so turns stalled 10 to 27 s on 429 retries | Tools accept the customer name directly (no separate lookup round); write-only rounds skip the final LLM call; prompt and tool schemas shortened (~2,400 → ~1,600 tokens). Balance question: 3 calls → 2. Credit entry: 3 → 1. On a 429 the merchant hears "Ek minute baad boliye" instead of waiting |
| 4 | Live run | The agent read out an undone payment as the "last payment" | `last_payment` skips reversed entries |
| 5 | Live run | A payment made at 00:30 IST was described as "kal" (the UTC date) | Last-payment date converted to IST; test `test_last_payment_date_is_ist` |
| 6 | Automated run | On Windows, two quick entries could get the same timestamp, so "undo last" picked one at random (flaky test) | Timestamps are strictly increasing within the process |

## 9. Known limits

- **Groq free tier.** The live script sends AI requests back to back, so 5 requests hit 429 and three cases took 17 to 53 s while waiting on retries. At a real counter, turns are spread out and stay under the limit. For a demo with rapid back-to-back turns, use Groq's Dev tier, or wait about a minute between bursts.
- **SQLite** suits the demo. For more than one server worker, use Postgres (`docker compose up`), which hasn't been tested here yet.
- **Reminder delivery** (WhatsApp/SMS) is mocked: messages are stored with status `SENT` or `NO_PHONE` and logged.
