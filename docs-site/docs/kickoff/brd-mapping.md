# From kickoff to BRD

The [BRD](../data-quality/index.md) is the engineer's first deliverable from this meeting. This page
checks it against what the manager asked for. It also notes what the meeting reveals about the
Underwriting service documented elsewhere on this site.

## Coverage: what the meeting asked for vs the BRD draft

Statuses: **Covered** · **Partly** · **Missing** · **Later BRD** (planned for a later part of the series).

| # | Asked for in the meeting                                                        | BRD draft status | Notes                                                                                           |
| - | ------------------------------------------------------------------------------- | :--------------: | ----------------------------------------------------------------------------------------------- |
| 1 | Be proactive; monitor three layers                                              | **Covered**      | Executive summary and problem statement match closely; the BRD series follows the three layers. |
| 2 | Reusable across UW, Risk Monitoring, Clover Capital                             | **Covered**      | Stated as a single modular architecture.                                                         |
| 3 | Day 1 monitors the **A1** dataset; per-source datasets on Day 2                 | **Missing**      | The BRD has a placeholder Snowflake path. Naming A1 as the first target would make Phase 1 concrete. |
| 4 | Only fields **used in the model or rules**                                      | **Missing**      | An important scope limit (TMX: about 50 of 2,000 fields). Worth stating explicitly.               |
| 5 | Quick win: **top 5 fields per source** first                                    | **Missing**      | A natural Phase 1a.                                                                               |
| 6 | Alert on a rise in **"no hit" responses** (e.g. Experian)                       | **Partly**       | No check type fits exactly. A "no hit" is a **response status**, not a null value, so Missing Values won't catch it. Needs a rate check on the no-hit flag. |
| 7 | Detect **new enumerations**                                                     | **Partly**       | The **Accepted Values** check type covers it once each field's allowed values are listed.        |
| 8 | **Drift** in application fields (avg ticket, annual sales)                      | **Conflict**     | See [below](#the-one-real-conflict-drift-on-input-data).                                         |
| 9 | Alerts: when, **who**, what **volume**                                          | **Partly**       | Frequency is set (hourly). Recipients and expected volume are not.                               |
| 10| A **"generate report"** button for stakeholders, including FSI/PSI              | **Partly**       | The BRD has a **scheduled weekly** report. The **on-demand** report isn't mentioned.             |
| 11| PSI with **10 score bands**; decide how often the baseline is refreshed          | **Partly**       | Benchmark is "Date". Band count and refresh cadence are open.                                    |
| 12| Component scores and final score; raw and weighted versions                     | **Partly**       | The Feature_Scores tab lists FICO, Phone Health, IDT Score, etc., but doesn't separate component from final, or raw from weighted. |
| 13| Read features, scores and rules from the **model service JSON output**          | **Missing**      | The BRD refers to generic "Snowflake paths". The meeting names one consistent source.           |
| 14| A **reference table** (table, field, check) so a new source needs only new rows | **Partly**       | The spreadsheet is effectively this table. The BRD could say so outright.                        |
| 15| Performance definition: closed / terminated = bad                                | **Later BRD**    | Planned "Model Performance Monitoring" part.                                                      |
| 16| AUC, Gini, KS, % bads captured at a configurable cutoff                          | **Later BRD**    | Same.                                                                                             |
| 17| Rule hit / no-hit bad rates                                                      | **Later BRD**    | Planned "Rule Monitoring" part.                                                                   |

## The one real conflict: drift on input data

- **The meeting:** the whiteboard lists **drift analysis** directly under the **input data** sources,
  and the manager gives average ticket and annual sales as examples.
- **The BRD:** Part 1 says input monitoring involves **no comparison** between a benchmark population
  and the active population. Drift is moved to feature & score monitoring.

Both are defensible. Average ticket and annual sales are raw inputs **and** model features, so drift on
them could live in either layer. But the BRD should either include input drift, or say clearly that it
was moved to Part 2 and why, so the manager doesn't think it was dropped.

## Questions the meeting already answers

Several items in the [BRD review notes](../data-quality/review-notes.md) are settled, or nearly, by what
the manager said:

| Review note                                    | What the meeting says                                                                         |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------- |
| [#1 Blocking or after-the-fact](../data-quality/review-notes.md#1-blocking-check-or-after-the-fact-monitoring) | Monitoring reads the model service's **JSON output**, which exists only **after** a decision. So it is **after-the-fact** monitoring. BO-01's "before being consumed by models" should be reworded. |
| [#5 Availability checks](../data-quality/review-notes.md#5-add-availability-checks) | The "no hit" rate is exactly this kind of check: data that silently stops arriving.          |
| Tool choice ([implementation guide](../data-quality/implementation.md#choosing-between-them)) | The manager names **Great Expectations**, which tips the choice toward Option A.        |
| [#4 FSI/CSI criteria](../data-quality/review-notes.md#4-clarify-what-the-fsicsi-criteria-measure) | The manager describes PSI as **10 score bands, baseline % vs new %**: an index, not a directional percentage. |

## What the meeting reveals about the Underwriting service { #what-the-meeting-reveals-about-the-underwriting-service }

The whiteboard's onboarding flow is the [Underwriting decisioning service](../data-flow.md) seen from the
business side. It fills in a few gaps:

| From the meeting                                                       | Matches on this site                                                                                            |
| ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| **MRD (formerly Diligence)** sends the application by API               | The "External service (MRD, RASI, …)" at [step 1](../data-flow.md).                                               |
| Model service output: **JSON with header, scores, features, decision** | The **"Full UW output structure"** the rules engine writes to S3 at [step 5a](../state-machine.md#phase-5-uw-rules-engine-cert-lambda-then-end), then loaded into the Snowflake history table. **This is the data the monitoring platform should read.** |
| **Component scores**, one per available source                         | The [primitive Lambdas](../state-machine.md#all-the-function-names), e.g. `idt-lexis-neustar-cert`.              |
| **Final score = linear combination** of the components                 | The lane **aggregate** Lambdas (`idt-aggregate-cert`, …). New detail: the aggregate is a **weighted sum**, with weights depending on which sources were available **(inferred)**. |
| **Up to 5 scores for IDT, up to 3 for credit**                         | IDT has **4** sources + 1 final = **5**; Credit has **2** sources + 1 final = **3**. This matches the state machine exactly **(inferred)**. MF would also be 5. |
| Raw score (probability) and a weighted score                          | Not yet documented on the UW pages.                                                                              |

## Suggested additions to the BRD

1. Name **A1** as the Day 1 data source and describe the Day 2 per-source datasets.
2. Add the scope rule: **only fields used in the model or rules**, starting with the **top 5 per source**.
3. Add a **"no hit rate"** check type (a rate of a response status, compared over time).
4. Resolve the **input drift** conflict above.
5. Name the **model service JSON output** (through the Snowflake history table) as the source for
   features, scores and rules.
6. Add an **on-demand report** requirement alongside the weekly schedule.
7. List **alert recipients** and an expected **alert volume** per day.
8. In the Feature_Scores tab, separate **component vs final** scores and **raw vs weighted**.
