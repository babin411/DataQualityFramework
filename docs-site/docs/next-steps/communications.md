# Emails, Jira & review

Ready-to-edit drafts for the things the manager asked you to send or prepare. Replace the
`<…>` placeholders before sending.

## 1. Reply to the kickoff invite { #reply-email }

The manager's instruction: *"Just respond to the meeting invite that we had. Send them the exact
template for the input monitoring as well as the feature / rule / model score tracking. Those two
tables. Send it to the whole group."*

```text
To:       <everyone on yesterday's monitoring invite>
Subject:  RE: Data Quality Monitoring kickoff: templates and next steps

Hi all,

Thanks for joining yesterday's session on data quality monitoring. As discussed, attached are the
two templates we walked through:

  1. Input Monitoring template: one row per field; which checks apply (missing values, field
     population, acceptable range, accepted values, format, duplicates, length, type,
     placeholders), the alert criteria, and whether a failure should send an alert.
  2. Feature / Rule / Model Score Monitoring template: the benchmark and active tables, and the
     stability checks (CSI / PSI) for features and scores, plus rule activity.

What we need from you: for each table you own or use, please fill in both templates for the
fields that feed a model or a rule.

If there are check types you'd like that aren't in the template, reply and let me know and we'll
look at adding them.

@Srayasee: with the new models going live, I'm sure we'll want this tracking on their data
tables. Could you start filling in these templates for every table you'd like tracked? That will
let us build the first use case on Underwriting. I've raised <JIRA-KEY> to track it.

The reports will be hourly alerts plus a weekly summary. A first version is being built now, and
I'll share it once it's reviewed.

Thanks,
<name>
```

!!! tip "Keep the email short"
    The attachments carry the detail. The email states what you need, from whom, and by when. If
    people need a deadline, add one: "by <date>, so we can include your tables in the first release".

## 2. Jira ticket { #jira-ticket }

*"Create a Jira ticket for underwriting … and ask Shreya to update those two tables for all the
tables they need to track for input data and for feature monitoring, for underwriting new models
only."*

| Field | Value |
| --- | --- |
| **Project / board** | <team board> |
| **Type** | Task (or Story) |
| **Summary** | UW new models: provide DQ monitoring templates (input + feature/score) |
| **Assignee** | Srayasee Chakraborty (modelling) |
| **Reporter** | You |
| **Labels** | `data-quality`, `underwriting`, `monitoring` |
| **Attachments** | BRD (Word document); Input Monitoring template; Feature/Rule/Score template |
| **Linked** | The UW new-model deployment ticket(s) |

**Description:**

```text
Context
We are setting up proactive data quality monitoring (see attached BRD). Underwriting's new
models are the first use case.

Ask
For the UW new models only, fill in both attached templates for every table that should be
tracked:
  1. Input Monitoring: the API request table (model inputs). Fields used by a model or rule,
     with check types, alert criteria and whether to alert.
  2. Feature / Rule / Score Monitoring: the full UW output (5c historical table). Benchmark
     and active paths, final and component scores, top features, rules.

For each table, please include the full Snowflake path (DATABASE.SCHEMA.TABLE).

Acceptance criteria
- Both templates returned for every UW new-model table to be tracked
- Every field listed exists in the named table
- Criteria written in the template's format (e.g. "> 30%", "(<=0 >=15)", "('01','02')")
- Fields ranked by importance, so the top 5 per source can go live first
```

## 3. Reminder: resend the templates { #reminder-email }

From the [check-in](check-in.md#resend-the-templates): the first message wasn't clear enough. Resend
the templates as **two named files**, with one instruction and a request for an **ETA**.

```text
To:       Srayasee Chakraborty
Cc:       <manager>
Subject:  UW new models: two monitoring templates to fill in (ETA?)

Hi Srayasee,

Following up on the data quality monitoring templates. My earlier note wasn't clear about what to
fill in, so here it is again, more simply.

Attached are two files:

  1. Template to be filled - Input data monitoring
     One row per input field: which health checks apply, the alert criteria, and whether a
     failure should send an alert.
  2. Template to be filled - Feature, rule and score monitoring
     The benchmark and active tables, and the thresholds for features, rules and scores.

For each table you'd like us to track, please fill in both files. Each has a short "how to fill
this in" note and an example row at the top.

Could you start with the Underwriting models, since they go live first? And could you let me know
an ETA for when these could be completed? It will help us plan the build. This is tracked in
<JIRA-KEY>.

Happy to walk through the templates on a quick call if that's easier.

Thanks,
<name>
```

!!! tip "Before you send"
    - Attach the files themselves, or link each file by name. A bare SharePoint link was the problem
      the first time.
    - Add the ETA to the Jira ticket as the due date once you have it.

## 4. Friday review { #friday-review }

**Friday, 12:30–1:00 pm.** Thirty minutes, so show, don't tell.

| Minutes | Item | What to show |
| --- | --- | --- |
| 0–3 | Status | Email sent, Jira raised, deployment work this week |
| 3–8 | The stand-in table | Path, refresh rate, the ~10 variables and why each was chosen |
| 8–13 | Template → checks | A filled-in template; the validation output; one parsed criterion of each kind |
| 13–23 | **The fixed views** | Page 1 summary → page 2 main report → one drill-down. Both reports, if ready |
| 23–27 | Alerting | Lambda + SNS design; the approvals list and their status; a test email if available |
| 27–30 | Decisions | The questions below |

### Questions to bring

| # | Question | Why it matters |
| --- | --- | --- |
| Q1 | Fields as rows or check types as rows on page 2? | Fixes the main report layout |
| Q2 | Power BI for production, or stay with HTML? Which workspace? | Decides the report build |
| Q3 | Who receives alerts? One team list, or per table owner? | SNS subscriptions |
| Q4 | Which existing Lambda sends emails today? Who owns it? | Reuse versus building a new path |
| Q5 | Which AWS account should the DQ Lambda run in? | Drives the approvals |
| Q6 | ~~Is A1 the same as the UW API request table?~~ **Answered:** A1 is today's API structure, A2 the January one ([details](requirements-update.md#a1-and-a2)) | Aligns the kickoff with the 1:1 |
| Q7 | When can we expect Srayasee's templates? | Plans the switch from the stand-in to the real tables |

After the [check-in](check-in.md), **Q4** has an owner (Austin has the email Lambda) and **Q7** becomes the ETA request in the
[reminder](#reminder-email). Ongoing catch-ups are **every other day**, about 10 minutes.
