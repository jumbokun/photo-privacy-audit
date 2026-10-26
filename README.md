# 本地照片地理隐私审计

这是一个离线、可复现的照片隐私审计工作流。它把个人照片审计和公开基准模型评估分开：个人照片只做元数据、人工视觉线索和跨帖重复性审计；地理定位模型只接受明确获准、带真值的基准预测结果。

工具不会上传图片，不调用地图或街景搜索，不生成私人地址候选。普通报告只记录 GPS 是否存在，不写坐标、家庭地址、设备序列号或原始 EXIF 值。

## 安装

```powershell
cd C:\work\web3work\photo-privacy-audit
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果希望读取更多相机格式，可另外安装 ExifTool 并让它出现在 `PATH` 中。没有 ExifTool 时，工具仍会使用 Pillow 做基础审计，但报告会标记 `pillow_limited`。

中国大陆、WSL 或无外网机器的安装方式见 [docs/CHINA.md](C:/work/web3work/photo-privacy-audit/docs/CHINA.md)。安装后先运行 `python audit.py doctor`，它会检查中文路径、东八区时区、依赖和离线策略。

## 第一轮审计

把原图放进一个受限目录；工具只读扫描，不覆盖原图。输出目录必须是输入目录之外的新目录。

```powershell
python audit.py audit C:\private\photos C:\private\photo-audit-20261026
```

一次运行审计、可选跨帖关联和汇总：

```powershell
python audit.py workflow C:\private\photos C:\private\photo-audit-20261026 --manifest C:\private\post_manifest.csv
```

输出会分为 `audit/`、`correlation/` 和 `summary/`；每次运行都要求使用新的输出目录，避免覆盖证据。

会生成：

- `metadata_report.csv`：哈希、文件属性、元数据存在性和状态；
- `visual_audit.csv`：人工视觉检查表，初始状态是 `pending`；
- `privacy_summary.md`：不含精确位置的汇总；
- `provenance.json`：工具版本、策略和代码摘要；
- `audit_manifest.json`：资产 ID 与输入根目录映射。

看到 `pending` 时不能把它解释成零风险。打开 `visual_audit.csv`，根据画面事实填写文字与标志、建筑设施、窗户反光、车辆人员、时间线索和重复背景；把每个风险分项填写为 0–4，并保持“可见事实”“风险推断”“已验证结论”的区分。

```powershell
python audit.py validate-checklist C:\private\photo-audit-20261026\visual_audit.csv
```

人工填写后可刷新汇总：

```powershell
python audit.py summary `
  C:\private\photo-audit-20261026\metadata_report.csv `
  C:\private\photo-audit-20261026\visual_audit.csv `
  C:\private\photo-audit-20261026\summary-20261026 `
  --correlation C:\private\photo-correlation-20261026\correlation_report.csv
```

## 跨帖关联

准备一个只包含以下列的 `post_manifest.csv`，其中 `relative_path` 必须是相对于照片根目录的路径，时间使用 ISO-8601：

```csv
asset_id,relative_path,post_id,post_timestamp
<metadata_report 中的 asset_id>,IMG_0042.jpg,post-001,2026-10-20T18:30:00+02:00
```

然后运行：

```powershell
python audit.py correlate C:\private\photo-audit-20261026\metadata_report.csv post_manifest.csv C:\private\photos C:\private\photo-correlation-20261026
```

结果只表示“完全相同文件”或“感知哈希相近”的复核线索。它不会断言不同角度的照片来自同一场景，也不会生成地点候选。

如果要把本地视觉编码器用于高召回复核，可先在本机生成一个 embedding CSV。每行包含 `asset_id`、`model_id` 和 JSON 向量；`model_id` 应包含模型与 checkpoint 摘要，例如 `dinov2-base@sha256:...`。工具只做归一化余弦相似度候选检索，不负责下载或调用模型：

```powershell
python audit.py embedding-generate C:\private\photos local_embeddings.csv `
  --backend pillow
# 已准备好的本地权重可使用 torchscript 或 transformers；命令不会联网下载。
python audit.py embedding-generate C:\private\photos local_embeddings-dinov2.csv `
  --backend transformers --model D:\models\dinov2-local
# OpenCLIP uses MODEL_NAME::LOCAL_CHECKPOINT and never downloads weights.
python audit.py embedding-generate C:\private\photos local_embeddings-openclip.csv `
  --backend open_clip --model ViT-B-32::D:\models\openclip\weights.bin
python audit.py embedding-correlate local_embeddings.csv embedding_candidates.csv `
  --threshold 0.90 --top-k 5
```

生成命令会同时写入 `local_embeddings.csv.provenance.json`，记录预处理、维度、依赖版本和 checkpoint 摘要。默认 Pillow 编码器用于可复现基线；接入 DINOv2、SigLIP 等本地编码器时，候选必须经过人工复核，不能把 embedding 相似度当作同一地点的证明。

在有 GPU 的 Linux/Godzilla 环境中，可以把 Hugging Face 模型先下载到本地目录，再用 `--backend transformers --model <local-dir>`；适配器使用 `local_files_only`，不会在推理时联网。DINOv2 和 SigLIP 的本地模型加载已在 RTX 4090 上完成 smoke test。

神经 encoder 的 Python 依赖单独列在 [requirements-geoloc.txt](C:/work/web3work/photo-privacy-audit/requirements-geoloc.txt)，CUDA 版 PyTorch 仍应按目标机器的驱动选择。

本地视觉模型如需辅助填写线索，可输出结构化 JSONL，再由工具校验：

```powershell
python audit.py vision-evidence local_vision_evidence.jsonl vision_evidence.csv
```

适配器只接受文字与标志、建筑、反光、车辆人员、时间线索和重复背景等观察类别，并拒绝经纬度、地址和反向地理编码字段。它不会自动把模型观察升级成已验证结论。

中文招牌可以在本机用 RapidOCR 提取，再和获准的酒店/地标实体 manifest 做行政区候选匹配：

```powershell
python audit.py ocr C:\private\photos ocr.csv
python audit.py entity-match ocr.csv approved_entities.csv entity_candidates.csv `
  --require-authorization
```

实体匹配只使用你提供的本地 manifest；它不会联网搜索酒店，也不会默认把 OCR 品牌名升级成住宿地点。

如果你拥有中国社群/媒体图片的使用授权，可以用 [authorized_urls.sample.csv](C:/work/web3work/photo-privacy-audit/config/authorized_urls.sample.csv) 格式批量导入。下载器要求每行带许可和授权状态，不会自行发现或爬取社交平台：

```powershell
python scripts/ingest_authorized_urls.py authorized_urls.csv data/approved/reference/chongqing-authorized
```

项目还附带一份可编辑的中国主要城市别名表 [china_city_aliases.sample.csv](C:/work/web3work/photo-privacy-audit/config/china_city_aliases.sample.csv)，可直接运行：

```powershell
python audit.py city-match ocr.csv config/china_city_aliases.sample.csv city_candidates.csv
```

它适合 OCR 已经读到城市名、区名或英文城市名的照片；没有文字地标时，仍需对应城市的获准参考图库来做视觉检索。

对于没有明确文字地标的照片，可以在本机运行 GeoCLIP，先输出全球 GPS 候选，再用本地中国城市中心表聚合：

```powershell
python audit.py geoclip-predict `
  C:\private\photo.jpg `
  C:\private\geoclip_candidates.json `
  --top-k 100

python audit.py geo-city-match `
  C:\private\geoclip_candidates.json `
  config/china_city_centers.sample.csv `
  C:\private\city_candidates.csv
```

如果已经下载中国行政区 GeoJSON，可直接把 GeoCLIP 坐标落到市/区边界：

```powershell
python audit.py admin-match `
  C:\private\geoclip_candidates.json `
  data\approved\geodata\geoBoundaries-CHN-ADM3.geojson `
  C:\private\admin_candidates.csv
```

当前本地数据目录可以放置 GeoBoundaries ADM3 或 GADM 边界；使用前请保留数据许可证和中国行政边界适用范围说明。

GeoCLIP 分数是候选排序分数，不是概率；城市聚合依赖本地中心表，不能替代行政区边界或独立参考图验证。

## 生成清理副本

元数据清理会写入一个新目录，支持 JPEG、PNG 和 WebP；不会覆盖原图。视觉上的门牌、车牌、反光等内容必须由人工图像编辑处理，再把处理后的副本重新审计。

```powershell
python audit.py sanitize C:\private\photos C:\private\photos-sanitized --asset IMG_0042.jpg
```

不提供 `--asset` 时会处理输入目录下所有受支持图片。运行前应确认输出目录不是输入目录的子目录。

### 获准参考图库检索

如果有明确授权、带经纬度标签的本地参考图库，可以在独立的研究模式下运行 `reference-retrieve`。它读取本地参考 manifest 和本地 embedding，按余弦相似度输出 top-k 候选，并支持经纬度半径和确定性空间网格过滤。候选报告记录模型、数据集、许可、网格共识和可解释证据；不会调用地图或网络服务。完整字段和示例见 [docs/REFERENCE_RETRIEVAL.md](C:/work/web3work/photo-privacy-audit/docs/REFERENCE_RETRIEVAL.md)。

```powershell
python audit.py reference-retrieve `
  query_embeddings.csv reference_manifest.csv reference_embeddings.csv candidates.csv `
  --top-k 20 --grid-size-deg 0.005 --rerank grid --require-authorization
```

把多张同组照片的候选直接做空间融合：

```powershell
python audit.py fuse-candidates candidates.csv fused_candidates.json `
  --cluster-radius 2 --top-k 5
```

### Street-view reference gallery

Use the street-view adapters only in research mode: probe coverage first,
download a bounded licensed sample, then generate local DINOv2/SigLIP
embeddings and run `reference-retrieve` plus `fuse-candidates`.

```powershell
python audit.py streetview-fetch kartaview `
  config/streetview_chongqing_probe.sample.csv `
  data/approved/reference/chongqing-kartaview.csv `
  --limit 25 --delay 1
```

KartaView, Panoramax, Mapillary and Baidu adapters preserve source/licence provenance;
Mapillary and Baidu require credentials in environment variables. The tool
does not scrape map tiles or social platforms. See
`docs/STREETVIEW_PROVIDERS.md` for coverage and authorization details.
输出是带中心、半径、支持图片和冲突状态的候选范围；只有经过获准留出集校准和独立证据核验后，才可以升级为已验证定位。

## 基准评估

`benchmark` 接受一个已经由获准基准数据集产生的预测 CSV，必需列为：

```csv
asset_id,true_lat,true_lon,pred_lat,pred_lon
q-001,50.1,8.6,50.1005,8.6010
```

```powershell
python audit.py benchmark predictions.csv benchmark_report.md `
  --dataset "authorized-dataset-v1" `
  --model "model-name@checkpoint" `
  --commit "git-sha-or-checkpoint-digest" `
  --bootstrap-iterations 2000
```

报告计算中位数、平均值、P90/P95、失败率、200 米 / 1 / 25 / 200 / 750 / 2500 公里命中率和 bootstrap 95% 区间；如果 CSV 里有 `region`、`location` 或 `group` 字段，还会给出分组指标。相邻场景或相同捕获身份跨 split 时，运行清单会记录泄漏警告。它不会把总体命中率改写成某张个人照片的定位概率；同时生成 `<report>.manifest.json`，记录预测文件哈希、种子、依赖和不含坐标的运行信息。

预测 CSV 如果包含 `confidence`（0–1），还会计算指定距离阈值的 Brier score 和 ECE；缺少置信度时会明确标记为不可用。

如果模型输出候选列表，可增加 `topk_json` 列（例如 `[[lat,lon], [lat,lon]]`），报告会补充 Recall@1/5/20/100，和官方检索式评估保持可比。

官方 `google-research/scaling-geoloc` 项目要求专门的数据目录（含 `arrayrecord` 和位置标注）、JAX/Flax/Scenic 等依赖，并使用自己的评估入口。这个仓库提供的是统一结果评估器；安装官方代码和下载数据、权重前，应分别核对许可证、版本和磁盘/GPU要求。

基准协议、数据契约和分级复现标准见 [docs/SOTA_BENCHMARK.md](C:/work/web3work/photo-privacy-audit/docs/SOTA_BENCHMARK.md) 与 [docs/SCHEMA.md](C:/work/web3work/photo-privacy-audit/docs/SCHEMA.md)。

## 数据保护

`.gitignore` 会忽略本项目的 `.venv`、`data`、日志、外部模型和凭据，但这不是加密。原图目录仍应使用磁盘加密、受限账户和备份策略。普通报告不写坐标，但 `metadata_report.csv` 仍可能包含文件名和哈希，请按私人资料保存。
### Stable IDs for reference embeddings

When a downloaded reference gallery already has stable IDs, pass its
approved manifest while generating vectors. The manifest must contain
`asset_id` and `local_path` or `relative_path`; every image in the input
directory must map to exactly one manifest row. This keeps embedding IDs
aligned with `reference-retrieve` instead of deriving new content hashes:

```powershell
python audit.py embedding-generate `
  data/approved/reference/chongqing-panoramax-images-v3 `
  data/approved/reference/chongqing-panoramax-v3-dinov2.csv `
  --backend transformers --model models/dinov2-base `
  --asset-manifest data/approved/reference/chongqing-panoramax-v3.csv
```

Relative paths are resolved against the image directory and then the manifest
directory. Rows for unavailable gallery assets may remain in a larger
manifest, but all discovered input images must be mapped; malformed or
ambiguous mappings fail closed.
