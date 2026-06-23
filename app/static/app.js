const map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([49.9929, 8.2473], 12);

window.addEventListener("load", () => map.invalidateSize({ pan: false }));
window.addEventListener("resize", () => map.invalidateSize({ pan: false }));

const osmBasemap = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxNativeZoom: 19,
  maxZoom: 20,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
}).addTo(map);

map.createPane("landusePane");
map.createPane("buildingPane");
map.createPane("pedestrianRoadPane");
map.createPane("vehicleRoadPane");
map.createPane("bicycleRoadPane");
map.createPane("vlmResultPane");
map.createPane("mapillaryImagePane");
map.createPane("gridPane");
map.createPane("boundaryPane");
map.getPane("landusePane").style.zIndex = 410;
map.getPane("gridPane").style.zIndex = 420;
map.getPane("buildingPane").style.zIndex = 430;
map.getPane("vehicleRoadPane").style.zIndex = 450;
map.getPane("bicycleRoadPane").style.zIndex = 455;
map.getPane("pedestrianRoadPane").style.zIndex = 460;
map.getPane("boundaryPane").style.zIndex = 460;
map.getPane("vlmResultPane").style.zIndex = 500;
map.getPane("mapillaryImagePane").style.zIndex = 510;
map.getPane("landusePane").style.pointerEvents = "none";
map.getPane("buildingPane").style.pointerEvents = "none";
map.getPane("pedestrianRoadPane").style.pointerEvents = "none";
map.getPane("vehicleRoadPane").style.pointerEvents = "none";
map.getPane("bicycleRoadPane").style.pointerEvents = "none";
map.getPane("gridPane").style.pointerEvents = "auto";
map.getPane("boundaryPane").style.pointerEvents = "none";
map.getPane("vlmResultPane").style.pointerEvents = "none";
map.getPane("mapillaryImagePane").style.pointerEvents = "none";

const gridRenderer = L.canvas({ pane: "gridPane", padding: 0.4 });
const landuseRenderer = L.canvas({ pane: "landusePane", padding: 0.4 });
const buildingRenderer = L.canvas({ pane: "buildingPane", padding: 0.4 });
const pedestrianRoadRenderer = L.canvas({ pane: "pedestrianRoadPane", padding: 0.4 });
const vehicleRoadRenderer = L.canvas({ pane: "vehicleRoadPane", padding: 0.4 });
const bicycleRoadRenderer = L.canvas({ pane: "bicycleRoadPane", padding: 0.4 });
const vlmResultRenderer = L.svg({ pane: "vlmResultPane", padding: 0.4 });
let selectedGridLayer = null;
let currentGrid = null;
let currentImageFeature = null;
let currentImageFeatures = [];
let vlmResultsByImageId = {};
let allVlmResultsByImageId = {};
let mapillaryGeometryMode = "original";
let requestSequence = 0;
let cellMapSequence = 0;
let vlmResultsSequence = 0;
let allVlmResultsSequence = 0;
let activeVlmJobId = null;
let vlmJobTimer = null;
let ollamaReady = false;
let selectedModel = "";
let selectedVlmTheme = "capture_position";

const VLM_DISPLAY_FIELDS = [
  "capture_position",
  "surface_material",
  "traffic_signal",
  "bench",
  "waste_basket",
  "independent_bicycle_road",
  "independent_pedestrian_road",
  "confidence",
  "reason",
];

const VLM_THEME_FIELDS = [
  "capture_position",
  "surface_material",
  "traffic_signal",
  "bench",
  "waste_basket",
  "independent_bicycle_road",
  "independent_pedestrian_road",
];

function censusGridStyle(feature) {
  const population = feature.properties.population;
  const intensity = population == null ? 0 : Math.min(population / 150, 1);
  return {
    renderer: gridRenderer,
    color: "#3d5149",
    weight: 0.45,
    opacity: 0.72,
    fillColor: population == null ? "#dbe3df" : `hsl(${150 - intensity * 45} 46% ${86 - intensity * 35}%)`,
    fillOpacity: population == null ? 0.03 : 0.22,
  };
}

const gridLayer = L.geoJSON(null, {
  renderer: gridRenderer,
  style: censusGridStyle,
  onEachFeature: (feature, layer) => {
    layer.on("click", (event) => {
      L.DomEvent.stopPropagation(event);
      selectGrid(feature, layer);
    });
  },
}).addTo(map);

const boundaryLayer = L.geoJSON(null, {
  pane: "boundaryPane",
  interactive: false,
  style: { color: "#172e25", weight: 2.2, fill: false, opacity: 0.9 },
}).addTo(map);

const imageLayer = L.geoJSON(null, {
  pointToLayer: (feature, latlng) => {
    const properties = feature.properties;
    const angle = Number(displayCompassAngle(properties));
    return L.marker(latlng, {
      icon: L.divIcon({
        className: "direction-marker",
        html: `<span style="transform: rotate(${Number.isFinite(angle) ? angle : 0}deg)">▲</span>`,
        iconSize: [20, 20],
        iconAnchor: [10, 10],
      }),
      pane: "mapillaryImagePane",
      keyboard: true,
      title: `拍摄方向 ${Number.isFinite(angle) ? angle.toFixed(1) : "0.0"}°`,
    });
  },
  onEachFeature: (feature, layer) => {
    layer.on("click", (event) => {
      L.DomEvent.stopPropagation(event);
      showImage(feature);
    });
  },
}).addTo(map);

const vlmResultLayer = L.geoJSON(null, {
  pane: "vlmResultPane",
  pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
    pane: "vlmResultPane",
    radius: 7,
    color: "#ffffff",
    weight: 2,
    fillColor: vlmThemeColor(feature.properties.theme_value),
    fillOpacity: 0.9,
    className: "vlm-result-point",
    interactive: true,
    renderer: vlmResultRenderer,
  }),
  onEachFeature: (feature, layer) => {
    layer.on("click", (event) => {
      L.DomEvent.stopPropagation(event);
      const sourceFeature = currentImageFeatures.find((item) => String(item.id) === feature.properties.image_id);
      if (sourceFeature) {
        showImage(sourceFeature);
      } else {
        showStoredVlmPoint(feature.properties.image_id);
      }
    });
    layer.bindTooltip(
      `${feature.properties.image_id}<br>${selectedVlmTheme}: ${feature.properties.theme_value}`,
      { direction: "top", opacity: 0.9 }
    );
  },
}).addTo(map);

const landuseLayer = L.geoJSON(null, {
  renderer: landuseRenderer,
  interactive: false,
  style: (feature) => ({
    color: landuseColor(feature.properties.class_name),
    weight: 2,
    opacity: 0.95,
    fillColor: landuseColor(feature.properties.class_name),
    fillOpacity: 0.42,
  }),
  onEachFeature: (feature, layer) => {
    const properties = feature.properties;
    layer.bindPopup(`
      <strong>${escapeHtml(properties.name || properties.class_name || "Landuse")}</strong><br>
      ${escapeHtml(properties.kind || "landuse")}: ${escapeHtml(properties.class_name || "unknown")}<br>
      面积 ${formatMetric(properties.area_m2, " m²", 1)}
    `);
  },
}).addTo(map);

const buildingLayer = L.geoJSON(null, {
  renderer: buildingRenderer,
  interactive: false,
  style: {
    color: "#004c99",
    weight: 2,
    opacity: 1,
    fillColor: "#1c7ed6",
    fillOpacity: 0.58,
  },
  onEachFeature: (feature, layer) => {
    const properties = feature.properties;
    layer.bindPopup(`
      <strong>${escapeHtml(properties.name || "Building")}</strong><br>
      building=${escapeHtml(properties.building || "yes")}<br>
      面积 ${formatMetric(properties.area_m2, " m²", 1)}
    `);
  },
}).addTo(map);

const pedestrianRoadLayer = createRoadLayer(pedestrianRoadRenderer);
const vehicleRoadLayer = createRoadLayer(vehicleRoadRenderer);
const bicycleRoadLayer = createRoadLayer(bicycleRoadRenderer);

L.control.layers(
  { OpenStreetMap: osmBasemap },
  {
    "Census 100m grid": gridLayer,
    "Mainz boundary": boundaryLayer,
    "OSM pedestrian roads": pedestrianRoadLayer,
    "OSM vehicle roads": vehicleRoadLayer,
    "OSM bicycle roads": bicycleRoadLayer,
    "OSM buildings in selected cell": buildingLayer,
    "OSM landuse in selected cell": landuseLayer,
    "Mapillary images": imageLayer,
    "VLM result thematic points": vlmResultLayer,
  },
  { collapsed: false, position: "topright" }
).addTo(map);

const gridIdElement = document.getElementById("grid-id");
const gridCoordinatesElement = document.getElementById("grid-coordinates");
const imageCountElement = document.getElementById("image-count");
const loadStatusElement = document.getElementById("load-status");
const mapDataStatusElement = document.getElementById("map-data-status");
const confirmMapillaryButton = document.getElementById("confirm-mapillary-button");
const downloadLink = document.getElementById("download-link");
const mapillaryGeometryModeSelect = document.getElementById("mapillary-geometry-mode");
const mapillaryHealthElement = document.getElementById("mapillary-health");
const modelHealthElement = document.getElementById("model-health");
const modelSelectElement = document.getElementById("model-select");
const imageDetailElement = document.getElementById("image-detail");
const processCellVlmButton = document.getElementById("process-cell-vlm-button");
const vlmProgressElement = document.getElementById("vlm-progress");
const vlmStatusElement = document.getElementById("vlm-status");
const forceVlmCheckbox = document.getElementById("force-vlm-checkbox");
const vlmThemeSelect = document.getElementById("vlm-theme-select");
const censusPopulationElement = document.getElementById("census-population");
const censusAverageAgeElement = document.getElementById("census-average-age");
const censusForeignersElement = document.getElementById("census-foreigners");
const censusUnder18Element = document.getElementById("census-under-18");
const censusOver65Element = document.getElementById("census-over-65");
const censusHouseholdSizeElement = document.getElementById("census-household-size");
const censusQualityFlagsElement = document.getElementById("census-quality-flags");
const censusCityShareElement = document.getElementById("census-city-share");
const censusGridIdElement = document.getElementById("census-grid-id");

async function apiGet(url) {
  const response = await fetch(url);
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (_error) {
      // Keep the HTTP status when the response is not JSON.
    }
    throw new Error(message);
  }
  return response.json();
}

async function apiPost(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (_error) {
      // Keep the HTTP status when the response is not JSON.
    }
    throw new Error(message);
  }
  return response.json();
}

async function checkHealth() {
  try {
    const health = await apiGet("/api/health");
    const ollama = health.ollama || {};
    ollamaReady = Boolean(ollama.connected && ollama.model_available !== false);
    mapillaryHealthElement.textContent = health.mapillary_configured
      ? "Mapillary 已配置"
      : "Mapillary 未配置";
    mapillaryHealthElement.classList.toggle("warning", !health.mapillary_configured);
    updateModelPicker(ollama);
    updateProcessButtonState();
  } catch (_error) {
    mapillaryHealthElement.textContent = "Mapillary 状态未知";
    mapillaryHealthElement.classList.add("warning");
    modelHealthElement.textContent = "模型状态未知";
    modelHealthElement.parentElement.classList.add("warning");
    modelSelectElement.disabled = true;
  }
}

function updateModelPicker(ollama) {
  const models = Array.isArray(ollama.models) ? ollama.models.filter(Boolean) : [];
  selectedModel = selectedModel || ollama.model || models[0] || "";
  if (!models.includes(selectedModel) && models.length > 0) {
    selectedModel = ollama.model && models.includes(ollama.model) ? ollama.model : models[0];
  }
  modelSelectElement.innerHTML = "";
  if (models.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = ollama.connected ? (ollama.model || "无模型") : "未连接";
    modelSelectElement.append(option);
  } else {
    models.forEach((model) => {
      const option = document.createElement("option");
      option.value = model;
      option.textContent = model;
      option.selected = model === selectedModel;
      modelSelectElement.append(option);
    });
  }
  modelSelectElement.disabled = !ollamaReady || models.length === 0;
  modelHealthElement.textContent = ollama.connected ? "模型已连接" : "模型未连接";
  modelHealthElement.parentElement.classList.toggle("warning", !ollamaReady);
}

async function initializeMainz() {
  loadStatusElement.textContent = "正在从容器加载 Mainz Census 格网…";
  try {
    const [mainz, grids] = await Promise.all([
      apiGet("/api/mainz"),
      apiGet("/api/mainz/grids"),
    ]);
    boundaryLayer.addData(mainz.boundary);
    gridLayer.addData(grids);
    map.fitBounds(boundaryLayer.getBounds(), { padding: [18, 18] });
    gridIdElement.textContent = `Mainz 全城 · ${grids.meta.cell_count.toLocaleString("zh-CN")} 个格网`;
    gridCoordinatesElement.textContent = `AGS ${grids.meta.ags} · Zensus ${grids.meta.census_date} · EPSG:3035`;
    loadStatusElement.textContent = "点击任一格网查看 Census 数据；不会自动访问 Mapillary。";
  } catch (error) {
    setError(error.message);
  }
}

function selectGrid(feature, layer) {
  if (selectedGridLayer) selectedGridLayer.setStyle(censusGridStyle(selectedGridLayer.feature));
  selectedGridLayer = layer;
  selectedGridLayer.setStyle({ color: "#f2a900", weight: 3, fillOpacity: 0.18 });
  selectedGridLayer.bringToFront();
  currentGrid = feature;
  requestSequence += 1;

  const properties = feature.properties;
  gridIdElement.textContent = properties.grid_id;
  gridCoordinatesElement.textContent = `EPSG:3035 · SW ${properties.x_sw}, ${properties.y_sw}`;
  censusGridIdElement.textContent = properties.grid_id;
  censusPopulationElement.textContent = properties.population == null
    ? "未公布/无记录"
    : `${properties.population.toLocaleString("zh-CN")} 人`;
  censusAverageAgeElement.textContent = formatMetric(properties.average_age, " 岁", 1);
  censusForeignersElement.textContent = formatMetric(properties.foreigners_pct, "%", 1);
  censusUnder18Element.textContent = formatMetric(properties.under_18_pct, "%", 1);
  censusOver65Element.textContent = formatMetric(properties.over_65_pct, "%", 1);
  censusHouseholdSizeElement.textContent = formatMetric(properties.average_household_size, " 人", 2);
  const qualityFlags = Object.entries(properties.quality_flags || {});
  censusQualityFlagsElement.textContent = qualityFlags.length
    ? qualityFlags.map(([field, note]) => `${field}: ${note}`).join("；")
    : "无特殊标记";
  censusCityShareElement.textContent = `${(properties.city_area_share * 100).toFixed(1)}%`;

  imageLayer.clearLayers();
  currentImageFeature = null;
  currentImageFeatures = [];
  vlmResultsByImageId = {};
  activeVlmJobId = null;
  stopVlmJobPolling();
  clearRoadLayers();
  buildingLayer.clearLayers();
  landuseLayer.clearLayers();
  imageCountElement.textContent = "0";
  confirmMapillaryButton.disabled = false;
  confirmMapillaryButton.textContent = "确认访问 Mapillary";
  downloadLink.classList.add("disabled");
  downloadLink.href = "#";
  loadStatusElement.classList.remove("error");
  loadStatusElement.textContent = "已选择格网。点击“确认访问 Mapillary”后才会请求街景数据。";
  resetVlmPanel("VLM 尚未处理。先确认访问 Mapillary，再手动处理当前 cell。");
  mapDataStatusElement.classList.remove("error");
  mapDataStatusElement.textContent = "正在从 PostGIS 读取该 cell 的 OSM 图层…";
  showAwaitingConfirmation();
  zoomToCurrentGrid();
  loadCellMapLayers(properties.grid_id);
  loadVlmResults(properties.grid_id);
}

async function loadCellMapLayers(gridId) {
  const sequence = ++cellMapSequence;
  try {
    const result = await apiGet(`/api/mainz/grids/${encodeURIComponent(gridId)}/map-layers`);
    if (sequence !== cellMapSequence) return;
    clearRoadLayers();
    buildingLayer.clearLayers();
    landuseLayer.clearLayers();
    if (!result.available) {
      mapDataStatusElement.textContent = `OSM 图层不可用：${result.reason}`;
      return;
    }
    landuseLayer.addData(result.layers.landuse);
    buildingLayer.addData(result.layers.buildings);
    const roads = splitRoads(result.layers.roads.features);
    pedestrianRoadLayer.addData(featureCollection(roads.pedestrian));
    vehicleRoadLayer.addData(featureCollection(roads.vehicle));
    bicycleRoadLayer.addData(featureCollection(roads.bicycle));
    mapDataStatusElement.textContent = `OSM cell图层：${result.meta.counts.roads} 条路（行人 ${roads.pedestrian.length}，车行 ${roads.vehicle.length}，自行车 ${roads.bicycle.length}），${result.meta.counts.buildings} 个建筑，${result.meta.counts.landuse} 个土地利用面。`;
  } catch (error) {
    if (sequence !== cellMapSequence) return;
    mapDataStatusElement.textContent = `OSM 图层加载失败：${error.message}`;
    mapDataStatusElement.classList.add("error");
  }
}

function createRoadLayer(renderer) {
  return L.geoJSON(null, {
    renderer,
    interactive: false,
    style: (feature) => ({
      color: roadColor(feature.properties.road_category),
      weight: roadWeight(feature.properties.road_category),
      opacity: 1,
    }),
    onEachFeature: (feature, layer) => {
      const properties = feature.properties;
      const roadUrl = `/api/osm/roads/${encodeURIComponent(properties.osm_id)}`;
      layer.bindPopup(`
        <div class="road-popup">
          <strong>${escapeHtml(properties.name || properties.highway || "Road")}</strong>
          类型=${escapeHtml(roadCategoryLabel(properties.road_category))}<br>
          highway=${escapeHtml(properties.highway || "unknown")}<br>
          cell内长度 ${formatMetric(properties.length_m, " m", 1)}<br>
          <a href="${roadUrl}" target="_blank" rel="noreferrer">打开整条道路 GeoJSON</a>
        </div>
      `);
    },
  }).addTo(map);
}

function clearRoadLayers() {
  pedestrianRoadLayer.clearLayers();
  vehicleRoadLayer.clearLayers();
  bicycleRoadLayer.clearLayers();
}

function splitRoads(features) {
  const groups = { pedestrian: [], vehicle: [], bicycle: [] };
  features.forEach((feature) => {
    const category = roadCategory(feature.properties);
    feature.properties.road_category = category;
    groups[category].push(feature);
  });
  return groups;
}

function featureCollection(features) {
  return { type: "FeatureCollection", features };
}

function roadCategory(properties) {
  const highway = properties.highway;
  const tags = properties.tags || {};
  if (highway === "cycleway" || tags.bicycle === "designated") return "bicycle";
  if (["footway", "path", "steps", "pedestrian", "platform", "corridor", "elevator"].includes(highway)) {
    return "pedestrian";
  }
  return "vehicle";
}

function roadCategoryLabel(category) {
  if (category === "pedestrian") return "行人道路";
  if (category === "bicycle") return "自行车路";
  return "车行道路";
}

function zoomToCurrentGrid() {
  if (!selectedGridLayer) return;
  map.fitBounds(selectedGridLayer.getBounds(), {
    padding: [140, 140],
    maxZoom: 17,
  });
}

async function loadImages() {
  if (!currentGrid) return;
  const sequence = ++requestSequence;
  const gridId = currentGrid.properties.grid_id;
  imageLayer.clearLayers();
  imageCountElement.textContent = "…";
  loadStatusElement.textContent = "已确认，正在访问 Mapillary…";
  loadStatusElement.classList.remove("error");
  confirmMapillaryButton.disabled = true;

  try {
    const result = await apiGet(`/api/grids/${encodeURIComponent(gridId)}/images`);
    if (sequence !== requestSequence) return;
    currentImageFeatures = result.features || [];
    renderImageLayer();
    imageCountElement.textContent = result.meta.count;
    const cacheText = result.meta.cache === "hit" ? "容器缓存" : "Mapillary API";
    const truncated = result.meta.truncated ? "；结果达到 API 上限" : "";
    loadStatusElement.textContent = `${cacheText} · ${formatDate(result.meta.fetched_at)}${truncated}`;
    downloadLink.classList.remove("disabled");
    downloadLink.href = `/api/grids/${encodeURIComponent(gridId)}/images.geojson`;
    if (result.features.length === 0) showEmptyImageState();
    updateProcessButtonState();
    await loadVlmResults(gridId);
    confirmMapillaryButton.textContent = "Mapillary 已加载";
    confirmMapillaryButton.disabled = true;
  } catch (error) {
    if (sequence !== requestSequence) return;
    currentImageFeatures = [];
    updateProcessButtonState();
    imageCountElement.textContent = "0";
    setError(error.message);
    if (sequence === requestSequence) confirmMapillaryButton.disabled = false;
  }
}

async function loadVlmResults(gridId) {
  const sequence = ++vlmResultsSequence;
  try {
    const result = await apiGet(`/api/grids/${encodeURIComponent(gridId)}/vlm-results`);
    if (sequence !== vlmResultsSequence || !currentGrid || currentGrid.properties.grid_id !== gridId) return;
    vlmResultsByImageId = result.results || {};
    Object.assign(allVlmResultsByImageId, vlmResultsByImageId);
    const count = result.count || 0;
    const latestUpdatedAt = latestVlmUpdatedAt(vlmResultsByImageId);
    const latestText = latestUpdatedAt ? `；最后更新 ${formatDate(latestUpdatedAt)}` : "；最后更新 null";
    if (!activeVlmJobId) {
      const imageCount = currentImageFeatures.length;
      vlmStatusElement.textContent = imageCount
        ? `数据库已有 ${count} 张图像的 VLM 结果${latestText}。点击 Process 可覆盖当前 cell 的分析。`
        : `数据库已有 ${count} 张图像的 VLM 结果${latestText}。加载 Mapillary 图像后可处理当前 cell。`;
    }
    renderVlmResultLayer();
    if (currentImageFeature) showImage(currentImageFeature);
  } catch (error) {
    if (sequence !== vlmResultsSequence) return;
    vlmStatusElement.textContent = `VLM 结果读取失败：${error.message}`;
    vlmStatusElement.classList.add("error");
  }
}

async function loadAllVlmResults() {
  const sequence = ++allVlmResultsSequence;
  try {
    const result = await apiGet("/api/vlm-results?limit=50000");
    if (sequence !== allVlmResultsSequence) return;
    allVlmResultsByImageId = result.results || {};
    renderVlmResultLayer();
    updateGlobalVlmStatus(result.count || 0);
  } catch (error) {
    if (sequence !== allVlmResultsSequence) return;
    vlmStatusElement.textContent = `全局 VLM 结果读取失败：${error.message}`;
    vlmStatusElement.classList.add("error");
  }
}

function updateGlobalVlmStatus(totalProcessed) {
  if (activeVlmJobId) return;
  if (!currentGrid) {
    vlmStatusElement.textContent = `数据库已有 ${totalProcessed} 张已处理图片。选择 cell 后可继续处理。`;
    return;
  }
  if (currentImageFeatures.length === 0) return;
  const currentCount = Object.keys(vlmResultsByImageId).length;
  const latestUpdatedAt = latestVlmUpdatedAt(vlmResultsByImageId);
  const latestText = latestUpdatedAt ? `；当前 cell 最后更新 ${formatDate(latestUpdatedAt)}` : "";
  vlmStatusElement.textContent = `数据库已有 ${totalProcessed} 张已处理图片；当前 cell ${currentCount} 张${latestText}。`;
}

async function startCellVlmJob() {
  if (!currentGrid || currentImageFeatures.length === 0 || activeVlmJobId) return;
  const gridId = currentGrid.properties.grid_id;
  selectedModel = modelSelectElement.value || selectedModel;
  resetVlmProgress(0, currentImageFeatures.length);
  processCellVlmButton.disabled = true;
  vlmStatusElement.classList.remove("error");
  vlmStatusElement.textContent = `正在创建 VLM 任务：${currentImageFeatures.length} 张图像，模型 ${selectedModel || "默认模型"}。`;

  try {
    const job = await apiPost(`/api/grids/${encodeURIComponent(gridId)}/vlm-jobs`, {
      images: currentImageFeatures,
      model: selectedModel,
      force: forceVlmCheckbox.checked,
    });
    activeVlmJobId = job.job_id;
    updateVlmJobUi(job);
    pollVlmJob(activeVlmJobId, gridId);
  } catch (error) {
    activeVlmJobId = null;
    vlmStatusElement.textContent = `VLM 任务创建失败：${error.message}`;
    vlmStatusElement.classList.add("error");
    updateProcessButtonState();
  }
}

function pollVlmJob(jobId, gridId) {
  stopVlmJobPolling();
  vlmJobTimer = window.setInterval(async () => {
    try {
      const job = await apiGet(`/api/vlm/jobs/${encodeURIComponent(jobId)}`);
      if (!currentGrid || currentGrid.properties.grid_id !== gridId || activeVlmJobId !== jobId) {
        stopVlmJobPolling();
        return;
      }
      updateVlmJobUi(job);
      await loadVlmResults(gridId);
      await loadAllVlmResults();
      if (["completed", "failed"].includes(job.status)) {
        stopVlmJobPolling();
        activeVlmJobId = null;
        await loadVlmResults(gridId);
        await loadAllVlmResults();
        updateProcessButtonState();
      }
    } catch (error) {
      stopVlmJobPolling();
      activeVlmJobId = null;
      vlmStatusElement.textContent = `VLM 进度读取失败：${error.message}`;
      vlmStatusElement.classList.add("error");
      updateProcessButtonState();
    }
  }, 1000);
}

function updateVlmJobUi(job) {
  const total = Number(job.total || 0);
  const processed = Number(job.processed || 0);
  resetVlmProgress(processed, total);
  const current = job.current_image_id ? `；当前 ${job.current_image_id}` : "";
  const analyzed = job.analyzed ? `；新分析 ${job.analyzed}` : "";
  const skipped = job.skipped ? `；跳过已有 ${job.skipped}` : "";
  const failed = job.failed ? `；失败 ${job.failed}` : "";
  const completed = job.completed_at ? `；完成 ${formatDate(job.completed_at)}` : "";
  vlmStatusElement.classList.toggle("error", job.status === "failed");
  vlmStatusElement.textContent = `VLM ${job.status}：${processed}/${total}${analyzed}${skipped}${failed}${current}${completed}`;
}

function resetVlmPanel(message) {
  resetVlmProgress(0, 1);
  vlmStatusElement.classList.remove("error");
  vlmStatusElement.textContent = message;
  updateProcessButtonState();
}

function resetVlmProgress(value, max) {
  vlmProgressElement.max = Math.max(Number(max) || 1, 1);
  vlmProgressElement.value = Math.min(Number(value) || 0, vlmProgressElement.max);
}

function stopVlmJobPolling() {
  if (vlmJobTimer) {
    window.clearInterval(vlmJobTimer);
    vlmJobTimer = null;
  }
}

function updateProcessButtonState() {
  const canProcess = Boolean(currentGrid && ollamaReady && currentImageFeatures.length > 0 && !activeVlmJobId);
  processCellVlmButton.disabled = !canProcess;
}

function showImage(feature) {
  const properties = feature.properties;
  currentImageFeature = feature;
  const geometry = displayGeometry(feature);
  const coordinates = geometry.coordinates;
  const originalCoordinates = properties.original_geometry?.coordinates;
  const computedCoordinates = properties.computed_geometry?.coordinates;
  const capturedAt = properties.captured_at
    ? new Date(properties.captured_at).toLocaleString("zh-CN")
    : "未知";
  const angle = displayCompassAngle(properties);
  const thumbnail = properties.thumb_1024_url
    ? `<img src="${escapeAttribute(properties.thumb_1024_url)}" alt="Mapillary 图像 ${escapeHtml(String(feature.id))}">`
    : '<div class="placeholder"><strong>无缩略图</strong></div>';
  const analysis = vlmResultsByImageId[String(feature.id)] || null;

  imageDetailElement.className = "image-detail";
  imageDetailElement.innerHTML = `
    ${thumbnail}
    <h2>图像 ${escapeHtml(String(feature.id))}</h2>
    <dl class="metadata">
      <dt>拍摄时间</dt><dd>${escapeHtml(capturedAt)}</dd>
      <dt>当前坐标</dt><dd>${coordinates[1].toFixed(7)}, ${coordinates[0].toFixed(7)}（${mapillaryGeometryMode === "computed" ? "Mapillary 校正" : "原始 GPS"}）</dd>
      <dt>原始 GPS</dt><dd>${escapeHtml(formatCoordinates(originalCoordinates))}</dd>
      <dt>校正坐标</dt><dd>${escapeHtml(formatCoordinates(computedCoordinates))}</dd>
      <dt>当前方向</dt><dd>${angle == null ? "未知" : `${Number(angle).toFixed(1)}°`}</dd>
      <dt>原始方向</dt><dd>${properties.compass_angle == null ? "未知" : `${Number(properties.compass_angle).toFixed(1)}°`}</dd>
      <dt>校正方向</dt><dd>${properties.computed_compass_angle == null ? "未知" : `${Number(properties.computed_compass_angle).toFixed(1)}°`}</dd>
      <dt>相机类型</dt><dd>${escapeHtml(properties.camera_type || "未知")}</dd>
      <dt>图像尺寸</dt><dd>${properties.width || "?"} × ${properties.height || "?"}</dd>
      <dt>序列 ID</dt><dd>${escapeHtml(String(properties.sequence_id || "未知"))}</dd>
    </dl>
    <a class="open-mapillary" href="${escapeAttribute(properties.mapillary_url)}" target="_blank" rel="noreferrer">在 Mapillary 街景中打开</a>
    <button id="process-current-image-button" class="vlm-action" ${ollamaReady && !activeVlmJobId ? "" : "disabled"}>Process 当前图片</button>
    ${renderVlmResult(analysis)}
  `;
  document.getElementById("process-current-image-button")?.addEventListener("click", startCurrentImageVlmJob);
}

function showStoredVlmPoint(imageId) {
  const analysis = allVlmResultsByImageId[String(imageId)];
  if (!analysis) return;
  currentImageFeature = null;
  imageDetailElement.className = "image-detail";
  imageDetailElement.innerHTML = `
    <div class="placeholder">
      <strong>已处理图片点</strong>
      <span>当前未加载该图片的 Mapillary 缩略图。选择对应 cell 并确认访问 Mapillary 后可查看原图。</span>
    </div>
    <h2>图像 ${escapeHtml(String(imageId))}</h2>
    <dl class="metadata">
      <dt>grid_id</dt><dd>${escapeHtml(analysis.grid_id || "null")}</dd>
      <dt>坐标</dt><dd>${escapeHtml(formatCoordinates(analysis.geometry?.coordinates))}</dd>
      <dt>主题字段</dt><dd>${escapeHtml(selectedVlmTheme)}</dd>
      <dt>主题值</dt><dd>${escapeHtml(formatVlmValue(analysis, selectedVlmTheme))}</dd>
    </dl>
    ${renderVlmResult(analysis)}
  `;
}

async function startCurrentImageVlmJob() {
  if (!currentGrid || !currentImageFeature || activeVlmJobId) return;
  const gridId = currentGrid.properties.grid_id;
  selectedModel = modelSelectElement.value || selectedModel;
  resetVlmProgress(0, 1);
  vlmStatusElement.classList.remove("error");
  vlmStatusElement.textContent = `正在处理当前图片 ${currentImageFeature.id}，模型 ${selectedModel || "默认模型"}。`;
  updateProcessButtonState();
  const button = document.getElementById("process-current-image-button");
  if (button) button.disabled = true;

  try {
    const job = await apiPost(`/api/grids/${encodeURIComponent(gridId)}/vlm-jobs`, {
      images: [currentImageFeature],
      model: selectedModel,
      force: true,
    });
    activeVlmJobId = job.job_id;
    updateVlmJobUi(job);
    pollVlmJob(activeVlmJobId, gridId);
  } catch (error) {
    activeVlmJobId = null;
    vlmStatusElement.textContent = `当前图片 VLM 任务创建失败：${error.message}`;
    vlmStatusElement.classList.add("error");
    updateProcessButtonState();
    if (button) button.disabled = false;
  }
}

function renderVlmResult(analysis) {
  const rows = VLM_DISPLAY_FIELDS.map((field) => {
    const value = formatVlmValue(analysis, field);
    return `<dt>${escapeHtml(field)}</dt><dd${value === "null" ? ' class="null-value"' : ""}>${escapeHtml(value)}</dd>`;
  }).join("");
  return `
    <div id="vlm-result" class="vlm-result${analysis?.error ? " error" : ""}">
      <dl>
        <dt>model</dt><dd>${escapeHtml(analysis?.model || "null")}</dd>
        <dt>updated_at</dt><dd>${escapeHtml(analysis?.updated_at ? formatDate(analysis.updated_at) : "null")}</dd>
        ${rows}
        <dt>error</dt><dd>${escapeHtml(analysis?.error || "null")}</dd>
      </dl>
    </div>
  `;
}

function formatVlmValue(analysis, field) {
  if (!analysis || !analysis.fields || analysis.fields[field] == null) return "null";
  const value = analysis.fields[field];
  if (field === "confidence" && typeof value === "number") return value.toFixed(2);
  return String(value);
}

function latestVlmUpdatedAt(resultsByImageId) {
  const timestamps = Object.values(resultsByImageId)
    .map((result) => result.updated_at)
    .filter(Boolean)
    .sort();
  return timestamps.length ? timestamps[timestamps.length - 1] : null;
}

function renderImageLayer() {
  imageLayer.clearLayers();
  const features = currentImageFeatures.flatMap((feature) => {
    const geometry = displayGeometry(feature);
    if (!geometry) return [];
    return [{ ...feature, geometry }];
  });
  imageLayer.addData({ type: "FeatureCollection", features });
}

function displayGeometry(feature) {
  const properties = feature.properties || {};
  if (mapillaryGeometryMode === "computed") {
    return properties.computed_geometry || properties.original_geometry || feature.geometry;
  }
  return properties.original_geometry || properties.computed_geometry || feature.geometry;
}

function displayCompassAngle(properties) {
  if (mapillaryGeometryMode === "computed") {
    return properties.computed_compass_angle ?? properties.compass_angle;
  }
  return properties.compass_angle ?? properties.computed_compass_angle;
}

function formatCoordinates(coordinates) {
  return Array.isArray(coordinates) && coordinates.length >= 2
    ? `${Number(coordinates[1]).toFixed(7)}, ${Number(coordinates[0]).toFixed(7)}`
    : "null";
}

function renderVlmResultLayer() {
  vlmResultLayer.clearLayers();
  const imageFeatureById = new Map(currentImageFeatures.map((feature) => [String(feature.id), feature]));
  const features = Object.values(allVlmResultsByImageId).flatMap((analysis) => {
    const imageFeature = imageFeatureById.get(String(analysis.image_id));
    const geometry = imageFeature ? displayGeometry(imageFeature) : analysis.geometry;
    if (!geometry) return [];
    const themeValue = formatVlmValue(analysis, selectedVlmTheme);
    return [{
      type: "Feature",
      id: analysis.image_id,
      geometry,
      properties: {
        image_id: String(analysis.image_id),
        theme_field: selectedVlmTheme,
        theme_value: themeValue,
        updated_at: analysis.updated_at,
      },
    }];
  });
  vlmResultLayer.addData({ type: "FeatureCollection", features });
}

function vlmThemeColor(value) {
  const colors = {
    vehicle_road: "#e03131",
    pedestrian_road: "#9c36b5",
    bicycle_road: "#087f5b",
    other_location: "#495057",
    asphalt: "#343a40",
    concrete: "#868e96",
    paving_stones: "#f08c00",
    unpaved: "#7f4f24",
    yes: "#087f5b",
    no: "#adb5bd",
    uncertain: "#f2a900",
    null: "#dee2e6",
  };
  return colors[value] || "#1c7ed6";
}

function showAwaitingConfirmation() {
  currentImageFeature = null;
  imageDetailElement.className = "image-detail empty";
  imageDetailElement.innerHTML = '<div class="placeholder"><strong>等待确认</strong><span>当前操作尚未访问 Mapillary API。</span></div>';
}

function showEmptyImageState() {
  currentImageFeature = null;
  imageDetailElement.className = "image-detail empty";
  imageDetailElement.innerHTML = '<div class="placeholder"><strong>格网内没有图像</strong><span>可选择相邻格网继续检查。</span></div>';
}

function setError(message) {
  loadStatusElement.textContent = message;
  loadStatusElement.classList.add("error");
}

function formatDate(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "时间未知";
}

function formatMetric(value, suffix, digits) {
  return value == null ? "未公布/无记录" : `${Number(value).toFixed(digits)}${suffix}`;
}

function roadColor(category) {
  if (category === "pedestrian") return "#9c36b5";
  if (category === "bicycle") return "#087f5b";
  return "#e03131";
}

function roadWeight(category) {
  const zoomBoost = map.getZoom() >= 17 ? 1.4 : 1;
  if (category === "vehicle") return 4.6 * zoomBoost;
  if (category === "bicycle") return 4 * zoomBoost;
  return 3.5 * zoomBoost;
}

function landuseColor(className) {
  const colors = {
    allotments: "#f08c00",
    construction: "#f76707",
    forest: "#2b8a3e",
    grass: "#74b816",
    meadow: "#66a80f",
    residential: "#ffd43b",
    commercial: "#f08c00",
    industrial: "#ae3ec9",
    retail: "#e8590c",
    park: "#37b24d",
    parking: "#495057",
    shrubbery: "#82c91e",
    social_facility: "#fab005",
    water: "#1c7ed6",
  };
  return colors[className] || "#fab005";
}

function escapeHtml(value) {
  return value.replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function escapeAttribute(value) {
  return escapeHtml(String(value));
}

confirmMapillaryButton.addEventListener("click", loadImages);
processCellVlmButton.addEventListener("click", startCellVlmJob);
modelSelectElement.addEventListener("change", () => {
  selectedModel = modelSelectElement.value;
  updateProcessButtonState();
});
mapillaryGeometryModeSelect.addEventListener("change", () => {
  mapillaryGeometryMode = mapillaryGeometryModeSelect.value === "computed" ? "computed" : "original";
  renderImageLayer();
  renderVlmResultLayer();
  if (currentImageFeature) showImage(currentImageFeature);
});
vlmThemeSelect.addEventListener("change", () => {
  selectedVlmTheme = VLM_THEME_FIELDS.includes(vlmThemeSelect.value)
    ? vlmThemeSelect.value
    : "capture_position";
  renderVlmResultLayer();
});
map.on("zoomend", () => {
  [pedestrianRoadLayer, vehicleRoadLayer, bicycleRoadLayer].forEach((layer) => {
    layer.setStyle((feature) => ({
      color: roadColor(feature.properties.road_category),
      weight: roadWeight(feature.properties.road_category),
      opacity: 1,
    }));
  });
});
checkHealth();
initializeMainz();
loadAllVlmResults();
