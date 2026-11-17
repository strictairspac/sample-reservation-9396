from pydantic import BaseModel, Field


class ReservationEnableInput(BaseModel):
    """Same code + same content is idempotent; changed content is rejected."""
    code: str = Field(min_length=2, max_length=80)
    note: str = Field(default='', max_length=300)
    content: str = Field(default='', max_length=500)
