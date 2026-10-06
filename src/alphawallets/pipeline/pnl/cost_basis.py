"""FIFO cost-basis engine for one (wallet, token) pair.

Pure Python: no DuckDB, no network, no I/O. The calculator (ADR 0012 step 4)
feeds it events read from the cache and writes the Realizations it returns.
Keeping it pure is what makes the methodology testable — ADR 0012's premise is
that a PnL error does not crash but produces a plausible number, and the only
defence against that is being able to assert the arithmetic in isolation.

Consumption order is **unified FIFO**: the oldest lot is consumed first,
regardless of whether it came from trading or an airdrop. Tokens are fungible
on-chain, so maintaining separate queues would bake in a policy — "harvest
airdrop gains first", or the reverse — that is not economically neutral and that
nobody asked for. ADR 0012 decision 4 requires trading PnL to be separable from
airdrop PnL, and that is satisfied by the `source` tag each Realization carries
from its lot, not by consumption order. A single sale crossing the boundary
emits one Realization per lot, each with its own source.

Insufficient balance raises rather than clamping. A sell exceeding the FIFO
balance means something upstream is wrong — a missing receive event, the wrong
decimals, a misattributed wallet — and filling it to the available amount would
turn a detectable bug into an unreconcilable number. Same reasoning as
`token_slot()` refusing to guess a pool slot and the schema-drift guard refusing
to migrate.

**Callers must feed events in chronological order.** The engine does not sort:
it trusts that `add_lot` and `consume` arrive in the order they happened. The
calculator satisfies this by ordering its DuckDB query on
`block_timestamp, log_index`. Feeding events out of order produces a wrong cost
basis silently, which is why it is stated here rather than defended against —
sorting inside the engine would hide a caller bug rather than surface it.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from decimal import Decimal

from alphawallets.pipeline.pnl.models import CostBasisLot, LotSource, Realization

logger = logging.getLogger(__name__)


class InsufficientBalanceError(ValueError):
    """A realization asked for more tokens than the FIFO stack holds.

    A ValueError subclass so a caller that catches ValueError around the
    arithmetic still catches this, but a caller that wants to distinguish an
    upstream data problem from a bad argument can.

    The message names the wallet, the token, the requested amount and the
    available amount, because the fix is always upstream and those four values
    are what identify which event is missing.
    """


class FIFOEngine:
    """FIFO cost-basis tracker for one wallet's holdings of one token.

    One instance per (wallet, token) pair — FIFO is only meaningful within a
    single asset, which is also why ADR 0012 decision 8 computes per
    (wallet, token) internally and aggregates at report time.

    Quantities are carried in **base units** (not decimal-adjusted) as Decimal,
    matching how the on-chain fetchers store them: a uint256 amount loses digits
    under float. USD figures are decimal-adjusted per whole token.
    """

    def __init__(self, wallet: str, token_address: str, token_decimals: int) -> None:
        """Create an empty engine for one (wallet, token).

        Args:
            wallet: Wallet address. Lowercased for consistency with the cache.
            token_address: Token contract address. Lowercased.
            token_decimals: The token's decimals. Required rather than defaulted
                to 18: a wrong value puts every PnL figure out by orders of
                magnitude, and a required argument forces the caller to source
                it rather than inherit a plausible default. AW_02 stores it per
                transfer, and the pool layout carries it per token.

        Raises:
            ValueError: On negative decimals. Zero is valid — some tokens have
                no fractional part.
        """
        if token_decimals < 0:
            raise ValueError(f"token_decimals must be non-negative, got {token_decimals}")

        self.wallet = wallet.lower()
        self.token_address = token_address.lower()
        self.token_decimals = token_decimals

        # Computed once: used on every realization, and 10**18 is not free.
        self._decimal_divisor = Decimal(10) ** token_decimals

        # One queue holding both sources, ordered by arrival. See the module
        # docstring for why consumption is not partitioned by source.
        self._lots: deque[CostBasisLot] = deque()

    # ---------- Mutation ----------

    def add_lot(
        self,
        acquired_at: datetime,
        qty_token: Decimal,
        unit_cost_usd: float,
        source: LotSource,
    ) -> None:
        """Append an acquisition to the back of the FIFO queue.

        Args:
            acquired_at: UTC timestamp of the acquisition.
            qty_token: Quantity in base units. Must be positive.
            unit_cost_usd: USD cost basis per whole token. Zero for an airdrop
                lot (ADR 0012 decision 4); price-at-receipt for a trading lot,
                including a transfer-in, which is decision 2 — treating a CEX
                withdrawal as free would inflate PnL for the commonest wallet
                shape.
            source: 'trading' or 'airdrop'.

        Raises:
            pydantic.ValidationError: On a non-positive quantity, a negative
                cost, a naive timestamp, or an unknown source. Validation lives
                in CostBasisLot so the engine and any other producer of lots
                enforce the same invariants.
        """
        self._lots.append(
            CostBasisLot(
                acquired_at=acquired_at,
                qty_token=qty_token,
                unit_cost_usd=unit_cost_usd,
                source=source,
            )
        )

    def consume(
        self,
        realized_at: datetime,
        qty_token: Decimal,
        unit_sale_usd: float,
    ) -> list[Realization]:
        """Consume quantity from the oldest lots, returning one Realization each.

        Walks the queue front to back. A lot consumed exactly or entirely is
        removed; a lot consumed partially is replaced by a copy carrying the
        reduced quantity and pushed back to the front — lots are frozen, so
        there is no in-place mutation anywhere in this class.

        Args:
            realized_at: UTC timestamp of the realizing event.
            qty_token: Quantity to consume, in base units. Must be positive.
            unit_sale_usd: USD price per whole token at realized_at.

        Returns:
            One Realization per lot touched, in consumption order — so a sale
            spanning three lots returns three rows. ADR 0012 decision 9 depends
            on this granularity: the window filter is on realized_at, and
            per-lot rows let each matched quantity be attributed to the moment
            it was realized.

        Raises:
            ValueError: On a non-positive qty_token.
            InsufficientBalanceError: When the stack holds less than requested.
                The stack is left untouched — the check runs before any
                consumption, so a failed call cannot leave the engine in a
                half-consumed state.
        """
        if qty_token <= 0:
            raise ValueError(f"qty_token must be positive, got {qty_token}")

        available = self.balance_token()
        if qty_token > available:
            raise InsufficientBalanceError(
                f"Cannot realize {qty_token} base units of {self.token_address} "
                f"for wallet {self.wallet}: only {available} available. "
                "A sell exceeding the FIFO balance means an acquisition is "
                "missing upstream, the token decimals are wrong, or the event "
                "was attributed to the wrong wallet — clamping would hide that."
            )

        # Decimal(str(x)) rather than Decimal(x): the latter inherits the float's
        # binary error (Decimal(0.1) is 0.1000000000000000055511151231257827...),
        # while the former takes the decimal literal the float was printed from.
        sale = Decimal(str(unit_sale_usd))

        realizations: list[Realization] = []
        remaining = qty_token

        while remaining > 0:
            lot = self._lots.popleft()
            taken = min(lot.qty_token, remaining)

            if taken < lot.qty_token:
                # Partial: put the remainder back at the front, still the oldest.
                self._lots.appendleft(lot.model_copy(update={"qty_token": lot.qty_token - taken}))

            cost = Decimal(str(lot.unit_cost_usd))
            whole_tokens = taken / self._decimal_divisor
            pnl = (sale - cost) * whole_tokens

            realizations.append(
                Realization(
                    realized_at=realized_at,
                    qty_token=taken,
                    unit_cost_usd=lot.unit_cost_usd,
                    unit_sale_usd=unit_sale_usd,
                    source=lot.source,
                    # float only at the boundary: the DuckDB column is DOUBLE,
                    # and everything above ran through Decimal.
                    pnl_usd=float(pnl),
                )
            )
            remaining -= taken

        logger.debug(
            "Consumed %s base units of %s for %s across %d lot(s)",
            qty_token,
            self.token_address,
            self.wallet,
            len(realizations),
        )
        return realizations

    # ---------- Inspection ----------

    def balance_token(self) -> Decimal:
        """Total quantity remaining across every lot, in base units."""
        return sum((lot.qty_token for lot in self._lots), Decimal(0))

    def balance_token_by_source(self) -> dict[LotSource, Decimal]:
        """Remaining quantity split by lot source, in base units.

        Both keys are always present, zero when empty, so a caller can read
        either without a default. This is the accounting split ADR 0012
        decision 4 asks for — kept separate in reporting while consumption stays
        unified.
        """
        totals: dict[LotSource, Decimal] = {"trading": Decimal(0), "airdrop": Decimal(0)}
        for lot in self._lots:
            totals[lot.source] += lot.qty_token
        return totals

    def avg_cost_basis_usd(self) -> float | None:
        """Quantity-weighted average cost basis per whole token, or None if empty.

        None rather than 0.0 on an empty stack: a zero average is a real,
        different statement — it is what a wallet holding only airdrop lots has
        — and collapsing the two would make `avg_cost_basis_usd == 0` ambiguous
        in the output row. WalletPnL.avg_cost_basis_usd is nullable for the same
        reason.

        Airdrop lots participate at their zero cost, pulling the average down
        proportionally, because this describes the cost of the whole remaining
        position rather than of its trading half.
        """
        total_qty = self.balance_token()
        if total_qty == 0:
            return None

        weighted = sum(
            (lot.qty_token * Decimal(str(lot.unit_cost_usd)) for lot in self._lots),
            Decimal(0),
        )
        return float(weighted / total_qty)

    def lots_snapshot(self) -> list[CostBasisLot]:
        """Return the current lots oldest-first as a plain list.

        A copy of the container, so a caller cannot reorder or drop lots by
        mutating what it gets back. The lots themselves are frozen, so the
        shallow copy is sufficient — there is no deeper state to protect.
        """
        return list(self._lots)

    def __repr__(self) -> str:
        """Readable in a test failure or a debugger session."""
        return (
            f"FIFOEngine(wallet={self.wallet!r}, token={self.token_address!r}, "
            f"decimals={self.token_decimals}, lots={len(self._lots)}, "
            f"balance={self.balance_token()})"
        )
