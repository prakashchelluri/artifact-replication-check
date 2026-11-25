"""Compare source and target inventories, check DNS, decide the exit code."""

from __future__ import annotations

import re
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field

from .config import DnsSpec
from .inventory import CheckError

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2


def natural_key(tag: str):
    """Sort v9 before v10 and 1.2 before 1.10."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", tag)]


@dataclass
class RepoResult:
    name: str
    source_tags: int = 0
    target_tags: int = 0
    missing_repo: bool = False  # repository does not exist on target at all
    missing: list[str] = field(default_factory=list)  # on source, not on target
    extra: list[str] = field(default_factory=list)  # on target, not on source
    digest_mismatch: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return "ERROR"
        if self.missing_repo:
            return "MISSING"
        if self.missing or self.extra or self.digest_mismatch:
            return "DRIFT"
        return "OK"


def compare_repo(source, target, repo: str, compare_digests: bool) -> RepoResult:
    result = RepoResult(repo)
    try:
        src_tags = source.tags(repo)
        if src_tags is None:
            result.error = f"repository not found on source ({source.site.name})"
            return result
        result.source_tags = len(src_tags)
        tgt_tags = target.tags(repo)
        if tgt_tags is None:
            result.missing_repo = True
            result.missing = sorted(src_tags, key=natural_key)
            return result
        result.target_tags = len(tgt_tags)
        src, tgt = set(src_tags), set(tgt_tags)
        result.missing = sorted(src - tgt, key=natural_key)
        result.extra = sorted(tgt - src, key=natural_key)
        if compare_digests:
            for tag in sorted(src & tgt, key=natural_key):
                sd, td = source.digest(repo, tag), target.digest(repo, tag)
                if sd != td:
                    result.digest_mismatch.append({"tag": tag, "source": sd, "target": td})
    except CheckError as exc:
        result.error = str(exc)
    return result


def compare_all(source, target, repos: list[str], compare_digests: bool) -> list[RepoResult]:
    # Repositories are independent, so check a few at once; each request has its own timeout.
    with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
        return list(pool.map(lambda r: compare_repo(source, target, r, compare_digests), repos))


@dataclass
class DnsResult:
    name: str
    canonical: str | None = None
    addresses: list[str] = field(default_factory=list)
    expect_addresses: list[str] = field(default_factory=list)
    expect_cname: str | None = None
    matches: bool = False
    error: str | None = None


def _norm(host: str) -> str:
    return host.lower().rstrip(".")


def check_dns(spec: DnsSpec, timeout: float) -> DnsResult:
    """Resolve spec.name and say whether it points at the target site."""
    result = DnsResult(spec.name, expect_addresses=spec.expect_addresses,
                       expect_cname=spec.expect_cname)
    # The resolver call itself has no timeout, so run it in a worker and stop waiting.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        canonical, _aliases, addrs = pool.submit(socket.gethostbyname_ex, spec.name).result(
            timeout=timeout)
    except FutureTimeout:
        result.error = f"DNS lookup of {spec.name} timed out after {timeout:g}s"
        return result
    except OSError as exc:
        result.error = f"DNS lookup of {spec.name} failed: {exc}"
        return result
    finally:
        pool.shutdown(wait=False)

    result.canonical, result.addresses = canonical, sorted(addrs)
    ok = True
    if spec.expect_addresses:  # every answer must be one of the target's addresses
        ok = bool(addrs) and set(addrs) <= set(spec.expect_addresses)
    if spec.expect_cname:
        ok = ok and _norm(canonical) == _norm(spec.expect_cname)
    result.matches = ok
    return result


def exit_code(results: list[RepoResult], dns: DnsResult | None, fail_on: str,
              phase: str) -> int:
    if any(r.error for r in results) or (dns and dns.error):
        return EXIT_ERROR
    missing = any(r.missing for r in results)
    digest = any(r.digest_mismatch for r in results)
    extra = any(r.extra for r in results)
    # After cut-over the name must point at the target; before, it is informational.
    dns_drift = phase == "post" and dns is not None and not dns.matches
    drift = {
        "missing": missing,
        "digest": digest,
        "any": missing or digest or extra,
    }[fail_on]
    return EXIT_DRIFT if drift or dns_drift else EXIT_OK
