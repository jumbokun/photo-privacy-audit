# 中国参考数据

当前重庆研究样本包含：

- `data/approved/geodata/gadm41_CHN_3.json`：GADM 4.1 中国三级行政区数据；用于内部点落区，需遵守 GADM 的学术/非商业许可和中国边界适用范围说明。
- `data/approved/geodata/geoBoundaries-CHN-ADM2.geojson` 与 `geoBoundaries-CHN-ADM3.geojson`：本地行政边界；用于 `admin-match`，保留其开放许可与来源信息。
- `data/approved/reference/chongqing-commons/`：Wikimedia Commons 地理搜索样本，包含 `reference_manifest.csv`、图片、DINOv2/SigLIP embedding 和 provenance sidecar。

Wikimedia Commons 的 [GeoData API](https://www.mediawiki.org/wiki/API:Geosearch) 支持按坐标和半径查找带地理信息的文件，[ImageInfo API](https://www.mediawiki.org/wiki/API:Imageinfo) 提供文件 URL、缩略图和扩展元数据。下载脚本会保留文件标题、坐标、来源 URL、许可和 `authorization_status=public`，不会把来源照片当作个人照片。

GADM 4.1 按国家下载是官方推荐方式，但其数据仅限学术/非商业使用并有中国边界适用范围注意事项；参考：[GADM data](https://gadm.org/data.html)。

```powershell
python scripts/download_commons_geo.py `
  --city Chongqing --lat 29.563 --lon 106.552 `
  --radius 10000 --limit 50 `
  --output data/approved/reference/chongqing-commons
```

参考图库的质量需要单独审计：地理坐标正确不代表图像内容适合跨视角检索。车辆、重复上传和无地标照片应降权或排除；只有通过验证集的 reference gallery 才能用于高精度候选报告。

对于中国社群/媒体，推荐使用 `scripts/ingest_authorized_urls.py` 导入明确授权的 URL/导出。百度、腾讯、高德等街景服务需要各自账号、密钥和服务条款；本项目不绕过访问控制，也不把“网页能看到”当成“允许批量下载”。
