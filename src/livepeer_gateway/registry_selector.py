"""Select registry route candidates by capability, offering, mode, price, constraints."""

from __future__ import annotations

import json
from functools import cmp_to_key
from typing import Any, Mapping

from .errors import LivepeerGatewayError
from .registry_types import RegistryRouteCandidate


def _is_json_object(v: Any) -> bool:
    return v is not None and isinstance(v, dict) and not isinstance(v, type)


def _deep_equal(a: Any, b: Any) -> bool:
    if a == b:
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        ka = sorted(a.keys())
        kb = sorted(b.keys())
        if ka != kb:
            return False
        return all(_deep_equal(a[k], b[k]) for k in ka)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_deep_equal(x, y) for x, y in zip(a, b, strict=True))
    return False


def _is_subset(candidate: Any, required: Any) -> bool:
    if required is None or not isinstance(required, (dict, list)):
        return _deep_equal(candidate, required)
    if isinstance(required, list):
        if not isinstance(candidate, list):
            return False
        return all(any(_deep_equal(cv, rv) for cv in candidate) for rv in required)
    if not isinstance(candidate, dict):
        return False
    return all(_is_subset(candidate.get(k), v) for k, v in required.items())


def _count_matching_leaves(candidate: Any, preferred: Any) -> int:
    if preferred is None or not isinstance(preferred, (dict, list)):
        return 1 if _deep_equal(candidate, preferred) else 0
    if isinstance(preferred, list):
        if not isinstance(candidate, list):
            return 0
        return sum(1 for pv in preferred if any(_deep_equal(cv, pv) for cv in candidate))
    if not isinstance(candidate, dict):
        return 0
    total = 0
    for k, v in preferred.items():
        total += _count_matching_leaves(candidate.get(k), v)
    return total


def _score_preference(extra: Mapping[str, Any], preferred: Mapping[str, Any] | None) -> tuple[bool, int]:
    if preferred is None or len(preferred) == 0:
        return True, 0
    full = _is_subset(extra, preferred)
    leaves = _count_matching_leaves(extra, preferred)
    return full, leaves


def _safe_big_int(s: str) -> int:
    try:
        return int(s, 10)
    except ValueError:
        return 0


def _compare_candidates(
    a: RegistryRouteCandidate,
    b: RegistryRouteCandidate,
    preferred_extra: Mapping[str, Any] | None,
) -> int:
    pa = _score_preference(a.extra, preferred_extra)
    pb = _score_preference(b.extra, preferred_extra)
    if pa[0] != pb[0]:
        return -1 if pa[0] else 1
    if pa[1] != pb[1]:
        return pb[1] - pa[1]
    wa = _safe_big_int(a.price_per_unit_wei)
    wb = _safe_big_int(b.price_per_unit_wei)
    if wa < wb:
        return -1
    if wa > wb:
        return 1
    url_cmp = (a.worker_url > b.worker_url) - (a.worker_url < b.worker_url)
    if url_cmp != 0:
        return url_cmp
    cap_cmp = (a.capability_id > b.capability_id) - (a.capability_id < b.capability_id)
    if cap_cmp != 0:
        return cap_cmp
    return (a.offering_id > b.offering_id) - (a.offering_id < b.offering_id)


def select_registry_candidates(
    candidates: tuple[RegistryRouteCandidate, ...],
    *,
    capability_id: str | None = None,
    offering_id: str | None = None,
    interaction_mode: str | None = None,
    required_constraints: Mapping[str, Any] | None = None,
    preferred_extra: Mapping[str, Any] | None = None,
    max_price_per_unit_wei: int | None = None,
) -> list[RegistryRouteCandidate]:
    cap_f = (capability_id or "").strip()
    off_f = (offering_id or "").strip()
    mode_f = (interaction_mode or "").strip()

    matches: list[RegistryRouteCandidate] = []
    for c in candidates:
        if cap_f and c.capability_id != cap_f:
            continue
        if off_f and c.offering_id != off_f:
            continue
        if mode_f and c.interaction_mode != mode_f:
            continue
        if max_price_per_unit_wei is not None:
            if _safe_big_int(c.price_per_unit_wei) > max_price_per_unit_wei:
                continue
        if required_constraints is not None and len(required_constraints) > 0:
            if not _is_subset(dict(c.constraints), dict(required_constraints)):
                continue
        matches.append(c)

    pref = dict(preferred_extra) if preferred_extra else None
    matches.sort(key=cmp_to_key(lambda x, y: _compare_candidates(x, y, pref)))
    return matches


def parse_constraints_json(raw: str | None) -> dict[str, Any] | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        v = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LivepeerGatewayError(f"invalid constraints JSON: {e}") from e
    if not isinstance(v, dict):
        raise LivepeerGatewayError("constraints JSON must be an object")
    return v


def parse_extra_json(raw: str | None) -> dict[str, Any] | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        v = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LivepeerGatewayError(f"invalid preferred extra JSON: {e}") from e
    if not isinstance(v, dict):
        raise LivepeerGatewayError("preferred extra JSON must be an object")
    return v
