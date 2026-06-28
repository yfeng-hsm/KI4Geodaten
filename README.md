# KI4Geodaten

基于 Mapillary 街景影像与 Ollama 视觉语言模型的城市空间要素识别、定位和制图系统。

## 项目文档

- [系统开发规划](docs/system-plan.md)

## 当前实现

第一阶段验证应用已经包含：

- Mainz 全城 10,155 个 Census 100m 格网的固定 vector layer
- 官方完整 `GITTER_ID_100m`、`ETRS89-LAEA / EPSG:3035` 边界和人口属性
- 点击格网只选择并显示 Census 数据，不自动访问 Mapillary
- 用户点击“确认访问 Mapillary”后才请求选中格网
- 重复点击当前格网保留已加载结果，不重新要求确认；切换格网才重置
- 按选中格网请求和缓存 Mapillary 图像元数据
- 对 Mapillary BBOX 响应进行精确格网过滤
- OSM 底图、周边 100m 格网和图像点可视化
- 图像点按拍摄方向显示旋转箭头
- Mapillary 图像点可在原始 GPS 位置/方向与 Mapillary 校正位置/方向之间切换
- 图像缩略图、元数据和 Mapillary 街景链接
- 单个格网 Mapillary 数据的 GeoJSON 下载
- PostGIS 中保存 Mainz OSM 路网、建筑和土地利用数据
- 选中格网后按 cell 裁剪显示 OSM 专题图层，同时保留整条道路 GeoJSON 查询
- Leaflet layer control 可开关 Census、边界、OSM 专题图层和 Mapillary 图像方向
- 右上角分别显示 Mapillary 状态和 Ollama 模型状态，并可从 Ollama 已安装模型中下拉选择
- 当前 cell 的 Mapillary 图像加载后，可手动点击 Process 批量分析该 cell 所有图像并显示进度；默认跳过已有 VLM 结果
- 每张图片详情中也可单独点击 Process 当前图片，单图处理会覆盖该图片旧结果
- VLM 结果按图像 ID 持久化到 PostGIS，重复处理同一图像会覆盖旧字段，便于后续扩展 prompt
- VLM result thematic points 图层可按主题字段给已处理图片点着色显示

## 启动

1. 创建环境文件：

   ```bash
   cp .env.example .env
   ```

2. 在 `.env` 中设置 `MAPILLARY_ACCESS_TOKEN`。

3. 启动应用：

   ```bash
   docker compose up --build
   ```

4. 打开 [http://localhost:8000](http://localhost:8000)。

页面启动时从容器加载 Mainz 全城 100m Census vector layer。点击格网只读取人口、Mainz 面积占比和格网 ID；点击“确认访问 Mapillary”后才会请求该格网内的街景。重复请求默认使用 Docker 命名卷中的缓存。修改代码后重新执行 `docker compose up --build`。

构建镜像时会从 Destatis 官方下载全国 Zensus 2022 格网和行政边界，只提取 Mainz（AGS `07315000`）子集并写入最终镜像。当前字段包括人口、平均年龄、外国人比例、18岁以下比例、65岁以上比例、平均家庭规模及官方质量标记。运行中的容器不需要访问 Destatis。

## OSM / PostGIS 数据导入

启动数据库和应用后，执行：

```bash
docker compose exec app python scripts/import_osm_mainz.py
```

脚本会从 Overpass API 下载 Mainz 边界 bbox 内的 OSM `highway`、`building`、`landuse`、`natural`、`leisure` 和部分 `amenity` way，写入 PostGIS。表结构保留两套几何：

- `geom`：OSM 原始 way 几何，用于后续街景点匹配整条道路。
- `geom_mainz`：按 Mainz 行政边界裁剪后的几何，用于城市范围查询。

前端选择 cell 时调用 `/api/mainz/grids/{grid_id}/map-layers`，后端用 PostGIS `ST_Intersection` 返回该 cell 内裁剪后的路网、建筑和土地利用。路网 popup 中的“打开整条道路 GeoJSON”会调用 `/api/osm/roads/{osm_id}`，返回未按 cell 截断的整条道路。

Overpass 原始响应缓存到 Docker 卷中的 `/app/data/cache/osm_mainz_overpass.json`；需要重新下载时加 `--refresh`。

## GraphHopper Map Matching

Map matching 使用 GraphHopper `/match`，输入为同一个 Mapillary `sequence_id` 内的原始 GPS 点。应用不会再把一个 cell 内不同 sequence 的图片按时间随机拼成轨迹；如果缓存里没有真实 `sequence_id`，map matching 图层会提示需要重新确认访问 Mapillary 来刷新元数据。

在 `.env` 中配置 GraphHopper 服务地址：

```bash
GRAPHHOPPER_BASE_URL=http://graphhopper:8989
GRAPHHOPPER_TIMEOUT_SECONDS=30
```

GraphHopper 服务由 `Dockerfile.graphhopper` 从官方源码构建。首次使用前先把已经导入 PostGIS 的 Mainz 路网导出为 GraphHopper 可导入的 OSM XML：

```bash
docker compose exec app python scripts/export_graphhopper_osm.py
docker compose up -d --build graphhopper app
```

GraphHopper 会从 `data/graphhopper/mainz.osm.xml` 导入图，并启用 `car`、`bike`、`foot` profiles。后端会根据该 sequence 内 VLM 的 `capture_position` 多数值选择 profile：车行观测使用 `car`，自行车观测使用 `bike`，行人观测使用 `foot`。如果还没有 VLM 结果，默认使用 `car`。

前端 `Map matched raw GPS` 图层中，橙色虚线是真实 Mapillary sequence 原始 GPS 轨迹，蓝色线是 GraphHopper 匹配后的轨迹；点击点可查看原图、原始 GPS 坐标和 GraphHopper snapped 坐标。

## Ollama VLM

应用通过 `OLLAMA_BASE_URL` 连接远程 Ollama。默认 compose 配置使用：

```bash
OLLAMA_BASE_URL=http://100.87.51.96:11434
OLLAMA_MODEL=gemma4:26b
OLLAMA_IMAGE_THUMB_SIZE=512
OLLAMA_CONCURRENCY=4
```

右上角状态会分别显示 Mapillary 配置状态和 Docker 容器内 Ollama 连通状态。模型下拉框来自 Ollama `/api/tags`。VLM 不会自动分析；需要先选择 cell、确认访问 Mapillary，然后点击右侧 Process 按钮处理当前 cell 的全部图像。

VLM 默认使用最大 512px 图像输入：后端优先下载 Mapillary `thumb_1024_url` 并在容器内缩放到 512px，再发给 Ollama。前端预览仍使用 `thumb_1024_url`。如果后续字段需要更细的远处设施识别，可以在 `.env` 中把 `OLLAMA_IMAGE_THUMB_SIZE` 改为 `1024` 后重建容器；如果需要更快但更粗略，可改为 `256`。

队列仍然一次只执行一个 VLM job，但单个 job 内会按 `OLLAMA_CONCURRENCY` 并发请求 Ollama；默认值为 `4`，对应 Ollama 服务端配置的 parallel 4。取消正在运行的 job 时，最多需要等待当前并发批次内的请求返回。

分析结果逐张写入 PostGIS 表 `vlm_image_analysis`，不会等整个 cell 完成后才保存。PostGIS 使用 Docker volume `postgres-data`，因此容器重启后结果仍保留。cell 批处理默认跳过已经存在的 `image_id`，避免重复消耗模型；勾选“覆盖已有 VLM 结果”后才会重新处理并覆盖当前 cell。每张图片详情中的单图 Process 会覆盖该图片旧结果。

表内使用 `fields jsonb` 保存字段，因此后续添加新的字段时，重新处理同一图像会覆盖旧结果；历史记录缺失的新字段在前端显示为 `null`。模型无法判断的字段必须由 prompt 和后端规范化为 `uncertain`。

当前 prompt 提取字段：

- `unusable_reason`: `none`, `poor_image_quality`, `transit_vehicle`, `railway_scene`, `uncertain`
- `capture_position`: `vehicle_road`, `pedestrian_road`, `bicycle_road`, `other_location`, `uncertain`
- `surface_material`: `asphalt`, `concrete`, `paving_stones`, `sett`, `unpaved`, `uncertain`
- `left_sidewalk`: `yes`, `no`, `uncertain`, `null`
- `left_sidewalk_surface_material`: `asphalt`, `concrete`, `paving_stones`, `sett`, `unpaved`, `uncertain`, `null`
- `right_sidewalk`: `yes`, `no`, `uncertain`, `null`
- `right_sidewalk_surface_material`: `asphalt`, `concrete`, `paving_stones`, `sett`, `unpaved`, `uncertain`, `null`
- `left_adjacent_road_type`: `vehicle_road`, `bicycle_road`, `none`, `uncertain`, `null`
- `left_adjacent_road_surface_material`: `asphalt`, `concrete`, `paving_stones`, `sett`, `unpaved`, `uncertain`, `null`
- `right_adjacent_road_type`: `vehicle_road`, `bicycle_road`, `none`, `uncertain`, `null`
- `right_adjacent_road_surface_material`: `asphalt`, `concrete`, `paving_stones`, `sett`, `unpaved`, `uncertain`, `null`
- `traffic_signal`: `yes`, `no`, `uncertain`
- `bench`: `yes`, `no`, `uncertain`
- `waste_basket`: `yes`, `no`, `uncertain`
- `independent_bicycle_road`: `yes`, `no`, `uncertain`
- `independent_pedestrian_road`: `yes`, `no`, `uncertain`

`left_sidewalk` 和 `right_sidewalk` 只对 `capture_position=vehicle_road` 的观察生效，以相机朝向为前方判断左右侧。行人道、自行车道和其它位置会写入 `null`。单排路缘石、curbstone 或边界石不算 `paving_stones`。`paving_stones` 表示平整、闭合、缝隙很窄的块材/砖/石板通行面；`sett` 表示更粗糙的天然石块铺面，石块之间不完全闭合且缝隙更明显。

`left_adjacent_road_type` 和 `right_adjacent_road_type` 只对 `capture_position=pedestrian_road` 的观察生效，以相机朝向为前方判断行人道路两侧是否紧邻车行道或自行车道，并记录该相邻道路可见通行面的表面材质。道路匹配和 road surface validation 会优先把这些相邻道路表面作为被匹配车行道/自行车道的表面观测，避免把人行道脚下材质误用于车行道。

火车、电车、轻轨等轨道交通车辆上/车厢内拍摄的图像，或主要对应铁路轨道空间的图像，会写入 `unusable_reason=transit_vehicle` 或 `railway_scene`，并强制 `capture_position=other_location`、`surface_material=uncertain`。道路匹配和 road surface validation 会排除这些图像。

过暗、过曝、严重模糊、遮挡、画面主要不是可判读街道空间的图像会写入 `unusable_reason=poor_image_quality`。这些图像同样会被道路匹配和 road surface validation 排除；当前 cell 的 5m 代表点自动处理会在同一 5m 聚类内继续寻找未处理图片作为替代，直到找到可用结果或该聚类图片耗尽。

## 测试

```bash
docker compose run --rm app pytest
```

开发依赖位于 `requirements-dev.txt`。Docker 生产镜像默认只安装运行依赖。

## 数据与坐标规则

- 格网使用 `ETRS89-LAEA Europe (EPSG:3035)`。
- 格网单元严格按 100 米整倍数对齐。
- ID 使用 Zensus 2022 CSV 的完整格式，例如 `CRS3035RES100mN2987100E4196900`。
- 最终镜像只包含约 4.7 MB 的 Mainz 子集，不包含全国原始 ZIP。
- 后续 Zensus 社会经济 CSV 可通过 `GITTER_ID_100m` 与 `grid_id` 直接关联。
- 容器内缓存保存在 Docker 命名卷 `mapillary-cache` 中。

## API

- `GET /api/health`
- `GET /api/ollama/status`
- `GET /api/mainz`
- `GET /api/mainz/grids`
- `GET /api/mainz/grids/{grid_id}`
- `GET /api/mainz/grids/{grid_id}/map-layers`
- `GET /api/mainz/road-surface-validation`
  - Mainz 全域道路 surface 评估。只返回 OSM `surface` 可归类且至少有 3 个 VLM `surface_material` 观测的 road segment；绿色表示语义一致，红色表示不一致。`sett`、`cobblestone`、`unhewn_cobblestone` 会归为 `sett`；`compacted`、`fine_gravel`、`gravel`、`ground`、`grass_paver` 等 OSM surface 会归为 `unpaved`。
- `GET /api/mainz/grids/{grid_id}/road-surface-validation`
  - 当前 100m cell 内道路 surface 评估，规则同全域接口。
- `GET /api/osm/roads/{osm_id}`
- `GET /api/osm/roads/{osm_id}/vlm-matches`
  - 参数：`max_distance_m` 默认 35，`close_override_m` 默认 5，`view_fov_deg` 默认 110，`on_road_visible_m` 默认 1，`no_heading_visible_m` 默认 5，`road_axis_tolerance_deg` 默认 35，`limit` 默认 200。
  - 匹配规则：每个 VLM 图像点只分配给一条兼容道路；`vehicle_road` 只能匹配车行道路，`pedestrian_road` 和 `bicycle_road` 可以匹配车行道路。优先选择 5m 内最近兼容道路，否则选择 35m 内同类型最近道路。匹配还需要满足道路最近点落在图像前方视野锥内，或图像朝向与最近道路段轴线基本一致。图像点距离道路不超过 1m 时视为站在道路上，保留匹配；无朝向历史点只保留 5m 内最近道路。
- `GET /api/grids/by-point?longitude=13.4095&latitude=52.5208`
- `GET /api/grids/around?longitude=13.4095&latitude=52.5208&radius=4`
- `GET /api/grids/{grid_id}/images`
- `GET /api/grids/{grid_id}/images?refresh=true`
- `GET /api/grids/{grid_id}/images.geojson`
- `POST /api/vlm/analyze-image`
- `GET /api/vlm-results`
- `GET /api/grids/{grid_id}/vlm-results`
- `POST /api/grids/{grid_id}/vlm-jobs`
- `GET /api/vlm/jobs`
- `GET /api/vlm/jobs/{job_id}`

VLM 分析结果写入 PostgreSQL 表 `vlm_image_analysis`。任务队列写入 PostgreSQL 表
`vlm_processing_jobs` 和 `vlm_processing_job_items`，容器重启后仍可查看最近任务，
处于 `running` 的任务会回到 `queued` 等待继续处理。Mapillary GeoJSON 文件缓存只用于避免重复访问
Mapillary API，不保存 VLM 标注结果。

交互式 API 文档位于 [http://localhost:8000/docs](http://localhost:8000/docs)。

## 来源标注

- 格网规则与坐标体系：GeoBasis-DE / BKG
- Census 网格数据：Statistische Ämter des Bundes und der Länder
- 街景影像与元数据：Mapillary
- 地图底图：OpenStreetMap contributors
