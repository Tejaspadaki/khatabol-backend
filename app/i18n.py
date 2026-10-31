"""Fixed texts the merchant sees or hears, in each supported language.

Hindi ("hi") is Hinglish in Latin script, Marathi ("mr") is Marathi in Latin script, matching what the
LLM is asked to reply in. Hindi is the default for anything unknown.
"""

LANGS = ("hi", "mr", "en")
DEFAULT_LANG = "hi"

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
WEEKDAYS = {
    "hi": ["somvar", "mangalvar", "budhvar", "guruvar", "shukravar", "shanivar", "ravivar"],
    "mr": ["somvar", "mangalvar", "budhvar", "guruvar", "shukravar", "shanivar", "ravivar"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}

MESSAGES: dict[str, dict[str, str]] = {
    # ---- dates
    "today": {"hi": "aaj", "mr": "aaj", "en": "today"},
    "tomorrow": {"hi": "kal", "mr": "udya", "en": "tomorrow"},
    "day_after": {"hi": "parson", "mr": "parva", "en": "day after tomorrow"},
    # ---- balances
    "balance_due": {"hi": "ab {amount} baaki", "mr": "ata {amount} baaki", "en": "now {amount} due"},
    "balance_advance": {"hi": "{amount} advance jama hai", "mr": "{amount} advance jama aahe",
                        "en": "{amount} paid in advance"},
    "balance_clear": {"hi": "poora hisaab saaf", "mr": "sagla hishob purna", "en": "all settled"},
    # ---- UPI announcements
    "upi_applied": {"hi": "{name} ne {amount} UPI se diye. {balance}.",
                    "mr": "{name} ni {amount} UPI ne dile. {balance}.",
                    "en": "{name} paid {amount} by UPI. {balance}."},
    "upi_sale_from": {"hi": "{amount} prapt hue, {name} se.", "mr": "{name} kadun {amount} milale.",
                      "en": "Received {amount} from {name}."},
    "upi_unmatched": {"hi": "{amount} {vpa} se aaye. Kiska hai?", "mr": "{vpa} kadun {amount} aale. Kunache aahet?",
                      "en": "{amount} came from {vpa}. Whose is it?"},
    "upi_sale": {"hi": "{amount} prapt hue.", "mr": "{amount} milale.", "en": "Received {amount}."},
    "upi_assigned": {"hi": "{amount} {name} ke khate mein, {balance}", "mr": "{amount} {name} chya khatyat, {balance}",
                     "en": "{amount} added to {name}'s khata, {balance}"},
    # ---- scheduler
    "daily_summary": {
        "hi": "Aaj {credit} udhaar diya, {collected} wapas aaya. Kul {outstanding} baaki hai.",
        "mr": "Aaj {credit} udhaar dila, {collected} parat aale. Ekun {outstanding} baaki aahe.",
        "en": "Today you gave {credit} on credit and collected {collected}. Total {outstanding} is due.",
    },
    "reminders_sent": {"hi": "{n} customers ko reminder bhej diya.", "mr": "{n} customers na reminder pathavla.",
                       "en": "Sent reminders to {n} customers."},
    "reminder_message": {
        "hi": "Namaste {customer} ji, {shop} se yaad dila rahe hain: aapka {amount} baaki hai{due}. "
              "Yahan se pay karein: {link}",
        "mr": "Namaskar {customer} ji, {shop} kadun aathvan: tumche {amount} baaki aahet{due}. "
              "Ithe pay kara: {link}",
        "en": "Hello {customer}, a reminder from {shop}: {amount} is due{due}. Pay here: {link}",
    },
    "reminder_due": {"hi": " ({date} tak)", "mr": " ({date} paryant)", "en": " (by {date})"},
    # ---- agent: fixed replies
    "fallback": {"hi": "Maaf kijiye, samajh nahi aaya. Phir se boliye?",
                 "mr": "Maaf kara, samajla nahi. Punha bola?",
                 "en": "Sorry, I didn't understand. Could you say that again?"},
    "llm_down": {"hi": "Abhi assistant se baat nahi ho pa rahi. Thodi der mein try kijiye, ya app se entry kijiye.",
                 "mr": "Ata assistant shi bolta yet nahi. Thodya velane try kara, kiva app madhun entry kara.",
                 "en": "The assistant is not reachable right now. Try again shortly, or add the entry in the app."},
    "llm_busy": {"hi": "Abhi bahut zyada requests aa rahi hain. Ek minute baad boliye, ya app se entry kijiye.",
                 "mr": "Ata khup requests yet aahet. Ek minitane bola, kiva app madhun entry kara.",
                 "en": "Too many requests right now. Try again in a minute, or add the entry in the app."},
    "say_again": {"hi": "Phir se boliye?", "mr": "Punha bola?", "en": "Could you say that again?"},
    "not_saved_one": {"hi": "{n} entry save nahi ki. ", "mr": "{n} entry save keli nahi. ",
                      "en": "{n} entry was not saved. "},
    "not_saved_many": {"hi": "{n} entries save nahi ki. ", "mr": "{n} entries save kelya nahit. ",
                       "en": "{n} entries were not saved. "},
    "goodbye": {"hi": "Theek hai, phir milte hain.", "mr": "Theek aahe, punha bhetu.", "en": "Okay, see you later."},
    "cancelled": {"hi": "Theek hai, cancel kar diya.", "mr": "Theek aahe, cancel kela.", "en": "Okay, cancelled."},
    "ask_save": {"hi": "Save karun?", "mr": "Save karu?", "en": "Shall I save?"},
    "big_confirm": {"hi": "Bada amount hai: {summary}. Pakka save karun?",
                    "mr": "Motha amount aahe: {summary}. Nakki save karu?",
                    "en": "That's a big amount: {summary}. Save it for sure?"},
    "save_failed": {"hi": "Save nahi hua: {error}", "mr": "Save jhala nahi: {error}", "en": "Couldn't save: {error}"},
    "done": {"hi": "Ho gaya.", "mr": "Jhala.", "en": "Done."},
    # ---- agent: what got committed
    "did_open_khata": {"hi": "{name} ka khata khul gaya", "mr": "{name} che khate ughadle",
                       "en": "opened a khata for {name}"},
    "did_link_upi": {"hi": "{name} ki UPI ID jud gayi", "mr": "{name} cha UPI ID jodla", "en": "{name}'s UPI ID linked"},
    "did_send_reminders": {"hi": "{n} reminder bhej diye", "mr": "{n} reminder pathavle", "en": "sent {n} reminders"},
    "did_undo": {"hi": "entry cancel ho gayi, {name}: {balance}", "mr": "entry cancel jhali, {name}: {balance}",
                 "en": "entry cancelled, {name}: {balance}"},
    # ---- agent: proposals waiting for "haan"
    "sum_new_customer": {"hi": "naya customer {name}", "mr": "nava customer {name}", "en": "new customer {name}"},
    "sum_credit": {"hi": "{name} ko {amount} udhaar", "mr": "{name} la {amount} udhaar", "en": "{amount} credit to {name}"},
    "sum_due": {"hi": ", {date} tak", "mr": ", {date} paryant", "en": ", due {date}"},
    "sum_payment": {"hi": "{name} se {amount} {mode} mile", "mr": "{name} kadun {amount} {mode} milale",
                    "en": "{amount} {mode} received from {name}"},
    "mode_cash": {"hi": "cash", "mr": "cash", "en": "cash"},
    "mode_upi": {"hi": "UPI se", "mr": "UPI ne", "en": "UPI"},
    "mode_other": {"hi": "", "mr": "", "en": ""},
    "sum_link_upi": {"hi": "{name} ki UPI ID {vpa}", "mr": "{name} cha UPI ID {vpa}", "en": "UPI ID {vpa} for {name}"},
    "sum_reminder_one": {"hi": "{name} ko {amount} ka WhatsApp reminder", "mr": "{name} la {amount} cha WhatsApp reminder",
                         "en": "WhatsApp reminder to {name} for {amount}"},
    "sum_reminder_all": {"hi": "{n} customers ko reminder, kul {total}", "mr": "{n} customers na reminder, ekun {total}",
                         "en": "reminders to {n} customers, total {total}"},
    "sum_undo": {"hi": "pichhli entry cancel: {name} ka {amount} {kind}", "mr": "maagchi entry cancel: {name} cha {amount} {kind}",
                 "en": "cancel last entry: {name}'s {amount} {kind}"},
    "kind_credit": {"hi": "udhaar", "mr": "udhaar", "en": "credit"},
    "kind_payment": {"hi": "payment", "mr": "payment", "en": "payment"},
    "sum_assign": {"hi": "{amount} ({vpa}) {name} ke khate mein jama", "mr": "{amount} ({vpa}) {name} chya khatyat jama",
                   "en": "{amount} ({vpa}) credited to {name}'s khata"},
    # ---- errors shown in the app
    "err_merchant_not_found": {"hi": "Merchant nahi mila.", "mr": "Merchant sapadla nahi.", "en": "Shop not found."},
    "err_customer_name": {"hi": "Customer ka naam samajh nahi aaya.", "mr": "Customer che naav samajle nahi.",
                          "en": "Couldn't understand the customer's name."},
    "err_customer_not_found": {"hi": "Ye customer is khata mein nahi hai.", "mr": "Ha customer ya khatyat nahi.",
                               "en": "This customer is not in this khata."},
    "err_amount_positive": {"hi": "Amount zero se zyada hona chahiye.", "mr": "Amount zero peksha jasta hava.",
                            "en": "Amount must be more than zero."},
    "err_amount_too_big": {"hi": "Amount bahut bada hai, dobara check kijiye.", "mr": "Amount khup motha aahe, punha tapasa.",
                           "en": "Amount is too large, please check again."},
    "err_entry_not_found": {"hi": "Ye entry nahi mili.", "mr": "Hi entry sapadli nahi.", "en": "Entry not found."},
    "err_reverse_reversal": {"hi": "Reversal ko reverse nahi kar sakte.", "mr": "Reversal la reverse karta yet nahi.",
                             "en": "A reversal can't be reversed."},
    "err_entry_cancelled": {"hi": "Ye entry pehle hi cancel ho chuki hai.", "mr": "Hi entry adhich cancel jhali aahe.",
                            "en": "This entry is already cancelled."},
    "err_bad_upi": {"hi": "UPI ID sahi nahi lag rahi (jaise naam@bank).", "mr": "UPI ID barobar vatat nahi (jase naav@bank).",
                    "en": "That UPI ID doesn't look right (like name@bank)."},
    "err_bad_filter": {"hi": "Filter {filter} samajh nahi aaya.", "mr": "Filter {filter} samajla nahi.",
                       "en": "Unknown filter {filter}."},
    "err_bad_due": {"hi": "Due date '{due}' samajh nahi aayi.", "mr": "Due date '{due}' samajli nahi.",
                    "en": "Couldn't understand the due date '{due}'."},
    "err_payment_not_found": {"hi": "Ye payment nahi mila.", "mr": "Ha payment sapadla nahi.", "en": "Payment not found."},
    "err_payment_positive": {"hi": "Payment amount zero se zyada hona chahiye.", "mr": "Payment amount zero peksha jasta hava.",
                             "en": "Payment amount must be positive."},
    "err_payment_linked": {"hi": "Ye payment pehle hi khate mein jud chuka hai.", "mr": "Ha payment adhich khatyat jodla aahe.",
                           "en": "This payment is already in the khata."},
    "err_payment_no_entry": {"hi": "Is payment se khate mein koi entry nahi bani thi.",
                             "mr": "Ya payment mule khatyat kahi entry jhali navhti.",
                             "en": "This payment didn't create any khata entry."},
    "err_reminder_needs_upi": {"hi": "Reminder ke liye pehle apni UPI ID set kijiye.",
                               "mr": "Reminder sathi adhi tumcha UPI ID set kara.",
                               "en": "Set your shop's UPI ID before sending reminders."},
    "err_new_customer_failed": {"hi": "Naya customer save nahi hua, isliye entry bhi nahi ho sakti.",
                                "mr": "Nava customer save jhala nahi, mhanun entry pan hou shakat nahi.",
                                "en": "The new customer wasn't saved, so the entry can't be saved either."},
}


def lang_of(value: str | None) -> str:
    """'mr', 'en-IN', 'en-US,en;q=0.9' -> a supported language; anything else -> Hindi."""
    code = (value or "").split(",")[0].strip().lower()[:2]
    return code if code in LANGS else DEFAULT_LANG


def t(lang: str | None, key: str, **params) -> str:
    texts = MESSAGES[key]
    return texts.get(lang_of(lang), texts[DEFAULT_LANG]).format(**params)
