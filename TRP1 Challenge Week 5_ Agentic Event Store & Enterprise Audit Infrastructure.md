# TRP1 WEEK 5: The Ledger

## *Agentic Event Store & Enterprise Audit Infrastructure.*  *Building the immutable memory and governance backbone for multi-agent AI systems at production scale.*

## ***If 2025 was the year of the agent, 2026 is the year multi-agent systems move into production. The shift depends on one thing: infrastructure that can be trusted. An event store is not optional infrastructure for production AI. It is the foundation.***

**Builds on:**  
**Week 1** Governance Hooks & Intent Traceability     
**Week 2** Automaton Auditor     
**Week 4** Brownfield Cartographer

# **Why This Project**

Every system you have built in this program has a memory problem. The Cartographer's lineage graph is rebuilt from scratch on each run. The Automaton Auditor's judgements are lost when the process ends. Week 1's governance hooks produce an intent log that no other system reads. These are not bugs — they are the natural limitations of systems that have no shared, persistent, append-only memory.

The Ledger fixes this permanently. It is the event store that all other systems in this program should have been writing to from Week 1\. By the end of this week, you will have a production-quality event sourcing infrastructure that: makes agent decisions auditable and reproducible, enables temporal queries for compliance and debugging, provides the append-only ledger that prevents the ephemeral memory failure mode described in the Gas Town pattern, and exposes everything as a typed, queryable API that downstream systems can consume.

The business case is precise. In 2026, the number-one reason enterprise AI deployments fail to reach production is not model quality — it is governance and auditability. Regulators, auditors, and enterprise risk teams require an immutable record of every AI decision and the data that informed it. The Ledger is that record. An FDE who can deploy it in the first week of a client engagement immediately unblocks the governance conversation that is otherwise the last thing to get resolved.

## **The Compounding Connection**

This project is the retroactive foundation for the entire program. When you build the Ledger, you are building the infrastructure that all prior projects should have been using. Your Week 2 audit verdicts become events in a GovernanceJudgement stream. The Ledger does not add a new system — it connects the ones you already have.

# **New Skills This Week**

## **Technical Skills**

* **Event store schema design:** Append-only tables, stream partitioning, hot/cold storage, PostgreSQL LISTEN/NOTIFY for real-time subscriptions  
* **CQRS implementation:** Separating command handlers from query handlers, projection management, eventual consistency patterns  
* **Aggregate design:** Consistency boundaries, business rule enforcement in domain logic, state machine patterns  
* **Optimistic concurrency control:** Version-based conflict detection, retry strategies, conflict resolution patterns  
* **Async projection daemon:** Checkpoint management, fault-tolerant background processing, projection lag measurement  
* **Upcasting & schema evolution:** Version migration chains, inference vs. null strategies, immutability guarantees  
* **Cryptographic audit chains:** Hash chain construction, tamper detection, regulatory package generation  
* **MCP command/resource architecture:** Tool design for LLM consumers, structured error types, precondition documentation

## **FDE Skills**

* **The governance conversation:** Ability to translate "we need auditability" from a risk/compliance stakeholder into a specific event store deployment recommendation within 48 hours  
* **Enterprise stack translation:** Mapping your PostgreSQL implementation to Marten/Wolverine (.NET) and EventStoreDB for clients who already have a stack preference  
* **The one-way door conversation:** Knowing how to communicate the migration complexity and long-term commitment of adopting event sourcing, so clients make the decision with accurate information  
* **SLO-based architecture:** Designing systems to explicit performance contracts rather than "as fast as possible" — the foundation of production-grade FDE work

# **The Week Standard**

By end of this week, you must be able to demonstrate: "Show me the complete decision history of application ID X" — from first event to final decision, with every AI agent action, every compliance check, every human review, all causal links intact, temporal query to any point in the lifecycle, and cryptographic integrity verification. If you cannot run this demonstration in under 60 seconds, the week is not complete.

# **Reading Material**

* [An empirical characterization of event sourced systems and their schema evolution — Lessons from industry](https://www.sciencedirect.com/science/article/pii/S0164121221000674) 

# **![][image1]**

# **Phase 0 — Domain Reconnaissance (Day 1, Morning)**

Event sourcing is one of the most misunderstood patterns in enterprise software. Most engineers who say they have used it have used a version of it — usually without optimistic concurrency control, without projection management, without upcasting, and without understanding why any of those things matter. Phase 0 establishes the conceptual precision required to build the Ledger correctly.

## **Core Concepts — Required Mastery**

**Event Sourcing vs. Event-Driven Architecture**

*These are not the same thing. Event-Driven Architecture (EDA) uses events as messages between services — the sender fires and forgets. Event Sourcing uses events as the system's source of truth — the events ARE the database. Your system today (agent activity tracing component callbacks, the Automaton Auditor's verdict stream) is EDA. The Ledger is event sourcing. The distinction matters because EDA events can be dropped or lost; event store entries never can. Study: Confluent's "Future of AI Agents is Event-Driven" (2025) and contrast with Greg Young's "CQRS and Event Sourcing" talks.*

**Aggregate Boundaries & Domain Events**

*An aggregate is a consistency boundary — a cluster of domain objects that must be mutated atomically. An event is the record of a fact that happened to an aggregate. The critical rule: aggregates communicate only through events, never through direct method calls. In the AI era, each AI agent is a natural aggregate boundary: its decisions are facts, recorded as events, never mutated. Study: Vernon's "Implementing Domain-Driven Design", Chapter 10 on aggregates.*

**CQRS — Command Query Responsibility Segregation**

*Write operations (Commands) and read operations (Queries) are handled by separate models. Commands append events to streams. Queries read from projections built from those events. The separation enables: independent scaling of reads and writes, multiple read-optimised projections from the same event stream, and the ability to rebuild any read model by replaying events. In the MCP context: MCP Tools are Commands; MCP Resources are Queries against projections.*

**Optimistic Concurrency Control**

*In an event store, two processes can simultaneously try to append to the same stream. Without concurrency control, you get split-brain state. The solution: every append operation specifies an expected\_version — the stream version it read before making its decision. If the stream's actual version has advanced (because someone else appended), the operation is rejected with a concurrency exception. The caller must reload and retry. This is how the Ledger prevents two AI agents from simultaneously making conflicting decisions. No locks required. No transactions spanning multiple aggregates.*

**Projections — Inline vs. Async**

*A projection transforms events into a read model. Inline projections update synchronously in the same transaction as the event write — strong consistency, higher write latency. Async projections update asynchronously via a background daemon — lower write latency, eventual consistency, and the ability to rebuild from scratch by replaying. The Marten library (the enterprise .NET standard for PostgreSQL-backed event stores) calls its async projection runner the "Async Daemon." Python equivalents achieve the same pattern with background asyncio tasks. Study: Marten docs on projection lifecycle; EventStoreDB catch-up subscriptions.*

**Upcasting — Handling Schema Evolution**

*In a CRUD system, you run a migration and the data changes. In an event store, the past is immutable. When your event schema evolves, you write an upcaster — a function that transforms old event structures into new ones at read time, without touching the stored events. This is the event sourcing solution to the problem identified by schema evolution analysis tools. In production, upcasters are registered in a chain: v1→v2→v3, applied automatically whenever old events are loaded. An event store without upcasting is an event store that will eventually break under the weight of its own history.*

**The Outbox Pattern — Guaranteed Event Delivery**

*The classic distributed systems problem: you append an event to the store AND need to publish it to a message bus (Kafka, Redis Streams, RabbitMQ). If the store write succeeds but the publish fails, your read models and downstream systems are inconsistent. The Outbox Pattern solves this: write events to both the event store and an "outbox" table in the same database transaction. A separate process polls the outbox and publishes reliably. This is how you connect The Ledger to the Polyglot Bridge (Week 10).*

**The Gas Town Persistent Ledger Pattern**

*Named for the infrastructure pattern in agentic systems where agent context is lost on process restart. The solution: every agent action is written to the event store as an event before the action is executed. On restart, the agent replays its event stream to reconstruct its context window. This is not just logging — it is the agent's memory backed by the most reliable storage primitive available: an append-only, ACID-compliant, PostgreSQL-backed event stream.*

## 

## **Stack Orientation — Enterprise Tools in 2026**

The enterprise market has converged on two primary event store backends. You must understand both even if you implement only one.

| TOOL | STACK | BEST FOR | ENTERPRISE ADOPTION | YOUR CHOICE IN THIS CHALLENGE |
| :---- | :---- | :---- | :---- | :---- |
| PostgreSQL \+ psycopg3 | Python (primary) | Single-database architectures; teams already on Postgres; FDE rapid deployment | Extremely high — Postgres is everywhere | PRIMARY — Build the event store schema and all phases using Postgres \+ asyncpg/psycopg3 |
| EventStoreDB 24.x | Any (HTTP API) | Dedicated high-throughput event stores; persistent subscriptions at scale; native gRPC streaming | Growing — the purpose-built standard | REFERENCE — Know the API; document in DOMAIN\_NOTES how your Postgres schema maps to EventStoreDB concepts |
| Marten 7.x \+ Wolverine | .NET / C\# | Enterprise .NET shops; Async Daemon for projection management; Wolverine for command routing | Dominant in .NET enterprise | CONCEPTUAL — Study the architecture; your Python implementation should mirror the same patterns |
| Kafka \+ Kafka Streams | Any | Very-high-throughput event streaming; not a true event store (retention limits) | Ubiquitous in large enterprise | INTEGRATION — Week 10 connects The Ledger to Kafka via the Outbox pattern |
| Redis Streams | Any | Lower-latency pub/sub; projection fan-out; not durable by default | Common as event bus layer | INTEGRATION — Use Redis Streams for real-time projection update notifications |

## **DOMAIN\_NOTES.md — Graded Deliverable**

Produce a DOMAIN\_NOTES.md before writing any implementation code. It must answer all of the following with specificity, not generality. This document is assessed independently of your code — a candidate who writes excellent code but cannot reason about the tradeoffs is not ready for enterprise event sourcing work.

1. **EDA vs. ES distinction:** A component uses callbacks (like LangChain traces) to capture event-like data. Is this Event-Driven Architecture (EDA) or Event Sourcing (ES)? If you redesigned it using The Ledger, what exactly would change in the architecture and what would you gain?  
2. **The aggregate question:** In the scenario below, you will build four aggregates. Identify one alternative boundary you considered and rejected. What coupling problem does your chosen boundary prevent?  
3. **Concurrency in practice:** Two AI agents simultaneously process the same loan application and both call append\_events with expected\_version=3. Trace the exact sequence of operations in your event store. What does the losing agent receive, and what must it do next?  
4. **Projection lag and its consequences:** Your LoanApplication projection is eventually consistent with a typical lag of 200ms. A loan officer queries "available credit limit" immediately after an agent commits a disbursement event. They see the old limit. What does your system do, and how do you communicate this to the user interface?  
5. **The upcasting scenario:** The CreditDecisionMade event was defined in 2024 with {application\_id, decision, reason}. In 2026 it needs {application\_id, decision, reason, model\_version, confidence\_score, regulatory\_basis}. Write the upcaster. What is your inference strategy for historical events that predate model\_version?  
6. **The Marten Async Daemon parallel:** Marten 7.0 introduced distributed projection execution across multiple nodes. Describe how you would achieve the same pattern in your Python implementation. What coordination primitive do you use, and what failure mode does it guard against?

## 

# **The Scenario — Apex Financial Services**

Apex Financial Services is deploying a multi-agent AI platform to process commercial loan applications. Four specialized AI agents collaborate on each application: a CreditAnalysis agent evaluates financial risk, a FraudDetection agent screens for anomalous patterns, a ComplianceAgent verifies regulatory eligibility, and a DecisionOrchestrator synthesises their outputs and produces a final recommendation. Human loan officers review the recommendation and make the final binding decision.

The regulatory environment requires: a complete, immutable audit trail of every AI decision and the data that informed it; the ability to reconstruct the exact state of any application at any point in time for regulatory examination; temporal queries (e.g., "what would the credit decision have been if we had used last month's risk model?"); and cryptographic integrity — any tampering with the audit trail must be detectable. The CTO has mandated that the system must not be modified to add auditability after the fact — auditability must be the architecture, not an annotation.

This is the canonical environment where event sourcing is not just beneficial — it is the only architecture that satisfies the requirements. Your task is to build The Ledger: the event store and its surrounding infrastructure that makes this system governable.

## **Why This Scenario**

Financial services is the highest-density event sourcing environment in enterprise software. Every loan decision, every risk calculation, every compliance check is a regulated event. The same architecture applies directly to any domain where audit trails are non-negotiable: healthcare prior authorisations, government benefit decisions, insurance claim adjudication, and — directly relevant to your work — AI agent decision logs in any enterprise deployment. Master this scenario and you have mastered the pattern for all of them.

## **The Four Aggregates**

| AGGREGATE | STREAM ID FORMAT | WHAT IT TRACKS | KEY BUSINESS INVARIANTS |
| :---- | :---- | :---- | :---- |
| LoanApplication | loan-{application\_id} | Full lifecycle of a commercial loan application from submission to decision | Cannot transition from Approved to UnderReview; cannot be approved if compliance check is pending; credit limit cannot exceed agent-assessed maximum |
| AgentSession | agent-{agent\_id}-{session\_id} | All actions taken by a specific AI agent instance during a work session, including model version, input data hashes, reasoning trace, and outputs | Every output event must reference a ContextLoaded event; every decision must reference the specific model version that produced it |
| ComplianceRecord | compliance-{application\_id} | Regulatory checks, rule evaluations, and compliance verdicts for each application | Cannot issue a compliance clearance without all mandatory checks; every check must reference the specific regulation version evaluated against |
| AuditLedger | audit-{entity\_type}-{entity\_id} | Cross-cutting audit trail linking events across all aggregates for a single business entity | Append-only; no events may be removed; must maintain cross-stream causal ordering via correlation\_id chains |

## **The Event Catalogue**

These are the events you will implement. The catalogue is intentionally incomplete — identifying the missing events is part of the Phase 1 domain exercise.

| EVENT TYPE | AGGREGATE | VERSION | KEY PAYLOAD FIELDS |
| :---- | :---- | :---- | :---- |
| ApplicationSubmitted | LoanApplication | 1 | application\_id, applicant\_id, requested\_amount\_usd, loan\_purpose, submission\_channel, submitted\_at |
| CreditAnalysisRequested | LoanApplication | 1 | application\_id, assigned\_agent\_id, requested\_at, priority |
| CreditAnalysisCompleted | AgentSession | 2 | application\_id, agent\_id, session\_id, model\_version, confidence\_score, risk\_tier, recommended\_limit\_usd, analysis\_duration\_ms, input\_data\_hash |
| FraudScreeningCompleted | AgentSession | 1 | application\_id, agent\_id, fraud\_score, anomaly\_flags\[\], screening\_model\_version, input\_data\_hash |
| ComplianceCheckRequested | ComplianceRecord | 1 | application\_id, regulation\_set\_version, checks\_required\[\] |
| ComplianceRulePassed | ComplianceRecord | 1 | application\_id, rule\_id, rule\_version, evaluation\_timestamp, evidence\_hash |
| ComplianceRuleFailed | ComplianceRecord | 1 | application\_id, rule\_id, rule\_version, failure\_reason, remediation\_required |
| DecisionGenerated | LoanApplication | 2 | application\_id, orchestrator\_agent\_id, recommendation (APPROVE/DECLINE/REFER), confidence\_score, contributing\_agent\_sessions\[\], decision\_basis\_summary, model\_versions{} |
| HumanReviewCompleted | LoanApplication | 1 | application\_id, reviewer\_id, override (bool), final\_decision, override\_reason (if override) |
| ApplicationApproved | LoanApplication | 1 | application\_id, approved\_amount\_usd, interest\_rate, conditions\[\], approved\_by (human\_id or "auto"), effective\_date |
| ApplicationDeclined | LoanApplication | 1 | application\_id, decline\_reasons\[\], declined\_by, adverse\_action\_notice\_required (bool) |
| AgentContextLoaded | AgentSession | 1 | agent\_id, session\_id, context\_source, event\_replay\_from\_position, context\_token\_count, model\_version |
| AuditIntegrityCheckRun | AuditLedger | 1 | entity\_id, check\_timestamp, events\_verified\_count, integrity\_hash, previous\_hash (chain) |

# **PHASE 1  ·  The Event Store Core — PostgreSQL Schema & Interface**

Build the event store foundation. Everything else is built on this. The schema is not a suggestion — it is the contract that every other component in this program will eventually write to and read from. Please identify and report if there are missing elements that could improve the schema validity in future scenarios. 

## **Database Schema**

Create the following tables. Justify every column in DESIGN.md — columns you cannot justify should not exist.

CREATE TABLE events (  
  event\_id         UUID PRIMARY KEY DEFAULT gen\_random\_uuid(),  
  stream\_id        TEXT NOT NULL,  
  stream\_position  BIGINT NOT NULL,  
  global\_position  BIGINT GENERATED ALWAYS AS IDENTITY,  
  event\_type       TEXT NOT NULL,  
  event\_version    SMALLINT NOT NULL DEFAULT 1,  
  payload          JSONB NOT NULL,  
  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,  
  recorded\_at      TIMESTAMPTZ NOT NULL DEFAULT clock\_timestamp(),  
  CONSTRAINT uq\_stream\_position UNIQUE (stream\_id, stream\_position)  
);

CREATE INDEX idx\_events\_stream\_id ON events (stream\_id, stream\_position);  
CREATE INDEX idx\_events\_global\_pos ON events (global\_position);  
CREATE INDEX idx\_events\_type ON events (event\_type);  
CREATE INDEX idx\_events\_recorded ON events (recorded\_at);

CREATE TABLE event\_streams (  
  stream\_id        TEXT PRIMARY KEY,  
  aggregate\_type   TEXT NOT NULL,  
  current\_version  BIGINT NOT NULL DEFAULT 0,  
  created\_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),  
  archived\_at      TIMESTAMPTZ,  
  metadata         JSONB NOT NULL DEFAULT '{}'::jsonb  
);

CREATE TABLE projection\_checkpoints (  
  projection\_name  TEXT PRIMARY KEY,  
  last\_position    BIGINT NOT NULL DEFAULT 0,  
  updated\_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()  
);

CREATE TABLE outbox (  
  id               UUID PRIMARY KEY DEFAULT gen\_random\_uuid(),  
  event\_id         UUID NOT NULL REFERENCES events(event\_id),  
  destination      TEXT NOT NULL,  
  payload          JSONB NOT NULL,  
  created\_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),  
  published\_at     TIMESTAMPTZ,  
  attempts         SMALLINT NOT NULL DEFAULT 0  
);

## **Core Python Interface**

Implement EventStore as an async Python class. The interface is fixed — implementation is yours.

class EventStore:  
    async def append(  
        self,  
        stream\_id: str,  
        events: list\[BaseEvent\],  
        expected\_version: int,          \# \-1 \= new stream; N \= exact version required  
        correlation\_id: str | None \= None,  
        causation\_id:   str | None \= None,  
    ) \-\> int:                            \# returns new stream version  
        """  
        Atomically appends events to stream\_id.  
        Raises OptimisticConcurrencyError if stream version \!= expected\_version.  
        Writes to outbox in same transaction.  
        """

    async def load\_stream(  
        self,  
        stream\_id: str,  
        from\_position: int \= 0,  
        to\_position:   int | None \= None,  
    ) \-\> list\[StoredEvent\]:             \# events in stream order, upcasted

    async def load\_all(  
        self,  
        from\_global\_position: int \= 0,  
        event\_types: list\[str\] | None \= None,  
        batch\_size: int \= 500,  
    ) \-\> AsyncIterator\[StoredEvent\]:   \# async generator, efficient for replay

    async def stream\_version(self, stream\_id: str) \-\> int:  
    async def archive\_stream(self, stream\_id: str) \-\> None:  
    async def get\_stream\_metadata(self, stream\_id: str) \-\> StreamMetadata:

## **Optimistic Concurrency — The Double-Decision Test**

This is the most critical test in Phase 1\. Two AI agents simultaneously attempt to append a CreditAnalysisCompleted event to the same loan application stream. Both read the stream at version 3 and pass expected\_version=3 to their append call. Exactly one must succeed. The other must receive OptimisticConcurrencyError and retry after reloading the stream.

Implement a test that spawns two concurrent asyncio tasks doing this. The test must assert: (a) total events appended to the stream \= 4 (not 5), (b) the winning task's event has stream\_position=4, (c) the losing task's OptimisticConcurrencyError is raised, not silently swallowed.

## **Why This Matters**

In the Apex loan scenario, this test represents two fraud-detection agents simultaneously flagging the same application. Without optimistic concurrency, both flags are applied and the application's state becomes inconsistent — no one knows which fraud score is authoritative. With it, one agent's decision wins; the other must reload and see whether its analysis is still relevant. This is not an edge case — at 1,000 applications/hour with 4 agents each, concurrency collisions happen constantly.

## **PHASE 2  ·  Domain Logic — Aggregates, Commands & Business Rules**

Implement the domain logic for LoanApplication and AgentSession. The pattern: command received → aggregate state reconstructed by replaying events → business rules validated → new events appended.

## **The Command Handler Pattern**

\# Every command handler follows this exact structure:  
async def handle\_credit\_analysis\_completed(  
    cmd: CreditAnalysisCompletedCommand,  
    store: EventStore,  
) \-\> None:  
    \# 1\. Reconstruct current aggregate state from event history  
    app \= await LoanApplicationAggregate.load(store, cmd.application\_id)  
    agent \= await AgentSessionAggregate.load(store, cmd.agent\_id, cmd.session\_id)

    \# 2\. Validate — all business rules checked BEFORE any state change  
    app.assert\_awaiting\_credit\_analysis()  
    agent.assert\_context\_loaded()                    \# Gas Town pattern  
    agent.assert\_model\_version\_current(cmd.model\_version)

    \# 3\. Determine new events — pure logic, no I/O  
    new\_events \= \[  
        CreditAnalysisCompleted(  
            application\_id \= cmd.application\_id,  
            agent\_id       \= cmd.agent\_id,  
            session\_id     \= cmd.session\_id,  
            model\_version  \= cmd.model\_version,  
            confidence\_score \= cmd.confidence\_score,  
            risk\_tier      \= cmd.risk\_tier,  
            recommended\_limit\_usd \= cmd.recommended\_limit\_usd,  
            analysis\_duration\_ms  \= cmd.duration\_ms,  
            input\_data\_hash \= hash\_inputs(cmd.input\_data),  
        )  
    \]

    \# 4\. Append atomically — optimistic concurrency enforced by store  
    await store.append(  
        stream\_id        \= f"loan-{cmd.application\_id}",  
        events           \= new\_events,  
        expected\_version \= app.version,  
        correlation\_id   \= cmd.correlation\_id,  
        causation\_id     \= cmd.causation\_id,  
    )

## **Business Rules to Enforce**

The following rules must be enforced in the aggregate domain logic, not in the API layer. A rule that is only checked in a request handler is not a business rule — it is a UI validation.

1. **Application state machine:** Valid transitions only: Submitted → AwaitingAnalysis → AnalysisComplete → ComplianceReview → PendingDecision → ApprovedPendingHuman / DeclinedPendingHuman → FinalApproved / FinalDeclined. Any out-of-order transition raises DomainError.  
2. **Agent context requirement (Gas Town):** An AgentSession aggregate MUST have an AgentContextLoaded event as its first event before any decision event can be appended. This enforces the persistent ledger pattern — no agent may make a decision without first declaring its context source.  
3. **Model version locking:** Once a CreditAnalysisCompleted event is appended for an application, no further CreditAnalysisCompleted events may be appended for the same application unless the first was superseded by a HumanReviewOverride. This prevents analysis churn.  
4. **Confidence floor:** A DecisionGenerated event with confidence\_score \< 0.6 must set recommendation \= "REFER" regardless of the orchestrator's analysis. This is a regulatory requirement, enforced in the aggregate.  
5. **Compliance dependency:** An ApplicationApproved event cannot be appended unless all ComplianceRulePassed events for the application's required checks are present in the ComplianceRecord stream. The LoanApplication aggregate must hold a reference to check this.  
6. **Causal chain enforcement:** Every DecisionGenerated event's contributing\_agent\_sessions\[\] list must reference only AgentSession stream IDs that contain a decision event for this application\_id. An orchestrator that references sessions that never processed this application must be rejected.

## **Aggregate State Reconstruction**

Each aggregate must implement a load() classmethod that replays its event stream and applies each event to build current state. The apply pattern must be explicit — one method per event type:

class LoanApplicationAggregate:  
    @classmethod  
    async def load(cls, store: EventStore, application\_id: str) \-\> "LoanApplicationAggregate":  
        events \= await store.load\_stream(f"loan-{application\_id}")  
        agg \= cls(application\_id=application\_id)  
        for event in events:  
            agg.\_apply(event)  
        return agg

    def \_apply(self, event: StoredEvent) \-\> None:  
        handler \= getattr(self, f"\_on\_{event.event\_type}", None)  
        if handler:  
            handler(event)  
        self.version \= event.stream\_position

    def \_on\_ApplicationSubmitted(self, event: StoredEvent) \-\> None:  
        self.state \= ApplicationState.SUBMITTED  
        self.applicant\_id \= event.payload\["applicant\_id"\]  
        self.requested\_amount \= event.payload\["requested\_amount\_usd"\]

    def \_on\_ApplicationApproved(self, event: StoredEvent) \-\> None:  
        self.state \= ApplicationState.FINAL\_APPROVED  
        self.approved\_amount \= event.payload\["approved\_amount\_usd"\]

# 

# **PHASE 3  ·  Projections — CQRS Read Models & Async Daemon** 

Projections are the read side of CQRS. They subscribe to the event stream and maintain read-optimised views that can be queried without loading and replaying aggregate streams. Build three projections and the async daemon that keeps them current.

## **The Async Projection Daemon**

The daemon is a background asyncio task that continuously polls the events table from the last processed global\_position, processes new events through registered projections, and updates projection\_checkpoints. It must be fault-tolerant: if a projection handler fails, the daemon must log the error, skip the offending event (with configurable retry count), and continue. A daemon that crashes on a bad event is a production incident.

class ProjectionDaemon:  
    def \_\_init\_\_(self, store: EventStore, projections: list\[Projection\]):  
        self.\_store \= store  
        self.\_projections \= {p.name: p for p in projections}  
        self.\_running \= False

    async def run\_forever(self, poll\_interval\_ms: int \= 100\) \-\> None:  
        self.\_running \= True  
        while self.\_running:  
            await self.\_process\_batch()  
            await asyncio.sleep(poll\_interval\_ms / 1000\)

    async def \_process\_batch(self) \-\> None:  
        \# Load lowest checkpoint across all projections  
        \# Load events from that position in batches  
        \# For each event, route to subscribed projections  
        \# Update checkpoints after each successful batch  
        \# Expose lag metric: global\_position \- last\_processed\_position  
        ...

## **Required Projections**

### **Projection 1: ApplicationSummary**

A read-optimised view of every loan application's current state. Stored as a Postgres table (one row per application). Updated inline by the daemon as new events arrive.

**Table schema:**

application\_id, state, applicant\_id,  
requested\_amount\_usd, approved\_amount\_usd,  
risk\_tier, fraud\_score,  
compliance\_status, decision,  
agent\_sessions\_completed\[\],  
last\_event\_type, last\_event\_at,  
human\_reviewer\_id, final\_decision\_at

### **Projection 2: AgentPerformanceLedger**

Aggregated performance metrics per AI agent model version. Enables the question: "Has agent v2.3 been making systematically different decisions than v2.2?"

**Table schema:**

agent\_id, model\_version,  
analyses\_completed, decisions\_generated,  
avg\_confidence\_score, avg\_duration\_ms,  
approve\_rate, decline\_rate, refer\_rate,  
human\_override\_rate,  
first\_seen\_at, last\_seen\_at

### **Projection 3: ComplianceAuditView (Critical)**

This projection is the regulatory read model — the view that a compliance officer or regulator queries when examining an application. It must be complete (every compliance event), traceable (every rule references its regulation version), and temporally queryable (state at any past timestamp).

Unlike the other projections, the ComplianceAuditView must support the temporal query interface: get\_state\_at(application\_id, timestamp) → ComplianceAuditView. This requires a snapshot strategy you must implement and justify in DESIGN.md.

* **get\_current\_compliance(application\_id)** → full compliance record with all checks, verdicts, and regulation versions  
* **get\_compliance\_at(application\_id, timestamp)** → compliance state as it existed at a specific moment (regulatory time-travel)  
* **get\_projection\_lag()** → milliseconds between latest event in store and latest event this projection has processed — must be exposed as a metric  
* **rebuild\_from\_scratch()** → truncate projection table and replay all events from position 0 — must complete without downtime to live reads

## **Projection Lag — The Non-Negotiable Metric**

### **The Lag Contract**

Your ApplicationSummary projection must maintain a lag of under 500ms in normal operation. Your ComplianceAuditView projection may lag up to 2 seconds. These are not arbitrary numbers — they are service-level objectives (SLOs) you define in your DESIGN.md and demonstrate in testing. A projection system with no lag measurement is not production-ready. Your daemon must expose get\_lag() for every projection it manages, and your test suite must assert that lag stays within bounds under a simulated load of 50 concurrent command handlers.

# **PHASE 4  ·  Upcasting, Integrity & The Gas Town Memory Pattern**

## **4A — Upcaster Registry**

Implement a centralized UpcasterRegistry that automatically applies version migrations whenever old events are loaded from the store. The event loading path must call the registry transparently — callers never manually invoke upcasters.

class UpcasterRegistry:  
    def \_\_init\_\_(self):  
        self.\_upcasters: dict\[tuple\[str, int\], Callable\] \= {}

    def register(self, event\_type: str, from\_version: int):  
        """Decorator. Registers fn as upcaster from event\_type@from\_version."""  
        def decorator(fn: Callable\[\[dict\], dict\]) \-\> Callable:  
            self.\_upcasters\[(event\_type, from\_version)\] \= fn  
            return fn  
        return decorator

    def upcast(self, event: StoredEvent) \-\> StoredEvent:  
        """Apply all registered upcasters for this event type in version order."""  
        current \= event  
        v \= event.event\_version  
        while (event.event\_type, v) in self.\_upcasters:  
            new\_payload \= self.\_upcasters\[(event.event\_type, v)\](current.payload)  
            current \= current.with\_payload(new\_payload, version=v \+ 1\)  
            v \+= 1  
        return current

\# Usage:  
registry \= UpcasterRegistry()

@registry.register("CreditAnalysisCompleted", from\_version=1)  
def upcast\_credit\_v1\_to\_v2(payload: dict) \-\> dict:  
    return {  
        \*\*payload,  
        "model\_version": "legacy-pre-2026",   \# inference for historical events  
        "confidence\_score": None,              \# genuinely unknown — do not fabricate  
    }

Implement upcasters for the following events and justify your inference strategy for missing historical fields in DESIGN.md:

1. **CreditAnalysisCompleted v1→v2:** Add model\_version (inferred from recorded\_at timestamp), confidence\_score (null — genuinely unknown; document why fabrication would be worse than null), regulatory\_basis (infer from rule versions active at recorded\_at date).  
2. **DecisionGenerated v1→v2:** Add model\_versions{} dict (reconstruct from contributing\_agent\_sessions by loading each session's AgentContextLoaded event — this requires a store lookup; document the performance implication).

### **The Immutability Test**

Your test suite must include a test that: (1) directly queries the events table in Postgres to get the raw stored payload of a v1 event, (2) loads the same event through your EventStore.load\_stream() and verifies it is upcasted to v2, (3) directly queries the events table again and verifies the raw stored payload is UNCHANGED. Any system where upcasting touches the stored events has broken the core guarantee of event sourcing. This test is mandatory and will be run during assessment.

## **4B — Cryptographic Audit Chain**

Regulatory-grade audit trails require tamper evidence. Implement a hash chain over the event log for the AuditLedger aggregate. Each AuditIntegrityCheckRun event records a hash of all preceding events plus the previous integrity hash, forming a blockchain-style chain. Any post-hoc modification of events breaks the chain.

async def run\_integrity\_check(  
    store: EventStore,  
    entity\_type: str,  
    entity\_id: str,  
) \-\> IntegrityCheckResult:  
    """  
    1\. Load all events for the entity's primary stream  
    2\. Load the last AuditIntegrityCheckRun event (if any)  
    3\. Hash the payloads of all events since the last check  
    4\. Verify hash chain: new\_hash \= sha256(previous\_hash \+ event\_hashes)  
    5\. Append new AuditIntegrityCheckRun event to audit-{entity\_type}-{entity\_id} stream  
    6\. Return result with: events\_verified, chain\_valid (bool), tamper\_detected (bool)  
    """

## **4C — The Gas Town Agent Memory Pattern**

Implement the pattern that prevents the catastrophic memory loss described in the program materials. An AI agent that crashes mid-session must be able to restart and reconstruct its exact context from the event store, then continue where it left off without repeating completed work.

async def reconstruct\_agent\_context(  
    store: EventStore,  
    agent\_id: str,  
    session\_id: str,  
    token\_budget: int \= 8000,  
) \-\> AgentContext:  
    """  
    1\. Load full AgentSession stream for agent\_id \+ session\_id  
    2\. Identify: last completed action, pending work items, current application state  
    3\. Summarise old events into prose (token-efficient)  
    4\. Preserve verbatim: last 3 events, any PENDING or ERROR state events  
    5\. Return: AgentContext with context\_text, last\_event\_position,  
               pending\_work\[\], session\_health\_status

    CRITICAL: if the agent's last event was a partial decision (no corresponding  
    completion event), flag the context as NEEDS\_RECONCILIATION — the agent  
    must resolve the partial state before proceeding.  
    """

Test this pattern with a simulated crash: start an agent session, append 5 events, then call reconstruct\_agent\_context() without the in-memory agent object. Verify that the reconstructed context contains enough information for the agent to continue correctly.

# **PHASE 5  ·  MCP Server — Exposing The Ledger as Enterprise Infrastructure** 

The MCP server is the interface between The Ledger and any AI agent or enterprise system that needs to interact with it. Tools (Commands) write events; Resources (Queries) read from projections. This is structural CQRS — the MCP specification naturally implements the read/write separation.

## **MCP Tools — The Command Side**

| TOOL NAME | COMMAND IT EXECUTES | CRITICAL VALIDATION | RETURN VALUE |
| :---- | :---- | :---- | :---- |
| submit\_application | ApplicationSubmitted | Schema validation via Pydantic; duplicate application\_id check | stream\_id, initial\_version |
| record\_credit\_analysis | CreditAnalysisCompleted | agent\_id must have active AgentSession with context loaded; optimistic concurrency on loan stream | event\_id, new\_stream\_version |
| record\_fraud\_screening | FraudScreeningCompleted | Same agent session validation; fraud\_score must be 0.0–1.0 | event\_id, new\_stream\_version |
| record\_compliance\_check | ComplianceRulePassed / ComplianceRuleFailed | rule\_id must exist in active regulation\_set\_version | check\_id, compliance\_status |
| generate\_decision | DecisionGenerated | All required analyses must be present; confidence floor enforcement | decision\_id, recommendation |
| record\_human\_review | HumanReviewCompleted | reviewer\_id authentication; if override=True, override\_reason required | final\_decision, application\_state |
| start\_agent\_session | AgentContextLoaded | Gas Town: required before any agent decision tools; writes context source and token count | session\_id, context\_position |
| run\_integrity\_check | AuditIntegrityCheckRun | Can only be called by compliance role; rate-limited to 1/minute per entity | check\_result, chain\_valid |

## **MCP Resources — The Query Side**

Resources expose projections. They must never load aggregate streams — all reads must come from projections. A resource that replays events on every query is an anti-pattern that will not scale.

| RESOURCE URI | PROJECTION SOURCE | SUPPORTS TEMPORAL QUERY? | SLO |
| :---- | :---- | :---- | :---- |
| ledger://applications/{id} | ApplicationSummary | No — current state only | p99 \< 50ms |
| ledger://applications/{id}/compliance | ComplianceAuditView | Yes — ?as\_of=timestamp | p99 \< 200ms |
| ledger://applications/{id}/audit-trail | AuditLedger stream (direct load — justified exception) | Yes — ?from=\&to= range | p99 \< 500ms |
| ledger://agents/{id}/performance | AgentPerformanceLedger | No — current metrics only | p99 \< 50ms |
| ledger://agents/{id}/sessions/{session\_id} | AgentSession stream (direct load) | Yes — full replay capability | p99 \< 300ms |
| ledger://ledger/health | ProjectionDaemon.get\_all\_lags() | No | p99 \< 10ms — this is the watchdog endpoint |

## **Tool Interface Design for LLM Consumption**

Tools and resources are consumed by AI agents, not humans. The description and parameter schema of each tool determines whether the consuming LLM uses it correctly — this is API design for a non-human consumer. Two requirements that most engineers miss:

* **Precondition documentation in the tool description:** "This tool requires an active agent session created by start\_agent\_session. Calling without an active session will return a PreconditionFailed error." An LLM that does not know this precondition will repeatedly fail and retry. The description is the only contract the LLM has.  
* **Structured error types, not messages:** Errors returned by tools must be typed objects: {error\_type: "OptimisticConcurrencyError", message: "...", stream\_id: "...", expected\_version: 3, actual\_version: 5, suggested\_action: "reload\_stream\_and\_retry"}. An LLM that receives an unstructured error message cannot reason about what to do. A typed error with suggested\_action enables autonomous recovery.

## **The MCP Integration Test**

Your MCP server must pass this integration test: start a fresh Ledger instance, then drive a complete loan application lifecycle — from ApplicationSubmitted through FinalApproved — using only MCP tool calls. No direct Python function calls. The test simulates what a real AI agent would do: it calls start\_agent\_session, then record\_credit\_analysis, then generate\_decision, then record\_human\_review, then queries the compliance audit view to verify the complete trace is present. If any step requires a workaround outside the MCP interface, the interface has a design flaw.

# **PHASE 6 (BONUS)  ·  What-If Projections & Regulatory Time Travel**

This phase is required for Score 5 and is the discriminator for trainees with genuine event sourcing experience. It is challenging and takes a full day. Attempt it only after Phases 1–5 are solid.

## **The What-If Projector**

The Apex compliance team needs to run counterfactual scenarios: "What would the decision have been if we had used the March risk model instead of the February risk model?" This requires replaying application history with a substituted event — a counterfactual — injected at the point of the original credit analysis.

async def run\_what\_if(  
    store: EventStore,  
    application\_id: str,  
    branch\_at\_event\_type: str,            \# e.g. "CreditAnalysisCompleted"  
    counterfactual\_events: list\[BaseEvent\],  \# events to inject instead of real ones  
    projections: list\[Projection\],        \# projections to evaluate under the scenario  
) \-\> WhatIfResult:  
    """  
    1\. Load all events for the application stream up to the branch point  
    2\. At the branch point, inject counterfactual\_events instead of real events  
    3\. Continue replaying real events that are causally INDEPENDENT of the branch  
    4\. Skip real events that are causally DEPENDENT on the branched events  
    5\. Apply all events (pre-branch real \+ counterfactual \+ post-branch independent)  
       to each projection  
    6\. Return: {real\_outcome, counterfactual\_outcome, divergence\_events\[\]}

    NEVER writes counterfactual events to the real store.  
    Causal dependency: an event is dependent if its causation\_id traces  
    back to an event at or after the branch point.  
    """

Demonstrate with the specific scenario: "What would the final decision have been if the credit analysis had returned risk\_tier='HIGH' instead of 'MEDIUM'?" Your what-if projector must produce a materially different ApplicationSummary outcome — demonstrating that business rule enforcement cascades correctly through the counterfactual.

## **Regulatory Examination Package**

Implement a generate\_regulatory\_package(application\_id, examination\_date) function that produces a complete, self-contained examination package containing:

1. The complete event stream for the application, in order, with full payloads.  
2. The state of every projection as it existed at examination\_date.  
3. The audit chain integrity verification result.  
4. A human-readable narrative of the application lifecycle, generated by replaying events and constructing a plain-English summary (one sentence per significant event).  
5. The model versions, confidence scores, and input data hashes for every AI agent that participated in the decision.

The package must be a self-contained JSON file that a regulator can verify against the database independently — they should not need to trust your system to validate that the package is accurate.

# **DESIGN.md — Required Sections**

This document is assessed with equal weight to the code. The principle: architecture is about tradeoffs. A decision without a tradeoff analysis is not an architectural decision — it is a default. Six required sections:

1. **Aggregate boundary justification:** Why is ComplianceRecord a separate aggregate from LoanApplication? What would couple if you merged them? Trace the coupling to a specific failure mode under concurrent write scenarios.  
2. **Projection strategy:** For each projection, justify: Inline vs. Async, and the SLO commitment. For the ComplianceAuditView temporal query, justify your snapshot strategy (event-count trigger, time trigger, or manual) and describe snapshot invalidation logic.  
3. **Concurrency analysis:** Under peak load (100 concurrent applications, 4 agents each), how many OptimisticConcurrencyErrors do you expect per minute on the loan-{id} streams? What is the retry strategy and what is the maximum retry budget before you return a failure to the caller?  
4. **Upcasting inference decisions:** For every inferred field in your upcasters, quantify the likely error rate and the downstream consequence of an incorrect inference. When would you choose null over an inference?  
5. **EventStoreDB comparison:** Map your PostgreSQL schema to EventStoreDB concepts: streams → stream IDs, your load\_all() → EventStoreDB $all stream subscription, your ProjectionDaemon → EventStoreDB persistent subscriptions. What does EventStoreDB give you that your implementation must work harder to achieve?  
6. **What you would do differently:** Name the single most significant architectural decision you would reconsider with another full day. This section is the most important — it shows whether you can distinguish between "what I built" and "what the best version of this would be."

# 

# 

# 

# 

# 

# **Assessment Rubric**

*Score 3 \= functional and demonstrates understanding. Score 5 \= production-ready, would deploy to a real enterprise client. Scores 4 and 5 require demonstrated understanding in DESIGN.md, not just working code.*

| CRITERION | 1 | 2 | 3 | 4 | 5 |
| :---- | :---- | :---- | :---- | :---- | :---- |
| **Event Store Core & Concurrency** | Schema present; no concurrency control | Append works; expected\_version not enforced | All interface methods; concurrency enforced; double-decision test passes | Outbox pattern; archive support; all edge cases; concurrent load test passes | Above \+ DESIGN.md justifies every schema column; retry strategy documented with error rate estimate |
| **Domain Logic & Business Rules** | One aggregate; no state machine | State machine present; some rules missing | Both aggregates; all 6 business rules enforced | Causal chain enforcement; Gas Town pattern; model version locking | Above \+ counterfactual command testing; all invariants tested under concurrent scenarios |
| **Projection Daemon & CQRS** | No projections or direct stream reads only | One projection; no lag metric | All 3 projections; lag metric exposed; daemon fault-tolerant | SLO tests passing; rebuild-from-scratch without downtime; temporal query on ComplianceAuditView | Above \+ snapshot invalidation; distributed daemon analysis in DESIGN.md |
| **Upcasting & Integrity** | No upcasting; store mutated on upcast | Upcaster exists; chain not automatic | Auto-upcasting via registry; immutability test passes | Both upcasters; inference justified; null vs. fabrication reasoning present | Above \+ hash chain integrity; generate\_regulatory\_package working; chain break detection |
| **MCP Server — Tool Design** | No MCP server | Tools present; error types unstructured | All 8 tools; structured errors; preconditions documented | Resources from projections (no stream reads in resources); SLOs met | Above \+ full lifecycle integration test via MCP only; LLM-consumption preconditions in all tool descriptions |
| **DESIGN.md — Architectural Reasoning** | Not present | Describes what was built; no tradeoff analysis | All 6 sections; tradeoffs identified | Quantitative analysis (error rates, lag SLOs, retry budgets) | "What I would do differently" shows genuine reflection; identifies the thing the implementation got wrong |
| **BONUS — What-If & Regulatory Package** | (Not attempted) | (Not attempted) | What-if projection working on test scenario | Counterfactual produces materially different outcome; causal dependency filtering correct | Above \+ regulatory package is independently verifiable; narrative generation coherent |
