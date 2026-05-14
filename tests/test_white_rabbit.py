"""Comprehensive tests for the White Rabbit clock distribution module.

Tests cover:
- WhiteRabbitClock: determinism, drift, jitter, sync pulse
- ClockDomain: propagation delay, independent clocks, sync_all
- HardwareTimestamp: correction arithmetic, accuracy class
- TimestampAuthority: stamping, comparison, calibration state machine
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from clock.white_rabbit import (
    AccuracyClass,
    CalibrationState,
    ClockDomain,
    HardwareTimestamp,
    TimestampAuthority,
    UncertaintyBound,
    WhiteRabbitClock,
)
from common.models import Nanos


# =============================================================================
# WhiteRabbitClock tests
# =============================================================================


class TestWhiteRabbitClock:
    """Tests for WhiteRabbitClock determinism, drift, jitter, and sync."""

    def test_deterministic_output_with_same_seed(self):
        """Two clocks with the same seed and identical wall-clock produce identical jitter sequences."""
        fixed_time = 1_000_000_000_000  # fixed wall-clock ns

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time):
            clock_a = WhiteRabbitClock("clk-a", seed=123)
            clock_b = WhiteRabbitClock("clk-b", seed=123)

        # Both clocks started at the same mock time, so _epoch_ns and _last_sync_ns are equal.
        # Now read timestamps - jitter is driven by the seeded RNG so should be identical.
        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 1_000_000):
            ts_a = clock_a.now_ns()
            ts_b = clock_b.now_ns()

        assert ts_a == ts_b, "Same seed should produce identical timestamps"

    def test_different_seeds_produce_different_output(self):
        """Different seeds should yield different jitter sequences over multiple samples."""
        fixed_time = 1_000_000_000_000

        # Use large jitter so differences are visible even after rounding to int ns
        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time):
            clock_a = WhiteRabbitClock("clk-a", jitter_ps=5000, seed=1)
            clock_b = WhiteRabbitClock("clk-b", jitter_ps=5000, seed=2)

        # Collect multiple samples - sequences should diverge
        samples_a = []
        samples_b = []
        for i in range(20):
            with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 1_000_000):
                samples_a.append(clock_a.now_ns())
                samples_b.append(clock_b.now_ns())

        # At least some samples should differ with high jitter and different seeds
        assert samples_a != samples_b

    def test_drift_accumulates_correctly(self):
        """Drift should scale linearly with elapsed time: drift_ns = elapsed * drift_ppb * 1e-9."""
        drift_ppb = 10.0  # 10 ppb for easier math
        start_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=start_time):
            clock = WhiteRabbitClock("clk", drift_ppb=drift_ppb, jitter_ps=0, seed=42)

        # Advance by 1 second (1e9 ns)
        elapsed_ns = 1_000_000_000
        with patch("clock.white_rabbit.time.time_ns", return_value=start_time + elapsed_ns):
            _ = clock.now_ns()

        # Expected drift: 1e9 * 10 * 1e-9 = 10 ns
        expected_drift = elapsed_ns * drift_ppb * 1e-9
        assert clock.current_drift_ns == pytest.approx(expected_drift, rel=1e-9)

    def test_drift_scales_with_elapsed_time(self):
        """Doubling elapsed time should double the drift."""
        drift_ppb = 5.0
        start_time = 2_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=start_time):
            clock = WhiteRabbitClock("clk", drift_ppb=drift_ppb, jitter_ps=0, seed=7)

        # 0.5 second elapsed
        with patch("clock.white_rabbit.time.time_ns", return_value=start_time + 500_000_000):
            _ = clock.now_ns()
            drift_half = clock.current_drift_ns

        # 1.0 second elapsed
        with patch("clock.white_rabbit.time.time_ns", return_value=start_time + 1_000_000_000):
            _ = clock.now_ns()
            drift_full = clock.current_drift_ns

        assert drift_full == pytest.approx(2 * drift_half, rel=1e-9)

    def test_jitter_stays_within_bounds(self):
        """Jitter (Gaussian) should statistically stay within ±5 sigma of jitter_ps/1000 ns."""
        jitter_ps = 100  # 100 ps RMS -> 0.1 ns RMS
        jitter_ns_rms = jitter_ps / 1000.0
        bound_ns = 5 * jitter_ns_rms  # 5-sigma bound = 0.5 ns

        start_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=start_time):
            clock = WhiteRabbitClock(
                "clk", drift_ppb=0.0, jitter_ps=jitter_ps, grandmaster_offset_ns=0.0, seed=42
            )

        # Take many samples and check jitter is bounded
        samples = []
        # With zero drift and zero offset, the only variable is jitter
        for i in range(500):
            with patch("clock.white_rabbit.time.time_ns", return_value=start_time):
                ts = clock.now_ns()
                # Deviation from the wall clock (with zero drift, zero offset)
                deviation = ts - start_time
                samples.append(deviation)

        # All deviations should be within 5-sigma (~0.5 ns -> rounds to 0 or ±1)
        for s in samples:
            assert abs(s) <= bound_ns + 1, f"Jitter {s} exceeded 5-sigma bound {bound_ns}"

    def test_jitter_distribution_is_centered(self):
        """Mean jitter over many samples should be approximately zero."""
        start_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=start_time):
            clock = WhiteRabbitClock(
                "clk", drift_ppb=0.0, jitter_ps=200, grandmaster_offset_ns=0.0, seed=99
            )

        deviations = []
        for _ in range(1000):
            with patch("clock.white_rabbit.time.time_ns", return_value=start_time):
                ts = clock.now_ns()
                deviations.append(ts - start_time)

        mean_dev = sum(deviations) / len(deviations)
        # Mean should be close to 0 (within a few fractions of ns given large sample)
        assert abs(mean_dev) < 0.05, f"Mean jitter {mean_dev} not centered near zero"

    def test_sync_pulse_resets_drift(self):
        """sync_pulse should zero out the drift accumulator."""
        start_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=start_time):
            clock = WhiteRabbitClock("clk", drift_ppb=10.0, jitter_ps=0, seed=42)

        # Let drift accumulate
        with patch("clock.white_rabbit.time.time_ns", return_value=start_time + 1_000_000_000):
            _ = clock.now_ns()
            assert clock.current_drift_ns > 0

        # Sync pulse resets drift
        with patch("clock.white_rabbit.time.time_ns", return_value=start_time + 1_000_000_000):
            clock.sync_pulse()

        assert clock.current_drift_ns == 0.0

    def test_sync_pulse_increments_counter(self):
        """Each sync_pulse should increment the sync count."""
        start_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=start_time):
            clock = WhiteRabbitClock("clk", seed=42)

        assert clock.sync_count == 0

        with patch("clock.white_rabbit.time.time_ns", return_value=start_time + 100):
            clock.sync_pulse()
        assert clock.sync_count == 1

        with patch("clock.white_rabbit.time.time_ns", return_value=start_time + 200):
            clock.sync_pulse()
        assert clock.sync_count == 2

    def test_now_ns_returns_int(self):
        """now_ns should always return an integer (Nanos)."""
        start_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=start_time):
            clock = WhiteRabbitClock("clk", seed=42)

        with patch("clock.white_rabbit.time.time_ns", return_value=start_time + 5000):
            ts = clock.now_ns()

        assert isinstance(ts, int)


# =============================================================================
# ClockDomain tests
# =============================================================================


class TestClockDomain:
    """Tests for ClockDomain fiber propagation, clock independence, and sync."""

    def test_propagation_delay_scales_with_fiber_length(self):
        """Delay should be proportional to fiber distance: delay = distance / (c/n) * 1e9."""
        domain = ClockDomain("test-domain", seed=1)
        domain.add_grandmaster("gm")
        domain.add_boundary_clock("bc1")
        domain.add_boundary_clock("bc2")

        distance_1 = 100.0  # meters
        distance_2 = 200.0  # meters
        refractive_index = 1.4682

        domain.add_fiber_path("gm", "bc1", distance_1, refractive_index)
        domain.add_fiber_path("gm", "bc2", distance_2, refractive_index)

        delay_1 = domain.propagation_delay_ns("gm", "bc1")
        delay_2 = domain.propagation_delay_ns("gm", "bc2")

        # Delay should double when distance doubles
        assert delay_2 == pytest.approx(2 * delay_1, rel=1e-10)

    def test_propagation_delay_formula(self):
        """Verify the exact propagation delay formula: d/(c/n) * 1e9 ns."""
        domain = ClockDomain("test-domain", seed=1)
        domain.add_grandmaster("gm")
        domain.add_boundary_clock("bc")

        distance_m = 1000.0  # 1 km
        refractive_index = 1.4682
        c_vacuum = 299_792_458.0

        domain.add_fiber_path("gm", "bc", distance_m, refractive_index)

        expected_delay_ns = distance_m / (c_vacuum / refractive_index) * 1e9
        actual_delay = domain.propagation_delay_ns("gm", "bc")

        assert actual_delay == pytest.approx(expected_delay_ns, rel=1e-12)

    def test_propagation_delay_approx_5ns_per_meter(self):
        """Standard fiber (~n=1.4682) gives approximately 4.9 ns/m propagation delay."""
        domain = ClockDomain("test-domain", seed=1)
        domain.add_grandmaster("gm")
        domain.add_boundary_clock("bc")

        distance_m = 1.0
        refractive_index = 1.4682

        domain.add_fiber_path("gm", "bc", distance_m, refractive_index)
        delay = domain.propagation_delay_ns("gm", "bc")

        # Should be ~4.897 ns/m (often approximated as ~5 ns/m)
        assert 4.8 < delay < 5.1, f"Expected ~5ns/m, got {delay}"

    def test_propagation_delay_bidirectional(self):
        """Fiber paths are registered bidirectionally (symmetric)."""
        domain = ClockDomain("test-domain", seed=1)
        domain.add_grandmaster("gm")
        domain.add_boundary_clock("bc")

        domain.add_fiber_path("gm", "bc", 500.0)

        delay_forward = domain.propagation_delay_ns("gm", "bc")
        delay_reverse = domain.propagation_delay_ns("bc", "gm")

        assert delay_forward == delay_reverse

    def test_missing_fiber_path_raises_keyerror(self):
        """Querying a non-existent fiber path should raise KeyError."""
        domain = ClockDomain("test-domain", seed=1)
        domain.add_grandmaster("gm")
        domain.add_boundary_clock("bc")

        with pytest.raises(KeyError, match="No fiber path registered"):
            domain.propagation_delay_ns("gm", "bc")

    def test_multiple_nodes_maintain_independent_clocks(self):
        """Each node has its own independent clock with its own drift/jitter."""
        fixed_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time):
            domain = ClockDomain("test-domain", seed=100)
            domain.add_grandmaster("gm", drift_ppb=0.01, jitter_ps=10)
            domain.add_boundary_clock("bc1", drift_ppb=1.0, jitter_ps=50)
            domain.add_boundary_clock("bc2", drift_ppb=5.0, jitter_ps=100)

        # Clocks have different drift rates
        gm = domain.get_clock("gm")
        bc1 = domain.get_clock("bc1")
        bc2 = domain.get_clock("bc2")

        assert gm.drift_ppb == 0.01
        assert bc1.drift_ppb == 1.0
        assert bc2.drift_ppb == 5.0

        # After elapsed time, drifts should differ
        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 1_000_000_000):
            _ = gm.now_ns()
            _ = bc1.now_ns()
            _ = bc2.now_ns()

        assert bc2.current_drift_ns > bc1.current_drift_ns > gm.current_drift_ns

    def test_sync_all_reduces_divergence(self):
        """sync_all should reset drift accumulators on all clocks, reducing divergence."""
        fixed_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time):
            domain = ClockDomain("test-domain", seed=50)
            domain.add_grandmaster("gm", drift_ppb=0.01)
            domain.add_boundary_clock("bc1", drift_ppb=5.0)
            domain.add_boundary_clock("bc2", drift_ppb=10.0)

        # Let clocks drift for 1 second
        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 1_000_000_000):
            _ = domain.get_clock("bc1").now_ns()
            _ = domain.get_clock("bc2").now_ns()

            assert domain.get_clock("bc1").current_drift_ns > 0
            assert domain.get_clock("bc2").current_drift_ns > 0

        # Sync all resets drift
        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 1_000_000_000):
            domain.sync_all()

        assert domain.get_clock("bc1").current_drift_ns == 0.0
        assert domain.get_clock("bc2").current_drift_ns == 0.0
        assert domain.get_clock("gm").current_drift_ns == 0.0

    def test_sync_all_increments_sync_count_for_all(self):
        """sync_all should increment sync_count on every clock in the domain."""
        fixed_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time):
            domain = ClockDomain("test-domain", seed=1)
            domain.add_grandmaster("gm")
            domain.add_boundary_clock("bc1")
            domain.add_boundary_clock("bc2")

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 100):
            domain.sync_all()

        for node_id in domain.node_ids:
            assert domain.get_clock(node_id).sync_count == 1

    def test_grandmaster_id_is_set(self):
        """After adding grandmaster, grandmaster_id should be set."""
        domain = ClockDomain("d1")
        assert domain.grandmaster_id is None

        domain.add_grandmaster("gm-main")
        assert domain.grandmaster_id == "gm-main"

    def test_node_ids_lists_all_clocks(self):
        """node_ids should return all registered node identifiers."""
        fixed_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time):
            domain = ClockDomain("d1")
            domain.add_grandmaster("gm")
            domain.add_boundary_clock("bc1")
            domain.add_boundary_clock("bc2")

        assert set(domain.node_ids) == {"gm", "bc1", "bc2"}


# =============================================================================
# HardwareTimestamp tests
# =============================================================================


class TestHardwareTimestamp:
    """Tests for HardwareTimestamp correction and accuracy class."""

    def test_correction_applied_returns_raw_minus_corrected(self):
        """correction_applied() should return corrected_ns - raw_ns."""
        ts = HardwareTimestamp(
            raw_ns=1_000_000_000,
            corrected_ns=1_000_000_050,
            clock_id="clk-1",
            domain_id="d0",
            accuracy_class=AccuracyClass.WR_SUBNS,
        )
        assert ts.correction_applied() == 50

    def test_correction_applied_negative(self):
        """correction_applied() can be negative when corrected < raw."""
        ts = HardwareTimestamp(
            raw_ns=1_000_000_100,
            corrected_ns=1_000_000_000,
            clock_id="clk-1",
            domain_id="d0",
        )
        assert ts.correction_applied() == -100

    def test_correction_applied_zero(self):
        """correction_applied() is zero when raw == corrected."""
        ts = HardwareTimestamp(
            raw_ns=5_000_000,
            corrected_ns=5_000_000,
            clock_id="clk-1",
            domain_id="d0",
        )
        assert ts.correction_applied() == 0

    def test_accuracy_class_enum_wr_subns(self):
        """AccuracyClass.WR_SUBNS should be usable and distinct."""
        ts = HardwareTimestamp(
            raw_ns=100,
            corrected_ns=100,
            clock_id="c",
            domain_id="d",
            accuracy_class=AccuracyClass.WR_SUBNS,
        )
        assert ts.accuracy_class == AccuracyClass.WR_SUBNS
        assert ts.accuracy_class != AccuracyClass.PTP_NS

    def test_accuracy_class_enum_ptp_ns(self):
        """AccuracyClass.PTP_NS should be a valid accuracy class."""
        ts = HardwareTimestamp(
            raw_ns=100,
            corrected_ns=100,
            clock_id="c",
            domain_id="d",
            accuracy_class=AccuracyClass.PTP_NS,
        )
        assert ts.accuracy_class == AccuracyClass.PTP_NS

    def test_accuracy_class_enum_software_us(self):
        """AccuracyClass.SOFTWARE_US should be a valid accuracy class."""
        ts = HardwareTimestamp(
            raw_ns=100,
            corrected_ns=100,
            clock_id="c",
            domain_id="d",
            accuracy_class=AccuracyClass.SOFTWARE_US,
        )
        assert ts.accuracy_class == AccuracyClass.SOFTWARE_US

    def test_default_accuracy_class_is_wr_subns(self):
        """Default accuracy_class should be WR_SUBNS."""
        ts = HardwareTimestamp(
            raw_ns=100,
            corrected_ns=100,
            clock_id="c",
            domain_id="d",
        )
        assert ts.accuracy_class == AccuracyClass.WR_SUBNS

    def test_hardware_timestamp_is_immutable(self):
        """HardwareTimestamp is frozen and should reject attribute assignment."""
        ts = HardwareTimestamp(
            raw_ns=100,
            corrected_ns=100,
            clock_id="c",
            domain_id="d",
        )
        with pytest.raises(Exception):  # FrozenInstanceError (subclass of AttributeError)
            ts.raw_ns = 999  # type: ignore[misc]

    def test_nanos_type_is_int(self):
        """The Nanos type alias should resolve to int."""
        assert Nanos is int or isinstance(42, Nanos)  # TypeAlias = int


# =============================================================================
# TimestampAuthority tests
# =============================================================================


class TestTimestampAuthority:
    """Tests for TimestampAuthority stamping, comparison, and calibration states."""

    @pytest.fixture
    def authority_with_domain(self):
        """Create a TimestampAuthority with a registered domain and clocks."""
        fixed_time = 1_000_000_000_000

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time):
            domain = ClockDomain("domain-0", seed=42)
            domain.add_grandmaster("gm")
            domain.add_boundary_clock("bc1", drift_ppb=0.1, jitter_ps=50)
            domain.add_boundary_clock("bc2", drift_ppb=0.5, jitter_ps=100)

        authority = TimestampAuthority("tsa-test")
        authority.register_domain(domain)
        return authority, domain, fixed_time

    def test_stamp_returns_valid_hardware_timestamp(self, authority_with_domain):
        """stamp() should return a HardwareTimestamp instance with correct fields."""
        authority, domain, fixed_time = authority_with_domain

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 1000):
            ts = authority.stamp("order_received", "gm")

        assert isinstance(ts, HardwareTimestamp)
        assert ts.clock_id == "gm"
        assert ts.domain_id == "domain-0"
        assert isinstance(ts.raw_ns, int)
        assert isinstance(ts.corrected_ns, int)

    def test_stamp_raw_ns_is_from_clock(self, authority_with_domain):
        """stamp() raw_ns should come from the specified clock's now_ns()."""
        authority, domain, fixed_time = authority_with_domain

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 5000):
            ts = authority.stamp("event", "gm")

        # raw_ns should be reasonably close to the mock wall-clock
        assert abs(ts.raw_ns - (fixed_time + 5000)) < 100  # within jitter bounds

    def test_stamp_applies_calibration_offset(self, authority_with_domain):
        """After calibration with offset, corrected_ns should differ from raw_ns."""
        authority, domain, fixed_time = authority_with_domain

        # Calibrate with a known offset
        authority.calibrate("bc1", reference_offset_ns=42.0)

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 10000):
            ts = authority.stamp("event", "bc1")

        # corrected = raw - offset, so correction_applied = corrected - raw = -offset
        # (approximately, since raw includes jitter)
        expected_correction = -42  # round(-42.0) = -42
        assert ts.correction_applied() == expected_correction

    def test_stamp_uncalibrated_has_software_accuracy(self, authority_with_domain):
        """An uncalibrated clock stamp should have SOFTWARE_US accuracy."""
        authority, domain, fixed_time = authority_with_domain

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 1000):
            ts = authority.stamp("event", "bc1")

        assert ts.accuracy_class == AccuracyClass.SOFTWARE_US

    def test_stamp_calibrated_has_wr_subns_accuracy(self, authority_with_domain):
        """A calibrated clock stamp should have WR_SUBNS accuracy."""
        authority, domain, fixed_time = authority_with_domain

        authority.calibrate("bc1", reference_offset_ns=0.0)

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 1000):
            ts = authority.stamp("event", "bc1")

        assert ts.accuracy_class == AccuracyClass.WR_SUBNS

    def test_stamp_holdover_has_ptp_ns_accuracy(self, authority_with_domain):
        """A clock in holdover should stamp with PTP_NS accuracy."""
        authority, domain, fixed_time = authority_with_domain

        authority.calibrate("bc1", reference_offset_ns=0.0)
        authority.mark_holdover("bc1")

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 1000):
            ts = authority.stamp("event", "bc1")

        assert ts.accuracy_class == AccuracyClass.PTP_NS

    def test_stamp_unknown_clock_raises_keyerror(self, authority_with_domain):
        """Stamping with an unregistered clock_id should raise KeyError."""
        authority, _, _ = authority_with_domain

        with pytest.raises(KeyError, match="not found"):
            authority.stamp("event", "nonexistent-clock")

    def test_compare_returns_uncertainty_bound(self, authority_with_domain):
        """compare() should return an UncertaintyBound instance."""
        authority, domain, fixed_time = authority_with_domain
        authority.calibrate("gm", reference_offset_ns=0.0)
        authority.calibrate("bc1", reference_offset_ns=0.0)

        with patch("clock.white_rabbit.time.time_ns", return_value=fixed_time + 1000):
            ts_a = authority.stamp("event_a", "gm")
            ts_b = authority.stamp("event_b", "bc1")

        result = authority.compare(ts_a, ts_b)
        assert isinstance(result, UncertaintyBound)
        assert hasattr(result, "delta_ns")
        assert hasattr(result, "uncertainty_ns")
        assert hasattr(result, "confidence")

    def test_compare_delta_ns_is_corrected_difference(self, authority_with_domain):
        """compare() delta_ns should be ts_a.corrected_ns - ts_b.corrected_ns."""
        authority, domain, fixed_time = authority_with_domain

        ts_a = HardwareTimestamp(
            raw_ns=1000, corrected_ns=1050, clock_id="gm", domain_id="domain-0"
        )
        ts_b = HardwareTimestamp(
            raw_ns=900, corrected_ns=950, clock_id="bc1", domain_id="domain-0"
        )

        result = authority.compare(ts_a, ts_b)
        assert result.delta_ns == 100.0  # 1050 - 950

    def test_compare_same_domain_uncertainty(self, authority_with_domain):
        """Same-domain WR_SUBNS comparison should have low uncertainty (~0.14 ns)."""
        authority, _, _ = authority_with_domain

        ts_a = HardwareTimestamp(
            raw_ns=1000, corrected_ns=1000, clock_id="gm", domain_id="domain-0",
            accuracy_class=AccuracyClass.WR_SUBNS,
        )
        ts_b = HardwareTimestamp(
            raw_ns=1000, corrected_ns=1000, clock_id="bc1", domain_id="domain-0",
            accuracy_class=AccuracyClass.WR_SUBNS,
        )

        result = authority.compare(ts_a, ts_b)
        # RSS of 0.1 and 0.1 = sqrt(0.01 + 0.01) = ~0.1414
        expected_unc = (0.1**2 + 0.1**2) ** 0.5
        assert result.uncertainty_ns == pytest.approx(expected_unc, rel=1e-6)

    def test_compare_cross_domain_doubles_uncertainty(self):
        """Cross-domain comparison should have 2x the uncertainty factor."""
        authority = TimestampAuthority("tsa")

        ts_a = HardwareTimestamp(
            raw_ns=1000, corrected_ns=1000, clock_id="gm1", domain_id="domain-A",
            accuracy_class=AccuracyClass.WR_SUBNS,
        )
        ts_b = HardwareTimestamp(
            raw_ns=1000, corrected_ns=1000, clock_id="gm2", domain_id="domain-B",
            accuracy_class=AccuracyClass.WR_SUBNS,
        )

        result = authority.compare(ts_a, ts_b)
        base_unc = (0.1**2 + 0.1**2) ** 0.5
        assert result.uncertainty_ns == pytest.approx(base_unc * 2.0, rel=1e-6)

    def test_compare_confidence_is_095(self, authority_with_domain):
        """Default confidence level should be 0.95."""
        authority, _, _ = authority_with_domain

        ts_a = HardwareTimestamp(
            raw_ns=100, corrected_ns=100, clock_id="gm", domain_id="domain-0"
        )
        ts_b = HardwareTimestamp(
            raw_ns=100, corrected_ns=100, clock_id="bc1", domain_id="domain-0"
        )

        result = authority.compare(ts_a, ts_b)
        assert result.confidence == 0.95

    # -------------------------------------------------------------------------
    # Calibration state machine tests
    # -------------------------------------------------------------------------

    def test_initial_state_is_uncalibrated(self, authority_with_domain):
        """All clocks start in UNCALIBRATED state after domain registration."""
        authority, _, _ = authority_with_domain

        for clock_id in ["gm", "bc1", "bc2"]:
            assert authority.get_calibration_state(clock_id) == CalibrationState.UNCALIBRATED

    def test_calibrate_transitions_to_calibrated(self, authority_with_domain):
        """calibrate() should move clock from UNCALIBRATED to CALIBRATED."""
        authority, _, _ = authority_with_domain

        assert authority.get_calibration_state("bc1") == CalibrationState.UNCALIBRATED
        authority.calibrate("bc1", reference_offset_ns=1.5)
        assert authority.get_calibration_state("bc1") == CalibrationState.CALIBRATED

    def test_mark_holdover_transitions_from_calibrated(self, authority_with_domain):
        """mark_holdover() should transition CALIBRATED -> HOLDOVER."""
        authority, _, _ = authority_with_domain

        authority.calibrate("bc1")
        assert authority.get_calibration_state("bc1") == CalibrationState.CALIBRATED

        authority.mark_holdover("bc1")
        assert authority.get_calibration_state("bc1") == CalibrationState.HOLDOVER

    def test_full_calibration_lifecycle(self, authority_with_domain):
        """Test full state machine: UNCALIBRATED -> CALIBRATING -> CALIBRATED -> HOLDOVER."""
        authority, _, _ = authority_with_domain

        # Start UNCALIBRATED
        assert authority.get_calibration_state("bc2") == CalibrationState.UNCALIBRATED

        # calibrate() internally transitions through CALIBRATING to CALIBRATED
        # We verify the final state is CALIBRATED
        authority.calibrate("bc2", reference_offset_ns=3.0)
        assert authority.get_calibration_state("bc2") == CalibrationState.CALIBRATED

        # Mark holdover
        authority.mark_holdover("bc2")
        assert authority.get_calibration_state("bc2") == CalibrationState.HOLDOVER

    def test_recalibrate_from_holdover(self, authority_with_domain):
        """A clock in HOLDOVER can be recalibrated back to CALIBRATED."""
        authority, _, _ = authority_with_domain

        authority.calibrate("bc1")
        authority.mark_holdover("bc1")
        assert authority.get_calibration_state("bc1") == CalibrationState.HOLDOVER

        # Recalibrate
        authority.calibrate("bc1", reference_offset_ns=0.5)
        assert authority.get_calibration_state("bc1") == CalibrationState.CALIBRATED

    def test_calibrated_clocks_property(self, authority_with_domain):
        """calibrated_clocks should list only CALIBRATED clock IDs."""
        authority, _, _ = authority_with_domain

        assert authority.calibrated_clocks == []

        authority.calibrate("gm")
        authority.calibrate("bc1")

        assert set(authority.calibrated_clocks) == {"gm", "bc1"}

    def test_get_calibration_state_unknown_clock_raises(self, authority_with_domain):
        """Querying state of an unregistered clock should raise KeyError."""
        authority, _, _ = authority_with_domain

        with pytest.raises(KeyError, match="not registered"):
            authority.get_calibration_state("ghost-clock")

    def test_mark_holdover_unknown_clock_raises(self, authority_with_domain):
        """mark_holdover on an unregistered clock should raise KeyError."""
        authority, _, _ = authority_with_domain

        with pytest.raises(KeyError, match="not registered"):
            authority.mark_holdover("ghost-clock")

    def test_calibrate_unknown_clock_raises(self, authority_with_domain):
        """calibrate() on an unregistered clock should raise KeyError."""
        authority, _, _ = authority_with_domain

        with pytest.raises(KeyError, match="not registered"):
            authority.calibrate("ghost-clock")

    def test_registered_domains_property(self, authority_with_domain):
        """registered_domains should list all registered domain IDs."""
        authority, _, _ = authority_with_domain
        assert "domain-0" in authority.registered_domains
