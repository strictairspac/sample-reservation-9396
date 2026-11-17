from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.models.entities import Transfer,Sample,Reservation,Audit
from src.utils.serialization import record
from . import reservations


def listing(db): return [record(x) for x in db.scalars(select(Transfer).order_by(Transfer.id))]


def _existing(db, data):
    """Idempotent retry by application code: same content is a no-op, different content is rejected."""
    current = db.scalar(select(Transfer).where(Transfer.code == data.code))
    if current is None:
        return None
    same = (current.sample_id == data.sample_id and current.destination == data.destination
            and current.quantity == data.quantity and (current.note or '') == (data.note or ''))
    if not same:
        raise HTTPException(409, f'申请编号 {data.code} 已存在且内容不同，拒绝覆盖')
    result = record(current)
    item = db.scalar(select(Reservation).where(Reservation.transfer_id == current.id))
    if item is not None:
        result['reservation'] = record(item)
    return result


def create(db, data, user):
    # Application registration and reservation happen in one transaction, so a
    # request committed before the activation cut is included in the batch and
    # a request after it gets its own reservation.  Never both, never neither.
    settings = reservations._ensure_settings(db)
    sample = db.execute(select(Sample).where(Sample.id == data.sample_id).with_for_update()).scalar_one_or_none()
    if not sample: raise HTTPException(404, '样本不存在')
    existing = _existing(db, data)
    if existing is not None:
        db.rollback()
        return existing
    reserved = reservations._reserved_total(db, sample.id) if settings.mode == 'on' else 0
    if data.quantity > sample.quantity - reserved:
        db.rollback()
        raise HTTPException(422, f'移交量超过样本可用量（库存 {sample.quantity} 份，已预留 {reserved} 份）')
    obj = Transfer(**data.model_dump())
    db.add(obj); db.add(Audit(actor=user.email, action='创建流转申请 ' + data.code))
    try: db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '编号已存在或引用对象无效，请调整后重试')
    if settings.mode == 'on':
        db.add(Reservation(code=obj.code, transfer_id=obj.id, sample_id=obj.sample_id,
                           quantity=obj.quantity, status='reserved'))
    try: db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '编号已存在或引用对象无效，请调整后重试')
    result = record(obj)
    if settings.mode == 'on':
        result['reservation'] = record(db.scalar(select(Reservation).where(Reservation.transfer_id == obj.id)))
    return result
