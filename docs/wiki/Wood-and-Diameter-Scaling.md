# Wood and diameter in V2

V2 compares the target and historical diameter/species. It uses six packaged timber
properties: Janka hardness, specific gravity, crush strength, shear strength,
modulus of rupture and modulus of elasticity.

Diameter enters through a bounded log ratio to the model's reference size, with
behavior learned for each event. This is not the old QAA interpolation table.

## Unknown species

Unknown or missing species use pooled property values and a missing indicator.
They are not relabeled as a known species based on a similar name. Warnings and
uncertainty can reflect unsupported conditions.

## Quality and other fields

`WoodProfile.quality` remains accepted for compatibility, but quality and moisture
are numeric no-ops in V2. The former effective-Janka quality formula and LLM quality
multiplier are retired. Exact block or batch identity is also inactive.

Adding a species/property requires a reliable one-to-one code join and temporal
validation. See [V2](Prediction-Engine-V2). V3 carries versioned event/material context
under its separate [contract](Prediction-Engine-V3).
