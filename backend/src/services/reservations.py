"""Online reservation enablement and reservation lifecycle.

Locking protocol (all business writers follow settings -> sample -> reservation):

* Transfer creation / inventory adjustment take ``SELECT ... FOR SHARE`` on the
  single ``reservation_settings`` row and then ``FOR UPDATE`` on the sample.
* An enablement batch takes ``SELECT ... FOR UPDATE`` on the settings row and
  then locks every sample in id order.  That is the migration cut: writers that
  started before the cut commit first and are included by the batch; writers
  that start after the cut wait, then read ``mode='on'`` and reserve their own
  application.  No application is missed twice.
* A single PostgreSQL advisory lock makes an enablement batch single-flight.
  It is session scoped, so a killed process releases it immediately and a retry
  can resume the interrupted batch.
* Confirm and cancel lock the sample and then the reservation, so exactly one
  terminal status wins; the loser receives 409.
"""
import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from src.config.database import Session, engine
from src.models.entities import (
    Audit,
    Reservation,
    ReservationRun,
    ReservationSettings,
    Sample,
    Transfer,
)
from src.utils.serialization import record

log = logging.getLogger(__name__)

ADVISORY_KEY = 0x52534552  # "RSER"
_locks = threading.local()

RESERVATION_STATUS_LABELS = {
    'reserved': '预留中',
    'confirmed': '已确认扣库存',
    'cancelled': '已撤销',
}
RUN_STATUS_LABELS = {
    'pending': '待启用',
    'processing': '处理中',
    'active': '已生效',
    'failed': '失败',
}


class _Shortage(Exception):
    def __init__(self, message, shortages):
        super().__init__(message)
        self.shortages = shortages


class _Conflict(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def content_hash(code, note='', content=''):
    """Identity of a batch: its code plus the enablement content it applies.

    ``note`` is an operator label and may differ between retries; only a change
    to the actual content makes the same code a different request.
    """
    payload = {'code': code, 'content': content or ''}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


def bootstrap():
    """Create the single settings row outside a request scope."""
    with Session() as db:
        if db.get(ReservationSettings, 1) is None:
            try:
                db.add(ReservationSettings(id=1, mode='off'))
                db.commit()
            except IntegrityError:
                db.rollback()


def _ensure_settings(db):
    """Return the settings row locked FOR SHARE, creating it if necessary.

    The FOR SHARE lock is the online cut with an enablement batch, which holds
    FOR UPDATE on the same row.  ``populate_existing`` matters: when this select
    waits for an activation to commit, the identity map must be refreshed so the
    caller observes ``mode='on'`` (otherwise the application would slip between
    the batch and the online path and be missed).
    """
    row = db.scalar(
        select(ReservationSettings)
        .where(ReservationSettings.id == 1)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if row is None:
        try:
            db.execute(
                pg_insert(ReservationSettings)
                .values(id=1, mode='off')
                .on_conflict_do_nothing(index_elements=['id'])
            )
            db.commit()
        except IntegrityError:
            db.rollback()
        row = db.scalar(
            select(ReservationSettings)
            .where(ReservationSettings.id == 1)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    return row


def _mode(db):
    row = db.get(ReservationSettings, 1)
    return row.mode if row else 'off'


def _reserved_total(db, sample_id, exclude_id=None):
    query = select(func.coalesce(func.sum(Reservation.quantity), 0)).where(
        Reservation.sample_id == sample_id,
        Reservation.status == 'reserved',
    )
    if exclude_id is not None:
        query = query.where(Reservation.id != exclude_id)
    return int(db.scalar(query) or 0)


def _read_run(run_id):
    with Session() as db:
        run = db.get(ReservationRun, run_id)
        return record(run) if run else None


# ---------------------------------------------------------------- advisory lock
def _try_lock():
    conn = engine.connect()
    got = conn.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': ADVISORY_KEY}).scalar()
    if not got:
        conn.close()
        return False
    _locks.conn = conn
    return True


def _unlock():
    conn = getattr(_locks, 'conn', None)
    _locks.conn = None
    if conn is not None:
        try:
            conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': ADVISORY_KEY})
        finally:
            conn.close()


# -------------------------------------------------------------------- queries
def listing(db):
    rows = db.execute(
        select(Reservation, Sample.code, Transfer.code, Transfer.destination)
        .join(Sample, Sample.id == Reservation.sample_id)
        .join(Transfer, Transfer.id == Reservation.transfer_id)
        .order_by(Reservation.id)
    ).all()
    items = []
    for reservation, sample_code, transfer_code, destination in rows:
        item = record(reservation)
        item.update(
            sample_code=sample_code,
            transfer_code=transfer_code,
            destination=destination,
            status_label=RESERVATION_STATUS_LABELS.get(reservation.status, reservation.status),
        )
        items.append(item)
    return items


def summary(db):
    settings = db.get(ReservationSettings, 1)
    runs = list(db.scalars(select(ReservationRun).order_by(ReservationRun.id.desc())))
    active = next((run for run in runs if run.status == 'active'), None)
    current = active or (runs[0] if runs else None)
    counts = {'reserved': 0, 'confirmed': 0, 'cancelled': 0}
    for status, total in db.execute(
        select(Reservation.status, func.count()).group_by(Reservation.status)
    ):
        counts[status] = int(total)
    reserved_by_sample = dict(
        db.execute(
            select(Reservation.sample_id, func.coalesce(func.sum(Reservation.quantity), 0))
            .where(Reservation.status == 'reserved')
            .group_by(Reservation.sample_id)
        ).all()
    )
    samples = []
    for sample in db.scalars(select(Sample).order_by(Sample.id)):
        reserved = int(reserved_by_sample.get(sample.id, 0) or 0)
        samples.append(
            {
                **record(sample),
                'reserved': reserved,
                'available': sample.quantity - reserved,
            }
        )
    run = record(current) if current else None
    if run:
        run['status_label'] = RUN_STATUS_LABELS.get(run['status'], run['status'])
    return {
        'mode': settings.mode if settings else 'off',
        'run': run,
        'runs': [{**record(item), 'status_label': RUN_STATUS_LABELS.get(item.status, item.status)} for item in runs[:20]],
        'counts': counts,
        'samples': samples,
    }


# ------------------------------------------------------------------ enablement
def enable(db, data, user):
    code = (data.code or getattr(data, 'batch_code', None) or '').strip()
    if not code:
        raise HTTPException(422, '启用批次编号不能为空')
    note = data.note or ''
    content = getattr(data, 'content', '') or ''
    digest = content_hash(code, note, content)

    run = db.scalar(select(ReservationRun).where(ReservationRun.code == code))
    if run is not None and run.content_hash != digest:
        raise HTTPException(409, f'批次 {code} 已存在且启用内容不同，拒绝重复启用')
    if run is not None and run.status == 'active':
        # Idempotent retry: never create a second set of reservations.
        return _read_run(run.id)

    if run is None:
        if _mode(db) == 'on':
            raise HTTPException(409, '预留流程已生效，无需再次启用')
        run = ReservationRun(
            code=code,
            status='pending',
            content_hash=digest,
            content=content,
            note=note,
            created_by=getattr(user, 'email', ''),
        )
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            # Another request created the same batch concurrently.
            db.rollback()
            run = db.scalar(select(ReservationRun).where(ReservationRun.code == code))
            if run is None:
                raise HTTPException(409, '启用批次编号冲突，请重试')
            if run.content_hash != digest:
                raise HTTPException(409, f'批次 {code} 已存在且启用内容不同，拒绝重复启用')
            if run.status == 'active':
                return _read_run(run.id)

    if not _try_lock():
        # A live request owns the batch.  Wait briefly so concurrent callers see
        # the terminal state; if another batch wins the switch, concede instead
        # of leaving a batch that can never apply.
        return _wait_or_concede(run.id)

    try:
        _process(run.id)
    finally:
        _unlock()
    return _read_run(run.id)


def _wait_or_concede(run_id, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = _read_run(run_id)
        if current['status'] in ('active', 'failed'):
            return current
        if _other_batch_active(run_id):
            _concede(run_id)
            return _read_run(run_id)
        time.sleep(0.05)
    current = _read_run(run_id)
    current['concurrent_wait'] = True
    return current


def _other_batch_active(run_id):
    """True when a different batch already switched the flow on."""
    with Session() as db:
        row = db.get(ReservationSettings, 1)
        if row is None or row.mode != 'on':
            return False
        other = db.scalar(select(ReservationRun).where(
            ReservationRun.status == 'active', ReservationRun.id != run_id))
        return other is not None


def _concede(run_id):
    try:
        with Session() as db:
            run = db.execute(
                select(ReservationRun).where(ReservationRun.id == run_id).with_for_update()
            ).scalar_one()
            if run.status in ('pending', 'processing'):
                run.status = 'failed'
                run.error = '预留流程已由其他批次生效，本批次未重复占量'
                run.updated_at = _now()
                db.commit()
    except Exception:
        log.exception('could not persist reservation enablement concession')


def _process(run_id):
    with Session() as db:
        run = db.execute(
            select(ReservationRun).where(ReservationRun.id == run_id).with_for_update()
        ).scalar_one()
        if run.status == 'active':
            return
        # pending, failed (retry after correction) or an interrupted
        # processing run whose advisory lock was released by the crash.
        run.status = 'processing'
        run.error = ''
        run.result = None
        run.updated_at = _now()
        db.commit()
    try:
        _activate(run_id)
    except _Shortage as exc:
        _fail(run_id, str(exc), {'shortages': exc.shortages})
    except _Conflict as exc:
        _fail(run_id, str(exc), {})
    except Exception as exc:  # keep the reason visible on the page
        log.exception('reservation enablement failed')
        _fail(run_id, f'启用失败：{exc}', {})


def _activate(run_id):
    with Session() as db:
        settings = db.execute(
            select(ReservationSettings).where(ReservationSettings.id == 1).with_for_update()
        ).scalar_one_or_none()
        if settings is None:
            settings = ReservationSettings(id=1, mode='off')
            db.add(settings)
            db.flush()
        run = db.execute(
            select(ReservationRun).where(ReservationRun.id == run_id).with_for_update()
        ).scalar_one()
        if run.status == 'active':
            db.rollback()
            return
        if settings.mode == 'on':
            raise _Conflict('预留流程已由其他批次生效，本批次未重复占量')

        samples = list(db.scalars(select(Sample).order_by(Sample.id).with_for_update()))
        transfers = list(db.scalars(select(Transfer).order_by(Transfer.id)))
        totals = defaultdict(int)
        for transfer in transfers:
            totals[transfer.sample_id] += transfer.quantity

        shortages = []
        for sample in samples:
            requested = totals.get(sample.id, 0)
            if requested > sample.quantity:
                shortages.append(
                    {
                        'sample_id': sample.id,
                        'sample_code': sample.code,
                        'quantity': sample.quantity,
                        'requested': requested,
                        'missing': requested - sample.quantity,
                    }
                )
        if shortages:
            detail = '；'.join(
                f"{item['sample_code']} 已有申请 {item['requested']} 份，库存 {item['quantity']} 份，缺 {item['missing']} 份"
                for item in shortages
            )
            raise _Shortage(f'整批未生效：{detail}。请修正库存或申请后以同一编号重试', shortages)

        existing = set(db.scalars(select(Reservation.transfer_id)))
        created = 0
        for transfer in transfers:
            if transfer.id in existing:
                continue
            db.add(
                Reservation(
                    code=transfer.code,
                    transfer_id=transfer.id,
                    sample_id=transfer.sample_id,
                    quantity=transfer.quantity,
                    status='reserved',
                    run_id=run_id,
                )
            )
            created += 1
        db.flush()

        settings.mode = 'on'
        settings.updated_at = _now()
        run.status = 'active'
        run.error = ''
        run.application_count = len(transfers)
        run.reserved_count = len(transfers)
        run.snapshot_max_transfer_id = max((transfer.id for transfer in transfers), default=0)
        run.result = {
            'mode': 'on',
            'application_count': len(transfers),
            'reservations_created': created,
            'samples': [
                {
                    'sample_code': sample.code,
                    'quantity': sample.quantity,
                    'reserved': totals.get(sample.id, 0),
                }
                for sample in samples
                if totals.get(sample.id, 0)
            ],
            'formula': '已有申请按原编号全量纳入；各样本预留合计不超过库存；同编号同内容重试不重复占量',
        }
        run.updated_at = _now()
        db.commit()


def _fail(run_id, message, details):
    try:
        with Session() as db:
            run = db.execute(
                select(ReservationRun).where(ReservationRun.id == run_id).with_for_update()
            ).scalar_one()
            if run.status == 'processing':
                run.status = 'failed'
                run.error = message
                run.result = details or {}
                run.updated_at = _now()
                db.commit()
    except Exception:
        log.exception('could not persist reservation enablement failure')


def retry(db, run_id, user):
    """Resume an interrupted / failed batch with its stored code and content."""
    run = db.get(ReservationRun, run_id)
    if not run:
        raise HTTPException(404, '启用批次不存在')
    data = type('StoredEnable', (), {
        'code': run.code,
        'note': run.note or '',
        'content': run.content or '',
    })()
    return enable(db, data, user)


# ------------------------------------------------------------------ lifecycle
def _terminal_conflict(status):
    label = RESERVATION_STATUS_LABELS.get(status, status)
    raise HTTPException(409, f'该预留已是终态（{label}），不能重复办理')


def confirm(db, reservation_id, user):
    item = db.get(Reservation, reservation_id)
    if not item:
        raise HTTPException(404, '预留记录不存在')
    _ensure_settings(db)  # same lock order as registration and inventory writes
    sample = db.execute(
        select(Sample).where(Sample.id == item.sample_id).with_for_update()
    ).scalar_one_or_none()
    if not sample:
        raise HTTPException(404, '样本不存在')
    item = db.execute(
        select(Reservation).where(Reservation.id == reservation_id).with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if item.status != 'reserved':
        db.rollback()
        _terminal_conflict(item.status)
    if sample.quantity < item.quantity:
        db.rollback()
        raise HTTPException(409, '样本可用库存不足，无法确认扣库存')
    sample.quantity -= item.quantity
    item.status = 'confirmed'
    item.updated_at = _now()
    db.add(Audit(actor=user.email, action=f'确认扣库存 {item.code}'))
    db.commit()
    remaining = _reserved_total(db, sample.id)
    result = record(item)
    result.update(status_label=RESERVATION_STATUS_LABELS['confirmed'])
    result.update(sample_quantity=sample.quantity, sample_available=sample.quantity - remaining)
    return result


def cancel(db, reservation_id, user):
    item = db.get(Reservation, reservation_id)
    if not item:
        raise HTTPException(404, '预留记录不存在')
    _ensure_settings(db)  # same lock order as registration and inventory writes
    sample = db.execute(
        select(Sample).where(Sample.id == item.sample_id).with_for_update()
    ).scalar_one_or_none()
    if not sample:
        raise HTTPException(404, '样本不存在')
    item = db.execute(
        select(Reservation).where(Reservation.id == reservation_id).with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if item.status != 'reserved':
        db.rollback()
        _terminal_conflict(item.status)
    item.status = 'cancelled'
    item.updated_at = _now()
    db.add(Audit(actor=user.email, action=f'撤销预留 {item.code}'))
    db.commit()
    remaining = _reserved_total(db, sample.id)
    result = record(item)
    result.update(status_label=RESERVATION_STATUS_LABELS['cancelled'])
    result.update(sample_quantity=sample.quantity, sample_available=sample.quantity - remaining)
    return result
