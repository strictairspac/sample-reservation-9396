from sqlalchemy import Column, Integer, String, JSON, DateTime, ForeignKey, Text, UniqueConstraint, CheckConstraint
from datetime import datetime, timezone
from src.config.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True);email=Column(String(120),unique=True,nullable=False)
    password=Column(String(300),nullable=False);role=Column(String(20),nullable=False)


class Sample(Base):
    __tablename__='samples'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False)
    title=Column(String(160),nullable=False);quantity=Column(Integer,nullable=False);location=Column(String(120),nullable=False)


class Transfer(Base):
    __tablename__='transfers'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False)
    sample_id=Column(Integer,ForeignKey('samples.id'),nullable=False);destination=Column(String(120),nullable=False)
    quantity=Column(Integer,nullable=False);note=Column(String(300),default='')


class Trial(Base):
    __tablename__='trials'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False)
    transfer_id=Column(Integer,ForeignKey('transfers.id'),nullable=False);timeout_ms=Column(Integer,nullable=False)
    status=Column(String(20),default='queued',nullable=False);result=Column(JSON,nullable=True)
    created_at=Column(DateTime(timezone=True),default=utcnow);error=Column(Text,default='')


class TrialBalance(Base):
    __tablename__='trial_balances'
    trial_id=Column(Integer,ForeignKey('trials.id'),primary_key=True);remaining=Column(Integer,nullable=False);received=Column(Integer,nullable=False,default=0)


class Receipt(Base):
    __tablename__='receipts'
    id=Column(Integer,primary_key=True);trial_id=Column(Integer,ForeignKey('trials.id'),nullable=False)
    actor=Column(String(20),nullable=False);quantity=Column(Integer,nullable=False)
    __table_args__=(UniqueConstraint('trial_id','actor'),)


class Audit(Base):
    __tablename__='audit_logs'
    id=Column(Integer,primary_key=True);actor=Column(String(120),nullable=False);action=Column(String(160),nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow)


class ReservationSettings(Base):
    """Single-row switch for the online reservation flow (id=1).

    The row is read with FOR SHARE by application/inventory transactions and
    locked FOR UPDATE by an activation transaction.  That gives activation a
    clean cut: every write that started before the cut commits first and is
    visible to the batch, every later write waits and reads mode='on'.
    """
    __tablename__='reservation_settings'
    id=Column(Integer,primary_key=True)
    mode=Column(String(20),nullable=False,default='off')
    updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow)


class ReservationRun(Base):
    """One online enablement batch.  All existing applications are included."""
    __tablename__='reservation_runs'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False)
    status=Column(String(20),nullable=False,default='pending')
    content_hash=Column(String(64),nullable=False,default='')
    snapshot_max_transfer_id=Column(Integer,nullable=False,default=0)
    content=Column(String(500),nullable=False,default='')
    application_count=Column(Integer,nullable=False,default=0)
    reserved_count=Column(Integer,nullable=False,default=0)
    note=Column(String(300),default='')
    error=Column(Text,default='')
    created_by=Column(String(120),default='')
    result=Column(JSON,nullable=True)
    created_at=Column(DateTime(timezone=True),default=utcnow)
    updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow)


class Reservation(Base):
    """A quantity held against a sample for one transfer application code."""
    __tablename__='reservations'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False)
    transfer_id=Column(Integer,ForeignKey('transfers.id'),unique=True,nullable=False)
    sample_id=Column(Integer,ForeignKey('samples.id'),nullable=False,index=True)
    quantity=Column(Integer,nullable=False)
    status=Column(String(20),nullable=False,default='reserved')
    run_id=Column(Integer,ForeignKey('reservation_runs.id'),nullable=True)
    created_at=Column(DateTime(timezone=True),default=utcnow)
    updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow)
    __table_args__=(CheckConstraint('quantity > 0',name='ck_reservations_quantity_positive'),)
