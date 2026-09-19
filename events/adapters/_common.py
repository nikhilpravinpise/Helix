"""Shared event construction for the adapters (subphase E3).

Exists for one reason: every adapter copies vendor free text into
`metadata` - a build console log, a pod's waiting message, a reviewer's
comment - and any of those can carry a credential. Subphase 1.3 made
redaction the evidence layer's job precisely so secrets never reach agent
context, and the event stream is a second path to that same context. An
adapter that builds `EngineeringEvent` directly would bypass it.

So adapters do not construct events. They call `vendor_event`, which
cannot forget.

`integrations.redaction` is reused rather than reimplemented: it is a pure
leaf module (it imports only `re` and `typing`), so depending on it
introduces no cycle, and a second redactor would be a second thing to
keep correct.
"""

from typing import Any

from events.schemas import EngineeringEvent
from integrations.redaction import redact


def vendor_event(**fields: Any) -> EngineeringEvent:
    """Build a canonical event from a vendor payload, metadata redacted.

    Redaction is applied to `metadata` only. The envelope fields are
    Helix's own vocabulary - a service name, an environment, an event type
    - and none of them is a place a credential can arrive.
    """
    fields["metadata"] = redact(fields.get("metadata") or {})
    return EngineeringEvent(**fields)
