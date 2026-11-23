"""Data quality report. Reads only the results table.

Runs in two places with the same code:
- in Snowflake (Streamlit in Snowflake): `make report-publish`; it uses the app's own session.
- on a laptop: `make app`; it logs in with the SNOWFLAKE_* settings from .env.

It does not import the dqf package, so it can be uploaded (or pasted) into Snowflake as one file.

One run checks one window of one schedule: an hour (hourly) or a day (daily). Record-level checks
judge every row of the window and fail when too many rows break the rule; file-level checks compute
one number for the whole window (row count, hours since the newest row, PSI / CSI).

Interactive: clicking a chart sets the filters in the sidebar (result, level, source, check type,
field, window), and every chart and table follows them. Clicks need Streamlit 1.35+ and Altair 5+;
with older versions the charts still show, and the sidebar filters still work.
"""
import inspect
import json
import os

import altair as alt
import pandas as pd
import streamlit as st


def _load_dotenv(path: str = ".env") -> None:
    if os.path.isfile(path):
        for line in open(path).read().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()
RESULT_TABLE = os.environ.get("DQF_RESULT_TABLE", "DQ_RESULT")      # in Snowflake: DB.SCHEMA.TABLE
DAYS = int(os.environ.get("DQF_REPORT_DAYS", "14"))
WINDOWS = int(os.environ.get("DQF_REPORT_WINDOWS", "48"))          # windows shown in the "across windows" charts
QUERY = (f"select * from {RESULT_TABLE} "
         f"where WINDOW_END >= dateadd(day, -{DAYS}, current_timestamp()::timestamp_ntz)")
NO_ROWS = 999999                                                    # what a check with no rows to judge stores

STATUS_LABEL = {"PASS": "Passed", "WARN": "Warning", "FAIL": "Failed", "ERROR": "Could not run"}
STATUS_COLOUR = {"Passed": "#2e7d4f", "Warning": "#d4a017", "Failed": "#c0392b", "Could not run": "#8e8e8e"}
ICON = {"PASS": "🟢 Passed", "WARN": "🟡 Warning", "FAIL": "🔴 Failed", "ERROR": "⚪ Could not run"}
# File level: one number for the whole window. Every other metric is a share of rows (record level).
FILE_METRICS = {"row_count", "hours_since_last", "stability_index"}
TYPE_MEANING = {
    "VOLUME": "The window has an expected number of rows",
    "FRESHNESS": "The newest row is recent enough",
    "MISSING_VALUES": "The field is empty (NULL, or a token that means empty)",
    "FIELD_POPULATION": "The field must be filled in",
    "ACCEPTABLE_RANGE": "The number lies between a minimum and a maximum",
    "ACCEPTED_VALUES": "The value is one of an allowed list",
    "PLACEHOLDER_VALUE": "The value is a placeholder instead of real data",
    "EXPECTED_FORMAT": "The value matches a pattern (regular expression)",
    "VALUE_LENGTH": "The value has an allowed number of characters",
    "DUPLICATE_VALUES": "The value appears only once in the window",
    "SOURCE_COVERAGE": "Every row has a partner in the compared source",
    "MATCHES_SOURCE": "The value equals the compared source's value",
    "LOST_IN_MAPPING": "A value in the compared source is not lost on the way",
    "BUSINESS_RULE": "A business rule (when / then) holds",
    "PSI": "The score's distribution is close to the benchmark",
    "CSI": "The feature's distribution is close to the benchmark",
}

st.set_page_config(page_title="Data quality", page_icon="✅", layout="wide")


def _stretch(fn) -> dict:
    """Full width. Newer Streamlit takes width="stretch"; older (as Snowflake may run) only knows
    use_container_width. Decided from the function's own signature, so any version works: the new
    parameter defaults to a word ("stretch") or is typed `Width` (st.altair_chart: `Width | None = None`)."""
    try:
        width = inspect.signature(fn).parameters["width"]
    except (KeyError, TypeError, ValueError):
        return {"use_container_width": True}
    new = isinstance(width.default, str) or "Width" in str(width.annotation)
    return {"width": "stretch"} if new else {"use_container_width": True}


def show_table(frame: pd.DataFrame) -> None:
    kwargs = _stretch(st.dataframe)
    if "hide_index" in inspect.signature(st.dataframe).parameters:      # Streamlit 1.23 and later
        kwargs["hide_index"] = True
    else:
        frame = frame.reset_index(drop=True)
    st.dataframe(frame, **kwargs)


# Clicks on charts reach Python through st.altair_chart(on_select=...): Streamlit 1.35+, Altair 5+.
CLICKABLE = hasattr(alt, "selection_point") and "on_select" in inspect.signature(st.altair_chart).parameters


def show_chart(chart, key: str | None = None) -> None:
    """A chart. With a key (and a chart made clickable()), a click is stored under that key and the
    page reruns; take_clicks() turns it into filters at the top of the next run."""
    kwargs = _stretch(st.altair_chart)
    if key and CLICKABLE:
        kwargs.update(on_select="rerun", key=key)
    st.altair_chart(chart, **kwargs)


def clickable(chart, fields: list[str]):
    """Let a click on a mark select it: the selection named "pick" holds the clicked mark's `fields`."""
    return chart.add_params(alt.selection_point(name="pick", fields=fields)) if CLICKABLE else chart


def multi(label: str, options: list, key: str) -> None:
    """A filter: nothing chosen means everything ("All"; the placeholder needs Streamlit 1.27+)."""
    extra = {"placeholder": "All"} if "placeholder" in inspect.signature(st.multiselect).parameters else {}
    st.multiselect(label, options, key=key, **extra)


def rerun() -> None:
    (getattr(st, "rerun", None) or st.experimental_rerun)()            # st.rerun: Streamlit 1.27 and later


# ---- data ---------------------------------------------------------------------------------------
def _connect():
    import snowflake.connector

    env = os.environ.get
    args = {k: env(f"SNOWFLAKE_{k.upper()}") for k in ("account", "user", "role", "warehouse", "database", "schema")}
    args.update(json.loads(env("SNOWFLAKE_CONNECT_ARGS") or "{}"))
    args.setdefault("platform_detection_timeout_seconds", 0)
    args["session_parameters"] = {"TIMEZONE": "UTC"}
    auth = env("SNOWFLAKE_AUTH", "keypair")
    if auth == "password":
        args["password"] = env("SNOWFLAKE_PASSWORD")
    elif auth == "externalbrowser":
        args["authenticator"] = "externalbrowser"
    else:
        args["private_key_file"] = os.path.expanduser(env("SNOWFLAKE_PRIVATE_KEY_PATH") or "")
        if env("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
            args["private_key_file_pwd"] = env("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    return snowflake.connector.connect(**args)


def _query(query: str) -> pd.DataFrame:
    try:                                                   # inside Snowflake
        from snowflake.snowpark.context import get_active_session

        session = get_active_session()
    except Exception:
        session = None
    if session is not None:
        session.sql("alter session set timezone = 'UTC'").collect()
        return session.sql(query).to_pandas()
    conn = _connect()                                      # on a laptop
    try:
        cur = conn.cursor().execute(query)
        return pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner="Reading the results from Snowflake...")
def load() -> pd.DataFrame:
    df = _query(QUERY)
    if df.empty:
        return df
    for col in ("EXPLANATION", "INVESTIGATE_SQL", "ALERT_SENT"):      # missing from rows stored before they existed
        if col not in df:
            df[col] = None
    for col in ("RUN_TS", "WINDOW_START", "WINDOW_END"):
        df[col] = pd.to_datetime(df[col])
    for col in ("OBSERVED_VALUE", "ELEMENT_COUNT", "UNEXPECTED_COUNT"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["STATUS_LABEL"] = df["STATUS"].map(STATUS_LABEL)
    df["CHECK_LEVEL"] = df["METRIC"].map(lambda m: "FILE" if m in FILE_METRICS else "RECORD")
    rule = df["RULE_NAME"].fillna("")
    df["CHECK"] = rule.where(rule != "", df["CHECK_TYPE"])
    # The row label in charts: the field, else the business rule, else the window as a whole.
    df["FIELD_NAME"] = df["FIELD_NAME"].fillna("").where(df["FIELD_NAME"].fillna("") != "", rule)
    df["FIELD_NAME"] = df["FIELD_NAME"].replace("", "(whole window)")
    df["WINDOW_LABEL"] = df["WINDOW_END"].dt.strftime("%Y-%m-%d %H:%M")
    return df


def measured(row) -> str:
    v = row.OBSERVED_VALUE
    if row.STATUS == "ERROR":
        return "could not run"
    if pd.isna(v):
        return ""
    if v >= NO_ROWS:
        return "no rows at all"
    return {"bad_pct": f"{v:.2f}% bad rows", "row_count": f"{v:,.0f} rows",
            "hours_since_last": f"{v:.1f} h since the newest row",
            "stability_index": f"index {v:.3f}"}.get(row.METRIC, "rule broken" if row.STATUS == "FAIL" else "rule holds")


def count(value) -> str:
    return "n/a" if pd.isna(value) else f"{int(value):,}"


# ---- look ---------------------------------------------------------------------------------------
st.markdown("""
<style>
.dq-card {border:1px solid rgba(128,128,128,.25); border-left:5px solid var(--c); border-radius:6px;
          padding:12px 16px; background:rgba(128,128,128,.04); min-height:92px}
.dq-card .label {font-size:.78rem; letter-spacing:.06em; text-transform:uppercase; opacity:.75}
.dq-card .value {font-size:1.9rem; font-weight:600; line-height:1.3}
.dq-card .sub {font-size:.78rem; opacity:.7}
.dq-band {background:#1f2328; color:#fff; padding:10px 16px; border-left:5px solid #e8743b;
          border-radius:4px; font-weight:600; margin:18px 0 10px}
</style>""", unsafe_allow_html=True)


def card(col, label: str, value, colour: str, sub: str = "") -> None:
    col.markdown(f"<div class='dq-card' style='--c:{colour}'><div class='label'>{label}</div>"
                 f"<div class='value'>{value:,}</div><div class='sub'>{sub}</div></div>", unsafe_allow_html=True)


def band(text: str) -> None:
    st.markdown(f"<div class='dq-band'>{text}</div>", unsafe_allow_html=True)


def status_scale():
    return alt.Scale(domain=list(STATUS_COLOUR), range=list(STATUS_COLOUR.values()))


# ---- filters, set from the sidebar or by clicking a chart --------------------------------------------
# dimension -> (results column, session-state key of its sidebar filter). Empty filter = everything.
DIMS = {"status": ("STATUS_LABEL", "f_status"), "level": ("CHECK_LEVEL", "f_level"),
        "source": ("SOURCE", "f_source"), "type": ("CHECK_TYPE", "f_type"), "field": ("FIELD_NAME", "f_field")}
FILTER_LABELS = (("Result", "f_status"), ("Level", "f_level"), ("Source", "f_source"),
                 ("Check type", "f_type"), ("Field", "f_field"))
# chart -> {column in the clicked mark: (the filter it changes, how)}
#   toggle  add the value to the filter, or take it out if it is already there (several can be chosen)
#   only    the filter becomes just this value            set  the window to open
CLICKS = {
    "click_status": {"STATUS_LABEL": ("f_status", "toggle")},
    "click_level": {"CHECK_LEVEL": ("f_level", "toggle")},
    "click_field": {"FIELD_NAME": ("f_field", "toggle")},
    "click_type": {"CHECK_TYPE": ("f_type", "toggle")},
    "click_trend": {"WINDOW_LABEL": ("f_window", "set")},
    "click_heat": {"WINDOW_LABEL": ("f_window", "set"), "FIELD_NAME": ("f_field", "only")},
}


def chart_key(name: str) -> str:
    """The key a clickable chart is drawn with. It changes after every click the page acts on, so the
    chart comes back with nothing selected: clicking the same mark again is then a new click (a chart
    that kept its selection would not report a second click on the same mark)."""
    return f"{name}_{st.session_state.get(f'gen_{name}', 0)}"


def take_clicks() -> None:
    """Turn a click on a chart into filters. Runs before the sidebar widgets exist, which is when
    Streamlit lets their values be set. A click on the chart's background selects nothing and is ignored."""
    for name, targets in CLICKS.items():
        try:
            points = list(st.session_state[chart_key(name)]["selection"]["pick"])
        except (KeyError, TypeError):
            continue
        if not points:
            continue
        st.session_state[f"gen_{name}"] = st.session_state.get(f"gen_{name}", 0) + 1
        point = points[-1]
        for column, (target, how) in targets.items():
            value = point.get(column)
            if value is None:
                continue
            if how == "set":
                st.session_state[target] = str(value)
            elif how == "only":
                st.session_state[target] = [value]
            else:
                chosen = list(st.session_state.get(target) or [])
                st.session_state[target] = [v for v in chosen if v != value] if value in chosen else chosen + [value]


def take_link(schedules: list) -> None:
    """Open on the run named in the address (?schedule=hourly&window=2026-10-17 12:00, as alerts link
    it). Once per session, so the sidebar still works afterwards. Snowsight addresses carry no parameters."""
    if st.session_state.get("link_taken"):
        return
    st.session_state["link_taken"] = True
    try:
        params = {k: st.query_params.get(k) for k in ("schedule", "window")}       # Streamlit 1.30+
    except AttributeError:
        params = {k: (v or [None])[0] for k, v in st.experimental_get_query_params().items()}
    if params.get("schedule") in schedules:
        st.session_state["f_schedule"] = params["schedule"]
    if params.get("window"):
        st.session_state["f_window"] = params["window"]


def clear_filters() -> None:
    for _, key in DIMS.values():
        st.session_state[key] = []


def filtered(frame: pd.DataFrame, skip: str = "") -> pd.DataFrame:
    """The rows matching every filter except the dimension `skip` (a chart shows all of its own
    dimension and highlights the chosen ones)."""
    for dim, (column, key) in DIMS.items():
        chosen = st.session_state.get(key) or []
        if dim != skip and chosen:
            frame = frame[frame[column].isin(chosen)]
    return frame


def highlight(values: pd.Series, dim: str) -> pd.Series:
    """1 for a chosen value (or when nothing is chosen), 0.35 for the rest: used as the mark opacity."""
    chosen = st.session_state.get(DIMS[dim][1]) or []
    return values.map(lambda v: 1.0 if not chosen or v in chosen else 0.35)


# ---- charts -------------------------------------------------------------------------------------------
def count_axis(title: str, top: int):
    """Whole numbers only. For small counts the ticks are listed: Vega otherwise picks 0.5 steps and,
    formatted as integers, shows 0 1 1 2 2."""
    if top <= 10:
        return alt.Axis(title=title, values=list(range(0, int(top) + 1)), format="d")
    return alt.Axis(title=title, tickMinStep=1, format="d")


def window_axis(schedule: str, title: str = "Window ending (UTC)"):
    """Windows as MM-DD HH:MM (hourly) or MM-DD (daily), horizontal. Rotated full timestamps took most
    of a chart's height, because Streamlit fits the whole chart (labels included) into the height given."""
    expr = "slice(datum.value, 5)" if schedule == "hourly" else "slice(datum.value, 5, 10)"
    return alt.Axis(title=title, labelAngle=0, labelExpr=expr, labelOverlap="greedy")


def donut(frame: pd.DataFrame):
    counts = (frame.groupby("STATUS_LABEL").size().reindex(list(STATUS_COLOUR), fill_value=0)
              .reset_index(name="COUNT"))
    counts["HL"] = highlight(counts.STATUS_LABEL, "status")
    ring = clickable(alt.Chart(counts).mark_arc(innerRadius=62, outerRadius=95, cursor="pointer").encode(
        theta=alt.Theta("COUNT:Q", stack=True),
        color=alt.Color("STATUS_LABEL:N", scale=status_scale(), legend=alt.Legend(title=None, orient="right")),
        opacity=alt.Opacity("HL:Q", scale=None, legend=None),
        tooltip=[alt.Tooltip("STATUS_LABEL:N", title="Result"), alt.Tooltip("COUNT:Q", title="Checks")]),
        ["STATUS_LABEL"])
    # One layer only: Streamlit does not support clicks on layered charts, so the total is in the caption.
    return ring.properties(height=230)


def stacked_bar(frame: pd.DataFrame, category: str, dim: str, title: str):
    """Horizontal bars, one per category, split by result; a click picks the category. The height is
    set per bar (alt.Step): a fixed total height leaves almost no room for the bars once Streamlit fits
    the axis and legend into a narrow column."""
    counts = frame.groupby([category, "STATUS_LABEL"]).size().reset_index(name="COUNT")
    counts["HL"] = highlight(counts[category], dim)
    return clickable(alt.Chart(counts).mark_bar(cursor="pointer").encode(
        y=alt.Y(f"{category}:N", title=None, sort="-x", axis=alt.Axis(labelLimit=220)),
        x=alt.X("COUNT:Q", axis=count_axis(title, counts.groupby(category).COUNT.sum().max())),
        color=alt.Color("STATUS_LABEL:N", scale=status_scale(), legend=alt.Legend(title=None, orient="bottom")),
        opacity=alt.Opacity("HL:Q", scale=None, legend=None),
        tooltip=[category, alt.Tooltip("STATUS_LABEL:N", title="Result"), alt.Tooltip("COUNT:Q", title="Checks")],
    ).properties(height=alt.Step(28)), [category])


def trend(frame: pd.DataFrame, chosen: str, schedule: str):
    counts = frame.groupby(["WINDOW_LABEL", "STATUS_LABEL"]).size().reset_index(name="COUNT")
    counts["HL"] = (counts.WINDOW_LABEL == chosen).map({True: 1.0, False: 0.45})
    return clickable(alt.Chart(counts).mark_bar(cursor="pointer").encode(
        x=alt.X("WINDOW_LABEL:O", sort="ascending", axis=window_axis(schedule, "Window ending, UTC (click one to open it)")),
        y=alt.Y("COUNT:Q", axis=count_axis("Checks", counts.groupby("WINDOW_LABEL").COUNT.sum().max())),
        color=alt.Color("STATUS_LABEL:N", scale=status_scale(), legend=alt.Legend(title=None, orient="bottom")),
        opacity=alt.Opacity("HL:Q", scale=None, legend=None),
        tooltip=[alt.Tooltip("WINDOW_LABEL:O", title="Window ending"), alt.Tooltip("STATUS_LABEL:N", title="Result"),
                 alt.Tooltip("COUNT:Q", title="Checks")],
    ).properties(height=320), ["WINDOW_LABEL"])


def heatmap(frame: pd.DataFrame, chosen: str, schedule: str):
    """Checks not passing per field and window, for the fields that did not pass at least once."""
    bad = frame[frame.STATUS != "PASS"]
    fields = sorted(bad.FIELD_NAME.unique())
    grid = (pd.MultiIndex.from_product([fields, sorted(frame.WINDOW_LABEL.unique())], names=["FIELD_NAME", "WINDOW_LABEL"])
            .to_frame(index=False)
            .merge(bad.groupby(["FIELD_NAME", "WINDOW_LABEL"]).size().reset_index(name="FAILED"), how="left")
            .fillna({"FAILED": 0}))
    grid["SEL"] = (grid.WINDOW_LABEL == chosen).map({True: 2.5, False: 0.0})
    return clickable(alt.Chart(grid).mark_rect(cursor="pointer", stroke="#e8743b").encode(
        x=alt.X("WINDOW_LABEL:O", axis=window_axis(schedule)),
        y=alt.Y("FIELD_NAME:N", title=None, axis=alt.Axis(labelLimit=220)),
        # grey to red: a green start would pass through brown on the way to red
        color=alt.Color("FAILED:Q", title="Checks not passing",
                        scale=alt.Scale(domain=[0, max(1, int(grid.FAILED.max()))], range=["#eceff1", "#c0392b"], interpolate="rgb"),
                        legend=alt.Legend(format="d", tickMinStep=1)),
        strokeWidth=alt.StrokeWidth("SEL:Q", scale=None, legend=None),
        tooltip=[alt.Tooltip("FIELD_NAME:N", title="Field"), alt.Tooltip("WINDOW_LABEL:O", title="Window ending"),
                 alt.Tooltip("FAILED:Q", title="Checks not passing")],
    ).properties(height=alt.Step(24)), ["FIELD_NAME", "WINDOW_LABEL"])


# ---- page ---------------------------------------------------------------------------------------
st.title("Data Quality Monitoring")
try:
    results = load()
except Exception as exc:
    st.error(f"Could not read {RESULT_TABLE}: {exc}")
    st.stop()
if results.empty:
    st.info(f"No results in {RESULT_TABLE} for the last {DAYS} days yet. Run the checks first: `make run-hourly`.")
    st.stop()

schedules = sorted(results.SCHEDULE.unique(), key=lambda s: (s != "hourly", s))
take_link(schedules)
take_clicks()
with st.sidebar:
    st.header("Filters")
    schedule = st.radio("Schedule", schedules, key="f_schedule", horizontal=True,
                        help="hourly: input checks on each hour. daily: volume and drift (PSI / CSI) by day.")
    history = results[results.SCHEDULE == schedule]
    windows = sorted(history.WINDOW_LABEL.unique(), reverse=True)
    if st.session_state.get("f_window") not in windows:       # first run, another schedule, or an old click
        st.session_state["f_window"] = windows[0]
    window = st.selectbox("Window ending (UTC)", windows, key="f_window",
                          help="One run checks one window: the hour (or day) that ends here. Or click a window in a chart.")
    st.caption(f"{len(windows)} {schedule} runs in the last {DAYS} days.")
    options = {"f_status": list(STATUS_COLOUR), "f_level": sorted(history.CHECK_LEVEL.unique()),
               "f_source": sorted(history.SOURCE.unique()), "f_type": sorted(history.CHECK_TYPE.unique()),
               "f_field": sorted(history.FIELD_NAME.unique())}
    for key, values in options.items():                       # drop choices this schedule does not have
        st.session_state[key] = [v for v in st.session_state.get(key, []) if v in values]
    for label, key in FILTER_LABELS:
        multi(label, options[key], key)
    st.button("Clear filters", on_click=clear_filters)
    if CLICKABLE:
        st.caption("Tip: click a slice or bar to add it to the filter, click it again to remove it. "
                   "Several can be chosen.")
    if st.button("Refresh data"):
        st.cache_data.clear()
        rerun()

run = history[history.WINDOW_LABEL == window]
# The "across windows" charts: the latest WINDOWS windows, and the one shown if it is older.
recent = history[history.WINDOW_LABEL.isin(set(windows[:WINDOWS]) | {window})]
alerted = history[history.ALERT_SENT == "Yes"]
sent_here = run.RUN_ID[run.ALERT_SENT == "Yes"].nunique()
sent_all = alerted.RUN_ID.nunique()

st.caption(f"{', '.join(sorted(run.TABLE_NAME.unique()))}  ·  {schedule} window "
           f"{run.WINDOW_START.min():%Y-%m-%d %H:%M} to {run.WINDOW_END.max():%Y-%m-%d %H:%M} UTC  ·  "
           f"checked {run.RUN_TS.max():%Y-%m-%d %H:%M} UTC")
n = {s: int((run.STATUS == s).sum()) for s in STATUS_LABEL}
cols = st.columns(6)
card(cols[0], "Total no. of checks", len(run), "#e8743b", f"{(run.CHECK_LEVEL == 'RECORD').sum()} record · "
                                                          f"{(run.CHECK_LEVEL == 'FILE').sum()} file level")
card(cols[1], "Total passed", n["PASS"], STATUS_COLOUR["Passed"], f"{n['PASS'] / len(run):.0%} of checks")
card(cols[2], "Warnings", n["WARN"], STATUS_COLOUR["Warning"], "drift past the warn limit")
card(cols[3], "Total failed", n["FAIL"], STATUS_COLOUR["Failed"],
     f"{int(((run.STATUS == 'FAIL') & (run.ALERT_FLAG == 'Yes')).sum())} with alerts on")
card(cols[4], "Could not run", n["ERROR"], STATUS_COLOUR["Could not run"], "SQL or column errors")
card(cols[5], "Total alerts sent", sent_here, "#127ea6",
     f"{sent_all} across all windows" if run.ALERT_SENT.notna().any() else "not recorded for this run")

active = [f"{label}: {', '.join(map(str, st.session_state[key]))}" for label, key in FILTER_LABELS
          if st.session_state.get(key)]
if active:
    st.info("Filtered by " + "  ·  ".join(active) + ". The cards show the whole run; "
            "use *Clear filters* in the sidebar to see everything.")

tab_summary, tab_types, tab_all, tab_fail = st.tabs(
    ["Executive Summary", "Types of Checks", "All Check Results", "Fail Details"])

# ---- 1. executive summary -------------------------------------------------------------------------
with tab_summary:
    band("Executive Summary")
    left, middle, right = st.columns(3)
    with left:
        st.subheader("Check results")
        in_donut = filtered(run, skip="status")
        st.caption(f"{len(in_donut):,} check{'' if len(in_donut) == 1 else 's'}. Click a slice to show or hide that result.")
        show_chart(donut(in_donut), key=chart_key("click_status"))
    with middle:
        st.subheader("Record vs file level")
        st.caption("Record level: every row is judged, then the share of bad rows is compared with the limit. "
                   "File level: one number for the whole window. Click a bar to show or hide it.")
        show_chart(stacked_bar(filtered(run, skip="level"), "CHECK_LEVEL", "level", "Checks"), key=chart_key("click_level"))
    with right:
        st.subheader("Fields needing attention")
        failing = filtered(run, skip="field")
        failing = failing[failing.STATUS != "PASS"]
        if failing.empty:
            st.success("Every check passed in this window.")
        else:
            st.caption("Fields (or rules) with at least one check not passing. Click one to add or remove it.")
            show_chart(stacked_bar(failing, "FIELD_NAME", "field", "Checks not passing"), key=chart_key("click_field"))

    band("Across windows")
    st.caption(f"The latest {min(WINDOWS, len(windows))} {schedule} windows. Click a window to open it.")
    show_chart(trend(filtered(recent), window, schedule), key=chart_key("click_trend"))
    across = filtered(recent, skip="field")
    if (across.STATUS != "PASS").any():
        st.markdown("**Which fields failed, window by window**")
        st.caption("Darker = more checks not passing. Click a cell to open that window and filter by that field. "
                   "The outlined column is the window shown.")
        show_chart(heatmap(across, window, schedule), key=chart_key("click_heat"))
    glance = (filtered(history).pivot_table(index="WINDOW_LABEL", columns="STATUS", values="CHECK_ID", aggfunc="count",
                                            fill_value=0)
              .reindex(columns=list(STATUS_LABEL), fill_value=0)
              .rename(columns=STATUS_LABEL))
    alerted_windows = set(alerted.WINDOW_LABEL)
    glance["Alert sent"] = glance.index.map(lambda w: "Yes" if w in alerted_windows else "")
    show_table(glance.sort_index(ascending=False).reset_index().rename(columns={"WINDOW_LABEL": "Window ending (UTC)"}))

# ---- 2. types of checks ---------------------------------------------------------------------------
with tab_types:
    band("Types of Checks")
    shown = filtered(run)
    by_type = (shown.groupby(["CHECK_TYPE", "CHECK_LEVEL"])
               .agg(Checks=("CHECK_ID", "count"), Passed=("STATUS", lambda s: int((s == "PASS").sum())),
                    Warnings=("STATUS", lambda s: int((s == "WARN").sum())),
                    Failed=("STATUS", lambda s: int((s == "FAIL").sum())),
                    Could_not_run=("STATUS", lambda s: int((s == "ERROR").sum())),
                    Fields=("FIELD_NAME", lambda s: ", ".join(sorted(set(s)))))
               .reset_index())
    by_type["Pass rate"] = (by_type.Passed / by_type.Checks).map("{:.0%}".format)
    by_type["What it checks"] = by_type.CHECK_TYPE.map(TYPE_MEANING).fillna("")
    left, right = st.columns([2, 3])
    with left:
        st.caption("Click a check type to add or remove it.")
        show_chart(stacked_bar(filtered(run, skip="type"), "CHECK_TYPE", "type", "Checks"), key=chart_key("click_type"))
    with right:
        show_table(by_type.rename(columns={"CHECK_TYPE": "Check type", "CHECK_LEVEL": "Level",
                                           "Could_not_run": "Could not run"})
                   [["Check type", "Level", "Checks", "Passed", "Warnings", "Failed", "Could not run", "Pass rate",
                     "What it checks", "Fields"]])

# ---- 3. all check results -------------------------------------------------------------------------
with tab_all:
    band("All Check Results")
    shown = filtered(run)
    st.caption(f"{len(shown)} of {len(run)} checks. Narrow them with the filters in the sidebar, or by clicking a chart.")
    show_table(pd.DataFrame({
        "Result": shown.STATUS.map(ICON), "Level": shown.CHECK_LEVEL.str.title(), "Source": shown.SOURCE,
        "Field": shown.FIELD_NAME, "Check": shown.CHECK, "Type": shown.CHECK_TYPE, "Rule": shown.CRITERIA,
        "Measured": [measured(r) for r in shown.itertuples()],
        "Bad rows": shown.UNEXPECTED_COUNT, "Rows checked": shown.ELEMENT_COUNT,
        "Alert on": shown.ALERT_FLAG, "Alert sent": shown.ALERT_SENT.fillna(""),
    }))

# ---- 4. fail details ------------------------------------------------------------------------------
with tab_fail:
    band("Fail Details")
    failed = filtered(run)
    failed = failed[failed.STATUS != "PASS"]
    if failed.empty:
        st.success("Nothing failed in this window" + (" for these filters." if active else "."))
    for r in failed.itertuples():
        title = f"{ICON[r.STATUS]} · {r.FIELD_NAME} · {r.CHECK} ({r.CHECK_LEVEL.lower()} level, {r.SOURCE})"
        with st.expander(title, expanded=len(failed) <= 3):
            st.write(r.EXPLANATION if isinstance(r.EXPLANATION, str) and r.EXPLANATION else
                     "No explanation stored: this result was written before the results table kept one. "
                     "Re-run the window to add it.")
            a, b, c, d = st.columns(4)
            a.metric("Measured", measured(r) or "n/a")
            b.metric("Bad rows", count(r.UNEXPECTED_COUNT))
            c.metric("Rows checked", count(r.ELEMENT_COUNT))
            d.metric("Alert on / sent", f"{r.ALERT_FLAG} / {r.ALERT_SENT if isinstance(r.ALERT_SENT, str) else 'n/a'}")
            st.caption(f"Rule: {r.CRITERIA}  ·  Table: {r.TABLE_NAME}")
            if r.STATUS == "ERROR":
                st.code(r.DETAILS, language="json")
            if isinstance(r.INVESTIGATE_SQL, str) and r.INVESTIGATE_SQL:
                st.markdown("**See the rows** (run in a Snowsight worksheet):")
                st.code(r.INVESTIGATE_SQL, language="sql")
            past = recent[recent.CHECK_ID == r.CHECK_ID].sort_values("WINDOW_END")
            past = past[past.OBSERVED_VALUE < NO_ROWS]
            if len(past) > 1:
                st.markdown("**This check across windows**")
                show_chart(alt.Chart(past).mark_bar().encode(
                    x=alt.X("WINDOW_LABEL:O", axis=window_axis(schedule)),
                    y=alt.Y("OBSERVED_VALUE:Q", title=r.METRIC.replace("_", " ")),
                    color=alt.Color("STATUS_LABEL:N", scale=status_scale(), legend=None),
                    tooltip=[alt.Tooltip("WINDOW_LABEL:O", title="Window ending"),
                             alt.Tooltip("STATUS_LABEL:N", title="Result"),
                             alt.Tooltip("OBSERVED_VALUE:Q", title="Measured", format=",.4f")],
                ).properties(height=220))

    band("Alerts sent")
    if alerted.empty:
        st.info(f"No alert has been recorded for the {schedule} runs in the last {DAYS} days. "
                "Runs from before the report recorded alerts, and backfills (which never alert), show none.")
    else:
        sent = (alerted.groupby(["WINDOW_LABEL", "RUN_ID"])
                .agg(Checked=("RUN_TS", "max"), Checks=("CHECK_ID", "count"),
                     Which=("CHECK", lambda s: ", ".join(sorted(set(s)))))
                .reset_index().sort_values("WINDOW_LABEL", ascending=False))
        show_table(sent.rename(columns={"WINDOW_LABEL": "Window ending (UTC)", "RUN_ID": "Run",
                                        "Checked": "Run at (UTC)", "Checks": "Checks in the alert",
                                        "Which": "Which"}))
