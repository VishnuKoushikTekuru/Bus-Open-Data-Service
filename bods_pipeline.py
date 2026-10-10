"""
BODS / Arriva route analysis pipeline
=====================================
Reads the TransXChange XML timetable files downloaded from the Bus Open Data
Service (data.bus-data.dft.gov.uk), builds one clean table with one row per
scheduled bus journey, and produces a per-route hourly frequency analysis.

Run from the repository folder:
    python bods_pipeline.py                       # reads data/raw, route 78
    python bods_pipeline.py data/sample           # the small included sample
    python bods_pipeline.py "path/to/xml/folder" --route 86A

Needs:  pip install pandas matplotlib

Outputs (written to an "output" folder next to this script):
    master_journeys_clean.csv        every journey in every file
    route_<ROUTE>_hourly.csv         journeys per hour for the chosen route
    route_<ROUTE>_hourly.png         chart of the same
"""

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET

import pandas as pd

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
DEFAULT_FOLDER = str(Path(__file__).resolve().parent / "data" / "raw")

# Every tag in a TransXChange file belongs to this XML namespace.
NS = {"txc": "http://www.transxchange.org.uk/"}

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# Some files use shorthand day tags instead of listing each day.
SHORTHAND_DAYS = {
    "MondayToFriday": WEEKDAYS,
    "MondayToSaturday": WEEKDAYS + ["Saturday"],
    "MondayToSunday": WEEKDAYS + ["Saturday", "Sunday"],
    "Weekend": ["Saturday", "Sunday"],
    "NotSaturday": WEEKDAYS + ["Sunday"],
}


# --------------------------------------------------------------------------
# Step 1: read one file
# --------------------------------------------------------------------------
def days_from_profile(operating_profile):
    """Return the list of day names an OperatingProfile says a service runs."""
    if operating_profile is None:
        return []
    days = []
    for el in operating_profile.findall(".//txc:RegularDayType/txc:DaysOfWeek/*", NS):
        tag = el.tag.split("}")[-1]
        days.extend(SHORTHAND_DAYS.get(tag, [tag]))
    return list(dict.fromkeys(days))  # remove repeats, keep order


def extract_journeys_from_file(filepath):
    """
    Return a list of dicts, one per scheduled journey in one TransXChange file.

    Lessons built in from checking the data against Arriva's own timetable:
      * the real route name is the <LineName> INSIDE the file, not the filename
      * direction comes from the journey's JourneyPattern, not the filename
      * operating days usually sit on the Service, not on each journey
    A malformed file returns [] so one bad file cannot stop the whole run.
    """
    filepath = Path(filepath)
    try:
        root = ET.parse(filepath).getroot()
    except ET.ParseError:
        return []

    # Line id -> route name (e.g. "78"), and Service code -> operating days
    line_names = {}
    service_days = {}
    first_service_days = []
    for service in root.findall(".//txc:Services/txc:Service", NS):
        code = service.find("txc:ServiceCode", NS)
        days = days_from_profile(service.find("txc:OperatingProfile", NS))
        if code is not None:
            service_days[code.text] = days
        if not first_service_days:
            first_service_days = days
        for line in service.findall("txc:Lines/txc:Line", NS):
            name = line.find("txc:LineName", NS)
            line_names[line.get("id")] = name.text if name is not None else "UNKNOWN"
    default_line = next(iter(line_names.values()), "UNKNOWN")

    # JourneyPattern id -> direction
    pattern_direction = {}
    for jp in root.findall(".//txc:JourneyPattern", NS):
        d = jp.find("txc:Direction", NS)
        pattern_direction[jp.get("id")] = d.text if d is not None else "unknown"

    rows = []
    for vj in root.findall(".//txc:VehicleJourney", NS):
        dep = vj.find("txc:DepartureTime", NS)
        jp_ref = vj.find("txc:JourneyPatternRef", NS)
        line_ref = vj.find("txc:LineRef", NS)
        service_ref = vj.find("txc:ServiceRef", NS)

        direction = pattern_direction.get(jp_ref.text, "unknown") if jp_ref is not None else "unknown"
        line = line_names.get(line_ref.text, default_line) if line_ref is not None else default_line

        # A journey can carry its own days; otherwise use its service's days.
        days = days_from_profile(vj.find("txc:OperatingProfile", NS))
        if not days:
            days = service_days.get(service_ref.text if service_ref is not None else None,
                                    first_service_days)

        rows.append({
            "route_line": line,
            "direction": direction,
            "departure_time": dep.text if dep is not None else None,
            "operating_days": ",".join(days) if days else "unspecified",
            "source_file": filepath.name,   # .name works on Windows and Mac/Linux
        })
    return rows


# --------------------------------------------------------------------------
# Step 2: read every file in the folder
# --------------------------------------------------------------------------
def build_master_dataset(folder):
    folder = Path(folder)
    files = sorted(folder.glob("*.xml"))
    print(f"Found {len(files)} XML files in {folder}")
    if not files:
        raise SystemExit("No .xml files found - check the folder path.")

    all_rows, failed = [], []
    for f in files:
        rows = extract_journeys_from_file(f)
        if not rows:
            failed.append(f.name)
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    print(f"Extracted {len(df)} journeys across {df['route_line'].nunique()} routes")
    if failed:
        print(f"WARNING: {len(failed)} files gave no journeys, e.g. {failed[:5]}")
    return df


# --------------------------------------------------------------------------
# Step 3: clean
# --------------------------------------------------------------------------
def label_day_type(days_text):
    """Turn 'Monday,Tuesday,...' into a short label."""
    if not days_text or days_text == "unspecified":
        return "unspecified"
    days = set(days_text.split(","))
    if days == set(WEEKDAYS):
        return "Mon-Fri"
    if days == {"Saturday"}:
        return "Saturday"
    if days == {"Sunday"}:
        return "Sunday"
    if days == {"Saturday", "Sunday"}:
        return "Weekend"
    return days_text


def clean_dataset(df):
    before = len(df)
    df = df.drop_duplicates().copy()
    print(f"Removed {before - len(df)} exact duplicate rows")

    # Parse the time, keep the hour as a number
    parsed = pd.to_datetime(df["departure_time"], format="%H:%M:%S", errors="coerce")
    bad = parsed.isna().sum()
    if bad:
        print(f"WARNING: {bad} rows had a departure time that could not be read")
    df["hour"] = parsed.dt.hour
    df["departure_time"] = parsed.dt.time

    df["day_type"] = df["operating_days"].apply(label_day_type)

    # A circular route (e.g. route 27, Sheil Road Circular) starts and ends at
    # the same place, so it only ever has "outbound" journeys. Flag these so
    # "journeys per direction" is not misread for them.
    routes_with_inbound = set(df.loc[df["direction"] == "inbound", "route_line"])
    df["route_type"] = df["route_line"].apply(
        lambda r: "linear" if r in routes_with_inbound else "circular")

    n_circ = df.loc[df["route_type"] == "circular", "route_line"].nunique()
    n_lin = df.loc[df["route_type"] == "linear", "route_line"].nunique()
    print(f"Routes: {n_lin} linear, {n_circ} circular")
    return df


# --------------------------------------------------------------------------
# Step 4: analyse one route
# --------------------------------------------------------------------------
def route_summary(df):
    """Journeys per route and day type (a quick overview of the whole network)."""
    return (df.groupby(["route_line", "day_type"]).size()
              .unstack(fill_value=0)
              .assign(total=lambda t: t.sum(axis=1))
              .sort_values("total", ascending=False))


def hourly_profile(df, route):
    """Journeys leaving in each hour, split by day type and direction."""
    r = df[df["route_line"] == route]
    if r.empty:
        raise SystemExit(f"Route '{route}' not found. Try one of: "
                         f"{sorted(df['route_line'].unique())[:15]} ...")
    return r.pivot_table(index="hour", columns=["day_type", "direction"],
                         values="departure_time", aggfunc="count", fill_value=0)


def plot_hourly(df, route, out_png):
    import matplotlib
    matplotlib.use("Agg")           # draw to a file, no window needed
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    r = df[df["route_line"] == route]
    directions = sorted(r["direction"].unique())
    fig, axes = plt.subplots(1, len(directions), figsize=(6 * len(directions), 4),
                             sharey=True, squeeze=False)
    hours = range(int(r["hour"].min()), int(r["hour"].max()) + 1)

    for ax, direction in zip(axes[0], directions):
        sub = r[r["direction"] == direction]
        for dtype, grp in sub.groupby("day_type"):
            counts = grp.groupby("hour").size().reindex(hours, fill_value=0)
            ax.plot(counts.index, counts.values, marker="o", label=dtype)
        ax.set_title(f"Route {route} - {direction}")
        ax.set_xlabel("Hour of day")
        ax.set_xticks(list(hours))
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))   # whole journeys only
        ax.grid(alpha=0.3)
        ax.legend(title="Day type")
    axes[0][0].set_ylabel("Scheduled departures")
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------
# Run it
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="BODS / Arriva timetable analysis")
    parser.add_argument("folder", nargs="?", default=DEFAULT_FOLDER,
                        help="folder containing the extracted .xml files")
    parser.add_argument("--route", default="78", help="route to analyse (default 78)")
    args = parser.parse_args()

    out_dir = Path(__file__).resolve().parent / "output"
    out_dir.mkdir(exist_ok=True)

    raw = build_master_dataset(args.folder)
    df = clean_dataset(raw)
    df.to_csv(out_dir / "master_journeys_clean.csv", index=False)
    print(f"\nSaved master_journeys_clean.csv ({len(df)} rows)")

    print("\nBusiest 10 routes by scheduled journeys:")
    print(route_summary(df).head(10))

    route = args.route
    profile = hourly_profile(df, route)
    print(f"\nRoute {route}: journeys per hour (by day type and direction)")
    print(profile)
    profile.to_csv(out_dir / f"route_{route}_hourly.csv")
    plot_hourly(df, route, out_dir / f"route_{route}_hourly.png")
    print(f"\nSaved route_{route}_hourly.csv and route_{route}_hourly.png to {out_dir}")


if __name__ == "__main__":
    main()
