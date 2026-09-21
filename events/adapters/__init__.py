"""Vendor payload to canonical event translation (subphase E3).

Each adapter owns all knowledge of one vendor's payload shape and emits
`EngineeringEvent` envelopes. Nothing downstream - not the consumer, not
an agent - should ever need to know that Kubernetes spells it
`containerStatuses` or that Jenkins spells a failure `FAILURE`.

Adapters are pure functions over already-fetched payloads. They do no
I/O: fetching is `integrations/`' job, and keeping the two apart means an
adapter can be tested against a recorded fixture with no network, which
is exactly how the E3 tests run.

## Why adapters do not call `events.classify`

`events.classify` exists for the Phase 1 `Signal` path, where a
source-plus-signature has to be guessed into a type. An adapter has the
vendor payload itself and knows the answer outright - a Jenkins build
with `result: FAILURE` is `build.failed`, no inference needed. Routing
adapters through the classifier would mean flattening a rich payload into
a signature just to guess back what it already said.

## Idempotency

Every event carries an id derived from the vendor payload's natural key
via `events.schemas.derive_event_id`, so a redelivered webhook produces a
byte-identical event id. The key must use only fields the vendor repeats
verbatim on redelivery - see that function's docstring.
"""

from events.adapters import elk, github, jenkins, kubernetes

__all__ = ["elk", "github", "jenkins", "kubernetes"]
