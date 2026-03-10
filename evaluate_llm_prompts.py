"""
LLM Prompt Optimization Harness
=================================

Tests multiple prompt template variations for Ollama qwen2.5:7b time prediction
and identifies the best-performing template by RMSE on held-out results.

Usage:
    python evaluate_llm_prompts.py
    python evaluate_llm_prompts.py --xlsx woodchopping_clean.xlsx
    python evaluate_llm_prompts.py --model qwen2.5:7b --held-out 50

The winning template RMSE and name are saved to:
    strathmark/llm_prompt_config.json

The predictor module reads this config at runtime.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"
CONFIG_OUTPUT = Path("strathmark/llm_prompt_config.json")


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PROMPT_TEMPLATES = {
    "direct_numeric": """
You are an expert woodchopping judge. Given the following competitor and wood information, predict the competitor's time in seconds. Reply with ONLY a single number (e.g. 32.4).

Competitor: {competitor_name}
Event: {event_code}
Wood species: {species}
Block diameter: {diameter_mm}mm
Wood quality: {quality}/10
Competitor's recent average time: {avg_time:.1f}s

Predicted time in seconds:""",

    "chain_of_thought": """
You are an expert woodchopping judge. Reason step by step, then give a single number.

Competitor: {competitor_name}
Event: {event_code} (SB=Standing Block, UH=Underhand)
Species: {species}
Diameter: {diameter_mm}mm
Quality: {quality}/10 (5=average, 10=very hard)
Recent average: {avg_time:.1f}s

Step 1: Consider diameter effect (larger = slower, ~1.4 power law)
Step 2: Consider quality effect (harder = slower, +-2% per point from 5)
Step 3: Consider competitor form

Final predicted time (number only):""",

    "few_shot": """
You are an expert woodchopping judge. Predict competitor time in seconds.

Examples:
- Open SB, Pine 300mm Q5, avg 18s → 18.2s
- Open SB, Pine 275mm Q5, avg 18s → 16.1s
- Open UH, Pine 300mm Q5, avg 22s → 21.8s
- Novice SB, Pine 300mm Q5, avg 40s → 39.5s

Now predict:
Competitor: {competitor_name}
Event: {event_code}
Species: {species}
Diameter: {diameter_mm}mm
Quality: {quality}/10
Recent average: {avg_time:.1f}s

Predicted time:""",

    "structured_json": """
You are a woodchopping time prediction system. Output valid JSON only.

Input:
{{"competitor": "{competitor_name}", "event": "{event_code}", "species": "{species}", "diameter_mm": {diameter_mm}, "quality": {quality}, "avg_time": {avg_time:.1f}}}

Output the prediction as JSON:
{{"predicted_time": <number>}}""",

    "calibrated_adjustment": """
Woodchopping time predictor. The baseline prediction is {avg_time:.1f}s.

Adjust for:
- Diameter {diameter_mm}mm vs 300mm standard: {'larger, so slower' if {diameter_mm} > 300 else 'smaller, so faster'}
- Wood quality {quality}/10 vs 5 standard: {'harder, so slower' if {quality} > 5 else 'softer, so faster' if {quality} < 5 else 'average, no change'}
- Event {event_code}: {'Standing Block' if '{event_code}' == 'SB' else 'Underhand'}

Reply with ONE number (adjusted predicted time in seconds):""",
}


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str, model: str, url: str = OLLAMA_URL, timeout: int = 30) -> Optional[str]:
    """Call Ollama and return response text, or None on failure."""
    try:
        import requests
        endpoint = url.rstrip("/") + "/api/generate"
        resp = requests.post(
            endpoint,
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        _log.debug("Ollama call failed: %s", e)
        return None


def _extract_number(text: str) -> Optional[float]:
    """Extract the first valid float from LLM response text."""
    if not text:
        return None
    import re
    # Try JSON {"predicted_time": 32.4} first
    m = re.search(r'"predicted_time"\s*:\s*([\d.]+)', text)
    if m:
        return float(m.group(1))
    # Fall back to first number in response
    m = re.search(r'\b(\d{1,3}(?:\.\d{1,2})?)\b', text)
    if m:
        val = float(m.group(1))
        if 5.0 <= val <= 300.0:
            return val
    return None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_template(
    template_name: str,
    template: str,
    test_cases: List[Dict],
    model: str,
    ollama_url: str,
) -> Dict:
    """
    Run a single template against all test cases and compute RMSE.

    Args:
        template_name: Identifier for this template.
        template: Prompt template string with {placeholders}.
        test_cases: List of dicts with keys: competitor_name, event_code,
                    species, diameter_mm, quality, avg_time, actual_time.
        model: Ollama model name.
        ollama_url: Ollama server URL.

    Returns:
        Dict with keys: template_name, rmse, mae, n_successful, errors.
    """
    errors = []
    predictions = []
    actuals = []

    for i, case in enumerate(test_cases):
        # Format prompt (catch format errors for templates with python expressions)
        try:
            prompt = template.format(**case)
        except (KeyError, IndexError, ValueError) as e:
            # Template has conditional expressions -- evaluate manually
            prompt = _safe_format(template, case)
            if prompt is None:
                _log.debug("Template format error for %s case %d: %s", template_name, i, e)
                errors.append(f"format_error: {e}")
                continue

        response = _call_ollama(prompt, model, ollama_url)
        predicted = _extract_number(response) if response else None

        if predicted is None:
            errors.append(f"no_parse: response={response!r}")
            continue

        predictions.append(predicted)
        actuals.append(float(case['actual_time']))

    if len(predictions) < 3:
        _log.warning("Template '%s': only %d successful predictions.", template_name, len(predictions))
        return {
            'template_name': template_name,
            'rmse': float('inf'),
            'mae': float('inf'),
            'n_successful': len(predictions),
            'n_total': len(test_cases),
            'errors': errors[:5],
        }

    preds = np.array(predictions)
    acts = np.array(actuals)
    errs = acts - preds
    rmse = float(np.sqrt(np.mean(errs ** 2)))
    mae = float(np.mean(np.abs(errs)))

    _log.info("Template '%s': RMSE=%.3f MAE=%.3f (%d/%d successful)",
              template_name, rmse, mae, len(predictions), len(test_cases))

    return {
        'template_name': template_name,
        'rmse': round(rmse, 3),
        'mae': round(mae, 3),
        'n_successful': len(predictions),
        'n_total': len(test_cases),
        'errors': errors[:5],
    }


def _safe_format(template: str, case: Dict) -> Optional[str]:
    """Safe format that replaces python conditional expressions."""
    import re
    # Remove python conditional expressions inside f-string-like constructs
    cleaned = re.sub(r'\{[^}]*if[^}]*\}', str(case.get('diameter_mm', 300)), template)
    try:
        return cleaned.format(**case)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Test case building
# ---------------------------------------------------------------------------

def build_test_cases(results_df, held_out: int = 50) -> List[Dict]:
    """
    Build test cases from held-out results.

    For each result, the 'avg_time' is the leave-one-out mean (all other results
    for this competitor/event), simulating what the predictor would know at the
    time of prediction.
    """
    try:
        from strathmark.utils import standardize_results_columns
        df = standardize_results_columns(results_df)
    except Exception:
        df = results_df.copy()

    required = ['competitor_name', 'event', 'raw_time', 'size_mm']
    if not all(c in df.columns for c in required):
        _log.error("Results DataFrame missing required columns. Need: %s", required)
        return []

    df = df.dropna(subset=required)
    df = df[df['raw_time'] > 0]
    df['event'] = df['event'].str.upper()
    df = df[df['event'].isin(['SB', 'UH'])]

    if df.empty:
        return []

    # Compute leave-one-out mean per competitor/event
    grp = df.groupby(['competitor_name', 'event'])['raw_time']
    df['count_event'] = grp.transform('count')
    df['sum_event'] = grp.transform('sum')
    df['avg_time'] = (df['sum_event'] - df['raw_time']) / (df['count_event'] - 1).clip(lower=1)

    # Filter to competitors with at least 3 results (for meaningful avg_time)
    df = df[df['count_event'] >= 3]

    if df.empty:
        return []

    # Sample held-out set
    sample = df.sample(min(held_out, len(df)), random_state=42)

    cases = []
    for _, row in sample.iterrows():
        cases.append({
            'competitor_name': str(row.get('competitor_name', 'Unknown')),
            'event_code': str(row.get('event', 'SB')),
            'species': str(row.get('species', 'Pine')),
            'diameter_mm': int(row.get('size_mm', 300)),
            'quality': int(row.get('quality', 5) if not pd.isna(row.get('quality', 5)) else 5),
            'avg_time': float(row.get('avg_time', row.get('raw_time', 30))),
            'actual_time': float(row['raw_time']),
        })

    return cases


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Evaluate LLM prompt templates for woodchopping time prediction."
    )
    parser.add_argument('--xlsx', metavar='PATH', help="Path to Excel workbook.")
    parser.add_argument('--model', default=DEFAULT_MODEL, help="Ollama model name.")
    parser.add_argument('--ollama-url', default=OLLAMA_URL, help="Ollama server URL.")
    parser.add_argument('--held-out', type=int, default=50, help="Number of held-out test cases.")
    args = parser.parse_args()

    import pandas as pd

    # Load data
    if args.xlsx:
        try:
            from strathmark.loader import load_woodchopping_xlsx
            _, _, results_df = load_woodchopping_xlsx(args.xlsx)
        except Exception as e:
            _log.error("Failed to load Excel: %s", e)
            sys.exit(1)
    else:
        try:
            from strathmark.db import pull_results
            results_df = pull_results()
        except Exception as e:
            _log.error("Failed to load from database: %s. Use --xlsx to specify a file.", e)
            sys.exit(1)

    if results_df is None or results_df.empty:
        _log.error("No data available.")
        sys.exit(1)

    # Check Ollama connectivity
    try:
        import requests
        r = requests.get(args.ollama_url, timeout=5)
        _log.info("Ollama server reachable at %s", args.ollama_url)
    except Exception:
        _log.error("Cannot reach Ollama at %s. Is Ollama running?", args.ollama_url)
        sys.exit(1)

    # Build test cases
    test_cases = build_test_cases(results_df, args.held_out)
    if not test_cases:
        _log.error("No valid test cases could be built. Need results with >= 3 entries per competitor.")
        sys.exit(1)

    _log.info("Evaluating %d prompt templates on %d test cases...", len(PROMPT_TEMPLATES), len(test_cases))

    # Evaluate each template
    results = []
    for name, template in PROMPT_TEMPLATES.items():
        _log.info("Testing template: %s", name)
        result = evaluate_template(name, template, test_cases, args.model, args.ollama_url)
        results.append(result)

    # Sort by RMSE
    results.sort(key=lambda r: r['rmse'])

    # Report
    print("\n=== LLM Prompt Template Evaluation Results ===")
    print(f"{'Template':<25} {'RMSE':>8} {'MAE':>8} {'Success':>10}")
    print("-" * 55)
    for r in results:
        success_pct = (r['n_successful'] / r['n_total'] * 100) if r['n_total'] > 0 else 0
        print(f"{r['template_name']:<25} {r['rmse']:>8.3f} {r['mae']:>8.3f} {success_pct:>9.1f}%")

    winner = results[0]
    print(f"\nBest template: {winner['template_name']} (RMSE={winner['rmse']:.3f})")

    # Save winning template to config
    config = {
        'winning_template': winner['template_name'],
        'winning_rmse': winner['rmse'],
        'winning_mae': winner['mae'],
        'template_text': PROMPT_TEMPLATES[winner['template_name']],
        'evaluated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'model': args.model,
        'n_test_cases': len(test_cases),
        'all_results': results,
    }
    CONFIG_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_OUTPUT, 'w') as f:
        json.dump(config, f, indent=2)
    _log.info("Saved winning template config to %s", CONFIG_OUTPUT)


if __name__ == '__main__':
    import pandas as pd
    main()
