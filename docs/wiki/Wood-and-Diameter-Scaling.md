# Wood and Diameter in V2

> **V2-specific behavior.** V3 is implemented with versioned event, diameter,
> species/material, property, taxonomy, and conversion evidence. Its checked-in evidence
> is rehearsal-tier; V2 remains production authority until explicit cutover.

V2 uses target/historical diameter and species joined to six physical properties:
Janka hardness, specific gravity, crush strength, shear strength, modulus of rupture,
and modulus of elasticity. Diameter enters as a clamped log ratio to the model reference
and is learned with event-specific behavior.

Unknown or missing species do not inherit a fabricated known-species label. They use
pooled property values plus an explicit missing indicator, and uncertainty/warnings can
reflect unsupported conditions.

`WoodProfile.quality` and historical `quality` remain required/accepted by compatible
data structures, but quality and moisture are numeric no-ops in V2. The old effective-
Janka quality formula and LLM quality multiplier are superseded. Exact log/block/batch
identity is also inactive until tournament software captures it with provenance.

Adding a species or property requires a one-to-one code join and temporal validation;
free-text similarity is not sufficient model evidence.
