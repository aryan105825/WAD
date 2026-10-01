# @exports: none (check script; exit 0 held, 1 violated, 2 infrastructure error)
# @imports: factory/lib/hammer.py:request, barrage, duplicate, abandon, base_url, seed, check, finish
# @env: BASE_URL
# @env: SEED
# @schema: requires a fresh service (claimed == 0, capacity >= 3); checks run in this order: duplicate_key_one_claim, abandon_retry_one_claim, concurrent_claims_within_capacity
# @schema: output is one JSON line per check plus a final {"violated": bool, "detail": str} line
from __future__ import annotations

import time

from factory.lib import hammer

CONCURRENT_CLAIMS = 50
DUPLICATES = 10
JITTER_MS = 2.0
ABANDON_PAUSE_S = 0.05
SETTLE_S = 0.2


def _claim_id(resp: hammer.Resp) -> str | None:
    """The claim_id of a 200 response, or None when the body does not have the documented shape."""
    if resp.status != 200:
        return None
    try:
        data = resp.json
    except ValueError:
        return None
    value = data.get("claim_id") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _state(base: str) -> dict:
    resp = hammer.request("GET", base + "/state")
    valid = False
    data: object = None
    if resp.status == 200:
        try:
            data = resp.json
            valid = (
                isinstance(data, dict)
                and isinstance(data.get("capacity"), int)
                and isinstance(data.get("claimed"), int)
            )
        except ValueError:
            valid = False
    if not valid:
        hammer.check("state_endpoint", False, f"GET /state returned status {resp.status} error={resp.error} body={resp.body[:80]!r}")
        hammer.finish()
    assert isinstance(data, dict)
    return data


def main() -> None:
    base = hammer.base_url()
    seed = hammer.seed()

    start = _state(base)
    if start["claimed"] != 0:
        raise RuntimeError(
            f"this check needs a fresh service but /state reports claimed={start['claimed']}. "
            "Run it against a newly started container (the gate does this)."
        )
    if start["capacity"] < 3:
        raise RuntimeError(f"this check needs capacity >= 3 but /state reports {start['capacity']}. Start the service with CAPACITY=5.")
    capacity = start["capacity"]

    # 1. The same Idempotency-Key sent many times at once must create exactly one claim.
    before = start["claimed"]
    job = {"method": "POST", "url": base + "/claim", "headers": {"Idempotency-Key": f"dup-{seed}"}}
    responses = hammer.duplicate(job, DUPLICATES, jitter_ms=JITTER_MS, seed=seed)
    ids = {_claim_id(r) for r in responses}
    delta = _state(base)["claimed"] - before
    statuses = sorted({r.status for r in responses})
    hammer.check(
        "duplicate_key_one_claim",
        all(r.status == 200 for r in responses) and len(ids) == 1 and None not in ids and delta == 1,
        f"statuses={statuses} distinct_claim_ids={len(ids)} claimed_delta={delta} (expected all 200, 1 id, delta 1)",
    )

    # 2. A request abandoned mid-flight and then retried with the same key must create exactly one claim.
    before = _state(base)["claimed"]
    hammer.abandon("POST", base + "/claim", headers={"Idempotency-Key": f"abandon-{seed}"})
    time.sleep(ABANDON_PAUSE_S)
    retry = hammer.request("POST", base + "/claim", headers={"Idempotency-Key": f"abandon-{seed}"})
    time.sleep(SETTLE_S)
    delta = _state(base)["claimed"] - before
    hammer.check(
        "abandon_retry_one_claim",
        retry.status == 200 and _claim_id(retry) is not None and delta == 1,
        f"retry_status={retry.status} error={retry.error} claimed_delta={delta} (expected 200 and delta 1)",
    )

    # 3. Many simultaneous claims must never exceed the capacity.
    state = _state(base)
    remaining = state["capacity"] - state["claimed"]
    jobs = [{"method": "POST", "url": base + "/claim"} for _ in range(CONCURRENT_CLAIMS)]
    responses = hammer.barrage(jobs, jitter_ms=JITTER_MS, seed=seed)
    unexpected = sorted({r.status for r in responses if r.status not in (200, 409)})
    winners = [r for r in responses if r.status == 200]
    winner_ids = [_claim_id(r) for r in winners]
    final = _state(base)
    hammer.check(
        "concurrent_claims_within_capacity",
        not unexpected
        and len(winners) == remaining
        and None not in winner_ids
        and len(set(winner_ids)) == len(winner_ids)
        and final["claimed"] <= capacity,
        f"successes={len(winners)} expected={remaining} unexpected_statuses={unexpected} "
        f"distinct_ids={len(set(winner_ids))} final_claimed={final['claimed']} capacity={capacity}",
    )

    hammer.finish()


if __name__ == "__main__":
    main()
