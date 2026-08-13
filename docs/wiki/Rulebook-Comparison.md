# Rulebook and Model Boundaries

STRATHMARK enforces the repository's AAA-compatible calculation invariants: a 3-second
minimum mark, a system ceiling of 183 seconds (with lower event ceilings allowed),
integer marks, and a deterministic bounded fallback.

Rulebooks define competition authority and procedures. Prediction Engine V2 is a
statistical aid for the handicapper; it does not replace show officials or add rules for
draws, divisions, eligibility, penalties, equipment, or result validity.

## What is model policy, not a rulebook rule

- strict exclusive-date evidence and partial pooling;
- chronological conformal intervals;
- the 2,048-sample equal-win-probability mark objective;
- manual override provenance;
- the closed active-feature allowlist;
- the decision to make unverified context numeric no-ops.

Division, round/heat, venue, lane/stand, run order, exact material identity,
quality/moisture, weather, equipment, fatigue, and penalty/DNF status are not active V2
factors. A tournament application may still use them for administration; STRATHMARK
does not claim the current model has learned their effects.

The handicapper can always supply a manual time. It is clearly labeled, has no model
interval, and does not become training evidence. The model's default mark sheet is
model-implied equalization, not a guarantee of equal actual finishes.

Consult the governing association's current official rulebook for event operation. This
page intentionally avoids reproducing potentially changing rule text.
