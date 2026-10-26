# 数据契约

## `metadata_report.csv`

`asset_id` 是稳定的本地资产标识；`relative_path` 只用于本地回溯；`sha256` 和感知哈希用于复现关联。`has_gps`、`has_timestamp`、`has_device` 和 `has_serial` 只表达字段存在性，绝不保存字段值。

## `visual_audit.csv`

每行对应一个资产。`review_status=pending` 表示尚未完成人工检查，不等于低风险。事实字段只描述画面中可见的内容；风险字段必须是 0–4 整数；`evidence_notes` 记录证据来源；未经验证的判断不能写成已验证结论。

## `post_manifest.csv`

```csv
asset_id,relative_path,post_id,post_timestamp
```

`relative_path` 必须指向照片根目录内的真实文件；路径越界、重复资产或缺行都会使关联运行失败。

## local embedding CSV

```csv
asset_id,model_id,vector
asset-001,dinov2-base@checkpoint-sha,"[0.1, -0.2, 0.3]"
```

向量只来自用户明确选择的本地 encoder；`model_id` 必须包含足以区分 checkpoint 的信息。相似度输出只是人工复核候选，不是同一地点证明。

## local embedding generation

The `embedding-generate` command is an offline adapter boundary. The default
`pillow` backend is deterministic and dependency-light; it is a reproducible
descriptor for pipeline tests and candidate review, not a geolocation model.
Optional `torchscript` and `transformers` backends only load local checkpoints
and refuse network downloads. Each run writes a `.provenance.json` sidecar with
the backend, preprocessing recipe, package versions, device, dimensions and a
SHA-256 digest of the checkpoint (or model directory).

```powershell
python audit.py embedding-generate C:\private\photos C:\private\vectors.csv `
  --backend pillow
python audit.py embedding-generate C:\private\photos C:\private\vectors.csv `
  --backend transformers --model D:\models\dinov2-local
# OpenCLIP uses MODEL_NAME::LOCAL_CHECKPOINT to keep the checkpoint explicit.
python audit.py embedding-generate C:\private\photos C:\private\vectors.csv `
  --backend open_clip --model ViT-B-32::D:\models\openclip\open_clip_pytorch_model.bin
```

The optional neural adapters are intentionally not part of the base
requirements. Install and provision model weights separately on the offline
machine, then retain the generated provenance sidecar with the embedding CSV.

## local OCR and entity manifest

`ocr.csv` contains local text observations only:

```csv
asset_id,text,confidence,box,backend,image_sha256
img-001,轻居酒店 ATOUR LIGHT,0.90,"[[0,0],[1,1]]",rapidocr-onnxruntime,sha256
```

`entity-match` requires a separate approved manifest. Aliases are separated
with `|` or `;`; `city` and `admin_region` are labels supplied by the manifest,
not reverse-geocoded by the tool:

```csv
entity_id,aliases,city,admin_region,authorization_status
approved-place,轻居酒店|ATOUR LIGHT,重庆市,渝中区,approved
```

The resulting match is still a candidate evidence row. It becomes verified
only after an independent reference or held-out benchmark confirms it.

## benchmark prediction CSV

最低字段为 `asset_id,true_lat,true_lon,pred_lat,pred_lon`。允许预测坐标同时留空表示失败，但真值仍必须有效。可选 `topk_json` 是按排名排列的 `[[lat,lon], ...]` 或 `[ {"lat": ..., "lon": ...}, ... ]` 候选列表，用于 Recall@1/5/20/100。可选 `split`、`capture_id`、`scene_id`、`source_sha256` 和分组字段用于泄漏审计与分组报告。
