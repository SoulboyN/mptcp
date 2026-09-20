# Native MPTCP reference figure QA

## Figure contract

- Core conclusion: on the tested strongly heterogeneous topology, default Linux MPTCP trades static throughput and ordered-delivery latency for substantially shorter interruption under a primary-path blackhole.
- Results-level question: what behavior does native Linux MPTCP add relative to ordinary Linux TCP under the same topology?
- Archetype: quantitative grid.
- Backend: Python/matplotlib only.
- Final size: 182.9 mm wide, three equal panels.
- Statistics: paired complete runs, `n=5` per protocol and scenario; black diamonds show means and error bars show two-sided 95% t intervals; no hypothesis-test p values.
- Source data: `source_data.csv`; no observations excluded.
- Reviewer risk: static behavior is topology-specific, and native ordered-delivery latency is not commensurate with the prototype's per-subflow arrival latency.

## Panel audit

| Panel | Unique role | Replicate unit | Display | Alignment | Collision | Result |
|---|---|---|---|---|---|---|
| a | Static throughput cost | complete run | paired raw points + mean and 95% CI, log y | PASS | PASS | PASS |
| b | Static ordered-delay cost | complete run | paired raw points + mean and 95% CI, log y | PASS | PASS | PASS |
| c | Primary-path blackhole resilience | complete run | paired raw points + mean and 95% CI | PASS | PASS | PASS |

PDF text is editable; the minimum rendered glyph size is 5.25 pt. Source validation reported 21 PASS, 0 WARN and 0 FAIL. The rendered collision audit reported 0 FAIL and 0 WARN. The alignment audit reported PASS with a 1.5 pt tolerance and no exemptions.
