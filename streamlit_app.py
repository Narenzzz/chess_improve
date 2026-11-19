"""Streamlit front end for chess_coach.py. Deploy on Streamlit Community Cloud with these files in the repo root: streamlit_app.py (this file, set it as the app's main file) chess_coach.py requirements.txt (chess, streamlit) packages.txt (stockfish) """
import argparse
import csv
import datetime as dt
import io
import json
import os
import re

import streamlit as st

import chess_coach as cc

st.set_page_config(page_title="Chess Coach", page_icon="♟", layout="wide")

BASE_DIR = os.environ.get("COACH_DATA_DIR",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), "coach_data"))


def user_dir(user):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", user).lower() or "default"
    return os.path.join(BASE_DIR, safe)


def read(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return None


def show_html(page, height):
    """Embed one of our generated pages (built by chess_coach.py, not user input)."""
    if hasattr(st, "iframe"):
        st.iframe(page, height=height)
    else:  # older Streamlit
        import streamlit.components.v1 as components
        components.html(page, height=height, scrolling=True)


def run_report(user, speeds):
    """Rebuild report, trainer and puzzles. Returns an error message or None."""
    try:
        cc.cmd_report(argparse.Namespace(user=user, speeds=",".join(speeds)))
    except SystemExit as e:
        return str(e)
    return None


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("♟ Chess Coach")
    user = st.text_input("Lichess username", value="Narenzxy").strip()
    speeds = st.multiselect("Game speeds", ["blitz", "rapid", "classical"], default=["rapid", "classical"])
    n_games = st.slider("Games to download", 20, 300, 60, step=10,
                        help="Only new games are analysed, so later runs are quick.")
    depth = st.slider("Stockfish depth", 8, 16, 12,
                      help="12 is a good balance on the free Streamlit server. Higher is slower.")
    go = st.button("Fetch & analyse new games", type="primary", width="stretch")
    st.caption("First run: roughly 2–6 seconds per game on the free server. "
               "Keep this tab open while it runs.")

if not user:
    st.info("Enter your Lichess username in the sidebar.")
    st.stop()
if not speeds:
    st.warning("Pick at least one game speed.")
    st.stop()

cc.set_data_dir(user_dir(user))

# ------------------------------------------------------------------ fetch + analyse
if go:
    with st.status("Working…", expanded=True) as status:
        st.write(f"Downloading up to {n_games} games from Lichess…")
        try:
            cc.cmd_fetch(argparse.Namespace(user=user, max=n_games, perf=",".join(speeds), token=None))
        except Exception as e:
            status.update(label="Download failed", state="error")
            st.error(f"Could not download games from Lichess: {e}. "
                     "If it says 429, Lichess is rate-limiting; wait a minute and try again.")
            st.stop()

        st.write("Analysing with Stockfish…")
        bar = st.progress(0.0, text="Starting engine…")

        def progress(i, total):
            bar.progress(i / total, text=f"Game {i} of {total}")

        try:
            cc.cmd_analyse(argparse.Namespace(user=user, depth=depth, threads=2, hash=128, stockfish=None),
                           progress=progress)
        except SystemExit as e:
            status.update(label="Analysis failed", state="error")
            st.error(str(e))
            st.stop()
        bar.progress(1.0, text="Analysis done")

        st.write("Building report…")
        err = run_report(user, speeds)
        if err:
            status.update(label="Nothing to report yet", state="error")
            st.error(err)
            st.stop()
        status.update(label="Done", state="complete", expanded=False)

# rebuild the report if analysis exists but the speeds changed or the report is missing
if os.path.exists(cc.CACHE_FILE) and not go:
    key = (user, tuple(sorted(speeds)), os.path.getmtime(cc.CACHE_FILE),
           os.path.getmtime(cc.LOG_FILE) if os.path.exists(cc.LOG_FILE) else 0)
    if st.session_state.get("report_key") != key:
        err = run_report(user, speeds)
        if err:
            st.warning(err)
        st.session_state["report_key"] = key

report = read(os.path.join(cc.DATA_DIR, "report.html"))

# ------------------------------------------------------------------ main area
st.title(f"Chess Coach: {user}")
if not report:
    st.info("No analysis yet. Click **Fetch & analyse new games** in the sidebar, "
            "or restore a backup in the **Backup** tab.")

tab_r, tab_t, tab_p, tab_l, tab_b = st.tabs(["Report", "Critical or normal?", "Puzzles", "Training log", "Backup"])

with tab_r:
    if report:
        show_html(report, 3600)

with tab_t:
    trainer = read(os.path.join(cc.DATA_DIR, "trainer.html"))
    if trainer:
        st.caption("10 seconds per position. Do 20 before each rapid game.")
        show_html(trainer, 640)
    else:
        st.info("The trainer appears after your first analysis.")

with tab_p:
    files = sorted(f for f in os.listdir(cc.DATA_DIR) if f.startswith("puzzles_") and f.endswith(".pgn"))
    if files:
        st.markdown("Your own mistakes as puzzles. On Lichess: **Study → New study → Import PGN**, "
                    "paste or upload one file (64 positions each).")
        for f in files:
            st.download_button(f"Download {f}", read(os.path.join(cc.DATA_DIR, f)), file_name=f,
                               mime="application/x-chess-pgn")
    else:
        st.info("No puzzles yet. They are made from your mistakes and blunders after analysis.")

with tab_l:
    with st.form("log", clear_on_submit=True):
        c1, c2 = st.columns(2)
        cat = c1.selectbox("Category", cc.LOG_CATEGORIES)
        mins = c2.number_input("Minutes", min_value=5, max_value=600, value=60, step=5)
        note = st.text_input("Note", placeholder="Edouard ch.1, ex 1-6")
        day = st.date_input("Date", value=dt.date.today())
        if st.form_submit_button("Log session", type="primary"):
            cc.cmd_log(argparse.Namespace(category=cat, minutes=int(mins), note=note, date=day.isoformat()))
            st.success(f"Logged {mins} min of {cat}.")
    rows = cc.load_log()
    if rows:
        st.dataframe([{"date": r["date"], "category": r["cat"], "minutes": r["min"], "note": r["note"]}
                      for r in sorted(rows, key=lambda r: r["date"], reverse=True)],
                     width="stretch", hide_index=True)
    st.caption("The free Streamlit server can reset its storage when the app restarts. "
               "Download a backup from the Backup tab every Sunday.")

with tab_b:
    st.markdown("Free Streamlit apps lose their files when they restart or sleep. "
                "Download these each Sunday and upload them here if your data disappears.")
    c1, c2 = st.columns(2)
    if os.path.exists(cc.CACHE_FILE):
        c1.download_button("Download analysis.json", read(cc.CACHE_FILE), file_name=f"analysis_{user}.json",
                           mime="application/json")
    if os.path.exists(cc.LOG_FILE):
        c2.download_button("Download training_log.csv", read(cc.LOG_FILE), file_name=f"training_log_{user}.csv",
                           mime="text/csv")
    up = st.file_uploader("Restore a backup (analysis .json or training log .csv)", type=["json", "csv"])
    if up is not None and st.button("Restore"):
        data = up.getvalue().decode("utf-8")
        if up.name.endswith(".json"):
            incoming = json.loads(data)
            cache = cc.load_cache()
            cache.update(incoming)
            cc.save_cache(cache)
            st.success(f"Restored {len(incoming)} analysed games.")
        else:
            rows = list(csv.DictReader(io.StringIO(data)))
            if not rows or "category" not in rows[0]:
                st.error("That doesn't look like a training log file.")
            else:
                with open(cc.LOG_FILE, "w", encoding="utf-8", newline="") as f:
                    f.write(data)
                st.success(f"Restored {len(rows)} log entries.")
        st.session_state.pop("report_key", None)
        st.rerun()