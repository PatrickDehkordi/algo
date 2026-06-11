#!/usr/bin/env python3
"""
Simplified Avellaneda-Stoikov market maker for Robinhood (default: SPY).
Routes orders through the Agentic account (agentic_allowed=True) via the
official Robinhood MCP endpoint — no username/password required.

Vol pipeline:
  1. VolEstimator     — GK (5-min) / YZ (daily) / blend via yfinance.
  2. VolOfVolDetector — widens spread when vol itself is changing fast.
  3. FillRateTracker  — auto-tunes kappa toward a target fill rate per side.

Run:
    python3 algo/market_maker.py
    python3 algo/market_maker.py --quote-size 0.1 --max-loss 10
"""

import math
import time
import logging
import argparse
import os
from dataclasses import dataclass
from typing import Optional

import yfinance as yf

from rh_client import RHMCPClient


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class Config:
    symbol: str = "SPY"
    agentic_account: str = ""   # set via --account or RH_AGENTIC_ACCOUNT env var

    # Position limits
    max_inventory: float = 5.0
    quote_size: float = 1.0

    # Avellaneda-Stoikov parameters
    gamma: float = 0.1
    sigma: float = 0.15             # fallback if vol estimator fails
    T: float = 1 / 390             # time horizon per cycle (1 min / 6.5hr day)
    kappa: float = 1.5

    # Spread bounds (applied after VoV multiplier)
    min_half_spread: float = 0.01
    max_half_spread: float = 0.50

    # Volatility estimator
    vol_method: str = "blend"       # gk | yz | blend
    vol_gk_lookback: int = 78       # GK: 5-min bars (78 = 1 trading day)
    vol_yz_lookback: int = 30       # YZ: daily bars (~6 weeks)
    vol_refresh_secs: int = 600

    # Vol-of-vol detector
    vov_window: int = 6
    vov_threshold: float = 0.20
    vov_scale: float = 3.0
    vov_max_mult: float = 2.0

    # Fill-rate tracker / kappa auto-tuner
    frt_window: int = 20
    frt_target: float = 0.30
    frt_kappa_min: float = 0.3
    frt_kappa_max: float = 15.0
    frt_adj_rate: float = 0.05
    frt_imbalance_warn: float = 0.25

    # Loop
    poll_interval: int = 30

    # Kill switch
    max_loss: float = 50.0


# ── Volatility estimator (yfinance) ───────────────────────────────────────────

class VolEstimator:
    """
    Annualized realized volatility via three methods (data from yfinance):

    gk    — Garman-Klass on 5-min intraday bars.
    yz    — Yang-Zhang on daily bars (handles overnight gaps).
    blend — arithmetic mean of gk and yz.
    """

    _GK_BARS_PER_YEAR = 252 * 78   # 5-min bars per trading year

    def __init__(self, symbol: str, method: str, gk_lookback: int,
                 yz_lookback: int, fallback: float):
        if method not in ("gk", "yz", "blend"):
            raise ValueError(f"Unknown vol method: {method!r}")
        self.symbol = symbol
        self.method = method
        self.gk_lookback = gk_lookback
        self.yz_lookback = yz_lookback
        self.sigma = fallback
        self._last_refresh: float = 0.0
        self._log = logging.getLogger("vol")

    def refresh(self) -> float:
        if self.method == "gk":
            result = self._garman_klass()
        elif self.method == "yz":
            result = self._yang_zhang()
        else:
            gk = self._garman_klass()
            yz = self._yang_zhang()
            result = (gk + yz) / 2
            self._log.info(f"σ (blend) = ({gk:.4f} + {yz:.4f}) / 2 = {result:.4f}")
        self.sigma = result
        self._last_refresh = time.monotonic()
        return self.sigma

    def needs_refresh(self, interval_secs: int) -> bool:
        return (time.monotonic() - self._last_refresh) >= interval_secs

    def _garman_klass(self) -> float:
        try:
            df = yf.download(self.symbol, period="5d", interval="5m",
                             progress=False, auto_adjust=True)
        except Exception as exc:
            self._log.warning(f"GK download failed: {exc}")
            return self.sigma
        if df.empty:
            return self.sigma
        rows = df.tail(self.gk_lookback)
        variances: list[float] = []
        for _, row in rows.iterrows():
            try:
                o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
            except (KeyError, TypeError, ValueError):
                continue
            if o <= 0 or l <= 0 or h < l or c <= 0:
                continue
            v = 0.5 * math.log(h / l) ** 2 - (2 * math.log(2) - 1) * math.log(c / o) ** 2
            if v > 0:
                variances.append(v)
        if not variances:
            return self.sigma
        sigma = max(0.01, min(2.0, math.sqrt(sum(variances) / len(variances) * self._GK_BARS_PER_YEAR)))
        self._log.info(f"σ (GK 5min):  {sigma:.4f} ({sigma*100:.1f}% ann)  n={len(variances)}")
        return sigma

    def _yang_zhang(self) -> float:
        try:
            df = yf.download(self.symbol, period="3mo", interval="1d",
                             progress=False, auto_adjust=True)
        except Exception as exc:
            self._log.warning(f"YZ download failed: {exc}")
            return self.sigma
        if df.empty or len(df) < 3:
            return self.sigma
        rows = df.tail(self.yz_lookback)
        ohlc = []
        for _, row in rows.iterrows():
            try:
                ohlc.append({"o": float(row["Open"]), "h": float(row["High"]),
                              "l": float(row["Low"]),  "c": float(row["Close"])})
            except (KeyError, TypeError, ValueError):
                continue
        if len(ohlc) < 3:
            return self.sigma
        overnight  = [math.log(ohlc[i]["o"] / ohlc[i-1]["c"]) for i in range(1, len(ohlc))]
        open_close = [math.log(b["c"] / b["o"]) for b in ohlc[1:]]
        m = len(overnight)
        u_bar, d_bar = sum(overnight) / m, sum(open_close) / m
        denom = m - 1 if m > 1 else 1
        sigma2_o  = sum((u - u_bar) ** 2 for u in overnight) / denom
        sigma2_oc = sum((d - d_bar) ** 2 for d in open_close) / denom
        rs_terms: list[float] = []
        for b in ohlc[1:]:
            h, l, o, c = b["h"], b["l"], b["o"], b["c"]
            if h > 0 and l > 0 and o > 0 and c > 0:
                rs_terms.append(math.log(h/c)*math.log(h/o) + math.log(l/c)*math.log(l/o))
        sigma2_rs = sum(rs_terms) / len(rs_terms) if rs_terms else 0.0
        k = 0.34 / (1.34 + (m + 1) / denom)
        sigma2_yz = sigma2_o + k * sigma2_oc + (1 - k) * sigma2_rs
        if sigma2_yz <= 0:
            return self.sigma
        sigma = max(0.01, min(2.0, math.sqrt(sigma2_yz * 252)))
        self._log.info(
            f"σ (YZ daily): {sigma:.4f} ({sigma*100:.1f}% ann)  "
            f"n={m}d  k={k:.3f}  σ²_o={sigma2_o:.2e}  σ²_oc={sigma2_oc:.2e}  σ²_rs={sigma2_rs:.2e}"
        )
        return sigma


# ── Vol-of-vol detector ───────────────────────────────────────────────────────

class VolOfVolDetector:
    """
    Tracks rolling CV of recent σ estimates; widens spread during regime
    transitions. spread_mult = clip(1 + scale × CV, 1.0, max_mult).
    """

    def __init__(self, window: int, threshold: float, scale: float, max_mult: float):
        self.window = window
        self.threshold = threshold
        self.scale = scale
        self.max_mult = max_mult
        self._history: list[float] = []
        self._log = logging.getLogger("vov")

    def update(self, sigma: float) -> None:
        self._history.append(sigma)
        if len(self._history) > self.window:
            self._history.pop(0)

    @property
    def cv(self) -> float:
        if len(self._history) < 2:
            return 0.0
        n = len(self._history)
        mean = sum(self._history) / n
        if mean <= 0:
            return 0.0
        return math.sqrt(sum((s - mean) ** 2 for s in self._history) / (n - 1)) / mean

    def spread_multiplier(self) -> float:
        if len(self._history) < 2:
            return 1.0
        current_cv = self.cv
        mult = min(self.max_mult, 1.0 + self.scale * current_cv)
        if current_cv >= self.threshold:
            self._log.warning(
                f"HIGH VoV  CV={current_cv:.3f} >= {self.threshold}  mult={mult:.2f}x  "
                f"σ_window={[f'{s:.4f}' for s in self._history]}"
            )
        else:
            self._log.info(f"VoV  CV={current_cv:.3f}  mult={mult:.2f}x")
        return mult


# ── Fill-rate tracker / kappa auto-tuner ─────────────────────────────────────

class FillRateTracker:
    """
    Tracks bid/ask fill rates over a rolling window and nudges kappa to
    converge toward a target fill rate per side.

      error  = combined_fill_rate − target
      Δkappa = −adj_rate × error × kappa
      kappa  = clip(kappa + Δkappa, kappa_min, kappa_max)

    Higher kappa → tighter spread → more fills (and vice versa).
    Imbalance warning fires when |bid_rate − ask_rate| > threshold.
    """

    def __init__(self, window: int, target: float, kappa_min: float,
                 kappa_max: float, adj_rate: float, imbalance_warn: float):
        self.window = window
        self.target = target
        self.kappa_min = kappa_min
        self.kappa_max = kappa_max
        self.adj_rate = adj_rate
        self.imbalance_warn = imbalance_warn
        self._bids: list[bool] = []
        self._asks: list[bool] = []
        self._log = logging.getLogger("frt")

    def record_bid(self, filled: bool) -> None:
        self._bids.append(filled)
        if len(self._bids) > self.window:
            self._bids.pop(0)

    def record_ask(self, filled: bool) -> None:
        self._asks.append(filled)
        if len(self._asks) > self.window:
            self._asks.pop(0)

    @property
    def bid_fill_rate(self) -> float:
        return sum(self._bids) / len(self._bids) if self._bids else 0.0

    @property
    def ask_fill_rate(self) -> float:
        return sum(self._asks) / len(self._asks) if self._asks else 0.0

    @property
    def combined_fill_rate(self) -> float:
        rates = ([self.bid_fill_rate] if self._bids else []) + \
                ([self.ask_fill_rate] if self._asks else [])
        return sum(rates) / len(rates) if rates else 0.0

    @property
    def imbalance(self) -> float:
        return self.bid_fill_rate - self.ask_fill_rate

    def _has_data(self) -> bool:
        return len(self._bids) >= self.window // 2 and len(self._asks) >= self.window // 2

    def adjust_kappa(self, kappa: float) -> float:
        bid_r, ask_r = self.bid_fill_rate, self.ask_fill_rate
        combined = self.combined_fill_rate
        if not self._has_data():
            self._log.info(
                f"FRT warming up  bid={bid_r:.2f}  ask={ask_r:.2f}  "
                f"n=({len(self._bids)},{len(self._asks)})"
            )
            return kappa
        error = combined - self.target
        new_kappa = max(self.kappa_min,
                        min(self.kappa_max, kappa - self.adj_rate * error * kappa))
        self._log.info(
            f"FRT  bid={bid_r:.2f}  ask={ask_r:.2f}  combined={combined:.2f}  "
            f"target={self.target:.2f}  imbal={self.imbalance:+.2f}  κ {kappa:.3f}→{new_kappa:.3f}"
        )
        if abs(self.imbalance) >= self.imbalance_warn:
            side = "BID-heavy" if self.imbalance > 0 else "ASK-heavy"
            self._log.warning(
                f"FILL IMBALANCE ({side})  imbal={self.imbalance:+.3f} — possible adverse selection"
            )
        return new_kappa


# ── Avellaneda-Stoikov math ───────────────────────────────────────────────────

def reservation_price(mid: float, inventory: float, cfg: Config) -> float:
    return mid - inventory * cfg.gamma * cfg.sigma ** 2 * cfg.T


def optimal_half_spread(cfg: Config) -> float:
    as_term  = (cfg.gamma * cfg.sigma ** 2 * cfg.T) / 2
    log_term = (1 / cfg.gamma) * math.log(1 + cfg.gamma / cfg.kappa)
    return as_term + log_term


def compute_quotes(mid: float, inventory: float, cfg: Config,
                   spread_mult: float = 1.0) -> tuple[float, float]:
    r_price = reservation_price(mid, inventory, cfg)
    half = max(cfg.min_half_spread,
               min(cfg.max_half_spread, optimal_half_spread(cfg) * spread_mult))
    return round(r_price - half, 2), round(r_price + half, 2)


# ── State ─────────────────────────────────────────────────────────────────────

@dataclass
class State:
    inventory: float = 0.0
    cost_basis: float = 0.0
    realized_pnl: float = 0.0
    active_bid_id: Optional[str] = None
    active_ask_id: Optional[str] = None
    bid_recorded_qty: float = 0.0  # cumulative qty already booked for active bid
    ask_recorded_qty: float = 0.0  # cumulative qty already booked for active ask

    @property
    def avg_cost(self) -> float:
        return self.cost_basis / self.inventory if self.inventory else 0.0

    def record_buy(self, qty: float, price: float) -> None:
        if self.inventory < 0:
            # Covering a short: realize PnL on the closed portion, then open long with remainder.
            closed = min(qty, -self.inventory)
            self.realized_pnl += closed * (self.avg_cost - price)
            self.cost_basis   += closed * self.avg_cost  # reduces negative cost_basis toward 0
            self.inventory    += qty
            remainder = qty - closed
            if remainder > 0:
                self.cost_basis += remainder * price
        else:
            self.cost_basis += qty * price
            self.inventory  += qty

    def record_sell(self, qty: float, price: float) -> None:
        if self.inventory > 0:
            # Closing a long: realize PnL on the closed portion, then open short with remainder.
            closed = min(qty, self.inventory)
            self.realized_pnl += closed * (price - self.avg_cost)
            self.cost_basis   -= closed * self.avg_cost
            self.inventory    -= qty
            remainder = qty - closed
            if remainder > 0:
                self.cost_basis -= remainder * price  # negative cost_basis tracks short entry
        else:
            # Opening or adding to a short position.
            self.cost_basis -= qty * price
            self.inventory  -= qty

    def unrealized_pnl(self, mid: float) -> float:
        return self.inventory * (mid - self.avg_cost) if self.inventory else 0.0


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("mm")

    log.info(f"=== Market maker: {cfg.symbol}  account={cfg.agentic_account} ===")
    log.info(f"γ={cfg.gamma}  κ={cfg.kappa}  T={cfg.T:.5f}")
    log.info(f"inventory±{cfg.max_inventory}  quote_size={cfg.quote_size}  poll={cfg.poll_interval}s")
    log.info(f"Vol:{cfg.vol_method.upper()} GK={cfg.vol_gk_lookback}bars YZ={cfg.vol_yz_lookback}d refresh={cfg.vol_refresh_secs}s")
    log.info(f"VoV:win={cfg.vov_window} thr={cfg.vov_threshold} scale={cfg.vov_scale} max={cfg.vov_max_mult}x")
    log.info(f"FRT:win={cfg.frt_window} tgt={cfg.frt_target:.0%} κ=[{cfg.frt_kappa_min},{cfg.frt_kappa_max}] adj={cfg.frt_adj_rate}")

    rh  = RHMCPClient(cfg.agentic_account)
    vol = VolEstimator(cfg.symbol, method=cfg.vol_method,
                       gk_lookback=cfg.vol_gk_lookback,
                       yz_lookback=cfg.vol_yz_lookback,
                       fallback=cfg.sigma)
    vov = VolOfVolDetector(window=cfg.vov_window, threshold=cfg.vov_threshold,
                           scale=cfg.vov_scale,   max_mult=cfg.vov_max_mult)
    frt = FillRateTracker(window=cfg.frt_window, target=cfg.frt_target,
                          kappa_min=cfg.frt_kappa_min, kappa_max=cfg.frt_kappa_max,
                          adj_rate=cfg.frt_adj_rate, imbalance_warn=cfg.frt_imbalance_warn)

    log.info("Fetching initial vol estimate...")
    cfg.sigma = vol.refresh()
    vov.update(cfg.sigma)
    log.info(f"Initial σ={cfg.sigma:.4f}  half-spread=${optimal_half_spread(cfg):.4f}")

    state = State()

    try:
        while True:
            # ── 1. Refresh vol + update VoV ───────────────────────────────────
            if vol.needs_refresh(cfg.vol_refresh_secs):
                cfg.sigma = vol.refresh()
                vov.update(cfg.sigma)

            spread_mult = vov.spread_multiplier()

            # ── 2. Poll fills ──────────────────────────────────────────────────
            bid_was_live = state.active_bid_id is not None
            ask_was_live = state.active_ask_id is not None
            bid_filled   = False
            ask_filled   = False

            if state.active_bid_id:
                s, qty, price = rh.order_status(state.active_bid_id)
                delta = qty - state.bid_recorded_qty
                if delta > 0 and price > 0:
                    state.record_buy(delta, price)
                    state.bid_recorded_qty += delta
                    label = "BID FILLED" if s == "filled" else "BID PARTIAL"
                    log.info(f"{label}  +{delta}@{price:.2f} | inv={state.inventory:.2f}")
                if s == "filled":
                    bid_filled = True
                    state.active_bid_id    = None
                    state.bid_recorded_qty = 0.0
                elif s in ("cancelled", "failed", "rejected"):
                    state.active_bid_id    = None
                    state.bid_recorded_qty = 0.0

            if state.active_ask_id:
                s, qty, price = rh.order_status(state.active_ask_id)
                delta = qty - state.ask_recorded_qty
                if delta > 0 and price > 0:
                    state.record_sell(delta, price)
                    state.ask_recorded_qty += delta
                    label = "ASK FILLED" if s == "filled" else "ASK PARTIAL"
                    log.info(f"{label}  -{delta}@{price:.2f} | inv={state.inventory:.2f}")
                if s == "filled":
                    ask_filled = True
                    state.active_ask_id    = None
                    state.ask_recorded_qty = 0.0
                elif s in ("cancelled", "failed", "rejected"):
                    state.active_ask_id    = None
                    state.ask_recorded_qty = 0.0

            # ── 3. Fetch market quote ─────────────────────────────────────────
            result = rh.get_quote(cfg.symbol)
            if result is None:
                log.warning("No quote — skipping cycle")
                time.sleep(cfg.poll_interval)
                continue
            mkt_bid, mkt_ask, mid = result

            # ── 4. PnL + kill-switch ──────────────────────────────────────────
            unrealized = state.unrealized_pnl(mid)
            total_pnl  = state.realized_pnl + unrealized
            eff_half   = min(cfg.max_half_spread,
                             max(cfg.min_half_spread, optimal_half_spread(cfg) * spread_mult))
            log.info(
                f"Mid={mid:.2f}  σ={cfg.sigma:.4f}  CV={vov.cv:.3f}  mult={spread_mult:.2f}x  "
                f"half={eff_half:.4f}  κ={cfg.kappa:.3f}  inv={state.inventory:.2f}  PnL={total_pnl:+.2f}"
            )
            if total_pnl < -cfg.max_loss:
                log.error(f"Max loss hit (${total_pnl:.2f}) — halting")
                break

            # ── 5. Record fills → adjust kappa → cancel stale quotes ──────────
            if bid_was_live:
                frt.record_bid(bid_filled)
            if ask_was_live:
                frt.record_ask(ask_filled)
            cfg.kappa = frt.adjust_kappa(cfg.kappa)

            if state.active_bid_id:
                rh.cancel(state.active_bid_id)
                state.active_bid_id = None
            if state.active_ask_id:
                rh.cancel(state.active_ask_id)
                state.active_ask_id = None

            # ── 6. Compute and place new quotes ───────────────────────────────
            our_bid, our_ask = compute_quotes(mid, state.inventory, cfg, spread_mult)
            log.info(f"Quoting  bid={our_bid}  ask={our_ask}  spread={our_ask - our_bid:.4f}")

            if state.inventory < cfg.max_inventory:
                state.active_bid_id = rh.place_limit(cfg.symbol, "buy",  our_bid, cfg.quote_size)
            else:
                log.info("Max long inventory — skipping bid")

            if state.inventory > -cfg.max_inventory:
                state.active_ask_id = rh.place_limit(cfg.symbol, "sell", our_ask, cfg.quote_size)
            else:
                log.info("Max short inventory — skipping ask")

            time.sleep(cfg.poll_interval)

    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        log.info("Cancelling open orders...")
        if state.active_bid_id:
            rh.cancel(state.active_bid_id)
        if state.active_ask_id:
            rh.cancel(state.active_ask_id)
        log.info(f"=== Shutdown  realized PnL=${state.realized_pnl:.2f}  inv={state.inventory:.2f} ===")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A-S market maker via Robinhood MCP")
    ap.add_argument("--account",             default=os.environ.get("RH_AGENTIC_ACCOUNT", ""),
                                             help="Agentic account number (or set RH_AGENTIC_ACCOUNT)")
    ap.add_argument("--symbol",              default="SPY")
    ap.add_argument("--gamma",               type=float, default=0.1)
    ap.add_argument("--sigma",               type=float, default=0.15,  help="Fallback vol")
    ap.add_argument("--kappa",               type=float, default=1.5)
    ap.add_argument("--max-inventory",       type=float, default=5.0)
    ap.add_argument("--quote-size",          type=float, default=1.0)
    ap.add_argument("--poll-interval",       type=int,   default=30)
    ap.add_argument("--max-loss",            type=float, default=50.0)
    ap.add_argument("--vol-method",          default="blend", choices=["gk", "yz", "blend"])
    ap.add_argument("--vol-gk-lookback",     type=int,   default=78)
    ap.add_argument("--vol-yz-lookback",     type=int,   default=30)
    ap.add_argument("--vol-refresh-secs",    type=int,   default=600)
    ap.add_argument("--vov-window",          type=int,   default=6)
    ap.add_argument("--vov-threshold",       type=float, default=0.20)
    ap.add_argument("--vov-scale",           type=float, default=3.0)
    ap.add_argument("--vov-max-mult",        type=float, default=2.0)
    ap.add_argument("--frt-window",          type=int,   default=20)
    ap.add_argument("--frt-target",          type=float, default=0.30)
    ap.add_argument("--frt-kappa-min",       type=float, default=0.3)
    ap.add_argument("--frt-kappa-max",       type=float, default=15.0)
    ap.add_argument("--frt-adj-rate",        type=float, default=0.05)
    ap.add_argument("--frt-imbalance-warn",  type=float, default=0.25)
    args = ap.parse_args()

    if not args.account:
        raise SystemExit(
            "Agentic account number required.\n"
            "Pass --account XXXXXXXXX  or  export RH_AGENTIC_ACCOUNT=XXXXXXXXX\n"
            "(find it in the Robinhood app under Account → Agentic)"
        )

    cfg = Config(
        agentic_account=args.account,
        symbol=args.symbol,
        gamma=args.gamma,
        sigma=args.sigma,
        kappa=args.kappa,
        max_inventory=args.max_inventory,
        quote_size=args.quote_size,
        poll_interval=args.poll_interval,
        max_loss=args.max_loss,
        vol_method=args.vol_method,
        vol_gk_lookback=args.vol_gk_lookback,
        vol_yz_lookback=args.vol_yz_lookback,
        vol_refresh_secs=args.vol_refresh_secs,
        vov_window=args.vov_window,
        vov_threshold=args.vov_threshold,
        vov_scale=args.vov_scale,
        vov_max_mult=args.vov_max_mult,
        frt_window=args.frt_window,
        frt_target=args.frt_target,
        frt_kappa_min=args.frt_kappa_min,
        frt_kappa_max=args.frt_kappa_max,
        frt_adj_rate=args.frt_adj_rate,
        frt_imbalance_warn=args.frt_imbalance_warn,
    )
    run(cfg)
