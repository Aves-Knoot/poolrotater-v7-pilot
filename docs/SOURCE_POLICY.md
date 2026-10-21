# Source and credential policy

The runtime is the separately pinned immutable payload, with exact dependency,
source, protocol and storage inventories. Installer source and ordinary checkout
code grant no live-provider authority. Runtime code must match its protected
registration before authority is read. No production state, OAuth/Discord keys,
private enrollment invitation, signer key or research database belongs here.
Unshipped synthetic fixtures remain outside the installed payload.
