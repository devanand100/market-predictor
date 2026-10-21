"""
Dhan v2 REST client (read-only market data; no orders). Adapted from bear_call_bot/lambda_function.py.

Lessons kept from the old bot:
  * RenewToken must be a GET (POST returns DH-905) and takes the `dhanClientId` header.
  * The renewed token's position in the response varies -> _find_jwt searches for it.
  * Rate limits arrive as HTTP 429 or as body status=failure with remark 805.
  * fundlimit spells the field `availabelBalance`.
Retries are deliberately minimal: at most ONE retry, only for rate limits / 5xx / timeouts.
"""
import datetime as dt
import json
import time
import urllib.error
import urllib.request

BASE = "https://api.dhan.co"
NIFTY_ID, VIX_ID = 13, 21
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class DhanAuthError(RuntimeError):
    pass


class Dhan:
    def __init__(self, client_id, token, min_gap_s=0.0):
        self.client_id = client_id
        self.token = token
        self.min_gap_s = min_gap_s       # spacing between calls (bulk downloads in reconcile)
        self._last = 0.0
        self.calls = 0

    # ------------------------------------------------------------ transport
    def call(self, endpoint, payload=None, method=None, timeout=12):
        if not self.token or not self.client_id:
            raise DhanAuthError("DHAN_CLIENT_ID / access token missing")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        method = method or ("POST" if payload is not None else "GET")
        for attempt in range(2):
            wait = self.min_gap_s - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            req = urllib.request.Request(BASE + endpoint, data=data, method=method,
                                         headers={"access-token": self.token, "client-id": self.client_id,
                                                  "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = json.loads(r.read().decode("utf-8"))
                if isinstance(body, dict) and body.get("status") == "failure" and "805" in json.dumps(body.get("remarks", "")):
                    if attempt == 0:
                        time.sleep(3)
                        continue
                    raise RuntimeError(f"Dhan rate-limited (805) on {endpoint}")
                return body
            except urllib.error.HTTPError as e:
                txt = e.read().decode("utf-8", "replace")
                if e.code == 401:
                    raise DhanAuthError(f"Dhan 401 on {endpoint}: {txt[:200]}")
                if e.code in (429, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(3)
                    continue
                raise RuntimeError(f"Dhan {e.code} on {endpoint}: {txt[:300]}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"Dhan network error on {endpoint}: {e}")
        raise RuntimeError(f"Dhan call failed on {endpoint}")

    # ------------------------------------------------------------ token
    def token_info(self, tok=None):
        """(valid, 'DD/MM/YYYY HH:MM' or error text) via GET /v2/profile."""
        tok = tok or self.token
        if not tok:
            return False, "no token"
        req = urllib.request.Request(BASE + "/v2/profile", method="GET",
                                     headers={"access-token": tok, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return True, json.loads(r.read().decode("utf-8")).get("tokenValidity", "?")
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
        except Exception as e:
            return False, str(e)

    def renew(self):
        """-> new token string (raises on failure). Only a still-valid token can be renewed."""
        req = urllib.request.Request(BASE + "/v2/RenewToken", method="GET",
                                     headers={"access-token": self.token, "dhanClientId": self.client_id,
                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8")
        try:
            new = _find_jwt(json.loads(raw))
        except ValueError:
            new = _find_jwt(raw.strip().strip('"'))
        if not new:
            raise RuntimeError(f"no token in RenewToken response: {raw[:200]}")
        return new

    # ------------------------------------------------------------ market data
    def expiry_list(self):
        r = self.call("/v2/optionchain/expirylist", {"UnderlyingScrip": NIFTY_ID, "UnderlyingSeg": "IDX_I"})
        return sorted(r.get("data", []))

    def option_chain(self, expiry):
        """-> (underlying ltp, {strike: {"ce": leg, "pe": leg}}) with leg = sid, ltp, bid, ask, iv, oi."""
        r = self.call("/v2/optionchain", {"UnderlyingScrip": NIFTY_ID, "UnderlyingSeg": "IDX_I", "Expiry": expiry})
        data = r.get("data", {}) or {}
        chain = {}
        for k, v in (data.get("oc") or {}).items():
            chain[int(round(float(k)))] = {t: _leg(v.get(t) or {}) for t in ("ce", "pe")}
        return float(data.get("last_price") or 0), chain

    def ltp(self, fno_ids=(), with_index=True):
        """-> {sid: ltp, 'spot': nifty, 'vix': vix}"""
        payload = {}
        ids = sorted({int(s) for s in fno_ids if s})
        if ids:
            payload["NSE_FNO"] = ids
        if with_index:
            payload["IDX_I"] = [NIFTY_ID, VIX_ID]
        r = self.call("/v2/marketfeed/ltp", payload)
        dd = r.get("data", {}) or {}
        out = {str(k): float(v.get("last_price") or 0) for k, v in (dd.get("NSE_FNO") or {}).items()}
        idx = dd.get("IDX_I") or {}
        out["spot"] = float((idx.get(str(NIFTY_ID)) or {}).get("last_price") or 0)
        out["vix"] = float((idx.get(str(VIX_ID)) or {}).get("last_price") or 0)
        return out

    def daily_closes(self, security_id, before, days=45):
        """[(date, close)] of the index's daily candles strictly before `before` (oldest first)."""
        r = self.call("/v2/charts/historical", {
            "securityId": str(security_id), "exchangeSegment": "IDX_I", "instrument": "INDEX", "expiryCode": 0,
            "oi": False, "fromDate": (before - dt.timedelta(days=days)).isoformat(), "toDate": before.isoformat()})
        out = []
        for ts, c in zip(r.get("timestamp", []), r.get("close", [])):
            day = dt.datetime.fromtimestamp(ts, IST).date()
            if day < before:
                out.append((day, float(c)))
        return out

    def intraday(self, security_id, seg, instrument, day, interval=1):
        """1-min (or `interval`) candles for one day -> {"HH:MM": close}."""
        nxt = (dt.date.fromisoformat(str(day)) + dt.timedelta(days=1)).isoformat()
        r = self.call("/v2/charts/intraday", {
            "securityId": str(security_id), "exchangeSegment": seg, "instrument": instrument, "interval": interval,
            "oi": False, "fromDate": str(day), "toDate": nxt})
        dd = r.get("data") if isinstance(r.get("data"), dict) else r
        out = {}
        for ts, c in zip(dd.get("timestamp", []), dd.get("close", [])):
            t = dt.datetime.fromtimestamp(ts, IST)
            if t.date().isoformat() == str(day):
                out[t.strftime("%H:%M")] = float(c)
        return out

    def rolling_option(self, flag, code, offset, opt, day, interval=1):
        """Dhan rolling-option candles (expired/rolling contracts) for one day.
        offset: 0 = ATM, +n / -n strikes. -> {"HH:MM": (strike, close, spot)}"""
        strike = "ATM" if offset == 0 else f"ATM{offset:+d}"
        r = self.call("/v2/charts/rollingoption", {
            "exchangeSegment": "NSE_FNO", "interval": interval, "securityId": str(NIFTY_ID), "instrument": "OPTIDX",
            "expiryFlag": flag, "expiryCode": code, "strike": strike, "drvOptionType": "CALL" if opt == "CE" else "PUT",
            "requiredData": ["close", "strike", "spot"], "fromDate": str(day),
            "toDate": (dt.date.fromisoformat(str(day)) + dt.timedelta(days=1)).isoformat()})
        dd = (r.get("data") or {}).get("ce" if opt == "CE" else "pe") or {}
        out = {}
        for ts, c, k, s in zip(dd.get("timestamp", []), dd.get("close", []), dd.get("strike", []), dd.get("spot", [])):
            t = dt.datetime.fromtimestamp(ts, IST)
            if t.date().isoformat() == str(day):
                out[t.strftime("%H:%M")] = (int(round(float(k))), float(c), float(s))
        return out

    def margin(self, legs):
        """legs: [{sid, side(+1 buy/-1 sell), qty, px}] -> {total, span, exposure, hedge_benefit} or None."""
        if not legs:
            return None
        try:
            r = self.call("/v2/margincalculator/multi", {
                "dhanClientId": self.client_id, "includePosition": False, "includeOrder": False,
                "scripList": [{"securityId": str(l["sid"]), "exchangeSegment": "NSE_FNO",
                               "transactionType": "BUY" if l["side"] > 0 else "SELL", "quantity": int(l["qty"]),
                               "productType": "MARGIN", "price": round(max(0.05, l["px"]), 2), "triggerPrice": 0}
                              for l in legs]})
            dd = r.get("data") if isinstance(r.get("data"), dict) else r
            total = float(dd.get("totalMargin") or dd.get("total_margin") or 0)
            if total > 0:
                return {"total": total, "span": float(dd.get("spanMargin") or 0),
                        "exposure": float(dd.get("exposureMargin") or dd.get("exposure") or 0),
                        "hedge_benefit": float(dd.get("hedgeBenefit") or 0)}
        except Exception as e:
            print(f"margin API failed: {e}")
        return None


def _leg(x):
    return {"sid": str(x.get("security_id") or x.get("securityId") or ""), "ltp": float(x.get("last_price") or 0),
            "bid": float(x.get("top_bid_price") or 0), "ask": float(x.get("top_ask_price") or 0),
            "iv": float(x.get("implied_volatility") or 0), "oi": int(x.get("oi") or 0)}


def _find_jwt(obj):
    if isinstance(obj, str):
        return obj if obj.count(".") == 2 and len(obj) > 100 else None
    if isinstance(obj, dict):
        for k in ("token", "accessToken", "access_token", "data"):
            if k in obj and (t := _find_jwt(obj[k])):
                return t
        for v in obj.values():
            if t := _find_jwt(v):
                return t
    return None
