"""Budget ledger in Postgres: the source of truth for spending.

Spending is derived from each deployment's wall-clock time and price. A live
deployment reserves everything up to its deadline, so a start or refresh is only
accepted if the money for its whole TTL is still available.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

BUDGET_LOCK_ID = 727_001

SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_period (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    pool_eur DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS deployment (
    id UUID PRIMARY KEY,
    entry_id TEXT NOT NULL,
    price_eur_per_hour DOUBLE PRECISION NOT NULL,
    requested_by TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    deadline TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    end_reason TEXT
);
"""

# Cost of each deployment inside the period: live ones count up to now.
_PERIOD_COSTS = """
SELECT d.entry_id,
       d.ended_at,
       d.price_eur_per_hour * GREATEST(0, EXTRACT(EPOCH FROM
           LEAST(COALESCE(d.ended_at, now()), now()) - GREATEST(d.started_at, p.started_at)
       )) / 3600 AS spent,
       CASE WHEN d.ended_at IS NULL THEN
           d.price_eur_per_hour * GREATEST(0, EXTRACT(EPOCH FROM d.deadline - now())) / 3600
       ELSE 0 END AS reserved
FROM deployment d, (SELECT * FROM budget_period ORDER BY id DESC LIMIT 1) p
WHERE COALESCE(d.ended_at, now()) > p.started_at
"""

_DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError)


class LedgerUnavailable(Exception):
    def __str__(self) -> str:
        return "Postgres is unavailable, so new starts and refreshes are refused"


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class BudgetSummary:
    period_id: int
    pool_eur: float
    spent_eur: float
    reserved_eur: float


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


class Ledger:
    def __init__(self, database_url: str, pool_eur: float, refuse_at_eur: float):
        self._url = database_url
        self._pool_eur = pool_eur
        self._refuse_at_eur = refuse_at_eur
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        pool = await asyncpg.create_pool(self._url, min_size=1, max_size=4, timeout=10)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", BUDGET_LOCK_ID)
            await connection.execute(SCHEMA)
            if await connection.fetchval("SELECT count(*) FROM budget_period") == 0:
                await connection.execute(
                    "INSERT INTO budget_period (pool_eur) VALUES ($1)", self._pool_eur
                )
        self._pool = pool

    @property
    def available(self) -> bool:
        return self._pool is not None

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise LedgerUnavailable()
        return self._pool

    async def _guard_budget(self, connection: Any, extra_eur: float) -> None:
        await connection.execute("SELECT pg_advisory_xact_lock($1)", BUDGET_LOCK_ID)
        committed = await connection.fetchval(
            f"SELECT COALESCE(sum(spent + reserved), 0) FROM ({_PERIOD_COSTS}) costs"
        )
        if float(committed) + extra_eur > self._refuse_at_eur:
            raise BudgetExceeded(
                f"Refused: {float(committed):.2f} € is spent or reserved and this needs "
                f"{extra_eur:.2f} € more, over the {self._refuse_at_eur:.2f} € limit. "
                "Run `bivouac budget renew` to reset the pool."
            )

    async def reserve(
        self,
        *,
        deployment_id: str,
        entry_id: str,
        price_eur_per_hour: float,
        requested_by: str,
        started_at: float,
        deadline: float,
    ) -> None:
        pool = self._require_pool()
        cost = price_eur_per_hour * (deadline - started_at) / 3600
        try:
            async with pool.acquire() as connection, connection.transaction():
                await self._guard_budget(connection, cost)
                await connection.execute(
                    """INSERT INTO deployment
                       (id, entry_id, price_eur_per_hour, requested_by, started_at, deadline)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    deployment_id,
                    entry_id,
                    price_eur_per_hour,
                    requested_by,
                    _utc(started_at),
                    _utc(deadline),
                )
        except _DB_ERRORS as error:
            raise LedgerUnavailable() from error

    async def extend(self, deployment_id: str, new_deadline: float, extra_eur: float) -> None:
        pool = self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                await self._guard_budget(connection, max(0.0, extra_eur))
                await connection.execute(
                    "UPDATE deployment SET deadline = $2 WHERE id = $1",
                    deployment_id,
                    _utc(new_deadline),
                )
        except _DB_ERRORS as error:
            raise LedgerUnavailable() from error

    async def set_deadline(self, deployment_id: str, deadline: float) -> None:
        """Undo a refresh that OVH did not accept, without a budget check."""
        pool = self._require_pool()
        try:
            await pool.execute(
                "UPDATE deployment SET deadline = $2 WHERE id = $1",
                deployment_id,
                _utc(deadline),
            )
        except _DB_ERRORS as error:
            raise LedgerUnavailable() from error

    async def end(self, deployment_id: str, ended_at: float, reason: str) -> None:
        pool = self._require_pool()
        try:
            await pool.execute(
                """UPDATE deployment SET ended_at = $2, end_reason = $3
                   WHERE id = $1 AND ended_at IS NULL""",
                deployment_id,
                _utc(ended_at),
                reason,
            )
        except _DB_ERRORS as error:
            raise LedgerUnavailable() from error

    async def summary(self) -> BudgetSummary:
        pool = self._require_pool()
        try:
            period = await pool.fetchrow(
                "SELECT id, pool_eur FROM budget_period ORDER BY id DESC LIMIT 1"
            )
            totals = await pool.fetchrow(
                f"""SELECT COALESCE(sum(spent), 0) AS spent,
                           COALESCE(sum(reserved), 0) AS reserved
                    FROM ({_PERIOD_COSTS}) costs"""
            )
        except _DB_ERRORS as error:
            raise LedgerUnavailable() from error
        return BudgetSummary(
            period_id=period["id"],
            pool_eur=period["pool_eur"],
            spent_eur=float(totals["spent"]),
            reserved_eur=float(totals["reserved"]),
        )

    async def renew(self) -> BudgetSummary:
        """Start a new period with a full pool; nothing carries over."""
        pool = self._require_pool()
        try:
            await pool.execute("INSERT INTO budget_period (pool_eur) VALUES ($1)", self._pool_eur)
        except _DB_ERRORS as error:
            raise LedgerUnavailable() from error
        return await self.summary()

    async def history(self) -> dict[str, list[dict[str, Any]]]:
        pool = self._require_pool()
        by_entry_query = f"""SELECT entry_id, sum(spent) AS spent_eur
                             FROM ({_PERIOD_COSTS}) costs GROUP BY entry_id ORDER BY 2 DESC"""
        by_day_query = """SELECT day::date::text AS day, sum(cost) AS spent_eur FROM (
              SELECT date_trunc('day', g, 'UTC') AS day,
                     d.price_eur_per_hour * GREATEST(0, EXTRACT(EPOCH FROM
                         LEAST(COALESCE(d.ended_at, now()), g + interval '1 day')
                         - GREATEST(d.started_at, g))) / 3600 AS cost
              FROM deployment d,
                   generate_series(date_trunc('day', d.started_at, 'UTC'),
                                   COALESCE(d.ended_at, now()), interval '1 day') g
            ) days GROUP BY 1 ORDER BY 1 DESC LIMIT 60"""
        try:
            by_entry = await pool.fetch(by_entry_query)
            by_day = await pool.fetch(by_day_query)
        except _DB_ERRORS as error:
            raise LedgerUnavailable() from error
        return {
            "by_entry": [
                {"entry_id": r["entry_id"], "spent_eur": float(r["spent_eur"])} for r in by_entry
            ],
            "by_day": [{"day": r["day"], "spent_eur": float(r["spent_eur"])} for r in by_day],
        }

    async def spent_this_month(self) -> float:
        """Deployment cost in the current UTC month, to compare with OVH's own figure."""
        pool = self._require_pool()
        try:
            value = await pool.fetchval(
                """SELECT COALESCE(sum(price_eur_per_hour * GREATEST(0, EXTRACT(EPOCH FROM
                       COALESCE(ended_at, now()) -
                       GREATEST(started_at, date_trunc('month', now(), 'UTC')))) / 3600), 0)
                   FROM deployment
                   WHERE COALESCE(ended_at, now()) > date_trunc('month', now(), 'UTC')"""
            )
        except _DB_ERRORS as error:
            raise LedgerUnavailable() from error
        return float(value)
