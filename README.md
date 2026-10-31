# Khata Voice: backend

FastAPI service for the voice-first udhaar ledger:

- Sarvam AI for speech-to-text and text-to-speech,
- Groq for the agent's LLM,
- SQLite by default, or Postgres.

The design and module map are in [PLAN.md](PLAN.md).

## Run

```bash
cd backend
uv sync
cp .env.example .env              # add GEMMA4_API_KEY (or GROQ_API_KEY with LLM_PROVIDER=groq) and SARVAM_API_KEY
uv run uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/app** in Chrome or Edge. Plain `http://` works for the microphone only on `localhost` / `127.0.0.1`.

1. Click **+ Demo shop** (first time only).
2. **Tap the blue mic once** to start a hands-free conversation. Speak, then pause: about 1 second of silence sends your turn. The agent answers aloud and starts listening again on its own.
3. Keep talking: "Mohan Lal ko 120 ka saamaan diya, kal tak", then "haan" to save it (or press **Haan, save karo**).
4. End it by saying **"bas"**, **"band karo"** or **"dhanyavaad"**, or press **■ Baat khatam**. "Haan bas" saves any pending entry first; plain "bas" says it wasn't saved. The conversation also closes after 60 s of silence.
5. Under **UPI payment**, press **Customer ne pay kiya** to hear the Soundbox-style announcement and watch the balance drop.

While the agent is speaking, the mic is paused so it can't hear itself. Tap the mic to interrupt it. Space works like the mic button, and Esc ends the conversation.

Replies are spoken by Sarvam TTS. Untick **Awaaz on** to keep it quiet.

API docs are at http://127.0.0.1:8000/docs. The older demo console is still at `/demo/simulate`.

To test the endpoints by hand, open http://127.0.0.1:8000/demo/tester. Every endpoint is a form there: pick a merchant, fill the inputs and press Send to see the response. It can also record voice, play TTS audio and show live WebSocket events.

The ledger, payments, reminders and demo page work with no keys. `/agent/*` needs `GEMMA4_API_KEY` (or `GROQ_API_KEY` with `LLM_PROVIDER=groq`), and voice/TTS needs `SARVAM_API_KEY`.

## Test

```bash
uv run pytest
```

The tests use a scripted fake LLM, a fake Sarvam client and a temporary SQLite DB, so they don't need keys or network access.

To test every endpoint against a running server with your real Groq and Sarvam keys:

```bash
uv run python scripts/live_api_test.py    # writes LIVE_TEST_RESULTS.md
```

[API_TESTS.md](API_TESTS.md) lists every endpoint, its test cases, the latest results, and the bugs this testing found.

## Simulate a customer's UPI payment

UPI payments are simulated. Khata Voice is a solution proposal for Paytm, so it has no live payment-platform integration.

```bash
curl -X POST localhost:8000/api/merchants/<id>/payments/incoming -H "Content-Type: application/json" \
  -d '{"payer_vpa":"ramesh.p@okaxis","amount_rupees":300}'
```

## Postgres

```bash
docker compose up --build
```
