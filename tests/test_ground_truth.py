"""Cost math sanity check against ccusage.

The redacted session was captured alongside a ccusage-reported total.
Our computation must match within ±1%.

Source: `~/.claude/projects/.../72781766-32ae-4eaa-a3b1-c4fdda225531.jsonl`
Captured via: `ccusage session --id 72781766... --json --offline` at redaction time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccforensics.jsonl import annotate_cost, dedup_entries, parse_file

FIXTURES = Path(__file__).parent / "fixtures"

# ccusage session --id 72781766-32ae-4eaa-a3b1-c4fdda225531 --offline --json
CCUSAGE_TOTAL_USD = 0.2353175


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def test_total_cost_matches_ccusage_within_1pct(pricing_data: dict) -> None:
    result = parse_file(FIXTURES / "real" / "redacted_session.jsonl")
    deduped = dedup_entries(result.entries)
    annotated = annotate_cost(deduped, pricing_data)
    total = sum(a.cost_usd for a in annotated if a.cost_usd)
    tolerance = CCUSAGE_TOTAL_USD * 0.01
    assert abs(total - CCUSAGE_TOTAL_USD) < tolerance, (
        f"ccforensics computed ${total:.4f}, ccusage reported ${CCUSAGE_TOTAL_USD:.4f}, "
        f"diff ${abs(total - CCUSAGE_TOTAL_USD):.4f} exceeds ±1% tolerance ${tolerance:.4f}"
    )
