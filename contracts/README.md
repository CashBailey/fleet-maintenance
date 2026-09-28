# Integration contracts

`gatorhub-fleetline-v1.json` is Fleetline's canonical vocabulary for its GatorHub
boundary. Fleetline is the runtime authority for this API and webhook contract. When the
GatorHub adapter is implemented, it should vendor this exact manifest and fail CI if its
copy or generated constants drift.

Use semantic versions. A breaking field, identifier, status, authorization, endpoint,
or event change requires a new major-version file and a compatibility window. Additive
optional vocabulary increments the minor version; clarifications that do not change the
wire behavior increment the patch version. Do not edit v1 in a way that changes the
meaning of an accepted v1 payload.

Fleetline's `core.test_gatorhub_contract` test checks the manifest against its real URL
names, status enum, integration role permissions, and webhook vocabulary. Cross-system
acceptance remains the responsibility of the real-stack E2E plan in
[`docs/gatorhub-integration.md`](../docs/gatorhub-integration.md).
