"""Account-number privacy and resolution.

Schwab identifies accounts in API paths by an opaque hash, and returns plain
account numbers in payloads. We keep the mapping here, show the model only
masked labels such as ``****1234`` (unless masking is disabled), and let the
model refer to an account by that label, its last digits, or its nickname.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .client import SchwabAPIError, SchwabClient

logger = logging.getLogger(__name__)


class AccountNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Account:
    number: str
    hash: str
    label: str
    nickname: str | None = None
    type: str | None = None
    primary: bool = False

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"account": self.label}
        if self.nickname:
            out["nickname"] = self.nickname
        if self.type:
            out["type"] = self.type
        if self.primary:
            out["primary"] = True
        return out


def mask_number(number: str) -> str:
    return f"****{number[-4:]}"


class AccountDirectory:
    def __init__(self, client: SchwabClient, *, mask: bool = True):
        self.client = client
        self.mask = mask
        self._accounts: list[Account] | None = None
        self._lock = asyncio.Lock()

    async def accounts(self, *, refresh: bool = False) -> list[Account]:
        if self._accounts is not None and not refresh:
            return self._accounts
        async with self._lock:
            if self._accounts is None or refresh:
                self._accounts = await self._load()
            return self._accounts

    async def _load(self) -> list[Account]:
        numbers = await self.client.get_account_numbers()
        prefs: dict[str, dict[str, Any]] = {}
        try:
            pref = await self.client.get_user_preference()
            for item in pref.get("accounts", []) or []:
                if item.get("accountNumber"):
                    prefs[str(item["accountNumber"])] = item
        except SchwabAPIError as exc:  # nicknames are nice-to-have only
            logger.info("userPreference unavailable: %s", exc)

        accounts = []
        for item in numbers:
            number = str(item["accountNumber"])
            pref_item = prefs.get(number, {})
            accounts.append(
                Account(
                    number=number,
                    hash=item["hashValue"],
                    label=mask_number(number) if self.mask else number,
                    nickname=(pref_item.get("nickName") or None),
                    type=pref_item.get("type"),
                    primary=bool(pref_item.get("primaryAccount")),
                )
            )
        return accounts

    async def by_number(self, number: str | int | None) -> Account | None:
        if number is None:
            return None
        number = str(number)
        for acct in await self.accounts():
            if acct.number == number:
                return acct
        return None

    async def label_for(self, number: str | int | None) -> str | None:
        if number is None:
            return None
        acct = await self.by_number(number)
        if acct:
            return acct.label
        return mask_number(str(number)) if self.mask else str(number)

    async def resolve(self, ref: str) -> Account:
        """Find an account by full number, masked label, trailing digits, nickname, or hash."""
        accounts = await self.accounts()
        needle = ref.strip()
        lowered = needle.lower()
        digits = "".join(ch for ch in needle if ch.isdigit())

        exact = [a for a in accounts if needle in (a.number, a.hash, a.label)]
        if not exact:
            exact = [a for a in accounts if a.nickname and a.nickname.lower() == lowered]
        if not exact and len(digits) >= 3 and digits == needle.lstrip("*.#x ").strip():
            exact = [a for a in accounts if a.number.endswith(digits)]
        if len(exact) == 1:
            return exact[0]

        choices = (
            ", ".join(f"{a.label}" + (f" ({a.nickname})" if a.nickname else "") for a in accounts) or "none"
        )
        if exact:
            raise AccountNotFound(f"'{ref}' matches more than one account; use one of: {choices}")
        raise AccountNotFound(f"No account matches '{ref}'. Available accounts: {choices}")

    async def scrub(self, value: Any) -> Any:
        """Replace any raw account number left in a payload with its masked label."""
        if not self.mask:
            return value
        mapping = {a.number: a.label for a in await self.accounts()}
        return _scrub(value, mapping)


def _scrub(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, mapping) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int)) and str(value) in mapping:
        return mapping[str(value)]
    if isinstance(value, str):
        for number, label in mapping.items():
            if number in value:
                value = value.replace(number, label)
    return value
