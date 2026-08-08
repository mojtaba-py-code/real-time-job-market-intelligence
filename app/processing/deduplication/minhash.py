"""MinHash signatures and locality-sensitive hashing.

Comparing every new posting against every stored posting is O(n²) and dies at
scale. MinHash reduces a document to a fixed-length signature whose Hamming
agreement estimates Jaccard similarity, and LSH banding turns "find similar
documents" into a handful of dictionary lookups.

The implementation is deterministic - the same text always produces the same
signature, in this process and the next one - and vectorised, so hashing a
document costs one NumPy expression rather than a Python loop over every
permutation.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np

#: Mersenne prime used as the permutation modulus. 2**31-1 is chosen
#: deliberately: with 31-bit token hashes, ``a * h + b`` stays below 2**63 and
#: therefore fits in a uint64 lane, which lets the whole signature be computed
#: with one vectorised NumPy expression instead of a Python loop.
_MERSENNE_PRIME = (1 << 31) - 1
_MAX_HASH = (1 << 31) - 1


def _permutations(count: int, *, seed: int = 0x5EED) -> list[tuple[int, int]]:
    """Derive ``count`` deterministic ``(a, b)`` permutation coefficients."""
    coefficients: list[tuple[int, int]] = []
    for index in range(count):
        digest = hashlib.blake2b(struct.pack(">II", seed, index), digest_size=16).digest()
        a = int.from_bytes(digest[:8], "big") % (_MERSENNE_PRIME - 1) + 1
        b = int.from_bytes(digest[8:], "big") % _MERSENNE_PRIME
        coefficients.append((a, b))
    return coefficients


def token_hash(token: str) -> int:
    """Stable 31-bit hash of a shingle."""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") & _MAX_HASH


class MinHasher:
    """Builds MinHash signatures of a fixed width."""

    def __init__(self, permutations: int = 128, *, seed: int = 0x5EED) -> None:
        if permutations <= 0:
            raise ValueError("permutations must be positive")
        self.permutations = permutations
        self._coefficients = _permutations(permutations, seed=seed)
        self._a = np.array([a for a, _ in self._coefficients], dtype=np.uint64)
        self._b = np.array([b for _, b in self._coefficients], dtype=np.uint64)

    def signature(self, tokens: Iterable[str]) -> list[int]:
        """Compute the signature of a token set.

        An empty input yields an empty signature, which the similarity function
        treats as "no information" rather than "identical".
        """
        hashes = {token_hash(token) for token in tokens if token}
        if not hashes:
            return []
        values = np.fromiter(hashes, dtype=np.uint64, count=len(hashes))
        permuted = (self._a[:, None] * values[None, :] + self._b[:, None]) % _MERSENNE_PRIME
        return [int(v) for v in permuted.min(axis=1)]


def jaccard_similarity(left: Sequence[int], right: Sequence[int]) -> float:
    """Estimate Jaccard similarity from two signatures of equal width."""
    if not left or not right or len(left) != len(right):
        return 0.0
    matches = sum(1 for a, b in zip(left, right, strict=True) if a == b)
    return matches / len(left)


def exact_jaccard(left: set[str], right: set[str]) -> float:
    """Exact Jaccard similarity, used to validate the estimator in tests."""
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union if union else 0.0


@dataclass(slots=True)
class LshIndex:
    """Banded LSH index over MinHash signatures.

    A signature is split into ``bands`` slices; two documents that agree on any
    whole band land in the same bucket and become candidates. Increasing the
    band count lowers the similarity threshold at which pairs are surfaced.
    """

    bands: int = 16
    permutations: int = 128
    rows: int = field(default=0, init=False)
    _buckets: dict[tuple[int, bytes], set[str]] = field(default_factory=dict, init=False)
    _signatures: dict[str, list[int]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.permutations % self.bands != 0:
            raise ValueError("permutations must be divisible by bands")
        self.rows = self.permutations // self.bands

    def add(self, key: str, signature: Sequence[int]) -> None:
        """Index one signature under ``key``."""
        if not signature:
            return
        self._signatures[key] = list(signature)
        for band, digest in self._band_keys(signature):
            self._buckets.setdefault((band, digest), set()).add(key)

    def candidates(self, signature: Sequence[int]) -> set[str]:
        """Keys that share at least one band with ``signature``."""
        if not signature:
            return set()
        found: set[str] = set()
        for band, digest in self._band_keys(signature):
            found.update(self._buckets.get((band, digest), ()))
        return found

    def query(
        self, signature: Sequence[int], *, threshold: float = 0.85
    ) -> list[tuple[str, float]]:
        """Return candidates above ``threshold``, most similar first."""
        scored = [
            (key, jaccard_similarity(signature, self._signatures[key]))
            for key in self.candidates(signature)
            if key in self._signatures
        ]
        return sorted(
            [(key, score) for key, score in scored if score >= threshold],
            key=lambda item: item[1],
            reverse=True,
        )

    def _band_keys(self, signature: Sequence[int]) -> list[tuple[int, bytes]]:
        keys: list[tuple[int, bytes]] = []
        width = min(len(signature), self.permutations)
        for band in range(self.bands):
            start = band * self.rows
            end = start + self.rows
            if start >= width:
                break
            chunk = signature[start:end]
            digest = hashlib.blake2b(
                b"".join(struct.pack(">I", value & _MAX_HASH) for value in chunk),
                digest_size=8,
            ).digest()
            keys.append((band, digest))
        return keys

    def __len__(self) -> int:
        return len(self._signatures)

    def clear(self) -> None:
        """Drop every indexed signature."""
        self._buckets.clear()
        self._signatures.clear()


__all__ = [
    "LshIndex",
    "MinHasher",
    "exact_jaccard",
    "jaccard_similarity",
    "token_hash",
]
