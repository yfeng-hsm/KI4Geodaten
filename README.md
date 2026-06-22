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
- 图像缩略图、元数据和 Mapillary 街景链接
- 单个格网 Mapillary 数据的 GeoJSON 下载

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
- `GET /api/mainz`
- `GET /api/mainz/grids`
- `GET /api/mainz/grids/{grid_id}`
- `GET /api/grids/by-point?longitude=13.4095&latitude=52.5208`
- `GET /api/grids/around?longitude=13.4095&latitude=52.5208&radius=4`
- `GET /api/grids/{grid_id}/images`
- `GET /api/grids/{grid_id}/images?refresh=true`
- `GET /api/grids/{grid_id}/images.geojson`

交互式 API 文档位于 [http://localhost:8000/docs](http://localhost:8000/docs)。

## 来源标注

- 格网规则与坐标体系：GeoBasis-DE / BKG
- Census 网格数据：Statistische Ämter des Bundes und der Länder
- 街景影像与元数据：Mapillary
- 地图底图：OpenStreetMap contributors
