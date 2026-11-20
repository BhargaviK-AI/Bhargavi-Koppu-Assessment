"""Build data/airports.json from the OurAirports dataset (public domain).

Keeps U.S. airports that have scheduled airline service and an IATA code.
Run once: python scripts/build_airports.py
"""
import csv
import json
from pathlib import Path
from urllib.request import urlopen

SRC_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
DATA = Path(__file__).resolve().parent.parent / "data"
RAW = DATA / "airports.raw.csv"


def main():
    DATA.mkdir(exist_ok=True)
    if not RAW.exists():
        print(f"Downloading {SRC_URL}")
        RAW.write_bytes(urlopen(SRC_URL).read())

    airports = []
    with RAW.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["iso_country"] != "US" or not row["iata_code"] or row["scheduled_service"] != "yes":
                continue
            if row["type"] not in ("large_airport", "medium_airport", "small_airport"):
                continue
            airports.append({
                "iata": row["iata_code"],
                "icao": row["icao_code"] or row["gps_code"] or None,
                "name": row["name"],
                "city": row["municipality"],
                "state": row["iso_region"].removeprefix("US-"),
                "lat": float(row["latitude_deg"]),
                "lon": float(row["longitude_deg"]),
                "size": row["type"].removesuffix("_airport"),
            })

    (DATA / "airports.json").write_text(json.dumps(airports))
    print(f"Wrote {len(airports)} airports")


if __name__ == "__main__":
    main()
