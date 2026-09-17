# Pursers client

`pursers-client` is the asynchronous Python client for a Pursers Central MCP
service. It provides `BoardClient` for authenticated board, ticket, memory,
state, and event operations used by Pursers runtimes and automation tools.

Applications supply the Central URL, bearer credential, board ID, and seat
identity. The client does not start Central, create credentials, or manage
operator configuration.
