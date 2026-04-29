# PuzzleEval Generalizability Benchmark

Phase 10 regression gate -- validates that the pipeline works across
multiple AI domains, not just the OCR happy path.

## Quick Start

```bash
# List available domains
python bench/run_benchmark.py --list

# Dry run (validate configs, no API calls)
python bench/run_benchmark.py --all

# Run specific domain live (requires ANTHROPIC_API_KEY)
python bench/run_benchmark.py --domain invoice_workflow --live

# View latest results
python bench/run_benchmark.py --report
```

## Domain Coverage

| Domain | Scopes | What it exercises |
|--------|--------|-------------------|
| invoice_workflow | 3 | OCR + extract + sync; SPA provider (Phase 1.5) |
| chatbot_simple | 1 | Single-scope regression baseline |
| classification_pipeline | 2 | Classify + route pattern |
| translation_chain | 2 | Translate + format pattern |
| summarization | 1 | Single-scope summarization |
| multi_step_data_extraction | 4 | Longest DAG (scrape->extract->classify->sync) |
| rejection_transparency | 3 | Drop-on-reject with niche providers |
| user_added_candidate | 2 | Phase 6 user-added provider flow |
| long_chain_parallel | 5 | Fan-out/fan-in DAG topology |
| scope_under_capacity | 2 | Scope with 0 tested candidates |

## Running Tests

```bash
# Standard test suite (skips generalizability)
ANTHROPIC_API_KEY=dummy pytest tests/

# Generalizability tests only
ANTHROPIC_API_KEY=dummy pytest -m generalizability tests/generalizability/ -v
```

## Results

Results are saved to `bench/results/` as JSON files with timestamps.
The `--report` flag shows a human-readable summary.
