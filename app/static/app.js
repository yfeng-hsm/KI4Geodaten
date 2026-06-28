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
map.createPane("roadMatchPane");
map.createPane("vlmResultPane");
map.createPane("mapillaryImagePane");
map.createPane("gridPane");
map.createPane("boundaryPane");
map.getPane("landusePane").style.zIndex = 410;
map.getPane("gridPane").style.zIndex = 420;
map.getPane("buildingPane").style.zIndex = 430;
map.getPane("bicycleRoadPane").style.zIndex = 450;
map.getPane("pedestrianRoadPane").style.zIndex = 455;
map.getPane("vehicleRoadPane").style.zIndex = 460;
map.getPane("boundaryPane").style.zIndex = 460;
map.getPane("vlmResultPane").style.zIndex = 500;
map.getPane("roadMatchPane").style.zIndex = 505;
map.getPane("mapillaryImagePane").style.zIndex = 510;
map.getPane("landusePane").style.pointerEvents = "none";
map.getPane("buildingPane").style.pointerEvents = "none";
map.getPane("pedestrianRoadPane").style.pointerEvents = "none";
map.getPane("vehicleRoadPane").style.pointerEvents = "none";
map.getPane("bicycleRoadPane").style.pointerEvents = "none";
map.getPane("gridPane").style.pointerEvents = "auto";
map.getPane("boundaryPane").style.pointerEvents = "none";
map.getPane("vlmResultPane").style.pointerEvents = "none";
map.getPane("roadMatchPane").style.pointerEvents = "auto";
map.getPane("mapillaryImagePane").style.pointerEvents = "none";

const gridRenderer = L.canvas({ pane: "gridPane", padding: 0.4 });
const landuseRenderer = L.canvas({ pane: "landusePane", padding: 0.4 });
const buildingRenderer = L.canvas({ pane: "buildingPane", padding: 0.4 });
const pedestrianRoadRenderer = L.canvas({ pane: "pedestrianRoadPane", padding: 0.4, tolerance: 8 });
const vehicleRoadRenderer = L.canvas({ pane: "vehicleRoadPane", padding: 0.4, tolerance: 8 });
const bicycleRoadRenderer = L.canvas({ pane: "bicycleRoadPane", padding: 0.4, tolerance: 8 });
const vlmResultRenderer = L.canvas({ pane: "vlmResultPane", padding: 0.4 });
const roadMatchRenderer = L.svg({ pane: "roadMatchPane", padding: 0.4 });
let selectedGridLayer = null;
let selectedGridNeighborhoodLayer = null;
let selectedRoadFeature = null;
let currentRoadFeatures = [];
let currentGrid = null;
let currentImageFeature = null;
let currentImageFeatures = [];
let currentMapMatchingLayers = null;
let currentMapMatchingSegmentSummaries = [];
let currentMapMatchingSelectedSegment = null;
let currentMapMatchingPointFeatures = [];
let currentConfirmedMapMatchingPointFeatures = [];
let allConfirmedMapMatchedPointFeatures = [];
let vlmResultsByImageId = {};
let allVlmResultsByImageId = {};
let currentCellProcessPlan = emptyProcessPlan();
let mapillaryGeometryMode = "original";
let requestSequence = 0;
let cellMapSequence = 0;
let mapMatchingSequence = 0;
let vlmResultsSequence = 0;
let allVlmResultsSequence = 0;
let vlmRenderFrame = null;
let activeVlmJobId = null;
let vlmJobTimer = null;
let ollamaReady = false;
let selectedModel = "";
let selectedVlmTheme = "capture_position";
let surfaceValidationRunning = false;
let mapMatchingRunning = false;

const VLM_DISPLAY_FIELDS = [
  "unusable_reason",
  "capture_position",
  "surface_material",
  "surface_material_candidates",
  "left_sidewalk",
  "left_sidewalk_surface_material",
  "left_sidewalk_surface_material_candidates",
  "right_sidewalk",
  "right_sidewalk_surface_material",
  "right_sidewalk_surface_material_candidates",
  "left_adjacent_road_type",
  "left_adjacent_road_surface_material",
  "left_adjacent_road_surface_material_candidates",
  "right_adjacent_road_type",
  "right_adjacent_road_surface_material",
  "right_adjacent_road_surface_material_candidates",
  "traffic_signal",
  "bench",
  "waste_basket",
  "independent_bicycle_road",
  "independent_pedestrian_road",
  "confidence",
  "reason",
];

const VLM_THEME_FIELDS = [
  "unusable_reason",
  "capture_position",
  "surface_material",
  "left_sidewalk",
  "left_sidewalk_surface_material",
  "right_sidewalk",
  "right_sidewalk_surface_material",
  "left_adjacent_road_type",
  "left_adjacent_road_surface_material",
  "right_adjacent_road_type",
  "right_adjacent_road_surface_material",
  "traffic_signal",
  "bench",
  "waste_basket",
  "independent_bicycle_road",
  "independent_pedestrian_road",
];

const CELL_PROCESS_DISTANCE_METERS = 5;
const VLM_RENDER_BOUNDS_PADDING = 0.18;
const VLM_RENDER_FEATURE_LIMIT = 1200;
const VLM_RENDER_VIRTUAL_MIN_ZOOM = 17;
const SELECTED_GRID_NEIGHBORHOOD_RADIUS = 1;
const OSM_LAYER_RADIUS = 0;

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
      const roadFeature = nearestRoadFeatureAtLatLng(event.latlng);
      if (roadFeature) {
        selectRoadForVlmMatching(roadFeature, event.latlng);
        return;
      }
      selectGrid(feature, layer);
    });
  },
}).addTo(map);

const selectedGridNeighborhoodLayerGroup = L.geoJSON(null, {
  pane: "gridPane",
  interactive: false,
  style: (feature) => ({
    color: feature.properties.selected ? "#f2a900" : "#f2a900",
    weight: feature.properties.selected ? 3 : 1.4,
    opacity: feature.properties.selected ? 1 : 0.78,
    fillColor: "#f2a900",
    fillOpacity: feature.properties.selected ? 0.16 : 0.055,
  }),
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
  pointToLayer: (feature, latlng) => thematicObservationLayer(feature, latlng, "vlmResultPane", vlmResultRenderer),
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
      `${feature.properties.image_id}<br>${feature.properties.observation_role}: ${feature.properties.theme_field}<br>${feature.properties.theme_value}`,
      { direction: "top", opacity: 0.9 }
    );
  },
}).addTo(map);

const landuseLayer = L.geoJSON(null, {
  renderer: landuseRenderer,
  interactive: false,
  style: (feature) => ({
    color: landuseColor(feature.properties.class_name),
    weight: 1,
    opacity: 0.38,
    fillColor: landuseColor(feature.properties.class_name),
    fillOpacity: 0.14,
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
    color: "#1971c2",
    weight: 1,
    opacity: 0.58,
    fillColor: "#1c7ed6",
    fillOpacity: 0.18,
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

const selectedRoadLayer = L.geoJSON(null, {
  pane: "roadMatchPane",
  interactive: true,
  renderer: roadMatchRenderer,
  style: { color: "#f2a900", weight: 7, opacity: 0.9 },
  onEachFeature: (feature, layer) => {
    layer.bindPopup(roadPopupHtml(feature.properties || {}), {
      maxWidth: 360,
      className: "road-popup",
    });
  },
});

const roadMatchLinkLayer = L.geoJSON(null, {
  pane: "roadMatchPane",
  interactive: false,
  renderer: roadMatchRenderer,
  style: {
    color: "#18201d",
    weight: 2.4,
    opacity: 0.85,
    dashArray: "6 6",
  },
});

const roadMatchPointLayer = L.geoJSON(null, {
  pane: "roadMatchPane",
  pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
    pane: "roadMatchPane",
    renderer: roadMatchRenderer,
    radius: feature.properties.virtual_observation ? 8 : 6,
    color: feature.properties.virtual_observation ? "#111827" : "#ffffff",
    weight: feature.properties.virtual_observation ? 2.5 : 2,
    dashArray: feature.properties.virtual_observation ? "3 3" : null,
    fillColor: feature.properties.virtual_observation
      ? vlmThemeColor(feature.properties.matched_road_surface_material)
      : roadColor(feature.properties.road_category),
    fillOpacity: 1,
    interactive: true,
  }),
  onEachFeature: (feature, layer) => {
    layer.on("click", (event) => {
      L.DomEvent.stopPropagation(event);
    });
    layer.bindPopup(roadObservationPopupHtml(feature.properties || {}), {
      maxWidth: 380,
      className: "road-popup",
    });
  },
});

const roadSurfaceValidationLayer = L.geoJSON(null, {
  pane: "roadMatchPane",
  renderer: roadMatchRenderer,
  interactive: true,
  style: (feature) => ({
    color: feature.properties.surface_validation === "match" ? "#2b8a3e" : "#c92a2a",
    weight: 13,
    opacity: 0.48,
    lineCap: "round",
    lineJoin: "round",
  }),
  onEachFeature: (feature, layer) => {
    layer.on("click", (event) => {
      L.DomEvent.stopPropagation(event);
    });
    layer.bindPopup(surfaceValidationPopupHtml(feature.properties || {}), {
      maxWidth: 380,
      className: "road-popup",
    });
  },
}).addTo(map);

const roadMatchLayerGroup = L.layerGroup([
  selectedRoadLayer,
  roadMatchLinkLayer,
]).addTo(map);
selectedRoadLayer.addTo(map);
roadMatchLinkLayer.addTo(map);

const mapMatchRoadLayer = L.geoJSON(null, {
  pane: "roadMatchPane",
  renderer: roadMatchRenderer,
  interactive: false,
  style: {
    color: "#1864ab",
    weight: 7,
    opacity: 0.32,
    lineCap: "round",
    lineJoin: "round",
  },
}).addTo(map);

const mapMatchRawTrajectoryLayer = L.geoJSON(null, {
  pane: "roadMatchPane",
  renderer: roadMatchRenderer,
  interactive: false,
  style: (feature) => ({
    color: mapMatchTrajectoryColor(feature, "#f08c00"),
    weight: 4.2,
    opacity: 0.9,
    dashArray: "8 7",
    lineCap: "round",
    lineJoin: "round",
  }),
}).addTo(map);

const mapMatchMatchedTrajectoryLayer = L.geoJSON(null, {
  pane: "roadMatchPane",
  renderer: roadMatchRenderer,
  interactive: false,
  style: (feature) => ({
    color: mapMatchTrajectoryColor(feature, "#1c7ed6"),
    weight: 6.5,
    opacity: 0.94,
    lineCap: "round",
    lineJoin: "round",
  }),
}).addTo(map);

const mapMatchLinkLayer = L.geoJSON(null, {
  pane: "roadMatchPane",
  renderer: roadMatchRenderer,
  interactive: false,
  style: {
    color: "#1864ab",
    weight: 1.6,
    opacity: 0.7,
    dashArray: "3 6",
  },
}).addTo(map);

const mapMatchPointLayer = L.geoJSON(null, {
  pane: "roadMatchPane",
  pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
    pane: "roadMatchPane",
    radius: 5,
    color: "#ffffff",
    weight: 1.6,
    fillColor: mapMatchColor(feature.properties.capture_position),
    fillOpacity: 0.95,
    interactive: true,
    renderer: roadMatchRenderer,
  }),
  onEachFeature: (feature, layer) => {
    const properties = feature.properties || {};
    layer.on("click", (event) => {
      L.DomEvent.stopPropagation(event);
      showMapMatchedImage(feature);
    });
    layer.bindTooltip(
      `${properties.image_id}<br>${properties.capture_position || "unknown"}<br>segment ${properties.segment_index ?? 0}`,
      { direction: "top", opacity: 0.9 }
    );
  },
}).addTo(map);

const confirmedMapMatchedPointLayer = L.geoJSON(null, {
  pane: "roadMatchPane",
  pointToLayer: (feature, latlng) => thematicObservationLayer(feature, latlng, "roadMatchPane", roadMatchRenderer),
  onEachFeature: (feature, layer) => {
    const properties = feature.properties || {};
    layer.on("click", (event) => {
      L.DomEvent.stopPropagation(event);
      const source = allConfirmedMapMatchedPointFeatures.find((item) => (
        String(item.properties?.image_id || item.id) === String(properties.image_id || feature.id)
      ));
      showMapMatchedImage(source || feature);
    });
    layer.bindTooltip(
      `confirmed ${properties.image_id || feature.id}<br>${properties.observation_role || "center"}: ${properties.theme_value || "not processed"}`,
      { direction: "top", opacity: 0.9 }
    );
  },
}).addTo(map);

const mapMatchLayerGroup = L.layerGroup([
  mapMatchRoadLayer,
  mapMatchRawTrajectoryLayer,
  mapMatchMatchedTrajectoryLayer,
  mapMatchLinkLayer,
  mapMatchPointLayer,
]).addTo(map);

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
    "Road-VLM match links": roadMatchLayerGroup,
    "Raw/corrected matched trajectories": mapMatchLayerGroup,
    "Confirmed map-matched points": confirmedMapMatchedPointLayer,
    "Road surface validation": roadSurfaceValidationLayer,
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
const graphhopperHealthElement = document.getElementById("graphhopper-health");
const modelHealthElement = document.getElementById("model-health");
const modelSelectElement = document.getElementById("model-select");
const detailScrollElement = document.querySelector(".detail-scroll");
const themePanelElement = document.querySelector(".theme-panel");
const imageDetailElement = document.getElementById("image-detail");
const processCellVlmButton = document.getElementById("process-cell-vlm-button");
const processCurrentImageButton = document.getElementById("process-current-image-button");
const deleteCellVlmButton = document.getElementById("delete-cell-vlm-button");
const validateRoadSurfaceButton = document.getElementById("validate-road-surface-button");
const surfaceValidationProgress = document.getElementById("surface-validation-progress");
const surfaceValidationStatus = document.getElementById("surface-validation-status");
const mapMatchingSequenceSelect = document.getElementById("map-matching-sequence-select");
const runGraphhopperDirectButton = document.getElementById("run-graphhopper-direct-button");
const runGraphhopperMatchingButton = document.getElementById("run-graphhopper-matching-button");
const mapMatchingSegmentSelect = document.getElementById("map-matching-segment-select");
const confirmCurrentMapMatchingButton = document.getElementById("confirm-current-mapmatching-button");
const processCurrentTrajectoryVlmButton = document.getElementById("process-current-trajectory-vlm-button");
const mapMatchingStatusElement = document.getElementById("map-matching-status");
const vlmProgressElement = document.getElementById("vlm-progress");
const vlmStatusElement = document.getElementById("vlm-status");
const forceVlmCheckbox = document.getElementById("force-vlm-checkbox");
const vlmThemeSelect = document.getElementById("vlm-theme-select");
const refreshJobsButton = document.getElementById("refresh-vlm-jobs-button");
const vlmJobsElement = document.getElementById("vlm-jobs");
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

async function apiDelete(url) {
  const response = await fetch(url, { method: "DELETE" });
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
    updateGraphHopperHealth(health.graphhopper || {});
    updateModelPicker(ollama);
    updateProcessButtonState();
  } catch (_error) {
    mapillaryHealthElement.textContent = "Mapillary 状态未知";
    mapillaryHealthElement.classList.add("warning");
    graphhopperHealthElement.textContent = "GraphHopper 状态未知";
    graphhopperHealthElement.classList.add("warning");
    modelHealthElement.textContent = "模型状态未知";
    modelHealthElement.parentElement.classList.add("warning");
    modelSelectElement.disabled = true;
  }
}

function updateGraphHopperHealth(graphhopper) {
  const configured = Boolean(graphhopper.configured);
  const ok = Boolean(graphhopper.ok);
  if (!configured) {
    graphhopperHealthElement.textContent = "GraphHopper 未配置";
  } else if (ok) {
    graphhopperHealthElement.textContent = "GraphHopper 已连接";
  } else {
    graphhopperHealthElement.textContent = "GraphHopper 连接失败";
  }
  graphhopperHealthElement.classList.toggle("warning", !ok);
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
  currentConfirmedMapMatchingPointFeatures = [];
  resetMapMatchingControls();
  vlmResultsByImageId = {};
  currentCellProcessPlan = emptyProcessPlan();
  activeVlmJobId = null;
  stopVlmJobPolling();
  clearRoadLayers();
  clearMapMatching();
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
  mapDataStatusElement.textContent = "正在从 PostGIS 读取当前 cell 的 OSM 图层；3x3 仅作为格网高亮…";
  showAwaitingConfirmation();
  loadSelectedGridNeighborhood(properties.grid_id);
  loadCellMapLayers(properties.grid_id);
  loadVlmResults(properties.grid_id);
}

async function loadCellMapLayers(gridId) {
  const sequence = ++cellMapSequence;
  try {
    const result = await apiGet(`/api/mainz/grids/${encodeURIComponent(gridId)}/map-layers?radius=${OSM_LAYER_RADIUS}`);
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
    currentRoadFeatures = [
      ...roads.pedestrian,
      ...roads.vehicle,
      ...roads.bicycle,
    ];
    pedestrianRoadLayer.addData(featureCollection(roads.pedestrian));
    vehicleRoadLayer.addData(featureCollection(roads.vehicle));
    bicycleRoadLayer.addData(featureCollection(roads.bicycle));
    mapDataStatusElement.textContent = `OSM 当前cell图层：${result.meta.counts.roads} 条路（行人 ${roads.pedestrian.length}，车行 ${roads.vehicle.length}，自行车 ${roads.bicycle.length}），${result.meta.counts.buildings} 个建筑，${result.meta.counts.landuse} 个土地利用面。3x3 只用于格网高亮。`;
  } catch (error) {
    if (sequence !== cellMapSequence) return;
    mapDataStatusElement.textContent = `OSM 图层加载失败：${error.message}`;
    mapDataStatusElement.classList.add("error");
  }
}

async function loadSelectedGridNeighborhood(gridId) {
  selectedGridNeighborhoodLayerGroup.clearLayers();
  selectedGridNeighborhoodLayer = null;
  try {
    const result = await apiGet(`/api/grids/${encodeURIComponent(gridId)}/around?radius=${SELECTED_GRID_NEIGHBORHOOD_RADIUS}`);
    selectedGridNeighborhoodLayerGroup.addData(result);
    selectedGridNeighborhoodLayer = selectedGridNeighborhoodLayerGroup;
    zoomToCurrentGrid();
  } catch (error) {
    zoomToCurrentGrid();
    loadStatusElement.textContent = `3x3 格网范围加载失败：${error.message}`;
    loadStatusElement.classList.add("error");
  }
}

function createRoadLayer(renderer) {
  return L.geoJSON(null, {
    renderer,
    interactive: false,
    style: (feature) => ({
      color: roadColor(feature.properties.road_category),
      weight: roadWeight(feature.properties.road_category),
      opacity: 0.62,
    }),
  }).addTo(map);
}

function clearRoadLayers() {
  currentRoadFeatures = [];
  pedestrianRoadLayer.clearLayers();
  vehicleRoadLayer.clearLayers();
  bicycleRoadLayer.clearLayers();
  clearRoadVlmMatches();
}

function clearMapMatching() {
  currentMapMatchingLayers = null;
  currentMapMatchingSegmentSummaries = [];
  currentMapMatchingSelectedSegment = null;
  currentMapMatchingPointFeatures = [];
  mapMatchRoadLayer.clearLayers();
  mapMatchRawTrajectoryLayer.clearLayers();
  mapMatchMatchedTrajectoryLayer.clearLayers();
  mapMatchLinkLayer.clearLayers();
  mapMatchPointLayer.clearLayers();
  resetMapMatchingSegmentSelect();
  updateMapMatchingButtonState();
}

function clearRoadVlmMatches() {
  selectedRoadFeature = null;
  selectedRoadLayer.clearLayers();
  roadMatchLinkLayer.clearLayers();
  roadMatchPointLayer.clearLayers();
}

async function selectRoadForVlmMatching(feature, clickLatLng = null) {
  const properties = feature.properties || {};
  selectedRoadFeature = feature;
  selectedRoadLayer.clearLayers();
  roadMatchLinkLayer.clearLayers();
  roadMatchPointLayer.clearLayers();
  selectedRoadLayer.addData(feature);
  selectedRoadLayer.bringToFront();
  mapDataStatusElement.classList.remove("error");
  mapDataStatusElement.textContent = `正在匹配道路 ${properties.osm_id} 与 VLM 图像点…`;
  try {
    const result = await apiGet(`/api/osm/roads/${encodeURIComponent(properties.osm_id)}/vlm-matches?max_distance_m=8&close_override_m=4&view_fov_deg=110&no_heading_visible_m=3&road_axis_tolerance_deg=35&limit=300`);
    if (!selectedRoadFeature || selectedRoadFeature.properties.osm_id !== properties.osm_id) return;
    selectedRoadLayer.clearLayers();
    selectedRoadLayer.addData(result.road);
    roadMatchLinkLayer.clearLayers();
    roadMatchPointLayer.clearLayers();
    roadMatchLinkLayer.addData(result.matches);
    selectedRoadLayer.bringToFront();
    roadMatchLinkLayer.bringToFront();
    if (clickLatLng) {
      selectedRoadLayer.eachLayer((layer) => layer.openPopup(clickLatLng));
    }
    const label = roadCategoryLabel(result.meta.road_category);
    mapDataStatusElement.textContent = `选中道路（${label}）匹配到 ${result.meta.count} 个 VLM 观测；地图只叠加虚线连接，观测符号使用常驻 VLM 图层。`;
  } catch (error) {
    mapDataStatusElement.textContent = `道路匹配失败：${error.message}`;
    mapDataStatusElement.classList.add("error");
  }
}

function resetMapMatchingControls() {
  clearMapMatching();
  mapMatchingRunning = false;
  if (mapMatchingSequenceSelect) {
    mapMatchingSequenceSelect.innerHTML = '<option value="">先加载 Mapillary 图像</option>';
    mapMatchingSequenceSelect.disabled = true;
  }
  if (mapMatchingStatusElement) {
    mapMatchingStatusElement.classList.remove("error");
    mapMatchingStatusElement.textContent = "加载当前 cell 的 Mapillary 图像后，可以手动组织 sequence 并运行 GraphHopper。";
  }
  updateMapMatchingButtonState();
}

function updateMapMatchingButtonState() {
  const hasSequence = currentImageFeatures.some((feature) => String(feature.properties?.sequence_id || "").trim());
  const runDisabled = mapMatchingRunning || !currentGrid || !hasSequence;
  if (runGraphhopperDirectButton) runGraphhopperDirectButton.disabled = runDisabled;
  if (runGraphhopperMatchingButton) runGraphhopperMatchingButton.disabled = runDisabled;
  const hasMapmatchedPreview = currentMapMatchingPointFeatures.some((feature) => (
    feature.properties?.mapmatched_geometry?.coordinates
  ));
  if (confirmCurrentMapMatchingButton) {
    confirmCurrentMapMatchingButton.disabled = Boolean(
      mapMatchingRunning
      || !currentGrid
      || !hasMapmatchedPreview
    );
  }
  if (processCurrentTrajectoryVlmButton) {
    const hasTrajectoryImages = currentMapMatchingPointFeatures.length > 0
      || currentConfirmedMapMatchingPointFeatures.length > 0;
    processCurrentTrajectoryVlmButton.disabled = Boolean(
      mapMatchingRunning
      || !currentGrid
      || !ollamaReady
      || activeVlmJobId
      || !hasTrajectoryImages
    );
  }
}

function resetMapMatchingSegmentSelect() {
  if (!mapMatchingSegmentSelect) return;
  mapMatchingSegmentSelect.innerHTML = '<option value="">先运行轨迹匹配</option>';
  mapMatchingSegmentSelect.disabled = true;
}

function updateMapMatchingSegmentOptions() {
  if (!mapMatchingSegmentSelect) return;
  const selectedBefore = mapMatchingSegmentSelect.value;
  const summaries = currentMapMatchingSegmentSummaries.length
    ? currentMapMatchingSegmentSummaries
    : summarizeMapMatchingSegments(currentMapMatchingLayers);
  currentMapMatchingSegmentSummaries = summaries;
  mapMatchingSegmentSelect.innerHTML = "";
  if (!summaries.length) {
    resetMapMatchingSegmentSelect();
    return;
  }
  summaries.forEach((summary, index) => {
    const segmentIndex = Number(summary.segment_index ?? index);
    const option = document.createElement("option");
    option.value = String(segmentIndex);
    const distance = summary.distance == null ? "" : ` · ${(Number(summary.distance) / 1000).toFixed(2)} km`;
    const profile = summary.profile ? ` · ${summary.profile}` : "";
    const userType = summary.user_type ? ` · ${summary.user_type}` : "";
    const candidateProfiles = Array.isArray(summary.candidate_profiles) && summary.candidate_profiles.length
      ? ` · candidates ${summary.candidate_profiles.join("/")}`
      : "";
    const typeStatus = summary.user_type_status || {};
    const typeText = typeStatus.total
      ? ` · type ${Number(typeStatus.winner_votes || 0)}/${Number(typeStatus.total || 0)} (${((Number(typeStatus.coverage_ratio || 0)) * 100).toFixed(0)}%)`
      : "";
    const status = summary.available === false ? " · 未匹配" : "";
    const confirmed = confirmedSegmentStatus(segmentIndex);
    const confirmedText = confirmed.total > 0
      ? ` · 已确认 ${confirmed.confirmed}/${confirmed.total}`
      : " · 未确认";
    option.textContent = `segment ${segmentIndex} · ${summary.matched ?? 0}/${summary.count ?? 0} 点${userType}${typeText}${profile}${candidateProfiles}${distance}${status}${confirmedText}`;
    mapMatchingSegmentSelect.append(option);
  });
  mapMatchingSegmentSelect.disabled = false;
  if (selectedBefore && Array.from(mapMatchingSegmentSelect.options).some((option) => option.value === selectedBefore)) {
    mapMatchingSegmentSelect.value = selectedBefore;
  }
  currentMapMatchingSelectedSegment = Number(mapMatchingSegmentSelect.value || summaries[0].segment_index || 0);
}

function confirmedSegmentStatus(segmentIndex) {
  const expected = new Set(
    (currentMapMatchingLayers?.points?.features || [])
      .filter((feature) => Number(feature.properties?.segment_index ?? 0) === Number(segmentIndex))
      .map((feature) => String(feature.properties?.image_id || feature.id || ""))
      .filter(Boolean)
  );
  if (!expected.size) return { confirmed: 0, total: 0 };
  const confirmed = new Set(
    allConfirmedMapMatchedPointFeatures
      .filter((feature) => {
        const properties = feature.properties || {};
        const confirmedSegment = Number(properties.segment_index ?? properties.mapmatched_segment_index ?? 0);
        return confirmedSegment === Number(segmentIndex);
      })
      .map((feature) => String(feature.properties?.image_id || feature.id || ""))
      .filter((imageId) => expected.has(imageId))
  );
  return { confirmed: confirmed.size, total: expected.size };
}

function summarizeMapMatchingSegments(layers) {
  const points = layers?.points?.features || [];
  const groups = new Map();
  points.forEach((feature) => {
    const properties = feature.properties || {};
    const segmentIndex = Number(properties.segment_index ?? 0);
    const summary = groups.get(segmentIndex) || {
      segment_index: segmentIndex,
      count: 0,
      matched: 0,
      available: false,
      profile: properties.profile || null,
      user_type: properties.user_type || null,
      user_type_votes: properties.user_type_votes || null,
      user_type_status: properties.user_type_status || null,
      candidate_profiles: properties.candidate_profiles || null,
      distance: null,
    };
    summary.count += 1;
    if (properties.mapmatched_geometry?.coordinates) {
      summary.matched += 1;
      summary.available = true;
    }
    if (!summary.profile && properties.profile) summary.profile = properties.profile;
    if (!summary.user_type && properties.user_type) summary.user_type = properties.user_type;
    if (!summary.user_type_votes && properties.user_type_votes) summary.user_type_votes = properties.user_type_votes;
    if (!summary.user_type_status && properties.user_type_status) summary.user_type_status = properties.user_type_status;
    if (!summary.candidate_profiles && properties.candidate_profiles) summary.candidate_profiles = properties.candidate_profiles;
    groups.set(segmentIndex, summary);
  });
  return Array.from(groups.values()).sort((a, b) => Number(a.segment_index) - Number(b.segment_index));
}

function renderSelectedMapMatchingSegment() {
  const segmentIndex = Number(currentMapMatchingSelectedSegment ?? 0);
  const layers = currentMapMatchingLayers || {};
  mapMatchRoadLayer.clearLayers();
  mapMatchRawTrajectoryLayer.clearLayers();
  mapMatchMatchedTrajectoryLayer.clearLayers();
  mapMatchLinkLayer.clearLayers();
  mapMatchPointLayer.clearLayers();
  mapMatchRoadLayer.addData(layers.matched_roads || featureCollection([]));
  mapMatchRawTrajectoryLayer.addData(filterFeaturesBySegment(layers.raw_trajectory, segmentIndex));
  mapMatchMatchedTrajectoryLayer.addData(filterFeaturesBySegment(layers.matched_trajectory, segmentIndex));
  mapMatchLinkLayer.addData(filterFeaturesBySegment(layers.links, segmentIndex));
  const selectedPoints = filterFeaturesBySegment(layers.points, segmentIndex);
  mapMatchPointLayer.addData(selectedPoints);
  currentMapMatchingPointFeatures = selectedPoints.features || [];
  mapMatchRawTrajectoryLayer.bringToFront();
  mapMatchMatchedTrajectoryLayer.bringToFront();
  mapMatchLinkLayer.bringToFront();
  mapMatchPointLayer.bringToFront();
  updateMapMatchingButtonState();
}

function filterFeaturesBySegment(collection, segmentIndex) {
  const features = (collection?.features || []).filter((feature) => (
    Number(feature.properties?.segment_index ?? 0) === Number(segmentIndex)
  ));
  return featureCollection(features);
}

function updateMapMatchingSequenceOptions() {
  if (!mapMatchingSequenceSelect) return;
  const groups = new Map();
  currentImageFeatures.forEach((feature) => {
    const sequenceId = String(feature.properties?.sequence_id || "").trim();
    if (!sequenceId) return;
    const group = groups.get(sequenceId) || [];
    group.push(feature);
    groups.set(sequenceId, group);
  });

  mapMatchingSequenceSelect.innerHTML = "";
  if (!groups.size) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "当前 cell 没有 sequence_id";
    mapMatchingSequenceSelect.append(option);
    mapMatchingSequenceSelect.disabled = true;
    if (mapMatchingStatusElement) {
      mapMatchingStatusElement.textContent = "当前 Mapillary 缓存没有真实 sequence_id；需要重新确认访问 Mapillary 刷新元数据。";
    }
    updateMapMatchingButtonState();
    return;
  }

  const sorted = Array.from(groups.entries()).sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = `自动选择最长 sequence（${sorted[0][1].length} 张）`;
  mapMatchingSequenceSelect.append(auto);
  sorted.forEach(([sequenceId, features]) => {
    const option = document.createElement("option");
    option.value = sequenceId;
    option.textContent = `${sequenceId} · 当前cell ${features.length} 张`;
    mapMatchingSequenceSelect.append(option);
  });
  mapMatchingSequenceSelect.disabled = false;
  if (mapMatchingStatusElement) {
    mapMatchingStatusElement.classList.remove("error");
    mapMatchingStatusElement.textContent = `当前 cell 有 ${sorted.length} 条 Mapillary sequence；点击按钮后才运行 GraphHopper。`;
  }
  updateMapMatchingButtonState();
}

async function runMapMatchingForCurrentGrid(useVisualUserType = true) {
  if (!currentGrid || mapMatchingRunning) return;
  const gridId = currentGrid.properties.grid_id;
  const sequence = ++mapMatchingSequence;
  mapMatchingRunning = true;
  updateMapMatchingButtonState();
  clearMapMatching();
  if (!map.hasLayer(mapMatchLayerGroup)) map.addLayer(mapMatchLayerGroup);
  const selectedSequence = mapMatchingSequenceSelect && mapMatchingSequenceSelect.value
    ? mapMatchingSequenceSelect.value
    : "";
  const sequenceParam = selectedSequence ? `&sequence_id=${encodeURIComponent(selectedSequence)}` : "";
  const visualParam = `&use_visual_user_type=${useVisualUserType ? "true" : "false"}`;
  if (mapMatchingStatusElement) {
    mapMatchingStatusElement.classList.remove("error");
    mapMatchingStatusElement.textContent = useVisualUserType
      ? "正在按视觉信息判断交通使用者，并进行 GraphHopper map matching…"
      : "正在直接进行几何 map matching；本次不考虑视觉识别信息…";
  }
  try {
    const result = await apiGet(`/api/grids/${encodeURIComponent(gridId)}/map-matching?limit=1000${sequenceParam}${visualParam}`);
    if (sequence !== mapMatchingSequence) return;
    clearMapMatching();
    currentConfirmedMapMatchingPointFeatures = [];
    const layers = result.layers || {};
    currentMapMatchingLayers = layers;
    currentMapMatchingSegmentSummaries = result.meta?.segment_summaries || [];
    updateMapMatchingSegmentOptions();
    renderSelectedMapMatchingSegment();
    const rawCount = (layers.raw_trajectory?.features || []).length;
    const matchedCount = (layers.matched_trajectory?.features || []).length;
    if (!result.available) {
      const hasProcessableRawTrajectory = useVisualUserType && rawCount > 0 && currentMapMatchingPointFeatures.length > 0;
      const firstSummary = currentMapMatchingSegmentSummaries[0] || {};
      const typeStatus = firstSummary.user_type_status || {};
      const typeText = typeStatus.total
        ? `当前类型票 ${Number(typeStatus.winner_votes || 0)}/${Number(typeStatus.total || 0)}，覆盖率 ${((Number(typeStatus.coverage_ratio || 0)) * 100).toFixed(0)}%，需要至少 ${((Number(typeStatus.min_coverage_ratio || 0.4)) * 100).toFixed(0)}%。`
        : "";
      const message = hasProcessableRawTrajectory
        ? `已显示橙色原始子轨迹，还没有蓝色 matched 轨迹。${typeText}请先选择子轨迹并点击“Process 当前子轨迹图片”，处理完成后再次运行匹配。`
        : `Map matching：没有蓝色 matched 轨迹：${result.reason || "GraphHopper 不可用"}。`;
      mapDataStatusElement.textContent = message;
      if (mapMatchingStatusElement) {
        mapMatchingStatusElement.textContent = message;
        mapMatchingStatusElement.classList.toggle("error", !hasProcessableRawTrajectory);
      }
      zoomToMapMatchingResult();
      updateMapMatchingButtonState();
      return;
    }
    zoomToMapMatchingResult();
    const distanceText = result.meta.distance == null ? "" : `，距离 ${(Number(result.meta.distance) / 1000).toFixed(2)} km`;
    const segmentText = result.meta.segments == null
      ? ""
      : `，断轨后 ${result.meta.matched_segments || 0}/${result.meta.segments} 段成功`;
    const cacheText = result.meta.sequence_cached_grid_count
      ? `，跨 ${result.meta.sequence_cached_grid_count} 个缓存 cell`
      : "";
    const selectedSegmentText = currentMapMatchingSegmentSummaries.length > 1
      ? ` 当前显示 segment ${currentMapMatchingSelectedSegment}，可在列表中切换后分别确认/处理。`
      : "";
    const modeText = useVisualUserType ? "按视觉信息" : "直接几何";
    const message = `GraphHopper ${modeText} map matching 完成：sequence ${result.meta.sequence_id || "unknown"}，${result.meta.matched}/${result.meta.count} 个点${cacheText}，蓝色 matched 轨迹 ${matchedCount}${segmentText}${distanceText}。${selectedSegmentText}请检查预览点，确认后再保存位置。`;
    mapDataStatusElement.textContent = message;
    if (mapMatchingStatusElement) {
      mapMatchingStatusElement.classList.remove("error");
      mapMatchingStatusElement.textContent = message;
    }
    updateMapMatchingButtonState();
  } catch (error) {
    if (sequence !== mapMatchingSequence) return;
    clearMapMatching();
    if (mapMatchingStatusElement) {
      mapMatchingStatusElement.textContent = `GraphHopper map matching 失败：${error.message}`;
      mapMatchingStatusElement.classList.add("error");
    }
  } finally {
    if (sequence === mapMatchingSequence) {
      mapMatchingRunning = false;
      updateMapMatchingButtonState();
    }
  }
}

function zoomToMapMatchingResult() {
  const layers = [
    mapMatchRawTrajectoryLayer,
    mapMatchMatchedTrajectoryLayer,
    mapMatchLinkLayer,
    mapMatchPointLayer,
  ];
  const nonEmptyLayers = layers.filter((layer) => layer.getLayers().length > 0);
  if (!nonEmptyLayers.length) return;
  const bounds = L.featureGroup(nonEmptyLayers).getBounds();
  if (!bounds.isValid()) return;
  map.fitBounds(bounds, {
    padding: [90, 90],
    maxZoom: 18,
  });
}

function updateSurfaceValidationButtonState() {
  if (!validateRoadSurfaceButton) return;
  validateRoadSurfaceButton.disabled = surfaceValidationRunning;
}

async function runRoadSurfaceValidation() {
  if (surfaceValidationRunning) return;
  surfaceValidationRunning = true;
  updateSurfaceValidationButtonState();
  roadSurfaceValidationLayer.clearLayers();
  surfaceValidationProgress.hidden = false;
  surfaceValidationProgress.removeAttribute("value");
  surfaceValidationStatus.classList.remove("error");
  surfaceValidationStatus.textContent = "正在计算 Mainz 全域 OSM surface 与 VLM surface_material 一致性…";
  mapDataStatusElement.classList.remove("error");
  mapDataStatusElement.textContent = "正在计算 Mainz 全域 surface 一致性…";
  try {
    const result = await apiGet("/api/mainz/road-surface-validation");
    if (!result.available) {
      const message = `surface 一致性计算不可用：${result.reason}`;
      mapDataStatusElement.textContent = message;
      surfaceValidationStatus.textContent = message;
      mapDataStatusElement.classList.add("error");
      surfaceValidationStatus.classList.add("error");
      return;
    }
    roadSurfaceValidationLayer.addData(result.layers.roads);
    roadSurfaceValidationLayer.bringToFront();
    const skipped = result.meta.skipped || {};
    const message = `全域 surface 一致性：${result.meta.count} 条可评估道路，绿色一致 ${result.meta.match}，红色不一致 ${result.meta.mismatch}。跳过：无 OSM surface ${skipped.no_osm_surface || 0}，OSM surface 未归类 ${skipped.unmapped_osm_surface || 0}，无 VLM 匹配 ${skipped.no_matches || 0}，无可用 VLM surface ${skipped.no_vlm_surface || 0}，少于 3 个 VLM surface 观测 ${skipped.too_few_vlm_observations || 0}。`;
    mapDataStatusElement.textContent = message;
    surfaceValidationStatus.textContent = message;
  } catch (error) {
    const message = `surface 一致性计算失败：${error.message}`;
    mapDataStatusElement.textContent = message;
    surfaceValidationStatus.textContent = message;
    mapDataStatusElement.classList.add("error");
    surfaceValidationStatus.classList.add("error");
  } finally {
    surfaceValidationRunning = false;
    surfaceValidationProgress.hidden = true;
    surfaceValidationProgress.value = 0;
    updateSurfaceValidationButtonState();
  }
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

function nearestRoadFeatureAtLatLng(latlng) {
  if (!currentRoadFeatures.length) return null;
  const clickPoint = map.latLngToLayerPoint(latlng);
  let nearest = null;
  let nearestDistance = Infinity;
  currentRoadFeatures.forEach((feature) => {
    const distance = featurePixelDistance(feature, clickPoint);
    if (distance < nearestDistance) {
      nearest = feature;
      nearestDistance = distance;
    }
  });
  return nearestDistance <= 12 ? nearest : null;
}

function featurePixelDistance(feature, clickPoint) {
  const geometry = feature.geometry || {};
  const lines = geometry.type === "LineString"
    ? [geometry.coordinates]
    : geometry.type === "MultiLineString"
      ? geometry.coordinates
      : [];
  let minDistance = Infinity;
  lines.forEach((line) => {
    for (let index = 1; index < line.length; index += 1) {
      const start = coordinateToLayerPoint(line[index - 1]);
      const end = coordinateToLayerPoint(line[index]);
      minDistance = Math.min(minDistance, pointToSegmentDistance(clickPoint, start, end));
    }
  });
  return minDistance;
}

function coordinateToLayerPoint(coordinate) {
  return map.latLngToLayerPoint(L.latLng(Number(coordinate[1]), Number(coordinate[0])));
}

function pointToSegmentDistance(point, start, end) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  if (dx === 0 && dy === 0) return point.distanceTo(start);
  const t = Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / (dx * dx + dy * dy)));
  return point.distanceTo(L.point(start.x + t * dx, start.y + t * dy));
}

function featureCollection(features) {
  return { type: "FeatureCollection", features };
}

function roadCategory(properties) {
  const highway = properties.highway;
  const tags = properties.tags || {};
  if (["platform", "corridor", "elevator"].includes(highway)) {
    return "pedestrian";
  }
  if (["yes", "designated"].includes(tags.psv) || ["yes", "designated"].includes(tags.bus) || tags.busway) {
    return "vehicle";
  }
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

function roadPopupHtml(properties) {
  const tags = properties.tags || {};
  const surface = properties.surface || tags.surface || "unknown";
  const stats = properties.match_stats || {};
  return `
    <div class="road-popup-content">
      <strong>${escapeHtml(properties.name || properties.highway || "OSM road")}</strong>
      <dl>
        <dt>类型</dt><dd>${escapeHtml(roadCategoryLabel(properties.road_category))}</dd>
        <dt>highway</dt><dd>${escapeHtml(properties.highway || "unknown")}</dd>
        <dt>surface</dt><dd>${escapeHtml(surface)}</dd>
        <dt>maxspeed</dt><dd>${escapeHtml(properties.maxspeed || tags.maxspeed || "unknown")}</dd>
        <dt>oneway</dt><dd>${escapeHtml(properties.oneway || tags.oneway || "unknown")}</dd>
        <dt>access</dt><dd>${escapeHtml(tags.access || "unknown")}</dd>
      </dl>
      <div class="road-popup-section">
        <strong>匹配统计</strong>
        ${formatRoadStats(stats)}
      </div>
    </div>
  `;
}

function roadObservationPopupHtml(properties) {
  const isVirtual = Boolean(properties.virtual_observation);
  return `
    <div class="road-popup-content">
      <strong>${isVirtual ? "虚拟观测点" : "中心观测点"}</strong>
      <dl>
        <dt>投票道路</dt><dd>${escapeHtml(roadCategoryLabel(properties.road_category))}</dd>
        <dt>投票表面</dt><dd>${escapeHtml(properties.matched_road_surface_material || "null")}</dd>
        <dt>候选权重</dt><dd>${escapeHtml(formatCounts(properties.matched_road_surface_votes || {}))}</dd>
        <dt>观测来源</dt><dd>${escapeHtml(properties.matched_road_surface_source || "null")}</dd>
        <dt>原始拍摄位置</dt><dd>${escapeHtml(properties.capture_position || "null")}</dd>
        <dt>原始脚下表面</dt><dd>${escapeHtml(properties.surface_material || "null")}</dd>
        <dt>虚拟侧向</dt><dd>${escapeHtml(properties.virtual_observation_side || "center")}</dd>
        <dt>投票权重</dt><dd>${Number(properties.observation_vote || 1)}</dd>
        <dt>匹配方法</dt><dd>${escapeHtml(properties.match_method || "unknown")}</dd>
      </dl>
      <button type="button" data-open-vlm-image="${escapeAttribute(properties.image_id)}">查看原图分析</button>
    </div>
  `;
}

function formatRoadStats(stats) {
  const sections = [
    ["拍摄位置", stats.capture_position],
    ["投票表面", stats.matched_road_surface_material],
    ["投票来源", stats.matched_road_surface_source],
    ["图像道路类别", stats.image_road_category],
    ["左侧相邻道路", stats.left_adjacent_road_type],
    ["左侧相邻道路表面", stats.left_adjacent_road_surface_material],
    ["右侧相邻道路", stats.right_adjacent_road_type],
    ["右侧相邻道路表面", stats.right_adjacent_road_surface_material],
    ["traffic_signal", stats.traffic_signal],
    ["bench", stats.bench],
    ["waste_basket", stats.waste_basket],
    ["独立自行车路", stats.independent_bicycle_road],
    ["独立人行道路", stats.independent_pedestrian_road],
  ];
  const rows = sections
    .filter(([, counts]) => counts && Object.keys(counts).length)
    .map(([label, counts]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(formatCounts(counts))}</dd>`);
  if (!rows.length) return `<p class="muted">当前没有匹配的 VLM 点。</p>`;
  return `<dl>${rows.join("")}</dl>`;
}

function formatCounts(counts) {
  return Object.entries(counts)
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .map(([key, value]) => `${key}: ${formatCountValue(value)}`)
    .join(" · ");
}

function formatCountValue(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return Number.isInteger(numeric) ? String(numeric) : numeric.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function surfaceValidationPopupHtml(properties) {
  const status = properties.surface_validation === "match" ? "一致" : "不一致";
  const statusClass = properties.surface_validation === "match" ? "surface-ok-text" : "surface-bad-text";
  return `
    <div class="road-popup-content">
      <strong>${escapeHtml(properties.name || properties.highway || "OSM road")}</strong>
      <dl>
        <dt>结果</dt><dd class="${statusClass}">${escapeHtml(status)}</dd>
        <dt>highway</dt><dd>${escapeHtml(properties.highway || "unknown")}</dd>
        <dt>OSM surface</dt><dd>${escapeHtml(properties.osm_surface || "unknown")}</dd>
        <dt>OSM 语义组</dt><dd>${escapeHtml(properties.osm_surface_group || "unknown")}</dd>
        <dt>VLM 多数</dt><dd>${escapeHtml(properties.vlm_surface_group || "unknown")}</dd>
        <dt>VLM 计数</dt><dd>${escapeHtml(formatCounts(properties.vlm_surface_group_counts || {}))}</dd>
        <dt>匹配点</dt><dd>${Number(properties.vlm_match_count || 0)} 个；可用 surface ${Number(properties.vlm_usable_surface_count || 0)} 个</dd>
      </dl>
    </div>
  `;
}

function zoomToCurrentGrid() {
  const boundsLayer = selectedGridNeighborhoodLayer || selectedGridLayer;
  if (!boundsLayer) return;
  map.fitBounds(boundsLayer.getBounds(), {
    padding: [80, 80],
    maxZoom: 17,
  });
}

async function selectGridByPoint(latlng) {
  try {
    const feature = await apiGet(`/api/grids/by-point?longitude=${encodeURIComponent(latlng.lng)}&latitude=${encodeURIComponent(latlng.lat)}`);
    let matchingLayer = null;
    gridLayer.eachLayer((layer) => {
      if (layer.feature?.properties?.grid_id === feature.properties.grid_id) {
        matchingLayer = layer;
      }
    });
    if (matchingLayer) {
      selectGrid(matchingLayer.feature, matchingLayer);
    }
  } catch (error) {
    loadStatusElement.textContent = `格网选择失败：${error.message}`;
    loadStatusElement.classList.add("error");
  }
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
    clearMapMatching();
    updateMapMatchingSequenceOptions();
    imageCountElement.textContent = result.meta.count;
    const cacheText = result.meta.cache === "hit" ? "容器缓存" : "Mapillary API";
    const truncated = result.meta.truncated ? "；结果达到 API 上限" : "";
    loadStatusElement.textContent = `${cacheText} · ${formatDate(result.meta.fetched_at)}${truncated}`;
    downloadLink.classList.remove("disabled");
    downloadLink.href = `/api/grids/${encodeURIComponent(gridId)}/images.geojson`;
    if (result.features.length === 0) showEmptyImageState();
    await loadVlmResults(gridId);
    refreshCellProcessPlan();
    confirmMapillaryButton.textContent = "Mapillary 已加载";
    confirmMapillaryButton.disabled = true;
  } catch (error) {
    if (sequence !== requestSequence) return;
    currentImageFeatures = [];
    resetMapMatchingControls();
    currentCellProcessPlan = emptyProcessPlan();
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
    refreshCellProcessPlan();
    if (currentImageFeature) showImage(currentImageFeature);
  } catch (error) {
    if (sequence !== vlmResultsSequence) return;
    vlmStatusElement.textContent = `VLM 结果读取失败：${error.message}`;
    vlmStatusElement.classList.add("error");
  }
}

async function loadConfirmedMapMatchedPoints() {
  try {
    const result = await apiGet("/api/mapillary/mapmatched-positions?limit=50000");
    const features = result.features || result.geojson?.features || [];
    allConfirmedMapMatchedPointFeatures = features;
    renderConfirmedMapMatchedPointLayer();
    updateMapMatchingSegmentOptions();
  } catch (error) {
    mapDataStatusElement.textContent = `已确认 map-matched 点加载失败：${error.message}`;
    mapDataStatusElement.classList.add("error");
  }
}

function renderConfirmedMapMatchedPointLayer() {
  confirmedMapMatchedPointLayer.clearLayers();
  const includeVirtual = map.getZoom() >= VLM_RENDER_VIRTUAL_MIN_ZOOM;
  const features = [];
  for (const sourceFeature of allConfirmedMapMatchedPointFeatures) {
    const properties = sourceFeature.properties || {};
    const imageId = String(properties.image_id || sourceFeature.id || "");
    const analysis = allVlmResultsByImageId[imageId];
    if (analysis) {
      const themed = vlmObservationFeatures(analysis, sourceFeature.geometry, includeVirtual)
        .map((feature) => ({
          ...feature,
          id: `confirmed:${imageId}:${feature.properties.observation_role}`,
        }));
      features.push(...themed);
    } else {
      features.push({
        type: "Feature",
        id: `confirmed:${imageId}:center`,
        geometry: sourceFeature.geometry,
        properties: {
          image_id: imageId,
          observation_role: "center",
          heading_deg: properties.computed_compass_angle ?? properties.compass_angle ?? 0,
          theme_field: "mapmatched_position",
          theme_value: "confirmed",
        },
      });
    }
  }
  confirmedMapMatchedPointLayer.addData(featureCollection(features));
}

async function loadAllVlmResults() {
  const sequence = ++allVlmResultsSequence;
  try {
    const result = await apiGet("/api/vlm-results?limit=50000");
    if (sequence !== allVlmResultsSequence) return;
    allVlmResultsByImageId = result.results || {};
    renderVlmResultLayer();
    renderConfirmedMapMatchedPointLayer();
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

async function loadVlmJobs() {
  if (!vlmJobsElement) return;
  try {
    const result = await apiGet("/api/vlm/jobs?limit=10");
    renderVlmJobs(result.jobs || []);
  } catch (error) {
    vlmJobsElement.textContent = `任务队列读取失败：${error.message}`;
    vlmJobsElement.classList.add("error");
  }
}

function renderVlmJobs(jobs) {
  vlmJobsElement.classList.remove("error");
  if (!jobs.length) {
    vlmJobsElement.textContent = "暂无任务。点击 Process 后只会先加入队列。";
    return;
  }
  vlmJobsElement.innerHTML = jobs.map((job) => {
    const total = Number(job.total || 0);
    const skipped = Number(job.skipped || 0);
    const attempted = Number(job.analyzed || 0);
    const effectiveTotal = Math.max(total - skipped, attempted, 0);
    const percent = effectiveTotal > 0 ? (attempted / effectiveTotal) * 100 : 0;
    const current = job.current_image_id ? ` · 当前 ${job.current_image_id}` : "";
    const error = job.error ? ` · ${job.error}` : "";
    const requestAvg = averageAnalyzedJobSeconds(job);
    const throughputAvg = averageThroughputSeconds(job);
    const requestAvgText = requestAvg == null ? "单请求平均 --" : `单请求平均 ${formatDuration(requestAvg)}/图`;
    const throughputAvgText = throughputAvg == null ? "吞吐平均 --" : `吞吐平均 ${formatDuration(throughputAvg)}/图`;
    const canCancel = ["queued", "running", "cancelling"].includes(job.status);
    const actionText = job.status === "running" ? "中断" : job.status === "cancelling" ? "中断中" : "取消";
    return `
      <div class="job-item job-${escapeAttribute(job.status || "unknown")}">
        <strong>${escapeHtml(job.status)} · ${attempted}/${effectiveTotal} (${percent.toFixed(1)}%)</strong>
        ${canCancel ? `<button type="button" data-cancel-vlm-job="${escapeAttribute(job.job_id)}"${job.status === "cancelling" ? " disabled" : ""}>${escapeHtml(actionText)}</button>` : ""}
        <progress class="job-progress" value="${escapeAttribute(attempted)}" max="${escapeAttribute(Math.max(effectiveTotal, 1))}"></progress>
        <div class="job-meta">${escapeHtml(job.grid_id || "unknown grid")} · ${escapeHtml(job.model || "model unknown")}</div>
        <div class="job-meta">${escapeHtml(throughputAvgText)} · ${escapeHtml(requestAvgText)} · 分析 ${attempted} · 跳过 ${skipped} · 失败 ${Number(job.failed || 0)}${escapeHtml(current)}${escapeHtml(error)}</div>
        <div class="job-meta">更新 ${escapeHtml(job.updated_at ? formatDate(job.updated_at) : "null")}</div>
      </div>
    `;
  }).join("");
}

function averageAnalyzedJobSeconds(job) {
  const successfulAnalyzed = Math.max(Number(job.analyzed || 0) - Number(job.failed || 0), 0);
  if (successfulAnalyzed <= 0) return null;
  const preciseSeconds = Number(job.analysis_seconds_sum || 0);
  if (preciseSeconds > 0) return preciseSeconds / successfulAnalyzed;
  if (!job.started_at) return null;
  const start = Date.parse(job.started_at);
  const end = Date.parse(job.completed_at || job.updated_at || new Date().toISOString());
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return (end - start) / 1000 / successfulAnalyzed;
}

function averageThroughputSeconds(job) {
  const completedWork = Number(job.analyzed || 0);
  const failed = Number(job.failed || 0);
  const countedWork = Math.max(completedWork - failed, 0);
  if (countedWork <= 0 || !job.started_at) return null;
  const start = Date.parse(job.started_at);
  const end = Date.parse(job.completed_at || job.updated_at || new Date().toISOString());
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return (end - start) / 1000 / countedWork;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "--";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m${String(rest).padStart(2, "0")}s`;
}

async function cancelVlmJob(jobId) {
  if (!jobId) return;
  try {
    await apiPost(`/api/vlm/jobs/${encodeURIComponent(jobId)}/cancel`, {});
    await loadVlmJobs();
  } catch (error) {
    vlmStatusElement.textContent = `取消 VLM 任务失败：${error.message}`;
    vlmStatusElement.classList.add("error");
  }
}

function emptyProcessPlan() {
  return {
    clusters: [],
    representativeImages: [],
    pendingImages: [],
    totalClusters: 0,
    processedClusters: 0,
    usableClusters: 0,
    unusableExhaustedClusters: 0,
    redundantCount: 0,
  };
}

function refreshCellProcessPlan() {
  currentCellProcessPlan = buildCellProcessPlan();
  updateCellProgressStatus();
  updateProcessButtonState();
}

function buildCellProcessPlan() {
  if (currentImageFeatures.length === 0) return emptyProcessPlan();
  const clusters = [];
  currentImageFeatures.forEach((feature) => {
    const geometry = displayGeometry(feature);
    const coordinates = geometry?.coordinates;
    if (!Array.isArray(coordinates)) return;
    let cluster = clusters.find((candidate) => (
      distanceMeters(coordinates, candidate.representativeCoordinates) <= CELL_PROCESS_DISTANCE_METERS
    ));
    if (!cluster) {
      cluster = {
        representativeCoordinates: coordinates,
        features: [],
      };
      clusters.push(cluster);
    }
    cluster.features.push(feature);
  });

  const representativeImages = [];
  const pendingImages = [];
  let processedClusters = 0;
  let usableClusters = 0;
  let unusableExhaustedClusters = 0;
  clusters.forEach((cluster) => {
    const usableFeature = cluster.features.find((feature) => hasUsableVlmResult(feature.id));
    const unprocessedFeature = cluster.features.find((feature) => !hasVlmResult(feature.id));
    const representative = usableFeature || unprocessedFeature || cluster.features[0];
    representativeImages.push(representative);
    if (usableFeature) {
      processedClusters += 1;
      usableClusters += 1;
    } else if (unprocessedFeature) {
      pendingImages.push(unprocessedFeature);
    } else {
      processedClusters += 1;
      unusableExhaustedClusters += 1;
    }
  });

  return {
    clusters,
    representativeImages,
    pendingImages,
    totalClusters: clusters.length,
    processedClusters,
    usableClusters,
    unusableExhaustedClusters,
    redundantCount: Math.max(currentImageFeatures.length - clusters.length, 0),
  };
}

function hasVlmResult(imageId) {
  const key = String(imageId);
  return Boolean(vlmResultsByImageId[key] || allVlmResultsByImageId[key]);
}

function hasUsableVlmResult(imageId) {
  const key = String(imageId);
  const analysis = vlmResultsByImageId[key] || allVlmResultsByImageId[key];
  if (!analysis || analysis.error) return false;
  const fields = analysis.fields || {};
  return (fields.unusable_reason || "none") === "none";
}

function updateCellProgressStatus() {
  if (!currentGrid || currentImageFeatures.length === 0 || activeVlmJobId) return;
  resetVlmProgress(currentCellProcessPlan.processedClusters, currentCellProcessPlan.totalClusters);
  const progressPercent = currentCellProcessPlan.totalClusters > 0
    ? (currentCellProcessPlan.processedClusters / currentCellProcessPlan.totalClusters) * 100
    : 0;
  const pending = forceVlmCheckbox.checked
    ? currentCellProcessPlan.representativeImages.length
    : currentCellProcessPlan.pendingImages.length;
  const latestUpdatedAt = latestVlmUpdatedAt(vlmResultsByImageId);
  const latestText = latestUpdatedAt ? `；最后更新 ${formatDate(latestUpdatedAt)}` : "";
  vlmStatusElement.classList.remove("error");
  vlmStatusElement.textContent = [
    `${CELL_PROCESS_DISTANCE_METERS}m位置进度 ${currentCellProcessPlan.processedClusters}/${currentCellProcessPlan.totalClusters} (${progressPercent.toFixed(1)}%)`,
    `有效覆盖 ${currentCellProcessPlan.usableClusters}`,
    `不可用耗尽 ${currentCellProcessPlan.unusableExhaustedClusters}`,
    `原始点 ${currentImageFeatures.length}`,
    `近邻冗余 ${currentCellProcessPlan.redundantCount}`,
    `待自动处理 ${pending}`,
    `${CELL_PROCESS_DISTANCE_METERS}m内已处理但不可用时会自动尝试近邻替代；其余近邻只允许单图手动 Process`,
    latestText.replace(/^；/, ""),
  ].filter(Boolean).join("；");
}

function distanceMeters(firstCoordinates, secondCoordinates) {
  const lon1 = Number(firstCoordinates[0]);
  const lat1 = Number(firstCoordinates[1]);
  const lon2 = Number(secondCoordinates[0]);
  const lat2 = Number(secondCoordinates[1]);
  const meanLat = ((lat1 + lat2) / 2) * Math.PI / 180;
  const metersPerDegreeLat = 111320;
  const metersPerDegreeLon = Math.cos(meanLat) * 111320;
  const dx = (lon1 - lon2) * metersPerDegreeLon;
  const dy = (lat1 - lat2) * metersPerDegreeLat;
  return Math.sqrt(dx * dx + dy * dy);
}

async function startCellVlmJob() {
  if (!currentGrid || currentImageFeatures.length === 0 || activeVlmJobId) return;
  refreshCellProcessPlan();
  const imagesToProcess = forceVlmCheckbox.checked
    ? currentCellProcessPlan.representativeImages
    : currentCellProcessPlan.pendingImages;
  if (imagesToProcess.length === 0) {
    updateCellProgressStatus();
    return;
  }
  const gridId = currentGrid.properties.grid_id;
  selectedModel = modelSelectElement.value || selectedModel;
  resetVlmProgress(currentCellProcessPlan.processedClusters, currentCellProcessPlan.totalClusters);
  processCellVlmButton.disabled = true;
  vlmStatusElement.classList.remove("error");
  vlmStatusElement.textContent = `正在加入 VLM 队列：${imagesToProcess.length} 个${CELL_PROCESS_DISTANCE_METERS}m代表点，模型 ${selectedModel || "默认模型"}。`;

  try {
    const job = await apiPost(`/api/grids/${encodeURIComponent(gridId)}/vlm-jobs`, {
      images: imagesToProcess,
      model: selectedModel,
      force: forceVlmCheckbox.checked,
    });
    activeVlmJobId = job.job_id;
    await loadVlmJobs();
    updateVlmJobUi(job);
    pollVlmJob(activeVlmJobId, gridId);
  } catch (error) {
    activeVlmJobId = null;
    vlmStatusElement.textContent = `VLM 任务创建失败：${error.message}`;
    vlmStatusElement.classList.add("error");
    updateProcessButtonState();
  }
}

function mapMatchedFeatureToVlmImage(feature) {
  const properties = feature.properties || {};
  const imageId = String(properties.image_id || feature.id || "");
  if (!imageId) return null;
  const gpsGeometry = properties.gps_geometry || properties.original_geometry || feature.geometry || null;
  const mapillaryGeometry = properties.mapillary_geometry || properties.computed_geometry || null;
  const mapmatchedGeometry = properties.mapmatched_geometry || null;
  const geometry = mapmatchedGeometry || mapillaryGeometry || gpsGeometry || feature.geometry || null;
  if (!geometry?.coordinates) return null;
  return {
    type: "Feature",
    id: imageId,
    geometry,
    properties: {
      ...properties,
      id: imageId,
      image_id: imageId,
      original_geometry: properties.original_geometry || gpsGeometry,
      gps_geometry: gpsGeometry,
      computed_geometry: properties.computed_geometry || mapillaryGeometry,
      mapillary_geometry: mapillaryGeometry,
      mapmatched_geometry: mapmatchedGeometry,
      mapmatched_segment_index: properties.segment_index,
      mapmatched_idx: properties.idx,
      mapmatched_profile: properties.profile,
    },
  };
}

function currentTrajectoryVlmImages() {
  const seen = new Set();
  const sourceFeatures = currentMapMatchingPointFeatures.length
    ? currentMapMatchingPointFeatures
    : currentConfirmedMapMatchingPointFeatures;
  return sourceFeatures
    .slice()
    .sort((a, b) => (
      Number(a.properties?.segment_index ?? 0) - Number(b.properties?.segment_index ?? 0)
      || Number(a.properties?.idx ?? 0) - Number(b.properties?.idx ?? 0)
    ))
    .map(mapMatchedFeatureToVlmImage)
    .filter((feature) => {
      if (!feature || seen.has(String(feature.id))) return false;
      seen.add(String(feature.id));
      return true;
    });
}

async function startCurrentTrajectoryVlmJob() {
  if (!currentGrid || activeVlmJobId) return;
  const imagesToProcess = currentTrajectoryVlmImages();
  if (imagesToProcess.length === 0) {
    if (mapMatchingStatusElement) {
      mapMatchingStatusElement.classList.add("error");
      mapMatchingStatusElement.textContent = "当前还没有可处理的轨迹图像点。先运行轨迹拆分或选择一个子轨迹。";
    }
    return;
  }
  const gridId = currentGrid.properties.grid_id;
  selectedModel = modelSelectElement.value || selectedModel;
  resetVlmProgress(0, Math.max(imagesToProcess.length, 1));
  processCurrentTrajectoryVlmButton.disabled = true;
  vlmStatusElement.classList.remove("error");
  vlmStatusElement.textContent = `正在把当前子轨迹 ${imagesToProcess.length} 张图片加入当前 cell 的 VLM 队列；已有结果会更新位置元数据，不重复调用模型。`;

  try {
    const job = await apiPost(`/api/grids/${encodeURIComponent(gridId)}/vlm-jobs`, {
      images: imagesToProcess,
      model: selectedModel,
      force: forceVlmCheckbox.checked,
    });
    activeVlmJobId = job.job_id;
    await loadVlmJobs();
    updateVlmJobUi(job);
    pollVlmJob(activeVlmJobId, gridId);
  } catch (error) {
    activeVlmJobId = null;
    vlmStatusElement.textContent = `当前轨迹 VLM 任务创建失败：${error.message}`;
    vlmStatusElement.classList.add("error");
    updateProcessButtonState();
    updateMapMatchingButtonState();
  }
}

async function confirmCurrentMapMatching() {
  if (!currentGrid || currentMapMatchingPointFeatures.length === 0 || mapMatchingRunning) return;
  const gridId = currentGrid.properties.grid_id;
  const features = currentMapMatchingPointFeatures
    .filter((feature) => feature.properties?.mapmatched_geometry?.coordinates)
    .map(mapMatchedFeatureToVlmImage)
    .filter(Boolean);
  if (features.length === 0) {
    mapMatchingStatusElement.classList.add("error");
    mapMatchingStatusElement.textContent = "当前预览轨迹没有可保存的 map-matched 点。";
    return;
  }
  confirmCurrentMapMatchingButton.disabled = true;
  mapMatchingStatusElement.classList.remove("error");
  mapMatchingStatusElement.textContent = `正在保存 ${features.length} 个已确认 map-matched 点到数据库…`;
  try {
    const result = await apiPost(`/api/grids/${encodeURIComponent(gridId)}/mapmatched-positions`, {
      features,
    });
    currentConfirmedMapMatchingPointFeatures = result.features || [];
    await loadConfirmedMapMatchedPoints();
    updateMapMatchingSegmentOptions();
    if (!map.hasLayer(confirmedMapMatchedPointLayer)) map.addLayer(confirmedMapMatchedPointLayer);
    confirmedMapMatchedPointLayer.bringToFront();
    mapMatchingStatusElement.textContent = `已确认保存 ${result.count || currentConfirmedMapMatchingPointFeatures.length} 个 map-matched 点；也可以继续点击 Process 当前子轨迹图片更新 VLM 字段。`;
    updateMapMatchingButtonState();
  } catch (error) {
    mapMatchingStatusElement.textContent = `保存确认匹配点失败：${error.message}`;
    mapMatchingStatusElement.classList.add("error");
    updateMapMatchingButtonState();
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
      await loadVlmJobs();
      await loadVlmResults(gridId);
      await loadAllVlmResults();
      if (["completed", "failed", "cancelled"].includes(job.status)) {
        stopVlmJobPolling();
        activeVlmJobId = null;
        await loadVlmResults(gridId);
        await loadAllVlmResults();
        updateProcessButtonState();
        await loadVlmJobs();
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
  refreshCellProcessPlan();
  const total = Number(job.total || 0);
  const skippedCount = Number(job.skipped || 0);
  const attempted = Number(job.analyzed || 0);
  const effectiveTotal = Math.max(total - skippedCount, attempted, 0);
  resetVlmProgress(attempted, Math.max(effectiveTotal, 1));
  const serverPercent = effectiveTotal > 0 ? (attempted / effectiveTotal) * 100 : 0;
  const progressPercent = currentCellProcessPlan.totalClusters > 0
    ? (currentCellProcessPlan.processedClusters / currentCellProcessPlan.totalClusters) * 100
    : 0;
  const current = job.current_image_id ? `；当前 ${job.current_image_id}` : "";
  const analyzed = job.analyzed ? `；新分析 ${job.analyzed}` : "";
  const skipped = job.skipped ? `；跳过已有 ${job.skipped}` : "";
  const failed = job.failed ? `；失败 ${job.failed}` : "";
  const completed = job.completed_at ? `；完成 ${formatDate(job.completed_at)}` : "";
  const requestAvg = averageAnalyzedJobSeconds(job);
  const throughputAvg = averageThroughputSeconds(job);
  const requestAvgText = requestAvg == null ? "单请求平均 --/图" : `单请求平均 ${formatDuration(requestAvg)}/图`;
  const throughputAvgText = throughputAvg == null ? "吞吐平均 --/图" : `吞吐平均 ${formatDuration(throughputAvg)}/图`;
  vlmStatusElement.classList.toggle("error", job.status === "failed");
  vlmStatusElement.textContent = `VLM ${job.status}：服务器分析进度 ${attempted}/${effectiveTotal} (${serverPercent.toFixed(1)}%)${analyzed}${skipped}${failed}；${throughputAvgText}；${requestAvgText}${current}${completed}；${CELL_PROCESS_DISTANCE_METERS}m代表点覆盖 ${currentCellProcessPlan.processedClusters}/${currentCellProcessPlan.totalClusters} (${progressPercent.toFixed(1)}%)；有效覆盖 ${currentCellProcessPlan.usableClusters}；不可用耗尽 ${currentCellProcessPlan.unusableExhaustedClusters}`;
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
  const hasSelectedImage = Boolean(currentImageFeature);
  processCellVlmButton.hidden = hasSelectedImage;
  processCurrentImageButton.hidden = !hasSelectedImage;

  const canProcessCell = Boolean(
    currentGrid
    && ollamaReady
    && currentImageFeatures.length > 0
    && !activeVlmJobId
    && (forceVlmCheckbox.checked
      ? currentCellProcessPlan.representativeImages.length > 0
      : currentCellProcessPlan.pendingImages.length > 0)
  );
  const canProcessImage = Boolean(
    currentGrid
    && currentImageFeature
    && ollamaReady
    && !activeVlmJobId
  );
  processCellVlmButton.disabled = !canProcessCell;
  processCurrentImageButton.disabled = !canProcessImage;
  if (deleteCellVlmButton) {
    deleteCellVlmButton.disabled = !currentGrid || activeVlmJobId || Object.keys(vlmResultsByImageId).length === 0;
  }
  updateMapMatchingButtonState();
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
    ${renderImageVlmActions(String(feature.id), Boolean(analysis), true)}
    ${renderVlmResult(analysis)}
  `;
  updateProcessButtonState();
  scrollImageDetailIntoView();
}

function showStoredVlmPoint(imageId) {
  const analysis = allVlmResultsByImageId[String(imageId)];
  if (!analysis) return;
  const properties = analysis.image_properties || {};
  const thumbnail = properties.thumb_1024_url || properties.thumb_256_url
    ? `<img src="${escapeAttribute(properties.thumb_1024_url || properties.thumb_256_url)}" alt="Mapillary 图像 ${escapeHtml(String(imageId))}">`
    : '<div class="placeholder"><strong>无缩略图</strong><span>当前缓存里没有该图片 URL。</span></div>';
  const mapillaryUrl = properties.mapillary_url || `https://www.mapillary.com/app/?pKey=${encodeURIComponent(imageId)}`;
  currentImageFeature = null;
  imageDetailElement.className = "image-detail";
  imageDetailElement.innerHTML = `
    ${thumbnail}
    <h2>图像 ${escapeHtml(String(imageId))}</h2>
    <dl class="metadata">
      <dt>grid_id</dt><dd>${escapeHtml(analysis.grid_id || "null")}</dd>
      <dt>坐标</dt><dd>${escapeHtml(formatCoordinates(analysis.geometry?.coordinates))}</dd>
      <dt>原始 GPS</dt><dd>${escapeHtml(formatCoordinates((properties.gps_geometry || properties.original_geometry)?.coordinates))}</dd>
      <dt>Mapillary 校正</dt><dd>${escapeHtml(formatCoordinates((properties.mapillary_geometry || properties.computed_geometry)?.coordinates))}</dd>
      <dt>蓝线采样坐标</dt><dd>${escapeHtml(formatCoordinates(properties.mapmatched_geometry?.coordinates))}</dd>
      <dt>拍摄时间</dt><dd>${escapeHtml(properties.captured_at ? formatDate(properties.captured_at) : "未知")}</dd>
      <dt>拍摄方向</dt><dd>${properties.compass_angle == null ? "未知" : `${Number(properties.compass_angle).toFixed(1)}°`}</dd>
      <dt>相机类型</dt><dd>${escapeHtml(properties.camera_type || "未知")}</dd>
      <dt>主题字段</dt><dd>${escapeHtml(selectedVlmTheme)}</dd>
      <dt>主题值</dt><dd>${escapeHtml(formatVlmValue(analysis, selectedVlmTheme))}</dd>
    </dl>
    <a class="open-mapillary" href="${escapeAttribute(mapillaryUrl)}" target="_blank" rel="noreferrer">在 Mapillary 街景中打开</a>
    ${renderImageVlmActions(String(imageId), Boolean(analysis), false)}
    ${renderVlmResult(analysis)}
  `;
  updateProcessButtonState();
  scrollImageDetailIntoView();
}

function showMapMatchedImage(feature) {
  const properties = feature.properties || {};
  const imageId = String(properties.image_id || feature.id || "");
  const sourceFeature = currentImageFeatures.find((item) => String(item.id) === imageId);
  currentImageFeature = sourceFeature || mapMatchedFeatureToVlmImage(feature);
  const rawCoordinates = feature.geometry?.coordinates;
  const snappedCoordinates = properties.snapped_geometry?.coordinates;
  const mapmatchedCoordinates = properties.mapmatched_geometry?.coordinates;
  const thumbnailUrl = properties.thumb_1024_url || properties.thumb_256_url;
  const thumbnail = thumbnailUrl
    ? `<img src="${escapeAttribute(thumbnailUrl)}" alt="Mapillary 图像 ${escapeHtml(imageId)}">`
    : '<div class="placeholder"><strong>无缩略图</strong><span>当前缓存里没有该图片 URL。</span></div>';
  const mapillaryUrl = properties.mapillary_url || `https://www.mapillary.com/app/?pKey=${encodeURIComponent(imageId)}`;
  const analysis = vlmResultsByImageId[imageId] || allVlmResultsByImageId[imageId] || null;

  imageDetailElement.className = "image-detail";
  imageDetailElement.innerHTML = `
    ${thumbnail}
    <h2>Map matching 图像 ${escapeHtml(imageId)}</h2>
    <dl class="metadata">
      <dt>轨迹序号</dt><dd>${escapeHtml(String(properties.idx ?? "null"))}</dd>
      <dt>拍摄时间</dt><dd>${escapeHtml(properties.captured_at ? formatDate(properties.captured_at) : "未知")}</dd>
      <dt>原始 GPS</dt><dd>${escapeHtml(formatCoordinates(rawCoordinates))}</dd>
      <dt>Mapillary 校正</dt><dd>${escapeHtml(formatCoordinates(properties.mapillary_geometry?.coordinates))}</dd>
      <dt>蓝线采样坐标</dt><dd>${escapeHtml(formatCoordinates(mapmatchedCoordinates))}</dd>
      <dt>匹配后坐标</dt><dd>${escapeHtml(formatCoordinates(snappedCoordinates))}</dd>
      <dt>匹配道路</dt><dd>${escapeHtml(roadCategoryLabel(properties.road_category))} · ${escapeHtml(properties.highway || "unknown")}</dd>
      <dt>OSM surface</dt><dd>${escapeHtml(properties.surface || "unknown")}</dd>
      <dt>距离</dt><dd>${escapeHtml(String(properties.distance_m ?? "null"))} m</dd>
      <dt>score</dt><dd>${escapeHtml(String(properties.score ?? "null"))}</dd>
      <dt>VLM位置</dt><dd>${escapeHtml(properties.capture_position || "null")}</dd>
      <dt>VLM表面</dt><dd>${escapeHtml(properties.surface_material || "null")}</dd>
      <dt>相机方向</dt><dd>${properties.heading_deg == null ? "未知" : `${Number(properties.heading_deg).toFixed(1)}°`}</dd>
      <dt>轨迹方向</dt><dd>${properties.track_heading_deg == null ? "未知" : `${Number(properties.track_heading_deg).toFixed(1)}°`}</dd>
      <dt>sequence</dt><dd>${escapeHtml(String(properties.sequence_id || "未知"))}</dd>
    </dl>
    <a class="open-mapillary" href="${escapeAttribute(mapillaryUrl)}" target="_blank" rel="noreferrer">在 Mapillary 街景中打开</a>
    ${renderImageVlmActions(imageId, Boolean(analysis), Boolean(currentImageFeature))}
    ${renderVlmResult(analysis)}
  `;
  updateProcessButtonState();
  scrollImageDetailIntoView();
}

async function startCurrentImageVlmJob() {
  if (!currentGrid || !currentImageFeature || activeVlmJobId) return;
  const gridId = currentGrid.properties.grid_id;
  selectedModel = modelSelectElement.value || selectedModel;
  resetVlmProgress(0, 1);
  vlmStatusElement.classList.remove("error");
  vlmStatusElement.textContent = `正在把当前图片 ${currentImageFeature.id} 加入 VLM 队列，模型 ${selectedModel || "默认模型"}。`;
  updateProcessButtonState();
  processCurrentImageButton.disabled = true;

  try {
    const job = await apiPost(`/api/grids/${encodeURIComponent(gridId)}/vlm-jobs`, {
      images: [currentImageFeature],
      model: selectedModel,
      force: true,
    });
    activeVlmJobId = job.job_id;
    await loadVlmJobs();
    updateVlmJobUi(job);
    pollVlmJob(activeVlmJobId, gridId);
  } catch (error) {
    activeVlmJobId = null;
    vlmStatusElement.textContent = `当前图片 VLM 任务创建失败：${error.message}`;
    vlmStatusElement.classList.add("error");
    updateProcessButtonState();
    processCurrentImageButton.disabled = false;
  }
}

function renderImageVlmActions(imageId, hasAnalysis, canProcess) {
  return `
    <div class="image-vlm-actions">
      <button type="button" data-image-action="process" data-image-id="${escapeAttribute(imageId)}"${canProcess ? "" : " disabled"}>Process 当前图片</button>
      <button type="button" class="danger" data-image-action="delete-vlm" data-image-id="${escapeAttribute(imageId)}"${hasAnalysis ? "" : " disabled"}>删除 process 信息</button>
    </div>
  `;
}

async function deleteCurrentImageVlmResult(imageId) {
  if (!imageId) return;
  const hadCurrentFeature = currentImageFeature && String(currentImageFeature.id) === String(imageId);
  vlmStatusElement.classList.remove("error");
  vlmStatusElement.textContent = `正在删除图片 ${imageId} 的 VLM process 信息…`;
  try {
    await apiDelete(`/api/vlm-results/${encodeURIComponent(imageId)}`);
    if (currentGrid) {
      await loadVlmResults(currentGrid.properties.grid_id);
    }
    await loadAllVlmResults();
    if (hadCurrentFeature && currentImageFeature) {
      showImage(currentImageFeature);
    } else {
      currentImageFeature = null;
      imageDetailElement.className = "image-detail empty";
      imageDetailElement.innerHTML = `
        <div class="placeholder">
          <strong>已删除 VLM process 信息</strong>
          <span>图片 ${escapeHtml(String(imageId))} 的分析结果已从数据库删除。</span>
        </div>
      `;
      updateProcessButtonState();
    }
    roadSurfaceValidationLayer.clearLayers();
    surfaceValidationStatus.textContent = "VLM 结果已变更；需要重新计算全域 road surface 一致性。";
    vlmStatusElement.textContent = `已删除图片 ${imageId} 的 VLM process 信息。`;
  } catch (error) {
    vlmStatusElement.textContent = `删除 VLM process 信息失败：${error.message}`;
    vlmStatusElement.classList.add("error");
  }
}

async function deleteCurrentCellVlmResults() {
  if (!currentGrid || activeVlmJobId) return;
  const gridId = currentGrid.properties.grid_id;
  const count = Object.keys(vlmResultsByImageId).length;
  if (count === 0) return;
  const confirmed = window.confirm(`删除当前 cell ${gridId} 的 ${count} 条 VLM 结果？这个操作只删除数据库分析结果，不删除 Mapillary 缓存。`);
  if (!confirmed) return;
  vlmStatusElement.classList.remove("error");
  vlmStatusElement.textContent = `正在删除当前 cell ${gridId} 的 VLM 数据…`;
  updateProcessButtonState();
  try {
    const result = await apiDelete(`/api/grids/${encodeURIComponent(gridId)}/vlm-results`);
    await loadVlmResults(gridId);
    await loadAllVlmResults();
    currentCellProcessPlan = emptyProcessPlan();
    refreshCellProcessPlan();
    roadSurfaceValidationLayer.clearLayers();
    surfaceValidationStatus.textContent = "VLM 结果已变更；需要重新计算全域 road surface 一致性。";
    vlmStatusElement.textContent = `已删除当前 cell ${gridId} 的 ${result.deleted} 条 VLM 结果。`;
  } catch (error) {
    vlmStatusElement.textContent = `删除当前 cell VLM 数据失败：${error.message}`;
    vlmStatusElement.classList.add("error");
  } finally {
    updateProcessButtonState();
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
  if (typeof value === "object") return JSON.stringify(value);
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

function thematicObservationLayer(feature, latlng, pane, renderer) {
  const properties = feature.properties || {};
  if (properties.observation_role === "center") {
    const angle = Number(properties.heading_deg);
    const color = vlmThemeColor(properties.theme_value);
    return L.marker(latlng, {
      pane,
      icon: L.divIcon({
        className: "vlm-direction-marker",
        html: `<span style="--vlm-color: ${escapeAttribute(color)}; transform: rotate(${Number.isFinite(angle) ? angle : 0}deg)">▲</span>`,
        iconSize: [24, 24],
        iconAnchor: [12, 12],
      }),
      keyboard: true,
      title: `${properties.image_id} center ${properties.theme_value}`,
    });
  }
  return L.circleMarker(latlng, {
    pane,
    radius: 3.5,
    color: "#18201d",
    weight: 1.4,
    fillColor: vlmThemeColor(properties.theme_value),
    fillOpacity: 0.9,
    className: "vlm-result-point vlm-result-point-virtual",
    interactive: true,
    renderer,
  });
}

function renderVlmResultLayer() {
  if (!map.hasLayer(vlmResultLayer)) return;
  vlmResultLayer.clearLayers();
  const renderBounds = map.getBounds().pad(VLM_RENDER_BOUNDS_PADDING);
  const includeVirtual = map.getZoom() >= VLM_RENDER_VIRTUAL_MIN_ZOOM;
  const imageFeatureById = new Map(currentImageFeatures.map((feature) => [String(feature.id), feature]));
  const features = [];
  for (const analysis of Object.values(allVlmResultsByImageId)) {
    const imageFeature = imageFeatureById.get(String(analysis.image_id));
    const storedProperties = analysis.image_properties || {};
    const geometry = storedProperties.mapmatched_geometry
      || analysis.geometry
      || (imageFeature ? displayGeometry(imageFeature) : null);
    if (!geometry) continue;
    const visibleFeatures = vlmObservationFeatures(analysis, geometry, includeVirtual)
      .filter((feature) => geometryInBounds(feature.geometry, renderBounds))
      .map((feature) => ({
        ...feature,
        id: `${analysis.image_id}:${feature.properties.observation_role}`,
      }));
    for (const feature of visibleFeatures) {
      features.push(feature);
      if (features.length >= VLM_RENDER_FEATURE_LIMIT) break;
    }
    if (features.length >= VLM_RENDER_FEATURE_LIMIT) break;
  }
  vlmResultLayer.addData({ type: "FeatureCollection", features });
}

function scheduleVlmResultLayerRender() {
  if (vlmRenderFrame) window.cancelAnimationFrame(vlmRenderFrame);
  vlmRenderFrame = window.requestAnimationFrame(() => {
    vlmRenderFrame = null;
    renderVlmResultLayer();
  });
}

function geometryInBounds(geometry, bounds) {
  if (!geometry || geometry.type !== "Point") return false;
  const [lon, lat] = geometry.coordinates || [];
  if (!Number.isFinite(Number(lon)) || !Number.isFinite(Number(lat))) return false;
  return bounds.contains([Number(lat), Number(lon)]);
}

function vlmObservationFeatures(analysis, centerGeometry, includeVirtual = true) {
  const centerValue = formatVlmValue(analysis, selectedVlmTheme);
  const features = [];
  if (isDisplayableVlmThemeValue(centerValue)) {
    features.push({
      type: "Feature",
      geometry: centerGeometry,
      properties: {
        image_id: String(analysis.image_id),
        observation_role: "center",
        observation_vote: 1,
        heading_deg: analysisHeading(analysis),
        theme_field: selectedVlmTheme,
        theme_value: centerValue,
        updated_at: analysis.updated_at,
      },
    });
  }
  if (includeVirtual) {
    const heading = analysisHeading(analysis);
    for (const side of ["left", "right"]) {
      const virtual = virtualVlmObservation(analysis, centerGeometry, heading, side);
      if (virtual) features.push(virtual);
    }
  }
  return features;
}

function virtualVlmObservation(analysis, centerGeometry, heading, side) {
  const fields = analysis.fields || {};
  const geometry = offsetLngLatGeometry(centerGeometry, heading, side, 2);
  if (!geometry) return null;
  let field = null;
  let value = null;
  let roleValue = null;
  if (fields.capture_position === "vehicle_road" && fields[`${side}_sidewalk`] === "yes") {
    field = `${side}_sidewalk_surface_material`;
    value = fields[field];
    roleValue = "pedestrian_road";
  } else if (["pedestrian_road", "bicycle_road"].includes(fields.capture_position)
    && ["vehicle_road", "pedestrian_road", "bicycle_road"].includes(fields[`${side}_adjacent_road_type`])) {
    field = `${side}_adjacent_road_surface_material`;
    value = fields[field];
    roleValue = fields[`${side}_adjacent_road_type`];
  }
  if (value == null) return null;
  const themed = virtualThemeValue(analysis, side, field, value, roleValue);
  if (!isDisplayableVlmThemeValue(themed.value)) return null;
  return {
    type: "Feature",
    geometry,
    properties: {
      image_id: String(analysis.image_id),
      observation_role: side,
      observation_vote: 1,
      theme_field: themed.field,
      theme_value: themed.value,
      updated_at: analysis.updated_at,
    },
  };
}

function virtualThemeValue(analysis, side, defaultField, defaultValue, roleValue) {
  const fields = analysis.fields || {};
  const sidePrefix = selectedVlmTheme.startsWith("left_") ? "left" : selectedVlmTheme.startsWith("right_") ? "right" : null;
  if (sidePrefix && sidePrefix !== side) {
    return { field: selectedVlmTheme, value: "null" };
  }
  if (selectedVlmTheme === "capture_position") {
    return { field: "virtual_capture_position", value: roleValue || "null" };
  }
  if (selectedVlmTheme === "surface_material") {
    return { field: defaultField, value: String(defaultValue) };
  }
  if (selectedVlmTheme === `${side}_sidewalk`
    || selectedVlmTheme === `${side}_sidewalk_surface_material`
    || selectedVlmTheme === `${side}_adjacent_road_type`
    || selectedVlmTheme === `${side}_adjacent_road_surface_material`) {
    return { field: selectedVlmTheme, value: formatVlmValue(analysis, selectedVlmTheme) };
  }
  if (fields[selectedVlmTheme] != null) {
    return { field: selectedVlmTheme, value: formatVlmValue(analysis, selectedVlmTheme) };
  }
  return { field: defaultField, value: String(defaultValue) };
}

function isDisplayableVlmThemeValue(value) {
  return String(value) !== "uncertain";
}

function analysisHeading(analysis) {
  const properties = analysis.image_properties || {};
  const angle = properties.computed_compass_angle ?? properties.compass_angle;
  const numeric = Number(angle);
  return Number.isFinite(numeric) ? numeric : null;
}

function offsetLngLatGeometry(geometry, heading, side, meters) {
  if (!geometry || geometry.type !== "Point" || heading == null || !["left", "right"].includes(side)) return null;
  const [lon, lat] = geometry.coordinates || [];
  if (!Number.isFinite(Number(lon)) || !Number.isFinite(Number(lat))) return null;
  const bearing = ((Number(heading) + (side === "left" ? -90 : 90)) % 360 + 360) % 360;
  const radians = bearing * Math.PI / 180;
  const radius = 6378137;
  const latRad = Number(lat) * Math.PI / 180;
  const deltaLat = (meters * Math.cos(radians)) / radius;
  const deltaLon = (meters * Math.sin(radians)) / (radius * Math.max(Math.cos(latRad), 1e-9));
  return {
    type: "Point",
    coordinates: [
      Number(lon) + deltaLon * 180 / Math.PI,
      Number(lat) + deltaLat * 180 / Math.PI,
    ],
  };
}

function vlmThemeColor(value) {
  const colors = {
    vehicle_road: "#e03131",
    pedestrian_road: "#9c36b5",
    bicycle_road: "#087f5b",
    car: "#e03131",
    pedestrian: "#9c36b5",
    bicycle: "#087f5b",
    other_location: "#495057",
    transit_vehicle: "#7048e8",
    poor_image_quality: "#e8590c",
    railway_scene: "#5c677d",
    asphalt: "#343a40",
    concrete: "#868e96",
    paving_stones: "#f08c00",
    sett: "#9c6644",
    unpaved: "#7f4f24",
    yes: "#087f5b",
    no: "#adb5bd",
    uncertain: "#4263eb",
    confirmed: "#0b7285",
    null: "#dee2e6",
  };
  return colors[value] || "#1c7ed6";
}

function showAwaitingConfirmation() {
  currentImageFeature = null;
  imageDetailElement.className = "image-detail empty";
  imageDetailElement.innerHTML = '<div class="placeholder"><strong>等待确认</strong><span>当前操作尚未访问 Mapillary API。</span></div>';
  updateProcessButtonState();
  scrollImageDetailIntoView();
}

function showEmptyImageState() {
  currentImageFeature = null;
  imageDetailElement.className = "image-detail empty";
  imageDetailElement.innerHTML = '<div class="placeholder"><strong>格网内没有图像</strong><span>可选择相邻格网继续检查。</span></div>';
  updateProcessButtonState();
  scrollImageDetailIntoView();
}

function scrollImageDetailIntoView() {
  if (!detailScrollElement) return;
  const stickyOffset = themePanelElement ? themePanelElement.offsetHeight : 0;
  detailScrollElement.scrollTop = Math.max(imageDetailElement.offsetTop - stickyOffset - 8, 0);
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

function mapMatchColor(capturePosition) {
  if (capturePosition === "pedestrian_road") return "#9c36b5";
  if (capturePosition === "bicycle_road") return "#087f5b";
  if (capturePosition === "vehicle_road") return "#e03131";
  return "#495057";
}

function mapMatchTrajectoryColor(feature, fallback) {
  const properties = feature?.properties || {};
  let value = null;
  if (selectedVlmTheme === "capture_position") {
    value = mapMatchingUserTypeToCapturePositionValue(properties.user_type);
  }
  return value ? vlmThemeColor(value) : fallback;
}

function mapMatchingUserTypeToCapturePositionValue(userType) {
  if (userType === "car") return "vehicle_road";
  if (userType === "bike") return "bicycle_road";
  if (userType === "foot") return "pedestrian_road";
  return null;
}

function roadWeight(category) {
  const zoomBoost = map.getZoom() >= 17 ? 1.4 : 1;
  if (category === "vehicle") return 3.2 * zoomBoost;
  if (category === "bicycle") return 2.8 * zoomBoost;
  return 2.6 * zoomBoost;
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
processCurrentImageButton.addEventListener("click", startCurrentImageVlmJob);
deleteCellVlmButton?.addEventListener("click", deleteCurrentCellVlmResults);
validateRoadSurfaceButton?.addEventListener("click", runRoadSurfaceValidation);
runGraphhopperDirectButton?.addEventListener("click", () => runMapMatchingForCurrentGrid(false));
runGraphhopperMatchingButton?.addEventListener("click", () => runMapMatchingForCurrentGrid(true));
confirmCurrentMapMatchingButton?.addEventListener("click", confirmCurrentMapMatching);
processCurrentTrajectoryVlmButton?.addEventListener("click", startCurrentTrajectoryVlmJob);
mapMatchingSegmentSelect?.addEventListener("change", () => {
  currentMapMatchingSelectedSegment = Number(mapMatchingSegmentSelect.value || 0);
  currentConfirmedMapMatchingPointFeatures = [];
  renderSelectedMapMatchingSegment();
  if (mapMatchingStatusElement) {
    mapMatchingStatusElement.classList.remove("error");
    const hasMapmatchedPreview = currentMapMatchingPointFeatures.some((feature) => (
      feature.properties?.mapmatched_geometry?.coordinates
    ));
    mapMatchingStatusElement.textContent = hasMapmatchedPreview
      ? `当前显示 segment ${currentMapMatchingSelectedSegment}。请检查这一段，确认保存后可继续 process。`
      : `当前显示 raw segment ${currentMapMatchingSelectedSegment}。请先点击“Process 当前子轨迹图片”，类型票达标后再次运行匹配。`;
  }
});
imageDetailElement.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-image-action]");
  if (!button) return;
  const action = button.dataset.imageAction;
  const imageId = button.dataset.imageId;
  if (action === "process") {
    startCurrentImageVlmJob();
  } else if (action === "delete-vlm") {
    deleteCurrentImageVlmResult(imageId);
  }
});
map.getContainer().addEventListener("click", (event) => {
  const button = event.target.closest("button[data-open-vlm-image]");
  if (!button) return;
  L.DomEvent.stopPropagation(event);
  showStoredVlmPoint(button.dataset.openVlmImage);
});
modelSelectElement.addEventListener("change", () => {
  selectedModel = modelSelectElement.value;
  updateProcessButtonState();
});
forceVlmCheckbox.addEventListener("change", () => {
  refreshCellProcessPlan();
});
refreshJobsButton?.addEventListener("click", loadVlmJobs);
vlmJobsElement?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-cancel-vlm-job]");
  if (!button) return;
  cancelVlmJob(button.dataset.cancelVlmJob);
});
mapillaryGeometryModeSelect.addEventListener("change", () => {
  mapillaryGeometryMode = mapillaryGeometryModeSelect.value === "computed" ? "computed" : "original";
  renderImageLayer();
  clearMapMatching();
  updateMapMatchingSequenceOptions();
  scheduleVlmResultLayerRender();
  refreshCellProcessPlan();
  if (currentImageFeature) showImage(currentImageFeature);
});
vlmThemeSelect.addEventListener("change", () => {
  selectedVlmTheme = VLM_THEME_FIELDS.includes(vlmThemeSelect.value)
    ? vlmThemeSelect.value
    : "capture_position";
  scheduleVlmResultLayerRender();
  renderConfirmedMapMatchedPointLayer();
  mapMatchRawTrajectoryLayer.setStyle((feature) => ({
    color: mapMatchTrajectoryColor(feature, "#f08c00"),
    weight: 4.2,
    opacity: 0.9,
    dashArray: "8 7",
    lineCap: "round",
    lineJoin: "round",
  }));
  mapMatchMatchedTrajectoryLayer.setStyle((feature) => ({
    color: mapMatchTrajectoryColor(feature, "#1c7ed6"),
    weight: 6.5,
    opacity: 0.94,
    lineCap: "round",
    lineJoin: "round",
  }));
});
map.on("zoomend", () => {
  [pedestrianRoadLayer, vehicleRoadLayer, bicycleRoadLayer].forEach((layer) => {
    layer.setStyle((feature) => ({
      color: roadColor(feature.properties.road_category),
      weight: roadWeight(feature.properties.road_category),
      opacity: 0.62,
    }));
  });
  scheduleVlmResultLayerRender();
  renderConfirmedMapMatchedPointLayer();
});
map.on("moveend", scheduleVlmResultLayerRender);
map.on("overlayadd", (event) => {
  if (event.layer === vlmResultLayer) scheduleVlmResultLayerRender();
});
map.on("click", (event) => {
  const roadFeature = nearestRoadFeatureAtLatLng(event.latlng);
  if (roadFeature) {
    selectRoadForVlmMatching(roadFeature, event.latlng);
    return;
  }
  selectGridByPoint(event.latlng);
});
checkHealth();
initializeMainz();
loadConfirmedMapMatchedPoints();
loadAllVlmResults();
loadVlmJobs();
window.setInterval(loadVlmJobs, 5000);
