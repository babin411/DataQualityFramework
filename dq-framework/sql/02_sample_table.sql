-- =================================================================================================
-- 02. The sample table: DQF_DB.DQF.UW_A1_RECORD
--
-- One row per merchant application, shaped like the A1 record of the Underwriting US V2 models
-- (Implementation Support Document): the application fields, the status of each data source, a few
-- model features, and the model service's output (scores, model used, decision).
--
-- 60 days of made-up data, 30 applications an hour, ending at the start of the current hour (UTC).
-- Known problems are planted so the checks have something to find (listed at the bottom).
--
-- Run in Snowsight, after 01_setup.sql. Re-run it shortly before a demo: the data is placed relative
-- to the current hour, so the newest hours age. Safe to re-run (CREATE OR REPLACE).
-- =================================================================================================

use role ACCOUNTADMIN;
use warehouse DQF_WH;
use schema DQF_DB.DQF;
alter session set timezone = 'UTC';

create or replace table UW_A1_RECORD as
with clock as (
    select date_trunc('hour', current_timestamp()::timestamp_ntz) as DATA_UNTIL
),
hours as (
    select row_number() over (order by seq4()) as HOURS_AGO               -- 1 = the latest full hour
    from table(generator(rowcount => 1440))                                -- 60 days
),
slots as (
    select row_number() over (order by seq4()) as N
    from table(generator(rowcount => 30))
),
draws as (
    select
        h.HOURS_AGO,
        s.N,
        dateadd(hour, -h.HOURS_AGO, c.DATA_UNTIL)                                          as HOUR_START,
        -- the last 7 full days before today: where the drift is planted
        dateadd(hour, -h.HOURS_AGO, c.DATA_UNTIL) >= dateadd(day, -7, date_trunc('day', c.DATA_UNTIL)) as RECENT_WEEK,
        uniform(0::float, 1::float, random()) as R_PLANT,                  -- who gets a planted problem
        uniform(0::float, 1::float, random()) as R_CHANNEL,
        uniform(0::float, 1::float, random()) as R_EXCLUDED,
        uniform(0::float, 1::float, random()) as R_BUREAU,
        uniform(0::float, 1::float, random()) as R_BIZID,
        uniform(0::float, 1::float, random()) as R_CLN,
        uniform(0::float, 1::float, random()) as R_NEU,
        uniform(0::float, 1::float, random()) as R_DEVICE,
        uniform(0::float, 1::float, random()) as R_TAXID
    from hours h
    cross join slots s
    cross join clock c
    where h.HOURS_AGO not between 8 and 10                                 -- PLANTED: no applications for 3 hours
),
apps as (
    select
        d.*,
        'APP' || to_char(HOUR_START, 'YYMMDDHH24')
              || lpad(iff(HOURS_AGO = 6 and N > 26, N - 26, N)::varchar, 3, '0')       as APPLICATION_ID,   -- PLANTED h6: duplicates
        dateadd(second, uniform(0, 3599, random()), HOUR_START)                          as REQUEST_TS,
        case when R_EXCLUDED < 0.03 then 'ADDITIONAL_OUTLET'
             when R_EXCLUDED < 0.05 then 'WISO' end                                      as EXCLUSION_REASON,
        lpad(uniform(0, 9999, random())::varchar, 4, '0')                                as SSN_LAST4,
        iff(R_BUREAU < 0.92 and not (HOURS_AGO = 4 and R_PLANT < 0.75), 'CY', iff(R_BUREAU < 0.97, 'NH', 'ER'))
                                                                                         as BUREAU_INQUIRY_STATUS,  -- PLANTED h4
        iff(R_BIZID < 0.94, 'CY', 'NH')                                                  as BIZID_INQUIRY_STATUS,
        iff(R_CLN < 0.95, 'CY', 'NH')                                                    as CLN_INQUIRY_STATUS,
        iff(R_NEU < 0.95, 'CY', 'NH')                                                    as NEU_INQUIRY_STATUS
    from draws d
),
scored as (
    select
        a.*,
        -- the tax ID's last 4 equal the SSN's for most sole proprietors; missing for some
        case when R_TAXID < 0.08 then null
             when R_TAXID < 0.55 then SSN_LAST4
             else lpad(uniform(0, 9999, random())::varchar, 4, '0') end                  as TAX_ID_LAST4,
        -- which merchant fraud model can be used depends on which sources returned data (section 2d)
        case when BUREAU_INQUIRY_STATUS = 'CY' and BIZID_INQUIRY_STATUS = 'CY' and CLN_INQUIRY_STATUS = 'CY' then 'SS_MF_101'
             when BUREAU_INQUIRY_STATUS = 'CY' and BIZID_INQUIRY_STATUS = 'CY' then 'SS_MF_100'
             else 'CS_MF_100' end                                                        as MF_MODEL_USED_RAW,
        least(900, greatest(300, round(normal(650, 80, random()) - iff(RECENT_WEEK, 60, 0))))  as MF_RAW,  -- PLANTED: drift
        least(900, greatest(300, round(normal(640, 70, random()))))                      as IT_RAW,
        least(900, greatest(300, round(normal(620, 90, random()))))                      as CR_RAW
    from apps a
)
select
    APPLICATION_ID,
    REQUEST_TS,
    -- ---- application (from the onboarding system) ----
    decode(uniform(1, 3, random()), 1, 'C100', 2, 'C200', 'C300')                        as CLIENT_ID,
    case when RECENT_WEEK then (case when R_CHANNEL < 0.60 then 'API'                   -- PLANTED: channel mix drifts
                                     when R_CHANNEL < 0.80 then 'WEB' else 'POS' end)    --   to 60 / 20 / 20
         when R_CHANNEL < 0.3333 then 'WEB' when R_CHANNEL < 0.6667 then 'POS' else 'API' end as CHANNEL,
    case when HOURS_AGO = 7 and R_PLANT < 0.30 then 'M' || uniform(10000, 99999, random())::varchar      -- PLANTED h7
         else 'M' || uniform(100000000, 999999999, random())::varchar end                as MERCH_ID,
    decode(uniform(1, 5, random()), 1, '5411', 2, '5812', 3, '5999', 4, '7230', '8099')  as MCC,
    decode(uniform(1, 6, random()), 1, 'NJ', 2, 'NY', 3, 'CA', 4, 'TX', 5, 'FL', 'GA')   as STATE,
    case when HOURS_AGO = 5 and R_PLANT < 0.40 then decode(uniform(1, 3, random()), 1, 'ABCDE', 2, '1234', 'N/A')  -- PLANTED h5
         else uniform(10000, 99950, random())::varchar end                               as ZIP,
    dateadd(day, -uniform(0, 30, random()), HOUR_START::date)                            as ENQUIRY_DATE,
    (case when HOURS_AGO = 1 and R_PLANT < 0.60 then null                                -- PLANTED h1
          when uniform(0::float, 1::float, random()) < 0.03 then null
          else uniform(500, 900000, random()) / 100 * iff(RECENT_WEEK, 1.6, 1) end)::number(12, 2)  as AVG_TICKET,  -- PLANTED drift
    iff(uniform(0::float, 1::float, random()) < 0.05, null, uniform(10000, 5000000, random()))::number(14, 2) as ANNUAL_VOLUME,
    SSN_LAST4,
    TAX_ID_LAST4,
    case when HOURS_AGO = 3 and R_PLANT < 0.15 then 'Y'                                  -- PLANTED h3: not a valid value
         when HOURS_AGO = 2 and R_PLANT < 0.20 and TAX_ID_LAST4 is not null and TAX_ID_LAST4 <> SSN_LAST4 then '1'  -- PLANTED h2
         when SSN_LAST4 is null or TAX_ID_LAST4 is null then 'Z'
         when SSN_LAST4 = TAX_ID_LAST4 then '1' else '0' end                             as SSN_TAX_ID_MATCH,
    -- ---- data sources: did each one return data? (CY = yes) ----
    BUREAU_INQUIRY_STATUS,
    BIZID_INQUIRY_STATUS,
    CLN_INQUIRY_STATUS,
    NEU_INQUIRY_STATUS,
    iff(R_DEVICE < 0.80, 'TMX' || uniform(100000, 999999, random())::varchar, null)      as TMX_REQUEST_ID,
    iff(R_DEVICE < 0.80, iff(uniform(0::float, 1::float, random()) < 0.95, 'success', 'fail'), null) as TMX_REQUEST_RESULT,
    -- ---- model features ----
    uniform(0, 100, random())                                                            as AAPBIZ_SCORE_PRIN,
    iff(RECENT_WEEK, uniform(9, 100, random()), uniform(0, 100, random()))               as TXN_PER_DAY_PERCENTILE,  -- PLANTED: mild drift
    iff(uniform(0::float, 1::float, random()) < 0.12, 1, 0)                              as HOMEBASED_PHONE_FLAG,
    uniform(5, 60, random())                                                             as JAVASCRIPT_FONT_COUNT,
    -- ---- exclusions: the A1 model is not called for these, so they have no output ----
    EXCLUSION_REASON,
    iff(EXCLUSION_REASON is null, MF_MODEL_USED_RAW, null)                               as MF_MODEL_USED,
    iff(EXCLUSION_REASON is null, iff(HOURS_AGO = 11 and R_PLANT < 0.10, 950, MF_RAW), null) as MF_SCORE,   -- PLANTED h11
    iff(EXCLUSION_REASON is null, IT_RAW, null)                                          as IT_SCORE,
    iff(EXCLUSION_REASON is null, CR_RAW, null)                                          as CR_SCORE,
    case when EXCLUSION_REASON is not null then null
         when HOURS_AGO = 12 and R_PLANT < 0.15 then 'AUTO_APPROVE'                      -- PLANTED h12
         when MF_RAW >= 650 and IT_RAW >= 600 then 'AUTO_APPROVE'
         when MF_RAW < 450 then 'AUTO_DECLINE'
         else 'MANUAL_REVIEW' end                                                        as DECISION
from scored;

-- PLANTED h12 only matters where the score is low: make some of those approvals low-scored.
update UW_A1_RECORD
set MF_SCORE = 350
where DECISION = 'AUTO_APPROVE' and MF_SCORE >= 400
  and datediff('hour', REQUEST_TS, date_trunc('hour', current_timestamp()::timestamp_ntz)) = 12   -- hour 12
  and uniform(0::float, 1::float, random()) < 0.5;


-- When the data ends, so tests know which hour is which.
create or replace table DQF_SAMPLE_META as
select date_trunc('hour', current_timestamp()::timestamp_ntz) as DATA_UNTIL;

grant select on all tables in schema DQF_DB.DQF to role DQF_ROLE;


-- Rows per hour for the last 14 hours (hours 8, 9 and 10 should be missing). datediff('hour') counts
-- hour boundaries crossed, so a row at 11:30 is 1 hour before 12:00: hour 1.
select datediff('hour', REQUEST_TS, (select DATA_UNTIL from DQF_SAMPLE_META)) as HOURS_AGO, count(*) as APPLICATIONS
from UW_A1_RECORD
group by 1 having HOURS_AGO <= 14 order by 1;

-- -------------------------------------------------------------------------------------------------
-- What is planted ("hour N" = N hours before the start of the current hour), and what should catch it
--
--   hour 1      AVG_TICKET missing for 60%                     -> MISSING_VALUES on AVG_TICKET (alerts)
--   hour 2      SSN_TAX_ID_MATCH = '1' where SSN and tax ID differ -> rule SSN_TAX_ID_MATCH_DERIVED
--   hour 3      SSN_TAX_ID_MATCH = 'Y' (not a valid value)     -> ACCEPTED_VALUES and the rule
--   hour 4      Experian bureau returns no data for 75%        -> ACCEPTED_VALUES on BUREAU_INQUIRY_STATUS
--   hour 5      malformed ZIP codes for 40%                    -> EXPECTED_FORMAT on ZIP
--   hour 6      duplicated APPLICATION_IDs                     -> DUPLICATE_VALUES on APPLICATION_ID
--   hour 7      MERCH_ID too short for 30%                     -> VALUE_LENGTH on MERCH_ID
--   hours 8-10  no applications at all                         -> VOLUME; FRESHNESS once 2 hours pass
--   hour 11     MF_SCORE = 950, outside 300-900                -> rule SCORES_IN_RANGE
--   hour 12     auto-approved with MF_SCORE below 400          -> rule DECISION_MATCHES_SCORE
--
--   the last 7 days, against the benchmark (60 to 30 days ago), checked daily:
--     AVG_TICKET 60% higher                                   -> CSI fails
--     CHANNEL mix: 60% API instead of a third                 -> CSI fails
--     MF_SCORE 60 points lower                                -> PSI fails
--     TXN_PER_DAY_PERCENTILE never below 9                    -> CSI warns (about 0.15)
--     IT_SCORE, HOMEBASED_PHONE_FLAG unchanged                -> pass
-- -------------------------------------------------------------------------------------------------
