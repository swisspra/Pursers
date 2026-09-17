# Personal error-state selector contract

This contract gives browser observers stable access to the two Personal error states that use the sanitized `feed_error` field. The attributes locate product state without depending on CSS classes, element order, colour, or verifier-authored expected values. The visible and accessible error text is the exact product-provided `feed_error` value.

## Bindings

| Catalogue row | Product element | Attribute binding | Accessible value |
|---|---|---|---|
| `dashboard-ui.state.permission-denied` | Connection banner | `[data-pursers-panel="connection"][data-pursers-state="error"][data-pursers-connection="error"]` | `#connection-detail` is inside a keyboard-focusable `role="status"` live region and contains `feed_error` character for character. |
| `personal.activity-error` | Activity feed panel and error notice | `[data-pursers-panel="activity-feed"][data-pursers-source="board-event-feed"][data-pursers-state="error"] [role="alert"][data-pursers-state="error"]` | The keyboard-focusable alert contains `feed_error` character for character. |

`data-pursers-state` is `loading` before the first response and changes to `error`, `ready`, or `empty` from product state. `data-pursers-source="board-event-feed"` names the live read tool; it is not a fixture label. Observers must read the rendered error value instead of supplying expected text to the page.

These bindings apply to the existing `ui://pursers/dashboard` MCP App frame. They do not define a second Personal route or a second runtime-attestation path.
