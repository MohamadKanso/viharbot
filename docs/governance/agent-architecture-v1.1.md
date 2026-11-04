# ViharBot
## Agent Architecture & Responsibilities Specification
**Version 1.1 | October 28, 2026**

**Status:** Proposed, reconciled with the existing repository  
**Governing documents:** Founding Charter v2.0; Agent Constitution v1.0  
**Repository:** `vchokshi/viharbot`  
**Deployment targets:** Local Docker, DigitalOcean staging, AWS production  
**Inference:** Externally managed LiteLLM gateway

### 1. Architectural Decision

ViharBot shall retain its eight established agent identities:

**Charlie, Nova, Atlas, Pixel, Sage, Cipher, Scout, and Orbit.**

> **Amendment (adopted):** the education-agents specification adds two further identities, Archie and Ada, without changing these eight, for ten in total. The founder adopted this amendment on 2026-11-03 in a comment on pull request #98. See [Education agents](../education-agents.md). Adoption covers the identities only; it does not authorize any execution capability, which still needs its own approval.

No existing identity shall be renamed, replaced, or removed to accommodate new consulting responsibilities.

The six-agent roster proposed in Architecture v1.0 is superseded.

Its useful consulting functions shall instead be mapped onto the eight existing agents through an extensible skills and capabilities system.

ViharBot shall distinguish five concepts:

1. **Identity:** The agent's persistent name, professional role, and personality.
2. **Skill:** A defined area of expertise or a reusable procedure.
3. **Capability:** A task type the orchestration system can assign.
4. **Execution binding:** The approved relationship between an identity, capability, and service.
5. **Runtime:** The infrastructure responsible for executing that service.

An agent may possess many skills, perform multiple capabilities, and use different approved runtimes over time.

A skill declaration shall never independently grant tool permissions.

### 2. Existing Architecture Baseline

The current repository defines eight identities and six operational specialist services.

Four identities are connected to those services:

| Identity | Existing executable capability |
|---|---|
| Charlie | Synthesis; optional orchestration planning |
| Nova | Research |
| Scout | Fact checking; source quality |
| Cipher | Security assessment; privacy review |

Atlas, Pixel, Sage, and Orbit exist as configured identities but do not yet have connected execution services.

The application currently uses FastAPI, Strands Agents, private REST communication, repository-controlled execution bindings, and external LiteLLM inference.

The default execution path is a fixed pipeline. Identity routing, controlled delegation, and iterative orchestration are separately feature-flagged.

Existing capabilities, bindings, API contracts, and execution modes must remain functional during migration.

### 3. Organizational Responsibilities

#### Charlie Chatwin — Chief Strategist & AI Orchestrator

**Primary responsibility:** Consulting engagement leadership.

Charlie owns problem framing, engagement planning, specialist coordination, synthesis, and final recommendations.

His expanded skills shall include executive advisory, consulting discovery, business and technical requirements analysis, stakeholder communication, and multidisciplinary decision support.

Charlie shall recommend delegation. The application shall authorize and execute it.

#### Nova — Director of Research & Knowledge

**Primary responsibility:** Research and knowledge development.

Nova owns structured research, evidence gathering, technical investigation, comparative analysis, and knowledge synthesis.

Her expanded skills shall include research planning, documentation analysis, technical literature review, competitive intelligence, and knowledge organization.

Nova shall preserve source provenance and distinguish evidence from inference.

#### Scout — Director of Intelligence & Discovery

**Primary responsibility:** Independent evidence validation.

Scout owns fact checking, source-quality assessment, verification, evidence discovery, and identification of unsupported claims.

His expanded skills shall include cross-source corroboration, contradiction detection, provenance verification, and confidence assessment.

Scout shall provide independent review rather than merely repeat Nova's findings.

#### Cipher — Chief Security & Risk Officer

**Primary responsibility:** Security, privacy, and enterprise risk.

Cipher owns cybersecurity assessments, privacy review, threat modeling, control analysis, and security governance.

Expanded skills shall include cloud security, DSPM, agentic AI security, IAM, Zero Trust, regulatory control mapping, and security architecture review.

Cipher shall identify material risks and recommend controls. Enforcement remains the responsibility of authorized platform components.

#### Atlas — Director of Exploration & Logistics

**Primary responsibility:** Planning, feasibility, and business strategy.

Atlas retains exploration and logistics as established specialties while expanding into enterprise strategy, financial evaluation, operating models, organizational readiness, and transformation roadmaps.

Atlas shall assess whether recommendations are economically and operationally practical.

#### Pixel — Director of Creative Intelligence

**Primary responsibility:** Visual communication and creative problem-solving.

Pixel owns design, visualization, presentations, diagrams, information architecture, and communication-oriented creative work.

Expanded skills shall include executive presentations, architecture visualization, UX design, data storytelling, and technical illustration.

Pixel shall translate complex findings into clear visual deliverables without changing their meaning.

#### Sage — Director of Philosophy & Human Perspective

**Primary responsibility:** Ethics, organizational behavior, and human-centered analysis.

Sage owns philosophical analysis, ethical reasoning, human factors, organizational change, and the societal implications of technology.

Expanded skills shall include AI governance, change management, stakeholder impact analysis, knowledge transfer, and organizational culture.

Sage shall help the team recognize human consequences that purely technical or financial analysis may overlook.

#### Orbit — Chief Automation & Engineering Officer

**Primary responsibility:** Engineering architecture and implementation.

Orbit owns automation, software engineering, infrastructure, integrations, and technical delivery.

Expanded skills shall include enterprise architecture, AWS, Azure, GCP, DevOps, CI/CD, infrastructure as code, testing, and deployment engineering.

Orbit shall distinguish design proposals from tested implementations and completed deployments.

Execution permissions shall remain separately controlled.

### 4. Skills Architecture

Skills shall be reusable, versioned, testable units of professional competence.

Each skill definition shall specify:

- Unique skill identifier and version.
- Description and business purpose.
- Primary and eligible agent identities.
- Required inputs.
- Expected outputs.
- Required tools or integrations.
- Required authorization scopes.
- Data classifications permitted.
- Dependencies.
- Applicable evaluation criteria.
- Implementation and deployment status.

Skills shall be registered independently of agent personality prompts.

A skill may be shared by multiple agents.

Only explicitly approved agent-capability-service bindings may be executed.

### 5. Capability Architecture

The existing capability vocabulary shall be preserved.

New capabilities shall be introduced incrementally and must not break existing contracts.

A capability may compose multiple skills.

For example, `enterprise_architecture_review` may use cloud architecture analysis, dependency mapping, cost evaluation, and security-control review.

A capability is executable only when:

1. It is registered.
2. It is marked implemented.
3. An approved execution binding exists.
4. Its service or runtime is available.
5. Required permissions are granted.
6. Its inputs satisfy the contract.
7. Applicable policy permits execution.

### 6. Collaboration Model

Charlie ordinarily serves as engagement coordinator.

Specialists may contribute independently or collaborate through approved workflows.

Examples:

**Enterprise AI strategy:** Charlie, Nova, Atlas, Cipher, Sage, and Orbit.

**Cybersecurity assessment:** Charlie, Cipher, Scout, Nova, and Orbit.

**Cloud modernization:** Charlie, Orbit, Atlas, Cipher, and Pixel.

**Executive presentation:** Charlie, Nova, Atlas, Scout, and Pixel.

**Agentic AI security laboratory:** Charlie, Cipher, Orbit, Nova, and Scout.

Not every task requires every agent.

Delegation shall be based on expertise, risk, dependencies, and expected value.

### 7. Security and Authority

Identity and skill membership shall not grant access to data, tools, or infrastructure.

Authorization must be enforced by the application and execution infrastructure.

The architecture shall support:

- Agent and workload identity.
- Scoped tool permissions.
- Engagement-level data isolation.
- Approval workflows.
- Controlled delegation.
- Execution budgets.
- Auditable actions.
- Security-policy enforcement.
- Environment-specific access restrictions.

Production operations and sensitive-data access shall require appropriate authorization.

### 8. Memory Architecture

ViharBot shall support five distinct memory scopes:

**Task memory:** Temporary execution context.

**Agent memory:** Agent-specific knowledge and validated experience.

**Engagement memory:** Authorized shared context for a consulting engagement.

**Institutional memory:** Approved reusable consulting knowledge.

**Founder knowledge:** Approved professional principles, methodologies, and anonymized experience.

The current append-only JSONL records are not equivalent to retrievable, governed agent memory.

Durable memory and retrieval shall be implemented separately.

### 9. Model Governance

All model inference shall traverse the externally managed LiteLLM gateway.

Agent identities may express preferred model characteristics, but actual model selection shall be governed by approved routing configuration.

Model routing shall consider task complexity, required capabilities, data sensitivity, reliability, latency, and cost.

LiteLLM configuration changes shall be documented and tested separately from ViharBot application changes.

### 10. Runtime Architecture

Agent identity shall remain independent of runtime placement.

Initial development shall preserve existing containerized services.

Future deployment shall support a combination of AWS container infrastructure and selected Amazon Bedrock AgentCore runtimes, subject to compatibility testing.

Every supported runtime must preserve:

- Task contracts.
- Authorization boundaries.
- LiteLLM inference routing.
- Structured event reporting.
- Trace correlation.
- Execution limits.
- Failure handling.

### 11. Observability

The existing streaming-event architecture shall be retained and extended.

Events must represent actual planning, delegation, execution, dependency, review, and terminal transitions.

The UI shall display the established agent identities rather than inventing new names for consulting functions.

Event schemas shall remain backward-compatible unless explicitly versioned.

### 12. Implementation Principles

The migration from the current implementation to this specification shall be additive.

The project shall:

1. Preserve all eight identities.
2. Preserve the six existing services.
3. Preserve existing execution bindings.
4. Introduce a separate skills registry.
5. Expand capabilities incrementally.
6. Connect planned identities through approved services.
7. Add authorization before privileged tools.
8. Introduce governed memory.
9. Validate multiple runtime types.
10. Deploy production infrastructure to AWS after staging validation.

### 13. Acceptance Criteria

Architecture v1.1 is satisfied when:

**AA-01:** All eight original identities load without modification to their stable IDs.

**AA-02:** Existing six-service workflows continue to pass regression tests.

**AA-03:** Skills can be declared and validated independently of execution.

**AA-04:** Each skill has a defined owner, inputs, outputs, status, and evaluation requirements.

**AA-05:** A declared skill cannot execute without an approved capability binding.

**AA-06:** Atlas, Pixel, Sage, and Orbit can be activated incrementally without replacing existing agents.

**AA-07:** All model inference continues through external LiteLLM.

**AA-08:** Security and authorization boundaries are enforced outside agent prompts.

**AA-09:** Agent activity remains observable through structured events.

**AA-10:** The architecture supports DigitalOcean staging and AWS production without coupling identity to deployment location.

### 14. Governing Principle

**Preserve identity. Expand expertise. Control execution. Measure outcomes.**

ViharBot shall evolve by extending the capabilities of its established consulting team.

Additional agents may be introduced when a distinct specialization, security boundary, or operational requirement justifies their existence.

New identities shall not be created merely to represent every consulting function.

**End of Architecture Specification v1.1**
