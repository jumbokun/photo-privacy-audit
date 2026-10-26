# 多图联合定位候选融合

`photo_audit.multi_image_fusion` 用于研究级 geolocation 评估：它接收每张图像由本地模型或获准检索器产生的候选坐标，按空间距离聚类，再把不同图片的证据融合成候选范围。输出的是 `status=candidate` 的中心点、半径和支持证据；模块不会反向地理编码，也不会把候选点写成已验证地址。

## 输入

输入可以是 CSV、JSON 或 JSONL。每一行至少包含：

```json
{
  "image_id": "photo-01",
  "candidate_id": "photo-01:1",
  "lat": 48.2083,
  "lon": 16.3731,
  "score": 0.91,
  "confidence": 0.8,
  "source": "retrieval",
  "uncertainty_km": 0.5,
  "evidence": "bridge geometry match"
}
```

`score` 和 `confidence` 必须在 0 到 1 之间。`source` 只影响透明的权重先验；有验证集时应使用该数据估计各来源的可靠性。`uncertainty_km` 表示候选来源自身的范围，值越大权重越低。

## 方法

1. 每张图像内部先按 `score × confidence × source_reliability` 归一化为质量，避免 top-k 较长的图像支配所有证据。
2. 用纬度带索引和 Haversine 距离构建空间连通分量。`cluster_radius_km` 是证据归组阈值，不是最终误差保证；链式分量的 `spread_km` 会被保留并降低一致性分数。
3. 每个分量使用加权球面质心，计算候选半径（到质心的加权 90 分位距离）、支持图片数、来源多样性、平均候选分数和不确定性。
4. `confidence` 和 `ranking_score` 是可解释的未校准启发式。只有在获准、固定的留出集上做校准后，才能把它们解释成概率。
5. 若两个空间分离的分量有相近支持度，输出 `status=conflict`，并在 `conflict.pairs` 中保留距离、相对分数和共享图片信息，避免强行合并成一个结论。

## 运行示例

```python
from photo_audit.multi_image_fusion import fuse_multiview, load_candidates

rows = load_candidates("candidates.jsonl")
report = fuse_multiview(rows, cluster_radius_km=2.0, top_k=5)
for candidate in report["clusters"]:
    print(candidate["center_lat"], candidate["center_lon"], candidate["radius_km"])
```

命令行等价写法：

```powershell
python audit.py fuse-candidates candidates.jsonl fusion.json --cluster-radius 2 --top-k 5
```

`reference-retrieve` 产生的 `match_status=no_candidates` 行会在 CLI 融合前自动跳过；其他输入行必须满足上述字段契约。

多图只有在图像属于同一拍摄组、候选来源具有独立证据时才会提高精度。相同模型对同一错误先验的重复输出不算独立验证；应在报告中记录模型、checkpoint、参考库和检索时间，并用 benchmark 的 Recall@K、距离阈值命中率和校准误差评估实际效果。个人照片仍应遵守项目的隐私边界，候选范围不能直接当作家庭地址或已验证定位。

