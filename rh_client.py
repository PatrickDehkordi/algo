#!/usr/bin/env python3
"""
HTTP client for the Robinhood Agentic MCP endpoint.

Reads the OAuth token that Claude Code stores in ~/.claude/.credentials.json
and sends JSON-RPC calls directly to https://agent.robinhood.com/mcp/trading.
No robin_stocks, no username/password — uses the same token the MCP session
already authenticated.

Token refresh is attempted automatically when within 5 minutes of expiry.
If refresh fails, re-authenticate via Claude Code and restart the algo.
"""

import json
import time
import uuid
import logging
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("rh")

_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
_KEY_PREFIX  = "robinhood-trading|"
_REFRESH_URL = "https://api.robinhood.com/oauth2/token/"


class RHMCPClient:
    """
    Wraps the Robinhood Agentic MCP endpoint.

    All orders are routed to `account_number`, which must be an
    agentic_allowed=True account (the Agentic cash account).
    """

    def __init__(self, account_number: str):
        self.account_number = account_number
        self._session_id: Optional[str] = None
        self._http = requests.Session()
        self._creds_key = ""
        self._server_url = ""
        self._access_token = ""
        self._refresh_token = ""
        self._client_id = ""
        self._expires_at = 0.0   # epoch seconds
        self._load_creds()

    # ── Token management ──────────────────────────────────────────────────────

    def _load_creds(self) -> None:
        raw = json.loads(_CREDS_PATH.read_text())
        mcp = raw.get("mcpOAuth", {})
        key = next((k for k in mcp if k.startswith(_KEY_PREFIX)), None)
        if not key:
            raise RuntimeError(
                "Robinhood MCP credentials not found in ~/.claude/.credentials.json.\n"
                "Re-authenticate: open Claude Code and run the MCP auth flow."
            )
        e = mcp[key]
        self._creds_key  = key
        self._server_url = e["serverUrl"]
        self._access_token  = e["accessToken"]
        self._refresh_token = e["refreshToken"]
        self._client_id  = e["clientId"]
        raw_exp = e.get("expiresAt", 0)
        # expiresAt is epoch-ms in Claude Code; normalise to epoch-s
        self._expires_at = raw_exp / 1000 if raw_exp > 1e10 else float(raw_exp)
        log.info(f"Loaded RH creds (key={key[:30]}…  expires={time.ctime(self._expires_at)})")

    def _ensure_fresh_token(self) -> None:
        if time.time() < self._expires_at - 300:
            return
        log.info("Token expiring — refreshing...")
        resp = self._http.post(
            _REFRESH_URL,
            json={
                "grant_type":    "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id":     self._client_id,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token  = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._expires_at    = time.time() + data.get("expires_in", 3600)
        self._persist_creds()
        log.info("Token refreshed OK")

    def _persist_creds(self) -> None:
        try:
            raw = json.loads(_CREDS_PATH.read_text())
            raw["mcpOAuth"][self._creds_key]["accessToken"]  = self._access_token
            raw["mcpOAuth"][self._creds_key]["refreshToken"] = self._refresh_token
            raw["mcpOAuth"][self._creds_key]["expiresAt"]    = int(self._expires_at * 1000)
            _CREDS_PATH.write_text(json.dumps(raw, indent=2))
        except Exception as exc:
            log.warning(f"Could not persist refreshed token: {exc}")

    # ── MCP transport ─────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json, text/event-stream",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _init_session(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id":      str(uuid.uuid4()),
            "method":  "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities":    {},
                "clientInfo":      {"name": "market-maker", "version": "1.0"},
            },
        }
        resp = self._http.post(self._server_url, json=payload,
                               headers=self._headers(), timeout=15)
        resp.raise_for_status()
        if sid := resp.headers.get("Mcp-Session-Id"):
            self._session_id = sid
            log.info(f"MCP session: {sid}")

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call an MCP tool. Returns the parsed data dict from the response."""
        self._ensure_fresh_token()
        if self._session_id is None:
            self._init_session()

        payload = {
            "jsonrpc": "2.0",
            "id":      str(uuid.uuid4()),
            "method":  "tools/call",
            "params":  {"name": name, "arguments": arguments},
        }
        resp = self._http.post(self._server_url, json=payload,
                               headers=self._headers(), timeout=30)
        resp.raise_for_status()

        rpc = resp.json()
        if "error" in rpc:
            raise RuntimeError(f"MCP {name} error: {rpc['error']}")

        content = rpc.get("result", {}).get("content", [])
        if not content:
            return {}
        return json.loads(content[0].get("text", "{}"))

    # ── Convenience wrappers ──────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Optional[tuple[float, float, float]]:
        """Return (bid, ask, mid). Returns None on failure."""
        try:
            result = self.call_tool("get_equity_quotes", {"symbols": [symbol]})
            quotes = result.get("data", {}).get("quotes", [])
            if not quotes:
                return None
            q = quotes[0]
            bid  = float(q.get("bid_price")       or q.get("last_trade_price") or 0)
            ask  = float(q.get("ask_price")        or q.get("last_trade_price") or 0)
            last = float(q.get("last_trade_price") or 0)
            if ask > bid > 0:
                return bid, ask, (bid + ask) / 2
            if last > 0:
                return last, last, last
            return None
        except Exception as exc:
            log.warning(f"get_quote({symbol}): {exc}")
            return None

    def place_limit(self, symbol: str, side: str, price: float,
                    qty: float) -> Optional[str]:
        """Place a GFD limit order. Returns order_id or None."""
        try:
            result = self.call_tool("place_equity_order", {
                "account_number": self.account_number,
                "symbol":         symbol,
                "side":           side,
                "type":           "limit",
                "limit_price":    f"{price:.2f}",
                "quantity":       str(qty),
                "time_in_force":  "gfd",
                "ref_id":         str(uuid.uuid4()),
            })
            order = result.get("data", {}).get("order", {})
            oid = order.get("id")
            log.info(f"Placed {side} {qty}@{price:.2f} → {oid}")
            return oid
        except Exception as exc:
            log.error(f"place_limit({side} {qty}@{price:.2f}): {exc}")
            return None

    def cancel(self, order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        try:
            self.call_tool("cancel_equity_order", {
                "account_number": self.account_number,
                "order_id":       order_id,
            })
            log.info(f"Cancelled {order_id}")
            return True
        except Exception as exc:
            log.warning(f"cancel({order_id}): {exc}")
            return False

    def order_status(self, order_id: str) -> tuple[str, float, float]:
        """Return (state, filled_qty, avg_fill_price)."""
        try:
            result = self.call_tool("get_equity_orders", {
                "account_number": self.account_number,
                "order_id":       order_id,
            })
            orders = result.get("data", {}).get("orders", [])
            if not orders:
                return "unknown", 0.0, 0.0
            o = orders[0]
            return (
                o.get("state", "unknown"),
                float(o.get("cumulative_quantity") or 0),
                float(o.get("average_price")       or 0),
            )
        except Exception as exc:
            log.warning(f"order_status({order_id}): {exc}")
            return "unknown", 0.0, 0.0
