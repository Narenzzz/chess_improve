"""Streamlit front end for chess_coach.py.

Deploy on Streamlit Community Cloud with these files in the repo root:
    streamlit_app.py   (this file, set it as the app's main file)
    chess_coach.py
    annotator.py
    requirements.txt   (chess, streamlit)
    packages.txt       (stockfish)
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import re

import streamlit as st

import importlib

import chess_coach as cc
import annotator as an

# Always use the files currently on disk (Streamlit keeps imported files in memory otherwise).
cc = importlib.reload(cc)
an = importlib.reload(an)
APP_VERSION = "2026-11-21c"

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


def secret(name, default=""):
    try:
        return st.secrets.get(name, default) or default
    except Exception:
        return default


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

    st.divider()
    st.subheader("Opening theory")
    try:
        secret_token = st.secrets.get("LICHESS_TOKEN", "")
    except Exception:
        secret_token = ""
    token = secret_token or st.text_input(
        "Lichess token", type="password",
        help="Free. Create it at lichess.org/account/oauth/token without ticking any boxes. "
             "To avoid pasting it every time, add LICHESS_TOKEN to the app's Secrets.")
    if secret_token:
        st.caption("Token loaded from app secrets.")
    theory_go = st.button("Check where I leave theory", width="stretch", disabled=not token)

    st.divider()
    _ok = getattr(an, "__version__", "?") == APP_VERSION == getattr(cc, "__version__", "?")
    st.caption(f"App version {APP_VERSION} · annotator {getattr(an, '__version__', 'old')} · "
               f"core {getattr(cc, '__version__', 'old')}" + ("" if _ok else " ⚠️ files don't match: re-upload all files"))

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

# ------------------------------------------------------------------ opening theory
if theory_go:
    if not os.path.exists(cc.CACHE_FILE):
        st.sidebar.error("Analyse your games first.")
    else:
        with st.status("Checking your openings against the masters database…", expanded=True) as status:
            bar = st.progress(0.0, text="Starting…")
            note = st.empty()

            def tprogress(i, total):
                bar.progress(i / total, text=f"Game {i} of {total}")

            def twait():
                note.info("Lichess asked us to slow down, waiting one minute before continuing…")

            try:
                cc.cmd_theory(argparse.Namespace(user=user, token=token), progress=tprogress, wait=twait)
            except SystemExit as e:
                status.update(label="Theory check stopped", state="error")
                st.error(str(e))
                st.stop()
            note.empty()
            err = run_report(user, speeds)
            if err:
                st.warning(err)
            status.update(label="Opening theory checked: see section 8 of the report", state="complete",
                          expanded=False)

# rebuild the report if analysis exists but the speeds changed or the report is missing
if os.path.exists(cc.CACHE_FILE) and not go and not theory_go:
    key = (user, tuple(sorted(speeds)), os.path.getmtime(cc.CACHE_FILE),
           os.path.getmtime(cc.LOG_FILE) if os.path.exists(cc.LOG_FILE) else 0,
           os.path.getmtime(cc.theory_file()) if os.path.exists(cc.theory_file()) else 0)
    if st.session_state.get("report_key") != key:
        err = run_report(user, speeds)
        if err:
            st.warning(err)
        st.session_state["report_key"] = key

report = read(os.path.join(cc.DATA_DIR, "report.html"))

# ------------------------------------------------------------------ main area
st.title("♟ Chess Coach")

tab_a, tab_r, tab_t, tab_p, tab_l, tab_b = st.tabs(
    ["Annotate", "Report", "Critical or normal?", "Puzzles", "Training log", "Backup"])

# ------------------------------------------------------------------ annotate one game
ICON = {"blunder": "🔴", "mistake": "🟠", "inaccuracy": "🟡"}

with tab_a:
    if not _ok:
        st.error("Your app files are from different versions. Upload all files from the latest zip "
                 "(Add file → Upload files) and reboot the app.")
    st.caption(f"Version {APP_VERSION}")
    ai_key = secret("GEMINI_API_KEY") or secret("AI_API_KEY")
    ai_cfg = {"provider": secret("AI_PROVIDER", "gemini"), "key": ai_key,
              "model": secret("AI_MODEL") or None, "base_url": secret("AI_BASE_URL") or None}
    my_names = [x for x in re.split(r"[,\s]+", secret("MY_USERNAMES", f"{user},naren94")) if x]

    pgn_text = st.text_area("Paste your game PGN", height=140, key="ann_pgn",
                            placeholder='[Event "..."]\n1. e4 c5 2. Nf3 ...',
                            help="chess.com app: open the game → Share → PGN → Copy. Lichess: game → Share & export → PGN.")
    c1, c2 = st.columns(2)
    side = c1.selectbox("You played", ["Auto-detect", "White", "Black"])
    depth_label = c2.selectbox("Engine depth", ["Fast (12)", "Normal (14)", "Deep (16)"], index=1,
                               help="Normal takes about 1–3 minutes on the free server.")
    if ai_key:
        st.caption(f"Explanations by AI ({ai_cfg['provider']}); every move it mentions is checked against Stockfish.")
    else:
        st.caption("No AI key set: you'll get engine-based notes. Add GEMINI_API_KEY in the app's Secrets for coach-style explanations.")

    if st.button("Annotate", type="primary", width="stretch", key="ann_go"):
        try:
            game = an.parse_pgn(pgn_text)
            color = {"White": True, "Black": False}.get(side)
            if color is None:
                color = an.detect_color(game, my_names)
            if color is None:
                raise an.AnnotateError("Couldn't tell which side you played. Choose White or Black above.")
            depth = int(re.search(r"\d+", depth_label).group())
            bar = st.progress(0.0, text="Starting Stockfish…")
            facts = an.analyse_game(game, color, depth=depth, threads=2, hash_mb=128,
                                    progress=lambda i, n: bar.progress(i / n, text=f"Engine: move {i} of {n}"))
            bar.progress(1.0, text="Writing the notes…" if ai_key else "Building notes…")
            with st.spinner("Writing coach notes… (can take a minute or two)" if ai_key else "Building notes…"):
                result = an.annotate(game, color, facts, ai=ai_cfg if ai_key else None)
            bar.empty()
            h = game.headers
            st.session_state["ann"] = {
                "result": result, "facts": facts, "color": color,
                "title": f"{h.get('White', '?')} vs {h.get('Black', '?')} ({h.get('Result', '?')})",
                "name": f"{h.get('White', '?')} - {h.get('Black', '?')} {h.get('Date', h.get('UTCDate', ''))}".strip(),
                "study_url": None, "pgn_src": pgn_text,
            }
        except an.AnnotateError as e:
            st.error(str(e))
        except SystemExit as e:
            st.error(str(e))

    ann = st.session_state.get("ann")
    if ann:
        res, facts, color = ann["result"], ann["facts"], ann["color"]
        info = res["info"]
        st.subheader(ann["title"])
        if info.get("ai_error"):
            st.warning(info["ai_error"])
            if ai_key and st.button("Write AI notes again", width="stretch", key="ann_retry"):
                with st.spinner("Writing coach notes… (can take a minute or two)"):
                    g2 = an.parse_pgn(ann["pgn_src"])
                    ann["result"] = an.annotate(g2, color, facts, ai=ai_cfg)
                    ann["study_url"] = None
                st.rerun()
        if info.get("ai_used"):
            removed = f" {info['dropped']} sentence(s) mentioning unverified moves were removed." if info["dropped"] else ""
            st.caption(f"Notes: {info['ai_used']} · Engine: Stockfish depth {facts['depth']}.{removed}")
        st.info(res["summary"])
        if res["lessons"]:
            st.markdown("**Lessons**\n" + "\n".join(f"{i}. {l}" for i, l in enumerate(res["lessons"], 1)))

        evals = [max(-6.0, min(6.0, p["eval_after"] / 100)) for p in facts["plies"]]
        st.line_chart({"Evaluation (White)": evals}, height=180, x_label="half-move", y_label="pawns")

        st.markdown("**Key moments**")
        for m in res["moments"]:
            icon = ICON.get(m["tag"], "🟢" if m["mine"] else "⚪")
            with st.expander(f"{icon} {an.move_label(m)} · {m['kind']}"):
                st.markdown(an.svg_img_tag(an.board_svg(m, color)), unsafe_allow_html=True)
                st.caption("Red arrow: move played · Green arrow: engine's best")
                st.write(res["notes"].get(m["id"], ""))
                if m["best_san"] != m["san"]:
                    st.caption(f"Engine line: {m['best_line']}")
                if m["spent"] is not None:
                    st.caption(f"Time used: {an.fmt_time(m['spent'])} · clock left {an.fmt_time(m['clock'])}")

        st.markdown("**Save it**")
        st.download_button("Download annotated PGN", res["pgn"], file_name="annotated.pgn",
                           mime="application/x-chess-pgn", width="stretch")
        with st.expander("Copy PGN"):
            st.code(res["pgn"], language=None, wrap_lines=True)

        study = secret("LICHESS_STUDY")
        if not study:
            study = st.text_input("Your Lichess study link (optional)", key="ann_study",
                                  help="Create a study on Lichess once, e.g. 'My annotated games', and paste its link. "
                                       "Add LICHESS_STUDY to Secrets to skip this.")
        if token and study:
            if st.button("Add to my Lichess study", width="stretch", key="ann_send"):
                try:
                    ann["study_url"] = an.send_to_study(token, study, res["pgn"], ann["name"],
                                                        "white" if color else "black")
                except an.AnnotateError as e:
                    st.error(str(e))
            if ann.get("study_url"):
                st.success("Added as a new chapter.")
                st.link_button("Open in Lichess", ann["study_url"], width="stretch")
        elif study and not token:
            st.caption("To send it to your study, add your Lichess token (with study write permission) in Secrets.")

with tab_r:
    if report:
        show_html(report, 3600)
    else:
        st.info("No analysis yet. Open the sidebar (› at the top left on mobile) and tap "
                "**Fetch & analyse new games**, or restore a backup in the **Backup** tab.")

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
    c1, c2, c3 = st.columns(3)
    if os.path.exists(cc.theory_file()):
        c3.download_button("Download theory.json", read(cc.theory_file()), file_name=f"theory_{user}.json",
                           mime="application/json")
    if os.path.exists(cc.CACHE_FILE):
        c1.download_button("Download analysis.json", read(cc.CACHE_FILE), file_name=f"analysis_{user}.json",
                           mime="application/json")
    if os.path.exists(cc.LOG_FILE):
        c2.download_button("Download training_log.csv", read(cc.LOG_FILE), file_name=f"training_log_{user}.csv",
                           mime="text/csv")
    up = st.file_uploader("Restore a backup (analysis .json, theory .json or training log .csv)",
                          type=["json", "csv"])
    if up is not None and st.button("Restore"):
        data = up.getvalue().decode("utf-8")
        if up.name.endswith(".json"):
            incoming = json.loads(data)
            sample = next(iter(incoming.values()), {}) if incoming else {}
            if "book_plies" in sample or "exit" in sample or sample.get("skip") == "custom start position":
                theory = cc.load_json(cc.theory_file(), {})
                theory.update(incoming)
                cc.save_json(cc.theory_file(), theory)
                st.success(f"Restored opening theory for {len(incoming)} games.")
            else:
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
