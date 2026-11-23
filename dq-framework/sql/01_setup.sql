-- =================================================================================================
-- 01. One-time Snowflake set-up for the data quality framework. Run in Snowsight as ACCOUNTADMIN.
--
-- Creates everything it needs, so it does not depend on any existing database:
--   database  DQF_DB              owned by ACCOUNTADMIN
--   schema    DQF_DB.DQF          holds the sample table and the results table
--   warehouse DQF_WH              extra-small, suspends after 60 seconds
--   role      DQF_ROLE            read the monitored tables, write only the results table
--   user      DQF_SVC             a service user (no password) that logs in with a key pair
--
-- Before running: create the key pair with  scripts/make_snowflake_key.sh  and paste the PUBLIC key
-- it prints into the ALTER USER statement at the bottom (one line, without the BEGIN/END lines).
-- Safe to re-run.
-- =================================================================================================

use role ACCOUNTADMIN;

create warehouse if not exists DQF_WH
    warehouse_size = 'XSMALL' auto_suspend = 60 auto_resume = true initially_suspended = true;
use warehouse DQF_WH;

-- A database of its own: an existing one (such as MY_DB) may belong to a role ACCOUNTADMIN cannot use.
create database if not exists DQF_DB comment = 'Data quality framework';
create schema if not exists DQF_DB.DQF;

create role if not exists DQF_ROLE;
grant usage on warehouse DQF_WH to role DQF_ROLE;
grant usage on database DQF_DB to role DQF_ROLE;
grant usage on schema DQF_DB.DQF to role DQF_ROLE;
-- read every table in the schema, now and later
grant select on all tables in schema DQF_DB.DQF to role DQF_ROLE;
grant select on future tables in schema DQF_DB.DQF to role DQF_ROLE;
-- create the results table (the framework does it on its first run, or `dqf init`)
grant create table on schema DQF_DB.DQF to role DQF_ROLE;
-- keep ACCOUNTADMIN above the new role, as Snowflake recommends
grant role DQF_ROLE to role SYSADMIN;

-- If a statement below fails, these show what exists:
--   show users like 'DQF_SVC';      show roles like 'DQF_ROLE';      select current_user();

-- The service user the framework logs in as.
create user if not exists DQF_SVC
    type = SERVICE
    default_role = DQF_ROLE
    default_warehouse = DQF_WH
    default_namespace = DQF_DB.DQF
    comment = 'Data quality framework (Lambda and local runs)';
grant role DQF_ROLE to user DQF_SVC;

-- Let your own user use the role too, for Snowsight and the report.
-- Your user name is wrapped in double quotes: a name with a dot or lower-case letters (j.babin411)
-- is otherwise read as DB.OBJECT in upper case, and the grant fails with "Object does not exist".
-- Run these two lines together: the variable only exists in the session that set it.
set me = '"' || current_user() || '"';
grant role DQF_ROLE to user identifier($me);

-- The public key printed by scripts/make_snowflake_key.sh:
alter user DQF_SVC set rsa_public_key = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAokWGJwjwDKZGoYQNaw/n/QMjnbXEZ3IlqSPEQWS24LMkJUACVsvom7V2zjGEWhp6snVYpZ/4I35mPFk2lClq948T50B1bzUX2ULHWxmGvT7dnpjiybdU+Se1BVWV0GbKq5EjRwhyQ/4phAbXUHcf4wBUnv8KbrghR9ZsM0nbOXCyy5IM8L/HspkHdZ8u1agbVQ3WX4fPDyhWFIkdcMbUPWR4wCpbnJvEx6HPIU02icXkhGHT5L7at9BO+E4uG43SR8eyAVVWahlQ1m6WZ2sEV2mQoI1cfZReiqs0M0h2H5vHK6iK4pRljWbh5vZaVZ/DadnwOcEqdZhyXnXWxIvImwIDAQAB';

-- Check: RSA_PUBLIC_KEY_FP here must match the fingerprint the script printed.
desc user DQF_SVC;
