# KartaView coverage probe

This project can use public KartaView imagery as an **authorized, attribution-
preserving reference source**.  The first step is a metadata-only coverage
probe; it deliberately does not download image bytes.

## Current endpoint and semantics

The live endpoint used by the probe is:

```text
GET https://api.kartaview.org/2.0/photo/
    ?lat=<decimal>&lng=<decimal>&radius=<metres>
```

The API currently accepts `radius` in the range 1--500 m.  Treat a response as
successful only when both HTTP status is 200 and `status.apiCode` is 600.  A
successful response with `result.data: []` means that no public KartaView
photos were returned within the requested radius; it is a coverage observation,
not a model prediction.  The result objects expose GPS, sequence ID, heading,
capture time and processed/thumbnail URLs.  The probe records only the first
three metadata objects and never follows those URLs.

The old `api.openstreetcam.org/1.0/...` URLs are still present in older API
documentation but returned HTTP 404 from this environment on 2026-10-26.  Use
the current `api.kartaview.org/2.0/photo/` endpoint and recheck periodically.

## Reproducible Chongqing smoke test

```powershell
.\.venv-geoloc\Scripts\python.exe scripts\probe_kartaview.py `
  --output data\reports\kartaview-chongqing-probe-YYYYMMDD.json
```

The built-in points cover Yuzhong and several adjacent central districts.  On
2026-10-26, all eight points returned `status.apiCode=600` with zero hits.  This
supports the narrow statement “no KartaView coverage was returned within 500 m
of these sampled points”; it does **not** prove that the whole municipality is
empty, nor does it justify scraping another platform.

To probe an explicitly selected set of points, provide a CSV with
`name,lat,lon` columns:

```powershell
.\.venv-geoloc\Scripts\python.exe scripts\probe_kartaview.py `
  --points .\data\working\points.csv `
  --radius-m 500
```

As a positive-control sanity check, Times Square returned 36 records and a
central Beijing point returned 1 record in the same run.  These controls verify
that an empty Chongqing result is not caused by a broken client or endpoint.

## Use in retrieval

Coverage probing should precede any reference download.  If a point has hits,
download only a small, explicitly authorized sample, retain the returned
license/attribution information, and write a provenance sidecar.  KartaView is
crowdsourced, so spatial coverage and capture dates are uneven; missing imagery
must lower confidence rather than silently trigger a different provider.

Official API documentation: <https://kartaview.org/doc/photos>.
