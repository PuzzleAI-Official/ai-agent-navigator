#!/usr/bin/env python
"""
Phase 10: Generalizability Benchmark Runner

Usage:
    python bench/run_benchmark.py --domain invoice_workflow
    python bench/run_benchmark.py --domain invoice_workflow --live
    python bench/run_benchmark.py --all
    python bench/run_benchmark.py --report

Modes:
    Default (no --live): Validates domain configs and prints expected
    assertions. Does NOT call any APIs. Used for CI/CD dry runs.

    --live: Runs the full pipeline against real APIs, records results
    to bench/results/{domain}_{timestamp}.json. Requires ANTHROPIC_API_KEY.

    --all: Runs all domains.

    --report: Prints a human-readable summary of the latest results.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DOMAINS_DIR = Path(__file__).parent.parent / "tests" / "generalizability" / "domains"
RESULTS_DIR = Path(__file__).parent / "results"


def load_domain(name: str) -> dict:
    path = DOMAINS_DIR / f"{name}.json"
    if not path.exists():
        print(f"ERROR: Domain config not found: {path}", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def list_domains() -> list[str]:
    return sorted(p.stem for p in DOMAINS_DIR.glob("*.json"))


def validate_domain(config: dict) -> list[str]:
    """Validate a domain config. Returns list of issues (empty = valid)."""
    issues = []
    for field in ("name", "input", "expected_scopes", "per_scope_assertions", "max_cost_usd"):
        if field not in config:
            issues.append(f"Missing required field: {field}")
    for scope, assertions in config.get("per_scope_assertions", {}).items():
        if "min_top_pass_rate" not in assertions:
            issues.append(f"Scope {scope}: missing min_top_pass_rate")
        if "min_candidates_tested" not in assertions:
            issues.append(f"Scope {scope}: missing min_candidates_tested")
    return issues


def print_domain_summary(config: dict):
    """Print a human-readable summary of a domain config."""
    print(f"\n{'='*60}")
    print(f"Domain: {config['name']}")
    print(f"{'='*60}")
    print(f"Input: {config['input'][:80]}...")
    print(f"Scopes: {', '.join(config['expected_scopes'])}")
    print(f"Cost ceiling: ${config['max_cost_usd']:.2f}")
    print(f"\nPer-scope assertions:")
    for scope, assertions in config["per_scope_assertions"].items():
        print(f"  {scope}: pass_rate >= {assertions['min_top_pass_rate']:.0%}, "
              f">= {assertions['min_candidates_tested']} candidates")
    if config.get("notes"):
        print(f"\nNotes: {config['notes']}")


def print_report():
    """Print human-readable summary of latest benchmark results."""
    if not RESULTS_DIR.exists():
        print("No results directory found. Run benchmarks first.")
        return
    results = sorted(RESULTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not results:
        print("No result files found.")
        return
    print(f"\n{'='*60}")
    print(f"Latest Benchmark Results ({len(results)} files)")
    print(f"{'='*60}")
    for path in results[:10]:
        data = json.loads(path.read_text(encoding="utf-8"))
        status = data.get("status", "unknown")
        domain = data.get("domain", path.stem)
        cost = data.get("total_cost_usd", 0)
        scopes = data.get("scopes_tested", 0)
        marker = "PASS" if status == "passed" else "FAIL"
        print(f"  [{marker}] {domain:<30} ${cost:.2f}  {scopes} scopes  {path.name}")


def main():
    parser = argparse.ArgumentParser(description="PuzzleEval Generalizability Benchmark")
    parser.add_argument("--domain", help="Run a specific domain")
    parser.add_argument("--all", action="store_true", help="Run all domains")
    parser.add_argument("--live", action="store_true", help="Run live (requires API key)")
    parser.add_argument("--report", action="store_true", help="Print latest results")
    parser.add_argument("--list", action="store_true", help="List available domains")
    args = parser.parse_args()

    if args.list:
        print("Available domains:")
        for d in list_domains():
            config = load_domain(d)
            scopes = len(config["expected_scopes"])
            print(f"  {d:<35} {scopes} scope(s)  ${config['max_cost_usd']:.0f} max")
        return

    if args.report:
        print_report()
        return

    domains = list_domains() if args.all else ([args.domain] if args.domain else [])
    if not domains:
        parser.print_help()
        return

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    for domain_name in domains:
        config = load_domain(domain_name)
        issues = validate_domain(config)
        if issues:
            print(f"ERROR: {domain_name} has config issues:", file=sys.stderr)
            for issue in issues:
                print(f"  - {issue}", file=sys.stderr)
            continue

        print_domain_summary(config)

        if args.live:
            print(f"\n[LIVE MODE] Running full pipeline for {domain_name}...")
            print("NOTE: Live mode requires ANTHROPIC_API_KEY and will incur API costs.")
            print("Live pipeline execution is not implemented in Phase 10 scaffold.")
            print(f"Use: python -m puzzleeval.cli --text '{config['input']}' --agent5 --no-interactive --pretty")
            # Future: integrate with CLI programmatically and record results
            result = {
                "domain": domain_name,
                "status": "not_implemented",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "notes": "Live mode scaffold -- run manually via CLI",
            }
        else:
            print(f"\n[DRY RUN] Validating config for {domain_name}...")
            result = {
                "domain": domain_name,
                "status": "validated",
                "config_valid": len(issues) == 0,
                "scopes_tested": len(config["expected_scopes"]),
                "max_cost_usd": config["max_cost_usd"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # Save result
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        result_path = RESULTS_DIR / f"{domain_name}_{timestamp}.json"
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"  Result saved: {result_path}")

    print(f"\nDone. {len(domains)} domain(s) processed.")


if __name__ == "__main__":
    main()
