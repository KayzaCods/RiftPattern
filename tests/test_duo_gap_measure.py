"""Unit tests for the read-only #22 co-presence measurement helper."""
from __future__ import annotations

import sys
import subprocess
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import duo_gap_measure as measure


def test_help_runs_from_project_root_without_import_error():
    root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [sys.executable, "scripts/duo_gap_measure.py", "--help"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_duo_guard_excludes_nearby_adc_outside_bot_but_tolerates_partial_positions():
    """The measurement must mirror trade_v1 without treating unknown as absent."""
    us_in_bot = (10_000, 4_000)
    adc_in_jungle = (10_000, 6_000)  # 2,000u away: close, but not bot presence.

    assert measure.duo_guard_excludes(us_in_bot, adc_in_jungle, 100, 2_500)
    assert not measure.duo_guard_excludes(us_in_bot, (10_000, None), 100, 2_500)
    assert not measure.duo_guard_excludes((10_000, None), (10_100, 4_000), 100, 2_500)
