from app.db.models import UpiTxnStatus
from app.events import bus
from app.ledger import service as ledger
from app.payments import reconciler
from app.payments.upi_links import payment_note, ref_from_note, upi_intent_link


def _event(merchant, txn="T1", vpa="ramesh.p@okaxis", paise=30000, note=None):
    return reconciler.PaymentEvent(merchant.id, txn, vpa, paise, note=note)


async def _ramesh_with_dues(session, merchant, paise=50000):
    c = await ledger.create_customer(session, merchant.id, "Ramesh Patil")
    await ledger.add_credit(session, merchant.id, c.id, paise)
    await ledger.link_upi(session, merchant.id, c.id, "ramesh.p@okaxis")
    return c


async def test_linked_payer_auto_settles_and_is_idempotent(session, merchant):
    c = await _ramesh_with_dues(session, merchant)
    q = bus.subscribe(merchant.id)
    r = await reconciler.handle_payment(session, _event(merchant))
    await session.commit()
    r.publish()
    assert r.txn.status == UpiTxnStatus.APPLIED
    assert c.balance_paise == 20000
    assert r.announcement == "Ramesh Patil ne ₹300 UPI se diye. Ab ₹200 baaki."
    assert (await q.get())["type"] == "upi_applied"

    again = await reconciler.handle_payment(session, _event(merchant))
    assert again.duplicate and c.balance_paise == 20000
    bus.unsubscribe(merchant.id, q)


async def test_overpayment_announces_advance(session, merchant):
    c = await _ramesh_with_dues(session, merchant, paise=20000)
    r = await reconciler.handle_payment(session, _event(merchant, paise=30000))
    assert c.balance_paise == -10000
    assert "₹100 advance jama hai" in r.announcement


async def test_linked_payer_without_dues_is_a_sale(session, merchant):
    c = await ledger.create_customer(session, merchant.id, "Ramesh Patil")
    await ledger.link_upi(session, merchant.id, c.id, "ramesh.p@okaxis")
    r = await reconciler.handle_payment(session, _event(merchant))
    assert r.txn.status == UpiTxnStatus.SALE and c.balance_paise == 0


async def test_unknown_payer_is_asked_then_learned(session, merchant):
    sunita = await ledger.create_customer(session, merchant.id, "Sunita Devi")
    await ledger.add_credit(session, merchant.id, sunita.id, 45000)
    r = await reconciler.handle_payment(session, _event(merchant, "T2", "sunita99@ybl", 20000))
    assert r.txn.status == UpiTxnStatus.UNMATCHED
    assert r.announcement == "₹200 sunita99@ybl se aaye. Kiska hai?"
    assert [t.txn_id for t in await reconciler.list_unmatched(session, merchant.id)] == ["T2"]

    await reconciler.assign_payment(session, merchant.id, "T2", sunita.id)
    assert sunita.balance_paise == 25000

    # next payment from the same VPA auto-settles
    r = await reconciler.handle_payment(session, _event(merchant, "T3", "sunita99@ybl", 5000))
    assert r.txn.status == UpiTxnStatus.APPLIED and sunita.balance_paise == 20000


async def test_unknown_payer_with_no_dues_anywhere_is_a_sale(session, merchant):
    r = await reconciler.handle_payment(session, _event(merchant, vpa="stranger@ybl"))
    assert r.txn.status == UpiTxnStatus.SALE


async def test_reminder_link_ref_matches_and_links_vpa(session, merchant):
    c = await ledger.create_customer(session, merchant.id, "Mohan Lal")
    await ledger.add_credit(session, merchant.id, c.id, 25000)
    link = upi_intent_link("sharma@paytm", "Sharma General Store", 25000, c.ref_code)
    assert f"tn=KHATA-{c.ref_code}" in link and "am=250.00" in link
    assert ref_from_note(f"paid {payment_note(c.ref_code)}") == c.ref_code

    r = await reconciler.handle_payment(session, _event(merchant, "T4", "mohan@ybl", 25000, payment_note(c.ref_code)))
    assert r.txn.status == UpiTxnStatus.APPLIED and c.balance_paise == 0
    assert (await ledger.customer_for_vpa(session, merchant.id, "mohan@ybl")).id == c.id


async def test_undo_wrong_match_then_reassign(session, merchant):
    ramesh = await _ramesh_with_dues(session, merchant)
    sunita = await ledger.create_customer(session, merchant.id, "Sunita Devi")
    await ledger.add_credit(session, merchant.id, sunita.id, 40000)
    await reconciler.handle_payment(session, _event(merchant, "T5"))
    assert ramesh.balance_paise == 20000

    txn = await reconciler.undo_auto_payment(session, merchant.id, "T5")
    assert txn.status == UpiTxnStatus.UNMATCHED and ramesh.balance_paise == 50000
    assert await ledger.customer_for_vpa(session, merchant.id, "ramesh.p@okaxis") is None

    await reconciler.assign_payment(session, merchant.id, "T5", sunita.id)
    assert sunita.balance_paise == 10000
