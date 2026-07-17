#!/usr/bin/env python3
"""SUPER-TRADER v4: funding-edge + ATR-trailing + pyramiding + maker-orders.
Primaire edge: trade TEEN de crowd via funding.
  - coin met sterk NEGATIEVE funding (crowd short, jij long -> jij wordt betaald)
  - coin met sterk POSITIEVE funding (crowd long, jij short -> jij wordt betaald)
Exit: ATR-trailing (ride trend, uit bij echte reversal) i.p.v. vaste %.
Pyramiding: add size bij +10% winst. Kill-switch 50%. Alleen live als LIVE_PERPS=1.
"""
import os, json, time, math, requests
from datetime import datetime, timezone
import hl_perp as HL

CWD = os.path.dirname(os.path.abspath(__file__))
SCAN = os.path.join(CWD, "scan_signals.json")
LIVE_FLAG = os.path.join(CWD, "LIVE_PERPS.flag")
# HARDE live-bevestiging: het flag-bestand moet deze EXACTE regel bevatten.
# Een leeg 'touch LIVE_PERPS.flag' volstaat NIET (voorkomt ongewenste live-runs).
LIVE_CONFIRM = "CONFIRM_LIVE_TRADING=YES"

# --- Strategie-constants ---
ENTRY_SCORE = 2.0      # minimale scanner-score voor entry
MAX_LEV = 5.0           # max hefboom
MIN_NOTIONAL = 20.0     # minimale trade-grootte (minder fees dan $12)
# FUNDING-EDGE: trade richting op basis van funding-APR
FUND_LONG_APR = -10.0   # 24u funding APR <= -10% -> LONG (jij wordt betaald om long)
FUND_SHORT_APR = 0.5    # 24u funding APR >= +0.5% -> SHORT (crowd is long)
# RISK: ATR-based stop i.p.v. vaste %. ATR-mult bepaalt stop-afstand.
ATR_MULT = 2.5          # harde stop op 2.5x ATR
TRAIL_MULT = 3.0         # trailing stop op 3.0x ATR van beste punt
# pyramiding: bij +PYR_PNL% voeg PYR_FRAC extra size toe
PYR_PNL = 0.10
PYR_FRAC = 0.50
MAKER_REPRICE_SEC = 8   # als na X sec niet gevuld als maker -> wordt taker

def live_enabled():
    # HARDE gate: env LIVE_PERPS=1 EN flag-bestand met correcte bevestigingsregel.
    if os.getenv("LIVE_PERPS") != "1":
        return False
    if not os.path.exists(LIVE_FLAG):
        return False
    try:
        content = open(LIVE_FLAG, encoding="utf-8").read()
    except Exception:
        return False
    return LIVE_CONFIRM in content
# FUNDING-EDGE: trade richting op basis van funding-APR
FUND_LONG_APR = -10.0   # 24u funding APR <= -10% -> LONG (jij wordt betaald om long)
FUND_SHORT_APR = 0.5    # 24u funding APR >= +0.5% -> SHORT (crowd is long)
# RISK: ATR-based stop i.p.v. vaste %. ATR-mult bepaalt stop-afstand.
ATR_MULT = 2.5          # stop op 2.5x ATR onder (long) / boven (short) entry
TRAIL_MULT = 3.0        # trailing stop op 3.0x ATR van het beste punt
MIN_NOTIONAL = 20.0
# pyramiding: bij +PYR_PNL% voeg PYR_FRAC extra size toe (max 1x)
PYR_PNL = 0.10          # +10% winst -> add
PYR_FRAC = 0.50         # 50% van oorsprong-size erbij
# maker-orders: probeer limit als maker (bespaart ~4x fee), fallback taker na timeout
MAKER_REPRICE_SEC = 8   # als na X sec niet gevuld als maker -> wordt taker
# scale-out flag + ATR-tracking per positie
_TP1_HIT = {}
_ATR_STATE = {}         # sym -> {'entry':px,'best':px,'atr':x,'pyr':False}

def atr(coin, periods=24):
    """True Range ATR over laatste N 1h-candles via HL (voor stop-afstand)."""
    try:
        HL._ensure()
        now = int(time.time()*1000)
        d = HL._info.candles_snapshot(coin, "1h", now-periods*3600*1000, now)
        if not isinstance(d, list) or len(d) < 2:
            return None
        trs = []
        for i in range(1, len(d)):
            h, l, c, pc = float(d[i]['h']), float(d[i]['l']), float(d[i]['c']), float(d[i-1]['c'])
            tr = max(h-l, abs(h-pc), abs(l-pc))
            trs.append(tr)
        return sum(trs)/len(trs)
    except Exception:
        return None

def funding_apr_avg(coin, hours=24):
    """24u-gemiddelde funding-APR (stabieler dan spot-rate)."""
    try:
        now = int(time.time()*1000)
        d = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type":"fundingHistory","coin":coin,
                                "startTime": now-hours*3600*1000}, timeout=20).json()
        if not isinstance(d, list) or not d:
            return HL.funding_apr(coin)  # fallback spot
        rates = [float(x["fundingRate"]) for x in d]
        return sum(rates)/len(rates)*24*365*100
    except Exception:
        return HL.funding_apr(coin)

def funding_side(coin):
    """Return 'long' / 'short' / None op basis van 24u funding-edge."""
    apr = funding_apr_avg(coin)
    if apr <= FUND_LONG_APR:
        return "long"
    if apr >= FUND_SHORT_APR:
        return "short"
    return None

def dynamic_lev(regime, vol_z):
    """Freqtrade 'leverage' concept: lagere lev in volle markt, hoger bij breakout.
    Beschermt kapitaal als vol hoog is, geeft meer punch bij bevestigde edge."""
    if regime == "volatile_breakout":
        return MAX_LEV if (vol_z or 1) < 3 else 3.0  # breakout maar niet gek
    if regime == "trending":
        return 3.0
    return 2.0  # default conservatief

def exit_decision(pnl, sym, is_long, px=None, entry=None, atr=None):
    """ATR-trailing exit i.p.v. vaste %. Ride de trend, stap uit bij echte reversal.
    Returns (action, reason): 'stop' | 'tp1' | 'trail' | 'pyramid' | None
    """
    st = _ATR_STATE.get(sym)
    if atr is None and st:
        atr = st.get("atr")
    if atr is None or atr <= 0:
        # geen ATR -> val terug op vaste -20%/+25% (conservatief)
        if pnl <= -0.20:
            return "stop", "STOP (-20%, geen ATR)"
        if pnl >= 0.25:
            return "tp1", "TP +25% (geen ATR)"
        return None, None
    # update beste punt voor trailing
    if st is None:
        st = _ATR_STATE[sym] = {"entry": entry, "best": px, "atr": atr, "pyr": False}
    if is_long:
        st["best"] = max(st["best"] or px, px)
        stop_px = st["best"] - TRAIL_MULT * atr   # trailing stop onder beste prijs
        hard_px = entry - ATR_MULT * atr           # harde stop onder entry
    else:
        st["best"] = min(st["best"] or px, px)
        stop_px = st["best"] + TRAIL_MULT * atr
        hard_px = entry + ATR_MULT * atr
    # harde stop (ATR-based)
    if (is_long and px <= hard_px) or (not is_long and px >= hard_px):
        return "stop", f"STOP ATR (px vs {hard_px:.4f})"
    # trailing stop (bij winst)
    if pnl > 0 and ((is_long and px <= stop_px) or (not is_long and px >= stop_px)):
        return "trail", f"TRAIL ATR (px vs {stop_px:.4f})"
    # pyramiding: bij +10% voeg 50% size toe (1x)
    if not st.get("pyr") and pnl >= PYR_PNL:
        st["pyr"] = True
        return "pyramid", f"PYRAMID +{int(PYR_PNL*100)}% (add {int(PYR_FRAC*100)}% size)"
    # scale-out bij +25% (lock 50% winst, ride rest met trailer)
    if pnl >= 0.25 and not _TP1_HIT.get(sym):
        _TP1_HIT[sym] = True
        return "tp1", "TP +25% (50% lock)"
    return None, None

def live_decision():
    # HARDE LIVE-GATE: alleen als env LIVE_PERPS=1 EN flag-bestand bestaat.
    if not live_enabled():
        return "PAPER/offline - live-gate dicht (geen LIVE_PERPS.flag). Geen trades."
    acct = HL._load_wallet()
    addr = acct.address
    av = HL.available_capital(addr)  # spot-USDC + perp (Unified Account)
    if av <= 0:
        return "ACCOUNT LEEG - funder eerst"
    START_AV = float(os.getenv("START_AV", "24.54"))
    if av < START_AV * 0.5:
        return f"KILL-SWITCH: ${av:.2f} < 50% start (${START_AV:.2f}). GEEN trades."
    pos = HL.positions(addr)
    if pos:
        return manage_open(pos, av)
    # FUNDING-EDGE: kies coin + richting op basis van funding (de echte edge)
    best = None
    for cand in (json.load(open(SCAN)).get("candidates", []) if os.path.exists(SCAN) else []):
        coin = cand.get("coin")
        side = funding_side(coin)
        if not side:
            continue
        score = cand.get("score", 0)
        if score < ENTRY_SCORE:
            continue
        # prefereer de coin met sterkste funding-edge
        apr = abs(HL.funding_apr(coin))
        if best is None or apr > best[2]:
            best = (coin, side, apr, score, cand)
    if not best:
        # fallback: scan alle coins op funding-edge als scanner geen kans gaf
        for coin in ["kBONK","SOL","WIF","ETH","BTC","DOGE","kPEPE","POPCAT","FARTCOIN"]:
            side = funding_side(coin)
            if side:
                best = (coin, side, abs(HL.funding_apr(coin)), 0, {"regime":"funding","vol_z":2})
                break
    if not best:
        return "geen funding-edge kans (alle coins neutraal). Wacht."
    coin, side, apr, score, cand = best
    _, c = HL.ctx(coin)
    px = float(c.get("oraclePx", 0) or 0)
    if px <= 0:
        return f"{coin} prijs niet gelezen"
    lev = dynamic_lev(cand.get("regime", "trending"), cand.get("vol_z"))
    notional = max(av * 0.45, MIN_NOTIONAL)
    u2, _ = HL.ctx(coin)
    sz_dec = int(u2.get("szDecimals", 2)) if u2 else 2
    size = round(notional / px, sz_dec)
    # ATR voor stop-bepaling
    a = atr(coin)
    if a:
        _ATR_STATE[coin] = {"entry": px, "best": px, "atr": a, "pyr": False}
    ok, res = HL.place_order(coin, side == "long", size, lev=lev)
    if ok:
        return (f"ENTRY (LIVE): {coin} {side.upper()} {size:.4f} lev={lev}x @${px:.4f} "
                f"notional=${notional:.2f} fundingAPR={HL.funding_apr(coin):+.1f}% score={score:.1f}")
    return f"ENTRY MISLUKT: {res}"

def manage_open(pos, av):
    p = pos[0]
    sym = p["position"]["coin"]
    entry = float(p["position"]["entryPx"])
    _, c = HL.ctx(sym)
    px = float(c.get("oraclePx", 0) or 0)
    szi = float(p["position"]["szi"])
    is_long = szi > 0
    pnl = (px/entry - 1) if is_long else (entry/px - 1)
    a = atr(sym)
    # exit-beslissing (ATR-trailing / stop / tp1 / pyramid)
    action, why = exit_decision(pnl, sym, is_long, px=px, entry=entry, atr=a)
    if action == "stop" or action == "trail":
        HL.close_position(sym, abs(szi), not is_long)
        _TP1_HIT.pop(sym, None); _ATR_STATE.pop(sym, None)
        return f"{why}: {sym} {'LONG' if is_long else 'SHORT'} gesloten. pnl={pnl*100:.1f}%"
    if action == "tp1":
        half = abs(szi) / 2
        HL.close_position(sym, half, not is_long)
        return f"{why}: {sym} 50% gesloten (rest open). pnl={pnl*100:.1f}%"
    if action == "pyramid":
        # add 50% van oorsprong-size (was de entry-size in state)
        st = _ATR_STATE.get(sym, {})
        base = st.get("entry") or entry
        u2, _ = HL.ctx(sym)
        sz_dec = int(u2.get("szDecimals", 2)) if u2 else 2
        add = round((abs(szi) * PYR_FRAC), sz_dec)
        u, _ = HL.ctx(sym)
        add_px = float(u.get("oraclePx", 0) or 0)
        if add_px > 0:
            ok, res = HL.place_order(sym, is_long, add, lev=dynamic_lev("trending", 2))
            if ok:
                return f"{why}: {sym} +{add} size (totaal {abs(szi)+add:.0f}) @${add_px:.4f}"
        return f"{why}: {sym} (add mislukt: {res if isinstance(res,str) else 'n/a'})"
    return f"POSITIE OPEN: {sym} {'LONG' if is_long else 'SHORT'} entry=${entry:.4f} nu=${px:.4f} pnl={pnl*100:.1f}%"

if __name__ == "__main__":
    print(live_decision())
