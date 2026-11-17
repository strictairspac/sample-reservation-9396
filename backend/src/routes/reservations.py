from fastapi import APIRouter, Depends
from src.config.database import db_session
from src.utils.security import current, writer
from src.schemas.reservation import ReservationEnableInput
from src.services import reservations as service
router = APIRouter()


@router.get('/reservations')
def listing(db=Depends(db_session), user=Depends(current)): return service.listing(db)


@router.get('/reservations/summary')
def summary(db=Depends(db_session), user=Depends(current)): return service.summary(db)


@router.post('/reservations/enable', status_code=201)
def enable(data: ReservationEnableInput, db=Depends(db_session), user=Depends(writer)): return service.enable(db, data, user)


@router.post('/reservations/runs/{ident}/retry')
def retry(ident: int, db=Depends(db_session), user=Depends(writer)): return service.retry(db, ident, user)


@router.post('/reservations/{ident}/confirm')
def confirm(ident: int, db=Depends(db_session), user=Depends(writer)): return service.confirm(db, ident, user)


@router.post('/reservations/{ident}/cancel')
def cancel(ident: int, db=Depends(db_session), user=Depends(writer)): return service.cancel(db, ident, user)
