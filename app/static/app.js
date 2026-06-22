const map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([49.9929, 8.2473], 12);

window.addEventListener("load", () => map.invalidateSize({ pan: false }));
window.addEventListener("resize", () => map.invalidateSize({ pan: false }));

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 20,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
}).addTo(map);

const vectorRenderer = L.canvas({ padding: 0.4 });
let selectedGridLayer = null;
let currentGrid = null;
let requestSequence = 0;

function censusGridStyle(feature) {
  const population = feature.properties.population;
  const intensity = population == null ? 0 : Math.min(population / 150, 1);
  return {
    renderer: vectorRenderer,
    color: "#3d5149",
    weight: 0.55,
    opacity: 0.8,
    fillColor: population == null ? "#dbe3df" : `hsl(${150 - intensity * 45} 46% ${86 - intensity * 35}%)`,
    fillOpacity: population == null ? 0.05 : 0.34,
  };
}

const gridLayer = L.geoJSON(null, {
  renderer: vectorRenderer,
  style: censusGridStyle,
  onEachFeature: (feature, layer) => {
    layer.on("click", (event) => {
      L.DomEvent.stopPropagation(event);
      selectGrid(feature, layer);
    });
  },
}).addTo(map);

const boundaryLayer = L.geoJSON(null, {
  style: { color: "#172e25", weight: 2.2, fill: false, opacity: 0.9 },
}).addTo(map);

const imageLayer = L.geoJSON(null, {
  pointToLayer: (feature, latlng) => {
    const properties = feature.properties;
    const angle = Number(properties.computed_compass_angle ?? properties.compass_angle ?? 0);
    return L.marker(latlng, {
      icon: L.divIcon({
        className: "direction-marker",
        html: `<span style="transform: rotate(${Number.isFinite(angle) ? angle : 0}deg)">▲</span>`,
        iconSize: [20, 20],
        iconAnchor: [10, 10],
      }),
      keyboard: true,
      title: `拍摄方向 ${Number.isFinite(angle) ? angle.toFixed(1) : "0.0"}°`,
    });
  },
  onEachFeature: (feature, layer) => layer.on("click", () => showImage(feature)),
}).addTo(map);

const gridIdElement = document.getElementById("grid-id");
const gridCoordinatesElement = document.getElementById("grid-coordinates");
const imageCountElement = document.getElementById("image-count");
const loadStatusElement = document.getElementById("load-status");
const confirmMapillaryButton = document.getElementById("confirm-mapillary-button");
const downloadLink = document.getElementById("download-link");
const healthElement = document.getElementById("health");
const imageDetailElement = document.getElementById("image-detail");
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

async function checkHealth() {
  try {
    const health = await apiGet("/api/health");
    healthElement.textContent = health.mapillary_configured
      ? "Mapillary 已配置"
      : "需要配置 Mapillary Token";
    healthElement.classList.toggle("warning", !health.mapillary_configured);
  } catch (_error) {
    healthElement.textContent = "后端连接失败";
    healthElement.classList.add("warning");
  }
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
  if (currentGrid?.properties.grid_id === feature.properties.grid_id) return;
  if (selectedGridLayer) selectedGridLayer.setStyle(censusGridStyle(selectedGridLayer.feature));
  selectedGridLayer = layer;
  selectedGridLayer.setStyle({ color: "#f2a900", weight: 3, fillOpacity: 0.48 });
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
  imageCountElement.textContent = "0";
  confirmMapillaryButton.disabled = false;
  confirmMapillaryButton.textContent = "确认访问 Mapillary";
  downloadLink.classList.add("disabled");
  downloadLink.href = "#";
  loadStatusElement.classList.remove("error");
  loadStatusElement.textContent = "已选择格网。点击“确认访问 Mapillary”后才会请求街景数据。";
  showAwaitingConfirmation();
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
    imageLayer.addData(result);
    imageCountElement.textContent = result.meta.count;
    const cacheText = result.meta.cache === "hit" ? "容器缓存" : "Mapillary API";
    const truncated = result.meta.truncated ? "；结果达到 API 上限" : "";
    loadStatusElement.textContent = `${cacheText} · ${formatDate(result.meta.fetched_at)}${truncated}`;
    downloadLink.classList.remove("disabled");
    downloadLink.href = `/api/grids/${encodeURIComponent(gridId)}/images.geojson`;
    if (result.features.length === 0) showEmptyImageState();
    confirmMapillaryButton.textContent = "Mapillary 已加载";
    confirmMapillaryButton.disabled = true;
  } catch (error) {
    if (sequence !== requestSequence) return;
    imageCountElement.textContent = "0";
    setError(error.message);
    if (sequence === requestSequence) confirmMapillaryButton.disabled = false;
  }
}

function showImage(feature) {
  const properties = feature.properties;
  const coordinates = feature.geometry.coordinates;
  const capturedAt = properties.captured_at
    ? new Date(properties.captured_at).toLocaleString("zh-CN")
    : "未知";
  const angle = properties.computed_compass_angle ?? properties.compass_angle;
  const thumbnail = properties.thumb_1024_url
    ? `<img src="${escapeAttribute(properties.thumb_1024_url)}" alt="Mapillary 图像 ${escapeHtml(String(feature.id))}">`
    : '<div class="placeholder"><strong>无缩略图</strong></div>';

  imageDetailElement.className = "image-detail";
  imageDetailElement.innerHTML = `
    ${thumbnail}
    <h2>图像 ${escapeHtml(String(feature.id))}</h2>
    <dl class="metadata">
      <dt>拍摄时间</dt><dd>${escapeHtml(capturedAt)}</dd>
      <dt>坐标</dt><dd>${coordinates[1].toFixed(7)}, ${coordinates[0].toFixed(7)}</dd>
      <dt>拍摄方向</dt><dd>${angle == null ? "未知" : `${Number(angle).toFixed(1)}°`}</dd>
      <dt>相机类型</dt><dd>${escapeHtml(properties.camera_type || "未知")}</dd>
      <dt>图像尺寸</dt><dd>${properties.width || "?"} × ${properties.height || "?"}</dd>
      <dt>序列 ID</dt><dd>${escapeHtml(String(properties.sequence_id || "未知"))}</dd>
    </dl>
    <a class="open-mapillary" href="${escapeAttribute(properties.mapillary_url)}" target="_blank" rel="noreferrer">在 Mapillary 街景中打开</a>
  `;
}

function showAwaitingConfirmation() {
  imageDetailElement.className = "image-detail empty";
  imageDetailElement.innerHTML = '<div class="placeholder"><strong>等待确认</strong><span>当前操作尚未访问 Mapillary API。</span></div>';
}

function showEmptyImageState() {
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

function escapeHtml(value) {
  return value.replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function escapeAttribute(value) {
  return escapeHtml(String(value));
}

confirmMapillaryButton.addEventListener("click", loadImages);
checkHealth();
initializeMainz();
