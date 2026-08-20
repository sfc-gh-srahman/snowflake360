-- Snowflake360 :: dedicated warehouse.
--
-- Everything Snowflake360 does runs here: the Streamlit app's queries, the nightly
-- landing rebuild, the curated dynamic table refreshes and the telemetry harvest.
-- One warehouse for the whole application is the point. It means the cost of
-- running Snowflake360 is a single line in WAREHOUSE_METERING_HISTORY that a
-- customer can read, budget and attribute without having to separate this app's
-- consumption from everything else sharing a general-purpose warehouse.
--
--   SELECT SUM(CREDITS_USED)
--     FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
--    WHERE WAREHOUSE_NAME = 'SF360_WH'
--      AND START_TIME >= DATEADD('day', -30, CURRENT_DATE());
--
-- ---------------------------------------------------------------------------
-- Sizing
-- ---------------------------------------------------------------------------
-- X-Small is a measurement, not a default. On the account this was developed
-- against, carrying roughly a year of ACCOUNT_USAGE and an organization of ~8,500
-- accounts:
--
--   * peak spilling across the whole pipeline was 0.32 GB to local storage and
--     ZERO to remote storage. Remote spilling is the signal that a warehouse is
--     too small for its workload, and there was none.
--   * every step of the six-task DAG finished in 21-50 seconds.
--   * the largest single operation, rebuilding LND_QUERY_ATTRIBUTION, scans about
--     19 GB and still completes inside 35 seconds.
--
-- Larger would finish the nightly run a little sooner and cost proportionally more
-- for the rest of the day, because a warehouse is billed for the time it is
-- running rather than the work it does. Change WAREHOUSE_SIZE below if your
-- account is materially larger, and check for remote spilling before deciding:
--
--   SELECT QUERY_ID, BYTES_SPILLED_TO_REMOTE_STORAGE
--     FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
--    WHERE WAREHOUSE_NAME = 'SF360_WH'
--      AND BYTES_SPILLED_TO_REMOTE_STORAGE > 0;
--
-- WAREHOUSE_TYPE is STANDARD rather than ADAPTIVE on purpose. Adaptive warehouses
-- are gated by region and edition, so requiring one would make this script fail to
-- install in accounts where it is unavailable. Adaptive is also a performance
-- feature rather than a cost reduction, so it would not lower the figure above.
-- Switch to it deliberately if it is available and the nightly run needs to be
-- faster.
--
-- AUTO_SUSPEND is 60 seconds because this workload is bursty: a nightly batch, plus
-- whatever interactive querying the app does when somebody opens it. Idle time
-- between those bursts is pure waste, and nothing here benefits from a warm cache
-- held for minutes. INITIALLY_SUSPENDED means creating the warehouse costs nothing.
--
-- MAX_CLUSTER_COUNT is 1 because the DAG is strictly sequential and the app serves
-- a handful of concurrent readers. Raise it only if several people use the app at
-- once and queries start queuing.

CREATE WAREHOUSE IF NOT EXISTS SF360_WH
  WAREHOUSE_TYPE      = 'STANDARD'
  WAREHOUSE_SIZE      = 'XSMALL'
  AUTO_SUSPEND        = 60
  AUTO_RESUME         = TRUE
  INITIALLY_SUSPENDED = TRUE
  MIN_CLUSTER_COUNT   = 1
  MAX_CLUSTER_COUNT   = 1
  COMMENT = 'Snowflake360: all app queries, nightly landing/curated refresh, and dynamic table refreshes. Dedicated so the cost of running Snowflake360 is attributable to one warehouse. Sized X-Small from measured load: peak pipeline spill is 0.32 GB local and zero remote, and each DAG step completes in under a minute.';

-- The app role needs USAGE to query and OPERATE to resume the warehouse when the
-- app is opened while it is suspended.
GRANT USAGE, OPERATE ON WAREHOUSE SF360_WH TO ROLE SF360_APP_ROLE;

-- APP_WAREHOUSE is recorded in CONFIG.SETTINGS by config_seed.sql, not here.
-- Writing it from this file made the warehouse depend on the CONFIG schema, which
-- does not exist yet at this point in the install -- the first thing a cold run of
-- setup.sql failed on.

SHOW WAREHOUSES LIKE 'SF360_WH';
