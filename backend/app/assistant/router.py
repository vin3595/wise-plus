"""Home > Ask: the same keyword answers the owner gets on WhatsApp."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.permissions import Perm
from ..deps import Auth, get_db, require
from .answers import answer

router = APIRouter(prefix="/assistant", tags=["assistant"])


class Ask(BaseModel):
    q: str = Field(min_length=0, max_length=200)


@router.post("/ask")
def ask(body: Ask, db: Session = Depends(get_db), auth: Auth = Depends(require(Perm.reports_view))) -> dict:
    return {"text": answer(db, body.q)}
