"""
main.py
=======

Identity Reconciliation web service.

    POST /identify   {"email": "...", "phoneNumber": "..."}
    ->  200 {"contact": {"primaryContactId": int,
                         "emails": [...],
                         "phoneNumbers": [...],
                         "secondaryContactIds": [...]}}

Run:
    pip install "fastapi[standard]" sqlalchemy "pydantic[email]"
    uvicorn main:app --reload

The whole problem reduces to maintaining disjoint sets over (email, phone)
pairs, where each set is keyed by its oldest member. Every request is one of
four transitions:

    1. no match                  -> open a new set (new primary)
    2. match, nothing new        -> no writes at all
    3. match, new info           -> add a member (new secondary)
    4. match spanning two sets   -> merge; the younger primary is demoted and
                                    its children are re-parented

Case 4 is the one that bites people: it is not enough to demote the younger
primary, its existing secondaries must be re-pointed too, otherwise the
"star" invariant breaks and the cluster silently splits in half on the next
read.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from models import Contact, LinkPrecedence, get_db, init_db, utcnow

logger = logging.getLogger("identity")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(
    title="Identity Reconciliation Service",
    version="1.0.0",
    description="Consolidates contact details that belong to the same person.",
)

# SQLite serialises writers at the file level and will raise "database is
# locked" under concurrent /identify calls. A process-wide lock around the
# read-modify-write section keeps the reconciliation atomic and the test
# suite deterministic. On Postgres this would be replaced by running the
# transaction at SERIALIZABLE isolation (or a row lock on the primary).
_reconcile_lock = threading.Lock()


@app.on_event("startup")
def _startup() -> None:
    init_db()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class IdentifyRequest(BaseModel):
    """Either field may be null, but not both."""

    email: Optional[EmailStr] = None
    # Phone numbers arrive as JSON strings *or* JSON numbers in the wild
    # ("phoneNumber": "123456" vs 123456). Accept both, store one canonical
    # string form -- otherwise the same person creates two clusters.
    phoneNumber: Optional[Union[str, int]] = None

    model_config = {"extra": "forbid"}

    @field_validator("phoneNumber", mode="before")
    @classmethod
    def _normalise_phone(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, bool):  # bool is a subclass of int -- reject it
            raise ValueError("invalid phoneNumber")
        if isinstance(v, int):
            v = str(v)
        if not isinstance(v, str):
            raise ValueError("invalid phoneNumber")
        v = v.strip()
        return v or None

    @field_validator("email", mode="before")
    @classmethod
    def _normalise_email(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("invalid email")
        v = v.strip().lower()  # emails are matched case-insensitively
        return v or None

    @model_validator(mode="after")
    def _at_least_one(self) -> "IdentifyRequest":
        if self.email is None and self.phoneNumber is None:
            raise ValueError("at least one identifier is required")
        return self


class ContactPayload(BaseModel):
    primaryContactId: int
    emails: List[str] = Field(default_factory=list)
    phoneNumbers: List[str] = Field(default_factory=list)
    secondaryContactIds: List[int] = Field(default_factory=list)


class IdentifyResponse(BaseModel):
    contact: ContactPayload


# --------------------------------------------------------------------------- #
# Error handling (bonus): opaque, non-revealing responses
# --------------------------------------------------------------------------- #
#
# Every failure returns the same flat shape with a random reference id. The
# real reason is written to the server log under that id, so operators can
# debug while a caller probing the endpoint learns nothing about the schema,
# the ORM, or which of the two fields it got wrong.


def _opaque(status_code: int, message: str, ref: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "rejected",
            "message": message,
            "reference": ref,
        },
    )


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    ref = uuid.uuid4().hex[:12]
    logger.warning("validation failure ref=%s detail=%s", ref, exc.errors())
    # Deliberately generic, and deliberately NOT 422: nothing here tells the
    # caller which field failed or what the expected type was.
    return _opaque(
        status.HTTP_400_BAD_REQUEST,
        "Transmission could not be processed.",
        ref,
    )


@app.exception_handler(StarletteHTTPException)
async def _http_handler(request: Request, exc: StarletteHTTPException):
    ref = uuid.uuid4().hex[:12]
    logger.warning("http failure ref=%s status=%s detail=%s", ref, exc.status_code, exc.detail)
    return _opaque(exc.status_code, "Transmission could not be processed.", ref)


@app.exception_handler(Exception)
async def _unhandled_handler(request: Request, exc: Exception):
    ref = uuid.uuid4().hex[:12]
    logger.exception("unhandled failure ref=%s", ref)
    return _opaque(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "Transmission could not be processed.",
        ref,
    )


# --------------------------------------------------------------------------- #
# Reconciliation core
# --------------------------------------------------------------------------- #


def _fetch_direct_matches(
    db: Session, email: Optional[str], phone: Optional[str]
) -> List[Contact]:
    """Rows that literally carry one of the incoming identifiers.

    Single indexed query; the OR is index-friendly on both branches because
    ``email`` and ``phoneNumber`` are each indexed.
    """
    predicates = []
    if email is not None:
        predicates.append(Contact.email == email)
    if phone is not None:
        predicates.append(Contact.phone_number == phone)

    return (
        db.query(Contact)
        .filter(Contact.deleted_at.is_(None), or_(*predicates))
        .all()
    )


def _load_cluster(db: Session, primary_id: int) -> List[Contact]:
    """Every live row in the cluster rooted at ``primary_id``, oldest first."""
    return (
        db.query(Contact)
        .filter(
            Contact.deleted_at.is_(None),
            or_(Contact.id == primary_id, Contact.linked_id == primary_id),
        )
        .order_by(Contact.created_at.asc(), Contact.id.asc())
        .all()
    )


def _merge_clusters(db: Session, primaries: Sequence[Contact]) -> Contact:
    """Collapse several primaries into the oldest one and return it.

    ``primaries`` must be sorted oldest-first. For each younger primary we:
      1. re-parent its secondaries onto the surviving primary, and
      2. demote it to secondary.

    Step 1 must happen before (or at least alongside) step 2 -- and it must
    happen at all. Skipping it is the classic bug: the demoted row points at
    the right place, but its children still point at *it*, so the cluster is
    no longer a star and half the members vanish from the next response.
    """
    survivor = primaries[0]
    now = utcnow()

    for younger in primaries[1:]:
        # Bulk UPDATE -- one statement per absorbed cluster instead of one
        # per row, which matters once a cluster has hundreds of members.
        db.query(Contact).filter(
            Contact.linked_id == younger.id,
            Contact.deleted_at.is_(None),
        ).update(
            {Contact.linked_id: survivor.id, Contact.updated_at: now},
            synchronize_session=False,
        )

        younger.link_precedence = LinkPrecedence.SECONDARY.value
        younger.linked_id = survivor.id
        younger.updated_at = now

        logger.info("merged cluster %s into %s", younger.id, survivor.id)

    db.flush()
    # The bulk UPDATE bypassed the identity map, so drop cached state before
    # anything reads these objects again.
    db.expire_all()
    return db.get(Contact, survivor.id)


def _adds_new_information(
    cluster: Iterable[Contact], email: Optional[str], phone: Optional[str]
) -> bool:
    """True when the request carries an identifier the cluster has not seen."""
    known_emails = {c.email for c in cluster if c.email}
    known_phones = {c.phone_number for c in cluster if c.phone_number}

    if email is not None and email not in known_emails:
        return True
    if phone is not None and phone not in known_phones:
        return True
    return False


def _build_payload(cluster: Sequence[Contact], primary: Contact) -> ContactPayload:
    """Serialise a cluster.

    Contract: the primary's own email/phone lead their respective lists,
    everything else follows in creation order, duplicates and NULLs dropped.
    """
    ordered = [primary] + [c for c in cluster if c.id != primary.id]

    emails: List[str] = []
    phones: List[str] = []
    secondary_ids: List[int] = []

    for contact in ordered:
        if contact.email and contact.email not in emails:
            emails.append(contact.email)
        if contact.phone_number and contact.phone_number not in phones:
            phones.append(contact.phone_number)
        if contact.id != primary.id:
            secondary_ids.append(contact.id)

    return ContactPayload(
        primaryContactId=primary.id,
        emails=emails,
        phoneNumbers=phones,
        secondaryContactIds=secondary_ids,
    )


def reconcile(db: Session, email: Optional[str], phone: Optional[str]) -> ContactPayload:
    """Apply one request to the identity graph and return the consolidated view."""

    matches = _fetch_direct_matches(db, email, phone)

    # ---- Case 1: nothing known about this person yet ---------------------- #
    if not matches:
        primary = Contact(
            email=email,
            phone_number=phone,
            link_precedence=LinkPrecedence.PRIMARY.value,
            linked_id=None,
        )
        db.add(primary)
        db.flush()  # assigns the id
        logger.info("created primary %s", primary.id)
        return _build_payload([primary], primary)

    # ---- Resolve every match up to its cluster root ----------------------- #
    root_ids = {c.cluster_root_id for c in matches}

    primaries = (
        db.query(Contact)
        .filter(Contact.id.in_(root_ids), Contact.deleted_at.is_(None))
        .order_by(Contact.created_at.asc(), Contact.id.asc())  # id breaks ties
        .all()
    )

    # ---- Case 4: the request bridges two or more clusters ----------------- #
    primary = _merge_clusters(db, primaries) if len(primaries) > 1 else primaries[0]

    cluster = _load_cluster(db, primary.id)

    # ---- Case 3: the request introduces something new --------------------- #
    if _adds_new_information(cluster, email, phone):
        secondary = Contact(
            email=email,
            phone_number=phone,
            link_precedence=LinkPrecedence.SECONDARY.value,
            linked_id=primary.id,
        )
        db.add(secondary)
        db.flush()
        cluster.append(secondary)
        logger.info("created secondary %s under %s", secondary.id, primary.id)

    # ---- Case 2 falls through here with zero writes ----------------------- #
    return _build_payload(cluster, primary)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.post(
    "/identify",
    response_model=IdentifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Consolidate contact details",
)
def identify(
    payload: IdentifyRequest, db: Session = Depends(get_db)
) -> IdentifyResponse:
    email = payload.email
    phone = payload.phoneNumber

    try:
        with _reconcile_lock:
            contact = reconcile(db, email, phone)
            db.commit()
    except SQLAlchemyError:
        db.rollback()
        # Re-raised so the catch-all handler logs it and returns the same
        # opaque body every other failure returns.
        raise

    return IdentifyResponse(contact=contact)


@app.get("/health", summary="Liveness probe")
def health() -> Dict[str, str]:
    return {"status": "ok"}

@app.get("/products", summary="List all products")
def get_products():
    return {"products": ["EV Truck Heavy", "EV Delivery Van", "EV Battery Pack"]}