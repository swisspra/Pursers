# Pursers brand assets

These files are normalized, byte-identical copies of operator-supplied Pursers
artwork. The artwork is opaque white and must not be recolored, redrawn, cropped,
or stretched. Keep an intentional white frame when it appears on a dark surface.

| Tracked file | Intrinsic size | SHA-256 | Use |
| --- | ---: | --- | --- |
| `pursers-fleet-hero.png` | 1774 × 887 | `f483e98f077391e51b6ced4004ac15657fe1b77be4174b7b6929993843df9446` | GitHub README hero |
| `pursers-wordmark-square.png` | 1254 × 1254 | `c4d79b9d3489769d30334d4907b9618cec02c64d9bdcb37071de2fd517ae3a17` | Square placements that have enough room for the full wordmark; not a favicon |
| `../../../tools/fleet-dashboard/ui/assets/brand/pursers-wordmark.png` | 1774 × 887 | `6a1b7ee76b9d91ea431d8ef63b9c8ba4b9ae5f5c822e67bc24745692b29805d5` | Compact Fleet Dashboard navigation |

## Updating the artwork

1. Obtain the authorized original PNGs and their declared SHA-256 values.
2. Verify each original's byte hash and intrinsic dimensions before copying it.
3. Copy the original bytes to the normalized path above. Do not run an image
   optimizer or editor; a brand refresh should preserve supplied bytes exactly.
4. Update this table only when the authorized source changes. Keep the README hero
   on the fleet illustration and the compact dashboard on the horizontal wordmark.
5. Run the Fleet dashboard focused tests, capture desktop and mobile views in both
   themes, run the repository leak scan and `git diff --check`, then regenerate and
   verify the integration manifest using the supported repository tools.
