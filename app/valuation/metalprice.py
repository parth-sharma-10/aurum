"""The live price feed: MetalpriceAPI, and the four ways it can lie by omission.

    GET https://api.metalpriceapi.com/v1/latest?base=USD&currencies=XAU,XAG,XPD,INR

    {"success": true, "base": "USD", "timestamp": 1755792000,
     "rates": {"XAU": 0.00053853, "USDXAU": 1856.906765, "INR": 95.70, ...}}

`rates["XAU"]` is troy ounces of gold per one US dollar. `rates["USDXAU"]` is
the reciprocal — dollars per troy ounce — and is the figure this module uses,
because reading the small number as a price is the mistake that produces a
gold quote of 0.0005 and a valuation of nothing.

Four traps, and what closes each:

**The unit.** MetalpriceAPI quotes XAU, XAG, XPD and XPT per **troy ounce**.
Other symbols do not share that convention — LME copper is per metric tonne —
so this provider answers for the four troy-ounce metals only and returns
UNAVAILABLE for anything else rather than dividing a tonne price by 31.1. The
conversion itself is not repeated here: the quote is handed to the same
`_quote_from_entry` the reference snapshot uses, so there is exactly one place
in this repository that knows a troy ounce is 31.1034768 g.

**The currency.** The free tier answers in USD. Aurum reports INR. The same
request carries `INR` in `currencies`, so the FX rate arrives from the same
call, at the same instant, with the same provenance as the metal price —
rather than being pulled from a second source an hour later.

**The call count.** One request answers for every metal and every item until it
expires. Without that, an assembly of four components would issue twelve
identical requests and exhaust a free monthly quota in an afternoon.

**The failure.** An outage must never become `price = 0`. Every failure path
returns a `MetalPrice` with no number and a reason, which is what makes
`FallbackProvider` degrade to the dated reference snapshot instead of taking
the pipeline down. A cached snapshot that is too old is still served — and
`PriceService` marks it STALE, because that is precisely what STALE means.

**The API key never enters `Config`.** It is read from the environment at the
point of use and is redacted out of every message this module can produce. A
key that lives in the settings object is one `/config` endpoint away from
being on someone's screen.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from app import config as config_module
from app.errors import redact
from app.valuation.prices import MetalPrice, PriceStatus, _quote_from_entry

#: Aurum's metal symbols to MetalpriceAPI's. Only metals this API quotes per
#: TROY OUNCE appear: mixing in a symbol quoted per tonne would put a factor
#: of 32 150 behind the same unit label.
SYMBOLS = {"Au": "XAU", "Ag": "XAG", "Pd": "XPD", "Pt": "XPT"}

ENV_API_KEY = "AURUM_METALPRICE_API_KEY"

DEFAULT_BASE_URL = "https://api.metalpriceapi.com/v1"

#: Documented error codes worth naming back to the operator. Anything else is
#: reported with whatever the API said, verbatim.
API_ERRORS = {
    101: "the API key is missing",
    102: "the API key is invalid",
    103: "the requested endpoint does not exist",
    104: "the monthly request allowance is exhausted",
    105: "the current plan does not permit this request",
    201: "the base currency is invalid",
    202: "one of the requested currencies is invalid",
    300: "the request returned no rates",
}


class FetchError(RuntimeError):
    """The call did not produce a usable payload. Never raised past `quote()`."""


@dataclass(frozen=True)
class Snapshot:
    """One answered request, and when it was answered."""

    base: str
    rates: dict
    #: The market timestamp the API reported, ISO 8601. What staleness is
    #: measured against — not the moment we happened to call.
    timestamp: str
    #: Monotonic clock reading when this was fetched. What the cache expires on.
    fetched_at: float

    def rate(self, key: str) -> float | None:
        value = self.rates.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if value > 0 else None


#: Cached snapshots, keyed by (base_url, base, currencies). Module level on
#: purpose: `PriceService.from_config()` builds a fresh provider for every item
#: valued, so an instance-level cache would expire on every single item and the
#: quota would be gone by lunchtime. `reset_cache()` exists for tests.
_CACHE: dict[tuple, Snapshot] = {}


def reset_cache() -> None:
    _CACHE.clear()


def cache_size() -> int:
    return len(_CACHE)


def _http_get(url: str, timeout_s: float) -> dict:
    """One GET, returning parsed JSON. Raises `FetchError` for anything else."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} from the price API") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"could not reach the price API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FetchError(f"the price API did not answer within {timeout_s:g}s") from exc
    except OSError as exc:
        raise FetchError(f"the price API call failed: {exc}") from exc
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise FetchError("the price API returned a body that is not JSON") from exc
    if not isinstance(payload, dict):
        raise FetchError("the price API returned JSON that is not an object")
    return payload


class MetalpriceProvider:
    """Live spot metal prices, in the reporting currency, cached and honest.

    Every failure is a `MetalPrice` with no number, so composing this with
    `FallbackProvider` degrades a feed outage to the dated reference snapshot
    rather than to a zero.
    """

    name = "metalprice"
    status = PriceStatus.LIVE

    def __init__(
        self,
        api_key: str | None = None,
        currency: str = "INR",
        base: str = "USD",
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 5.0,
        cache_seconds: float = 900.0,
        fetch=None,
        clock=time.monotonic,
    ) -> None:
        self.api_key = api_key
        self.currency = currency
        self.base = base
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.cache_seconds = cache_seconds
        #: Injectable so the whole matrix of API failures is testable without a
        #: network, a key or a live market.
        self._fetch = fetch or _http_get
        self._clock = clock

    @classmethod
    def from_config(cls, cfg: config_module.Config | None = None) -> MetalpriceProvider:
        cfg = config_module.load() if cfg is None else cfg
        return cls(
            # Straight from the environment, never through Config: see the
            # module docstring.
            api_key=config_module.secret(ENV_API_KEY),
            currency=cfg["pricing.currency"],
            base=cfg["pricing.metalprice.base"],
            base_url=cfg["pricing.metalprice.base_url"],
            timeout_s=cfg["pricing.metalprice.timeout_s"],
            cache_seconds=cfg["pricing.metalprice.cache_seconds"],
        )

    # -- the request -------------------------------------------------------
    @property
    def _currencies(self) -> list[str]:
        """Every symbol one request must carry: the metals, plus the FX leg."""
        wanted = list(SYMBOLS.values())
        if self.currency != self.base:
            wanted.append(self.currency)
        return wanted

    @property
    def _cache_key(self) -> tuple:
        return (self.base_url, self.base, tuple(self._currencies))

    @property
    def endpoint(self) -> str:
        """The request URL **without** the key, for logs and error messages."""
        query = urllib.parse.urlencode(
            {"base": self.base, "currencies": ",".join(self._currencies)}
        )
        return f"{self.base_url}/latest?{query}"

    def _url(self) -> str:
        query = urllib.parse.urlencode(
            {
                "api_key": self.api_key or "",
                "base": self.base,
                "currencies": ",".join(self._currencies),
            }
        )
        return f"{self.base_url}/latest?{query}"

    def _parse(self, payload: dict) -> Snapshot:
        if payload.get("success") is False:
            error = payload.get("error") or {}
            code = error.get("code")
            known = API_ERRORS.get(code if isinstance(code, int) else -1)
            detail = error.get("info") or known or "no reason given"
            raise FetchError(f"the price API refused the request (code {code}): {detail}")
        rates = payload.get("rates")
        if not isinstance(rates, dict) or not rates:
            raise FetchError("the price API answered with no rates")
        stamp = payload.get("timestamp")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            when = datetime.fromtimestamp(float(stamp), tz=UTC).isoformat(timespec="seconds")
        else:
            # No market timestamp means nothing can judge this quote's age, and
            # a quote whose age cannot be judged must not be published as live.
            raise FetchError("the price API answered without a timestamp")
        return Snapshot(
            base=str(payload.get("base") or self.base),
            rates=rates,
            timestamp=when,
            fetched_at=self._clock(),
        )

    def snapshot(self) -> tuple[Snapshot | None, str | None]:
        """The current rates, from cache when fresh. `(snapshot, problem)`.

        A failed refresh with a cached snapshot in hand returns the cached one:
        an old real price that `PriceService` will mark STALE is more use than
        nothing, and is still never presented as current.
        """
        if not self.api_key:
            return None, (
                f"No MetalpriceAPI key is configured. Export {ENV_API_KEY} to enable "
                "live pricing. Nothing is invented in its place."
            )
        cached = _CACHE.get(self._cache_key)
        if cached is not None and self._clock() - cached.fetched_at < self.cache_seconds:
            return cached, None
        try:
            fresh = self._parse(self._fetch(self._url(), self.timeout_s))
        except FetchError as exc:
            problem = redact(str(exc))
            if cached is not None:
                return cached, (
                    f"Serving the last successful quote: {problem}. Its age is "
                    "reported and it is never presented as current."
                )
            return None, problem
        except Exception as exc:  # an external feed may do anything at all
            problem = redact(f"the price API call failed unexpectedly: {exc}")
            return (cached, problem) if cached is not None else (None, problem)
        _CACHE[self._cache_key] = fresh
        return fresh, None

    # -- the provider contract --------------------------------------------
    def quote(self, metal: str, material: str) -> MetalPrice:
        symbol = SYMBOLS.get(metal)
        if symbol is None:
            return MetalPrice(
                metal=metal,
                material=material,
                status=PriceStatus.UNAVAILABLE,
                provider=self.name,
                reason=(
                    f"MetalpriceAPI is used for {', '.join(sorted(SYMBOLS))} only, which "
                    f"it quotes per troy ounce. {metal} is quoted on a different unit "
                    "basis and is not converted here."
                ),
            )

        snapshot, problem = self.snapshot()
        if snapshot is None:
            return MetalPrice(
                metal=metal,
                material=material,
                status=(PriceStatus.UNAVAILABLE if not self.api_key else PriceStatus.ERROR),
                provider=self.name,
                reason=problem,
            )

        # The reciprocal key is the price of one troy ounce in the base
        # currency. Falling back to 1/rate covers a response that carries only
        # the forward pair; a rate of zero or a non-number gives neither.
        per_ozt = snapshot.rate(f"{snapshot.base}{symbol}")
        if per_ozt is None:
            forward = snapshot.rate(symbol)
            per_ozt = None if forward is None else 1.0 / forward
        if per_ozt is None:
            return MetalPrice(
                metal=metal,
                material=material,
                status=PriceStatus.UNAVAILABLE,
                provider=self.name,
                reason=(
                    f"The live feed quoted no usable rate for {symbol}. Neither "
                    f"{snapshot.base}{symbol} nor {symbol} held a positive number."
                ),
            )

        fx = {}
        if self.currency != snapshot.base:
            rate = snapshot.rate(self.currency)
            if rate is None:
                return MetalPrice(
                    metal=metal,
                    material=material,
                    status=PriceStatus.ERROR,
                    provider=self.name,
                    reason=(
                        f"The live feed carried no {snapshot.base}/{self.currency} rate, so "
                        f"a {snapshot.base} price cannot be reported in {self.currency}. "
                        "No parity is assumed."
                    ),
                )
            fx = {
                snapshot.base: {
                    "rate": rate,
                    "quote": f"{snapshot.base}/{self.currency}",
                    "timestamp": snapshot.timestamp,
                    "source": "MetalpriceAPI /v1/latest, same request as the metal price",
                }
            }

        entry = {
            "price_per_unit": per_ozt,
            "unit": "ozt",
            "currency": snapshot.base,
            "timestamp": snapshot.timestamp,
            "source": f"MetalpriceAPI {self.endpoint} ({symbol})",
        }
        return _quote_from_entry(
            metal,
            material,
            entry,
            status=PriceStatus.LIVE,
            provider=self.name,
            reason=(
                f"LIVE quote from MetalpriceAPI, market timestamp {snapshot.timestamp}. "
                + (problem or "")
            ).strip(),
            fx=fx,
            currency=self.currency,
        )
