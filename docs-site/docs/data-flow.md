# End-to-end data flow

The diagram is split into three horizontal bands inside the AWS boundary. The numbers below match the
numbered arrows on the diagram.

```mermaid
flowchart TB
    EXT["External service<br/>(MRD, RASI, ...)"]:::ext
    SFcfg[("Snowflake<br/>config tables")]:::snow
    SFhist[("Snowflake<br/>historical table")]:::snow

    subgraph AWS["UW AWS Architecture"]
        direction TB

        subgraph BAND1["Asynchronous process &mdash; config caching"]
            direction LR
            L1["SF2AWS Config Table<br/>Pipe Lambda"]:::lambda
            S3ref[("S3<br/>reference bucket")]:::s3
        end

        subgraph BAND2["Real-time process &mdash; decisioning"]
            direction LR
            ALB["ALB"]:::alb
            POST["Post-ALB Lambda"]:::lambda
            SM["Step Functions<br/>state machine<br/><i>(orchestration)</i>"]:::sfn
            RE["Rules Engine Lambda<br/><i>(final orchestration step)</i>"]:::lambda
        end

        subgraph BAND3["Asynchronous process &mdash; transaction logging"]
            direction LR
            S3log[("S3<br/>transaction log bucket")]:::s3
            L2["AWS2SF Trans Log<br/>Pipe Lambda"]:::lambda
        end
    end

    %% Band 1: config caching (read side, async)
    SFcfg -->|ingest reference data from config tables| L1
    L1 -->|cache reference data in S3| S3ref

    %% Band 2: real-time decisioning
    EXT -->|1 - API request| ALB
    ALB -->|2 - state machine invocation| POST
    POST --> SM
    S3ref -->|3 - read reference data| SM
    SM -->|4 - state machine runs| RE
    SM -.->|6 - state machine response| POST
    POST -.-> ALB
    ALB -.->|7 - API response| EXT

    %% Band 3: transaction logging (write side, async)
    %% 5a now originates from the Rules Engine Lambda, not the state machine construct
    RE -->|5a - write Full UW output structure| S3log
    S3log -->|5b - new output file triggers async write| L2
    L2 -->|5c - write Full UW output to Snowflake| SFhist

    classDef lambda fill:#BC5618,stroke:#B5531E,color:#fff;
    classDef s3 fill:#54802D,stroke:#4C7A26,color:#fff;
    classDef snow fill:#127EA6,stroke:#1877A3,color:#fff;
    classDef sfn fill:#C93C92,stroke:#8E2E68,color:#fff;
    classDef alb fill:#8A4CFF,stroke:#5B2EB5,color:#fff;
    classDef ext fill:#2E3B4E,stroke:#1a2330,color:#fff;
```

The **Step Functions state machine** above expands into the layered orchestration described in
[Decisioning lanes & layers](decisioning-lanes.md).

!!! tip "The model team's view of the same flow"
    The [Implementation Support Document](next-steps/implementation-support-doc.md#the-process-flow-section-0)
    describes the request path as ten steps, from MRD collecting the data to the response on screen,
    with the owner and failure handling of each.

## Band 1: Asynchronous config caching (top)

| Arrow                                      | From → To                                                     | What happens                                                  |
| ------------------------------------------ | ------------------------------------------------------------- | ------------------------------------------------------------- |
| _Ingest reference data from config tables_ | Snowflake config tables → **SF2AWS Config Table Pipe Lambda** | Lambda runs each query from its state file against Snowflake. |
| _Cache reference data in S3_               | Lambda → **S3 (reference bucket)**                            | Changed results (by fingerprint) are written as files to S3.  |

This runs independently of any request (scheduled or triggered, **(inferred)**: the trigger is not shown).

## Band 2: Real-time decisioning (middle)

```mermaid
sequenceDiagram
    autonumber
    participant X as External service (MRD, RASI...)
    participant A as ALB
    participant P as Post-ALB Lambda
    participant S as Step Functions state machine
    participant RE as Rules Engine Lambda (in state machine)
    participant R as S3 reference cache
    participant T as S3 transaction logs
    X->>A: API request
    A->>P: forward
    P->>S: start execution (state machine invocation)
    S->>R: read reference data
    S->>S: run Normalizer → lane models → aggregates
    S->>RE: Rules Engine (render final UW decision)
    RE->>T: 5a. write Full UW output structure
    RE-->>S: decision
    S-->>P: state machine response
    P-->>A: response
    A-->>X: API response (UW decision)
```

1. **API Request**: an external service (the diagram lists _MRD, RASI, etc._) sends an underwriting
   request (e.g. a merchant application) to the **Application Load Balancer**.
2. **State Machine Invocation**: the ALB forwards to the **Post-ALB Lambda**, which validates/shapes the
   payload **(inferred)** and starts the **AWS Step Functions** state machine.
3. **Read Reference Data**: the state machine reads the cached config from S3 (mappings such as TMX code
   categories, MCC stats).
4. **State Machine runs**: orchestration of the decisioning layers. The normalizer runs first, then
   the three lanes in parallel, then the rules engine. See [lanes & layers](decisioning-lanes.md) for the
   concepts and [Inside the state machine](state-machine.md) for every state and Lambda.
5. **5a. Write Full UW output structure**: the **Rules Engine Lambda** (the final step in the
   orchestration) writes the full UW output structure — inputs, scores and decision — directly to the S3
   transaction-log bucket. _(Updated: this write now originates from the Rules Engine Lambda itself, not
   from the state-machine construct.)_
6. **State Machine Response**: the result returns to the Post-ALB Lambda.
7. **API Response**: the decision goes back through the ALB to the caller.

Because the response returns synchronously, the Step Functions workflow is most likely an
**Express workflow** invoked synchronously **(inferred)**.

## Band 3: Asynchronous transaction logging (bottom)

!!! note "The request is logged too"
    Per the manager, the **API request (step 1)** is also written to a Snowflake table, although the
    diagram doesn't show it. That table is the **input** for data quality monitoring; the 5c historical
    table is the **output**. See [What to monitor](next-steps/what-to-monitor.md).

| Arrow  | From → To                                         | What happens                                                                             |
| ------ | ------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| **5a** | **Rules Engine Lambda** → **S3 (transaction log bucket)** | The Rules Engine writes the **full UW output structure** per decision. _(Updated: previously drawn from the state machine.)_ |
| **5b** | S3 → **AWS2SF Trans Log Pipe Lambda**             | A _new output file_ (S3 event notification) triggers the async write.                    |
| **5c** | Lambda → **Snowflake historical table**           | The full UW output is written to a historical table for analytics, monitoring and model retraining. |

## Data direction summary

```mermaid
flowchart LR
    SFc[(Snowflake config)] -->|SF → AWS| AWS[AWS real-time service]
    AWS -->|AWS → SF| SFh[(Snowflake history)]
```

- **SF2AWS** = Snowflake **to** AWS (config/reference data, read side).
- **AWS2SF** = AWS **to** Snowflake (transaction logs, write side).
