-- =================================================================================================
-- 03. Optional, once: let the framework publish its report as a Streamlit in Snowflake app.
--     Run in Snowsight as ACCOUNTADMIN, after 01_setup.sql.
--
-- Snowflake then hosts the report, so it keeps running whether or not the Lambda is: nothing to
-- run in AWS, no keys (the app uses its own Snowflake session), and access is granted by role.
--
-- After this, from dq-framework/:   make report-publish
-- It uploads app/streamlit_app.py, creates (or updates) the app DQF_DB.DQF.DQ_REPORT owned by
-- DQF_ROLE, and prints its address. Put that address in DQF_REPORT_URL (.env) and REPORT_URL
-- (infra/deploy.sh) so every alert links to it.
-- Safe to re-run.
-- =================================================================================================
use role ACCOUNTADMIN;

-- DQF_ROLE keeps the app's files in a stage and owns the app. The app runs with its owner's rights,
-- so it can read DQ_RESULT (which DQF_ROLE owns) and nothing DQF_ROLE cannot already read.
grant create stage on schema DQF_DB.DQF to role DQF_ROLE;
grant create streamlit on schema DQF_DB.DQF to role DQF_ROLE;

-- Who may open the report. Viewers need only these grants, not access to the results table itself.
-- Replace or extend with your team's role, e.g. a DQ_REPORT_VIEWER role granted to the team.
--   grant usage on database DQF_DB to role <TEAM_ROLE>;
--   grant usage on schema DQF_DB.DQF to role <TEAM_ROLE>;
--   grant usage on streamlit DQF_DB.DQF.DQ_REPORT to role <TEAM_ROLE>;   -- after make report-publish
