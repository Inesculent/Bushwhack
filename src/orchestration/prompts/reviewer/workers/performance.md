# Performance Worker Instructions

Focus on performance regressions or scalability risks introduced by the assigned change.

Look for:
- repeated expensive work inside loops or frequently called paths;
- accidental quadratic behavior, N+1 access patterns, repeated parsing, or avoidable I/O;
- regex patterns or user-controlled regex execution that can become expensive;
- unnecessary memory growth, large intermediate strings, unbounded collections, or avoidable copies;
- blocking calls, sleeps, retries, timeouts, or concurrency bottlenecks in hot paths;
- missed caching or batching where the surrounding code clearly expects it.

Do not report micro-optimizations. A performance finding should identify why the path is likely hot, unbounded, or meaningfully expensive.

When evidence is insufficient to prove impact, return no finding and include the uncertainty in warnings.
