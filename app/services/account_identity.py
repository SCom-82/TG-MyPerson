"""The account's own user id must match the logged-in session (review 13.10, R2-1).

accounts.external_user_id is what the isolated tables trust for is_self and
is_outgoing. A session of ANOTHER messenger user under an account would
write its messages into this account's partition — so an empty id is filled
from the session, and a different one (or one already taken by another
account of the platform) stops the session from being used.
"""

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models import Account


class IdentityMismatch(Exception):
    """The session belongs to another user than the account."""


async def ensure_account_user(account_id: int, user_id: int) -> None:
    """Fill an empty accounts.external_user_id or confirm it; IdentityMismatch otherwise."""
    async with async_session() as db:
        row = (await db.execute(
            select(Account.alias, Account.platform, Account.external_user_id).where(Account.id == account_id)
        )).one_or_none()
        if row is None:
            raise IdentityMismatch(f"account {account_id} not found")
        alias, platform, known = row
        if known is not None:
            if known != user_id:
                raise IdentityMismatch(
                    f"session of {platform} user {user_id} under account '{alias}' (user {known})"
                )
            return
        try:
            await db.execute(update(Account).where(Account.id == account_id).values(external_user_id=user_id))
            await db.commit()
        except IntegrityError:
            raise IdentityMismatch(
                f"{platform} user {user_id} already belongs to another account; not usable as '{alias}'"
            )
