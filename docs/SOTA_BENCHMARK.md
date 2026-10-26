# 研究级基准路线

这个项目把“个人照片隐私审计”和“公开基准上的地理定位能力评估”拆成两个数据边界。SOTA 不是一个可以由单次运行证明的标签；它需要固定数据许可、测试拆分、模型检查点、推理参数和评估脚本，并在同一测试协议下比较。

## 公开基线

- `google-research/scaling-geoloc` 的官方实现按地点组织 query/aerial 数据，使用 `arrayrecord`、位置索引和专用评估入口；它报告 `recall@k@threshold` 与距离指标。参见 <https://github.com/google-research/scaling-geoloc/blob/main/README.md>。
- NeurIPS 论文报告其覆盖的大范围欧洲数据上，超过 68% 的查询误差不超过 200 米；这个数字只描述论文数据和协议，不能外推到个人照片。参见 <https://proceedings.neurips.cc/paper_files/paper/2025/hash/c2891461cb314d016da16efa06f7f1d5-Abstract-Conference.html>。
- IMAGEO-Bench 研究 LLM 的图像地理定位能力，适合参考分层误差、覆盖范围和推理能力的报告方式：<https://arxiv.org/abs/2508.01608>。
- ERGeoBench 将单视角、全景和交互式探索分开，提醒评估者把“看一张图”和“主动选择视角”当成不同任务：<https://github.com/kaiXuewen/ERGeoBench>。

## 固定协议

每个 benchmark run 应记录：

1. 数据集版本、许可证、下载来源、文件清单摘要和地理覆盖；
2. 按地点或场景分组的 train/validation/test，禁止相邻帧或同一拍摄序列跨 split；
3. 模型仓库 commit、权重 SHA-256、预处理、随机种子、硬件和依赖锁；
4. 失败预测如何计入分母、坐标合法性、时间限制和显存限制；
5. 原始预测文件的哈希，以及只输出聚合结果的公开报告。

## 指标

核心报告应至少包含中位数、平均值、P90/P95、失败率和 Haversine 命中率：

`200 m, 1 km, 25 km, 200 km, 750 km, 2500 km`

为了和官方 scaling-geoloc 结果对齐，工具同时输出 `50 m, 100 m, 500 m, 2 km, 5 km, 10 km, 50 km, 100 km, 500 km, 1000 km` 等细分阈值。

同时报告 bootstrap 95% 区间、国家/城市类型/场景类别分组结果，以及可用时的置信度校准。失败预测是命中失败，不能从命中率分母中删除。

## 运行等级

- **L0：** 只有聚合预测 CSV，无法检查 split 泄漏；结果只能做快速诊断。
- **L1：** 有 split 与场景/捕获身份字段，工具完成身份跨 split 泄漏审计。
- **L2：** 有固定 checkpoint、依赖锁、运行清单和独立留出集，结果可复现。
- **L3：** 有多个模型在同一测试集上运行，含分组结果、区间、校准和人工错误分析。

个人照片目录永远不进入这些 benchmark run。照片审计可以使用 hash 和本地 embedding 发现重复线索，但不调用地理搜索、反向地理编码或地址候选生成。

## 授权研究流水线

当数据许可允许时，研究运行按以下顺序保留中间产物：

`local encoder + provenance → reference-retrieve → grid-consensus rerank → fuse-candidates → benchmark calibration`

检索器使用参考图的 geotagged manifest 和同 checkpoint embedding；多图融合按每张图片归一化质量、空间连通分量、球面质心和冲突检测输出候选范围。融合的 confidence 仍是未校准启发式，必须用独立留出集校准后才能报告为概率。
