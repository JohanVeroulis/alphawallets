"""PnL pipeline — FIFO cost basis, airdrop-aware, per ADR 0012.

Reads decoded fetcher tables (uniswap_v3_swap, erc20_transfer, token_price);
writes wallet_pnl. No Alchemy calls — pure derivation over the local cache.

Module layout mirrors the ADR 0012 implementation plan:

    models.py              # CostBasisLot, Realization, WalletPnL (frozen)
    airdrop_registry.py    # Hand-verified distribution contracts
    cost_basis.py          # FIFO engine, pure functions
    transfer_treatment.py  # Transfer classifier
    calculator.py          # Orchestrator
    writer.py              # DuckDB writer
    __main__.py            # CLI
"""
