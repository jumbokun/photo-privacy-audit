# Street-level imagery providers

Street imagery is a useful leverage point for a geolocation benchmark: it
turns a coordinate into a local, time-stamped visual reference gallery that
can be searched by the same local image encoder as the query photo. It is not a
guarantee of coverage or an authorization to identify a private person. The
workflow therefore stores provider provenance and keeps this path separate
from the default privacy audit.

## Provider policy

| Provider | Adapter | Credential | Coverage/licence notes |
| --- | --- | --- | --- |
| KartaView | `kartaview` | none for public API | Current public endpoint is `api.kartaview.org/2.0/photo/`; imagery is typically CC BY-SA, but coverage is uneven and often absent in mainland Chinese cities. |
| Mapillary | `mapillary` | `MAPILLARY_ACCESS_TOKEN` | Graph API only; token is read from the environment and never written to reports. Review the current Mapillary terms and image-level licence before redistribution. |
| Baidu Panorama | `baidu` | `BAIDU_MAP_AK` plus enterprise approval | Explicit-point static panorama requests only. Baidu's official notice says panorama services are not available to personal-certified developers; paid usage still requires an approved enterprise scenario. |
| Panoramax | `panoramax` | none for public STAC search | Public STAC search at `api.panoramax.xyz`; per-item license and collection/provider metadata are retained. Public coverage varies by instance. |

The project does not scrape Weibo, Douyin, Xiaohongshu, private groups, map web
tiles, or undocumented provider endpoints. For Chinese community/media
material, use `scripts/ingest_authorized_urls.py` with an explicit export or
written permission and preserve the licence in its manifest.

For a personal operator who cannot obtain Baidu enterprise approval, Mapillary
is the main candidate to try: its official documentation describes registering
an application and obtaining a client token, and its FAQ says the service is
free. Access is still account/UI-dependent and is not assumed to be available;
the adapter accepts a token only when the account owner supplies it locally via
`MAPILLARY_ACCESS_TOKEN`. It never uses leaked/shared tokens or web-app
scraping. A commercial imagery vendor with a written China license is the
other compliant paid route.

## Query and fetch

Create a bounded query CSV. Coordinates must be supplied by the operator; the
tool never infers a search area from a private photo.

```csv
query_id,lat,lon,radius_m,heading
yuzhong-center,29.5630,106.5520,250,
```

For Panoramax, an explicit bbox can be added when a city/grid cell is the
intended probe. The four columns are WGS84 `min_lon,min_lat,max_lon,max_lat`;
the center coordinates remain required for compatibility and audit
readability:

```csv
query_id,lat,lon,radius_m,min_lon,min_lat,max_lon,max_lat
chongqing-cell,29.56,106.55,5000,106.40,29.40,106.70,29.70
```

Run a metadata-only probe first:

```powershell
python audit.py streetview-fetch kartaview queries.csv `
  data/approved/reference/chongqing-kartaview/manifest.csv `
  --limit 25 --delay 1
```

The standalone probe in `scripts/probe_kartaview.py` uses the same current
endpoint and records only bounded metadata. It treats `status.apiCode=600` as
success and a zero-length result as a real coverage observation. The legacy
`api.openstreetcam.org/1.0/...` endpoint is supported only as an explicit
compatibility override and is not the default.

If coverage exists and the provider terms allow local analysis, download the
thumbnail images into a new directory:

```powershell
python audit.py streetview-fetch kartaview queries.csv `
  data/approved/reference/chongqing-kartaview/manifest.csv `
  --download-dir data/approved/reference/chongqing-kartaview/images `
  --limit 25 --delay 1
```

Mapillary and Baidu are opt-in:

```powershell
$env:MAPILLARY_ACCESS_TOKEN = "<token supplied by the account owner>"
python audit.py streetview-fetch mapillary queries.csv mapillary.csv --limit 25

$env:BAIDU_MAP_AK = "<key supplied by the account owner>"
python audit.py streetview-fetch baidu queries.csv baidu.csv --limit 1
```

The command writes `<manifest>.provenance.json` beside the CSV. It includes
the query-file digest, endpoint, request count, licence URL, row/failure
counts, and a bounded error list. It does not include access tokens. Image
rows include source IDs, source URLs, coordinates, capture time when supplied,
licence, and SHA-256 of each downloaded file. Empty results are valid evidence
of missing coverage, not a reason to relax rate limits or switch to scraping.

Panoramax uses the same bounded center/radius CSV format; the adapter converts
each query to a WGS84 bbox and sends a documented STAC JSON search. It prefers
the `assets.thumb` image and preserves `source_id`, `source_collection`,
`source_provider`, per-item `license`, the license link, and the STAC `self`
URL. For example:

```powershell
python audit.py streetview-fetch panoramax queries.csv `
  data/approved/reference/chongqing-panoramax/manifest.csv `
  --download-dir data/approved/reference/chongqing-panoramax/images `
  --limit 50 --delay 1
```

The current download allowlist includes the public Panoramax API and known
federated instance hosts such as `panoramax.openstreetmap.fr` and
`panoramax.mapcomplete.org`. An empty Panoramax result is a coverage
observation; it must not be treated as evidence that the query photo was taken
at any returned coordinate.

When adjacent bboxes overlap, the fetcher de-duplicates repeated provider
assets by `asset_id` before writing the manifest. This prevents dense overlap
from inflating retrieval scores while preserving the original query digest in
the provenance sidecar.

For a second-stage local check on a small, already retrieved gallery, run the
offline SIFT/ORB + RANSAC verifier. It reports only descriptor matches and
homography inliers; it does not perform network search or turn a match into an
exact-address conclusion:

```powershell
python scripts/verify_local_features.py `
  C:\private\query.jpg `
  data/approved/reference/chongqing-panoramax-images `
  data/reports/local-feature-verification.json
```

## Using the gallery

The manifest is compatible with the existing approved-reference retrieval
contract (`asset_id`, `lat`, `lon`, `source`, `dataset`, `license`,
`authorization_status`). Generate local DINOv2/SigLIP embeddings, then run
`reference-retrieve` and `fuse-candidates`. Keep the provider manifest and
embedding provenance together; do not mix imagery from incompatible licences
or dates without recording the distinction.

Street imagery can improve city/district hypotheses, but visual similarity is
not proof that a private photograph was taken at a specific hotel, room, or
floor. Exact-personal-location reports remain disabled by default.

## Why this is the highest-leverage addition

The street-view gallery is not fed directly into a model prompt. It is a
geotagged, locally indexed reference set:

1. Probe provider coverage and record an empty result as evidence.
2. Download a bounded, licence-approved sample around candidate city/grid
   cells, preserving capture time, heading, source ID and content hash.
3. Generate DINOv2/SigLIP embeddings locally and retrieve nearest visual
   references.
4. Re-rank by grid consensus, capture/heading diversity and (when the top
   candidates warrant it) local feature verification.
5. Fuse independent photos only when their spatial clusters agree; otherwise
   report conflict and lower confidence.

This converts a generic visual guess into a measurable retrieval experiment.
It still cannot create coverage where the provider has none: the Chongqing
smoke test currently has zero public KartaView hits in the sampled 500 m
windows, so a Chinese-city run needs either a credentialed provider such as
Baidu/Mapillary or an explicitly licensed local/community export.

