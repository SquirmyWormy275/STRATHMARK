"""
Handicap Fairness Assessment
==============================

AI-powered (and statistical fallback) fairness assessment of Monte Carlo
simulation results, plus championship race analysis.

Public functions:
    get_ai_assessment_of_handicaps()  -- LLM fairness report for handicap events
    get_championship_race_analysis()  -- LLM sports-commentary for championship races
    simulate_and_assess_handicaps()   -- Combined Monte Carlo + display + AI wrapper

All functions return plain text strings suitable for printing.
No side effects unless show=True is passed to simulate_and_assess_handicaps().

Source references (STRATHEX):
    woodchopping/simulation/fairness.py -> get_ai_assessment_of_handicaps()
    woodchopping/simulation/fairness.py -> get_championship_race_analysis()
    woodchopping/simulation/fairness.py -> simulate_and_assess_handicaps()
    woodchopping/simulation/fairness.py -> _validate_fairness_assessment()
    woodchopping/simulation/fairness.py -> format_ai_assessment()
"""

from __future__ import annotations

import textwrap
from typing import Any, Dict, List, Optional

import numpy as np

from strathmark.config import llm_config, sim_config
from strathmark.llm import call_ollama
from strathmark.variance import run_monte_carlo_simulation
from strathmark.visualization import generate_simulation_summary, visualize_simulation_results

# JSON schema for LLM fairness assessment (Ollama structured output)
FAIRNESS_ASSESSMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "rating": {
            "type": "string",
            "enum": ["Excellent", "Very Good", "Good", "Fair", "Poor", "Unacceptable"],
        },
        "statistical_analysis": {"type": "string"},
        "pattern_diagnosis": {"type": "string"},
        "prediction_accuracy": {"type": "string"},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "rating",
        "statistical_analysis",
        "pattern_diagnosis",
        "prediction_accuracy",
        "recommendations",
    ],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_fairness_assessment(
    response: str,
    win_rate_spread: float,
    ideal_win_rate: float,
    most_favored: str,
    most_disadvantaged: str,
    win_rate_deviations: Dict[str, float],
) -> str:
    """Append warnings if the LLM response is missing required sections."""
    required_sections = [
        "FAIRNESS RATING:",
        "STATISTICAL ANALYSIS:",
        "PATTERN DIAGNOSIS:",
        "PREDICTION ACCURACY:",
        "RECOMMENDATIONS:",
    ]
    missing = [s.rstrip(":") for s in required_sections if s not in response.upper()]
    if missing:
        warning = (
            "\n\n[WARN] AI RESPONSE VALIDATION WARNING:\n"
            "The following expected sections were not found in the AI analysis:\n"
            + "\n".join(f"  - {s}" for s in missing)
            + "\n\nThis may indicate the AI response was truncated or malformed."
            "\nConsider reviewing the raw simulation statistics above for complete assessment."
        )
        return response + warning

    valid_ratings = ["EXCELLENT", "VERY GOOD", "GOOD", "FAIR", "POOR", "UNACCEPTABLE"]
    if not any(r in response.upper() for r in valid_ratings):
        if win_rate_spread < 3:
            expected = "EXCELLENT"
        elif win_rate_spread < 6:
            expected = "VERY GOOD"
        elif win_rate_spread < 10:
            expected = "GOOD"
        elif win_rate_spread < 16:
            expected = "FAIR"
        else:
            expected = "POOR"
        warning = (
            f"\n\n[WARN] RATING VALIDATION WARNING:\n"
            f"No recognized fairness rating found "
            f"(expected: {expected} based on {win_rate_spread:.1f}% spread)."
        )
        return response + warning

    return response


def _stats_dict_to_str(stats_val: Any) -> str:
    """Convert either a CompetitorTimeStats dataclass or a plain dict to a display string."""
    if stats_val is None:
        return ""
    if hasattr(stats_val, "mean"):
        # CompetitorTimeStats dataclass
        return (
            f"mean={stats_val.mean:.1f}s, std_dev={stats_val.std_dev:.2f}s, "
            f"range={stats_val.min_time:.1f}s-{stats_val.max_time:.1f}s, "
            f"consistency={stats_val.consistency_rating}"
        )
    # Plain dict (STRATHEX legacy format)
    return (
        f"mean={stats_val.get('mean', 0):.1f}s, "
        f"std_dev={stats_val.get('std_dev', 0):.2f}s, "
        f"range={stats_val.get('min', 0):.1f}s-{stats_val.get('max', 0):.1f}s, "
        f"consistency={stats_val.get('consistency_rating', 'N/A')}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _variance_warning_text(analysis: Dict[str, Any]) -> str:
    """Return a variance imbalance warning line if ratio exceeds 2.0."""
    ratio = analysis.get("variance_ratio", 1.0)
    if ratio > 2.0:
        variances = analysis.get("competitor_variances", {})
        max_name = max(variances, key=variances.get) if variances else "unknown"
        min_name = min(variances, key=variances.get) if variances else "unknown"
        return (
            f"\nVARIANCE IMBALANCE WARNING: {max_name} has {ratio:.1f}x more variance than "
            f"{min_name}. This may cause misleading fairness statistics."
        )
    return ""


def get_ai_assessment_of_handicaps(analysis: Dict[str, Any]) -> str:
    """
    Use an LLM to assess the fairness of handicap marks based on Monte Carlo results.

    If Ollama is unavailable, returns a statistical fallback assessment.

    Args:
        analysis: Dict returned by run_monte_carlo_simulation().

    Returns:
        Multi-line plain-text assessment with sections:
            FAIRNESS RATING, STATISTICAL ANALYSIS, PATTERN DIAGNOSIS,
            PREDICTION ACCURACY, RECOMMENDATIONS.
    """
    winner_pcts = analysis["winner_percentages"]
    competitors = analysis["competitors"]

    max_win_rate = max(winner_pcts.values())
    min_win_rate = min(winner_pcts.values())
    win_rate_spread = max_win_rate - min_win_rate
    ideal_win_rate = 100.0 / len(competitors)

    deviations = {name: pct - ideal_win_rate for name, pct in winner_pcts.items()}
    most_favored = max(deviations, key=deviations.get)
    most_disadvantaged = min(deviations, key=deviations.get)

    winner_data = "\n".join(
        f"  - {name}: {pct:.2f}% win rate (deviation: {deviations[name]:+.2f}%)"
        for name, pct in sorted(winner_pcts.items(), key=lambda x: x[1], reverse=True)
    )
    competitor_details = "\n".join(
        f"  - {comp['name']}: {comp['predicted_time']:.1f}s predicted + Mark {comp['mark']}"
        for comp in sorted(competitors, key=lambda x: x["predicted_time"], reverse=True)
    )

    win_rate_std_dev = float(np.std(list(winner_pcts.values())))
    cv = (win_rate_std_dev / ideal_win_rate * 100) if ideal_win_rate > 0 else 0

    # Per-competitor time statistics section
    competitor_stats_section = ""
    if analysis.get("competitor_time_stats"):
        stats_lines = []
        for name, stats in sorted(
            analysis["competitor_time_stats"].items(),
            key=lambda x: x[1].mean if hasattr(x[1], "mean") else x[1].get("mean", 0),
        ):
            stats_lines.append(f"  - {name}: {_stats_dict_to_str(stats)}")
        competitor_stats_section = (
            "\n\nPER-COMPETITOR STATISTICS:\n"
            + "\n".join(stats_lines)
            + """

CONSISTENCY RATING THRESHOLDS:
- Very High (std_dev <= 2.5s): Elite consistency, highly predictable
- High (std_dev <= 3.0s): Normal variance, matches +-3s model assumption
- Moderate (std_dev <= 3.5s): Above expected variance
- Low (std_dev > 3.5s): High variability, unpredictable outcomes

VARIANCE MODEL VALIDATION:
The system assumes +-3s absolute performance variation for all competitors.
If a competitor's std_dev significantly exceeds 3.0s, this suggests:
1. Prediction may be inaccurate (wrong baseline time)
2. Competitor has genuinely high performance variability
3. Wood quality or conditions introduce extra uncertainty

CONSISTENCY ANALYSIS REQUIRED:
In your PATTERN DIAGNOSIS section, you MUST comment on:
- Are there competitors with unusually high variance (std_dev > 3.5s)?
- Does high variance correlate with prediction confidence (LOW confidence -> high variance)?
- Are there competitors with surprisingly tight clustering (std_dev < 2.5s)?
- Does the +-3s model hold across all competitors, or are there outliers?
- Do biased competitors also show unusual variance patterns?"""
        )

    n_sims = analysis["num_simulations"]
    prompt = f"""You are a master woodchopping handicapper and statistician analyzing the fairness of predicted handicap marks through Monte Carlo simulation.

HANDICAPPING PRINCIPLES

PRIMARY GOAL: Create handicaps where ALL competitors have EQUAL probability of winning.
- In a fair handicap system, skill level should NOT predict victory
- A novice with Mark 3 should win as often as an expert with Mark 25
- The slowest competitor should have the same chance as the fastest

HANDICAPPING MECHANISM:
1. Predict each competitor's raw cutting time
2. Slowest predicted time receives Mark 3 (starts first)
3. Faster predicted times receive higher marks (delayed starts)
4. If predictions are perfect, everyone finishes simultaneously
5. Natural variation (+-3s) creates competitive spread

SIMULATION METHODOLOGY

WHAT WE TESTED:
- Simulated {n_sims:,} races with {len(competitors)} competitors
- Applied +-3 second ABSOLUTE performance variation (realistic race conditions)
- Variation represents: technique consistency, wood grain, fatigue, environmental conditions

WHY ABSOLUTE VARIANCE (+-3s for everyone):
- Real factors affect all skill levels equally in absolute seconds
- Wood grain knot costs 2s for novice AND expert (not proportional to skill)
- Technique wobble affects everyone by similar absolute time
- This is a CRITICAL breakthrough in fair handicapping

STATISTICAL SIGNIFICANCE:
- With {n_sims:,} simulations, margin of error is extremely small
- Patterns in results are REAL, not random noise
- Even 1-2% win rate differences are statistically meaningful

SIMULATION RESULTS

COMPETITOR PREDICTIONS AND MARKS:
{competitor_details}

IDEAL WIN RATE: {ideal_win_rate:.2f}% per competitor
(Perfect handicapping means all competitors win exactly {ideal_win_rate:.2f}% of races)

ACTUAL WIN RATES:
{winner_data}

STATISTICAL MEASURES:
- Win Rate Spread: {win_rate_spread:.2f}% (maximum minus minimum)
- Standard Deviation: {win_rate_std_dev:.2f}%
- Coefficient of Variation: {cv:.1f}%
{_variance_warning_text(analysis)}

FINISH TIME ANALYSIS:
- Average finish spread: {analysis["avg_spread"]:.1f} seconds
- Median finish spread: {analysis["median_spread"]:.1f} seconds
- Tight finishes (<10s): {analysis["tight_finish_prob"] * 100:.1f}% of races
- Very tight finishes (<5s): {analysis["very_tight_finish_prob"] * 100:.1f}% of races{competitor_stats_section}

FRONT AND BACK MARKER PERFORMANCE:
- Front Marker (slowest): {analysis["front_marker_name"]} - {analysis["front_marker_wins"] / n_sims * 100:.1f}% wins
- Back Marker (fastest): {analysis["back_marker_name"]} - {analysis["back_marker_wins"] / n_sims * 100:.1f}% wins

PATTERN IDENTIFICATION:
- Most Favored: {most_favored} ({winner_pcts[most_favored]:.2f}%, +{deviations[most_favored]:.2f}%)
- Most Disadvantaged: {most_disadvantaged} ({winner_pcts[most_disadvantaged]:.2f}%, {deviations[most_disadvantaged]:.2f}%)

FAIRNESS CRITERIA

RATING SCALE (based on win rate spread):
EXCELLENT (Spread <= 3%): All win rates within +-1.5% of ideal
VERY GOOD (Spread <= 6%): All win rates within +-3% of ideal
GOOD (Spread <= 10%): Acceptable for competition
FAIR (Spread <= 16%): Noticeable imbalance, adjustments recommended
POOR (Spread > 16%): Significant bias requiring recalibration
UNACCEPTABLE (Any competitor >2x or <0.5x ideal): Extreme bias

YOUR ANALYSIS TASK

Provide a comprehensive assessment in the following structure:

1. FAIRNESS RATING: State one of: Excellent / Very Good / Good / Fair / Poor / Unacceptable

2. STATISTICAL ANALYSIS (2-3 sentences):
   - Interpret the win rate spread of {win_rate_spread:.2f}%
   - Comment on finish time spreads (average {analysis["avg_spread"]:.1f}s)
   - Assess if variation is appropriate for exciting competition

3. PATTERN DIAGNOSIS (2-3 sentences):
   - Identify which diagnostic pattern (if any) is present
   - Explain WHY this pattern occurred based on competitor times
   - Reference specific competitors showing the bias

4. PREDICTION ACCURACY (1-2 sentences):
   - Are the predictions systematically biased or just slightly off?
   - Is the issue with one competitor or system-wide?

5. RECOMMENDATIONS (2-3 specific actions):
   Format as bullet points.

RESPONSE REQUIREMENTS:
- Keep total response to 8-12 sentences maximum
- Be specific and actionable
- Cite actual numbers from the data above

Your Expert Assessment:"""

    response = call_ollama(
        prompt,
        num_predict=llm_config.TOKENS_FAIRNESS_ASSESSMENT,
        format_schema=FAIRNESS_ASSESSMENT_SCHEMA,
    )

    if response:
        try:
            import json

            result = json.loads(response)
            # Format structured result into plain-text sections
            formatted = (
                f"FAIRNESS RATING: {result['rating']}\n\n"
                f"STATISTICAL ANALYSIS:\n{result['statistical_analysis']}\n\n"
                f"PATTERN DIAGNOSIS:\n{result['pattern_diagnosis']}\n\n"
                f"PREDICTION ACCURACY:\n{result['prediction_accuracy']}\n\n"
                f"RECOMMENDATIONS:\n"
            )
            for rec in result.get("recommendations", []):
                formatted += f"  - {rec}\n"

            return _validate_fairness_assessment(
                formatted,
                win_rate_spread,
                ideal_win_rate,
                most_favored,
                most_disadvantaged,
                deviations,
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            # Fall through to statistical fallback
            pass

    # Statistical fallback
    if win_rate_spread < 3:
        rating, assessment = (
            "EXCELLENT",
            "Handicaps are nearly perfect. Predictions are highly accurate with minimal bias.",
        )
    elif win_rate_spread < 6:
        rating, assessment = (
            "VERY GOOD",
            "Handicaps are working very well. Minor prediction variations are within acceptable range.",
        )
    elif win_rate_spread < 10:
        rating, assessment = (
            "GOOD",
            "Handicaps are acceptable for competition. Some prediction refinement would improve fairness.",
        )
    elif win_rate_spread < 16:
        rating, assessment = (
            "FAIR",
            "Noticeable imbalance detected. Predictions show systematic bias requiring adjustment.",
        )
    else:
        rating, assessment = (
            "POOR",
            "Significant imbalance requiring major prediction recalibration.",
        )

    front_wins = analysis["front_marker_wins"] / n_sims * 100
    back_wins = analysis["back_marker_wins"] / n_sims * 100
    if front_wins > ideal_win_rate + 3:
        pattern = "Front marker advantage detected (soft wood bias likely)."
    elif back_wins > ideal_win_rate + 3:
        pattern = "Back marker advantage detected (hard wood bias likely)."
    else:
        pattern = "No clear front/back marker bias pattern."

    rec_1 = (
        "Handicaps are ready for competition use - no adjustments needed."
        if win_rate_spread < 6
        else f"Review predictions for {most_favored} and {most_disadvantaged} - time estimates may need adjustment."
    )
    rec_2 = (
        "Continue collecting historical data to improve future predictions."
        if win_rate_spread < 10
        else "Consider adjusting quality/species factors in prediction model."
    )
    rec_3 = (
        "Monitor real competition results to validate simulation predictions."
        if win_rate_spread < 16
        else "Recalibrate baseline calculations before using these handicaps in competition."
    )

    return (
        f"FAIRNESS RATING: {rating}\n\n"
        f"STATISTICAL ANALYSIS: With {len(competitors)} competitors, ideal win rate is "
        f"{ideal_win_rate:.1f}% each. Actual spread is {win_rate_spread:.2f}% "
        f"(from {min_win_rate:.1f}% to {max_win_rate:.1f}%). {assessment} "
        f"Average finish spread of {analysis['avg_spread']:.1f}s creates exciting competition.\n\n"
        f"PATTERN DIAGNOSIS: {pattern} {most_favored} is most favored at "
        f"{winner_pcts[most_favored]:.1f}% wins (+{deviations[most_favored]:.1f}% above ideal), "
        f"while {most_disadvantaged} is disadvantaged at "
        f"{winner_pcts[most_disadvantaged]:.1f}% wins ({deviations[most_disadvantaged]:.1f}% below ideal).\n\n"
        f"PREDICTION ACCURACY: Statistical analysis only (Ollama unavailable).\n\n"
        f"RECOMMENDATIONS:\n- {rec_1}\n- {rec_2}\n- {rec_3}"
        + (_variance_warning_text(analysis) if analysis.get("variance_imbalanced") else "")
    )


def get_championship_race_analysis(
    analysis: Dict[str, Any],
    predictions: List[Dict],
) -> str:
    """
    Use an LLM to generate sports-commentary style analysis for a championship race.

    In championship format all competitors start together (no handicaps); fastest time wins.

    Args:
        analysis: Dict returned by run_monte_carlo_simulation().
        predictions: List of competitor prediction dicts, each with:
                     'name', 'predicted_time', 'method_used', 'confidence'.

    Returns:
        Multi-line plain-text race preview with sections:
            RACE FAVORITE, KEY MATCHUPS, PODIUM BATTLE, DARK HORSE,
            CONSISTENCY ANALYSIS, RACE DYNAMICS.
    """
    winner_pcts = analysis["winner_percentages"]
    avg_positions = analysis["avg_finish_positions"]
    competitor_stats = analysis.get("competitor_time_stats", {})

    favorite_name = max(winner_pcts.items(), key=lambda x: x[1])[0]
    favorite_win_rate = winner_pcts[favorite_name]

    pred_times = {p["name"]: p["predicted_time"] for p in predictions}

    matchups = []
    for i, p1 in enumerate(predictions):
        for p2 in predictions[i + 1 :]:
            diff = abs(p1["predicted_time"] - p2["predicted_time"])
            if diff <= 2.0:
                matchups.append((p1["name"], p2["name"], diff))

    dark_horses = [
        (name, pct) for name, pct in winner_pcts.items() if pct >= 10.0 and name != favorite_name
    ]

    consistency_outliers = []
    for name, stats in competitor_stats.items():
        std = stats.std_dev if hasattr(stats, "std_dev") else stats.get("std_dev", 3.0)
        if std <= 2.5:
            consistency_outliers.append((name, "very high", std))
        elif std > 3.5:
            consistency_outliers.append((name, "very low", std))

    n_sims = analysis["num_simulations"]
    prompt = f"""You are a professional woodchopping race analyst providing an engaging race preview for a championship event. All competitors start together (no handicaps) - fastest time wins.

SIMULATION RESULTS ({n_sims:,} races):

WIN PROBABILITIES:
{
        chr(10).join(
            f"- {name}: {pct:.1f}% (predicted time: {pred_times.get(name, 0):.1f}s, avg finish: {avg_positions.get(name, 0):.2f})"
            for name, pct in sorted(winner_pcts.items(), key=lambda x: x[1], reverse=True)
        )
    }

INDIVIDUAL TIME STATISTICS:
{chr(10).join(f"- {name}: {_stats_dict_to_str(stats)}" for name, stats in competitor_stats.items())}

CLOSE MATCHUPS (within 2 seconds):
{
        chr(10).join(f"- {n1} vs {n2} ({d:.1f}s difference)" for n1, n2, d in matchups)
        if matchups
        else "- No particularly close matchups"
    }

DARK HORSE CANDIDATES (>10% win rate):
{
        chr(10).join(f"- {name}: {pct:.1f}% win rate" for name, pct in dark_horses)
        if dark_horses
        else "- None identified"
    }

CONSISTENCY OUTLIERS:
{
        chr(10).join(
            f"- {name}: {rating} consistency (std_dev={std:.2f}s)"
            for name, rating, std in consistency_outliers
        )
        if consistency_outliers
        else "- All competitors show normal variance"
    }

YOUR TASK:
Provide an engaging championship race analysis in sports-commentary style with these sections:
1. RACE FAVORITE - Identify most likely winner ({favorite_name}: {favorite_win_rate:.1f}%)
2. KEY MATCHUPS - Highlight 2-3 most interesting competitive matchups
3. PODIUM BATTLE - Analyze the race for 2nd and 3rd place
4. DARK HORSE / UPSET POTENTIAL - Identify long-shot competitors with realistic upset chances
5. CONSISTENCY ANALYSIS - Comment on competitors with unusual consistency
6. RACE DYNAMICS - Overall competitive narrative and final prediction

STYLE: Engaging sports-commentary tone. Keep each section concise (2-4 sentences).

Generate the analysis now:"""

    try:
        ai_response = call_ollama(
            prompt,
            model=llm_config.DEFAULT_MODEL,
            num_predict=llm_config.TOKENS_CHAMPIONSHIP_ANALYSIS,
        )
        if ai_response:
            return ai_response.strip()
    except Exception:
        pass

    # Statistical fallback
    top_3 = ", ".join(
        f"{name} ({pct:.1f}%)"
        for name, pct in sorted(winner_pcts.items(), key=lambda x: x[1], reverse=True)[:3]
    )
    matchup_str = (
        "\n".join(
            f"- {n1} vs {n2}: Separated by only {d:.1f}s in predicted time"
            for n1, n2, d in matchups[:3]
        )
        if matchups
        else "No particularly close matchups identified."
    )
    dark_horse_str = (
        "\n".join(
            f"- {name} has upset potential with {pct:.1f}% win rate"
            for name, pct in dark_horses[:2]
        )
        if dark_horses
        else "No significant dark horse candidates."
    )

    return (
        f"RACE FAVORITE: {favorite_name}\n"
        f"{favorite_name} is the clear favorite with a {favorite_win_rate:.1f}% win probability "
        f"and predicted time of {pred_times.get(favorite_name, 0):.1f}s.\n\n"
        f"KEY MATCHUPS:\n{matchup_str}\n\n"
        f"PODIUM BATTLE:\n"
        f"Top podium contenders based on win probabilities: {top_3}\n\n"
        f"DARK HORSE:\n{dark_horse_str}\n\n"
        f"RACE DYNAMICS:\n"
        f"This race features {len(predictions)} competitors with win rates ranging from "
        f"{min(winner_pcts.values()):.1f}% to {max(winner_pcts.values()):.1f}%. "
        + (
            "The favorite is heavily favored - expect a dominant performance."
            if favorite_win_rate > 50
            else "Multiple competitors have realistic win chances - expect tight competition."
        )
        + f"\n\n(Statistical race analysis based on {n_sims:,} simulations)"
    )


def format_ai_assessment(assessment_text: str, width: int = 100) -> None:
    """
    Format and print an AI assessment with intelligent text wrapping.

    Section headers and bullet points receive special formatting.
    Blank lines between sections are maintained.

    Args:
        assessment_text: Raw assessment text.
        width: Maximum line width for wrapping.
    """
    paragraphs = assessment_text.split("\n\n")
    for paragraph in paragraphs:
        if not paragraph.strip():
            continue
        for line in paragraph.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if ":" in stripped and (stripped.isupper() or stripped.split(":")[0].isupper()):
                print(stripped)
            elif stripped.startswith(("-", "*", "\u2022", "\u25cf")):
                for wl in textwrap.wrap(
                    stripped, width=width, initial_indent="  ", subsequent_indent="    "
                ):
                    print(wl)
            else:
                for wl in textwrap.wrap(stripped, width=width):
                    print(wl)
        print()


def simulate_and_assess_handicaps(
    competitors_with_marks: List[Dict[str, Any]],
    num_simulations: Optional[int] = None,
    show: bool = True,
) -> Dict[str, Any]:
    """
    Run a complete Monte Carlo + AI assessment workflow.

    Steps:
        1. Run Monte Carlo simulation.
        2. Optionally display statistical summary and win rate bar chart.
        3. Get AI fairness assessment.
        4. Optionally print everything.

    Args:
        competitors_with_marks: List of dicts with 'name', 'mark', 'predicted_time'.
        num_simulations: Override for simulation count (defaults to sim_config value).
        show: If True, print summary, chart, and assessment to stdout.

    Returns:
        Dict with keys:
            'analysis'   -- raw simulation analysis dict
            'summary'    -- formatted simulation summary string
            'chart'      -- win rate bar chart string
            'assessment' -- AI fairness assessment string
    """
    if not competitors_with_marks or len(competitors_with_marks) < 2:
        if show:
            print("Need at least 2 competitors to run simulation.")
        return {"analysis": {}, "summary": "", "chart": "", "assessment": ""}

    if num_simulations is None:
        num_simulations = sim_config.NUM_SIMULATIONS

    analysis = run_monte_carlo_simulation(competitors_with_marks, num_simulations)
    summary = generate_simulation_summary(analysis)
    chart = visualize_simulation_results(analysis)
    assessment = get_ai_assessment_of_handicaps(analysis)

    if show:
        print(summary)
        print(chart)
        print("\n" + "=" * 70)
        print("AI HANDICAPPING ASSESSMENT")
        print("=" * 70)
        print("\nAnalyzing fairness of handicaps...")
        print("")
        format_ai_assessment(assessment, width=100)
        print("=" * 70)

    return {
        "analysis": analysis,
        "summary": summary,
        "chart": chart,
        "assessment": assessment,
    }
