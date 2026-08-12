import io
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # no GUI backend needed — we're just rendering to image files
import matplotlib.pyplot as plt

from database import engine

# A consistent color per moderation decision, used across charts
DECISION_COLORS = {"allow": "#2f9e56", "flag": "#c98a2c", "block": "#c94b3f"}


def load_scans_df(user_email: str = None) -> pd.DataFrame:
    """Pulls the scans table out of the SQLite database into a pandas DataFrame,
    optionally filtered to one parent's scans."""
    query = "SELECT * FROM scans"
    params = ()
    if user_email:
        query += " WHERE user_email = ?"
        params = (user_email,)
    return pd.read_sql(query, engine, params=params)


def _empty_chart(ax, message="No scans yet"):
    ax.text(0.5, 0.5, message, ha="center", va="center", color="#86868b", fontsize=11)
    ax.axis("off")


def _fig_to_png_bytes(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", transparent=True, dpi=140)
    plt.close(fig)
    buf.seek(0)
    return buf


def chart_decisions(user_email: str = None) -> io.BytesIO:
    """Pie chart: how many scans were allowed / flagged / blocked."""
    df = load_scans_df(user_email)
    fig, ax = plt.subplots(figsize=(4.5, 4.5))

    if df.empty:
        _empty_chart(ax)
    else:
        counts = df["moderation_decision"].value_counts()
        colors = [DECISION_COLORS.get(k, "#999999") for k in counts.index]
        ax.pie(counts.values, labels=counts.index, autopct="%1.0f%%", colors=colors,
               textprops={"color": "#1c1c1e", "fontsize": 10})
        ax.set_title("Moderation Decisions", fontsize=12, color="#1c1c1e")

    return _fig_to_png_bytes(fig)


def chart_content_types(user_email: str = None) -> io.BytesIO:
    """Bar chart: how many scans were text vs image vs audio vs video vs website."""
    df = load_scans_df(user_email)
    fig, ax = plt.subplots(figsize=(6, 4))

    if df.empty:
        _empty_chart(ax)
    else:
        counts = df["content_type"].value_counts()
        ax.bar(counts.index, counts.values, color="#c96442")
        ax.set_title("Scans by Content Type", fontsize=12, color="#1c1c1e")
        ax.set_ylabel("Number of scans")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    return _fig_to_png_bytes(fig)


def chart_timeline(user_email: str = None) -> io.BytesIO:
    """Line chart: how many scans happened per day, over time."""
    df = load_scans_df(user_email)
    fig, ax = plt.subplots(figsize=(7, 4))

    if df.empty:
        _empty_chart(ax)
    else:
        df["created_at"] = pd.to_datetime(df["created_at"])
        daily = df.groupby(df["created_at"].dt.date).size()
        ax.plot(daily.index.astype(str), daily.values, marker="o", color="#c96442")
        ax.set_title("Scans Over Time", fontsize=12, color="#1c1c1e")
        ax.set_ylabel("Scans")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        plt.xticks(rotation=40, ha="right")

    return _fig_to_png_bytes(fig)


def summary_stats(user_email: str = None) -> dict:
    """Quick headline numbers pandas can compute directly from the DataFrame."""
    df = load_scans_df(user_email)

    if df.empty:
        return {"total": 0, "allowed": 0, "flagged": 0, "blocked": 0, "most_common_type": None}

    decision_counts = df["moderation_decision"].value_counts()
    return {
        "total": int(len(df)),
        "allowed": int(decision_counts.get("allow", 0)),
        "flagged": int(decision_counts.get("flag", 0)),
        "blocked": int(decision_counts.get("block", 0)),
        "most_common_type": df["content_type"].value_counts().idxmax(),
    }