"""Hash-bound native acceleration for exact V3 optimizer candidate metrics."""

from __future__ import annotations

import ctypes
import json
import struct
import sys
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Any, Mapping

import numpy as np

from strathmark.v3.contracts.canonical import canonical_digest

_SCHEMA_VERSION = "strathmark-v3-native-optimizer-kernel-v1"
_ALGORITHM = "exact-winner-spread-v1"
_SAMPLING_ALGORITHM = "exact-quantile-linear-pool-and-rank-uniform-u256-v4"
_ABI_VERSION = 1
_NATIVE_ROOT = Path(__file__).resolve().parents[1] / "native"
_BINARY_NAME = "strathmark_v3_optimizer_kernel.dll"
_MANIFEST_NAME = "optimizer_kernel_manifest.json"
_EXPECTED_MANIFEST_FIELDS = {
    "schema_version",
    "manifest_digest",
    "algorithm",
    "sampling_algorithm",
    "abi_version",
    "platform",
    "source_sha256",
    "binary_sha256",
    "compiler",
    "target_cpu",
    "panic_strategy",
    "thread_limit",
    "required_draw_count",
    "maximum_entrants",
}


class KernelIntegrityError(RuntimeError):
    """The installed binary or manifest differs from its sealed identity."""


class KernelUnavailableError(RuntimeError):
    """The sealed native kernel is not executable on this host."""


class KernelExecutionError(RuntimeError):
    """The native kernel rejected an otherwise typed evaluation request."""


class NativeOptimizerKernel:
    """Loaded exact kernel whose bytes and ABI were verified before execution."""

    def __init__(
        self,
        *,
        library: Any,
        identity: Mapping[str, Any],
        binary: Path,
    ) -> None:
        self._library = library
        self.identity = dict(identity)
        self.binary = binary
        self._create = library.strathmark_v3_optimizer_context_create
        self._create.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t)
        self._create.restype = ctypes.c_void_p
        self._free = library.strathmark_v3_optimizer_context_free
        self._free.argtypes = (ctypes.c_void_p,)
        self._free.restype = None
        self._evaluate = library.strathmark_v3_optimizer_evaluate
        self._evaluate.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int64,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._evaluate.restype = ctypes.c_int32
        self._mark_dominated = library.strathmark_v3_optimizer_mark_dominated
        self._mark_dominated.argtypes = (
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._mark_dominated.restype = ctypes.c_int32
        self._sample_three_quantiles = library.strathmark_v3_sample_three_quantiles
        self._sample_three_quantiles.argtypes = (
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
        )
        self._sample_three_quantiles.restype = ctypes.c_int32
        self._sample_linear_pool = library.strathmark_v3_sample_linear_pool_three_quantiles
        self._sample_linear_pool.argtypes = (
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._sample_linear_pool.restype = ctypes.c_int32
        self._sample_linear_pool_quantiles = library.strathmark_v3_sample_linear_pool_quantiles
        self._sample_linear_pool_quantiles.argtypes = (
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._sample_linear_pool_quantiles.restype = ctypes.c_int32
        self._sample_linear_pool_quantiles_wide = (
            library.strathmark_v3_sample_linear_pool_quantiles_wide
        )
        self._sample_linear_pool_quantiles_wide.argtypes = (
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._sample_linear_pool_quantiles_wide.restype = ctypes.c_int32
        self._generate_independent_rank_uniforms = (
            library.strathmark_v3_generate_independent_rank_uniforms
        )
        self._generate_independent_rank_uniforms.argtypes = (
            ctypes.c_uint64,
            ctypes.c_size_t,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._generate_independent_rank_uniforms.restype = ctypes.c_int32

    @classmethod
    def from_paths(cls, *, binary: Path, manifest: Path) -> NativeOptimizerKernel:
        identity = _read_and_verify_manifest(manifest)
        if not binary.is_file():
            raise KernelIntegrityError("native optimizer kernel binary is missing")
        if _file_sha256(binary) != identity["binary_sha256"]:
            raise KernelIntegrityError("native optimizer kernel binary digest mismatch")
        if sys.platform != "win32":
            raise KernelUnavailableError("bundled native optimizer kernel requires Windows x86-64")
        try:
            library = ctypes.CDLL(str(binary.resolve()))
            abi = library.strathmark_v3_optimizer_kernel_abi_version
            abi.argtypes = ()
            abi.restype = ctypes.c_uint32
            if abi() != identity["abi_version"]:
                raise KernelIntegrityError("native optimizer kernel ABI differs")
            return cls(library=library, identity=identity, binary=binary)
        except KernelIntegrityError:
            raise
        except (AttributeError, OSError) as exc:
            raise KernelIntegrityError(
                "native optimizer kernel could not load its sealed ABI"
            ) from exc

    def context(self, samples: Any) -> NativeKernelContext:
        array = np.ascontiguousarray(samples, dtype=np.int32)
        if (
            array.ndim != 2
            or array.shape[0] != self.identity["required_draw_count"]
            or not 1 <= array.shape[1] <= self.identity["maximum_entrants"]
            or np.any(array <= 0)
            or np.any(array > 2_000_000_000)
        ):
            raise KernelExecutionError(
                "native optimizer context requires the bounded sample matrix"
            )
        handle = self._create(ctypes.c_void_p(array.ctypes.data), array.shape[0], array.shape[1])
        if not handle:
            raise KernelExecutionError("native optimizer context creation failed")
        return NativeKernelContext(self, handle, array)

    def mark_dominated(
        self,
        sources: Any,
        targets: Any,
        target_dominated: Any,
        nonstrict: Any,
        strict: Any,
    ) -> None:
        source_rows = np.ascontiguousarray(sources, dtype=np.int64)
        target_rows = np.ascontiguousarray(targets, dtype=np.int64)
        nonstrict_values = np.ascontiguousarray(nonstrict, dtype=np.int64)
        strict_values = np.ascontiguousarray(strict, dtype=np.int64)
        if (
            source_rows.ndim != 2
            or source_rows.shape[1:] != (4,)
            or not len(source_rows)
            or target_rows.ndim != 2
            or target_rows.shape[1:] != (4,)
            or not len(target_rows)
            or not isinstance(target_dominated, np.ndarray)
            or target_dominated.dtype != np.bool_
            or target_dominated.shape != (len(target_rows),)
            or not target_dominated.flags.c_contiguous
            or not target_dominated.flags.writeable
            or nonstrict_values.shape != (4,)
            or strict_values.shape != (4,)
        ):
            raise KernelExecutionError(
                "native Pareto evaluation requires bounded contiguous objective rows"
            )
        status = self._mark_dominated(
            ctypes.c_void_p(source_rows.ctypes.data),
            len(source_rows),
            ctypes.c_void_p(target_rows.ctypes.data),
            len(target_rows),
            ctypes.c_void_p(target_dominated.ctypes.data),
            ctypes.c_void_p(nonstrict_values.ctypes.data),
            ctypes.c_void_p(strict_values.ctypes.data),
        )
        if status != 0:
            raise KernelExecutionError(f"native Pareto evaluation failed with status {status}")

    def sample_three_quantiles(
        self,
        probability_words_le: bytes | None,
        time_rows: tuple[tuple[int, int, int], ...],
        *,
        draw_count: int,
    ) -> tuple[tuple[int, ...], ...]:
        if (
            not isinstance(probability_words_le, bytes)
            or draw_count != self.identity["required_draw_count"]
            or len(probability_words_le) != draw_count * 16
            or not isinstance(time_rows, tuple)
            or not 1 <= len(time_rows) <= 3
            or any(
                not isinstance(row, tuple)
                or len(row) != 3
                or any(type(item) is not int for item in row)
                for row in time_rows
            )
        ):
            raise KernelExecutionError("native quantile sampling requires a bounded standard grid")
        times = np.ascontiguousarray(time_rows, dtype=np.int32)
        output = np.empty((len(time_rows), draw_count), dtype=np.int32)
        probability_buffer = ctypes.c_char_p(probability_words_le)
        status = self._sample_three_quantiles(
            ctypes.cast(probability_buffer, ctypes.c_void_p),
            draw_count,
            ctypes.c_void_p(times.ctypes.data),
            len(time_rows),
            ctypes.c_void_p(output.ctypes.data),
        )
        if status != 0 or np.any(output <= 0) or np.any(output > 2_000_000_000):
            raise KernelExecutionError(f"native quantile sampling failed with status {status}")
        return tuple(tuple(int(item) for item in row) for row in output)

    def generate_independent_rank_uniforms(
        self, *, seed: int, draw_count: int, stream_count: int
    ) -> tuple[tuple[str, ...], ...]:
        if (
            type(seed) is not int
            or not 0 <= seed <= (1 << 64) - 1
            or draw_count != self.identity["required_draw_count"]
            or type(stream_count) is not int
            or not 1 <= stream_count <= self.identity["maximum_entrants"]
        ):
            raise KernelExecutionError(
                "native rank-uniform generation requires the sealed field bound"
            )
        quotient_words = np.empty((draw_count, stream_count, 2), dtype=np.uint64)
        scales = np.empty((draw_count, stream_count), dtype=np.uint8)
        status = self._generate_independent_rank_uniforms(
            seed,
            draw_count,
            stream_count,
            ctypes.c_void_p(quotient_words.ctypes.data),
            ctypes.c_void_p(scales.ctypes.data),
        )
        if status != 0:
            raise KernelExecutionError(
                f"native rank-uniform generation failed with status {status}"
            )
        rows: list[tuple[str, ...]] = []
        for stream in range(stream_count):
            values = []
            for draw in range(draw_count):
                quotient = int(quotient_words[draw, stream, 0]) | (
                    int(quotient_words[draw, stream, 1]) << 64
                )
                scale = int(scales[draw, stream])
                if not 28 <= scale <= 47 or not 0 < quotient <= 10**scale:
                    raise KernelExecutionError(
                        "native rank-uniform output exceeds the sealed decimal bound"
                    )
                if quotient == 10**scale:
                    values.append("1")
                else:
                    values.append(("0." + str(quotient).rjust(scale, "0")).rstrip("0").rstrip("."))
            rows.append(tuple(values))
        return tuple(rows)

    def sample_linear_pool_three_quantiles(
        self,
        probability_words_le: bytes | None,
        weight_values: tuple[str, ...],
        time_rows: tuple[tuple[int, int, int], ...],
        *,
        draw_count: int,
    ) -> tuple[int, ...]:
        if (
            not isinstance(probability_words_le, bytes)
            or draw_count != self.identity["required_draw_count"]
            or len(probability_words_le) != draw_count * 16
            or not isinstance(weight_values, tuple)
            or not 2 <= len(weight_values) <= 3
            or len(time_rows) != len(weight_values)
            or any(not isinstance(value, str) for value in weight_values)
            or any(
                not isinstance(row, tuple)
                or len(row) != 3
                or any(type(item) is not int for item in row)
                for row in time_rows
            )
        ):
            raise KernelExecutionError(
                "native linear-pool sampling requires a bounded standard grid"
            )
        return self.sample_linear_pool_quantiles(
            probability_words_le,
            weight_values,
            ("0.1", "0.5", "0.9"),
            time_rows,
            draw_count=draw_count,
        )

    def sample_linear_pool_quantiles(
        self,
        probability_words_le: bytes | None,
        weight_values: tuple[str, ...],
        probability_values: tuple[str, ...],
        time_rows: tuple[tuple[int, ...], ...],
        *,
        draw_count: int,
    ) -> tuple[int, ...]:
        if (
            not isinstance(probability_words_le, bytes)
            or draw_count != self.identity["required_draw_count"]
            or len(probability_words_le) != draw_count * 16
            or not isinstance(weight_values, tuple)
            or not 2 <= len(weight_values) <= 3
            or not isinstance(probability_values, tuple)
            or not 3 <= len(probability_values) <= 16
            or len(time_rows) != len(weight_values)
            or any(not isinstance(value, str) for value in weight_values)
            or any(not isinstance(value, str) for value in probability_values)
            or any(
                not isinstance(row, tuple)
                or len(row) != len(probability_values)
                or any(type(item) is not int for item in row)
                for row in time_rows
            )
        ):
            raise KernelExecutionError(
                "native linear-pool sampling requires a bounded quantile grid"
            )
        try:
            weights = tuple(Fraction(value) for value in weight_values)
        except (ValueError, ZeroDivisionError) as exc:
            raise KernelExecutionError("native linear-pool weights are not exact decimals") from exc
        decimal_places = max(
            28,
            *(len(value.partition(".")[2].rstrip("0")) for value in weight_values),
        )
        if decimal_places > 74:
            raise KernelExecutionError(
                "native linear-pool weight precision exceeds the sealed bound"
            )
        scale = 10**decimal_places
        scaled_weights = tuple(weight * scale for weight in weights)
        if any(value.denominator != 1 or value.numerator <= 0 for value in scaled_weights):
            raise KernelExecutionError("native linear-pool weights exceed the sealed decimal scale")
        integer_weights = tuple(value.numerator for value in scaled_weights)
        if sum(integer_weights) != scale:
            raise KernelExecutionError("native linear-pool weights must sum exactly to one")
        grid_scale = 10**28
        try:
            scaled_grid = tuple(Fraction(value) * grid_scale for value in probability_values)
        except (ValueError, ZeroDivisionError) as exc:
            raise KernelExecutionError(
                "native linear-pool probabilities are not exact decimals"
            ) from exc
        if (
            any(value.denominator != 1 for value in scaled_grid)
            or tuple(value.numerator for value in scaled_grid)
            != tuple(sorted({value.numerator for value in scaled_grid}))
            or not 0 < scaled_grid[0] < scaled_grid[-1] < grid_scale
        ):
            raise KernelExecutionError("native linear-pool probabilities exceed the sealed grid")
        times = np.ascontiguousarray(time_rows, dtype=np.int32)
        output = np.empty(draw_count, dtype=np.int32)
        if decimal_places <= 37:
            packed_weights = bytearray(len(integer_weights) * 16)
            for index, value in enumerate(integer_weights):
                struct.pack_into(
                    "<QQ",
                    packed_weights,
                    index * 16,
                    value & ((1 << 64) - 1),
                    value >> 64,
                )
            packed_grid = bytearray(len(scaled_grid) * 16)
            for index, value in enumerate(scaled_grid):
                integer = value.numerator
                struct.pack_into(
                    "<QQ",
                    packed_grid,
                    index * 16,
                    integer & ((1 << 64) - 1),
                    integer >> 64,
                )
            probability_buffer = ctypes.c_char_p(probability_words_le)
            weight_buffer = ctypes.c_char_p(bytes(packed_weights))
            grid_buffer = ctypes.c_char_p(bytes(packed_grid))
            status = self._sample_linear_pool_quantiles(
                ctypes.cast(probability_buffer, ctypes.c_void_p),
                draw_count,
                ctypes.cast(weight_buffer, ctypes.c_void_p),
                len(integer_weights),
                10 ** (decimal_places - 28),
                ctypes.cast(grid_buffer, ctypes.c_void_p),
                len(scaled_grid),
                ctypes.c_void_p(times.ctypes.data),
                ctypes.c_void_p(output.ctypes.data),
            )
        else:
            packed_weights = b"".join(value.to_bytes(32, "little") for value in integer_weights)
            grid_places = max(
                len(value.partition(".")[2].rstrip("0")) for value in probability_values
            )
            if grid_places > 9:
                raise KernelExecutionError("native wide linear-pool grid exceeds the sealed scale")
            grid_denominator = 10**grid_places
            grid_values = tuple(Fraction(value) * grid_denominator for value in probability_values)
            if any(value.denominator != 1 for value in grid_values):
                raise KernelExecutionError("native wide linear-pool grid is not exact")
            grid = np.ascontiguousarray([value.numerator for value in grid_values], dtype=np.uint32)
            probability_buffer = ctypes.c_char_p(probability_words_le)
            weight_buffer = ctypes.c_char_p(packed_weights)
            status = self._sample_linear_pool_quantiles_wide(
                ctypes.cast(probability_buffer, ctypes.c_void_p),
                draw_count,
                decimal_places - 28,
                ctypes.cast(weight_buffer, ctypes.c_void_p),
                len(integer_weights),
                ctypes.c_void_p(grid.ctypes.data),
                grid_denominator,
                len(grid_values),
                ctypes.c_void_p(times.ctypes.data),
                ctypes.c_void_p(output.ctypes.data),
            )
        if status != 0 or np.any(output <= 0) or np.any(output > 2_000_000_000):
            raise KernelExecutionError(f"native linear-pool sampling failed with status {status}")
        return tuple(int(item) for item in output)


class NativeKernelContext:
    """Own one immutable native comparison cache and its Python sample lifetime."""

    def __init__(self, kernel: NativeOptimizerKernel, handle: int, samples: np.ndarray) -> None:
        self._kernel = kernel
        self._handle = handle
        self._samples = samples

    def evaluate(self, delays: Any, *, credit_scale: int) -> tuple[Any, Any]:
        if self._handle is None:
            raise KernelExecutionError("native optimizer context is closed")
        delay_rows = np.ascontiguousarray(delays, dtype=np.int32)
        if (
            delay_rows.ndim != 2
            or delay_rows.shape[0] == 0
            or delay_rows.shape[1] != self._samples.shape[1]
            or np.any(delay_rows < 0)
            or np.any(delay_rows > 180_000)
            or np.any(delay_rows % 1_000 != 0)
            or isinstance(credit_scale, bool)
            or not isinstance(credit_scale, int)
            or credit_scale <= 0
        ):
            raise KernelExecutionError(
                "native optimizer evaluation requires bounded whole-second delays"
            )
        spreads = np.empty(len(delay_rows), dtype=np.int64)
        credits = np.empty((len(delay_rows), self._samples.shape[1]), dtype=np.int64)
        status = self._kernel._evaluate(
            ctypes.c_void_p(self._handle),
            ctypes.c_void_p(delay_rows.ctypes.data),
            len(delay_rows),
            credit_scale,
            ctypes.c_void_p(spreads.ctypes.data),
            ctypes.c_void_p(credits.ctypes.data),
        )
        if status != 0 or np.any(spreads < 0):
            raise KernelExecutionError(f"native optimizer evaluation failed with status {status}")
        return spreads, credits

    def close(self) -> None:
        if self._handle is not None:
            self._kernel._free(ctypes.c_void_p(self._handle))
            self._handle = None

    def __enter__(self) -> NativeKernelContext:
        if self._handle is None:
            raise KernelExecutionError("native optimizer context is closed")
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def bundled_kernel_identity() -> dict[str, Any]:
    identity = _read_and_verify_manifest(_NATIVE_ROOT / _MANIFEST_NAME)
    source = _NATIVE_ROOT / "optimizer_kernel.rs"
    binary = _NATIVE_ROOT / _BINARY_NAME
    if _file_sha256(source) != identity["source_sha256"]:
        raise KernelIntegrityError("native optimizer kernel source digest mismatch")
    if _file_sha256(binary) != identity["binary_sha256"]:
        raise KernelIntegrityError("native optimizer kernel binary digest mismatch")
    return identity


def load_bundled_kernel(*, required: bool = False) -> NativeOptimizerKernel | None:
    binary = _NATIVE_ROOT / _BINARY_NAME
    manifest = _NATIVE_ROOT / _MANIFEST_NAME
    if not binary.is_file() or not manifest.is_file():
        if required:
            raise KernelUnavailableError("bundled native optimizer kernel is absent")
        return None
    try:
        return NativeOptimizerKernel.from_paths(binary=binary, manifest=manifest)
    except KernelUnavailableError:
        if required:
            raise
        return None


def _read_and_verify_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KernelIntegrityError("native optimizer kernel manifest is unreadable") from exc
    if (
        not isinstance(value, dict)
        or set(value) != _EXPECTED_MANIFEST_FIELDS
        or value.get("schema_version") != _SCHEMA_VERSION
        or value.get("algorithm") != _ALGORITHM
        or value.get("sampling_algorithm") != _SAMPLING_ALGORITHM
        or value.get("abi_version") != _ABI_VERSION
        or value.get("platform") != "windows-x86_64"
        or value.get("required_draw_count") != 4096
        or value.get("maximum_entrants") != 12
        or value.get("thread_limit") != 8
        or value.get("panic_strategy") != "abort"
    ):
        raise KernelIntegrityError("native optimizer kernel manifest fields differ")
    body = {key: item for key, item in value.items() if key != "manifest_digest"}
    if value.get("manifest_digest") != canonical_digest(body):
        raise KernelIntegrityError("native optimizer kernel manifest digest mismatch")
    for key in ("manifest_digest", "source_sha256", "binary_sha256"):
        digest = value.get(key)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise KernelIntegrityError("native optimizer kernel digest is invalid")
    return value


def _file_sha256(path: Path) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise KernelIntegrityError("native optimizer kernel artifact is unreadable") from exc


__all__ = [
    "KernelExecutionError",
    "KernelIntegrityError",
    "KernelUnavailableError",
    "NativeKernelContext",
    "NativeOptimizerKernel",
    "bundled_kernel_identity",
    "load_bundled_kernel",
]
