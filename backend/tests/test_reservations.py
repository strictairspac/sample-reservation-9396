"""Reservation flow acceptance tests against a real PostgreSQL database.

Run with a disposable database (the tests truncate business tables):

    cd backend && TEST_DATABASE_URL=postgresql+psycopg://... pytest tests -q
"""
import threading
import time
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, text

from src.config.database import Session, engine
from src.models.entities import Reservation, ReservationRun, ReservationSettings, Sample, Transfer, User
from src.schemas.reservation import ReservationEnableInput
from src.schemas.sample import SampleInput
from src.schemas.transfer import TransferInput
from src.services import reservations as svc
from src.services import samples as sample_svc
from src.services import transfers as transfer_svc


def user():
    with Session() as db:
        return db.scalar(select(User).where(User.email == 'operator@qingyu.test'))


def enable(code='RSV-T1', note='', content=''):
    with Session() as db:
        return svc.enable(db, ReservationEnableInput(code=code, note=note, content=content), user())


def reservation(code):
    with Session() as db:
        return db.scalar(select(Reservation).where(Reservation.code == code))


def count(db, model, *where):
    return int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


@contextmanager
def pause_sql(fragments, reached, release):
    """Hold the current thread right after a statement containing all fragments.

    Runs on the caller's thread only, so a test can create a deterministic
    interleaving without touching production code.
    """
    me = threading.current_thread()
    if isinstance(fragments, str):
        fragments = (fragments,)

    def listener(conn, cursor, statement, parameters, context, executemany):
        if threading.current_thread() is me and all(f in statement for f in fragments):
            reached.set()
            assert release.wait(10), 'test did not release the paused transaction'

    event.listen(engine, 'after_cursor_execute', listener)
    try:
        yield
    finally:
        event.remove(engine, 'after_cursor_execute', listener)


# --------------------------------------------------------------- batch basics
def test_enable_reserves_every_existing_application_by_original_code():
    result = enable()
    assert result['status'] == 'active'
    with Session() as db:
        app_codes = {t.code for t in db.scalars(select(Transfer))}
        reservations = list(db.scalars(select(Reservation)))
        assert {r.code for r in reservations} == app_codes
        transfer_by_code = {t.code: t for t in db.scalars(select(Transfer))}
        for item in reservations:
            assert item.quantity == transfer_by_code[item.code].quantity
            assert item.transfer_id == transfer_by_code[item.code].id
            assert item.status == 'reserved'
        assert db.get(ReservationSettings, 1).mode == 'on'


def test_shortage_rejects_whole_batch_then_same_code_retry_recovers():
    with Session() as db:
        sample = db.scalar(select(Sample).where(Sample.code == 'QY-261117-A'))
        item = db.scalar(select(Transfer).where(Transfer.code == 'MOVE-1117-01'))
        item.quantity = sample.quantity + 5  # 125 > 120
        db.commit()
    result = enable()
    assert result['status'] == 'failed'
    assert result['error']
    assert result['result']['shortages'][0]['sample_code'] == 'QY-261117-A'
    with Session() as db:
        assert count(db, Reservation) == 0
        assert db.get(ReservationSettings, 1).mode == 'off'

    with Session() as db:
        item = db.scalar(select(Transfer).where(Transfer.code == 'MOVE-1117-01'))
        item.quantity = 30
        db.commit()
    retried = enable()
    assert retried['status'] == 'active'
    assert retried['id'] == result['id']
    with Session() as db:
        assert count(db, Reservation, Reservation.code == 'MOVE-1117-01') == 1


def test_same_code_same_content_retry_does_not_double_reserve():
    first = enable()
    with Session() as db:
        total = count(db, Reservation)
        reserved = db.scalar(select(func.coalesce(func.sum(Reservation.quantity), 0)))
    second = enable()
    assert second['id'] == first['id']
    with Session() as db:
        assert count(db, Reservation) == total
        assert db.scalar(select(func.coalesce(func.sum(Reservation.quantity), 0))) == reserved


def test_same_code_changed_content_is_rejected():
    enable()
    with pytest.raises(HTTPException) as exc:
        enable(content='different')
    assert exc.value.status_code == 409
    with Session() as db:
        assert count(db, Reservation) == 3


def test_same_code_with_only_note_changed_retry_is_idempotent():
    enable(note='first label')
    result = enable(note='second label')
    assert result['status'] == 'active'
    with Session() as db:
        assert count(db, ReservationRun) == 1
        assert count(db, Reservation) == 3


def test_concurrent_different_batches_only_one_applies():
    codes = ['RSV-A', 'RSV-B', 'RSV-C']
    barrier = threading.Barrier(len(codes))
    outcomes = []

    def act(code):
        barrier.wait()
        try:
            with Session() as db:
                outcomes.append((code, svc.enable(db, ReservationEnableInput(code=code), user())['status']))
        except HTTPException as exc:
            outcomes.append((code, exc.status_code))

    threads = [threading.Thread(target=act, args=(code,)) for code in codes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert [status for _, status in outcomes].count('active') == 1
    with Session() as db:
        assert count(db, Reservation) == 3
        assert [r.status for r in db.scalars(select(ReservationRun))].count('active') == 1


# ------------------------------------- applications & stock during enablement
def test_new_application_after_enable_is_reserved_once():
    enable()
    data = TransferInput(code='AFTER-1', sample_id=1, destination='理化检测室', quantity=10)
    with Session() as db:
        created = transfer_svc.create(db, data, user())
    assert created['reservation']['quantity'] == 10
    with Session() as db:
        assert count(db, Reservation, Reservation.code == 'AFTER-1') == 1


def test_application_committed_before_cut_is_included_by_batch_only():
    """Application holds the settings share lock, batch waits, application commits."""
    reached, release = threading.Event(), threading.Event()
    errors = []

    def applier():
        try:
            with pause_sql(('reservation_settings.id', 'FOR SHARE'), reached, release), Session() as db:
                transfer_svc.create(
                    db,
                    TransferInput(code='CUT-1', sample_id=2, destination='金属分析室', quantity=5),
                    user(),
                )
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=applier)
    thread.start()
    assert reached.wait(5)
    batch_outcome = {}

    def batch():
        batch_outcome.update(enable())

    batch_thread = threading.Thread(target=batch)
    batch_thread.start()
    time.sleep(0.3)  # batch blocks on the settings row held by the applier
    assert batch_thread.is_alive()
    release.set()
    thread.join(10)
    batch_thread.join(10)
    assert not errors
    assert batch_outcome['status'] == 'active'
    with Session() as db:
        assert count(db, Reservation, Reservation.code == 'CUT-1') == 1
        assert count(db, Reservation) == 4


def test_application_started_after_cut_is_reserved_online_only():
    """Batch holds the settings exclusive lock; a later application waits and reads mode=on."""
    reached, release = threading.Event(), threading.Event()
    batch_outcome = {}
    errors = []

    def batch():
        try:
            with pause_sql(('reservation_settings.id', 'FOR UPDATE'), reached, release):
                batch_outcome.update(enable())
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=batch)
    thread.start()
    assert reached.wait(5)

    def applier():
        try:
            with Session() as db:
                transfer_svc.create(
                    db,
                    TransferInput(code='AFTER-CUT', sample_id=2, destination='金属分析室', quantity=5),
                    user(),
                )
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    applier_thread = threading.Thread(target=applier)
    applier_thread.start()
    time.sleep(0.3)  # applier blocks on the settings share lock
    assert applier_thread.is_alive()
    release.set()
    thread.join(15)
    applier_thread.join(15)
    assert not errors
    assert batch_outcome['status'] == 'active'
    with Session() as db:
        # The batch took a snapshot before this application; the application
        # reserved itself on the online path.  Exactly one reservation.
        assert count(db, Reservation, Reservation.code == 'AFTER-CUT') == 1
        assert count(db, Reservation) == 4
        reserved = db.scalar(
            select(func.coalesce(func.sum(Reservation.quantity), 0))
            .where(Reservation.sample_id == 2, Reservation.status == 'reserved')
        )
        assert reserved <= db.get(Sample, 2).quantity


def test_stock_reduction_below_reserved_is_rejected():
    enable()
    with Session() as db:
        reserved = svc._reserved_total(db, 1)
        assert db.get(Sample, 1).quantity > reserved
    with pytest.raises(HTTPException) as exc:
        with Session() as db:
            sample = db.get(Sample, 1)
            sample_svc.update(
                db, 1,
                SampleInput(code=sample.code, title=sample.title, quantity=reserved - 1,
                            location=sample.location),
                user(),
            )
    assert exc.value.status_code == 409
    with Session() as db:
        assert db.get(Sample, 1).quantity == 120


def test_stock_increase_stays_available():
    enable()
    with Session() as db:
        sample = db.get(Sample, 1)
        sample_svc.update(
            db, 1,
            SampleInput(code=sample.code, title=sample.title, quantity=150, location=sample.location),
            user(),
        )
    with Session() as db:
        summary = svc.summary(db)
    row = next(s for s in summary['samples'] if s['id'] == 1)
    assert row['available'] == 150 - row['reserved']


# ------------------------------------------------------- confirm and cancel
def test_confirm_deducts_stock_once():
    enable()
    item = reservation('MOVE-1117-02')
    with Session() as db:
        result = svc.confirm(db, item.id, user())
    assert result['status'] == 'confirmed'
    assert result['sample_quantity'] == 60
    with Session() as db:
        assert db.get(Sample, 2).quantity == 60
        with pytest.raises(HTTPException) as exc:
            svc.confirm(db, item.id, user())
        assert exc.value.status_code == 409


def test_cancel_releases_reservation_and_blocks_confirm():
    enable()
    item = reservation('MOVE-1117-03')
    with Session() as db:
        result = svc.cancel(db, item.id, user())
    assert result['status'] == 'cancelled'
    assert result['sample_available'] == 60
    with Session() as db:
        with pytest.raises(HTTPException) as exc:
            svc.confirm(db, item.id, user())
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException) as second:
            svc.cancel(db, item.id, user())
        assert second.value.status_code == 409


def test_concurrent_confirm_and_cancel_have_single_terminal_state():
    enable()
    item = reservation('MOVE-1117-01')
    barrier = threading.Barrier(2)
    outcomes = []

    def act(method):
        barrier.wait()
        try:
            with Session() as db:
                method(db, item.id, user())
            outcomes.append('ok')
        except HTTPException as exc:
            outcomes.append(exc.status_code)

    threads = [threading.Thread(target=act, args=(svc.confirm,)),
               threading.Thread(target=act, args=(svc.cancel,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert outcomes.count('ok') == 1
    assert outcomes.count(409) == 1
    with Session() as db:
        final = db.get(Reservation, item.id)
        assert final.status in ('confirmed', 'cancelled')
        sample = db.get(Sample, 1)
        if final.status == 'confirmed':
            assert sample.quantity == 120 - 30
        else:
            assert sample.quantity == 120


# -------------------------------------------------- concurrent enablement
def test_concurrent_enable_requests_create_one_batch():
    outcomes = []
    barrier = threading.Barrier(4)

    def act():
        barrier.wait()
        try:
            with Session() as db:
                result = svc.enable(db, ReservationEnableInput(code='RSV-RACE'), user())
            outcomes.append(result['status'])
        except HTTPException as exc:
            outcomes.append(exc.status_code)

    threads = [threading.Thread(target=act) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    with Session() as db:
        runs = list(db.scalars(select(ReservationRun)))
        assert len(runs) == 1
        assert runs[0].status == 'active'
        assert count(db, Reservation) == 3
        assert db.get(ReservationSettings, 1).mode == 'on'


def test_retry_endpoint_resumes_failed_batch_with_stored_content():
    with Session() as db:
        db.add(ReservationRun(code='RSV-FAILED', status='failed', content_hash=svc.content_hash('RSV-FAILED'),
                              content='', error='previous failure'))
        db.commit()
        run_id = db.scalar(select(ReservationRun.id).where(ReservationRun.code == 'RSV-FAILED'))
    with Session() as db:
        result = svc.retry(db, run_id, user())
    assert result['status'] == 'active'
    with Session() as db:
        assert db.get(ReservationRun, run_id).status == 'active'
        assert count(db, Reservation) == 3


def test_interrupted_enablement_can_be_resumed():
    """A crashed processor leaves processing + advisory lock released; retry resumes."""
    with Session() as db:
        db.add(ReservationRun(code='RSV-CRASH', status='processing',
                              content_hash=svc.content_hash('RSV-CRASH')))
        db.commit()
    result = enable(code='RSV-CRASH')
    assert result['status'] == 'active'
    with Session() as db:
        assert count(db, Reservation) == 3


def test_second_batch_after_active_is_rejected():
    enable()
    with pytest.raises(HTTPException) as exc:
        enable(code='RSV-OTHER')
    assert exc.value.status_code == 409


def test_empty_application_book_enables_with_zero_reservations():
    with engine.begin() as conn:
        conn.execute(text('TRUNCATE transfers RESTART IDENTITY CASCADE'))
    result = enable()
    assert result['status'] == 'active'
    with Session() as db:
        assert count(db, Reservation) == 0
        assert db.get(ReservationSettings, 1).mode == 'on'


# --------------------------------------------- application registration rules
def test_transfer_retry_same_content_is_idempotent_and_changed_content_rejected():
    with Session() as db:
        first = transfer_svc.create(
            db, TransferInput(code='IDEM-1', sample_id=1, destination='理化检测室', quantity=12), user())
    with Session() as db:
        again = transfer_svc.create(
            db, TransferInput(code='IDEM-1', sample_id=1, destination='理化检测室', quantity=12), user())
    assert again['id'] == first['id']
    with Session() as db:
        assert count(db, Transfer, Transfer.code == 'IDEM-1') == 1
    with pytest.raises(HTTPException) as exc:
        with Session() as db:
            transfer_svc.create(
                db, TransferInput(code='IDEM-1', sample_id=1, destination='复核室', quantity=12), user())
    assert exc.value.status_code == 409


def test_stock_down_and_application_concurrently_keep_available_non_negative():
    enable()
    outcomes = []
    barrier = threading.Barrier(2)
    with Session() as db:
        reserved = svc._reserved_total(db, 3)
        stock = db.get(Sample, 3).quantity

    def downsize():
        barrier.wait()
        try:
            with Session() as db:
                sample = db.get(Sample, 3)
                sample_svc.update(db, 3, SampleInput(code=sample.code, title=sample.title,
                                                     quantity=stock - 10, location=sample.location), user())
            outcomes.append('down')
        except HTTPException as exc:
            outcomes.append(exc.status_code)

    def apply():
        barrier.wait()
        try:
            with Session() as db:
                transfer_svc.create(db, TransferInput(code='RACE-STOCK', sample_id=3,
                                                     destination='复核室', quantity=20), user())
            outcomes.append('app')
        except HTTPException as exc:
            outcomes.append(exc.status_code)

    threads = [threading.Thread(target=downsize), threading.Thread(target=apply)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    with Session() as db:
        sample = db.get(Sample, 3)
        reserved_after = svc._reserved_total(db, 3)
        assert sample.quantity - reserved_after >= 0
        for item in db.scalars(select(Reservation).where(Reservation.status == 'reserved')):
            assert item.quantity > 0
    assert 'app' in outcomes or 422 in outcomes


def test_original_concurrency_trial_still_works_after_enablement():
    from src.schemas.trial import TrialInput
    from src.services import trials as trial_svc
    enable()
    with Session() as db:
        result = trial_svc.create(
            db, TrialInput(name='drill-after-enable', transfer_id=1, timeout_ms=250), user())
    assert result['status'] == 'queued'
    with Session() as db:
        assert trial_svc.detail(db, result['id'])['status'] == 'queued'
