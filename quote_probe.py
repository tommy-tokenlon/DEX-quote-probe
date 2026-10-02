"""
Tokenlon large-quote competitiveness probe (prototype v3).

Pulls same-moment quotes for ETH/USDT, ETH/USDC, ETH/DAI and USDT/USDC (both directions) at
$10K / $50K / $100K from
Tokenlon v5, Tokenlon v6 and competitors, normalises them to net USD received
(after fees and gas), and logs gaps in bps.

Venues:
  - Tokenlon v5  imToken mainnet Market tab (main volume). SockJS/STOMP WebSocket via
                 publisher.tokenlon.im; throwaway key generated per run (in memory only)
                 to sign for a JWT. Received amount already net of Tokenlon fee; gas paid
                 by relayer (gasFee reported in feeDetail; whether it is charged is unconfirmed).
  - Tokenlon v6  web app / non-mainnet imToken. Public GET api.tokenlon.im/v6/swap/quote/1.
                 amountOut is BEFORE fee and BEFORE gas -> script deducts feeFactor once and
                 gasUsed x gasPrice.
  - CoW Swap     public quote API. Gas netted in fee; excludes solver surplus.
  - ParaSwap     public prices API.
  - Uniswap v3   QuoterV2 eth_call, single pool (floor, not a routed price).
  - 1inch        Swap API (ONEINCH_API_KEY). Counts as a competitor; note the API quote sits ~24 bps
                 below the 1inch web app (unexplained), so it may understate 1inch.

Usage:
  pip install -r requirements.txt
  python3 quote_probe.py           # live run, appends to data/quotes_log_v3.csv  (Windows: python)
  python3 quote_probe.py --mock    # offline logic test
  python3 report.py                # weekly overview from the log (see report.py)

Env (optional):
  ETH_RPC_URL       default https://ethereum-rpc.publicnode.com
  ONEINCH_API_KEY   1inch key
"""
import csv
import json
import os
import random
import ssl
import string
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal

import requests

# ---------------------------------------------------------------- config
RPC_URL = os.environ.get("ETH_RPC_URL", "https://ethereum-rpc.publicnode.com")
ONEINCH_KEY = os.environ.get("ONEINCH_API_KEY", "")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(DATA_DIR, "quotes_log_v3.csv")
TIMEOUT = 15

QUOTE_FROM = "0x0000000000000000000000000000000000000001"   # CoW needs a `from`; no funds
V6_TEST_ADDRESS = "0x1111111111111111111111111111111111111111"  # any address works

TOKENS = {
    # symbol: (erc20 address, decimals, is_stable, native address used by Tokenlon v6)
    "ETH":  ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18, False, "0x0000000000000000000000000000000000000000"),
    "USDT": ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6, True, "0xdAC17F958D2ee523a2206206994597C13D831ec7"),
    "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6, True, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    "DAI":  ("0x6B175474E89094C44Da98b954EedeAC495271d0F", 18, True, "0x6B175474E89094C44Da98b954EedeAC495271d0F"),
}

# (sell, buy) — Metabase convention A_B = user sells A for B. Each pair is probed both ways.
PAIRS = [("ETH", "USDT"), ("ETH", "USDC"), ("ETH", "DAI"), ("USDT", "USDC")]
SCENARIOS = [d for a, b in PAIRS for d in ((a, b), (b, a))]
SIZES_USD = [10_000, 50_000, 100_000]

TOKENLON_VENUES = {"Tokenlon v5", "Tokenlon v6"}
EXCLUDE_FROM_BEST = set()       # venues logged but never counted as "best"

UNI_QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
UNI_FEE_TIERS = [100, 500, 3000]
UNI_BASE_GAS = 60_000
CHAINLINK_ETH_USD = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"
UNI_USDC_WETH_005 = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"
SEL_SLOT0 = "3850c7bd"
SEL_QUOTE_EXACT_INPUT_SINGLE = "c6a5026a"
SEL_LATEST_ROUND_DATA = "feaf968c"

ONEINCH_BASE = "https://api.1inch.com/swap"
ONEINCH_VERSIONS = ["v6.0", "v5.2"]
_oneinch_ok_version = None


# ---------------------------------------------------------------- helpers
def rpc(method, params):
    r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]


def eth_call(to, data):
    return rpc("eth_call", [{"to": to, "data": "0x" + data}, "latest"])


def pad_addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def pad_int(n):
    return format(int(n), "x").rjust(64, "0")


def eth_usd_mid():
    try:
        res = eth_call(UNI_USDC_WETH_005, SEL_SLOT0)[2:]
        sqrt_p = int(res[0:64], 16)
        return 10**12 / ((sqrt_p / 2**96) ** 2), "uniswap_mid"
    except Exception:
        res = eth_call(CHAINLINK_ETH_USD, SEL_LATEST_ROUND_DATA)[2:]
        return int(res[64:128], 16) / 1e8, "chainlink"


def gas_price_wei():
    return int(rpc("eth_gasPrice", []), 16)


def human_to_raw(amount_str, decimals):
    return int(Decimal(str(amount_str)) * (Decimal(10) ** decimals))


def usd_value(sym, raw_amount, eth_usd):
    amt = raw_amount / (10 ** TOKENS[sym][1])
    return amt if TOKENS[sym][2] else amt * eth_usd


def fmt_amount(x, sym):
    """Human-readable sell amount used identically by every venue."""
    return f"{x:.0f}" if TOKENS[sym][2] else f"{x:.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- Tokenlon v5
class TokenlonV5Quoter:
    """Tokenlon v5 quote client. Throwaway key per run."""
    PUBLISHER_RPC = "https://publisher.tokenlon.im/rpc"
    PUBLISHER_WS = "wss://publisher.tokenlon.im/exchange"
    STRATEGY = "AMMV2,RFQV2"

    def __init__(self):
        from eth_account import Account  # lazy import: only needed for v5
        self.account = Account.create()
        self.address = self.account.address
        self.ws = None
        self._sub_seq = 0

    def _jwt(self):
        from eth_account.messages import encode_defunct
        ts = int(time.time())
        sig = self.account.sign_message(encode_defunct(text=str(ts))).signature.hex()
        sig = sig if sig.startswith("0x") else "0x" + sig
        resp = requests.post(self.PUBLISHER_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "auth.getSdkJwtToken",
                                                      "params": {"timestamp": ts, "signature": sig}}, timeout=TIMEOUT).json()
        if resp.get("error"):
            raise RuntimeError(f"auth failed: {resp['error']}")
        return resp["result"]

    def connect(self):
        import websocket
        auth = urllib.parse.quote(f"Token {self._jwt()}")
        server = f"{random.randint(0, 999):03d}"
        session = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        # python.org macOS builds ship an empty system CA store; fall back to certifi only then,
        # so OS/corporate roots still apply wherever the system store works (e.g. Windows).
        sslopt = {}
        if ssl.create_default_context().cert_store_stats()["x509_ca"] == 0:
            import certifi
            sslopt = {"ca_certs": certifi.where()}
        self.ws = websocket.create_connection(f"{self.PUBLISHER_WS}/{server}/{session}/websocket?Authorization={auth}",
                                              timeout=30, sslopt=sslopt)
        if self.ws.recv() != "o":
            raise RuntimeError("SockJS open frame not received")
        self._send("CONNECT\naccept-version:1.1,1.0\nheart-beat:0,0\n\n\x00")
        for frame in self._recv_frames():
            if frame.startswith("CONNECTED"):
                return
            raise RuntimeError(f"STOMP connect failed: {frame[:200]}")

    def close(self):
        if self.ws:
            try:
                self._send("DISCONNECT\n\n\x00")
                self.ws.close()
            finally:
                self.ws = None

    def _send(self, frame):
        self.ws.send(json.dumps([frame]))

    def _recv_frames(self):
        while True:
            msg = self.ws.recv()
            if msg == "h":
                continue
            if msg.startswith("a"):
                return json.loads(msg[1:])
            if msg.startswith("c"):
                raise RuntimeError(f"SockJS closed: {msg}")

    def quote(self, base, quote, amount, timeout=15):
        if not self.ws:
            self.connect()
        self._sub_seq += 1
        sub_id = f"sub-{self._sub_seq}"
        topic = f"/user/order/{base}_{quote}/SELL/{amount}/{self.address}"
        self._send(f"SUBSCRIBE\nid:{sub_id}\ndestination:{topic}\n"
                   "X-TOKENLON-VERSION:5.2.0\nX-DEVICE-TOKEN:tokenlon-v5-sdk\n"
                   f"X-SEND-BY-RELAYER:true\nX-STRATEGY:{self.STRATEGY}\n\n\x00")
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                for frame in self._recv_frames():
                    if not frame.startswith("MESSAGE") or f"subscription:{sub_id}" not in frame:
                        continue
                    body = frame.split("\n\n", 1)[1].rstrip("\x00")
                    if body == "None":
                        continue
                    return json.loads(body)
            raise TimeoutError(f"no quote for {topic}")
        finally:
            try:
                self._send(f"UNSUBSCRIBE\nid:{sub_id}\n\n\x00")
            except Exception:
                pass


def quote_tokenlon_v5(sell, buy, amount_str, ctx):
    if ctx.get("v5") is None:
        ctx["v5"] = TokenlonV5Quoter()
    msg = ctx["v5"].quote(sell, buy, amount_str)
    if not msg.get("exchangeable"):
        raise RuntimeError(f"no quote: {msg.get('reason') or msg.get('message')}")
    order = msg["order"]
    fee = int(order["feeFactor"])
    maker = int(order["makerAssetAmount"])
    received = maker * (10000 - fee) // 10000          # same as SDK Quoter.receivedAmount
    gas_fee = (order.get("feeDetail") or {}).get("gasFee")
    return {"out_raw": received, "pre_fee_out_raw": maker, "gas_usd": 0.0, "fee_bps": fee,
            "source": f"{order.get('protocol')}/{msg.get('mmCode')}", "quote_id": order.get("quoteId", ""),
            "note": f"relayer gas; feeDetail.gasFee={gas_fee}"}


# ---------------------------------------------------------------- Tokenlon v6
def quote_tokenlon_v6(sell, buy, amount_str, ctx):
    r = requests.get("https://api.tokenlon.im/v6/swap/quote/1", params={
        "userAddress": V6_TEST_ADDRESS, "recipient": V6_TEST_ADDRESS,
        "fromToken": TOKENS[sell][3], "toToken": TOKENS[buy][3],
        "amount": amount_str, "slippage": 100}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:80]}")
    d = r.json()
    fee = int(d["feeFactor"])
    pre = human_to_raw(d["amountOut"], TOKENS[buy][1])           # before fee, before gas
    net = pre * (10000 - fee) // 10000                              # fee applied ONCE
    gas_usd = int(d.get("gasUsed") or 0) * ctx["gas_price"] / 1e18 * ctx["eth_usd"]
    protos = sorted({s.get("protocol", "?") for route in d.get("routes", []) for hop in route for s in hop})
    return {"out_raw": net, "pre_fee_out_raw": pre, "gas_usd": gas_usd, "fee_bps": fee,
            "source": f"{d.get('orderType')}/{'+'.join(protos)}", "quote_id": d.get("uniqueId", ""),
            "note": f"gasUsed={d.get('gasUsed')}"}


# ---------------------------------------------------------------- competitors
def quote_cow(sell, buy, amount_str, ctx):
    body = {"sellToken": TOKENS[sell][0], "buyToken": TOKENS[buy][0], "from": QUOTE_FROM, "receiver": QUOTE_FROM,
            "kind": "sell", "sellAmountBeforeFee": str(human_to_raw(amount_str, TOKENS[sell][1])),
            "partiallyFillable": False, "signingScheme": "eip712", "onchainOrder": False, "priceQuality": "optimal"}
    r = requests.post("https://api.cow.fi/mainnet/api/v1/quote", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    q = r.json()["quote"]
    return {"out_raw": int(q["buyAmount"]), "gas_usd": 0.0, "note": "gas in fee; excl. surplus"}


def quote_paraswap(sell, buy, amount_str, ctx):
    r = requests.get("https://api.paraswap.io/prices", params={
        "srcToken": TOKENS[sell][0], "destToken": TOKENS[buy][0],
        "amount": str(human_to_raw(amount_str, TOKENS[sell][1])),
        "srcDecimals": TOKENS[sell][1], "destDecimals": TOKENS[buy][1], "side": "SELL", "network": 1}, timeout=TIMEOUT)
    r.raise_for_status()
    pr = r.json()["priceRoute"]
    return {"out_raw": int(pr["destAmount"]), "gas_usd": float(pr.get("gasCostUSD") or 0), "note": "aggregator"}


def quote_uniswap(sell, buy, amount_str, ctx):
    sell_raw = human_to_raw(amount_str, TOKENS[sell][1])
    best = None
    for fee in UNI_FEE_TIERS:
        data = (SEL_QUOTE_EXACT_INPUT_SINGLE + pad_addr(TOKENS[sell][0]) + pad_addr(TOKENS[buy][0])
                + pad_int(sell_raw) + pad_int(fee) + pad_int(0))
        try:
            res = eth_call(UNI_QUOTER_V2, data)[2:]
        except Exception:
            continue
        out, gas_est = int(res[0:64], 16), int(res[192:256], 16)
        if best is None or out > best[0]:
            best = (out, gas_est, fee)
    if best is None:
        raise RuntimeError("no uniswap v3 pool")
    out, gas_est, fee = best
    gas_usd = (gas_est + UNI_BASE_GAS) * ctx["gas_price"] / 1e18 * ctx["eth_usd"]
    return {"out_raw": out, "gas_usd": gas_usd, "note": f"single pool fee={fee}"}


def quote_1inch(sell, buy, amount_str, ctx):
    global _oneinch_ok_version
    if not ONEINCH_KEY:
        raise RuntimeError("skipped: ONEINCH_API_KEY not set")
    last = None
    for v in ([_oneinch_ok_version] if _oneinch_ok_version else ONEINCH_VERSIONS):
        r = requests.get(f"{ONEINCH_BASE}/{v}/1/quote",
                         params={"src": TOKENS[sell][0], "dst": TOKENS[buy][0],
                                 "amount": str(human_to_raw(amount_str, TOKENS[sell][1])), "includeGas": "true"},
                         headers={"Authorization": f"Bearer {ONEINCH_KEY}", "accept": "application/json"},
                         timeout=TIMEOUT)
        if r.status_code in (404, 410):
            last = f"{v}: HTTP {r.status_code}"
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{v}: HTTP {r.status_code} {r.text[:80]}")
        j = r.json()
        _oneinch_ok_version = v
        gas_usd = int(j.get("gas") or 0) * ctx["gas_price"] / 1e18 * ctx["eth_usd"]
        return {"out_raw": int(j.get("dstAmount") or j.get("toAmount")), "gas_usd": gas_usd,
                "note": f"classic {v}; excluded from best"}
    raise RuntimeError(last or "1inch: no working version")


VENUES = {
    "Tokenlon v5": quote_tokenlon_v5,
    "Tokenlon v6": quote_tokenlon_v6,
    "CoW Swap": quote_cow,
    "ParaSwap": quote_paraswap,
    "Uniswap v3": quote_uniswap,
    "1inch": quote_1inch,
}


# ---------------------------------------------------------------- mock
def mock_venue(name):
    def f(sell, buy, amount_str, ctx):
        in_usd = float(amount_str) * (1 if TOKENS[sell][2] else ctx["eth_usd"])
        base = {"Tokenlon v5": 4, "Tokenlon v6": 8, "CoW Swap": 2, "ParaSwap": 3, "Uniswap v3": 6, "1inch": 26}[name]
        pre_usd = in_usd * (1 - (base + random.uniform(-2, 2)) / 1e4)
        to_raw = lambda usd: int(usd / (1 if TOKENS[buy][2] else ctx["eth_usd"]) * 10 ** TOKENS[buy][1])
        if name in TOKENLON_VENUES:
            return {"out_raw": to_raw(pre_usd * (1 - 30 / 1e4)), "pre_fee_out_raw": to_raw(pre_usd),
                    "gas_usd": 0.0 if name == "Tokenlon v5" else 1.5, "fee_bps": 30, "source": "MOCK", "note": "MOCK"}
        return {"out_raw": to_raw(pre_usd), "gas_usd": 0.0 if name == "CoW Swap" else 1.0, "note": "MOCK"}
    return f


# ---------------------------------------------------------------- main
def bps(x, ref):
    return round((x - ref) / ref * 1e4, 2) if (x is not None and ref) else ""


def run(mock=False):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if mock:
        ctx = {"eth_usd": 2650.0, "gas_price": 2 * 10**9, "ref": "mock"}
        venues = {v: mock_venue(v) for v in VENUES}
    else:
        mid, src = eth_usd_mid()
        ctx = {"eth_usd": mid, "gas_price": gas_price_wei(), "ref": src}
        venues = VENUES
    print(f"[{ts} UTC] ETH/USD={ctx['eth_usd']:.2f} ({ctx['ref']})  gas={ctx['gas_price']/1e9:.2f} gwei  mock={mock}")

    rows = []
    try:
        for sell, buy in SCENARIOS:
            for size in SIZES_USD:
                amount_str = fmt_amount(size if TOKENS[sell][2] else size / ctx["eth_usd"], sell)
                in_usd = float(amount_str) * (1 if TOKENS[sell][2] else ctx["eth_usd"])
                res = {}
                for name, fn in venues.items():
                    try:
                        q = fn(sell, buy, amount_str, ctx)
                        net = usd_value(buy, q["out_raw"], ctx["eth_usd"]) - q["gas_usd"]
                        pre = (usd_value(buy, q["pre_fee_out_raw"], ctx["eth_usd"]) - q["gas_usd"]
                               if "pre_fee_out_raw" in q else None)
                        res[name] = dict(q, net=net, pre=pre, err="")
                    except Exception as e:
                        res[name] = {"net": None, "pre": None, "err": str(e)[:150]}
                    time.sleep(0.2)

                ok = {k: v for k, v in res.items() if v["net"] is not None}
                eligible = {k: v for k, v in ok.items() if k not in EXCLUDE_FROM_BEST}
                comps = {k: v for k, v in eligible.items() if k not in TOKENLON_VENUES}
                best_venue = max(eligible, key=lambda k: eligible[k]["net"]) if eligible else ""
                best = eligible[best_venue]["net"] if eligible else None
                best_comp_venue = max(comps, key=lambda k: comps[k]["net"]) if comps else ""
                best_comp = comps[best_comp_venue]["net"] if comps else None

                for name, r in res.items():
                    rows.append({
                        "ts_utc": ts, "pair": f"{sell}_{buy}", "size_usd": size, "sell_amount": amount_str,
                        "in_usd": round(in_usd, 2), "venue": name,
                        "net_usd": round(r["net"], 2) if r["net"] is not None else "",
                        "pre_fee_net_usd": round(r["pre"], 2) if r.get("pre") is not None else "",
                        "fee_bps": r.get("fee_bps", ""), "gas_usd": round(r["gas_usd"], 2) if r.get("gas_usd") is not None and r["net"] is not None else "",
                        "bps_vs_mid": bps(r["net"], in_usd),
                        "bps_vs_best": bps(r["net"], best),
                        "bps_vs_best_competitor": bps(r["net"], best_comp),
                        "pre_fee_bps_vs_best_competitor": bps(r.get("pre"), best_comp),
                        "best_venue": best_venue, "best_competitor": best_comp_venue,
                        "is_best": int(name == best_venue), "source": r.get("source", ""),
                        "quote_id": r.get("quote_id", ""), "note": r.get("note", ""), "error": r["err"],
                        "mock": int(mock), "ref_price": round(ctx["eth_usd"], 2), "ref_source": ctx["ref"],
                        "gas_gwei": round(ctx["gas_price"] / 1e9, 3),
                    })

                cells = []
                for k, r in res.items():
                    if r["net"] is None:
                        cells.append(f"{k}:—")
                    else:
                        s = f"{k}:{bps(r['net'], best_comp):+.1f}"
                        if r.get("pre") is not None:
                            s += f"(pre-fee {bps(r['pre'], best_comp):+.1f})"
                        cells.append(s)
                print(f"{sell + '_' + buy:<10} ${size:>7,}  best competitor={best_comp_venue:<9} | " + "  ".join(cells))
    finally:
        if ctx.get("v5") is not None:
            ctx["v5"].close()

    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if new_file:
            w.writeheader()
        w.writerows(rows)
    print(f"\nbps shown vs best competitor (excl. Tokenlon). {len(rows)} rows -> {LOG_FILE}")
    errs = [r for r in rows if r["error"] and "not set" not in r["error"]]
    for r in errs:
        print(f"  ! {r['pair']} {r['size_usd']} {r['venue']}: {r['error']}")


if __name__ == "__main__":
    run(mock="--mock" in sys.argv)
