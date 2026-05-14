"""White Rabbit PTP clock distribution simulation for sub-nanosecond precision timing.

Models the White Rabbit extension to IEEE 1588 PTP, providing deterministic
sub-nanosecond synchronization across a network of hardware clocks connected
via fiber optic links. Used for precise event timestamping in trading systems.

Reference: https://white-rabbit.web.cern.ch/
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from random import Random
from typing import TypeAlias

from common.models import Nanos

# Sub-nanosecond types for internal precision
Picoseconds: TypeAlias = int
PartsPerBillion: TypeAlias = float


class AccuracyClass(Enum):
    """Clock accuracy classification per IEEE 1588 / White Rabbit profiles."""

    WR_SUBNS = auto()     # White Rabbit: < 1 ns (typically ~10-100 ps)
    PTP_NS = auto()       # Standard PTP: ~1-100 ns
    SOFTWARE_US = auto()  # Software timestamping: ~1-10 µs


class CalibrationState(Enum):
    """Tracks the calibration lifecycle of a clock in the hierarchy."""

    UNCALIBRATED = auto()
    CALIBRATING = auto()
    CALIBRATED = auto()
    HOLDOVER = auto()  # Lost sync, coasting on last known correction


class WhiteRabbitClock:
    """Simulates a White Rabbit PTP hardware clock with sub-nanosecond precision.

    Models realistic clock behavior including:
    - Crystal oscillator drift (parts per billion)
    - Phase noise / jitter (picoseconds RMS)
    - PTP sync pulse discipline
    - Deterministic output via seeded PRNG

    Args:
        clock_id: Unique identifier for this clock instance.
        grandmaster_offset_ns: Static offset from the grandmaster reference (ns).
        drift_ppb: Oscillator frequency drift in parts per billion.
        jitter_ps: RMS phase jitter in picoseconds.
        seed: RNG seed for deterministic reproducibility.
    """

    def __init__(
        self,
        clock_id: str,
        grandmaster_offset_ns: float = 0.0,
        drift_ppb: PartsPerBillion = 0.1,
        jitter_ps: Picoseconds = 50,
        seed: int = 42,
    ) -> None:
        self.clock_id = clock_id
        self.grandmaster_offset_ns = grandmaster_offset_ns
        self.drift_ppb = drift_ppb
        self.jitter_ps = jitter_ps

        self._rng = Random(seed)
        self._epoch_ns: Nanos = time.time_ns()
        self._drift_accumulator_ns: float = 0.0
        self._last_sync_ns: Nanos = self._epoch_ns
        self._sync_count: int = 0

    def now_ns(self) -> Nanos:
        """Return the current nanosecond timestamp with realistic drift and jitter.

        The timestamp includes:
        - Wall-clock time as base
        - Accumulated drift since last sync pulse
        - Gaussian phase jitter
        - Static grandmaster offset

        Returns:
            Nanosecond timestamp reflecting this clock's view of time.
        """
        wall_ns = time.time_ns()
        elapsed_since_sync = wall_ns - self._last_sync_ns

        # Drift accumulates linearly: drift_ppb * elapsed_ns * 1e-9 gives ns of drift
        drift_ns = elapsed_since_sync * self.drift_ppb * 1e-9
        self._drift_accumulator_ns = drift_ns

        # Jitter: Gaussian with RMS = jitter_ps, converted to ns
        jitter_ns = self._rng.gauss(0.0, self.jitter_ps / 1000.0)

        corrected = wall_ns + self.grandmaster_offset_ns + drift_ns + jitter_ns
        return int(round(corrected))

    def sync_pulse(self) -> None:
        """Simulate reception of a PTP sync pulse from the grandmaster.

        Resets the drift accumulator and records the sync event.
        In a real White Rabbit system, this occurs every sync interval
        (typically 1 second) and disciplines the local oscillator.
        """
        self._last_sync_ns = time.time_ns()
        self._drift_accumulator_ns = 0.0
        self._sync_count += 1

    def elapsed_since(self, reference_ns: Nanos) -> Nanos:
        """Compute drift-corrected elapsed time since a reference timestamp.

        Applies the current drift rate to correct the raw elapsed measurement,
        providing a more accurate interval than simple subtraction.

        Args:
            reference_ns: The reference nanosecond timestamp to measure from.

        Returns:
            Drift-corrected elapsed nanoseconds.
        """
        raw_elapsed = self.now_ns() - reference_ns
        # Remove drift contribution from the elapsed measurement
        drift_correction = raw_elapsed * self.drift_ppb * 1e-9
        corrected_elapsed = raw_elapsed - int(round(drift_correction))
        return corrected_elapsed

    @property
    def sync_count(self) -> int:
        """Number of sync pulses received since instantiation."""
        return self._sync_count

    @property
    def current_drift_ns(self) -> float:
        """Current accumulated drift in nanoseconds since last sync."""
        return self._drift_accumulator_ns


class ClockDomain:
    """Models a network of synchronized White Rabbit clocks.

    Represents a PTP clock domain with a grandmaster and multiple boundary
    clocks connected via fiber optic links. Each node maintains its own
    WhiteRabbitClock instance with configurable path asymmetry.

    Args:
        domain_id: Unique identifier for this clock domain.
        sync_interval_s: Interval between sync pulses in seconds (WR default: 1.0).
        seed: Base RNG seed for deterministic domain behavior.
    """

    def __init__(
        self,
        domain_id: str = "domain-0",
        sync_interval_s: float = 1.0,
        seed: int = 42,
    ) -> None:
        self.domain_id = domain_id
        self.sync_interval_s = sync_interval_s
        self._seed = seed
        self._rng = Random(seed)

        self._clocks: dict[str, WhiteRabbitClock] = {}
        self._fiber_paths: dict[tuple[str, str], float] = {}  # (src, dst) -> delay_ns
        self._grandmaster_id: str | None = None

    def add_grandmaster(
        self,
        node_id: str,
        drift_ppb: PartsPerBillion = 0.01,
        jitter_ps: Picoseconds = 10,
    ) -> WhiteRabbitClock:
        """Add the grandmaster clock to this domain.

        The grandmaster has zero offset by definition and typically has
        the lowest drift and jitter (OCXO or atomic reference).

        Args:
            node_id: Identifier for the grandmaster node.
            drift_ppb: Grandmaster oscillator drift (typically very low).
            jitter_ps: Grandmaster phase jitter in picoseconds.

        Returns:
            The created WhiteRabbitClock instance.
        """
        clock = WhiteRabbitClock(
            clock_id=node_id,
            grandmaster_offset_ns=0.0,
            drift_ppb=drift_ppb,
            jitter_ps=jitter_ps,
            seed=self._rng.randint(0, 2**32 - 1),
        )
        self._clocks[node_id] = clock
        self._grandmaster_id = node_id
        return clock

    def add_boundary_clock(
        self,
        node_id: str,
        asymmetry_ns: float = 0.0,
        drift_ppb: PartsPerBillion = 0.1,
        jitter_ps: Picoseconds = 50,
    ) -> WhiteRabbitClock:
        """Add a boundary clock node to this domain.

        Boundary clocks relay timing from the grandmaster with their own
        local oscillator characteristics and configurable path asymmetry.

        Args:
            node_id: Identifier for the boundary clock node.
            asymmetry_ns: Fixed asymmetric delay offset in nanoseconds.
            drift_ppb: Local oscillator drift in parts per billion.
            jitter_ps: Local phase jitter in picoseconds.

        Returns:
            The created WhiteRabbitClock instance.
        """
        clock = WhiteRabbitClock(
            clock_id=node_id,
            grandmaster_offset_ns=asymmetry_ns,
            drift_ppb=drift_ppb,
            jitter_ps=jitter_ps,
            seed=self._rng.randint(0, 2**32 - 1),
        )
        self._clocks[node_id] = clock
        return clock

    def add_fiber_path(
        self,
        source: str,
        dest: str,
        distance_m: float,
        refractive_index: float = 1.4682,
    ) -> None:
        """Register a fiber optic path between two nodes.

        Computes propagation delay based on fiber length and refractive index.
        Speed of light in fiber = c / n ≈ 2.041e8 m/s for standard SMF-28.

        Args:
            source: Source node identifier.
            dest: Destination node identifier.
            distance_m: Fiber path length in meters.
            refractive_index: Effective group refractive index (default: SMF-28).
        """
        c_vacuum = 299_792_458.0  # m/s
        v_fiber = c_vacuum / refractive_index
        delay_s = distance_m / v_fiber
        delay_ns = delay_s * 1e9

        # Bidirectional path (symmetric by default)
        self._fiber_paths[(source, dest)] = delay_ns
        self._fiber_paths[(dest, source)] = delay_ns

    def propagation_delay_ns(self, source: str, dest: str) -> float:
        """Return the one-way propagation delay between two nodes in nanoseconds.

        Args:
            source: Source node identifier.
            dest: Destination node identifier.

        Returns:
            Propagation delay in nanoseconds.

        Raises:
            KeyError: If no fiber path is registered between source and dest.
        """
        key = (source, dest)
        if key not in self._fiber_paths:
            raise KeyError(
                f"No fiber path registered between '{source}' and '{dest}'"
            )
        return self._fiber_paths[key]

    def timestamp_at(self, node_id: str) -> Nanos:
        """Return the current timestamp as seen by a specific node's clock.

        Args:
            node_id: Identifier of the clock node to query.

        Returns:
            Nanosecond timestamp from that node's perspective.

        Raises:
            KeyError: If the node_id is not registered in this domain.
        """
        if node_id not in self._clocks:
            raise KeyError(f"Node '{node_id}' not found in domain '{self.domain_id}'")
        return self._clocks[node_id].now_ns()

    def sync_all(self) -> None:
        """Broadcast a sync pulse to all clocks in the domain.

        Simulates the grandmaster issuing a Sync message that propagates
        to all boundary clocks (with their respective propagation delays).
        """
        for clock in self._clocks.values():
            clock.sync_pulse()

    def get_clock(self, node_id: str) -> WhiteRabbitClock:
        """Retrieve the clock instance for a given node.

        Args:
            node_id: Identifier of the clock node.

        Returns:
            The WhiteRabbitClock instance.

        Raises:
            KeyError: If the node_id is not registered.
        """
        if node_id not in self._clocks:
            raise KeyError(f"Node '{node_id}' not found in domain '{self.domain_id}'")
        return self._clocks[node_id]

    @property
    def grandmaster_id(self) -> str | None:
        """Identifier of the current grandmaster clock, or None if unset."""
        return self._grandmaster_id

    @property
    def node_ids(self) -> list[str]:
        """List of all node identifiers in this domain."""
        return list(self._clocks.keys())


@dataclass(frozen=True, slots=True)
class HardwareTimestamp:
    """Immutable hardware timestamp with accuracy metadata.

    Represents a single timestamping event captured by a hardware clock,
    including both the raw capture and any corrections applied.

    Attributes:
        raw_ns: Raw nanosecond timestamp as captured by hardware.
        corrected_ns: Timestamp after applying clock corrections.
        clock_id: Identifier of the clock that captured this timestamp.
        domain_id: PTP domain identifier.
        accuracy_class: Classification of timestamp accuracy.
    """

    raw_ns: Nanos
    corrected_ns: Nanos
    clock_id: str
    domain_id: str
    accuracy_class: AccuracyClass = AccuracyClass.WR_SUBNS

    def correction_applied(self) -> Nanos:
        """Return the correction offset applied to the raw timestamp.

        Returns:
            Difference between corrected and raw timestamps in nanoseconds.
            Positive values indicate the corrected time is ahead of raw.
        """
        return self.corrected_ns - self.raw_ns


@dataclass
class UncertaintyBound:
    """Represents the uncertainty in a cross-clock timestamp comparison.

    Attributes:
        delta_ns: Measured time difference in nanoseconds.
        uncertainty_ns: Symmetric uncertainty bound (±) in nanoseconds.
        confidence: Confidence level (0.0 to 1.0) for the uncertainty bound.
    """

    delta_ns: float
    uncertainty_ns: float
    confidence: float = 0.95


class TimestampAuthority:
    """Manages clock hierarchy and provides authoritative timestamping.

    Coordinates multiple clock domains, tracks calibration state, and
    provides methods for stamping events and comparing timestamps across
    different clocks with proper uncertainty quantification.

    Args:
        authority_id: Unique identifier for this timestamp authority.
    """

    def __init__(self, authority_id: str = "tsa-primary") -> None:
        self.authority_id = authority_id
        self._domains: dict[str, ClockDomain] = {}
        self._calibration_states: dict[str, CalibrationState] = {}
        self._calibration_offsets: dict[str, float] = {}  # clock_id -> offset_ns

    def register_domain(self, domain: ClockDomain) -> None:
        """Register a clock domain under this authority.

        All clocks in the domain become available for stamping and comparison.

        Args:
            domain: The ClockDomain to register.
        """
        self._domains[domain.domain_id] = domain
        for node_id in domain.node_ids:
            self._calibration_states[node_id] = CalibrationState.UNCALIBRATED
            self._calibration_offsets[node_id] = 0.0

    def calibrate(self, clock_id: str, reference_offset_ns: float = 0.0) -> None:
        """Calibrate a clock against the known reference offset.

        Transitions the clock through CALIBRATING -> CALIBRATED states
        and records the correction offset.

        Args:
            clock_id: Identifier of the clock to calibrate.
            reference_offset_ns: Measured offset from reference in nanoseconds.

        Raises:
            KeyError: If the clock_id is not registered.
        """
        if clock_id not in self._calibration_states:
            raise KeyError(f"Clock '{clock_id}' not registered with this authority")

        self._calibration_states[clock_id] = CalibrationState.CALIBRATING
        self._calibration_offsets[clock_id] = reference_offset_ns
        self._calibration_states[clock_id] = CalibrationState.CALIBRATED

    def stamp(self, event: str, clock_id: str) -> HardwareTimestamp:
        """Create an authoritative hardware timestamp for an event.

        Captures the current time from the specified clock and applies
        any calibration corrections.

        Args:
            event: Description of the event being timestamped.
            clock_id: Identifier of the clock to use for stamping.

        Returns:
            A HardwareTimestamp with raw and corrected values.

        Raises:
            KeyError: If the clock_id is not found in any registered domain.
        """
        # Find the domain containing this clock
        domain_id: str | None = None
        clock: WhiteRabbitClock | None = None

        for did, domain in self._domains.items():
            if clock_id in domain.node_ids:
                domain_id = did
                clock = domain.get_clock(clock_id)
                break

        if clock is None or domain_id is None:
            raise KeyError(
                f"Clock '{clock_id}' not found in any registered domain"
            )

        raw_ns = clock.now_ns()
        correction = self._calibration_offsets.get(clock_id, 0.0)
        corrected_ns = int(round(raw_ns - correction))

        # Determine accuracy class based on calibration state
        cal_state = self._calibration_states.get(clock_id, CalibrationState.UNCALIBRATED)
        if cal_state == CalibrationState.CALIBRATED:
            accuracy = AccuracyClass.WR_SUBNS
        elif cal_state == CalibrationState.HOLDOVER:
            accuracy = AccuracyClass.PTP_NS
        else:
            accuracy = AccuracyClass.SOFTWARE_US

        return HardwareTimestamp(
            raw_ns=raw_ns,
            corrected_ns=corrected_ns,
            clock_id=clock_id,
            domain_id=domain_id,
            accuracy_class=accuracy,
        )

    def compare(self, ts_a: HardwareTimestamp, ts_b: HardwareTimestamp) -> UncertaintyBound:
        """Compare two timestamps from potentially different clocks.

        Computes the time difference with uncertainty bounds based on
        the accuracy classes of both timestamps.

        Args:
            ts_a: First hardware timestamp.
            ts_b: Second hardware timestamp.

        Returns:
            UncertaintyBound with the delta and symmetric error bound.
        """
        delta_ns = float(ts_a.corrected_ns - ts_b.corrected_ns)

        # Uncertainty model based on accuracy classes
        uncertainty_map: dict[AccuracyClass, float] = {
            AccuracyClass.WR_SUBNS: 0.1,       # 100 ps
            AccuracyClass.PTP_NS: 10.0,         # 10 ns
            AccuracyClass.SOFTWARE_US: 1000.0,  # 1 µs
        }

        unc_a = uncertainty_map[ts_a.accuracy_class]
        unc_b = uncertainty_map[ts_b.accuracy_class]

        # Root-sum-square of independent uncertainties
        combined_uncertainty = (unc_a**2 + unc_b**2) ** 0.5

        # Cross-domain comparison adds additional uncertainty
        if ts_a.domain_id != ts_b.domain_id:
            combined_uncertainty *= 2.0  # Conservative factor for inter-domain

        return UncertaintyBound(
            delta_ns=delta_ns,
            uncertainty_ns=combined_uncertainty,
            confidence=0.95,
        )

    def get_calibration_state(self, clock_id: str) -> CalibrationState:
        """Query the current calibration state of a clock.

        Args:
            clock_id: Identifier of the clock to query.

        Returns:
            Current CalibrationState.

        Raises:
            KeyError: If the clock_id is not registered.
        """
        if clock_id not in self._calibration_states:
            raise KeyError(f"Clock '{clock_id}' not registered with this authority")
        return self._calibration_states[clock_id]

    def mark_holdover(self, clock_id: str) -> None:
        """Mark a clock as being in holdover (lost sync with grandmaster).

        Args:
            clock_id: Identifier of the clock entering holdover.

        Raises:
            KeyError: If the clock_id is not registered.
        """
        if clock_id not in self._calibration_states:
            raise KeyError(f"Clock '{clock_id}' not registered with this authority")
        self._calibration_states[clock_id] = CalibrationState.HOLDOVER

    @property
    def registered_domains(self) -> list[str]:
        """List of registered domain identifiers."""
        return list(self._domains.keys())

    @property
    def calibrated_clocks(self) -> list[str]:
        """List of clock IDs currently in CALIBRATED state."""
        return [
            cid
            for cid, state in self._calibration_states.items()
            if state == CalibrationState.CALIBRATED
        ]
