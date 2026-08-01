# When IAC Bus Is Needed

Use IAC Bus when multiple agents or tools need lightweight coordination without
sharing a process, database connection, or local filesystem.

Good fits:

- progress and blocker reporting across autonomous agents
- supervisor-to-worker task handoff
- queue-style work leasing with `claim` / `ack` / `nack`
- simple channel polling from agents that only have outbound HTTP
- live deployment smoke checks where a durable API is more useful than local logs

Avoid IAC Bus when:

- a single process can call a function directly
- the data is secret or user-private and does not need to leave the agent runtime
- strict durability, ordering, replay, or access control is required
- a workflow needs high-volume streaming or low-latency pub/sub semantics

Minimum viable use is one channel, a stable `sender`, and a cursor stored from
the last seen message ID. Add queues only when workers need exclusive leases.
