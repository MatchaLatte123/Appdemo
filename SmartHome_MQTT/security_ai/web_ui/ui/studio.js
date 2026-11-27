'use strict';

const el = id => document.getElementById(id);
const num = id => Number(el(id).value);
const format = new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 4 });
const percentFormat = new Intl.NumberFormat('vi-VN', { style: 'percent', maximumFractionDigits: 2 });
const fmt = value => value == null ? '—' : typeof value === 'number' ? format.format(value) : String(value);
const pct = value => value == null ? '—' : percentFormat.format(value);
const json = value => JSON.stringify(value, null, 2);
const selected = () => [...el('algorithms').querySelectorAll('input:checked')].map(input => input.value);
const featureSet = () => el('trainingFeatures').value;
const labels = {
  LOGISTIC: 'Logistic Regression', RANDOM_FOREST: 'Random Forest', EXTRA_TREES: 'Extra Trees',
  XGBOOST: 'XGBoost', LIGHTGBM: 'LightGBM', HIST_GRADIENT: 'HistGradientBoosting',
};
const jobLabels = {
  IDLE: 'Sẵn sàng', RUNNING: 'Đang xử lý', COMPLETE: 'Đã hoàn tất', FAILED: 'Job gặp lỗi',
  CANCELLED: 'Đã dừng', TIMEOUT: 'Hết thời gian', MEMORY_LIMIT: 'Vượt giới hạn RAM',
};
const actionLabels = { import: 'Nạp dữ liệu', prepare: 'Tiền xử lý', train: 'Huấn luyện', evaluate: 'Đánh giá Test' };
const comparisonSides = [['CIC_38', 'compare38'], ['CIC_39', 'compare39']];

let options, data = {}, runs = [], selectedReport;
let currentTab = 'dataset', lastJob, locked = false, reportRequest = 0;
let evaluationRunsRequest = 0;
let featureReport, featureRequest = 0;
let comparisonData, comparisonRequest = 0;

function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text != null) item.textContent = text;
  if (className) item.className = className;
  return item;
}

function message(error) {
  el('message').textContent = typeof error === 'string' ? error : error.message;
}

async function api(route, body) {
  const args = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Studio-Token': options.token },
    body: JSON.stringify(body),
  };
  const response = await fetch('/api/' + route, args);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Studio không phản hồi.');
  return result;
}

function table(id, headers, rows) {
  const target = el(id);
  if (!rows.length) {
    target.replaceChildren();
    return;
  }
  const output = document.createElement('table');
  const head = output.createTHead().insertRow();
  for (const title of headers) {
    const th = node('th', title);
    th.scope = 'col';
    head.append(th);
  }
  const body = output.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const value of row) tr.insertCell().textContent = fmt(value);
  }
  target.replaceChildren(output);
}

function facts(id, rows) {
  el(id).replaceChildren(...rows.map(([label, value]) => {
    const group = node('div');
    group.append(node('dt', label), node('dd', value));
    return group;
  }));
}

function navigate() {
  const previous = currentTab;
  const requested = location.hash.slice(1);
  const tabs = [...el('tabs').querySelectorAll('[data-tab]')];
  if (tabs.some(link => link.dataset.tab === requested)) currentTab = requested;
  for (const link of tabs) {
    if (link.dataset.tab === currentTab) {
      link.setAttribute('aria-current', 'page');
      el('crumb').textContent = link.textContent;
    } else {
      link.removeAttribute('aria-current');
    }
  }
  for (const section of document.querySelectorAll('main > section')) section.hidden = section.id !== currentTab;
  if (currentTab !== previous) window.scrollTo(0, 0);
}

function drawFeatures() {
  const contract = featureReport.features[el('featureset').value];
  const title = featureReport.title + ' · ' + contract.order.length + ' đặc trưng';
  if (el('featureset').value === 'DATASET') {
    const scaler = featureReport.scaler;
    el('featureDescription').textContent = title
      + (featureReport.excluded_feature ? ' · Đã loại ' + featureReport.excluded_feature : '')
      + (scaler ? ' · Tham số Z-score tính trên Train trước chuẩn hóa' : '');
    table('featureTable', scaler ? ['STT', 'Đặc trưng', 'Trung bình Train', 'Hệ số chia (scale)'] : ['STT', 'Đặc trưng'],
      contract.order.map((name, index) => scaler
        ? [index + 1, name, scaler[name]?.mean, scaler[name]?.scale]
        : [index + 1, name]));
    return;
  }
  const audit = featureReport.audit || {};
  el('featureDiagnostics').hidden = false;
  el('featureDescription').textContent = title + ' · ' + contract.status;
  table('featureTable', ['Đặc trưng', 'Công thức collector đề xuất', 'Đơn vị', 'P05 / P50 / P95', 'Giá trị phổ biến'],
    contract.order.map(name => {
      const stats = audit.stats?.[name];
      return [name, contract.candidate_formulas[name].formula, contract.candidate_formulas[name].unit,
        stats?.p05_p50_p95.map(fmt).join(' / '), pct(stats?.modal_fraction)];
    }));
  el('correlation').textContent = json({
    scope: audit.scope, missingness: featureReport.quality, correlation: audit.correlation,
    reduction: contract.reduction, compatibility: contract.parity,
  });
}

async function loadFeatureReport() {
  const request = ++featureRequest;
  const preset = el('featureset');
  featureReport = null;
  preset.replaceChildren();
  el('featurePresetField').hidden = true;
  el('featureDiagnostics').hidden = true;
  el('featureTable').replaceChildren();
  el('featureDescription').textContent = 'Đang tải báo cáo…';
  try {
    const report = await api('feature-report?dataset=' + encodeURIComponent(el('featureDataset').value));
    if (request !== featureRequest) return;
    featureReport = report;
    const presets = Object.keys(report.features);
    preset.replaceChildren(...presets.map(name => new Option(name, name)));
    preset.value = report.default;
    el('featurePresetField').hidden = presets.length < 2;
    drawFeatures();
  } catch (error) {
    if (request === featureRequest) el('featureDescription').textContent = error.message;
  }
}

function estimate() {
  const train = data.splits?.TRAIN;
  const cap = num('traincap');
  const validation = data.splits?.VALIDATION?.rows;
  const quick = el('preset').value === 'QUICK_EXPERIMENT';
  const used = train?.labels && cap > 0 ? Object.values(train.labels).reduce((sum, n) => sum + Math.min(cap, n), 0) : train?.scenarios && cap > 0 ? [0, 1].reduce((sum, label) => {
    const available = train.scenarios.filter(item => item.y === label).reduce((total, item) => total + item.rows, 0);
    return sum + Math.min(cap, available);
  }, 0) : train?.rows;
  const matrix = used == null ? null : used * options.features[featureSet()].order.length * 4 / 2 ** 20;
  const usedValidation = validation == null ? null : quick ? Math.min(validation, num('valcap')) : validation;
  facts('estimate', [
    ['Dataset', data.version || '—'],
    ['Thuật toán', fmt(selected().length)],
    ['Bộ đặc trưng', featureSet()],
    ['Train dùng / tổng', fmt(used) + ' / ' + fmt(train?.rows)],
    ['Validation dùng / tổng', fmt(usedValidation) + ' / ' + fmt(validation)],
    ['Test', fmt(data.splits?.TEST?.rows)],
    ['Ma trận đặc trưng', matrix == null ? '—' : fmt(matrix) + ' MiB'],
  ]);
  el('trainingDataset').textContent = data.dataset_id ? 'Manifest ' + data.dataset_id : '';
  el('trainingDataset').title = data.manifest || '';
  el('valcap').disabled = !quick;
  el('autoTest').disabled = quick;
  renderAutoTestHint();
}

function renderAutoTestHint() {
  const lock = data.test_lock;
  const full = el('preset').value === 'FULL_TRAINING';
  el('autoReuseTest').disabled = !full || !el('autoTest').checked;
  let hint = 'Studio chọn model theo Attack Recall, FPR và F1 trên validation.';
  if (!full) {
    hint = 'QUICK tự lưu báo cáo validation. Chọn FULL để chạy Test.';
  } else if (lock?.unavailable) {
    hint = 'Không xác minh được trạng thái Test.';
  } else if (!el('autoTest').checked) {
    hint = 'Studio chấm validation. Bạn có thể chấm Test thủ công trong tab Đánh giá.';
  } else if (lock?.run_id && el('autoReuseTest').checked) {
    hint = 'Studio sẽ chấm model đứng đầu validation trên Test đã dùng và lưu riêng kết quả benchmark.';
  } else if (lock?.run_id) {
    hint = 'Test đã khóa cho run ' + lock.run_id.slice(0, 8) + '. Bật dùng lại Test để benchmark run mới.';
  }
  el('autoTestHint').textContent = hint;
}

function automaticTestMessage(result) {
  const evaluation = result?.automatic_test;
  if (!evaluation) return '';
  const winner = result.best_validation_run.slice(0, 8);
  const lead = 'Đã lưu báo cáo validation. Run được chọn ' + winner + '. ';
  if (evaluation.status === 'TEST_COMPLETE') return lead + 'Đã tự động chấm test.';
  if (evaluation.status === 'TEST_COMPLETE_REUSED') return lead + 'Đã chấm lại Test và lưu benchmark.';
  if (evaluation.status === 'SKIPPED_LOCKED') return lead + 'Tập test đã khóa cho run ' + evaluation.locked_run_id.slice(0, 8) + '.';
  if (evaluation.status === 'TEST_FAILED') return lead + 'Không hoàn tất test. ' + evaluation.reason;
  if (evaluation.status === 'VALIDATION_ONLY_QUICK') return lead + 'QUICK dùng validation.';
  return lead;
}

const parameterDrafts = {};
const selectedLevels = {};
const invalidParameters = {};

function syncParameterJson() {
  el('hp').value = json(Object.fromEntries(selected().map(name => [name, parameterDrafts[name]])));
}

function readParameterJson() {
  const value = JSON.parse(el('hp').value);
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.entries(value).some(([name, params]) => !selected().includes(name)
        || !params || typeof params !== 'object' || Array.isArray(params))) {
    throw new Error('JSON phải chứa cấu hình của các thuật toán đang được chọn.');
  }
  return value;
}

function createParameterField(key, rule, draft, name) {
  const label = node('label', null, 'field parameter-field');
  label.append(node('code', key));
  const field = node(rule.kind === 'choice' ? 'select' : 'input');
  field.dataset.parameter = key;
  const hasOverride = Object.hasOwn(draft, key);
  if (rule.kind === 'choice') {
    field.append(new Option('Mặc định · ' + json(rule.default), ''));
    for (const item of rule.choices) field.append(new Option(String(item), json(item)));
  } else {
    field.type = 'text';
    field.spellcheck = false;
    field.placeholder = 'Mặc định · ' + json(rule.default);
    if (['integer', 'float'].includes(rule.kind)) field.inputMode = 'decimal';
  }
  if (hasOverride) field.value = json(draft[key]);
  if (invalidParameters[name].has(key)) {
    field.value = invalidParameters[name].get(key);
    field.setAttribute('aria-invalid', 'true');
    field.setCustomValidity('Giá trị không hợp lệ.');
  }
  field.addEventListener('change', () => {
    try {
      const text = field.value.trim();
      if (text === '') delete draft[key];
      else {
        const value = JSON.parse(text);
        if (value === null ? rule.default !== null && !rule.nullable && !['max_leaf_nodes', 'validation_fraction'].includes(key)
              && rule.kind !== 'feature_count'
            : ['integer', 'float'].includes(rule.kind) && (typeof value !== 'number'
              || (rule.kind === 'integer' && !Number.isInteger(value)) || value < rule.min || value > rule.max)) {
          throw new Error('Giá trị ngoài phạm vi.');
        }
        draft[key] = value;
      }
      invalidParameters[name].delete(key);
      field.removeAttribute('aria-invalid');
      field.setCustomValidity('');
      syncParameterJson();
      message('');
    } catch {
      invalidParameters[name].set(key, field.value);
      field.setAttribute('aria-invalid', 'true');
      field.setCustomValidity('Giá trị không hợp lệ.');
      message(name + ' · ' + key + ' chưa hợp lệ.');
    }
    controls();
  });
  label.append(field);
  const hint = rule.hint || (key === 'n_jobs' ? 'Để trống để dùng giới hạn CPU của lượt chạy.'
    : key === 'random_state' ? 'Để trống để dùng seed chung.'
    : ['integer', 'float'].includes(rule.kind) ? fmt(rule.min) + ' – ' + fmt(rule.max) : '');
  if (hint) label.append(node('small', hint, 'parameter-hint'));
  return label;
}

function renderParameters() {
  const name = el('parameterAlgorithm').value;
  if (!name) return;
  const schema = options.parameter_schema?.[name] || {};
  const term = el('parameterSearch').value.trim().toLowerCase();
  const fields = Object.entries(schema).filter(([key]) => key.toLowerCase().includes(term));
  el('level').value = selectedLevels[name];
  el('parameterFields').replaceChildren(...fields.map(([key, rule]) =>
    createParameterField(key, rule, parameterDrafts[name], name)));
  if (!fields.length) {
    const notice = Object.keys(schema).length ? 'Không tìm thấy tham số khớp.'
      : 'Phiên bản server đang chạy chưa cung cấp danh sách tham số. Có thể dùng JSON của preset.';
    el('parameterFields').append(node('p', notice, 'parameter-hint'));
  }
  el('managedParameters').textContent = (options.managed_parameters?.[name] || []).join(' · ');
  controls();
}

function applyParameterJson() {
  const value = readParameterJson();
  for (const [name, params] of Object.entries(value)) {
    parameterDrafts[name] = { ...params };
    invalidParameters[name].clear();
  }
  syncParameterJson();
  renderParameters();
}

function dataset() {
  const prepared = data.status === 'PREPARED';
  const cards = [
    ['Tệp CSV', data.files, data.bytes ? fmt(data.bytes / 2 ** 30) + ' GiB dữ liệu nguồn' : 'Nguồn local'],
    ['Dòng đủ điều kiện', data.eligible, 'Trước kiểm tra trùng lặp'],
    ['Dòng sẵn sàng', data.total_rows, 'Sau lọc giữa các tập'],
    ['Nhóm giao nhau', data.group_overlap, data.group_policy === 'EXPLORATORY_FILE_GROUP_SPLIT' ? 'Theo nhóm CSV' : 'Theo nhóm nguồn'],
  ];
  el('datasetSummary').replaceChildren(...cards.map(([label, value, note]) => {
    const card = node('div', null, 'metric');
    card.append(node('span', label), node('strong', fmt(value)), node('small', note));
    return card;
  }));

  const splits = ['TRAIN', 'VALIDATION', 'TEST'].filter(key => data.splits?.[key]);
  const track = node('div', null, 'split-track');
  track.setAttribute('aria-hidden', 'true');
  const legend = node('div', null, 'split-legend');
  for (const key of splits) {
    const fraction = data.splits[key].fraction;
    const part = node('i');
    part.style.flexGrow = fraction;
    track.append(part);
    legend.append(node('span', key + ' ' + pct(fraction)));
  }
  el('splitBar').replaceChildren(track, legend);
  const labels = data.label_mapping ? Object.keys(data.label_mapping) : ['Normal', 'Attack'];
  table('splitRows', ['Tập', 'Số dòng', 'Tỷ lệ', ...labels], splits.map(key => {
    const split = data.splits[key];
    if (split.labels) return [key, split.rows, pct(split.fraction), ...labels.map(label => split.labels[label])];
    const normal = split.scenarios.filter(item => item.y === 0).reduce((sum, item) => sum + item.rows, 0);
    return [key, split.rows, pct(split.fraction), normal, split.rows - normal];
  }));
  facts('integrity', [
    ['Nhóm giao nhau', fmt(data.group_overlap)],
    ['Chữ ký trùng sau lọc', fmt(data.dedup?.cross_split_signatures_after)],
    ['Dòng CSV lỗi cấu trúc', fmt(data.malformed)],
    ['Phương pháp chia', data.group_policy === 'EXPLORATORY_FILE_GROUP_SPLIT' ? 'Thăm dò theo tệp CSV' : fmt(data.group_policy)],
  ]);
  el('splitStatus').textContent = ({
    PREPARED: data.training_supported === false ? 'Đã nạp · Chế độ xem dữ liệu đa lớp' : 'Sẵn sàng', INVENTORIED: 'Đã kiểm kê', DATASET_NOT_FOUND: 'Không tìm thấy nguồn',
    UNPREPARED: 'Chưa chuẩn bị',
  })[data.status] || fmt(data.status);
  el('toTraining').hidden = !prepared || data.training_supported === false;
  estimate();
}

function runSelector(id, models, preferred) {
  const field = el(id), old = preferred || field.value;
  field.replaceChildren(...models.map(run => new Option(
    (labels[run.algorithm] || run.algorithm) + ' · ' + run.run_id.slice(0, 8), run.run_id,
  )));
  if (!models.length) field.append(new Option('Chưa có lượt chạy', ''));
  if (models.some(run => run.run_id === old)) field.value = old;
  field.disabled = !models.length;
}

function evaluationQuery() {
  return '?dataset=' + encodeURIComponent(el('evaluationDatasetPicker').value);
}

async function loadEvaluationRuns(preferredRun) {
  const request = ++evaluationRunsRequest;
  const field = el('evalRun'), previous = field.value;
  field.disabled = true;
  field.replaceChildren(new Option('Đang tải…', ''));
  await evaluation();
  try {
    const index = await api('registry' + evaluationQuery());
    if (request !== evaluationRunsRequest) return;
    runSelector('evalRun', index.runs, preferredRun || previous);
    await evaluation();
  } catch (error) {
    if (request === evaluationRunsRequest) {
      if (!field.value) field.replaceChildren(new Option('Không tải được lượt chạy', ''));
      message(error);
    }
  }
}

function renderRegistry() {
  el('registryEmpty').hidden = !!runs.length;
  table('registryRows', ['Run', 'Thuật toán', 'Preset', 'Scaler', 'Ngưỡng', 'Dataset', 'SHA-256', 'Test'],
    runs.map(run => [run.run_id, labels[run.algorithm] || run.algorithm, run.run_type, run.scaling,
      run.threshold, run.dataset_version ? run.dataset_version + ' · ' + run.dataset_id : run.dataset_id,
      run.model_sha256, run.test_repeat_count
      ? 'Dùng lại ×' + fmt(run.test_repeat_count) : run.test ? 'Đã đánh giá' : 'Chờ']));
}

async function loadComparison() {
  const request = ++comparisonRequest;
  el('comparisonStatus').textContent = 'Đang tải các lượt huấn luyện…';
  try {
    const result = await api('cic-comparison');
    if (request !== comparisonRequest) return;
    comparisonData = result;
    for (const [title, id] of comparisonSides) {
      const field = el(id), old = field.value, models = result.datasets[title];
      field.replaceChildren(...models.map(run => new Option(
        labels[run.algorithm] + ' · ' + run.run_id.slice(0, 8) + (run.test ? '' : ' · Chưa có Test'), run.run_id)));
      field.disabled = !models.length;
      if (!models.length) field.append(new Option('Chưa có model', ''));
      field.value = models.some(run => run.run_id === old) ? old
        : (models.find(run => run.test) || models[0])?.run_id || '';
    }
    drawComparison();
  } catch (error) {
    if (request !== comparisonRequest) return;
    comparisonData = null;
    el('comparisonContent').hidden = true;
    el('comparisonStatus').textContent = error.message;
  }
}

function drawComparison() {
  if (!comparisonData) return;
  const split = el('compareSplit').value;
  const models = comparisonSides.map(([title, id]) =>
    comparisonData.datasets[title].find(run => run.run_id === el(id).value));
  const results = models.map(run => run?.[split]);
  const hasModels = models.some(Boolean);
  el('comparisonContent').hidden = !hasModels;
  el('comparisonStatus').textContent = hasModels
    ? (comparisonData.paired_test
      ? 'Theo manifest, hai dataset dùng cùng các mẫu và cách chia tập. CIC_39 thêm đặc trưng Number.'
      : 'Chưa xác nhận hai dataset dùng cùng các mẫu đánh giá.')
    : 'Chưa có lượt huấn luyện cho CIC_38 hoặc CIC_39.';
  if (!hasModels) return;

  const aggregate = [['accuracy', 'Accuracy'], ['macro_f1', 'Macro F1'], ['weighted_f1', 'Weighted F1']];
  const delta = (a, b) => a == null || b == null ? '—'
    : new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 2, signDisplay: 'exceptZero' }).format((b - a) * 100);
  table('comparisonRows', ['Chỉ số', 'CIC_38', 'CIC_39', '39 − 38 (điểm %)'], [
    ['Thuật toán', ...models.map(run => labels[run?.algorithm])],
    ['Số đặc trưng', ...models.map(run => run?.feature_count)],
    ['Kết quả', ...models.map(run => !run ? 'Chưa có model' : split === 'validation' ? 'Validation'
      : run.test_source === 'reused' ? 'Test chấm lại' : run.test ? 'Test lần đầu' : 'Chưa chấm Test')],
    ['Số mẫu đánh giá', ...results.map(metrics => metrics?.rows)],
    ...aggregate.map(([key, label]) => [label, ...results.map(metrics => pct(metrics?.[key])),
      delta(results[0]?.[key], results[1]?.[key])]),
    ['Thời gian train (s)', ...models.map(run => run?.training_seconds)],
    ['Dung lượng model (MiB)', ...models.map(run => run ? run.model_bytes / 2 ** 20 : null)],
  ]);

  const classMetrics = results.map(result => new Map((result?.per_class || []).map(row => [row.label, row])));
  const classes = new Set(classMetrics.flatMap(rows => [...rows.keys()]));
  const measure = ['precision', 'recall', 'f1'];
  table('comparisonClassRows', ['Lớp', ...comparisonSides.flatMap(([title]) =>
    ['Precision', 'Recall', 'F1'].map(metric => title + ' ' + metric))], [...classes].map(label => [
    label, ...classMetrics.flatMap(rows => measure.map(metric => pct(rows.get(label)?.[metric]))),
  ]));
  renderConfusion('comparisonMatrix38', results[0]);
  renderConfusion('comparisonMatrix39', results[1]);
}

function plot(id, points, xLabel, yLabel) {
  const canvas = el(id), ctx = canvas.getContext('2d');
  const { width, height } = canvas;
  const left = 54, right = width - 16, top = 14, bottom = height - 40;
  ctx.clearRect(0, 0, width, height);
  ctx.font = '12px Segoe UI';
  ctx.fillStyle = '#536580';
  ctx.strokeStyle = '#e0e8f2';
  for (let i = 0; i <= 4; i++) {
    const x = left + i * (right - left) / 4, y = bottom - i * (bottom - top) / 4;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.stroke();
    ctx.fillText(fmt(i / 4), x - 6, bottom + 17);
    ctx.fillText(fmt(i / 4), left - 30, y + 4);
  }
  ctx.fillText(xLabel, (left + right - ctx.measureText(xLabel).width) / 2, height - 5);
  ctx.save();
  ctx.translate(12, (top + bottom + ctx.measureText(yLabel).width) / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(yLabel, 0, 0);
  ctx.restore();
  if (!points?.length) return;
  ctx.strokeStyle = '#2356a9';
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach(([x, y], index) => {
    const px = left + x * (right - left), py = bottom - y * (bottom - top);
    if (index) ctx.lineTo(px, py);
    else ctx.moveTo(px, py);
  });
  ctx.stroke();
  ctx.lineWidth = 1;
}

function renderTest(test, repeated) {
  const exists = !!test?.per_class;
  el('testHeading').textContent = repeated ? 'Tập Test · lần chấm lại gần nhất' : 'Kết quả tập Test';
  el('testMissing').hidden = exists;
  el('testMetrics').hidden = !exists;
  if (!exists) return;
  el('testSummary').replaceChildren(...[['Macro F1', test.macro_f1], ['Weighted F1', test.weighted_f1]]
    .map(([title, score]) => {
      const card = node('div', null, 'metric');
      card.append(node('span', title), node('strong', pct(score)));
      const bar = node('progress');
      bar.max = 1; bar.value = score; bar.setAttribute('aria-label', title);
      card.append(bar);
      return card;
    }));
  table('testClassRows', ['Lớp', 'Precision', 'Recall', 'F1-score', 'Số mẫu'],
    test.per_class.map(row => [row.label, pct(row.precision), pct(row.recall), pct(row.f1), row.support]));
  el('testClassChart').replaceChildren(...test.per_class.map(row => {
    const group = node('div', null, 'class-chart-row');
    group.append(node('strong', row.label));
    for (const [key, title] of [['precision','Precision'],['recall','Recall'],['f1','F1-score']]) {
      const line = node('div', null, 'class-chart-line');
      const meter = node('progress');
      meter.max = 1; meter.value = row[key]; meter.className = 'bar-' + key;
      meter.setAttribute('aria-label', row.label + ' · ' + title);
      line.append(node('span', title), meter, node('span', pct(row[key])));
      group.append(line);
    }
    return group;
  }));
  renderConfusion('testConfusion', test);
}

function renderConfusion(id, test) {
  if (!test?.confusion_matrix?.length || !test.confusion_labels?.length) {
    el(id).replaceChildren(node('p', 'Chưa có ma trận nhầm lẫn.', 'parameter-hint'));
    return;
  }
  table(id, ['Thật / Dự đoán', ...test.confusion_labels], test.confusion_matrix.map((row, index) =>
    [test.confusion_labels[index], ...row]));
  [...el(id).querySelectorAll('tbody tr')].forEach((row, index) => {
    const values = test.confusion_matrix[index];
    const total = values.reduce((sum, n) => sum + n, 0);
    Array.from(row.cells).slice(1).forEach((cell, col) => {
      const ratio = total ? values[col] / total : 0;
      cell.style.backgroundColor = index === col ? 'rgba(37, 111, 106,' + ratio*.32 + ')' : 'rgba(195, 78, 44,' + ratio*.45 + ')';
      cell.title = pct(ratio) + ' trong lớp ' + test.confusion_labels[index];
      if (index === col) cell.classList.add('matrix-diagonal');
    });
  });
}

async function evaluation() {
  const id = el('evalRun').value, request = ++reportRequest;
  selectedReport = null;
  el('evaluationEmpty').hidden = !!id;
  el('evaluationContent').hidden = true;
  el('evaluationTestStatus').textContent = '';
  el('evaluationDataset').textContent = '';
  controls();
  if (!id) return;
  const run = await api('run/' + id + evaluationQuery());
  if (request !== reportRequest) return;
  selectedReport = run;
  const dataset = run.dataset;
  if (dataset) {
    el('evaluationDataset').textContent = 'Dataset ' + dataset.version + ' · Manifest ' + dataset.id
      + ' · Test ' + fmt(dataset.splits?.TEST?.rows) + ' mẫu';
  }
  renderEvaluationTestStatus();
  const results = [
    ['Test lần đầu', run.test],
    ['Test dùng lại gần nhất', run.repeated_test?.metrics],
  ].filter(([, value]) => value);
  const metrics = [
    ['accuracy', 'Accuracy'], ['macro_f1', 'Macro F1'], ['weighted_f1', 'Weighted F1'],
    ['attack_recall', 'Attack Recall'], ['specificity', 'Specificity'], ['fpr', 'FPR'],
    ['f1', 'F1'], ['roc_auc', 'ROC-AUC'], ['pr_auc', 'PR-AUC'],
  ].filter(([key]) => results.some(([, value]) => value[key] != null));
  table('evalTables', ['Lần chấm', 'Số mẫu', ...metrics.map(([, label]) => label)], results.map(([name, value]) =>
    [name, value.rows, ...metrics.map(([key]) => pct(value[key]))]));
  el('testRepeatDetails').hidden = !run.test_repeat_count;
  table('testRepeatHistory', ['Thời điểm', 'Macro F1', 'Attack Recall', 'FPR', 'Ngưỡng'],
    (run.test_repeat_history || []).map(attempt => [
      new Date(attempt.evaluated_utc).toLocaleString('vi-VN'), pct(attempt.macro_f1), pct(attempt.attack_recall), pct(attempt.fpr), attempt.threshold,
    ]));
  const test = run.repeated_test?.metrics || run.test;
  renderTest(test, !!run.repeated_test);
  const curves = test?.curves;
  el('binaryCurves').hidden = !curves;
  if (curves) {
    plot('pr', curves.pr, 'Recall', 'Precision');
    plot('roc', curves.roc, 'False positive rate', 'True positive rate');
  }
  const details = {fn: test?.fn, fp: test?.fp, thresholds: test?.thresholds,
    scenarios: test?.scenarios, groups: test?.groups};
  el('testDetails').hidden = !Object.values(details).some(value => value != null);
  el('evalDetail').textContent = json(details);
  el('evaluationContent').hidden = false;
  controls();
}

function syncDatasetOptions(next) {
  const training = el('trainingFeatures');
  const changed = options?.workspace !== next.workspace || !training.options.length;
  options = next;
  if (!changed) return;
  training.replaceChildren(...Object.keys(options.features).map(name => new Option(name, name)));
  training.value = options.defaults.features;
  el('scaling').value = options.defaults.scaling;
  el('preset').value = options.defaults.preset;
}

async function reload(preferredRun) {
  const [datasetData, index, nextOptions, catalog] = await Promise.all([api('dataset'), api('registry'), api('options'), api('datasets')]);
  data = datasetData; runs = index.runs;
  syncDatasetOptions(nextOptions);
  el('datasetPicker').replaceChildren(...catalog.datasets.map(row => new Option(row.title, row.path)));
  el('datasetPicker').value = catalog.active;
  const featureDataset = el('featureDataset');
  const previous = featureDataset.value;
  featureDataset.replaceChildren(...catalog.datasets.map(row => new Option(row.title, row.path)));
  featureDataset.value = catalog.datasets.some(row => row.path === previous) ? previous : catalog.active;
  const evaluationPicker = el('evaluationDatasetPicker');
  const previousEvaluation = evaluationPicker.value;
  evaluationPicker.replaceChildren(...catalog.datasets.map(row => new Option(row.title, row.path)));
  evaluationPicker.value = !preferredRun && catalog.datasets.some(row => row.path === previousEvaluation)
    ? previousEvaluation : catalog.active;
  if (evaluationPicker.value !== previousEvaluation) el('manualReuseTest').checked = false;
  el('compatibilityNote').textContent = data.label_mapping ? 'Dataset: ' + (data.title || data.version)
    : 'Benchmark only · Collector MQTT và Video chưa được xác minh tương thích với dữ liệu huấn luyện.';

  dataset();
  runSelector('exportRun', runs);
  renderRegistry();
  await Promise.all([loadEvaluationRuns(preferredRun), loadFeatureReport(), loadComparison()]);
}

function renderEvaluationTestStatus() {
  const run = selectedReport;
  const lockedRun = run?.test_lock?.run_id;
  let text = '';
  if (run?.repeated_test) {
    text = 'Đã dùng lại Test ' + fmt(run.test_repeat_count) + ' lần. Đây là kết quả benchmark.';
  } else if (run?.run_type === 'EXPERIMENTAL') {
    text = 'Lượt huấn luyện QUICK không hỗ trợ chấm Test.';
  } else if (run?.test && run?.automatic_test?.status === 'TEST_COMPLETE') {
    text = 'Đã tự động chấm Test sau khi huấn luyện.';
  } else if (run && lockedRun) {
    text = 'Lần chấm đầu thuộc run ' + lockedRun.slice(0, 8) + '.';
  }
  el('evaluationTestStatus').textContent = text;
}

function controls() {
  const running = lastJob?.state === 'RUNNING';
  const blocked = locked || running;
  el('datasetPicker').disabled = blocked;
  el('loadDataset').disabled = blocked;
  el('fpr').disabled = !!data.label_mapping;
  el('train').disabled = blocked || data.training_supported === false || data.status !== 'PREPARED' || !selected().length;
  const lock = selectedReport?.test_lock;
  const canReuse = !!lock?.run_id && el('manualReuseTest').checked;
  el('manualReuseTest').disabled = blocked || !lock?.run_id || !selectedReport || selectedReport.run_type === 'EXPERIMENTAL';
  const testLockedElsewhere = lock?.run_id && lock.run_id !== el('evalRun').value;
  el('evaluate').disabled = blocked || !selectedReport || lock?.unavailable || selectedReport.run_type === 'EXPERIMENTAL'
    || (!canReuse && (testLockedElsewhere || !!selectedReport.test));
  el('evaluate').textContent = canReuse ? 'Chấm lại Test' : 'Khóa run và đánh giá Test';
  el('export').disabled = locked || !el('exportRun').value;
  for (const button of document.querySelectorAll('.cancel')) button.disabled = locked || !running;
}

function renderJob(job) {
  const action = actionLabels[job.action] || job.action;
  const label = jobLabels[job.state] || job.state;
  el('jobStatus').dataset.state = job.state;
  el('jobStatus').textContent = action && job.state === 'RUNNING' ? action + ' đang chạy' : label;
  el('job').textContent = 'Nhật ký xử lý · ' + label + (action ? ' · ' + action : '')
    + (job.peak_rss_mib ? ' · RAM ' + fmt(job.peak_rss_mib) + ' MiB' : '');
  const log = el('logs');
  const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 32;
  log.textContent = job.logs.join('\n') || 'Chưa có nhật ký.';
  if (follow) log.scrollTop = log.scrollHeight;
}

async function poll() {
  try {
    const job = await api('job');
    const changed = job.id !== lastJob?.id || job.state !== lastJob?.state;
    lastJob = job;
    renderJob(job);
    controls();
    if (changed && job.state !== 'RUNNING') {
      const winner = job.state === 'COMPLETE' && job.action === 'train' ? job.result?.best_validation_run : null;
      const outcome = automaticTestMessage(job.result);
      el('trainingOutcome').textContent = outcome;
      el('trainingOutcome').hidden = !outcome;
      await reload(winner);
      if (winner && currentTab === 'training') location.hash = 'evaluation';
      controls();
    }
  } catch (error) {
    message(error);
  }
  setTimeout(poll, 1400);
}

function revealLog() {
  const activity = el('activity');
  activity.querySelector('details').open = true;
  activity.scrollIntoView({ block: 'nearest' });
}

function bind(id, action) {
  el(id).addEventListener('click', async () => {
    locked = true;
    controls();
    message('');
    try {
      const result = await action();
      if (result?.state === 'RUNNING') {
        lastJob = { ...result, logs: [] };
        renderJob(lastJob);
        revealLog();
      }
    } catch (error) {
      message(error);
      el('message').scrollIntoView({ block: 'nearest' });
    } finally {
      locked = false;
      controls();
    }
  });
}

async function start() {
  syncDatasetOptions(await api('options'));
  for (const name of options.algorithms) {
    selectedLevels[name] = 'quick';
    invalidParameters[name] = new Map();
    parameterDrafts[name] = { ...options.presets[name].quick };
    el('parameterAlgorithm').append(new Option(labels[name] || name, name));
    const label = node('label', null, 'algorithm'), input = node('input');
    input.type = 'checkbox';
    input.value = name;
    input.checked = name === 'LOGISTIC';
    input.addEventListener('change', () => {
      if (input.checked) {
        el('parameterAlgorithm').value = name;
        renderParameters();
      }
      syncParameterJson();
      optionsChanged();
    });
    label.append(input, node('span', labels[name] || name));
    el('algorithms').append(label);
  }
  function optionsChanged() {
    el('algorithmCount').textContent = fmt(selected().length) + ' đã chọn';
    estimate();
    controls();
  }
  el('featureDataset').addEventListener('change', loadFeatureReport);
  el('featureset').addEventListener('change', drawFeatures);
  el('trainingFeatures').addEventListener('change', estimate);
  for (const id of ['traincap', 'valcap', 'preset']) el(id).addEventListener('input', estimate);
  el('autoTest').addEventListener('change', renderAutoTestHint);
  el('autoReuseTest').addEventListener('change', renderAutoTestHint);
  el('manualReuseTest').addEventListener('change', controls);
  for (const id of ['compare38', 'compare39', 'compareSplit']) el(id).addEventListener('change', drawComparison);
  el('parameterAlgorithm').addEventListener('change', () => {
    el('parameterSearch').value = '';
    renderParameters();
  });
  el('parameterSearch').addEventListener('input', renderParameters);
  el('applyPreset').addEventListener('click', () => {
    const name = el('parameterAlgorithm').value;
    selectedLevels[name] = el('level').value;
    invalidParameters[name].clear();
    parameterDrafts[name] = { ...options.presets[name][selectedLevels[name]] };
    syncParameterJson();
    renderParameters();
  });
  el('applyParameterJson').addEventListener('click', () => {
    try {
      applyParameterJson();
      message('');
    } catch (error) {
      message(error);
    }
  });
  el('evalRun').addEventListener('change', () => evaluation().catch(message));
  el('evaluationDatasetPicker').addEventListener('change', () => {
    el('manualReuseTest').checked = false;
    loadEvaluationRuns();
  });
  el('showLog').addEventListener('click', revealLog);
  window.addEventListener('hashchange', navigate);

  bind('loadDataset', async () => {
    const splitPaths = {TRAIN: el('splitTrain').value.trim(), VALIDATION: el('splitValidation').value.trim(),
      TEST: el('splitTest').value.trim()};
    const result = await api('import', {path: el('datasetPath').value, target: el('datasetTarget').value,
      features: el('datasetFeatures').value,
      ...(Object.values(splitPaths).some(Boolean) ? {split_paths: splitPaths} : {})});
    if (result.workspace) await reload();
    return result;
  });
  el('datasetPicker').addEventListener('change', async () => {
    locked = true;
    controls();
    try {
      await api('select-dataset', {path: el('datasetPicker').value});
      message('');
      await reload();
    } catch (error) { message(error); }
    finally { locked = false; controls(); }
  });
  bind('train', () => {
    const invalid = selected().find(name => invalidParameters[name].size);
    if (invalid) throw new Error('Kiểm tra lại siêu tham số của ' + labels[invalid] + '.');
    return api('train', {
      algorithms: selected(), features: featureSet(), preset: el('preset').value, level: 'quick',
      scaling: el('scaling').value, seed: num('seed'), threads: num('threads'), memory_mib: num('memory'),
      timeout_seconds: num('timeout'), train_cap_per_class: num('traincap'), validation_cap: num('valcap'),
      normal_fpr_limit: num('fpr'), balanced: el('balanced').checked, auto_test: el('autoTest').checked, reuse_test: el('autoReuseTest').checked,
      hyperparameters: readParameterJson(), dataset_manifest_sha256: data.dataset_manifest_sha256,
    });
  });
  bind('evaluate', () => {
    const reuse = !!selectedReport?.test_lock?.run_id && el('manualReuseTest').checked;
    const prompt = reuse ? 'Chấm trên tập Test đã dùng và lưu kết quả benchmark riêng?'
      : 'Khóa tập test cho lượt chạy đã chọn?';
    if (confirm(prompt)) return api('evaluate', { run_id: el('evalRun').value, reuse_test: reuse,
      dataset: el('evaluationDatasetPicker').value });
  });
  bind('export', async () => {
    const result = await api('export', { run_id: el('exportRun').value });
    el('exportPath').textContent = result.status + ' · ' + result.path;
    el('exportPath').hidden = false;
  });
  document.querySelector('.cancel').addEventListener('click', async () => {
    try {
      const result = await api('cancel', {});
      lastJob = { ...lastJob, ...result };
      renderJob(lastJob);
      controls();
    } catch (error) {
      message(error);
    }
  });

  navigate();
  await reload();
  syncParameterJson();
  renderParameters();
  optionsChanged();
  poll();
}

start().catch(message);
