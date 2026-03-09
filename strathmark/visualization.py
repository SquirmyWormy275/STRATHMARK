"""
Simulation Result Visualization
================================

Text-based visualization of Monte Carlo simulation results.
No dependencies beyond the Python standard library.

Output is plain text, 70 characters wide, no ANSI codes, no emojis.

Source references (STRATHEX):
    woodchopping/simulation/visualization.py -> generate_simulation_summary()
    woodchopping/simulation/visualization.py -> visualize_simulation_results()
"""

from typing import Dict, Any


def generate_simulation_summary(analysis: Dict[str, Any]) -> str:
    """
    Generate a comprehensive text summary of Monte Carlo simulation results.

    Args:
        analysis: Dict returned by run_monte_carlo_simulation().

    Returns:
        Formatted multi-line string suitable for printing. No side effects.
    """
    lines = []
    lines.append("\n" + "=" * 70)
    lines.append("MONTE CARLO SIMULATION RESULTS")
    lines.append("=" * 70)
    lines.append(
        f"Simulated {analysis['num_simulations']:,} races "
        f"with +/- 3s absolute performance variation"
    )
    if analysis.get('heat_variance_seconds') is not None:
        lines.append(
            f"Heat variance: +/-{analysis['heat_variance_seconds']:.1f}s shared effect"
        )
    lines.append("")

    lines.append("FINISH TIME SPREADS:")
    lines.append(f"  Average spread: {analysis['avg_spread']:.1f} seconds")
    lines.append(f"  Median spread:  {analysis['median_spread']:.1f} seconds")
    lines.append(f"  Range: {analysis['min_spread']:.1f}s - {analysis['max_spread']:.1f}s")
    lines.append(
        f"  Tight finish (<10s): {analysis['tight_finish_prob'] * 100:.1f}% of races"
    )
    lines.append(
        f"  Very tight (<5s):    {analysis['very_tight_finish_prob'] * 100:.1f}% of races"
    )
    lines.append("")

    lines.append("WIN PROBABILITIES:")
    sorted_winners = sorted(
        analysis['winner_percentages'].items(), key=lambda x: x[1], reverse=True
    )
    for name, pct in sorted_winners:
        lines.append(
            f"  {name:25s} {pct:5.1f}% "
            f"({analysis['winner_counts'][name]:,} wins)"
        )
    lines.append("")

    lines.append("AVERAGE FINISH POSITIONS:")
    sorted_positions = sorted(
        analysis['avg_finish_positions'].items(), key=lambda x: x[1]
    )
    for name, avg_pos in sorted_positions:
        lines.append(f"  {name:25s} Avg position: {avg_pos:.2f}")
    lines.append("")

    lines.append("FRONT/BACK MARKER ANALYSIS:")
    front = analysis['front_marker_name']
    back = analysis['back_marker_name']
    n = analysis['num_simulations']
    lines.append(f"  Front marker (slowest): {front}")
    lines.append(f"    Win rate: {analysis['front_marker_wins'] / n * 100:.1f}%")
    lines.append(f"  Back marker (fastest): {back}")
    lines.append(f"    Win rate: {analysis['back_marker_wins'] / n * 100:.1f}%")

    # Optional podium margin stats
    if analysis.get('avg_podium_margin_12') is not None:
        lines.append("")
        lines.append("PODIUM MARGINS:")
        lines.append(f"  1st - 2nd avg gap: {analysis['avg_podium_margin_12']:.2f}s")
        if analysis.get('avg_podium_margin_23') is not None:
            lines.append(
                f"  2nd - 3rd avg gap: {analysis['avg_podium_margin_23']:.2f}s"
            )
        if analysis.get('photo_finish_pct') is not None:
            threshold = analysis.get('photo_finish_threshold', 0.25)
            lines.append(
                f"  Photo finish (<{threshold:.2f}s): {analysis['photo_finish_pct']:.1f}%"
            )

    # Optional most common finish order
    if analysis.get('most_common_order') is not None:
        lines.append("")
        scope = analysis.get('most_common_order_scope', 'full')
        pct = analysis.get('most_common_order_pct', 0.0)
        order_str = " -> ".join(analysis['most_common_order'])
        lines.append(f"MOST COMMON FINISH ORDER ({scope}):")
        lines.append(f"  {order_str}")
        lines.append(f"  Occurred in {pct:.1f}% of races")

    lines.append("=" * 70)
    return "\n".join(lines)


def visualize_simulation_results(analysis: Dict[str, Any]) -> str:
    """
    Generate a text-based bar chart of win rate distribution.

    Returns a formatted string (no side effects). Bars are scaled relative
    to the highest win rate, fitting within 40 characters.

    Args:
        analysis: Dict returned by run_monte_carlo_simulation().

    Returns:
        Formatted multi-line string with ASCII bar chart. No side effects.
    """
    lines = []
    lines.append("\n" + "=" * 70)
    lines.append("WIN RATE VISUALIZATION")
    lines.append("=" * 70)

    winner_pcts = analysis['winner_percentages']
    if not winner_pcts:
        lines.append("  (no data)")
        lines.append("=" * 70)
        return "\n".join(lines)

    max_pct = max(winner_pcts.values())
    sorted_winners = sorted(winner_pcts.items(), key=lambda x: x[1], reverse=True)

    for name, pct in sorted_winners:
        bar_length = int((pct / max_pct) * 40) if max_pct > 0 else 0
        bar = "\u2588" * bar_length
        lines.append(f"{name:25s} {pct:5.1f}% {bar}")

    lines.append("=" * 70)
    return "\n".join(lines)
