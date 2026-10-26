# Offline reference retrieval (authorized research mode)

The ordinary photo privacy audit deliberately omits location candidates. This
separate command is for a locally held, approved reference set with latitude
and longitude labels. It performs cosine retrieval over embeddings generated
by a local encoder; it does not download models, call maps, reverse geocode,
or contact a network service.

The reference manifest is a UTF-8 CSV with these required columns:

```csv
asset_id,lat,lon,source,dataset,license,authorization_status
ref-001,48.2000,16.3700,approved-set,city-v1,local-license,approved
```

For a street-view provider, keep the provider's immutable identifiers and
license chain in the same manifest when available:
`provider`, `provider_image_id`, `capture_id`, `capture_date`, `source_url`,
`license_url`, and `license_provenance`. These fields are copied into the
candidate report so a high score can be traced to a concrete licensed asset;
they are not evidence that the query and reference show the same capture.

`authorization_status`, when supplied, must be one of `approved`,
`authorized`, `benchmark`, `research`, `public`, `licensed`, `consented`, or
`allowlisted`. Add `--require-authorization` to reject an unmarked manifest.
The embedding files use
the existing local embedding contract (`asset_id`, `model_id`, `vector`).
Queries and references must use the same model identifier and vector
dimension, including the checkpoint digest in `model_id`.

Run a retrieval pass as follows:

```powershell
python audit.py reference-retrieve `
  query_embeddings.csv `
  reference_manifest.csv `
  reference_embeddings.csv `
  candidates.csv `
  --top-k 20 `
  --grid-size-deg 0.005 `
  --rerank grid `
  --require-authorization
```

Optional `--center-lat`, `--center-lon`, and `--radius-km` apply a geographic
distance filter before ranking. `--grid-id` can be repeated to select one or
more deterministic latitude/longitude cells. A cell is a grouping aid; its
size is recorded in the manifest and should not be read as coordinate
precision.

The output includes candidate rank, cosine score, candidate coordinates,
source/dataset/license fields, grid consensus statistics, the applied spatial
filter, and a plain-text evidence string. A query with no eligible reference
gets an explicit `no_candidates` row so failures remain visible in downstream
evaluation. Candidate rows are research outputs and must not be used to infer
the location of a private photograph without authorization and independent
review.

Use `stratified_sample_manifest` before encoding a large gallery when a
provider or grid is overrepresented. Its hash-ranked round-robin sampling is
deterministic for a fixed seed and keeps provider/grid strata represented;
record the seed and the pre/post manifest SHA-256 in the run provenance.

The report also carries `image_id`, `candidate_id`, `lat`, `lon`, `score`, and
`source` aliases so it can feed the multi-image fusion command directly. The
fusion `score` is the affine `[0, 1]` mapping of the raw cosine value; the raw
value remains in `cosine_similarity`.

The CLI writes a `<report>.provenance.json` sidecar containing input SHA-256
digests, retrieval parameters, model identifier, and counts. It intentionally
does not copy candidate coordinates into the sidecar; coordinates remain only
in the authorized research report.

`--rerank grid` ranks candidate cells by their top similarity consensus before
the final per-reference tie-break. Use `--rerank embedding` when reproducing a
pure nearest-neighbour baseline.

