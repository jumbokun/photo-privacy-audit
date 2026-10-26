# 中国环境部署

这套工具默认 `network_mode: offline`，不调用地图、街景、云端视觉模型或境外 API。所有原图、哈希、EXIF 存在性、视觉证据和报告都在本机处理。

## Windows

```powershell
cd C:\work\web3work\photo-privacy-audit
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-cn.ps1
python audit.py doctor
```

默认使用清华 PyPI 镜像；可以用组织镜像覆盖：

```powershell
$env:PHOTO_AUDIT_PIP_INDEX_URL = "https://你的组织镜像/simple"
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-cn.ps1
```

网络完全隔离时，在可联网机器下载 wheel：

```powershell
python -m pip download --dest wheelhouse -r requirements-dev.txt
```

把 `wheelhouse` 拷贝到目标机后：

```powershell
$env:PHOTO_AUDIT_WHEELHOUSE = "D:\wheelhouse"
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-cn.ps1
```

## Linux / WSL

```bash
cd /path/to/photo-privacy-audit
bash scripts/bootstrap-cn.sh
```

## 本地化细节

- 默认时区是 `Asia/Shanghai`，报告同时记录东八区时间和 UTC 时间。
- CSV 使用 UTF-8 BOM，适配 Windows Excel 打开中文字段和中文文件名。
- 中文文件名会进入稳定的本地资产映射，但普通报告不会写 GPS 坐标或设备字段值。
- `doctor` 会检查 Python、Pillow、PyYAML、tzdata、时区、离线策略和中文路径；ExifTool 是增强项，不是硬依赖。
- EXIF、视觉证据和 benchmark 数据不会自动上传到第三方 OCR、地图或模型服务。

pip 官方支持通过 `--index-url` 和 `PIP_INDEX_URL` 指定包索引，也支持 `--no-index --find-links` 从本地 wheelhouse 安装；脚本只使用这些标准机制。
