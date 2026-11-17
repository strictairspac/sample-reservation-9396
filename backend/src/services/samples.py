from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import Sample
from .common import save
from . import reservations
from src.utils.serialization import record


def listing(db): return [record(x) for x in db.scalars(select(Sample).order_by(Sample.id))]


def create(db, data, user): return save(db,Sample(**data.model_dump()),user,'创建样本 '+data.code)


def update(db, ident, data, user):
    reservations._ensure_settings(db)  # join the online cut before touching stock
    obj = db.execute(select(Sample).where(Sample.id == ident).with_for_update()).scalar_one_or_none()
    if not obj:
        db.rollback()
        raise HTTPException(404, '样本不存在')
    mode = reservations._mode(db)
    reserved = reservations._reserved_total(db, obj.id)
    if mode == 'on' and data.quantity < reserved:
        db.rollback()
        raise HTTPException(409, f'可用量不能为负：库存拟下调为 {data.quantity} 份，已预留 {reserved} 份')
    other = db.scalar(select(Sample).where(Sample.code == data.code, Sample.id != ident))
    if other:
        db.rollback()
        raise HTTPException(409, '编号已存在或引用对象无效，请调整后重试')
    for k, v in data.model_dump().items():
        setattr(obj, k, v)
    return save(db, obj, user, '更新样本 ' + data.code)
