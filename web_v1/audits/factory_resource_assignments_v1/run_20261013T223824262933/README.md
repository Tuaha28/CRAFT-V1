# Audit provenance and interpretation

This run read six immutable source/readiness JSON captures and their pinned manifest. It did not connect to MySQL and did not import or call the scheduler. Inputs were hash-checked before and after analysis. Outputs are new audit artifacts; no source capture, Django record, schedule, or frozen benchmark was modified.

Historical execution means captured actual-work segments clipped at the snapshot reference timestamp. These captures are generated TEST data under `frozen-plan-then-actual-v1`, not verified operational history. Calendar checks verify conformance to the seeder model only. Single employee/machine per lot records are observed schema rows, not authoritative crew rules. The seeded route model checks matching lot-index predecessor completion; CRAFT whole-operation release is stricter. Cross-line allocations are allowed by this seeder’s factory-wide skilled-worker selection, but the factory policy is unknown.

No crew-size field or factory-approved staffing rule was found in the captured source. The 200-operation cycle / 25-operation CP-SAT-window target is not established safe by this audit: all unresolved real partials remain blocked, crew requirements are unknown, and factory policies for lot transfer, calendars, and line mobility require authoritative confirmation.
