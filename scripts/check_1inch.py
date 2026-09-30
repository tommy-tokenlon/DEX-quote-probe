"""
One-off check #2: why 1inch API is ~24 bps below the 1inch web app.
  A) prints the full classic quote response (look for any fee fields)
  B) tries the Fusion (intent) quoter, which is what the web app likely uses by default

    python check_1inch.py
Then refresh the web app with 10,000 USDT -> ETH within ~30 seconds and note the ETH received.
"""
import json
import os
import requests

KEY = os.environ.get("ONEINCH_API_KEY", "")
if not KEY:
    raise SystemExit("ONEINCH_API_KEY not set in this window")
H = {"Authorization": f"Bearer {KEY}", "accept": "application/json"}

USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
ETH_NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
AMOUNT = str(10_000 * 10**6)
WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"  # any public EOA; quote only, nothing is signed

print("=== A) classic quote, full response ===")
r = requests.get("https://api.1inch.com/swap/v6.0/1/quote",
                 params={"src": USDT, "dst": ETH_NATIVE, "amount": AMOUNT,
                         "includeProtocols": "true", "includeGas": "true"},
                 headers=H, timeout=15)
print("HTTP", r.status_code)
try:
    j = r.json()
    protos = j.pop("protocols", None)
    print(json.dumps(j, indent=1)[:1500])
    if protos:
        print("route (first hop):", json.dumps(protos)[:600])
except Exception:
    print(r.text[:800])

print("\n=== B) Fusion quoter ===")
for ver in ["v2.0", "v1.0"]:
    for dst_name, dst in [("ETH", ETH_NATIVE), ("WETH", WETH)]:
        url = f"https://api.1inch.com/fusion/quoter/{ver}/1/quote/receive"
        p = {"fromTokenAddress": USDT, "toTokenAddress": dst, "amount": AMOUNT,
             "walletAddress": WALLET, "enableEstimate": "false"}
        try:
            r = requests.get(url, params=p, headers=H, timeout=15)
            print(f"\n{ver} {dst_name}: HTTP {r.status_code}")
            if r.status_code == 200:
                j = r.json()
                top = {k: j[k] for k in ("toTokenAmount", "recommended_preset", "feeToken", "prices", "volume") if k in j}
                print(json.dumps(top, indent=1)[:800])
                presets = j.get("presets", {})
                for name, pr in presets.items():
                    s = int(pr.get("auctionStartAmount", 0)) / 1e18
                    e = int(pr.get("auctionEndAmount", 0)) / 1e18
                    print(f"  preset {name:7} start={s:.6f} ETH  end={e:.6f} ETH  duration={pr.get('auctionDuration')}s")
            else:
                print(r.text[:300])
        except Exception as ex:
            print(f"{ver} {dst_name}: error {str(ex)[:120]}")
