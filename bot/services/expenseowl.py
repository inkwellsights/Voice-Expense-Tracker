"""ExpenseOwl REST API client.

ExpenseOwl exposes an unauthenticated JSON API (no `/api/` prefix):

- PUT    /expense                       create — body: {name, amount, category, date, tags?}
- GET    /expenses                      list — returns [] OR {expenses: [...]}
- DELETE /expense/delete?id=<uuid>      delete by id
- GET    /config                        runtime config (categories, currency, ...)

Endpoints discovered by reading the running UI's `fetch(...)` calls on v4+.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from ..config import TIMEZONE

logger = logging.getLogger(__name__)


class ExpenseOwlError(RuntimeError):
    pass


class ExpenseOwl:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def create(
        self,
        *,
        name: str,
        amount: float,
        category: str,
        date: datetime | None = None,
        tags: list[str] | None = None,
        kind: str = "expense",
    ) -> dict[str, Any]:
        when = date or datetime.now(TIMEZONE)
        # ExpenseOwl convention (discovered by reading dashboard JS):
        #   positive amount = income/gain, negative amount = expense.
        # Use the caller's intent: kind=="income" stores positive,
        # anything else stores negative.
        magnitude = abs(float(amount))
        signed_amount = magnitude if kind == "income" else -magnitude
        payload: dict[str, Any] = {
            "name": name,
            "amount": signed_amount,
            "category": category,
            "date": when.isoformat(),
        }
        if tags:
            payload["tags"] = list(tags)
        url = f"{self.base_url}/expense"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.put(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise ExpenseOwlError(f"Could not reach ExpenseOwl at {url}: {exc}") from exc

        if response.status_code >= 300:
            raise ExpenseOwlError(
                f"ExpenseOwl PUT failed ({response.status_code}): {response.text[:300]}"
            )

        try:
            body = response.json()
        except ValueError:
            body = {}
        # ExpenseOwl returns the saved object including its id; merge with the
        # payload so callers always have name/amount/category/date available too.
        if isinstance(body, dict):
            merged = {**payload, **body}
            return merged
        return payload

    async def list_all(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/expenses"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise ExpenseOwlError(f"Could not reach ExpenseOwl at {url}: {exc}") from exc
        if response.status_code >= 300:
            raise ExpenseOwlError(
                f"ExpenseOwl GET failed ({response.status_code}): {response.text[:300]}"
            )
        data = response.json()
        if isinstance(data, dict) and "expenses" in data:
            data = data["expenses"]
        if data is None:
            return []
        if not isinstance(data, list):
            raise ExpenseOwlError(f"Unexpected ExpenseOwl payload: {str(data)[:200]}")
        return data

    async def edit(
        self,
        expense_id: str,
        *,
        name: str,
        amount: float,
        category: str,
        date: str,
        tags: list[str],
    ) -> None:
        """Overwrite an existing expense by id. ExpenseOwl's edit endpoint
        replaces the whole record — pass back every field even if only tags
        are changing. `date` should be the ISO-8601 string already on the row.
        """
        url = f"{self.base_url}/expense/edit"
        payload = {
            "name": name,
            "amount": float(amount),
            "category": category,
            "date": date,
            "tags": list(tags),
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.put(
                    url,
                    params={"id": expense_id},
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise ExpenseOwlError(f"Could not reach ExpenseOwl at {url}: {exc}") from exc
        if response.status_code >= 300:
            raise ExpenseOwlError(
                f"ExpenseOwl PUT /expense/edit failed ({response.status_code}): "
                f"{response.text[:300]}"
            )

    async def delete(self, expense_id: str) -> None:
        url = f"{self.base_url}/expense/delete"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.delete(url, params={"id": expense_id})
        except httpx.HTTPError as exc:
            raise ExpenseOwlError(f"Could not reach ExpenseOwl at {url}: {exc}") from exc
        if response.status_code >= 300:
            raise ExpenseOwlError(
                f"ExpenseOwl DELETE failed ({response.status_code}): {response.text[:300]}"
            )


def parse_expense_date(raw: Any) -> datetime | None:
    """Best-effort parse of ExpenseOwl's date field into a tz-aware datetime."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=TIMEZONE)
    text = str(raw)
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed
