"""Agent-reported model usage: a fact the agent states, never one Athena measures.

An agent's inference happens inside its own process, so Athena cannot meter it. This module owns
the one grammar for what an agent may *report* on a check-in or a completion, using the GenAI
span attribute names with their ``gen_ai.usage.`` prefix dropped (``model``, ``input_tokens``,
``output_tokens``, ``cached_tokens``) plus ``estimated_cost``, ``currency`` and ``reported_at``.

Each report is the usage since that agent's previous report, so totals are plain sums. Reports
live inside activity detail JSON; there is no column, no ceiling and no enforcement.
"""

from __future__ import annotations

from datetime import datetime
import json
import math

from athena.core import report_credentials

VERB_USAGE_REPORTED = "usage_reported"
USAGE_MARKER = "reported_usage_v1 "

USAGE_KEYS = (
    "model",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "estimated_cost",
    "currency",
    "reported_at",
)
MAX_TOKENS = 10**12
MAX_COST = 10**9
MAX_MODEL_CHARS = 100
MAX_REPORTED_AT_CHARS = 40

# Active ISO 4217 alphabetic codes. A closed list, so "ZZZ" and "usd" are refused alike.
CURRENCIES = frozenset(
    """AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN
    BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP
    GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS
    KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR
    MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF
    SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD
    TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWG""".split()
)


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"usage.{name} must be a whole number")
    if value < 0 or value > MAX_TOKENS:
        raise ValueError(f"usage.{name} must be between 0 and {MAX_TOKENS}")
    return value


def normalize_usage(raw: object) -> dict:
    """Return the usage object as stated, or raise ``ValueError`` naming the first problem.

    Shape only: Athena does not check the figures against anything. Credential refusal is a
    separate write-path step (:func:`refuse_credentials`) so stored history stays readable.
    """
    if not isinstance(raw, dict) or not raw:
        raise ValueError("usage must be a non-empty object")
    unknown = sorted(str(key) for key in raw if key not in USAGE_KEYS)
    if unknown:
        raise ValueError(
            f"usage has unknown key {unknown[0]!r}; allowed: {', '.join(USAGE_KEYS)}"
        )
    usage: dict = {}
    if "model" in raw:
        model = raw["model"]
        if not isinstance(model, str) or not 1 <= len(model) <= MAX_MODEL_CHARS:
            raise ValueError(f"usage.model must be 1 to {MAX_MODEL_CHARS} characters")
        if not model.isprintable():
            raise ValueError("usage.model must contain only printable characters")
        usage["model"] = model
    for name in ("input_tokens", "output_tokens", "cached_tokens"):
        if name in raw:
            usage[name] = _count(raw[name], name)
    if ("estimated_cost" in raw) != ("currency" in raw):
        raise ValueError("usage.estimated_cost and usage.currency go together")
    if "estimated_cost" in raw:
        cost = raw["estimated_cost"]
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
            or cost > MAX_COST
        ):
            raise ValueError(
                f"usage.estimated_cost must be a number from 0 to {MAX_COST}"
            )
        currency = raw["currency"]
        if not isinstance(currency, str) or currency not in CURRENCIES:
            raise ValueError("usage.currency must be an ISO 4217 code such as USD")
        usage["estimated_cost"] = cost
        usage["currency"] = currency
    if "reported_at" in raw:
        stamp = raw["reported_at"]
        if not isinstance(stamp, str) or len(stamp) > MAX_REPORTED_AT_CHARS:
            raise ValueError("usage.reported_at must be an ISO 8601 timestamp")
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            raise ValueError(
                "usage.reported_at must be an ISO 8601 timestamp"
            ) from None
        if parsed.tzinfo is None:
            raise ValueError("usage.reported_at must include a UTC offset or Z")
        usage["reported_at"] = stamp
    return usage


def refuse_credentials(usage: dict) -> None:
    """Raise ``ValueError`` naming only a fixed kind when the model text looks like a secret."""
    kind = report_credentials.credential_kind(usage.get("model", ""))
    if kind is not None:
        raise ValueError(
            f"usage.model contains {kind}; remove the credential and retry"
        )


def event_detail(run_id: str, usage: dict) -> str:
    return USAGE_MARKER + json.dumps(
        {"run_id": run_id, "usage": usage},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def from_event(event: dict) -> dict | None:
    """Read one native ``usage_reported`` event, or ``None`` when it is not a valid one."""
    if (
        event.get("verb") != VERB_USAGE_REPORTED
        or event.get("imported_at") is not None
        or not str(event.get("detail", "")).startswith(USAGE_MARKER)
    ):
        return None
    try:
        raw = json.loads(event["detail"][len(USAGE_MARKER) :])
        if not isinstance(raw, dict) or set(raw) != {"run_id", "usage"}:
            return None
        usage = normalize_usage(raw["usage"])
        if usage != raw["usage"] or not isinstance(raw["run_id"], str):
            return None
        return {"run_id": raw["run_id"], "usage": usage}
    except (ValueError, RecursionError):
        return None
