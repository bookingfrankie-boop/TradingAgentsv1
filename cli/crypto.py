from __future__ import annotations

import json

import typer

from tradingagents.crypto import CryptoEngine, OKXClient, OKXError

app = typer.Typer(
    name="tradingagents-crypto",
    help="Low-cost OKX crypto scanner/backtester/paper trader",
)


def _print_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


@app.command()
def status():
    """Check public OKX connectivity; private balance is queried only when credentials exist."""
    try:
        client = OKXClient()
        instruments = client.get_instruments("USDC")
        result = {
            "okx_base_url": client.base_url,
            "public_ok": True,
            "usdc_spot_instruments": len(instruments),
            "private_credentials": bool(
                client.api_key and client.secret_key and client.passphrase
            ),
        }
        if result["private_credentials"]:
            result["account_config"] = client.get_account_config()
            result["balance"] = client.get_account_balance()
        _print_json(result)
    except OKXError as exc:
        typer.echo(f"OKX ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def scan():
    """Scan configured USDC spot pairs using deterministic technical rules."""
    try:
        _print_json(CryptoEngine().scan())
    except OKXError as exc:
        typer.echo(f"OKX ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def backtest(symbol: str = typer.Argument(..., help="e.g. SOL-USDC")):
    """Backtest one USDC spot pair with fees and slippage."""
    try:
        _print_json(CryptoEngine().backtest(symbol))
    except (OKXError, ValueError) as exc:
        typer.echo(f"BACKTEST ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def paper():
    """Run one paper-trading evaluation step; never submits an exchange order."""
    try:
        _print_json(CryptoEngine().paper_step())
    except (OKXError, ValueError) as exc:
        typer.echo(f"PAPER ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
