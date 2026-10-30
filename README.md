# nyc-transit-live

Live NYC transit from GTFS-RT. One Python process (~100 MB RSS), no database.

Sources (checked 2026-10-30): MTA subway, LIRR, Metro-North GTFS-RT need no key (api-endpoint.mta.info);
MTA bus (Bus Time) answered keyless but is documented as key-required (set BUS_KEY if it starts failing);
NJ Transit needs a free developer.njtransit.com account (not implemented); PATH has no official RT feed.
Subway and most Metro-North have no lat/lon in GTFS-RT, so positions are interpolated between stops from TripUpdate times.

Endpoints: `/` MapLibre map, `/tiles/{z}/{x}/{y}.png` transparent overlay (for OsmAnd), `/api/vehicles.geojson`,
`/vehicles.gpx`, `/osmand` (one-tap link page), `/healthz`.

    pip install -r requirements.txt && python app.py   # PORT=8080
