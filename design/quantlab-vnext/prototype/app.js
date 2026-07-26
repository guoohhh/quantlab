const pageMeta = {
  today: { space: "投资空间", label: "今日" },
  discover: { space: "投资空间", label: "市场与发现" },
  researchCenter: { space: "投资空间", label: "研究台" },
  research: { space: "研究台", label: "标的研究" },
  portfolio: { space: "投资空间", label: "组合" },
  journal: { space: "投资空间", label: "复盘" },
  help: { space: "支持", label: "帮助中心" },
  lab: { space: "专业空间", label: "研究与审计" },
};

const app = {
  page: "today",
  orderStep: 1,
  orderBlocked: false,
};

const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
const narrowQuery = window.matchMedia("(max-width: 760px)");
const precisionPointerQuery = window.matchMedia("(hover: hover) and (pointer: fine)");

function motionAllowed() {
  return !motionQuery.matches && !navigator.connection?.saveData;
}

function scrollBehavior() {
  return motionAllowed() ? "smooth" : "auto";
}

const stateSelector = document.querySelector("#stateSelector");
const stateSurface = document.querySelector("#stateSurface");
const mainContent = document.querySelector("#mainContent");
const degradationBanner = document.querySelector("#degradationBanner");
const errorBanner = document.querySelector("#errorBanner");
const backgroundJobCard = document.querySelector("#backgroundJobCard");
const scrim = document.querySelector("#scrim");
const activityDrawer = document.querySelector("#activityDrawer");
const chatDrawer = document.querySelector("#chatDrawer");
const orderSheet = document.querySelector("#orderSheet");
const commandPalette = document.querySelector("#commandPalette");
const toast = document.querySelector("#toast");
const ambientField = document.querySelector("#ambientField");
const coordinateReadout = document.querySelector(".top-coordinate span");

const pageCoordinates = {
  today: "01 / TODAY",
  discover: "02 / MARKET",
  researchCenter: "03 / RESEARCH DESK",
  research: "R-07 / EVIDENCE",
  portfolio: "04 / PORTFOLIO",
  journal: "05 / REVIEW",
  help: "H / HANDBOOK",
  lab: "L / AUDIT",
};

const orderFixture = {
  price: 1482.32,
  availableCash: 2078038.45,
  totalAssets: 6574806.45,
  currentPositionValue: 1630552,
  currentPositionPct: 24.8,
  currentShares: 1100,
  buyFeeRate: 0.00025,
  sellFeeRate: 0.00075,
};

function formatCurrency(value) {
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value).replace("CN¥", "¥");
}

function updateCoordinate() {
  if (!coordinateReadout) return;
  coordinateReadout.textContent = `${pageCoordinates[app.page]} · DEMO 08:20`;
}

let ambientContext = null;
let ambientFrame = 0;
let ambientLastPaint = 0;
let ambientWidth = 0;
let ambientHeight = 0;

function ambientAllowed() {
  return Boolean(ambientField) && motionAllowed() && !narrowQuery.matches;
}

function resizeAmbientField() {
  if (!ambientField || !ambientAllowed()) return;
  const bounds = ambientField.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
  ambientWidth = Math.max(1, Math.round(bounds.width));
  ambientHeight = Math.max(1, Math.round(bounds.height));
  ambientField.width = Math.round(ambientWidth * dpr);
  ambientField.height = Math.round(ambientHeight * dpr);
  ambientContext = ambientField.getContext("2d", { alpha: true });
  ambientContext.setTransform(dpr, 0, 0, dpr, 0, 0);
  drawAmbientField(performance.now());
}

function drawAmbientField(timestamp = 0) {
  if (!ambientContext || !ambientAllowed() || !ambientWidth || !ambientHeight) return;
  const dark = ambientField.classList.contains("is-dark");
  const phase = timestamp * 0.00009;
  const colors = dark
    ? ["rgba(221,229,235,.18)", "rgba(203,218,210,.15)", "rgba(228,212,203,.14)"]
    : ["rgba(66,91,116,.17)", "rgba(55,106,89,.13)", "rgba(107,100,136,.12)"];

  ambientContext.clearRect(0, 0, ambientWidth, ambientHeight);
  ambientContext.globalCompositeOperation = dark ? "screen" : "multiply";
  ambientContext.lineWidth = dark ? 0.7 : 0.65;

  for (let index = 0; index < 6; index += 1) {
    const baseY = ambientHeight * (0.1 + index * 0.155);
    const amplitude = 18 + index * 4.5;
    const gapStart = ambientWidth * (0.46 + (index % 3) * 0.055);
    const gapEnd = gapStart + 34 + index * 2;
    let drawing = false;

    ambientContext.beginPath();
    for (let x = -36; x <= ambientWidth + 36; x += 18) {
      const y = baseY
        + Math.sin(x * 0.0042 + index * 0.74 + phase) * amplitude
        + Math.sin(x * 0.0105 + index * 1.21 - phase * 0.55) * amplitude * 0.28;
      const inGap = index === 2 && x > gapStart && x < gapEnd;
      if (inGap) {
        drawing = false;
      } else if (!drawing) {
        ambientContext.moveTo(x, y);
        drawing = true;
      } else {
        ambientContext.lineTo(x, y);
      }
    }
    ambientContext.strokeStyle = colors[index % colors.length];
    ambientContext.stroke();
  }
}

function ambientLoop(timestamp) {
  if (!ambientAllowed() || document.hidden) {
    ambientFrame = 0;
    return;
  }
  if (timestamp - ambientLastPaint >= 66) {
    ambientLastPaint = timestamp;
    drawAmbientField(timestamp);
  }
  ambientFrame = window.requestAnimationFrame(ambientLoop);
}

function startAmbientField() {
  if (!ambientAllowed()) {
    stopAmbientField();
    if (ambientContext) ambientContext.clearRect(0, 0, ambientWidth, ambientHeight);
    return;
  }
  resizeAmbientField();
  if (!ambientFrame && !document.hidden) ambientFrame = window.requestAnimationFrame(ambientLoop);
}

function stopAmbientField() {
  if (ambientFrame) window.cancelAnimationFrame(ambientFrame);
  ambientFrame = 0;
}

function syncAmbientMode() {
  if (!ambientField) return;
  ambientField.classList.toggle("is-dark", app.page === "lab");
  drawAmbientField(performance.now());
}

function icon(id) {
  return `<svg aria-hidden="true"><use href="#${id}"/></svg>`;
}

function closeLayers() {
  [activityDrawer, chatDrawer, orderSheet].forEach((element) => {
    element.classList.remove("open");
    element.setAttribute("aria-hidden", "true");
  });
  commandPalette.classList.remove("open");
  commandPalette.setAttribute("aria-hidden", "true");
  scrim.classList.remove("visible");
}

function updateNav(page) {
  const navPage = page === "research" ? "researchCenter" : page;
  document.querySelectorAll(".nav-item[data-target], .mobile-nav [data-target]").forEach((button) => {
    button.classList.toggle("active", button.dataset.target === navPage);
  });
}

function setPage(page, options = {}) {
  if (!pageMeta[page]) return;
  closeLayers();
  const previousPage = app.page;
  const commitPage = () => {
    app.page = page;
    document.querySelectorAll(".page-view").forEach((section) => {
      section.classList.toggle("active", section.dataset.page === page);
    });
    updateNav(page);
    document.querySelector("#spaceLabel").textContent = pageMeta[page].space;
    document.querySelector("#pageLabel").textContent = pageMeta[page].label;
    document.querySelector(".workspace").classList.toggle("lab-active", page === "lab");
    renderState(stateSelector.value);
    updateCoordinate();
    syncAmbientMode();
  };
  const resetScroll = () => {
    if (!options.keepScroll) window.scrollTo({ top: 0, behavior: scrollBehavior() });
  };

  if (previousPage !== page && motionAllowed() && document.startViewTransition) {
    try {
      const transition = document.startViewTransition(commitPage);
      transition.ready.then(resetScroll).catch(resetScroll);
      return;
    } catch {
      commitPage();
    }
  } else {
    commitPage();
  }
  resetScroll();
}

function setResearchAsset(name, symbol) {
  if (symbol !== "600519") {
    closeLayers();
    showToast("该标的未接入完整演示报告", `${name} 不会复用贵州茅台的证据与订单上下文。`, "warning");
    return false;
  }
  document.querySelector("#researchAssetName").textContent = name;
  document.querySelector("#researchAssetCode").textContent = `${symbol}.SH`;
  document.querySelectorAll(".chat-context strong").forEach((node) => {
    node.textContent = `${name} · 报告 R-07`;
  });
  return true;
}

document.addEventListener("click", (event) => {
  const targetButton = event.target.closest("[data-target]");
  if (targetButton) {
    event.preventDefault();
    const currentState = stateSelector.value;
    if (targetButton.closest("#stateSurface")) {
      stateSelector.value = "normal";
    } else if (currentState === "loading" && targetButton.dataset.target !== app.page) {
      stateSelector.value = "background";
    } else if (["empty", "error"].includes(currentState) && targetButton.dataset.target !== app.page) {
      stateSelector.value = "normal";
    }
    setPage(targetButton.dataset.target);
    return;
  }

  const asset = event.target.closest(".asset-link");
  if (asset) {
    event.preventDefault();
    if (setResearchAsset(asset.dataset.name || "贵州茅台", asset.dataset.symbol || "600519")) {
      setPage("research");
    }
  }
});

document.addEventListener("click", (event) => {
  const scrollButton = event.target.closest("[data-scroll-target]");
  if (!scrollButton) return;
  const target = document.querySelector(`#${scrollButton.dataset.scrollTarget}`);
  if (!target) return;
  event.preventDefault();
  const group = scrollButton.closest("nav");
  group?.querySelectorAll("[data-scroll-target]").forEach((button) => {
    button.classList.toggle("active", button === scrollButton);
  });
  target.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
});

function openDrawer(drawer) {
  closeLayers();
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  scrim.classList.add("visible");
}

function setDrawerTab(group) {
  document.querySelectorAll("[data-drawer-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.drawerTab === group);
  });
  document.querySelectorAll("[data-drawer-group]").forEach((item) => {
    item.hidden = item.dataset.drawerGroup !== group;
  });
}

document.querySelector("#activityTrigger").addEventListener("click", () => { setDrawerTab("pending"); openDrawer(activityDrawer); });
document.querySelector("#activityInline").addEventListener("click", () => { setDrawerTab("pending"); openDrawer(activityDrawer); });
document.querySelector("#activityFooter").addEventListener("click", () => { setDrawerTab("pending"); openDrawer(activityDrawer); });
document.querySelector("#openJobCenter").addEventListener("click", () => { setDrawerTab("jobs"); openDrawer(activityDrawer); });
document.querySelector("#researchJobCenter").addEventListener("click", () => { setDrawerTab("jobs"); openDrawer(activityDrawer); });
document.querySelectorAll("[data-drawer-tab]").forEach((button) => {
  button.addEventListener("click", () => setDrawerTab(button.dataset.drawerTab));
});
document.querySelector("#chatTrigger").addEventListener("click", () => openDrawer(chatDrawer));
document.querySelector("#researchChat").addEventListener("click", () => openDrawer(chatDrawer));
document.querySelector("#sideChat").addEventListener("click", () => openDrawer(chatDrawer));
document.querySelector("#researchHubChat").addEventListener("click", () => openDrawer(chatDrawer));
document.querySelector("#journalChat").addEventListener("click", () => openDrawer(chatDrawer));
document.querySelectorAll(".drawer-close").forEach((button) => button.addEventListener("click", closeLayers));
scrim.addEventListener("click", closeLayers);

function openPalette() {
  closeLayers();
  commandPalette.classList.add("open");
  commandPalette.setAttribute("aria-hidden", "false");
  const input = document.querySelector("#paletteInput");
  input.value = "";
  filterPaletteResults("");
  setTimeout(() => input.focus(), 40);
}

document.querySelector("#commandTrigger").addEventListener("click", openPalette);
document.querySelector("#discoverSearch").addEventListener("click", openPalette);
document.querySelector("#researchSearch").addEventListener("click", openPalette);
document.querySelector(".mobile-menu").addEventListener("click", openPalette);
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openPalette();
  }
  if (event.key === "Escape") closeLayers();
});
commandPalette.addEventListener("click", (event) => {
  if (event.target === commandPalette) closeLayers();
});

function visiblePaletteResults() {
  return [...document.querySelectorAll(".palette-result")].filter((result) => !result.hidden);
}

function setPaletteActive(index) {
  const results = visiblePaletteResults();
  if (!results.length) return;
  const normalized = (index + results.length) % results.length;
  results.forEach((result, resultIndex) => {
    result.classList.toggle("keyboard-active", resultIndex === normalized);
  });
  commandPalette.dataset.activeIndex = String(normalized);
}

function filterPaletteResults(query) {
  const normalized = query.trim().toLowerCase();
  document.querySelectorAll(".palette-result").forEach((result) => {
    result.hidden = normalized.length > 0 && !result.textContent.toLowerCase().includes(normalized);
  });
  let empty = commandPalette.querySelector(".palette-empty");
  if (!empty) {
    empty = document.createElement("div");
    empty.className = "palette-empty";
    empty.textContent = "没有匹配对象。原型不会为展示而伪造搜索结果。";
    commandPalette.querySelector(".palette-footer").before(empty);
  }
  empty.hidden = visiblePaletteResults().length > 0;
  setPaletteActive(0);
}

document.querySelector("#paletteInput").addEventListener("input", (event) => {
  filterPaletteResults(event.currentTarget.value);
});
document.querySelector("#paletteInput").addEventListener("keydown", (event) => {
  if (!["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) return;
  event.preventDefault();
  const current = Number(commandPalette.dataset.activeIndex || 0);
  if (event.key === "ArrowDown") setPaletteActive(current + 1);
  if (event.key === "ArrowUp") setPaletteActive(current - 1);
  if (event.key === "Enter") visiblePaletteResults()[current]?.click();
});

function updateOrderStep() {
  const previousStep = Number(orderSheet.dataset.step || 0);
  document.querySelectorAll(".order-panel").forEach((panel) => {
    panel.classList.toggle("active", Number(panel.dataset.step) === app.orderStep);
  });
  document.querySelectorAll(".order-steps span").forEach((step, index) => {
    step.classList.toggle("active", index + 1 <= app.orderStep);
  });
  const back = document.querySelector("#orderBack");
  const next = document.querySelector("#orderNext");
  back.textContent = app.orderStep === 1 ? "取消" : "返回修改";
  if (app.orderStep === 1) next.textContent = "运行交易前检查";
  if (app.orderStep === 2) next.textContent = "进入确认";
  if (app.orderStep === 3) next.textContent = "确认并创建";
  const checkbox = document.querySelector("#confirmCheckbox");
  next.disabled = (app.orderStep === 3 && !checkbox.checked) || (app.orderStep === 2 && app.orderBlocked);
  orderSheet.dataset.step = String(app.orderStep);
  orderSheet.classList.toggle("seal-armed", app.orderStep === 3 && checkbox.checked);
  orderSheet.classList.remove("seal-committed");

  if (app.orderStep === 3 && previousStep !== 3 && motionAllowed()) {
    const seal = document.querySelector(".confirm-illustration");
    seal.classList.remove("seal-enter");
    void seal.offsetWidth;
    seal.classList.add("seal-enter");
  }
}

function updateOrderCalculations() {
  const quantityInput = document.querySelector("#orderQuantity");
  const direction = document.querySelector("#orderDirection").value;
  const isBuy = direction === "buy";
  const isExit = direction === "exit";
  const quantity = isExit
    ? orderFixture.currentShares
    : Math.max(100, Math.round((Number(quantityInput.value) || 100) / 100) * 100);
  quantityInput.value = String(quantity);
  quantityInput.disabled = isExit;
  document.querySelectorAll(".number-field button").forEach((button) => {
    button.disabled = isExit;
  });

  const amount = orderFixture.price * quantity;
  const fee = amount * (isBuy ? orderFixture.buyFeeRate : orderFixture.sellFeeRate);
  const afterCash = isBuy
    ? orderFixture.availableCash - amount - fee
    : orderFixture.availableCash + amount - fee;
  const afterPositionValue = Math.max(0, orderFixture.currentPositionValue + (isBuy ? amount : -amount));
  const afterCashPct = (afterCash / orderFixture.totalAssets) * 100;
  const afterPositionPct = (afterPositionValue / orderFixture.totalAssets) * 100;
  const downsideImpact = afterPositionPct * 0.1;
  app.orderBlocked = isBuy ? afterCash < 0 : quantity > orderFixture.currentShares;

  const actionLabel = isBuy ? "买入" : isExit ? "清仓卖出" : "减仓";
  const afterLabel = isBuy ? "买入后" : isExit ? "清仓后" : "减仓后";

  document.querySelector("#orderAmount").textContent = formatCurrency(amount);
  document.querySelector("#orderFee").textContent = formatCurrency(fee);
  document.querySelector("#liquidityRuleTitle").textContent = isBuy ? "A 股整手与可用现金" : "可卖数量与 T+1";
  document.querySelector("#checkCashAfter").textContent = app.orderBlocked
    ? isBuy
      ? `${quantity.toLocaleString("zh-CN")} 股整手；可用现金不足 ${formatCurrency(Math.abs(afterCash))}。`
      : `当前可卖 ${orderFixture.currentShares.toLocaleString("zh-CN")} 股，不能提交 ${quantity.toLocaleString("zh-CN")} 股。`
    : isBuy
      ? `${quantity.toLocaleString("zh-CN")} 股整手；预计成交后现金 ${formatCurrency(afterCash)}。`
      : `当前可卖 ${orderFixture.currentShares.toLocaleString("zh-CN")} 股；预计成交后现金 ${formatCurrency(afterCash)}。`;
  document.querySelector("#checkConcentration").textContent = isBuy
    ? afterPositionPct >= 30
      ? `单标的与食品饮料暴露均为 ${afterPositionPct.toFixed(1)}%，已超过 30% 预警线。`
      : `单标的与食品饮料暴露均为 ${afterPositionPct.toFixed(1)}%，接近 30% 预警线。`
    : `单标的与食品饮料暴露将降至 ${afterPositionPct.toFixed(1)}%，集中度风险下降。`;
  document.querySelector("#impactCash").textContent = app.orderBlocked ? "不可用" : `${afterCashPct.toFixed(1)}%`;
  document.querySelector("#impactDown").textContent = `账户约 −${downsideImpact.toFixed(2)}%`;
  document.querySelector("#impactFee").textContent = formatCurrency(fee);
  document.querySelector("#positionChange").textContent = `${orderFixture.currentPositionPct.toFixed(1)}% → ${afterPositionPct.toFixed(1)}%`;
  document.querySelector("#positionAfter").style.width = `${Math.min(100, (afterPositionPct / 40) * 100)}%`;
  document.querySelector("#positionActionLabel").textContent = afterLabel;
  document.querySelector(".position-folio").setAttribute("aria-label", `食品饮料行业暴露将由 24.8% 变为 ${afterPositionPct.toFixed(1)}%，30% 为预警线`);
  document.querySelector("#confirmOrderSummary").innerHTML = `${actionLabel} 贵州茅台 ${quantity.toLocaleString("zh-CN")} 股。当前为开盘前场景，订单将进入 <code>pending</code>，不会假设立即成交。`;
  document.querySelector("#confirmAmount").textContent = formatCurrency(amount);
  document.querySelector("#confirmFee").textContent = formatCurrency(fee);
  document.querySelector("#confirmChoice").textContent = `${actionLabel} ${quantity.toLocaleString("zh-CN")} 股，系统已保存与软建议的差异`;
  document.querySelector("#orderBoundary").textContent = isBuy
    ? "软建议：不新增或买入 100 股；小于 100 股不满足 A 股买入整手规则。你可以忽略软建议，但硬闸门不能绕过。"
    : "AI 研究只作为减仓参考；最终数量由你决定，可卖数量与 T+1 是不能绕过的硬规则。";

  const summary = document.querySelector("#orderCheckSummary");
  const summaryTitle = summary.querySelector("strong");
  const summaryDetail = summary.querySelector("p");
  const cashRow = document.querySelector("#cashCheckRow");
  const cashIcon = cashRow.querySelector(".check-icon");
  const cashState = cashRow.querySelector("em");
  const concentrationRow = document.querySelector("#concentrationRow");
  const concentrationIcon = concentrationRow.querySelector(".check-icon");
  const concentrationState = concentrationRow.querySelector("em");

  summary.classList.toggle("blocked", app.orderBlocked);
  summary.classList.toggle("review", !app.orderBlocked && isBuy);
  summary.classList.toggle("pass", !app.orderBlocked && !isBuy);
  cashRow.classList.toggle("blocked", app.orderBlocked);
  cashIcon.classList.toggle("pass", !app.orderBlocked);
  cashIcon.classList.toggle("blocked", app.orderBlocked);
  cashIcon.textContent = app.orderBlocked ? "×" : "✓";
  cashState.textContent = app.orderBlocked ? "阻断" : "通过";
  concentrationIcon.classList.toggle("review", isBuy);
  concentrationIcon.classList.toggle("pass", !isBuy);
  concentrationIcon.textContent = isBuy ? "!" : "✓";
  concentrationState.textContent = isBuy ? "复核" : "通过";

  if (app.orderBlocked) {
    summaryTitle.textContent = isBuy ? "可用现金不足，不能进入确认" : "可卖数量不足，不能进入确认";
    summaryDetail.textContent = "硬规则未通过；系统不会创建订单，也不会补造现金或可卖数量。";
  } else if (!isBuy) {
    summaryTitle.textContent = isExit ? "清仓规则通过，等待你确认" : "减仓规则通过，等待你确认";
    summaryDetail.textContent = `预计行业暴露降至 ${afterPositionPct.toFixed(1)}%；AI 研究不会替你提交订单。`;
  } else if (afterPositionPct >= 30) {
    summaryTitle.textContent = "可以继续，但集中度已超过预警线";
    summaryDetail.textContent = `硬规则通过；${quantity.toLocaleString("zh-CN")} 股会使食品饮料暴露升至 ${afterPositionPct.toFixed(1)}%。`;
  } else {
    summaryTitle.textContent = "可以继续，但建议降低数量";
    summaryDetail.textContent = `硬规则通过；${quantity.toLocaleString("zh-CN")} 股会使食品饮料行业暴露接近 30% 预警线。`;
  }
}

function openOrder() {
  if (stateSelector.value === "degraded" || stateSelector.value === "error") {
    showToast("当前不能进入确认", "行情或数据状态不满足交易前检查要求。", "warning");
    return;
  }
  closeLayers();
  app.orderStep = 1;
  document.querySelector("#confirmCheckbox").checked = false;
  document.querySelector("#orderDirection").value = "buy";
  document.querySelector("#orderQuantity").value = "100";
  orderSheet.dataset.step = "0";
  orderSheet.classList.remove("seal-armed", "seal-committed");
  updateOrderCalculations();
  updateOrderStep();
  orderSheet.classList.add("open");
  orderSheet.setAttribute("aria-hidden", "false");
  scrim.classList.add("visible");
}

document.querySelectorAll(".open-order").forEach((button) => button.addEventListener("click", openOrder));
document.querySelector(".order-close").addEventListener("click", closeLayers);
document.querySelector("#orderBack").addEventListener("click", () => {
  if (app.orderStep === 1) closeLayers();
  else {
    app.orderStep -= 1;
    updateOrderStep();
  }
});
document.querySelector("#orderNext").addEventListener("click", (event) => {
  if (app.orderStep < 3) {
    app.orderStep += 1;
    updateOrderStep();
    return;
  }
  if (!document.querySelector("#confirmCheckbox").checked) return;
  const commitOrder = () => {
    const quantity = Number(document.querySelector("#orderQuantity").value) || 100;
    const direction = document.querySelector("#orderDirection").value;
    const actionLabel = direction === "buy" ? "买入" : direction === "exit" ? "清仓卖出" : "减仓";
    const firstOrder = document.querySelector(".order-timeline .timeline-item");
    if (firstOrder) {
      const newOrder = document.createElement("div");
      newOrder.className = "timeline-item demo-order";
      newOrder.innerHTML = `<span class="timeline-dot pending"></span><div><strong>贵州茅台 · ${actionLabel} ${quantity.toLocaleString("zh-CN")} 股</strong><p>用户确认 · pending · 预估费用 ${document.querySelector("#orderFee").textContent}</p></div><em>pending</em>`;
      firstOrder.insertAdjacentElement("beforebegin", newOrder);
    }
    closeLayers();
    setPage("portfolio");
    showToast("模拟委托已创建", `贵州茅台 · ${actionLabel} ${quantity.toLocaleString("zh-CN")} 股 · pending`);
  };
  if (motionAllowed()) {
    orderSheet.classList.add("seal-committed");
    event.currentTarget.disabled = true;
    window.setTimeout(commitOrder, 210);
  } else {
    commitOrder();
  }
});
document.querySelector("#confirmCheckbox").addEventListener("change", updateOrderStep);

document.querySelectorAll(".number-field button").forEach((button) => {
  button.addEventListener("click", () => {
    const input = button.parentElement.querySelector("input");
    const current = Number(input.value) || 100;
    input.value = Math.max(100, current + (button.textContent.trim() === "＋" ? 100 : -100));
    updateOrderCalculations();
    updateOrderStep();
  });
});
document.querySelector("#orderQuantity").addEventListener("change", () => {
  updateOrderCalculations();
  updateOrderStep();
});
document.querySelector("#orderDirection").addEventListener("change", () => {
  updateOrderCalculations();
  updateOrderStep();
});

function showToast(title, detail, kind = "success") {
  toast.querySelector("strong").textContent = title;
  toast.querySelector("p").textContent = detail;
  toast.querySelector(":scope > span").textContent = kind === "warning" ? "!" : "✓";
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 4200);
}
toast.querySelector("button").addEventListener("click", () => toast.classList.remove("show"));

const emptyStates = {
  today: ["今天还没有账户或待办", "可以先浏览市场和研究标的；无需为了完成流程而创建订单。", "去发现", "discover"],
  discover: ["今天没有满足条件的研究线索", "空候选是正常结果。系统不会为了展示而把弱信号包装成机会。", "查看自选", "today"],
  researchCenter: ["研究台还没有报告或任务", "研究会在冻结对象身份、证据日期和版本后出现；空白不会用演示报告填充。", "搜索标的", "discover"],
  research: ["还没有这只标的的研究", "生成后会冻结证据、有效日期和版本；刷新页面不会重复调用 AI。", "生成研究", "research"],
  portfolio: ["还没有用户模拟账户", "你可以先研究和观察。创建账户后，订单、持仓和盈亏会保存在独立账本。", "创建模拟账户", "portfolio"],
  journal: ["还没有可复盘的决策", "研究、用户选择、订单和结果形成完整链路后，才会出现在这里。", "开始研究", "discover"],
  help: ["没有找到匹配内容", "换一个关键词，或从快速开始和能力边界继续浏览。", "返回快速开始", "help"],
  lab: ["当前没有可展示的专业记录", "这不会改变普通投资者流程；实验、影子账户和正式证据始终独立记录。", "返回今日", "today"],
};

function stateTemplate(kind) {
  const [title, detail, action, target] = emptyStates[app.page] || emptyStates.today;
  if (kind === "empty") {
    return `<div class="state-surface-inner"><div class="state-illustration">${icon("i-search")}</div><h2>${title}</h2><p>${detail}</p><div class="state-actions"><button class="primary-button" data-target="${target}" type="button">${action}</button><button class="secondary-button" data-target="help" type="button">查看说明</button></div></div>`;
  }
  if (kind === "loading") {
    return `<div class="state-surface-inner"><div class="state-illustration">${icon("i-discover")}</div><h2>正在研究当前对象</h2><p>可以继续浏览其他页面。任务完成后会保存结果并通知你，不需要停留等待。</p><div class="loading-steps"><div class="done"><i>✓</i><span>确认标的与有效数据日期</span><em>完成</em></div><div class="done"><i>✓</i><span>冻结行情、财务与持仓证据</span><em>完成</em></div><div class="active"><i></i><span>汇总反证与失效条件</span><em>处理中</em></div><div><i>4</i><span>保存可追溯报告</span><em>等待</em></div></div><div class="skeleton-blocks"><i></i><i></i><i></i><i></i></div><div class="state-actions"><button class="secondary-button" id="sendToBackground" type="button">转到后台</button><button class="text-button" id="cancelLoadingTask" type="button">取消任务</button></div></div>`;
  }
  return `<div class="state-surface-inner"><div class="state-illustration">${icon("i-shield")}</div><h2>这次内容没有完成加载</h2><p>上次成功内容仍然保留；系统没有用旧数据冒充当前结果，也没有创建任何订单。</p><div class="state-actions"><button class="primary-button" id="safeRetry" type="button">安全重试</button><button class="secondary-button" data-target="lab" type="button">查看原因</button></div></div>`;
}

function renderState(state) {
  mainContent.classList.remove("state-mode");
  stateSurface.classList.remove("visible");
  degradationBanner.classList.remove("visible");
  errorBanner.classList.remove("visible");
  backgroundJobCard.classList.remove("visible");
  document.querySelectorAll(".open-order").forEach((button) => { button.disabled = false; });

  if (["empty", "loading"].includes(state)) {
    mainContent.classList.add("state-mode");
    stateSurface.innerHTML = stateTemplate(state);
    stateSurface.classList.add("visible");
    const backgroundButton = document.querySelector("#sendToBackground");
    if (backgroundButton) {
      backgroundButton.addEventListener("click", () => {
        stateSelector.value = "background";
        renderState("background");
      });
    }
    const retryButton = document.querySelector("#safeRetry");
    if (retryButton) {
      retryButton.addEventListener("click", () => {
        stateSelector.value = "loading";
        renderState("loading");
      });
    }
    const cancelButton = document.querySelector("#cancelLoadingTask");
    if (cancelButton) {
      cancelButton.addEventListener("click", () => {
        stateSelector.value = "normal";
        renderState("normal");
        showToast("研究任务已取消", "没有生成报告，也没有创建任何订单。", "warning");
      });
    }
  }

  if (state === "degraded") {
    degradationBanner.classList.add("visible");
    document.querySelectorAll(".open-order").forEach((button) => { button.disabled = true; });
  }

  if (state === "error") {
    errorBanner.classList.add("visible");
    document.querySelectorAll(".open-order").forEach((button) => { button.disabled = true; });
  }

  if (state === "background") backgroundJobCard.classList.add("visible");
}

stateSelector.addEventListener("change", () => renderState(stateSelector.value));
document.querySelector("#errorRetry").addEventListener("click", () => {
  stateSelector.value = "loading";
  renderState("loading");
});
document.querySelector("#dismissJob").addEventListener("click", () => backgroundJobCard.classList.remove("visible"));

const helpContent = {
  quick: {
    crumb: "帮助中心 / 快速开始",
    title: "快速开始：从一个真实问题出发",
    lead: "无需课程、问卷或强制引导。你可以从持仓、一个代码，或今天的市场变化直接开始。",
    body: `<div class="quick-steps"><div><span>1</span><h3>找到标的</h3><p>从全局搜索、发现、自选或持仓打开标的工作区。</p></div><div><span>2</span><h3>读判断与反证</h3><p>先看当前判断、主要反证、失效条件和数据截止时间。</p></div><div><span>3</span><h3>追问或检查订单</h3><p>Chat 会绑定当前报告；模拟订单必须经过独立检查与确认。</p></div><div><span>4</span><h3>回到组合与复盘</h3><p>跟踪委托、持仓、盈亏，并在到期时对照原始判断。</p></div></div><h3>你随时可以停在哪里</h3><p>看到“继续观察”“暂无候选”或“数据不可用”时，不需要为了完成流程而下单。QuantLab 不会为演示强行生成建议。</p>`,
  },
  discover: {
    crumb: "帮助中心 / 发现与研究",
    title: "从线索进入研究，而不是从榜单直接下单",
    lead: "发现页按证据变化组织市场、行业和标的。候选只代表研究优先级。",
    body: `<h3>研究线索包含什么</h3><p>每条线索同时显示为什么值得继续看、证据覆盖、数据缺口和与当前组合的关系。资金活跃度不能单独生成买入结论。</p><h3>怎样进入研究</h3><p>点击标的后会打开对象工作区，并固定证券身份、有效数据日期和报告版本。没有足够数据时，系统会保持空或不可用。</p>`,
  },
  report: {
    crumb: "帮助中心 / AI 报告",
    title: "如何阅读一份 AI 研究报告",
    lead: "先确认数据日期，再读当前判断、反证与失效条件；模型把握度不是胜率。",
    body: `<h3>建议顺序</h3><p>1. 数据与报告身份；2. 当前研究判断；3. 支持证据；4. 反对证据；5. 失效条件；6. 对当前账户的影响；7. 来源与审计。</p><div class="article-callout">${icon("i-info")}<p><strong>概率不是收益承诺。</strong>“5 日方向概率 54%”只描述模型在当前证据下的研究输出。样本不足时，它不能证明交易收益增量。</p></div><h3>数据降级时</h3><p>受影响的段落会标记“部分可用”或“不可用”。缺失部分不会由 AI 代写，依赖缺失数据的高风险操作会关闭。</p>`,
  },
  simulation: {
    crumb: "帮助中心 / 模拟交易",
    title: "模拟订单如何检查、确认与成交",
    lead: "所有订单只作用于当前用户模拟账户，不会发送到券商。",
    body: `<h3>确认前会检查什么</h3><p>服务器行情、交易日、A 股整手、T+1、可用现金、冻结资源、费用、仓位和硬风险。AI 研究只作为参考，不能覆盖硬规则。</p><h3>常见状态</h3><p><code>pending</code> 等待可成交时段；<code>partially_filled</code> 部分成交；<code>filled</code> 完成；<code>cancelled</code> 用户撤销；<code>rejected</code> 违反规则；<code>expired</code> 超过有效期。</p><h3>为什么和券商可能不同</h3><p>模拟成交使用系统的价格、费用与滑点假设，不是券商逐笔回报。页面会持续显示成交假设。</p>`,
  },
  portfolio: {
    crumb: "帮助中心 / 组合与盈亏",
    title: "怎样理解持仓、盈亏与基准",
    lead: "不要只看绝对赚亏。组合页同时展示现金、费用、最大回撤与同期基准。",
    body: `<h3>已实现与未实现</h3><p>已实现盈亏来自完成卖出的持仓；未实现盈亏来自当前持仓按最近有效行情盯市。行情过期时会保留最近值并标注日期。</p><h3>账户不会混算</h3><p>用户模拟、外部成交记录、策略影子和 Historical Demo 使用不同账本与环境标识。</p>`,
  },
  chat: {
    crumb: "帮助中心 / Chat 与动态",
    title: "Chat、通知和后台任务如何协作",
    lead: "Chat 绑定当前报告或账户；动态中心跨页面保留需要你处理的事项和后台进度。",
    body: `<h3>Chat 能做什么</h3><p>解释证据、比较版本、提出反证，以及生成任务或订单草稿。草稿必须前往独立页面确认。</p><h3>可以离开加载页</h3><p>长任务会进入后台，显示 queued、running、waiting、failed 或 completed。安全重试会复用可验证的已完成步骤。</p>`,
  },
  data: {
    crumb: "帮助中心 / 数据状态",
    title: "可用、部分可用、降级和不可用分别意味着什么",
    lead: "状态不仅是颜色，也决定哪些内容可以显示、哪些操作必须关闭。",
    body: `<h3>四个常见状态</h3><p><strong>可用：</strong>数据与身份通过检查；<strong>部分可用：</strong>缺少部分字段，相关结论收窄；<strong>降级：</strong>保留最近成功内容，高风险操作关闭；<strong>不可用：</strong>不生成当前结论或成交。</p><h3>为什么数据不是今天</h3><p>休市、数据源失败或发布时滞都会导致请求日期和有效数据日不同。报告只使用最近可验证日期，并同时显示二者。</p>`,
  },
  boundary: {
    crumb: "帮助中心 / 能力边界",
    title: "QuantLab 能做什么，明确不会做什么",
    lead: "它是研究与模拟验证工具，不是自动交易员，也不提供收益保证。",
    body: `<h3>可以</h3><p>发现和研究股票、ETF；整理行情、财务、事件、资金与组合证据；继续追问；生成交易草稿；执行用户确认后的模拟订单；保存决策与复盘。</p><h3>不会</h3><p>连接券商自动下单、补造缺失数据、把历史演示混入正式证据、用 LLM 放宽硬规则，或暗示稳赢与保证收益。</p><div class="article-callout">${icon("i-shield")}<p>用户模拟结果不会进入策略正式成绩或训练集；系统影子账户也不能由用户手工改仓。</p></div>`,
  },
  faq: {
    crumb: "帮助中心 / 常见问题",
    title: "常见问题",
    lead: "围绕操作、数据和证据边界的快速回答。",
    body: `<h3>AI 会替我创建订单吗？</h3><p>不会。AI 与 Chat 最多生成草稿；模拟订单必须经过独立检查并由你最终确认。</p><h3>为什么看不到确认按钮？</h3><p>通常是行情不可操作、硬规则未通过、账户内容变化，或上次检查失效。页面会显示具体原因。</p><h3>历史演示代表当前表现吗？</h3><p>不代表。它使用冻结数据复现产品流程，永远不进入当前账户、正式成绩或训练集。</p><h3>后台任务失败会留下订单吗？</h3><p>不会。任务失败不会自动执行草稿，任何订单仍需要独立检查与用户确认。</p>`,
  },
};

function renderHelp(key) {
  const content = helpContent[key] || helpContent.quick;
  document.querySelectorAll("[data-help]").forEach((button) => {
    button.classList.toggle("active", button.dataset.help === key && button.closest(".help-nav"));
  });
  document.querySelector("#helpArticle").innerHTML = `<div class="help-breadcrumb">${content.crumb}</div><h2>${content.title}</h2><p class="article-lead">${content.lead}</p>${content.body}<div class="article-callout">${icon("i-shield")}<p><strong>始终由你决定。</strong>AI 可以研究、解释、提出草稿与风险提醒；最终模拟订单需要独立确认，系统不连接券商。</p></div><h3>继续了解</h3><div class="article-links"><button data-help="report" type="button">如何阅读 AI 报告 ${icon("i-arrow")}</button><button data-help="simulation" type="button">模拟交易如何成交 ${icon("i-arrow")}</button></div>`;
}

document.addEventListener("click", (event) => {
  const helpButton = event.target.closest("[data-help]");
  if (!helpButton) return;
  event.preventDefault();
  setPage("help", { keepScroll: true });
  renderHelp(helpButton.dataset.help);
  document.querySelector("#helpArticle").scrollIntoView({ behavior: scrollBehavior(), block: "start" });
});

document.querySelector("#helpSearchInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    const query = event.currentTarget.value.trim();
    let target = "faq";
    if (!query) target = "quick";
    else if (/报告|AI|概率|判断|证据/.test(query)) target = "report";
    else if (/模拟|订单|成交|券商|买入|卖出/.test(query)) target = "simulation";
    else if (/组合|持仓|盈亏|基准/.test(query)) target = "portfolio";
    else if (/Chat|对话|通知|任务|后台/i.test(query)) target = "chat";
    else if (/数据|行情|日期|降级|过期|不可用/.test(query)) target = "data";
    else if (/边界|自动|保证|收益|能力/.test(query)) target = "boundary";
    renderHelp(target);
    document.querySelector("#helpArticle").scrollIntoView({ behavior: scrollBehavior(), block: "start" });
  }
});

document.querySelector("#watchlistToggle").addEventListener("click", (event) => {
  const button = event.currentTarget;
  const selected = button.getAttribute("aria-pressed") !== "true";
  button.setAttribute("aria-pressed", String(selected));
  button.textContent = selected ? "已加入自选" : "加入自选";
  showToast(selected ? "已加入自选" : "已移出自选", "贵州茅台 · 本地概念原型状态");
});

document.querySelectorAll(".filter-chips, .period-control").forEach((control) => {
  control.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    control.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
  });
});

const researchDeskLenses = document.querySelector(".research-center-page .workspace-lenses");
researchDeskLenses?.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const buttons = [...researchDeskLenses.querySelectorAll("button")];
  const index = buttons.indexOf(button);
  buttons.forEach((item) => item.classList.toggle("active", item === button));
  const rows = [...document.querySelectorAll("button.thesis-row")];
  rows.forEach((row) => { row.hidden = index === 1 && row.dataset.symbol !== "600519"; });
  if (index === 2) document.querySelector(".run-monitor")?.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
  if (index === 3) showToast("自选观察 · 概念视图", "正式产品会显示保存的自选、提醒规则和最近变化。", "warning");
  if (index === 4) showToast("历史版本 · 概念视图", "正式产品会按标的、报告版本和有效日期筛选。", "warning");
});

const accountSwitcherButtons = document.querySelectorAll(".account-switcher > button");
accountSwitcherButtons[1]?.addEventListener("click", () => {
  showToast("外部记录当前为空", "不会使用用户模拟持仓填充外部只读账户。", "warning");
});
document.querySelector(".account-switcher .add-account")?.addEventListener("click", () => {
  showToast("概念原型不写入新账户", "正式产品会在独立流程中创建用户模拟账户。", "warning");
});

document.querySelectorAll("[data-prototype-action]").forEach((button) => {
  button.addEventListener("click", () => {
    showToast(`${button.dataset.prototypeAction} · 概念入口`, "本轮原型展示信息架构与状态边界，不写入生产运行配置。", "warning");
  });
});

function initReactiveSurfaces() {
  if (!motionAllowed() || !precisionPointerQuery.matches) return;
  const surfaces = document.querySelectorAll([
    ".account-overview",
    ".decision-brief",
    ".portfolio-chart-card",
    ".review-feature",
    ".discovery-card",
    ".thesis-board",
    ".evidence-domain-card",
  ].join(","));

  surfaces.forEach((surface) => {
    surface.classList.add("surface-reactive");
    surface.addEventListener("pointermove", (event) => {
      const bounds = surface.getBoundingClientRect();
      surface.style.setProperty("--spot-x", `${event.clientX - bounds.left}px`);
      surface.style.setProperty("--spot-y", `${event.clientY - bounds.top}px`);
    });
    surface.addEventListener("pointerleave", () => {
      surface.style.setProperty("--spot-x", "-100px");
      surface.style.setProperty("--spot-y", "-100px");
    });
  });
}

function initEvidenceFocus() {
  const brief = document.querySelector(".decision-brief");
  const support = document.querySelector(".evidence-card.supportive");
  const oppose = document.querySelector(".evidence-card.opposing");
  if (!brief || !support || !oppose) return;

  const bind = (card, focusClass) => {
    card.tabIndex = 0;
    const activate = () => brief.classList.add(focusClass);
    const deactivate = () => brief.classList.remove(focusClass);
    card.addEventListener("pointerenter", activate);
    card.addEventListener("pointerleave", deactivate);
    card.addEventListener("focus", activate);
    card.addEventListener("blur", deactivate);
  };

  bind(support, "focus-support");
  bind(oppose, "focus-oppose");
}

function handleMotionPreferenceChange() {
  if (ambientAllowed()) startAmbientField();
  else stopAmbientField();
}

window.addEventListener("resize", () => {
  if (ambientAllowed()) resizeAmbientField();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopAmbientField();
  else startAmbientField();
});
motionQuery.addEventListener?.("change", handleMotionPreferenceChange);
narrowQuery.addEventListener?.("change", handleMotionPreferenceChange);
navigator.connection?.addEventListener?.("change", handleMotionPreferenceChange);

document.querySelector(".chat-composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const textarea = event.currentTarget.querySelector("textarea");
  const value = textarea.value.trim();
  if (!value) return;
  const messages = document.querySelector(".chat-messages");
  const userMessage = document.createElement("div");
  userMessage.className = "message user";
  userMessage.innerHTML = `<p>${value.replace(/[<>]/g, "")}</p><time>刚刚</time>`;
  messages.appendChild(userMessage);
  textarea.value = "";
  messages.scrollTop = messages.scrollHeight;
  window.setTimeout(() => {
    const assistant = document.createElement("div");
    assistant.className = "message assistant";
    assistant.innerHTML = `<span class="assistant-mark">Q</span><div><p>这是交互原型。正式产品会基于当前冻结报告回答，并逐条附上证据引用；任何操作只会生成待确认草稿。</p></div>`;
    messages.appendChild(assistant);
    messages.scrollTop = messages.scrollHeight;
  }, 550);
});

renderHelp("quick");
renderState("normal");
updateOrderCalculations();
updateOrderStep();
updateCoordinate();
setDrawerTab("pending");
initReactiveSurfaces();
initEvidenceFocus();
startAmbientField();
