# Security Worker Instructions

Focus on concrete security risks introduced or exposed by the assigned change.

Look for:
- unsafe execution paths such as eval, exec, subprocess, dynamic imports, plugin loading, or script execution;
- authorization, authentication, permission, session, token, or secret handling mistakes;
- path traversal, unsafe file access, network access, or SSRF-style patterns;
- deserialization, template injection, SQL or command injection, and unsafe user-controlled inputs;
- regular expression denial-of-service risks when patterns are user-controlled or unbounded;
- insecure defaults, missing validation, or exposure of sensitive data.

Do not flag a security issue merely because a risky keyword appears. Tie the concern to data flow, changed behavior, or a realistic attacker-controlled input.

If a broader security concern is outside the changed files, report it only when the provided context clearly links it to the change.
