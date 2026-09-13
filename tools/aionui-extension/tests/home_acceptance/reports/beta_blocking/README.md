# Beta-blocking recipe dry-run reports

All eight files are worker-produced `NOT final` observations. The fixture
server started successfully and unit validation passed, but the browser was
blocked before localhost navigation: the ego-browser bootstrap was unavailable
inside the agent sandbox, then the MCP browser permission was declined and its
policy explicitly prohibited alternate browser/CDP workarounds.

No recipe was represented as passing. Candidate-change classification remains
undetermined for rows 14, 18, 20, 40, 70, 95, 97, and 133. Any recipe later
shown to need a candidate change must be routed to `TK-4981bc2da3ac`; this
fixture ticket does not modify the candidate.
