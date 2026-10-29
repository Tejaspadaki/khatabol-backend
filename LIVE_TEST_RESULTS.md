# Live API test results

Run: 2026-10-30 00:15 against `http://127.0.0.1:8765` with real Groq + Sarvam.
Produced by `scripts/live_api_test.py`; case IDs match [API_TESTS.md](API_TESTS.md).

**37 / 37 passed.**

| Case | Endpoint | Check | Result | Time | Detail |
|---|---|---|---|---|---|
| SYS-01 | `GET /health` | 200 {ok:true} | PASS | 35 ms |  |
| SYS-02 | `GET /` | redirects to demo | PASS | 1 ms |  |
| DEM-01 | `POST /demo/seed` | 201, demo shop created | PASS | 44 ms | merchant 5cb513be |
| DEM-02 | `GET /demo/merchants` | newest first | PASS | 4 ms |  |
| DEM-03 | `GET /demo/simulate` | HTML console | PASS | 2 ms |  |
| MER-01 | `POST /api/merchants` | 201 | PASS | 5 ms |  |
| MER-04 | `GET /api/merchants/{m}` | 200 | PASS | 3 ms |  |
| MER-05 | `GET /api/merchants/{m}` | 404 unknown | PASS | 2 ms |  |
| CUS-01 | `POST .../customers` | 201, name normalized | PASS | 5 ms |  |
| CUS-05 | `GET .../customers` | 6 customers, sorted | PASS | 4 ms |  |
| CUS-06 | `GET .../customers?q=` | ambiguous / match | PASS | 6 ms |  |
| CUS-13 | `POST .../credit` | spoken due resolved | PASS | 7 ms | due 2026-11-05 |
| CUS-14 | `POST .../credit` | 400 bad due | PASS | 1 ms |  |
| CUS-17 | `POST .../payment` | balance ₹300 | PASS | 7 ms |  |
| CUS-19 | `POST .../upi` | VPA linked | PASS | 5 ms |  |
| CUS-08 | `GET .../customers/{c}` | balance, due, UPI | PASS | 5 ms |  |
| CUS-11 | `GET .../history` | newest first | PASS | 4 ms |  |
| CUS-22 | `POST .../entries/{e}/reverse` | reversed once only | PASS | 12 ms |  |
| CUS-10 | `GET .../customers/{c}` | other shop -> 404 | PASS | 2 ms |  |
| COL-01 | `GET .../dues?filter=today` | Ramesh Patil + Sunita | PASS | 9 ms | total ₹950 |
| COL-05 | `POST .../reminders` | 2 sent with UPI links | PASS | 12 ms |  |
| COL-08 | `POST .../reminders` | 422 no target | PASS | 1 ms |  |
| PAY-01/WS-01 | `POST .../payments/incoming + WS` | auto-settle + live event | PASS | 35 ms | Ramesh Patil ne ₹300 UPI se diye. Ab ₹200 baaki. |
| PAY-02 | `POST .../payments/incoming` | duplicate ignored | PASS | 6 ms |  |
| PAY-04 | `POST .../payments/incoming` | unknown payer asked | PASS | 7 ms | ₹200 sunita99@ybl se aaye. Kiska hai? |
| PAY-10 | `GET .../payments/unmatched` | 1 waiting | PASS | 3 ms |  |
| PAY-11 | `POST .../payments/{t}/assign` | resolved, VPA learned | PASS | 16 ms | ₹200 Sunita Devi ke khate mein, ab ₹250 baaki. |
| PAY-13 | `POST .../payments/{t}/undo` | reversed, back to queue | PASS | 13 ms |  |
| COL-10 | `GET .../summary` | spoken summary | PASS | 5 ms | Aaj ₹500 udhaar diya, ₹200 wapas aaya. Kul ₹2,700 baaki hai. |
| AGT-16 [AI] | `POST .../tts` | Sarvam bulbul WAV | PASS | 1879 ms | 214734 bytes WAV |
| AGT-01 [AI] | `POST .../agent/message` | balance answered from tools | PASS | 1681 ms | Ramesh Patil ka ₹500 baaki hai, jo 28 October ko due tha aur ab overdue hai. Aakhri payment ₹200 thi 21 October ko.  (tools: get_balance) |
| AGT-02a [AI] | `POST .../agent/message` | credit proposed, not saved | PASS | 612 ms | Mohan Lal ko ₹120 udhaar (saamaan), kal tak. Save karun? |
| AGT-02b | `POST .../agent/message` | 'haan' commits (no LLM) | PASS | 16 ms | Ho gaya. Mohan Lal: ab ₹370 baaki. |
| AGT-03 [AI] | `POST .../agent/message` | multi-intent, then 'nahi' | PASS | 17171 ms | Ramesh Sharma ko ₹100 udhaar (saamaan). Sunita Devi se ₹50 cash mile. Save karun? |
| AGT-08 [AI] | `POST .../agent/message speak` | reply + Sarvam audio | PASS | 52570 ms | Aaj ₹750 aana hai. Ramesh Patil ₹500 (overdue, 28 Oct) aur Sunita Devi ₹250.  (+358394 bytes audio) |
| AGT-10 [AI] | `POST .../agent/voice` | STT -> agent -> TTS | PASS | 33422 ms | heard 'Sunita Devi ka kitna baaki hai?' -> 'Sunita Devi ka ₹250 baaki hai, aur woh aaj due hai. Aaj hi ₹200 payment bhi aayi thi.' |
| AGT-13 | `POST .../agent/voice` | 400 empty clip | PASS | 3 ms |  |
