# ViharBot
## Agent Skills & Capabilities Registry
**Version 1.0 | October 2026**

**Status:** Proposed  
**Architecture reference:** Agent Architecture v1.1  
**Scope:** Eight existing agent identities

### 1. Registry Model

Each skill shall be represented by a versioned, machine-readable definition.

Required metadata:

| Field | Purpose |
|---|---|
| `skill_id` | Stable, unique identifier |
| `version` | Semantic version |
| `name` | Human-readable skill name |
| `description` | Professional purpose |
| `owner` | Primary agent identity |
| `eligible_agents` | Additional authorized candidates |
| `category` | Consulting discipline |
| `inputs` | Required input contract |
| `outputs` | Expected output contract |
| `required_tools` | Tools needed for execution |
| `required_scopes` | Authorization requirements |
| `data_classifications` | Permitted information classifications |
| `dependencies` | Other skills or capabilities required |
| `evaluation` | Acceptance and quality criteria |
| `status` | Lifecycle status |

A skill's existence in this registry shall not imply that it is executable.

### 2. Skill Lifecycle

Skills shall use the following lifecycle:

**Proposed → Defined → Implemented → Tested → Enabled → Deprecated**

`proposed`: Concept identified; no execution contract guaranteed.

`defined`: Metadata and input/output contracts approved.

`implemented`: Executable implementation exists.

`tested`: Required automated tests pass.

`enabled`: Approved for execution in a specified environment.

`deprecated`: No longer available for new assignments.

Execution availability shall additionally depend on capability bindings, runtime health, and authorization.

### 3. Charlie — Strategy and Orchestration

| Skill ID | Skill | Expected deliverable |
|---|---|---|
| `consulting.discovery` | Consulting discovery | Problem statement, objectives, constraints |
| `consulting.requirements` | Requirements analysis | Business and technical requirements |
| `consulting.engagement_planning` | Engagement planning | Work plan and specialist assignments |
| `consulting.executive_advisory` | Executive advisory | Decision brief and recommendations |
| `consulting.solution_evaluation` | Solution evaluation | Options and trade-off analysis |
| `consulting.synthesis` | Multidisciplinary synthesis | Integrated consulting recommendation |
| `consulting.stakeholder_management` | Stakeholder analysis | Stakeholder map and communication plan |
| `consulting.quality_review` | Deliverable quality review | Review findings and acceptance decision |

**Existing implementation alignment:** Synthesis and optional iterative planning.

**Priority:** Extend existing orchestration rather than introduce a second coordinator.

**Quality requirements:** Clear objectives, traceable delegation, evidence-based synthesis, and accurate execution reporting.

### 4. Nova — Research and Knowledge

| Skill ID | Skill | Expected deliverable |
|---|---|---|
| `research.technical` | Technical research | Technical findings report |
| `research.market` | Market intelligence | Market landscape assessment |
| `research.comparative` | Comparative research | Evidence-based comparison |
| `research.documentation` | Documentation analysis | Document findings and references |
| `research.standards` | Standards research | Standards applicability assessment |
| `research.literature` | Literature review | Research synthesis |
| `research.knowledge_mapping` | Knowledge mapping | Topic and dependency map |
| `research.evidence_collection` | Evidence collection | Provenance-aware evidence set |

**Existing implementation alignment:** Research capability.

**Priority:** Add source retrieval, provenance, and research validation through authorized tools.

**Quality requirements:** Relevant evidence, source attribution, uncertainty reporting, and clear distinction between research and inference.

### 5. Scout — Intelligence Validation

| Skill ID | Skill | Expected deliverable |
|---|---|---|
| `verification.fact_check` | Fact checking | Claim verification results |
| `verification.source_quality` | Source-quality assessment | Source reliability findings |
| `verification.corroboration` | Cross-source corroboration | Corroboration matrix |
| `verification.contradictions` | Contradiction detection | Conflicting claims report |
| `verification.provenance` | Provenance validation | Evidence traceability report |
| `verification.claim_extraction` | Claim extraction | Structured claims inventory |
| `verification.confidence` | Evidence confidence assessment | Qualified confidence findings |
| `verification.independent_review` | Independent review | Review and challenge report |

**Existing implementation alignment:** Fact checking and source quality.

**Priority:** Improve independent verification while preserving the current evidence-validation contracts.

**Quality requirements:** No invented verification, explicit unsupported claims, and reproducible review criteria.

### 6. Cipher — Security and Risk

| Skill ID | Skill | Expected deliverable |
|---|---|---|
| `security.risk_assessment` | Enterprise risk assessment | Risk register |
| `security.threat_modeling` | Threat modeling | Threat model |
| `security.cloud_posture` | Cloud security posture | Control-gap assessment |
| `security.data_security` | Data security and DSPM | Data-risk assessment |
| `security.agentic_ai` | Agentic AI security | Agent risk assessment |
| `security.iam` | Identity and access review | Privilege analysis |
| `security.zero_trust` | Zero Trust architecture | Control recommendations |
| `security.compliance_mapping` | Framework and control mapping | Control matrix |
| `security.privacy` | Privacy review | Privacy impact findings |
| `security.application_security` | Application security | Application risk findings |
| `security.incident_readiness` | Incident readiness | Response-readiness assessment |
| `security.guardrail_evaluation` | AI guardrail evaluation | Guardrail test report |

**Existing implementation alignment:** Security assessment and privacy review.

**Priority:** Expand into agentic AI security and DSPM, including the planned AWS/Cyera laboratory.

**Quality requirements:** Evidence-backed findings, appropriate severity, practical mitigations, and clear control ownership.

### 7. Atlas — Business Strategy and Logistics

| Skill ID | Skill | Expected deliverable |
|---|---|---|
| `strategy.business_case` | Business-case development | Business case |
| `strategy.roi_analysis` | ROI and financial analysis | Financial evaluation |
| `strategy.tco_analysis` | Total cost of ownership | Cost model |
| `strategy.transformation` | Transformation strategy | Transformation roadmap |
| `strategy.operating_model` | Operating-model design | Target operating model |
| `strategy.vendor_evaluation` | Vendor evaluation | Vendor comparison |
| `strategy.organizational_readiness` | Organizational readiness | Readiness assessment |
| `strategy.logistics` | Logistics planning | Resource and logistics plan |
| `strategy.portfolio_prioritization` | Initiative prioritization | Prioritized investment portfolio |
| `strategy.value_realization` | Value realization | Benefits and measurement plan |

**Existing implementation alignment:** Logistics capability declared but not connected.

**Priority:** Activate business-case development and feasibility analysis while retaining the established logistics specialization.

**Quality requirements:** Explicit assumptions, defensible calculations, operational feasibility, and measurable outcomes.

### 8. Pixel — Creative Intelligence

| Skill ID | Skill | Expected deliverable |
|---|---|---|
| `creative.presentation_design` | Presentation development | Presentation |
| `creative.architecture_visualization` | Architecture visualization | Architecture diagram |
| `creative.data_storytelling` | Data storytelling | Visual analytical narrative |
| `creative.ux_design` | User experience design | UX specification |
| `creative.information_design` | Information architecture | Structured information design |
| `creative.executive_communication` | Executive visual communication | Executive-ready visual artifact |
| `creative.technical_illustration` | Technical illustration | Technical visual |
| `creative.brand_consistency` | Visual identity consistency | Brand-compliant design |

**Existing implementation alignment:** Creative design capability declared but not connected.

**Priority:** Activate architecture diagrams and executive presentations.

**Quality requirements:** Technical accuracy, readability, accessibility, and fidelity to source findings.

### 9. Sage — Philosophy and Human Perspective

| Skill ID | Skill | Expected deliverable |
|---|---|---|
| `human.ethical_analysis` | Ethical analysis | Ethical impact assessment |
| `human.ai_governance` | AI governance | Governance recommendations |
| `human.change_management` | Change management | Change-management plan |
| `human.stakeholder_impact` | Stakeholder impact analysis | Human impact assessment |
| `human.organizational_culture` | Organizational culture | Culture and adoption assessment |
| `human.decision_frameworks` | Decision frameworks | Decision-support framework |
| `human.knowledge_transfer` | Knowledge transfer | Education and enablement plan |
| `human.training_design` | Training design | Learning objectives and curriculum |
| `human.adoption_strategy` | Technology adoption | Adoption strategy |
| `human.responsible_ai` | Responsible AI assessment | Responsible AI recommendations |

**Existing implementation alignment:** Philosophical analysis declared but not connected.

**Priority:** Activate AI governance and organizational change analysis.

**Quality requirements:** Balanced stakeholder analysis, explicit ethical trade-offs, actionable recommendations, and respect for human accountability.

### 10. Orbit — Engineering and Automation

| Skill ID | Skill | Expected deliverable |
|---|---|---|
| `engineering.enterprise_architecture` | Enterprise architecture | Architecture specification |
| `engineering.cloud_architecture` | Cloud architecture | Cloud reference design |
| `engineering.network_architecture` | Network architecture | Network design |
| `engineering.software_development` | Software engineering | Code and tests |
| `engineering.devops` | DevOps engineering | Delivery pipeline design |
| `engineering.infrastructure_as_code` | Infrastructure as code | IaC artifacts |
| `engineering.automation` | Workflow automation | Automation implementation |
| `engineering.integration` | Systems integration | Integration specification |
| `engineering.ai_platforms` | AI platform engineering | AI platform architecture |
| `engineering.agent_platforms` | Agent platform engineering | Agent runtime design |
| `engineering.testing` | Engineering validation | Test results |
| `engineering.deployment` | Deployment engineering | Deployment plan and results |
| `engineering.reliability` | Reliability engineering | Reliability assessment |
| `engineering.observability` | Observability engineering | Monitoring and tracing design |

**Existing implementation alignment:** Automation engineering declared but not connected.

**Priority:** Activate architecture review and controlled implementation capabilities.

**Quality requirements:** Reproducibility, testability, least-privilege execution, rollback planning, and accurate deployment reporting.

### 10a. Education Extension: Archie and Ada

*Proposed amendment, see [Education agents](../education-agents.md). These skills are `proposed` and executable by nobody.*

#### Archie — Knowledge and Learning

| Skill ID | Skill | Expected deliverable |
|---|---|---|
| `education.pr_analysis` | Pull request analysis | Pull request analysis notes |
| `education.engineering_history` | Engineering history | Engineering history summary |
| `education.knowledge_extraction` | Knowledge extraction | Provenance-tagged learning artifact |
| `education.memory_synthesis` | Memory synthesis | Synthesis of authorized knowledge |
| `education.institutional_knowledge` | Institutional knowledge curation | Institutional knowledge entries |
| `education.instructor_material` | Instructor material authoring | Instructor guide |
| `education.technical_explanation` | Technical explanation | Technical explanation |
| `education.lesson_delivery` | Lesson delivery | Lesson delivery plan |
| `education.socratic_teaching` | Socratic teaching | Socratic question sequence |
| `education.knowledge_validation` | Knowledge validation | Validation status report |

#### Ada — Curriculum and Learning Experience

| Skill ID | Skill | Expected deliverable |
|---|---|---|
| `education.curriculum_design` | Curriculum design | Curriculum structure |
| `education.lesson_authoring` | Lesson authoring | Lesson |
| `education.learning_objectives` | Learning objectives | Learning objectives |
| `education.prerequisite_mapping` | Prerequisite mapping | Prerequisite map |
| `education.exercise_generation` | Exercise generation | Exercise set |
| `education.assessment_design` | Assessment design | Assessment |
| `education.curriculum_versioning` | Curriculum versioning | Curriculum change record |
| `education.learning_progression` | Learning progression | Progression plan |
| `education.student_feedback` | Student feedback | Feedback on learner responses |
| `education.learning_analytics` | Learning analytics | Learning gap report |

### 11. Shared Skills

Some skills should be available to multiple agents without creating duplicate identities.

| Shared skill | Primary owner | Supporting agents |
|---|---|---|
| Executive advisory | Charlie | Atlas |
| Technical research | Nova | Scout, Orbit |
| Evidence validation | Scout | Nova |
| Security architecture | Cipher | Orbit |
| Enterprise architecture | Orbit | Cipher, Atlas |
| AI governance | Sage | Cipher, Atlas |
| Business-case development | Atlas | Charlie, Orbit |
| Technical visualization | Pixel | Orbit |
| Organizational transformation | Atlas | Sage, Charlie |
| Agentic AI security | Cipher | Orbit, Nova |
| Implementation planning | Orbit | Atlas, Charlie |
| Knowledge transfer | Sage | Nova, Pixel |

Supporting expertise does not automatically grant execution authority.

### 12. Skills Evaluation

Each enabled skill shall have automated evaluations appropriate to its purpose.

Evaluation dimensions include:

**Correctness:** Is the output accurate?

**Completeness:** Were required elements included?

**Evidence:** Are claims supported?

**Security:** Were authorization and data boundaries respected?

**Relevance:** Does the output address the task?

**Consistency:** Does the output conform to its schema?

**Efficiency:** Were resources used appropriately?

**Collaboration:** Were dependencies and peer-review requirements satisfied?

**Actionability:** Can the output support a real decision or implementation?

### 13. Registry Governance

New skills may be proposed by agents or human operators.

A skill shall not become executable until its implementation, permissions, tests, and execution binding have been approved.

Changes shall be version-controlled.

Removing or materially changing an existing skill shall require compatibility review.

The registry shall support future agents without requiring changes to the identities or histories of existing team members.

**End of Skills Registry v1.0**
