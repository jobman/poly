import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from eth_account import Account
from eth_utils import keccak

from py_clob_client_v2 import ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions, Side
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams, OrderPayload
from py_clob_client_v2.config import get_contract_config


DATA_API_URL = "https://data-api.polymarket.com"
DEFAULT_STATE_FILE = "satt_live_state.json"


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def build_client():
    load_dotenv(".env")
    host = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").strip()
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip()
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))

    if not private_key or not funder:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS are required")

    temp_client = ClobClient(
        host=host,
        key=private_key,
        chain_id=chain_id,
        funder=funder,
        signature_type=signature_type,
    )
    api_creds = temp_client.create_or_derive_api_key()
    client = ClobClient(
        host=host,
        key=private_key,
        chain_id=chain_id,
        creds=api_creds,
        signature_type=signature_type,
        funder=funder,
    )
    return client, chain_id, funder, signature_type


def get_rpc_url(chain_id):
    explicit = os.getenv("POLYGON_RPC_URL", "").strip()
    if explicit:
        return explicit

    rpc_token = os.getenv("RPC_TOKEN", "").strip()
    if not rpc_token:
        raise RuntimeError("Set POLYGON_RPC_URL or RPC_TOKEN for on-chain balanceOf checks")

    if chain_id == 137:
        return f"https://polygon-mainnet.g.alchemy.com/v2/{rpc_token}"
    if chain_id == 80002:
        return f"https://polygon-amoy.g.alchemy.com/v2/{rpc_token}"
    raise RuntimeError(f"Unsupported chain id for RPC helper: {chain_id}")


def rpc_call_balance_of(rpc_url, contract_address, owner, token_id):
    selector = keccak(text="balanceOf(address,uint256)")[:4].hex()
    owner_arg = owner.lower().replace("0x", "").rjust(64, "0")
    token_arg = hex(int(str(token_id)))[2:].rjust(64, "0")
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {
                "to": contract_address,
                "data": f"0x{selector}{owner_arg}{token_arg}",
            },
            "latest",
        ],
        "id": 1,
    }
    response = requests.post(rpc_url, json=payload, timeout=20)
    response.raise_for_status()
    result = response.json()
    if "error" in result:
        raise RuntimeError(f"RPC error: {result['error']}")
    return int(result["result"], 16)


def get_positions(profile_address):
    response = requests.get(
        f"{DATA_API_URL}/positions",
        params={"user": profile_address},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def resolve_order_id(payload):
    if isinstance(payload, dict):
        for key in ("orderID", "id", "orderId"):
            if payload.get(key):
                return str(payload[key])
    return None


def print_json(title, payload):
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def cmd_balance(args):
    client, chain_id, funder, signature_type = build_client()
    contract_config = get_contract_config(chain_id)
    rpc_url = get_rpc_url(chain_id)

    api_balance = client.get_balance_allowance(
        BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=str(args.token_id),
            signature_type=signature_type,
        )
    )
    api_raw = int(str((api_balance or {}).get("balance") or 0))
    chain_raw = rpc_call_balance_of(
        rpc_url=rpc_url,
        contract_address=contract_config.conditional_tokens,
        owner=funder,
        token_id=args.token_id,
    )

    print(f"Wallet: {funder}")
    print(f"Chain ID: {chain_id}")
    print(f"CTF contract: {contract_config.conditional_tokens}")
    print(f"Token ID: {args.token_id}")
    print(f"Conditional balance via CLOB balance_allowance: {api_raw}")
    print(f"Conditional balance via Polygon balanceOf: {chain_raw}")
    print(f"Match: {api_raw == chain_raw}")
    print_json("Raw API response", api_balance or {})


def cmd_limit_cycle(args):
    client, chain_id, funder, _ = build_client()
    expiration = int(time.time()) + int(args.expiration_seconds)

    print(f"Wallet: {funder}")
    print(f"Chain ID: {chain_id}")
    print(f"Placing SELL LIMIT for token {args.token_id} size={args.size} price={args.price}")

    response = client.create_and_post_order(
        order_args=OrderArgs(
            token_id=str(args.token_id),
            price=float(args.price),
            size=float(args.size),
            side=Side.SELL,
            expiration=expiration,
        ),
        options=PartialCreateOrderOptions(tick_size=str(args.tick_size)),
        order_type=OrderType.GTD,
    )
    order_id = resolve_order_id(response)
    print_json("Place order response", response)
    if not order_id:
        raise RuntimeError("Could not resolve order id from place response")

    open_orders = client.get_open_orders()
    matching_open = [order for order in open_orders if str(order.get("id") or order.get("orderID") or order.get("orderId")) == order_id]
    print_json("Matching open order after placement", matching_open)

    cancel_response = client.cancel_order(OrderPayload(orderID=order_id))
    print_json("Cancel response", cancel_response)

    time.sleep(max(1, int(args.post_cancel_wait_seconds)))
    open_orders_after = client.get_open_orders()
    still_open = [order for order in open_orders_after if str(order.get("id") or order.get("orderID") or order.get("orderId")) == order_id]
    print_json("Matching open order after cancel", still_open)
    print(f"Cancelled successfully: {len(still_open) == 0}")


def cmd_recovery_cleanup(args):
    client, chain_id, funder, _ = build_client()
    positions = get_positions(funder)
    open_orders = client.get_open_orders()
    state_path = Path(args.state_file)
    state = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))

    active_asset_ids = {
        str(position.get("asset") or position.get("asset_id") or "")
        for position in positions
        if safe_float(position.get("size")) > 0
    }
    tracked_limit_orders = state.get("limit_orders", {}) if isinstance(state, dict) else {}

    orphan_orders = []
    for order in open_orders:
        order_id = str(order.get("id") or order.get("orderID") or order.get("orderId") or "")
        asset_id = str(order.get("asset_id") or order.get("assetId") or order.get("token_id") or "")
        side = str(order.get("side") or "").upper()
        if side != "SELL":
            continue
        if asset_id in active_asset_ids:
            continue
        orphan_orders.append(
            {
                "order_id": order_id,
                "asset_id": asset_id,
                "price": order.get("price"),
                "size": order.get("original_size") or order.get("size"),
                "tracked_in_state": asset_id in tracked_limit_orders,
            }
        )

    print(f"Wallet: {funder}")
    print(f"Chain ID: {chain_id}")
    print(f"Active position asset ids: {sorted(active_asset_ids)}")
    print_json("Detected orphan SELL open orders", orphan_orders)

    if not args.execute:
        print("Dry run only. Re-run with --execute to cancel detected orphan orders.")
        return

    cancelled = []
    failed = []
    for orphan in orphan_orders:
        try:
            response = client.cancel_order(OrderPayload(orderID=orphan["order_id"]))
            cancelled.append({"order_id": orphan["order_id"], "response": response})
        except Exception as error:
            failed.append({"order_id": orphan["order_id"], "error": str(error)})

    print_json("Cancelled orphan orders", cancelled)
    print_json("Failed cancellations", failed)


def build_parser():
    parser = argparse.ArgumentParser(description="Live Polymarket diagnostics for balance, limit order lifecycle, and recovery cleanup.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    balance_parser = subparsers.add_parser("balance", help="Compare conditional balance from CLOB API vs Polygon CTF balanceOf")
    balance_parser.add_argument("--token-id", required=True, help="Conditional token id")
    balance_parser.set_defaults(func=cmd_balance)

    limit_parser = subparsers.add_parser("limit-cycle", help="Place a real SELL LIMIT and cancel it")
    limit_parser.add_argument("--token-id", required=True, help="Conditional token id")
    limit_parser.add_argument("--price", required=True, type=float, help="Limit sell price")
    limit_parser.add_argument("--size", required=True, type=float, help="Share size to sell")
    limit_parser.add_argument("--tick-size", default="0.01", help="Tick size")
    limit_parser.add_argument("--expiration-seconds", type=int, default=300, help="Seconds until order expiry")
    limit_parser.add_argument("--post-cancel-wait-seconds", type=int, default=2, help="Wait before verifying cancellation")
    limit_parser.set_defaults(func=cmd_limit_cycle)

    cleanup_parser = subparsers.add_parser("recovery-cleanup", help="Inspect or cancel orphan SELL open orders against live Polygon account state")
    cleanup_parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Path to bot state file")
    cleanup_parser.add_argument("--execute", action="store_true", help="Actually cancel orphan orders")
    cleanup_parser.set_defaults(func=cmd_recovery_cleanup)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
