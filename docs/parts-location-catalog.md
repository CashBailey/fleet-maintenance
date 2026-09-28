# Parts physical-location catalog

For the current single-yard operation, use the existing two-level inventory model as a library address:

- **Warehouse** = building, storage area, room, or parts cage.
- **Bin code** = the full physical address below that point, for example `CAB-02/SHELF-03/DRAWER-01/BIN-04`.
- **Bin name** = a plain-language label, for example `Cage A, cabinet 2, shelf 3, drawer 1`.

The part search, stock-on-hand, reservation, receipt, issue, return, and immutable history all resolve through that bin address. A part is never assigned a hand-entered balance or untracked location.

Before loading the on-hand inventory, label every physical storage point with its bin code and perform a controlled count. Add deeper first-class location levels only if operations need independent permissions, transfers, or counts at those intermediate levels.
