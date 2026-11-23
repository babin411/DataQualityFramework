# What was discussed

The meeting, organised by topic rather than in the order things were said. Quotes are paraphrased from
the transcript and lightly cleaned up.

## 1. Why this project exists

- **Monitoring has historically been weak.** Problems reach the team only when the business notices a
  rule behaving oddly: "Hey, I saw an uptick in this rule. Can you look into it?"
- **The business only sees rules.** Stakeholders don't get the model scores or features today, so a
  problem upstream is invisible until it changes a rule outcome.
- **The goal is to be proactive,** both internally and externally: find data problems quickly, and tell
  the business before they notice.
- The manager calls this "a vision since day one" that the team never got to.

## 2. Input data (layer 1)

**What to monitor first.** For the initial release the input is the **A1** dataset: a consolidated set
of fields the team has needed in the past, chosen because it's easy to deploy. Later, in the "native
deployment" (**Day 2**), each data source gets **its own dataset**: LexisNexis, GNF response,
application, and so on.

**The tool.** The manager specifically mentions **Great Expectations**.

**What should trigger an alert.** Examples given:

| Trigger                         | Example                                                    | Why it matters                                                                                  |
| ------------------------------- | ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| **Rise in "no hit" responses**  | More applicants come back from **Experian** with no record | A broken integration (for example, badly formatted names sent to the bureau) or a population change. Either way the model loses an input. |
| **Drift in application fields** | **Average ticket** or **annual sales** shifts over time    | The applicants look different from before.                                                     |
| **New enumerations**            | A categorical field gets a value **never seen before**     | The model may handle an unknown category badly and produce an **unexpected value**.            |
| **Population change**           | The mix of applicants changes                              | The model was built on a different population.                                                  |

**Scope: only the fields that matter.** Monitor only fields **used in the model or in rules**. TMX alone
sends about **2,000 fields** and the model uses about **50**. Two consequences:

- **Phase 1 can start now**, even before the new model is deployed, because the used fields are already
  known.
- **Quick win:** start with the **five most important fields per data source**, rather than all of them
  (around 250).

## 3. Alerts

The manager lists three open questions (circled on the board):

- **When** to alert?
- **Who** to alert?
- **What volume** of alerts to expect? Too many and they get ignored.

Alerts go to **the team** (internal). The team then tells stakeholders when something matters:
"I noticed you had an increase here; that's something we should keep an eye on."

## 4. Features & scores (layer 2)

**Features** are either **raw** fields or **transformations** of them. This applies to the **new model
only**, not the legacy one.

**How an application gets scored.** This is the centre of the whiteboard
([redrawn here](whiteboard.md#d-the-onboarding-flow-centre)):

1. An onboarding application comes into **MRD** (previously called **Diligence**), the platform for
   decisioning and managing applications.
2. It lands in a table, calls third-party data, and comes back into MRD.
3. MRD sends an **API** request to the **model service**.
4. The model service **retrieves the model**, works out **which features it needs** and calculates
   them, scores, then runs the **rules** to reach a **decision**.
5. It returns one **JSON object** per application: a **header**, the **scores**, the **features** and
   the **decision**. The manager calls this "a really rich data set".

**Scores are plural.** There are three model suites: **identity theft**, **merchant fraud** and
**credit risk**, so at minimum three scores. Within each suite:

- The service checks **what data it has**: LexisNexis, the bureau, the application, TMX.
- For each available source it produces a **component score**.
- The **final score** is a **linear combination** (a weighted sum) of the component scores.
- Identity theft can have **up to five** scores; credit **up to three**.
- Each score comes as a **raw score** (a probability) and a **weighted** version.

**Stability measures.** For features, a **Feature Stability Index (FSI/CSI)**. For scores, a
**Population Stability Index (PSI)**: split scores into **10 bands** and compare the share of the
**baseline** population in each band with the share of the **new** population. The manager is "not the
biggest fan" of these indices, but they exist and stakeholders expect them in reports. Open questions:
how often is the baseline updated (monthly?), and should the team watch **drift** or **changes in
values**? The engineer is asked to **recommend** what's best.

The PSI calculation is worked through on the
[Feature & score monitoring](../data-quality/feature-score-monitoring.md#how-csi-and-psi-are-calculated)
page.

## 5. Reporting

There is a formal **report to stakeholders**, and the manager wants this system to produce it. The
picture is a **"generate report" button** that outputs things like FSI and PSI on demand. This is
separate from the internal alerts.

## 6. Model & rule performance (layer 3) { #model-rule-performance }

This layer needs a **feedback loop**: outcomes for accounts that were approved.

**The performance definition.** For every approved application, check whether the account was later
**closed or terminated**. Those are **bad**. Accounts that were not closed or terminated are **good**.
This definition has to be coded up, and other data will be needed to support it. Outcomes take time to
appear, which is the "six-month lag".

**Model metrics** named on the board:

| Metric                   | What it measures                                                                                     | How to read it                                   |
| ------------------------ | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| **AUC**                  | The chance that a randomly chosen bad account gets a riskier score than a randomly chosen good one.   | 0.5 = no better than chance; 1.0 = perfect.      |
| **Gini**                 | The same information rescaled: `Gini = 2 × AUC − 1`.                                                 | 0 = no better than chance; 1 = perfect. AUC 0.75 → Gini 0.50. |
| **KS**                   | The biggest gap, at any score cutoff, between the share of all bads flagged and the share of all goods flagged. | KS 0.40 means at the best cutoff you flag 40 percentage points more of the bads than of the goods. |
| **% bads captured at 5%**| Take the **riskiest 5%** of applicants by score. What share of **all** bads are in that group?       | Higher is better. A random model captures about 5%. |

**Worked example (illustrative):** 10,000 approved accounts, of which 200 turned bad. Sort by risk
score and take the riskiest 5%, which is 500 accounts. If 60 of the 200 bads are among them, the model
**captures 30% of bads in 5% of the population**, six times better than random. The manager wants the
**5% to be configurable**: credit might use 1% instead.

### Rule performance { #rule-performance }

For each rule, compare the accounts where it **fired (hit)** with those where it **didn't (no hit)**:

| Rule R (illustrative) | Accounts | Bad (closed / terminated) | Good  | Bad rate  |
| --------------------- | -------: | ------------------------: | ----: | --------: |
| **Hit**               | 400      | 40                        | 360   | **10.0%** |
| **No hit**            | 9,600    | 160                       | 9,440 | **1.7%**  |

Accounts that hit this rule go bad about **six times** as often as those that don't, so the rule is doing
its job. If the two bad rates converge over time, the rule is losing value.

!!! note "A catch with declined applications (my addition)"
Outcomes only exist for **approved** accounts. A rule that **declines** an application leaves no
outcome to measure, so this table works best for rules that flag or refer without fully blocking. It is
worth raising with the reviewers when asking how they measure rule performance today.

The manager suggests asking the two reviewers how they currently report on performance, and expects this
layer to take **the next four to six months**.

## 7. Build it once, reuse it everywhere

- The model service's **JSON output has the same structure** across all three solutions, so features,
  scores and rules can always be read from it. "That's fantastic."
- **Different per solution:** the **definition of bad** (Underwriting vs Clover Capital), and the
  **input data**.
- **Configure, don't code.** Keep a **reference table** listing, for each data source, *which table,
  which fields, and which checks*. Adding a data source should mean **updating that table and nothing
  else**. The manager points to Austin's [feature store](../feature-store/index.md) as the example of
  doing this well.
- "Think: what happens if I swap out a data source? How will that break everything here?" Because "we're
  never going to be static as a company".

## 8. What Clover Capital is

Clover Capital offers **credit** to merchants. It starts from the merchants already covered by **risk
monitoring**: the team decides whether to **pre-approve / pre-qualify** a merchant for an offer. When a
merchant then applies, the application goes through **underwriting**, with a **different set of
underwriting models** from the ones documented on this site.

## Transcript corrections

The speech-to-text made a number of mistakes. These are my readings:

| In the transcript                          | Most likely meant                                   |
| ------------------------------------------ | --------------------------------------------------- |
| "my tricks" (2,000 fields)                 | **TMX** (ThreatMetrix)                              |
| "no hit responses from experience"         | from **Experian** (the bureau)                      |
| "new animations"                           | **new enumerations** (unseen category values)       |
| "Clown Capital"                            | **Clover Capital**                                  |
| "CFI or a feature stability index"         | **CSI** / FSI                                       |
| "fields in model or intervals"             | fields in model **or in rules** (as on the board)   |
| "a six month black"                        | a six-month **lag**                                 |
| "write articles"                           | **raw attributes** **(inferred)**                   |
| "bad termite", "a battery"                 | **bad / terminated**, **bad rate** **(inferred)**   |
| "what's monitoring"                        | **risk monitoring** **(inferred)**                  |
| "Shira's", "Shirafsi"                      | a reviewer's name; unclear                          |
