"""Command line entry point: artifact-replication-check -c config.yaml"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .compare import EXIT_ERROR, check_dns, compare_all, exit_code
from .config import ConfigError, load_config
from .inventory import make_source

RESULT_WORDS = {0: "IN SYNC", 1: "DRIFT FOUND", 2: "ERRORS"}


def _short(items: list[str], limit: int = 15) -> str:
    more = f" ... (+{len(items) - limit} more)" if len(items) > limit else ""
    return ", ".join(items[:limit]) + more


def _digest(d: str | None) -> str:
    """Short form for the console (full digests are in --json)."""
    return d[:19] if d else "<missing>"


def render_text(cfg, results, dns, code: int) -> str:
    out = [
        f"Source: {cfg.source.name:<12} {cfg.source.url}",
        f"Target: {cfg.target.name:<12} {cfg.target.url}",
        f"Digests compared: {'yes' if cfg.compare_digests else 'no'}",
        "",
    ]
    width = max(len("REPOSITORY"), *(len(r.name) for r in results))
    out.append(f"{'REPOSITORY':<{width}}  SOURCE  TARGET  MISSING  EXTRA  DIGEST  STATUS")
    for r in results:
        out.append(f"{r.name:<{width}}  {r.source_tags:>6}  {r.target_tags:>6}  "
                   f"{len(r.missing):>7}  {len(r.extra):>5}  {len(r.digest_mismatch):>6}  "
                   f"{r.status}")

    details = []
    for r in results:
        if r.error:
            details.append(f"  {r.name}: ERROR {r.error}")
            continue
        if r.missing_repo:
            details.append(f"  {r.name}: repository missing on target ({len(r.missing)} tags)")
        elif r.missing:
            details.append(f"  {r.name}: missing on target: {_short(r.missing)}")
        if r.extra:
            details.append(f"  {r.name}: only on target: {_short(r.extra)}")
        for m in r.digest_mismatch:
            details.append(f"  {r.name}:{m['tag']} digest differs: "
                           f"source={_digest(m['source'])} target={_digest(m['target'])}")
    if details:
        out += ["", "Details:", *details]

    if dns:
        out.append("")
        if dns.error:
            out.append(f"DNS: ERROR {dns.error}")
        else:
            want = ", ".join(dns.expect_addresses + ([f"CNAME {dns.expect_cname}"]
                                                     if dns.expect_cname else []))
            verdict = "points to target" if dns.matches else "does NOT point to target"
            out.append(f"DNS: {dns.name} -> {dns.canonical} {', '.join(dns.addresses)} "
                       f"(expected {want}): {verdict}")

    out += ["", f"Result: {RESULT_WORDS[code]} (exit {code})"]
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="artifact-replication-check",
        description="Verify artifact replication between two sites before/after a DNS cut-over.",
        epilog="Exit codes: 0 in sync, 1 drift found, 2 errors.",
    )
    p.add_argument("-c", "--config", required=True, help="path to the YAML config")
    p.add_argument("--json", action="store_true", help="print a JSON report")
    p.add_argument("--fail-on", choices=["missing", "digest", "any"], default="any",
                   help="which drift makes the exit code 1 (default: any)")
    p.add_argument("--digests", action="store_true",
                   help="compare manifest digests of tags on both sites (overrides config)")
    p.add_argument("--phase", choices=["pre", "post"], default="pre",
                   help="post: fail if the DNS name does not point at the target (default: pre)")
    p.add_argument("--timeout", type=float, help="per-request timeout in seconds (default 10)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config, timeout=args.timeout)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.digests:
        cfg.compare_digests = True

    results = compare_all(make_source(cfg.source), make_source(cfg.target),
                          cfg.repositories, cfg.compare_digests)
    dns = check_dns(cfg.dns, cfg.source.timeout) if cfg.dns else None
    code = exit_code(results, dns, args.fail_on, args.phase)

    if args.json:
        report = {
            "source": {"name": cfg.source.name, "url": cfg.source.url},
            "target": {"name": cfg.target.name, "url": cfg.target.url},
            "compare_digests": cfg.compare_digests,
            "repositories": [asdict(r) | {"status": r.status} for r in results],
            "dns": asdict(dns) if dns else None,
            "result": RESULT_WORDS[code],
            "exit_code": code,
        }
        print(json.dumps(report, indent=2))
    else:
        print(render_text(cfg, results, dns, code))
    return code


if __name__ == "__main__":
    sys.exit(main())
