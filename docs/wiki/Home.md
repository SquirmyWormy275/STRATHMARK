# STRATHMARK Wiki

STRATHMARK 2.0.0 is an offline-capable woodchopping prediction and handicap-mark
engine. It uses strictly prior evidence, returns calibrated positive finish-time
distributions, and assigns marks jointly for a field.

Start with the mandatory [Woodchopping Handicap Foundations](Handicap-Mark-Math) domain
guide. Then read [Prediction Engine V2](Prediction-Engine-V2) and follow the [Quick
Start](Quick-Start) or [REST API](REST-API).

## Stable rules

- Mark floor 3; system ceiling 183, with lower event ceilings allowed.
- One exclusive UTC cutoff and one immutable model bundle per request.
- Active factors: stable identity/history, event, prior dates, diameter, species
  physical properties, and gender including missing.
- Unverified tournament, venue, material, condition, and status factors are numeric
  no-ops.
- Forecast interval and race-performance `std_dev` are separate quantities.
- Numeric LLM prediction is retired; LLMs are narrative-only.
- The deterministic joint optimizer uses 2,048 samples and a rounded-gap fallback.
- Public prediction routes are stateless; trusted logging is explicit and authenticated.

## Release evidence

The frozen 128-row temporal benchmark recorded V2 core MAE 16.1301 seconds versus
20.5172, RMSE 33.6904 versus 44.4791, and 94.53% coverage for the nominal 90%
interval. These results are specific to the checked-in workbook and split and do not
prove universal accuracy or actual equal finishes.

## Where it fits

STRATHMARK is a calculation library, not tournament-management software. STRATHEX and
other scoring applications call it so prediction and mark logic live in one versioned
place. Future tournament software may collect currently unavailable factors, but those
fields remain inactive until a later model validates them.
