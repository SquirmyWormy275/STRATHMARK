---
title: LLM Cascade and Monte Carlo Tuning
status: historical-superseded
superseded_date: 2026-08-11
superseded_by: docs/PREDICTION_ENGINE_V2.md
---

# LLM Cascade and Monte Carlo Tuning — Historical

This page previously documented v0.3-era numeric LLM and cascade hardening. Those
mechanisms are superseded in STRATHMARK 2.0.0.

V3 later introduced a separately designed, validated, and audited numeric LLM council.
That is not this legacy cascade. See
[`../../PREDICTION_ENGINE_V3.md`](../../PREDICTION_ENGINE_V3.md). The bullets below are
V2-specific and remain exact while V2 retains production authority.

- LLMs cannot generate, select, or adjust numeric predictions or marks.
- The legacy `llm` compatibility key is always `None`.
- The authoritative model is the prior-only hierarchical V2 core.
- The optional residual is evidence-gated and inactive in 2.0.0.
- Mark choice uses a deterministic 2,048-sample joint optimizer.
- The public post-mark Monte Carlo API remains separate and is capped at 250,000 races.

This file remains only to preserve historical links. Use
[`../../PREDICTION_ENGINE_V2.md`](../../PREDICTION_ENGINE_V2.md) for current behavior.
