# GB10 测试数据目录清单（2026-09-30）

本清单通过 SSH 在 GB10 上只读采集，不是根据文件名推测生成。采集时间为机器报告的 `2026-09-29T17:08:06Z`（北京时间 9 月 30 日 01:08），未独立校准服务器时钟。完整字段见 [inventory.json](inventory.json)。

范围为明确选定的本项目实验数据目录及三个独立数据文件，不是整台机器扫描，也不包含 ComfyUI、模型权重、用户配置或其他项目。真实主目录、主机名、IP、SSH 端口和用户名均未发布。原始数据仍在 GB10，没有上传或重新分发。

## 目录结构与规模

以下相对于脱敏的 `<DATA_ROOT>`；只展示分组，具体文件名和精确字节数见 JSON。

```text
<DATA_ROOT>/
├── gpu-analysis-experiments/
│   ├── tlc-yellow-2017-raw/       12 个月原始 Parquet + 一份二列派生表
│   ├── tlc-yellow-2017/           另一份二列派生表
│   └── diversity-20260929/        gas-drift / retail-ii / susy + 原始归档
├── bigtest/                      大规模及宽表 CSV
├── sizetest/                     不同规模 CSV
├── shapetest/                    不同行列形状 CSV
├── sales_demo.csv
├── sales_demo_small.csv
└── power_clean.csv
```

| 分组 | 本次文件数 | 逻辑文件大小（十进制 GB） |
| --- | ---: | ---: |
| TLC 原始目录及其中派生表 | 13 | 1.896 |
| TLC 另一份派生表 | 1 | 0.295 |
| 新公开数据及归档 | 8 | 1.636 |
| bigtest | 8 | 15.202 |
| sizetest | 12 | 2.706 |
| shapetest | 4 | 2.785 |
| 三个独立 CSV | 3 | 3.722 |
| 合计 | **49** | **28.243** |

合计精确值为 28,242,762,577 字节。这里包含派生副本、压缩包及结果归档，**不是 49 份独立数据集**，也不是去重后的磁盘实际占用。两份 TLC 派生文件行数相同但字节大小/哈希不同，不能直接视为同一字节文件。`gas-incomplete.zip` 与 `gas.zip` 本次 SHA-256 相同，名称不能单独证明其损坏；本次不删除或改名任何文件。

## 与已有实验记录的对应

| 数据 | 本次从 Parquet footer 读取的行数 | 复核与历史证据 |
| --- | ---: | --- |
| TLC 2017 全年 12 个月 | 合计 113,500,327 | 月度文件逐个读取元数据；[数据获取/转换脚本](../../benchmark/public_tlc_dataset.py) |
| 两份 TLC 二列派生表 | 各 113,500,327 | 与月度元数据行数合计相同；未重新逐值核对派生转换。[核心实验](../FINAL_CORE_DEPLOYMENT.md) |
| Gas Sensor Array Drift | 13,910 | Parquet 和原始归档全文件 SHA-256 与 [source-gas.json](../new-public-data-20260929/source-gas.json) 一致 |
| Online Retail II | 1,067,371 | Parquet 和原始归档全文件 SHA-256 与 [source-retail.json](../new-public-data-20260929/source-retail.json) 一致 |
| SUSY | 5,000,000 | Parquet 和原始归档全文件 SHA-256 与 [source-susy.json](../new-public-data-20260929/source-susy.json) 一致 |

公开数据来源、许可限制、转换方法、测试命令和 CPU/GPU 对照见 [八组工作负载实验](../new-public-data-20260929/README.md)。TLC 实验的冷/热、独立/批量口径见 [核心验收](../FINAL_CORE_DEPLOYMENT.md)。这些仍是历史实验，不是此次目录采集重新测得的成绩。

CSV 本次只读取文件属性和满足大小阈值的全文件哈希，**没有逐行重数**。JSON 的 `rows: null` 表示未复核，不是零行；文件名中的数字不能作为本次实际行数证据。没有对应结果链接的文件只证明存在，不标为“已测试通过”。

## 采集方式与复现

使用 [dataset_inventory.py](../../scripts/dataset_inventory.py)，只输出相对路径、格式、字节数、可读取的 Parquet 行列元数据和可选 SHA-256，不输出数据记录。默认只对不超过 64 MiB 的文件计算完整哈希；本次显式设置 1024 MiB，44 个文件得到全文件哈希，5 个大文件明确跳过。17 个 Parquet 文件获得元数据。全部文件在各自检查前后的大小与修改时间一致；这不是整个目录的原子快照，也不防御检查期间恶意篡改并恢复时间戳。

在持有数据的机器上运行，替换为自己的路径：

```bash
python scripts/dataset_inventory.py --root /path/to/data-root --hash-max-mib 1024 --paths gpu-analysis-experiments/tlc-yellow-2017-raw gpu-analysis-experiments/tlc-yellow-2017 gpu-analysis-experiments/diversity-20260929 bigtest sizetest shapetest sales_demo_small.csv sales_demo.csv power_clean.csv
```

读取 Parquet 元数据需要 PyArrow；没有该依赖时 `parquet_metadata_available=false`，不会伪造行数。输出只包含指定目录下支持的后缀，排除隐藏路径、准备依赖目录、profiles 和符号链接，不读取 SSH 配置或模型连接文件。运行前自行检查所选目录的文件名也适合公开。

**证据边界：目录清单证明文件存在及部分文件身份；数值正确性和加速比仍需对应实验结果支撑。** 没有重新跑分析任务，没有改动 GB10 文件或服务，也没有上传原始压缩包及未经脱敏的日志、校准配置。
