/* ═══ app.jsx — main application ═══ */

/* Client-side validation */
function validate(config) {
  const errors = [], warnings = [];

  if (config.endDate < config.startDate) errors.push("End date must be after start date.");
  if (config.salesRows <= 0) errors.push("Total rows must be > 0.");
  if (config.maxPrice <= config.minPrice) errors.push("Max unit price must exceed min unit price.");
  if (config.returnsEnabled && (config.returnRate < 0 || config.returnRate > 1)) errors.push("Return rate must be 0\u20131.");

  const regionalSum = config.pctIndia + config.pctUs + config.pctEu + config.pctAsia;
  if (regionalSum <= 0) warnings.push("Customer regional % sum to 0.");
  if (config.chunkSize > config.salesRows) warnings.push("Chunk size exceeds total rows.");
  if (config.format === "csv" && config.salesRows > 5000000) warnings.push("Large CSV outputs can be slow. Consider parquet.");
  if (config.skipOrderCols) warnings.push("Order columns skipped \u2014 Returns will not be generated.");
  if (config.customers > config.salesRows && config.salesRows > 0) warnings.push(`Customers (${config.customers.toLocaleString()}) exceed sales rows.`);
  if (config.products > config.salesRows && config.salesRows > 0) warnings.push(`Products (${config.products.toLocaleString()}) exceed sales rows.`);
  if (config.customers > 0 && config.salesRows / config.customers < 1.5) warnings.push(`Low rows-per-customer ratio (${(config.salesRows / config.customers).toFixed(1)}).`);

  const geoSum = Object.values(config.geoWeights).reduce((acc, val) => acc + val, 0);
  if (Math.abs(geoSum - 1) > .05 && geoSum > 0) warnings.push(`Geography weights sum to ${(geoSum * 100).toFixed(0)}% (expected ~100%).`);
  if (config.returnsEnabled && config.returnMaxDays <= config.returnMinDays) warnings.push("Return max days should exceed min days.");
  if (config.marginMax <= config.marginMin) warnings.push("Max margin should exceed min margin.");
  if (config.subEnabled && config.subMaxSubscriptions < 1) errors.push("Subscriptions max must be >= 1.");
  if (config.subEnabled && config.subParticipationRate > 1) errors.push("Subscription participation rate must be <= 1.0.");
  if (config.transfersEnabled && config.transfersAnnualRate > 1) warnings.push("Transfer annual rate should be <= 1.0.");
  if (config.transfersEnabled && config.transfersSameRegionPref > 1) warnings.push("Same-region preference should be <= 1.0.");

  return {errors, warnings};
}

/* Debounced sync */
function useDebounce(fn, ms) {
  const timerRef = useRef(null);
  return useCallback((...args) => {
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => fn(...args), ms);
  }, [fn, ms]);
}

/* ═══════════════════════════════════════════════════════════════════
   App
   ═══════════════════════════════════════════════════════════════════ */
function App() {
  const [cfg, setCfg] = useState(null);
  const [presets, setPresets] = useState({});
  const [presetBucket, setPresetBucket] = useState("");
  const [selectedPreset, setSelectedPreset] = useState(null);
  const [toast, setToast] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [logs, setLogs] = useState([]);
  const [isRunning, setIsRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useRef(null);

  /* Run history */
  const [runHistory, setRunHistory] = useState([]);

  /* Dimension tab */
  const [dimTab, setDimTab] = useState("customers");

  /* Sidebar collapse */
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [validationOpen, setValidationOpen] = useState(false);
  const [regenOpen, setRegenOpen] = useState(false);

  /* Preset preview */
  const [previewPreset, setPreviewPreset] = useState(null);

  /* Models YAML state */
  const [modelsYaml, setModelsYaml] = useState("");
  const [modelsOrig, setModelsOrig] = useState("");
  const [modelsDisk, setModelsDisk] = useState("");
  const [modelsErr, setModelsErr] = useState(null);
  const modelsDirty = modelsYaml !== modelsOrig;
  const modelsApplied = modelsOrig !== modelsDisk;

  /* Models form state (visual editor) */
  const [modelsTab, setModelsTab] = useState("visual");
  const [modelsForm, setModelsForm] = useState(null);

  /* Config YAML state */
  const [cfgYaml, setCfgYaml] = useState("");
  const [cfgYamlOrig, setCfgYamlOrig] = useState("");
  const [cfgYamlErr, setCfgYamlErr] = useState(null);
  const cfgYamlDirty = cfgYaml !== cfgYamlOrig;

  /* Page navigation */
  const [page, setPage_] = useState("main");
  const setPage = useCallback((targetPage) => {
    document.activeElement?.blur();
    setPage_(targetPage);
    if (targetPage === "config") loadCfgYaml();
    if (targetPage === "models") {
      fetch(API + "/models").then(r => r.text()).then(text => { setModelsYaml(text); setModelsOrig(text); }).catch(() => {});
      fetch(API + "/models/form").then(r => r.json()).then(data => setModelsForm(data)).catch(() => {});
    }
  }, []);

  /* ─── Initial load ─── */
  useEffect(() => {
    fetch(API + "/config").then(r => r.json()).then(data => {
      data.geoWeights = data.geoWeights || {};
      data.regenAll = false;
      data.regenDims = {};
      data.autoWorkers = !data.workers;
      setCfg(data);
    }).catch(() => {
      setLoadError("Failed to load config from server");
      setCfg({seed: 42, format: "parquet", salesOutput: "sales", skipOrderCols: false, compression: "snappy", rowGroupSize: 2000000, mergeParquet: true, partitionEnabled: true, maxLinesPerOrder: 5, salesOptimize: true, qualityReport: true, startDate: "2020-01-01", endDate: "2025-12-31", fiscalMonthOffset: 0, asOfDate: "", includeCalendar: true, includeIso: false, includeFiscal: true, includeWeeklyFiscal: false, wfFirstDay: 0, wfWeeklyType: "Last", wfQuarterType: "445", wfTypeStartFiscalYear: 1, salesRows: 103285, chunkSize: 1000000, autoWorkers: false, workers: 8, customers: 48837, stores: 10, products: 2581, promotions: 20, pctIndia: 10, pctUs: 51, pctEu: 39, pctAsia: 0, pctOrg: 1, customerActiveRatio: .98, firstYearPct: .27, householdPct: .35, valueScale: 1, minPrice: 10, maxPrice: 3000, productActiveRatio: .98, marginMin: .20, marginMax: .35, brandNormalize: false, brandNormalizeAlpha: .35, geoWeights: {"United States": .35, India: .2, "United Kingdom": .1, Germany: .1, France: .1, Australia: .07, Canada: .08}, returnsEnabled: true, returnRate: .03, returnMinDays: 1, returnMaxDays: 60, promoNewCustWindow: 3, subEnabled: false, subGenerateBridge: false, subParticipationRate: .65, subAvgSubscriptions: 1.5, subMaxSubscriptions: 5, subChurnRate: .25, subTrialRate: .30, subTrialConversionRate: .85, subTrialDays: 14, subSeed: 700, storeEnsureIsoCoverage: true, storeStaffingRanges: {Supermarket: [8, 20], Hypermarket: [15, 40], Convenience: [2, 6]}, storeRegionWeights: {}, storeOpeningStart: "1995-01-01", storeOpeningEnd: "2023-12-31", storeClosingEnd: "2028-12-31", storeAssortmentEnabled: true, storeOnlineStores: 5, storeClosingEnabled: true, storeCloseShare: .10, employeeEmailDomain: "contoso.com", transfersEnabled: false, transfersAnnualRate: .05, transfersMinTenureMonths: 6, transfersSameRegionPref: .70, erFromCurrencies: ["USD"], erToCurrencies: ["CAD", "GBP", "EUR", "INR", "AUD", "CNY", "JPY"], erBaseCurrency: "USD", erFutureDrift: .02, erIncludeMonthly: true, budgetEnabled: true, budgetDefaultGrowth: .05, inventoryEnabled: true, inventoryGrain: "monthly", inventoryShrinkageEnabled: true, inventoryShrinkageRate: .02, regenAll: false, regenDims: {}});
    });
    fetch(API + "/presets").then(r => r.json()).then(data => { setPresets(data); setPresetBucket(Object.keys(data)[0] || ""); }).catch(() => {});
    fetch(API + "/models").then(r => r.text()).then(text => { setModelsYaml(text); setModelsOrig(text); setModelsDisk(text); }).catch(() => {});
    fetch(API + "/models/form").then(r => r.json()).then(data => setModelsForm(data)).catch(() => {});
  }, []);

  /* ─── Debounced config sync ─── */
  const syncToBackend = useDebounce((vals) => {
    fetch(API + "/config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({values: vals})}).then(r => { if (!r.ok) console.warn("Config sync failed", r.status) }).catch(() => {});
  }, 400);

  const setConfigField = useCallback((key, value) => {
    setCfg(prev => {
      const next = {...prev, [key]: value};
      syncToBackend(next);
      return next;
    });
  }, [syncToBackend]);

  const reloadFormFromServer = useCallback(() => {
    fetch(API + "/config").then(r => r.json()).then(data => {
      data.geoWeights = data.geoWeights || {};
      data.regenAll = cfg?.regenAll || false;
      data.regenDims = cfg?.regenDims || {};
      data.autoWorkers = !data.workers;
      setCfg(data);
    }).catch(() => {});
  }, []);

  const loadCfgYaml = useCallback(() => {
    fetch(API + "/config/yaml/disk").then(r => r.text()).then(text => {
      setCfgYaml(text); setCfgYamlOrig(text); setCfgYamlErr(null);
    }).catch(() => {});
  }, []);

  const flash = (message) => { setToast(message); setTimeout(() => setToast(null), 2400); };

  const setGeoWeight = (country, weight) => setCfg(prev => {
    const next = {...prev, geoWeights: {...prev.geoWeights, [country]: Math.max(0, Math.min(1, weight))}};
    syncToBackend(next);
    return next;
  });

  const applyPreset = (name) => {
    fetch(API + "/presets/apply", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name})})
      .then(() => fetch(API + "/config").then(r => r.json()).then(data => {
        data.geoWeights = data.geoWeights || {};
        data.regenAll = cfg.regenAll;
        data.regenDims = cfg.regenDims;
        data.autoWorkers = !data.workers;
        setCfg(data);
      }))
      .catch(() => {});
    setSelectedPreset(name);
    flash(`Applied: ${name}`);
  };

  /* ─── Generate ─── */
  const runGenerate = () => {
    if (errors.length > 0 || isRunning) return;
    const regenDims = cfg.regenAll ? DIMS : Object.keys(cfg.regenDims).filter(key => cfg.regenDims[key]);
    fetch(API + "/generate", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({regen_dimensions: regenDims.length ? regenDims : null})})
      .then(r => r.json()).then(() => {
        setLogs([]); setIsRunning(true); setElapsed(0);
        const start = Date.now();
        timerRef.current = setInterval(() => setElapsed((Date.now() - start) / 1000), 100);
        const eventSource = new EventSource(API + "/generate/stream");
        const cleanup = () => {
          clearInterval(timerRef.current);
          timerRef.current = null;
          setIsRunning(false);
          eventSource.close();
        };
        eventSource.onmessage = (event) => {
          const data = JSON.parse(event.data);
          if (data.type === "log") setLogs(prev => [...prev, data.line]);
          if (data.type === "status") setElapsed(data.elapsed);
          if (data.type === "end") {
            cleanup(); setElapsed(data.elapsed);
            const formattedElapsed = data.elapsed < 60 ? `${data.elapsed.toFixed(1)}s` : `${Math.floor(data.elapsed / 60)}m ${Math.floor(data.elapsed % 60)}s`;
            flash(data.status === "done" ? `Pipeline completed in ${formattedElapsed}` : `Pipeline failed after ${formattedElapsed}`);
            setRunHistory(prev => [{status: data.status, elapsed: data.elapsed, rows: cfg.salesRows, time: new Date().toLocaleTimeString()}, ...prev].slice(0, 10));
          }
          if (data.type === "idle") { cleanup(); }
        };
        eventSource.onerror = () => { cleanup(); };
      }).catch(err => { setLogs(["Failed to start pipeline: " + err.message]); });
  };

  const cancelGenerate = () => {
    fetch(API + "/generate/cancel", {method: "POST"}).then(() => { clearInterval(timerRef.current); setIsRunning(false); }).catch(() => {});
  };

  /* ─── Ctrl+Enter to generate ─── */
  useEffect(() => {
    const handler = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); runGenerate(); }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  });

  /* ─── Models YAML ─── */
  const saveModels = () => {
    setModelsErr(null);
    fetch(API + "/models", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({yaml_text: modelsYaml})})
      .then(r => { if (!r.ok) return r.json().then(data => { throw new Error(data.detail || "Save failed") }); return r.json(); })
      .then(data => {
        setModelsOrig(modelsYaml); setModelsErr(null); flash("Models saved (in memory)");
      })
      .catch(err => setModelsErr(err.message));
  };

  const resetModels = () => {
    fetch(API + "/models/reset", {method: "POST"})
      .then(r => r.json())
      .then(data => {
        fetch(API + "/models").then(r => r.text()).then(text => { setModelsYaml(text); setModelsOrig(text); setModelsDisk(text); setModelsErr(null); flash("Models reset to disk version"); });
        fetch(API + "/models/form").then(r => r.json()).then(formData => setModelsForm(formData)).catch(() => {});
      }).catch(() => {});
  };

  /* ─── Models form sync ─── */
  const syncModelsForm = useDebounce((vals) => {
    fetch(API + "/models/form", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({values: vals})})
      .then(r => r.json()).then(data => { if (data.yaml_text) { setModelsYaml(data.yaml_text); setModelsOrig(data.yaml_text); } }).catch(() => {});
  }, 400);

  const setModelField = useCallback((key, value) => {
    setModelsForm(prev => { const next = {...prev, [key]: value}; syncModelsForm(next); return next; });
  }, [syncModelsForm]);

  /* ─── Config YAML ─── */
  const saveCfgYaml = () => {
    setCfgYamlErr(null);
    fetch(API + "/config/yaml", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({yaml_text: cfgYaml})})
      .then(r => { if (!r.ok) return r.json().then(data => { throw new Error(data.detail || "Save failed") }); return r.json(); })
      .then(() => { setCfgYamlOrig(cfgYaml); setCfgYamlErr(null); flash("Config applied (in memory)"); reloadFormFromServer(); })
      .catch(err => setCfgYamlErr(err.message));
  };

  const resetCfgYaml = () => {
    fetch(API + "/config/yaml/reset", {method: "POST"})
      .then(r => r.json())
      .then(() => { loadCfgYaml(); reloadFormFromServer(); flash("Config reset to disk version"); })
      .catch(() => {});
  };

  const refreshCfgYamlFromUI = () => {
    fetch(API + "/config/yaml").then(r => r.text()).then(text => { setCfgYaml(text); setCfgYamlOrig(text); setCfgYamlErr(null); flash("Loaded current UI settings"); }).catch(() => {});
  };

  /* ─── Render ─── */
  if (!cfg) return <div style={{padding: 40, textAlign: "center", color: "var(--muted)"}}>{loadError ? <div style={{color: "#e74c3c"}}>{loadError}</div> : "Loading configuration..."}</div>;
  const {errors, warnings} = validate(cfg);
  const showParquet = cfg.format === "parquet" || cfg.format === "deltaparquet";
  const showDelta = cfg.format === "deltaparquet";

  /* Shorthand aliases used throughout JSX for brevity */
  const s = setConfigField;
  const mf = modelsForm;
  const sm = setModelField;

  /* ─── Config form sections (used in main page) ─── */
  const renderConfigForm = () => (<>
        {/* 1 OUTPUT */}
        <Section num="1" title="Output">
          <R2>
            <F label="Output format"><Sel value={cfg.format} onChange={v => s("format", v)} options={["parquet", "csv", "deltaparquet"]} labels={["Parquet", "CSV", "Delta (Parquet)"]} /></F>
            <F label="Sales output"><Sel value={cfg.salesOutput} onChange={v => s("salesOutput", v)} options={["sales", "sales_order", "both"]} labels={["Sales (flat)", "Order Header + Detail", "Both"]} /></F>
          </R2>
          <R3>
            <F label="Max lines per order"><N value={cfg.maxLinesPerOrder} onChange={v => s("maxLinesPerOrder", v)} min={1} max={20} step={1} /></F>
            <F label="Seed" help="Global random seed for reproducibility."><N value={cfg.seed} onChange={v => s("seed", v)} min={0} step={1} /></F>
            <F label=" "><div style={{display: "flex", flexDirection: "column", gap: 8, paddingTop: 16}}><Check checked={cfg.skipOrderCols} onChange={v => s("skipOrderCols", v)} label="Skip order columns" /><Check checked={cfg.salesOptimize} onChange={v => s("salesOptimize", v)} label="Optimize merged files" /><Check checked={cfg.qualityReport} onChange={v => s("qualityReport", v)} label="Quality report" /></div></F>
          </R3>
        </Section>

        {/* 2 DATES */}
        <Section num="2" title="Dates">
          <R3>
            <F label="Start date"><input type="date" style={iS} value={cfg.startDate} onChange={e => s("startDate", e.target.value)} /></F>
            <F label="End date"><input type="date" style={iS} value={cfg.endDate} onChange={e => s("endDate", e.target.value)} /></F>
            <F label="As-of date" help="Optional override for 'today' in lifecycle logic. Leave blank for system date."><input type="date" style={iS} value={cfg.asOfDate || ""} onChange={e => s("asOfDate", e.target.value)} /></F>
          </R3>
          <Box title="Calendar modes">
            <R4>
              <div style={{paddingTop: 2}}><Check checked={true} onChange={() => {}} label="Calendar" disabled /><div style={{fontSize: 10, color: "var(--muted)", marginTop: 2, marginLeft: 24}}>Always on</div></div>
              <Check checked={cfg.includeIso} onChange={v => s("includeIso", v)} label="ISO weeks" />
              <Check checked={cfg.includeFiscal} onChange={v => s("includeFiscal", v)} label="Fiscal" />
              <Check checked={cfg.includeWeeklyFiscal} onChange={v => s("includeWeeklyFiscal", v)} label="Weekly fiscal (4-4-5)" />
            </R4>
            {(cfg.includeFiscal || cfg.includeWeeklyFiscal) && <div style={{marginTop: 14}}><R3>
              <F label="Fiscal year starts"><Sel value={String(cfg.fiscalMonthOffset)} onChange={v => s("fiscalMonthOffset", parseInt(v))} options={MONTHS.map((_, i) => String(i))} labels={MONTHS} /></F>
              {cfg.includeWeeklyFiscal && <><F label="First day of week"><Sel value={String(cfg.wfFirstDay)} onChange={v => s("wfFirstDay", parseInt(v))} options={DAYS.map((_, i) => String(i))} labels={DAYS} /></F><F label="Quarter pattern"><Sel value={cfg.wfQuarterType} onChange={v => s("wfQuarterType", v)} options={["445", "454", "544"]} /></F></>}
            </R3></div>}
            {cfg.includeWeeklyFiscal && <div style={{marginTop: 10}}><F label="Type start fiscal year" help="Controls which fiscal year a week belongs to when it spans two years."><Sel value={String(cfg.wfTypeStartFiscalYear)} onChange={v => s("wfTypeStartFiscalYear", parseInt(v))} options={["1", "2"]} labels={["Type 1 (week belongs to year it starts in)", "Type 2 (week belongs to year it ends in)"]} /></F></div>}
          </Box>
        </Section>

        {/* 3 SCALE */}
        <Section num="3" title="Scale">
          <F label="Sales rows" help="Total rows to generate in the Sales fact table."><N value={cfg.salesRows} onChange={v => s("salesRows", v)} min={1} step={10000} /></F>
          <R4>
            <F label="Customers"><N value={cfg.customers} onChange={v => s("customers", v)} min={1} step={1000} /></F>
            <F label="Products"><N value={cfg.products} onChange={v => s("products", v)} min={1} step={500} /></F>
            <F label="Stores"><N value={cfg.stores} onChange={v => s("stores", v)} min={1} step={10} /></F>
            <F label="Promotions"><N value={cfg.promotions} onChange={v => s("promotions", v)} min={0} step={5} /></F>
          </R4>
        </Section>

        {/* 4 DIMENSIONS & FEATURES — tabbed panel */}
        <Section num="4" title="Dimensions & Features">
          <div style={{display: "grid", gridTemplateColumns: "repeat(7,1fr)", gap: 6, marginTop: 6, marginBottom: 4}}>
            {[
              ["customers", "Customers"], ["products", "Products"], ["stores", "Stores"], ["employees", "Employees"],
              ["returns", "Returns", cfg.returnsEnabled], ["promotions", "Promos"],
              ["subscriptions", "Subscriptions", cfg.subEnabled],
              ["exchange", "FX Rates"], ["budget", "Budget", cfg.budgetEnabled], ["inventory", "Inventory", cfg.inventoryEnabled],
              ["wishlists", "Wishlists", cfg.wlEnabled], ["complaints", "Complaints", cfg.ccEnabled],
            ].map(([tabKey, label, flag]) => {
              const active = dimTab === tabKey;
              return (
                <button key={tabKey} onClick={() => setDimTab(tabKey)}
                  style={{padding: "5px 0", borderRadius: 6, fontSize: 11.5, fontWeight: active ? 600 : 500, cursor: "pointer", fontFamily: "var(--sans)", transition: "all .12s", whiteSpace: "nowrap", display: "flex", alignItems: "center", justifyContent: "center", gap: 5, border: active ? "1px solid var(--accent)" : "1px solid var(--border)", background: active ? "var(--glow)" : "transparent", color: active ? "var(--accent)" : "var(--dim)"}}
                  onMouseOver={event => { if (!active) { event.currentTarget.style.background = "var(--alt)"; event.currentTarget.style.color = "var(--text)"; } }}
                  onMouseOut={event => { if (!active) { event.currentTarget.style.background = "transparent"; event.currentTarget.style.color = "var(--dim)"; } }}
                >
                  {label}
                  {flag !== undefined && <span style={{width: 6, height: 6, borderRadius: "50%", background: flag ? "var(--ok)" : "var(--muted)", flexShrink: 0}} />}
                </button>
              );
            })}
          </div>

          {/* ── Customers ── */}
          {dimTab === "customers" && <div style={{marginTop: 8}}>
            <Box title="Regional mix (% of customers)">
              <R4>
                <F label="Americas %"><N value={cfg.pctUs} onChange={v => s("pctUs", v)} min={0} max={100} /></F>
                <F label="Europe %"><N value={cfg.pctEu} onChange={v => s("pctEu", v)} min={0} max={100} /></F>
                <F label="India %"><N value={cfg.pctIndia} onChange={v => s("pctIndia", v)} min={0} max={100} /></F>
                <F label="APAC %"><N value={cfg.pctAsia} onChange={v => s("pctAsia", v)} min={0} max={100} /></F>
              </R4>
              <Hint>Sum: {cfg.pctIndia + cfg.pctUs + cfg.pctEu + cfg.pctAsia}% (auto-normalized)</Hint>
              <F label="Organization %" help="Percentage of customers that are organizations (B2B)."><N value={cfg.pctOrg} onChange={v => s("pctOrg", v)} min={0} max={100} step={1} /></F>
            </Box>
            <R2>
              <F label="Active ratio" help="Fraction of customers active during date range."><N value={cfg.customerActiveRatio} onChange={v => s("customerActiveRatio", v)} min={.1} max={1} step={.01} /></F>
              <F label="Household %" help="Fraction of customers assigned to households."><N value={cfg.householdPct} onChange={v => s("householdPct", v)} min={0} max={1} step={.05} /></F>
            </R2>
            <Box title="SCD Type 2 (Slowly Changing Dimension)">
              <Check checked={cfg.custScd2Enabled} onChange={v => s("custScd2Enabled", v)} label="Enable customer version history" />
              {cfg.custScd2Enabled && <R2>
                <F label="Change rate" help="Fraction of customers who get attribute changes per year."><N value={cfg.custScd2ChangeRate} onChange={v => s("custScd2ChangeRate", v)} min={0} max={1} step={0.05} /></F>
                <F label="Max versions" help="Maximum version rows per customer."><N value={cfg.custScd2MaxVersions} onChange={v => s("custScd2MaxVersions", v)} min={1} max={10} step={1} /></F>
              </R2>}
            </Box>
          </div>}

          {/* ── Products ── */}
          {dimTab === "products" && <div style={{marginTop: 8}}>
            <Box title="Pricing">
              <R3>
                <F label="Value scale" help="Multiplier on base product prices."><N value={cfg.valueScale} onChange={v => s("valueScale", v)} min={.01} max={10} step={.05} /></F>
                <F label="Min unit price"><N value={cfg.minPrice} onChange={v => s("minPrice", v)} min={0} step={10} /></F>
                <F label="Max unit price"><N value={cfg.maxPrice} onChange={v => s("maxPrice", v)} min={1} step={50} /></F>
              </R3>
              {cfg.maxPrice > cfg.minPrice && <Hint accent>Scaled range: ~{((cfg.minPrice || 0) * (cfg.valueScale || 1)).toLocaleString()} {"\u2192"} {((cfg.maxPrice || 0) * (cfg.valueScale || 1)).toLocaleString()}</Hint>}
            </Box>
            <Box title="Cost Margins">
              <R2>
                <F label="Min margin %" help="Minimum cost margin as a fraction (e.g. 0.20 = 20%)."><N value={cfg.marginMin} onChange={v => s("marginMin", v)} min={0} max={1} step={.01} /></F>
                <F label="Max margin %" help="Maximum cost margin as a fraction."><N value={cfg.marginMax} onChange={v => s("marginMax", v)} min={0} max={1} step={.01} /></F>
              </R2>
              <Hint>Margin range: {((cfg.marginMin || 0) * 100).toFixed(0)}% {"\u2013"} {((cfg.marginMax || 0) * 100).toFixed(0)}%</Hint>
            </Box>
            <Box title="Brand Normalization">
              <Check checked={cfg.brandNormalize} onChange={v => s("brandNormalize", v)} label="Pull brand prices toward global median" />
              {cfg.brandNormalize && <div style={{marginTop: 8}}><F label="Alpha (brand identity retention)" help="0 = full normalization, 1 = no normalization."><N value={cfg.brandNormalizeAlpha} onChange={v => s("brandNormalizeAlpha", v)} min={0} max={1} step={.05} /></F></div>}
            </Box>
            <F label="Active ratio" help="Fraction of products active during date range."><N value={cfg.productActiveRatio} onChange={v => s("productActiveRatio", v)} min={.1} max={1} step={.01} /></F>
            <Box title="SCD Type 2 (Price History)">
              <Check checked={cfg.prodScd2Enabled} onChange={v => s("prodScd2Enabled", v)} label="Enable product price version history" />
              {cfg.prodScd2Enabled && <R3>
                <F label="Revision frequency" help="Months between price revisions."><N value={cfg.prodScd2RevisionFreq} onChange={v => s("prodScd2RevisionFreq", v)} min={1} max={60} step={1} /></F>
                <F label="Price drift" help="Price change per revision (e.g. 0.05 = ~5%)."><N value={cfg.prodScd2PriceDrift} onChange={v => s("prodScd2PriceDrift", v)} min={0} max={0.5} step={0.01} /></F>
                <F label="Max versions" help="Maximum version rows per product."><N value={cfg.prodScd2MaxVersions} onChange={v => s("prodScd2MaxVersions", v)} min={1} max={10} step={1} /></F>
              </R3>}
            </Box>
          </div>}

          {/* ── Stores ── */}
          {dimTab === "stores" && <div style={{marginTop: 8}}>
            <div style={{marginBottom: 8}}><Check checked={cfg.storeEnsureIsoCoverage} onChange={v => s("storeEnsureIsoCoverage", v)} label="Ensure ISO country coverage" /></div>
            <Box title="Staffing ranges" help="Min/max staff per store type. Online stores always have 1.">
              {Object.entries(cfg.storeStaffingRanges || {}).map(([type, range]) => {
                const [lo, hi] = Array.isArray(range) ? range : [2, 6];
                const setRange = r => s("storeStaffingRanges", {...(cfg.storeStaffingRanges || {}), [type]: r});
                return <R3 key={type}>
                  <F label={type}>
                    <div style={{display: "flex", gap: 8, alignItems: "center"}}>
                      <N value={lo} onChange={v => setRange([v, hi])} min={1} step={1} />
                      <span style={{color: "var(--dim)"}}>to</span>
                      <N value={hi} onChange={v => setRange([lo, v])} min={1} step={1} />
                    </div>
                  </F>
                </R3>;
              })}
              {Object.keys(cfg.storeStaffingRanges || {}).length === 0 && <Hint>No staffing ranges configured. Stores use default staff counts.</Hint>}
            </Box>
            <Box title="Region weights" help="Fraction of stores per currency region. Omit for population-weighted distribution.">
              {Object.entries(cfg.storeRegionWeights || {}).map(([code, weight]) =>
                <R2 key={code}>
                  <F label={code}><N value={weight} onChange={v => s("storeRegionWeights", {...(cfg.storeRegionWeights || {}), [code]: v})} min={0} max={1} step={0.05} /></F>
                </R2>
              )}
              {Object.keys(cfg.storeRegionWeights || {}).length === 0 && <Hint>No region weights set. Stores distributed by population weighting.</Hint>}
            </Box>
            <Box title="Online stores">
              <F label="Online store count" help="Number of online stores carved from total store count."><N value={cfg.storeOnlineStores} onChange={v => s("storeOnlineStores", v)} min={0} step={1} /></F>
            </Box>
            <Box title="Store opening/closing dates">
              <R3>
                <F label="Opening start"><input type="date" style={iS} value={cfg.storeOpeningStart} onChange={e => s("storeOpeningStart", e.target.value)} /></F>
                <F label="Opening end"><input type="date" style={iS} value={cfg.storeOpeningEnd} onChange={e => s("storeOpeningEnd", e.target.value)} /></F>
                <F label="Closing end"><input type="date" style={iS} value={cfg.storeClosingEnd} onChange={e => s("storeClosingEnd", e.target.value)} /></F>
              </R3>
              <div style={{marginTop: 8}}><Check checked={cfg.storeClosingEnabled} onChange={v => s("storeClosingEnabled", v)} label="Enable store closures" /></div>
              {cfg.storeClosingEnabled && <F label="Close share" help="Fraction of physical stores that close."><N value={cfg.storeCloseShare} onChange={v => s("storeCloseShare", v)} min={0} max={1} step={.05} /></F>}
            </Box>
            <Box title="Assortment">
              <Check checked={cfg.storeAssortmentEnabled} onChange={v => s("storeAssortmentEnabled", v)} label="Enable product assortment filtering" />
              <Hint>When off, every store sells every product (cross-join).</Hint>
            </Box>
          </div>}

          {/* ── Employees ── */}
          {dimTab === "employees" && <div style={{marginTop: 8}}>
            <F label="Email domain"><input type="text" style={iS} value={cfg.employeeEmailDomain} onChange={e => s("employeeEmailDomain", e.target.value)} /></F>
            <Box title="Transfers" help="Inter-store staff transfers generate additional assignment rows.">
              <Check checked={cfg.transfersEnabled} onChange={v => s("transfersEnabled", v)} label="Enable inter-store transfers" />
              {cfg.transfersEnabled && <R3>
                <F label="Annual rate" help="Fraction of eligible staff that transfer per year."><N value={cfg.transfersAnnualRate} onChange={v => s("transfersAnnualRate", v)} min={0} max={1} step={0.05} /></F>
                <F label="Min tenure (months)" help="Minimum months at current store before eligible."><N value={cfg.transfersMinTenureMonths} onChange={v => s("transfersMinTenureMonths", v)} min={1} step={1} /></F>
                <F label="Same-region preference" help="Probability transfer stays within the same region."><N value={cfg.transfersSameRegionPref} onChange={v => s("transfersSameRegionPref", v)} min={0} max={1} step={0.05} /></F>
              </R3>}
            </Box>
          </div>}

          {/* ── Returns ── */}
          {dimTab === "returns" && <div style={{marginTop: 8}}>
            <div style={{marginTop: 4}}><Check checked={cfg.returnsEnabled} onChange={v => s("returnsEnabled", v)} label="Enable returns generation" /></div>
            {cfg.returnsEnabled && <R3>
              <F label="Return rate" help="Fraction of sales rows returned."><N value={cfg.returnRate} onChange={v => s("returnRate", v)} min={0} max={1} step={.005} /></F>
              <F label="Min days after sale"><N value={cfg.returnMinDays} onChange={v => s("returnMinDays", v)} min={1} step={1} /></F>
              <F label="Max days after sale"><N value={cfg.returnMaxDays} onChange={v => s("returnMaxDays", v)} min={1} step={5} /></F>
            </R3>}
          </div>}

          {/* ── Promotions ── */}
          {dimTab === "promotions" && <div style={{marginTop: 8}}>
            <Box title="Promotion counts by type">
              <R4>
                <F label="Seasonal" help="Seasonal discount promos per year window."><N value={cfg.promoSeasonal} onChange={v => s("promoSeasonal", v)} min={0} max={100} step={1} /></F>
                <F label="Clearance" help="Clearance sale promos (steep discounts)."><N value={cfg.promoClearance} onChange={v => s("promoClearance", v)} min={0} max={100} step={1} /></F>
                <F label="Limited Time" help="Limited-time offer promos."><N value={cfg.promoLimited} onChange={v => s("promoLimited", v)} min={0} max={100} step={1} /></F>
                <F label="Flash Sale" help="Short 1–2 day flash sales."><N value={cfg.promoFlash} onChange={v => s("promoFlash", v)} min={0} max={100} step={1} /></F>
              </R4>
              <R4>
                <F label="Volume" help="Volume/bulk discount promos."><N value={cfg.promoVolume} onChange={v => s("promoVolume", v)} min={0} max={100} step={1} /></F>
                <F label="Loyalty" help="Loyalty-exclusive promos."><N value={cfg.promoLoyalty} onChange={v => s("promoLoyalty", v)} min={0} max={100} step={1} /></F>
                <F label="Bundle" help="Bundle deal promos."><N value={cfg.promoBundle} onChange={v => s("promoBundle", v)} min={0} max={100} step={1} /></F>
                <F label="New Customer" help="New customer welcome promos."><N value={cfg.promoNewCustomer} onChange={v => s("promoNewCustomer", v)} min={0} max={100} step={1} /></F>
              </R4>
              <Hint>Total: {(cfg.promoSeasonal || 0) + (cfg.promoClearance || 0) + (cfg.promoLimited || 0) + (cfg.promoFlash || 0) + (cfg.promoVolume || 0) + (cfg.promoLoyalty || 0) + (cfg.promoBundle || 0) + (cfg.promoNewCustomer || 0)} promos (+ holidays auto-generated per year)</Hint>
            </Box>
            <F label="New customer window (months)" help="Months after CustomerStartDate where New Customer promo applies. 0 = same month only."><N value={cfg.promoNewCustWindow} onChange={v => s("promoNewCustWindow", v)} min={0} max={24} step={1} /></F>
          </div>}

          {/* ── Subscriptions ── */}
          {dimTab === "subscriptions" && <div style={{marginTop: 8}}>
            <div style={{marginTop: 4}}><Check checked={cfg.subEnabled} onChange={v => s("subEnabled", v)} label="Enable subscriptions" /></div>
            {cfg.subEnabled && <>
              <div style={{marginTop: 8}}><Check checked={cfg.subGenerateBridge} onChange={v => s("subGenerateBridge", v)} label="Generate bridge table" /></div>
              <R3>
                <F label="Participation rate"><N value={cfg.subParticipationRate} onChange={v => s("subParticipationRate", v)} min={0} max={1} step={0.05} /></F>
                <F label="Avg subscriptions"><N value={cfg.subAvgSubscriptions} onChange={v => s("subAvgSubscriptions", v)} min={1} step={0.5} /></F>
                <F label="Max subscriptions"><N value={cfg.subMaxSubscriptions} onChange={v => s("subMaxSubscriptions", v)} min={1} step={1} /></F>
              </R3>
              <R4>
                <F label="Churn rate" help="Probability of cancellation."><N value={cfg.subChurnRate} onChange={v => s("subChurnRate", v)} min={0} max={1} step={0.05} /></F>
                <F label="Trial rate" help="Fraction of subscriptions with free trial."><N value={cfg.subTrialRate} onChange={v => s("subTrialRate", v)} min={0} max={1} step={0.05} /></F>
                <F label="Trial days" help="Duration of free trial period."><N value={cfg.subTrialDays} onChange={v => s("subTrialDays", v)} min={1} max={90} step={1} /></F>
                <F label="Trial conversion" help="Fraction of trial users that convert to paid."><N value={cfg.subTrialConversionRate} onChange={v => s("subTrialConversionRate", v)} min={0} max={1} step={0.05} /></F>
              </R4>
              <F label="Seed"><N value={cfg.subSeed} onChange={v => s("subSeed", v)} min={0} step={1} /></F>
            </>}
          </div>}

          {/* ── Exchange Rates ── */}
          {dimTab === "exchange" && <div style={{marginTop: 8}}>
            <R2>
              <F label="From currencies" help="Comma-separated source currency codes (e.g. USD, CAD).">
                <input type="text" style={iS} value={(cfg.erFromCurrencies || []).join(", ")} onChange={e => s("erFromCurrencies", e.target.value.split(",").map(c => c.trim().toUpperCase()).filter(Boolean))} />
              </F>
              <F label="To currencies" help="Comma-separated target currency codes.">
                <input type="text" style={iS} value={(cfg.erToCurrencies || []).join(", ")} onChange={e => s("erToCurrencies", e.target.value.split(",").map(c => c.trim().toUpperCase()).filter(Boolean))} />
              </F>
            </R2>
            <R2>
              <F label="Base currency"><input type="text" style={iS} value={cfg.erBaseCurrency} onChange={e => s("erBaseCurrency", e.target.value.toUpperCase())} /></F>
              <F label="Future annual drift" help="Annual compounding rate for projected rates beyond today."><N value={cfg.erFutureDrift} onChange={v => s("erFutureDrift", v)} min={0} max={.5} step={.005} /></F>
            </R2>
            <div style={{marginTop: 10}}><Check checked={cfg.erIncludeMonthly} onChange={v => s("erIncludeMonthly", v)} label="Generate monthly exchange rates table" /></div>
          </div>}

          {/* ── Budget ── */}
          {dimTab === "budget" && <div style={{marginTop: 8}}>
            <div style={{marginTop: 4}}><Check checked={cfg.budgetEnabled} onChange={v => s("budgetEnabled", v)} label="Generate Budget fact table" /></div>
            {cfg.budgetEnabled && <R2>
              <F label="Default backcast growth"><N value={cfg.budgetDefaultGrowth} onChange={v => s("budgetDefaultGrowth", v)} min={-1} max={1} step={.01} /></F>
            </R2>}
          </div>}

          {/* ── Inventory ── */}
          {dimTab === "inventory" && <div style={{marginTop: 8}}>
            <div style={{marginTop: 4}}><Check checked={cfg.inventoryEnabled} onChange={v => s("inventoryEnabled", v)} label="Generate Inventory Snapshot fact table" /></div>
            {cfg.inventoryEnabled && <>
              <R2>
                <F label="Grain"><Sel value={cfg.inventoryGrain} onChange={v => s("inventoryGrain", v)} options={["monthly", "daily"]} /></F>
                <F label=" "><div style={{paddingTop: 16}}><Check checked={cfg.inventoryShrinkageEnabled} onChange={v => s("inventoryShrinkageEnabled", v)} label="Enable shrinkage" /></div></F>
              </R2>
              {cfg.inventoryShrinkageEnabled && <F label="Shrinkage rate" help="Annual shrinkage as a fraction (e.g. 0.02 = 2%)."><N value={cfg.inventoryShrinkageRate} onChange={v => s("inventoryShrinkageRate", v)} min={0} max={.1} step={.005} /></F>}
            </>}
          </div>}

          {/* ── Wishlists ── */}
          {dimTab === "wishlists" && <div style={{marginTop: 8}}>
            <div style={{marginTop: 4}}><Check checked={cfg.wlEnabled} onChange={v => s("wlEnabled", v)} label="Generate Customer Wishlists" /></div>
            {cfg.wlEnabled && <>
              <R3>
                <F label="Participation rate" help="Fraction of customers who have wishlists."><N value={cfg.wlParticipationRate} onChange={v => s("wlParticipationRate", v)} min={0} max={1} step={0.05} /></F>
                <F label="Avg items" help="Average wishlist items per customer."><N value={cfg.wlAvgItems} onChange={v => s("wlAvgItems", v)} min={1} step={0.5} /></F>
                <F label="Max items"><N value={cfg.wlMaxItems} onChange={v => s("wlMaxItems", v)} min={1} step={1} /></F>
              </R3>
              <R3>
                <F label="Pre-browse days" help="Days before purchase a product can be wishlisted."><N value={cfg.wlPreBrowseDays} onChange={v => s("wlPreBrowseDays", v)} min={1} step={10} /></F>
                <F label="Affinity strength" help="How strongly wishlists follow purchase history."><N value={cfg.wlAffinityStrength} onChange={v => s("wlAffinityStrength", v)} min={0} max={1} step={0.05} /></F>
                <F label="Conversion rate" help="Fraction of wishlist items eventually purchased."><N value={cfg.wlConversionRate} onChange={v => s("wlConversionRate", v)} min={0} max={1} step={0.05} /></F>
              </R3>
              <F label="Seed"><N value={cfg.wlSeed} onChange={v => s("wlSeed", v)} min={0} step={1} /></F>
            </>}
          </div>}

          {/* ── Complaints ── */}
          {dimTab === "complaints" && <div style={{marginTop: 8}}>
            <div style={{marginTop: 4}}><Check checked={cfg.ccEnabled} onChange={v => s("ccEnabled", v)} label="Generate Customer Complaints" /></div>
            {cfg.ccEnabled && <>
              <R3>
                <F label="Complaint rate" help="Fraction of orders that generate a complaint."><N value={cfg.ccComplaintRate} onChange={v => s("ccComplaintRate", v)} min={0} max={1} step={0.005} /></F>
                <F label="Repeat rate" help="Fraction of complainers who complain again."><N value={cfg.ccRepeatComplaintRate} onChange={v => s("ccRepeatComplaintRate", v)} min={0} max={1} step={0.05} /></F>
                <F label="Max complaints"><N value={cfg.ccMaxComplaints} onChange={v => s("ccMaxComplaints", v)} min={1} step={1} /></F>
              </R3>
              <R3>
                <F label="Resolution rate" help="Fraction of complaints that get resolved."><N value={cfg.ccResolutionRate} onChange={v => s("ccResolutionRate", v)} min={0} max={1} step={0.05} /></F>
                <F label="Escalation rate" help="Fraction of complaints that get escalated."><N value={cfg.ccEscalationRate} onChange={v => s("ccEscalationRate", v)} min={0} max={1} step={0.05} /></F>
                <F label="Seed"><N value={cfg.ccSeed} onChange={v => s("ccSeed", v)} min={0} step={1} /></F>
              </R3>
              <R3>
                <F label="Avg response days"><N value={cfg.ccAvgResponseDays} onChange={v => s("ccAvgResponseDays", v)} min={1} step={1} /></F>
                <F label="Max response days"><N value={cfg.ccMaxResponseDays} onChange={v => s("ccMaxResponseDays", v)} min={1} step={1} /></F>
                <F label=" " />
              </R3>
            </>}
          </div>}
        </Section>

        {/* 5 ADVANCED */}
        <Section num="5" title="Advanced" defaultOpen={false}>
          <Box title="Pipeline tuning">
            <R3>
              <F label="Chunk size" help="Rows per parallel chunk."><N value={cfg.chunkSize} onChange={v => s("chunkSize", v)} min={10000} step={100000} /></F>
              <F label="Workers"><div style={{display: "flex", alignItems: "center", gap: 10}}><Check checked={cfg.autoWorkers} onChange={v => { s("autoWorkers", v); if (v) s("workers", 0); }} label="Auto" />{!cfg.autoWorkers && <N value={cfg.workers} onChange={v => s("workers", v)} min={1} max={32} style={{width: 100}} />}</div></F>
              <F label=" " />
            </R3>
          </Box>
          {showParquet && <Box title="Parquet options">
            <R3>
              <F label="Compression"><Sel value={cfg.compression} onChange={v => s("compression", v)} options={["snappy", "zstd", "gzip", "none"]} /></F>
              <F label="Row group size"><N value={cfg.rowGroupSize} onChange={v => s("rowGroupSize", v)} min={100000} step={500000} /></F>
              <F label=" "><div style={{display: "flex", flexDirection: "column", gap: 8, paddingTop: 3}}><Check checked={cfg.mergeParquet} onChange={v => s("mergeParquet", v)} label="Merge parquet chunks" />{showDelta && <Check checked={cfg.partitionEnabled} onChange={v => s("partitionEnabled", v)} label="Partition by Year/Month" />}</div></F>
            </R3>
          </Box>}
        </Section>

  </>);

  const sidebarWidth = sidebarOpen ? 360 : 0;

  return (
    <div className="app-layout" style={{display: "flex", height: "100vh", overflow: "hidden"}}>
      {toast && <div style={{position: "fixed", top: 16, right: 16, zIndex: 999, background: toast.includes("failed") ? "var(--err)" : "var(--accent)", color: "#fff", padding: "9px 20px", borderRadius: 8, fontSize: 13, fontWeight: 600, boxShadow: "0 6px 24px var(--shadow)", animation: "slideIn .28s ease"}}>{toast}</div>}

      {/* ═══ SIDEBAR ═══ */}
      <div className="app-sidebar" style={{width: sidebarWidth, flexShrink: 0, height: "100vh", background: "var(--surface)", borderRight: sidebarOpen ? "1px solid var(--border)" : "none", overflowY: "auto", overflowX: "hidden", transition: "width .2s", position: "sticky", top: 0}}>
        {sidebarOpen && <div style={{padding: 20, width: 360}}>
        <div style={{display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10}}>
          <div style={{fontSize: 10.5, fontWeight: 700, color: "var(--muted)", letterSpacing: ".09em", textTransform: "uppercase"}}>Presets</div>
          <div style={{display: "flex", gap: 6}}><ThemeToggle /><button onClick={() => setSidebarOpen(false)} title="Collapse sidebar" style={{width: 32, height: 32, borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--dim)", fontSize: 14, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center"}} onMouseOver={event => { event.currentTarget.style.borderColor = "var(--accent)"; }} onMouseOut={event => { event.currentTarget.style.borderColor = "var(--border)"; }}>{"\u2039"}</button></div>
        </div>
        {Object.keys(presets).length > 0 ? (<>
          <Sel value={presetBucket} onChange={setPresetBucket} options={Object.keys(presets)} />
          <div style={{marginTop: 8, display: "flex", flexDirection: "column", gap: 4}}>{Object.entries(presets[presetBucket] || {}).map(([name, presetData]) => {
            const isSelected = selectedPreset === name;
            const preset = presetData || {};
            const customerCount = preset.customers || preset.total_customers;
            const productCount = preset.products || preset.num_products;
            const rowCount = preset.sales_rows || preset.total_rows;
            return (
              <button key={name} onClick={() => applyPreset(name)}
                onMouseEnter={() => setPreviewPreset(name)}
                onMouseLeave={() => setPreviewPreset(null)}
                style={{display: "flex", flexDirection: "column", alignItems: "flex-start", width: "100%", padding: "9px 12px", borderRadius: 8, cursor: "pointer", border: `1px solid ${isSelected ? "var(--accent)" : "var(--border)"}`, background: isSelected ? "var(--glow)" : "var(--surface)", fontFamily: "var(--sans)", transition: "all .12s", gap: 4, position: "relative"}}>
                <span style={{fontSize: 12.5, fontWeight: isSelected ? 600 : 400, color: isSelected ? "var(--accent)" : "var(--text)", textAlign: "left", lineHeight: 1.35}}>{name}</span>
                {(customerCount || productCount || rowCount) && <span style={{display: "flex", gap: 5}}>
                  {rowCount && <span style={{fontSize: 10, padding: "1px 6px", borderRadius: 4, background: isSelected ? "rgba(79,91,213,.12)" : "var(--alt)", color: isSelected ? "var(--accent)" : "var(--muted)", fontFamily: "var(--mono)", fontWeight: 500}}>{rowCount >= 1000000 ? (rowCount / 1000000).toFixed(1) + "M" : (rowCount / 1000).toFixed(rowCount % 1000 ? 1 : 0) + "K"} rows</span>}
                  {customerCount && <span style={{fontSize: 10, padding: "1px 6px", borderRadius: 4, background: isSelected ? "rgba(79,91,213,.12)" : "var(--alt)", color: isSelected ? "var(--accent)" : "var(--muted)", fontFamily: "var(--mono)", fontWeight: 500}}>{(customerCount / 1000).toFixed(customerCount % 1000 ? 1 : 0)}K cust</span>}
                  {productCount && <span style={{fontSize: 10, padding: "1px 6px", borderRadius: 4, background: isSelected ? "rgba(79,91,213,.12)" : "var(--alt)", color: isSelected ? "var(--accent)" : "var(--muted)", fontFamily: "var(--mono)", fontWeight: 500}}>{(productCount / 1000).toFixed(productCount % 1000 ? 1 : 0)}K prod</span>}
                </span>}
              </button>
            );
          })}</div>
        </>) : (<div style={{fontSize: 12, color: "var(--muted)"}}>No presets loaded</div>)}

        {/* Preset preview tooltip */}
        {previewPreset && presets[presetBucket] && presets[presetBucket][previewPreset] && (() => {
          const presetData = presets[presetBucket][previewPreset];
          const changes = [];
          if (presetData.sales_rows || presetData.total_rows) { const val = presetData.sales_rows || presetData.total_rows; if (val !== cfg.salesRows) changes.push({k: "Sales rows", from: cfg.salesRows.toLocaleString(), to: val.toLocaleString()}); }
          if (presetData.customers || presetData.total_customers) { const val = presetData.customers || presetData.total_customers; if (val !== cfg.customers) changes.push({k: "Customers", from: cfg.customers.toLocaleString(), to: val.toLocaleString()}); }
          if (presetData.products || presetData.num_products) { const val = presetData.products || presetData.num_products; if (val !== cfg.products) changes.push({k: "Products", from: cfg.products.toLocaleString(), to: val.toLocaleString()}); }
          if (presetData.stores && presetData.stores !== cfg.stores) changes.push({k: "Stores", from: String(cfg.stores), to: String(presetData.stores)});
          if (changes.length === 0) return null;
          return (
            <div style={{marginTop: 8, padding: "8px 10px", borderRadius: 6, background: "var(--alt)", border: "1px solid var(--border)", fontSize: 11, animation: "fadeIn .15s ease"}}>
              <div style={{fontWeight: 600, color: "var(--dim)", marginBottom: 4}}>Preview changes:</div>
              {changes.map(change => (
                <div key={change.k} style={{display: "flex", justifyContent: "space-between", padding: "2px 0", color: "var(--text)"}}>
                  <span>{change.k}</span>
                  <span><span style={{color: "var(--err)", textDecoration: "line-through"}}>{change.from}</span>{" \u2192 "}<span style={{color: "var(--ok)", fontWeight: 600}}>{change.to}</span></span>
                </div>
              ))}
            </div>
          );
        })()}

        <div style={{borderTop: "1px solid var(--border)", margin: "20px 0"}} />
        <div style={{fontSize: 10.5, fontWeight: 700, color: "var(--muted)", letterSpacing: ".09em", textTransform: "uppercase", marginBottom: 10}}>Configuration</div>
        <div style={{display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 6}}>
          <button onClick={() => setPage(page === "models" ? "main" : "models")} style={{padding: "9px 10px", borderRadius: 8, border: `1px solid ${page === "models" ? "var(--accent)" : "var(--border)"}`, background: page === "models" ? "var(--glow)" : "var(--surface)", color: page === "models" ? "var(--accent)" : "var(--dim)", fontSize: 12, fontWeight: page === "models" ? 600 : 500, cursor: "pointer", fontFamily: "var(--sans)", transition: "all .15s", display: "flex", alignItems: "center", justifyContent: "center", gap: 6}}>
            <span>Models</span>
            {(modelsDirty || modelsApplied) && <span style={{width: 7, height: 7, borderRadius: "50%", background: modelsDirty ? "var(--warn)" : "var(--accent)", flexShrink: 0}} />}
          </button>
          <button onClick={() => setPage(page === "config" ? "main" : "config")} style={{padding: "9px 10px", borderRadius: 8, border: `1px solid ${page === "config" ? "var(--accent)" : "var(--border)"}`, background: page === "config" ? "var(--glow)" : "var(--surface)", color: page === "config" ? "var(--accent)" : "var(--dim)", fontSize: 12, fontWeight: page === "config" ? 600 : 500, cursor: "pointer", fontFamily: "var(--sans)", transition: "all .15s", display: "flex", alignItems: "center", justifyContent: "center", gap: 6}}>
            <span>Config</span>
            {cfgYamlDirty && <span style={{width: 7, height: 7, borderRadius: "50%", background: "var(--warn)", flexShrink: 0}} />}
          </button>
        </div>
        {page === "models" && <div style={{fontSize: 11, color: "var(--muted)", marginBottom: 4}}>Editing models.yaml in memory</div>}
        {page === "config" && <div style={{fontSize: 11, color: "var(--muted)", marginBottom: 4}}>Editing config.yaml in memory</div>}
        <button onClick={() => {
          Promise.all([
            fetch(API + "/config/yaml").then(r => r.text()),
            fetch(API + "/models").then(r => r.text()),
          ]).then(([cfgText, modelsText]) => {
            const downloadFile = (content, name, type) => { const blob = new Blob([content], {type}); const anchor = document.createElement("a"); anchor.href = URL.createObjectURL(blob); anchor.download = name; anchor.click(); URL.revokeObjectURL(anchor.href); };
            downloadFile(cfgText, "config.yaml", "text/yaml");
            setTimeout(() => downloadFile(modelsText, "models.yaml", "text/yaml"), 100);
          }).catch(() => {});
        }} style={{width: "100%", padding: "7px 12px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--dim)", fontSize: 11.5, cursor: "pointer", fontFamily: "var(--sans)"}}>Download configs</button>

        <div style={{borderTop: "1px solid var(--border)", margin: "20px 0"}} />
        <div style={{fontSize: 10.5, fontWeight: 700, color: "var(--muted)", letterSpacing: ".09em", textTransform: "uppercase", marginBottom: 10}}>Data</div>
        <button onClick={() => setPage(page === "data" ? "main" : "data")} style={{width: "100%", padding: "9px 12px", borderRadius: 8, border: `1px solid ${page === "data" ? "var(--accent)" : "var(--border)"}`, background: page === "data" ? "var(--glow)" : "var(--surface)", color: page === "data" ? "var(--accent)" : "var(--dim)", fontSize: 12.5, fontWeight: page === "data" ? 600 : 500, cursor: "pointer", fontFamily: "var(--sans)", transition: "all .15s", display: "flex", alignItems: "center", justifyContent: "space-between"}}>
          <span>Data Preview</span>
        </button>
        {page === "data" && <div style={{fontSize: 11, color: "var(--muted)", marginTop: 6}}>Browse generated datasets</div>}
        <button onClick={() => setPage(page === "import" ? "main" : "import")} style={{width: "100%", padding: "9px 12px", borderRadius: 8, border: `1px solid ${page === "import" ? "var(--accent)" : "var(--border)"}`, background: page === "import" ? "var(--glow)" : "var(--surface)", color: page === "import" ? "var(--accent)" : "var(--dim)", fontSize: 12.5, fontWeight: page === "import" ? 600 : 500, cursor: "pointer", fontFamily: "var(--sans)", transition: "all .15s", display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 6}}>
          <span>SQL Server Import</span>
        </button>
        {page === "import" && <div style={{fontSize: 11, color: "var(--muted)", marginTop: 6}}>Import CSV data into SQL Server</div>}

        {/* Regenerate Dimensions */}
        {cfg && (() => {
          const regenCount = cfg.regenAll ? DIMS.length : DIMS.filter(d => cfg.regenDims[d]).length;
          return <>
          <div style={{borderTop: "1px solid var(--border)", margin: "20px 0"}} />
          <button onClick={() => setRegenOpen(!regenOpen)} style={{width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between", padding: "9px 12px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--dim)", fontSize: 12.5, fontWeight: 500, cursor: "pointer", fontFamily: "var(--sans)", transition: "all .12s"}}>
            <span style={{display: "flex", alignItems: "center", gap: 8}}>
              <span style={{fontSize: 10.5, fontWeight: 700, color: "var(--muted)", letterSpacing: ".09em", textTransform: "uppercase"}}>Regenerate</span>
              {regenCount > 0 && <span style={{padding: "1px 7px", borderRadius: 10, background: "var(--accent)", color: "#fff", fontSize: 10, fontWeight: 600}}>{cfg.regenAll ? "all" : regenCount}</span>}
            </span>
            <span style={{fontSize: 11, color: "var(--muted)", transition: "transform .15s", transform: regenOpen ? "rotate(180deg)" : "none"}}>{"\u25BE"}</span>
          </button>
          {regenOpen && <div style={{padding: "10px 12px", marginTop: 6, borderRadius: 8, border: "1px solid var(--border)", background: "var(--alt)"}}>
            <div style={{marginBottom: 8}}><Check checked={cfg.regenAll} onChange={v => s("regenAll", v)} label="All dimensions" /></div>
            <div style={{display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4}}>{DIMS.map(dim => <Check key={dim} checked={cfg.regenAll || !!cfg.regenDims[dim]} onChange={v => s("regenDims", {...cfg.regenDims, [dim]: v})} label={dim.replace("_", " ")} disabled={cfg.regenAll} />)}</div>
          </div>}
          </>;
        })()}

        {/* Run history */}
        {runHistory.length > 0 && <>
          <div style={{borderTop: "1px solid var(--border)", margin: "20px 0"}} />
          <div style={{fontSize: 10.5, fontWeight: 700, color: "var(--muted)", letterSpacing: ".09em", textTransform: "uppercase", marginBottom: 8}}>Run History</div>
          {runHistory.map((run, idx) => (
            <div key={idx} className="run-history-item" style={{display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", borderRadius: 6, fontSize: 11.5, marginBottom: 2}}>
              <span style={{width: 7, height: 7, borderRadius: "50%", background: run.status === "done" ? "var(--ok)" : "var(--err)", flexShrink: 0}} />
              <span style={{color: "var(--text)", fontFamily: "var(--mono)", flex: 1}}>{run.rows >= 1000000 ? (run.rows / 1000000).toFixed(1) + "M" : (run.rows / 1000).toFixed(run.rows % 1000 ? 1 : 0) + "K"} rows</span>
              <span style={{color: "var(--accent)", fontFamily: "var(--mono)"}}>{run.elapsed < 60 ? run.elapsed.toFixed(1) + "s" : Math.floor(run.elapsed / 60) + "m " + Math.floor(run.elapsed % 60) + "s"}</span>
              <span style={{color: "var(--muted)", fontSize: 10}}>{run.time}</span>
            </div>
          ))}
        </>}
        </div>}
      </div>

      {/* Sidebar collapsed toggle */}
      {!sidebarOpen && <div style={{position: "fixed", top: 12, left: 12, zIndex: 100, display: "flex", gap: 6}}>
        <button onClick={() => setSidebarOpen(true)} title="Open sidebar" style={{width: 36, height: 36, borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--dim)", fontSize: 16, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "0 2px 8px var(--shadow)"}} onMouseOver={event => { event.currentTarget.style.borderColor = "var(--accent)"; }} onMouseOut={event => { event.currentTarget.style.borderColor = "var(--border)"; }}>{"\u203A"}</button>
        <ThemeToggle />
      </div>}

      {/* ═══ MAIN ═══ */}
      <div className="main-content" style={{flex: 1, overflowY: "auto", display: "flex", flexDirection: "column"}}>
      <div style={{flex: 1, padding: "28px 40px", paddingBottom: 28, maxWidth: (page === "data" || page === "import") ? "none" : 1120}}>

        {page === "models" ? (
        /* ═══ MODELS EDITOR PAGE ═══ */
        <div>
          <button onClick={() => setPage("main")} style={{display: "inline-flex", alignItems: "center", gap: 6, padding: "6px 14px", borderRadius: 7, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--dim)", fontSize: 12.5, cursor: "pointer", fontFamily: "var(--sans)", marginBottom: 18, transition: "all .12s"}} onMouseOver={event => event.currentTarget.style.borderColor = "var(--accent)"} onMouseOut={event => event.currentTarget.style.borderColor = "var(--border)"}>
            {"\u2190"} Back to configuration
          </button>
          <h1 style={{fontSize: 22, fontWeight: 700, letterSpacing: "-.02em", marginBottom: 4}}>Models Configuration</h1>
          <p style={{fontSize: 13, color: "var(--dim)", marginTop: 3, marginBottom: 18}}>
            Sales behavior models {"\u2014"} basket size, pricing, brand popularity, and returns. Changes are in-memory only.
          </p>

          {/* Tab bar */}
          <div style={{display: "flex", gap: 0, marginBottom: 18, borderBottom: "2px solid var(--border)"}}>
            {[["visual", "Visual"], ["yaml", "YAML"]].map(([tabKey, label]) => (
              <button key={tabKey} onClick={() => setModelsTab(tabKey)} style={{padding: "9px 22px", fontSize: 13, fontWeight: modelsTab === tabKey ? 600 : 400, color: modelsTab === tabKey ? "var(--accent)" : "var(--dim)", background: "none", border: "none", borderBottom: modelsTab === tabKey ? "2px solid var(--accent)" : "2px solid transparent", marginBottom: -2, cursor: "pointer", fontFamily: "var(--sans)", transition: "all .12s"}}>{label}</button>
            ))}
          </div>

          {modelsTab === "yaml" ? (
            <YamlEditor value={modelsYaml} onChange={setModelsYaml} filename="models.yaml" dirty={modelsDirty} applied={modelsApplied} error={modelsErr} onApply={saveModels} onReset={resetModels} />
          ) : mf ? (
            <div>
            {/* ── Macro Demand ── */}
            <Section num="M" title="Macro Demand" defaultOpen={true}>
              <R2>
                <F label="Mode" help="'once' plays factors once, 'repeat' loops them."><Sel value={mf.demandMode} onChange={v => sm("demandMode", v)} options={["once", "repeat"]} /></F>
                <F label="Year-level factors" help="Comma-separated demand multipliers per year.">
                  <input type="text" style={iS} value={(mf.demandFactors || []).join(", ")} onChange={e => sm("demandFactors", e.target.value.split(",").map(x => parseFloat(x.trim())).filter(x => !isNaN(x)))} />
                </F>
              </R2>
              {mf.demandFactors && mf.demandFactors.length > 0 && <div style={{marginTop: 10, display: "flex", alignItems: "flex-end", gap: 2, height: 60}}>
                {mf.demandFactors.map((factor, idx) => { const maxFactor = Math.max(...mf.demandFactors); return (
                  <div key={idx} style={{flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 2}}>
                    <div style={{width: "100%", background: "var(--accent)", borderRadius: "3px 3px 0 0", height: Math.max(4, factor / maxFactor * 48), opacity: .7 + .3 * (factor / maxFactor), transition: "height .2s"}} />
                    <span style={{fontSize: 9, color: "var(--muted)", fontFamily: "var(--mono)"}}>{factor.toFixed(1)}</span>
                  </div>
                ); })}
              </div>}
            </Section>

            {/* ── Quantity (basket size) ── */}
            <Section num="Q" title="Quantity (Basket Size)">
              <R3>
                <F label="Poisson lambda" help="Base expected items per order."><N value={mf.qtyLambda} onChange={v => sm("qtyLambda", v)} min={0.1} max={10} step={.1} /></F>
                <F label="Min qty"><N value={mf.qtyMin} onChange={v => sm("qtyMin", v)} min={1} step={1} /></F>
                <F label="Max qty"><N value={mf.qtyMax} onChange={v => sm("qtyMax", v)} min={1} step={1} /></F>
              </R3>
              <F label="Noise sigma" help="Random noise added to quantity (std dev)."><N value={mf.qtyNoise} onChange={v => sm("qtyNoise", v)} min={0} max={.5} step={.01} /></F>
              <Box title="Monthly seasonality factors">
                <div style={{display: "grid", gridTemplateColumns: "repeat(12,1fr)", gap: 6}}>
                  {MONTHS.map((month, idx) => (
                    <div key={month} style={{textAlign: "center"}}>
                      <div style={{fontSize: 10, color: "var(--muted)", marginBottom: 3}}>{month}</div>
                      <input type="number" style={{...iS, padding: "5px 3px", textAlign: "center", fontSize: 12}} value={(mf.qtyMonthly || [])[idx] || 1} min={.5} max={2} step={.01}
                        onChange={e => { const arr = [...(mf.qtyMonthly || Array(12).fill(1))]; arr[idx] = parseFloat(e.target.value) || 1; sm("qtyMonthly", arr); }} />
                    </div>
                  ))}
                </div>
              </Box>
            </Section>

            {/* ── Pricing ── */}
            <Section num="P" title="Pricing" defaultOpen={false}>
              <Box title="Inflation">
                <R3>
                  <F label="Annual rate"><N value={mf.inflationRate} onChange={v => sm("inflationRate", v)} min={0} max={.5} step={.005} /></F>
                  <F label="Month volatility"><N value={mf.inflationVolatility} onChange={v => sm("inflationVolatility", v)} min={0} max={.1} step={.001} /></F>
                  <F label="Factor clip range">
                    <div style={{display: "flex", gap: 6, alignItems: "center"}}>
                      <N value={mf.inflationClipMin} onChange={v => sm("inflationClipMin", v)} min={.5} max={2} step={.01} style={{width: 80}} />
                      <span style={{color: "var(--muted)"}}>–</span>
                      <N value={mf.inflationClipMax} onChange={v => sm("inflationClipMax", v)} min={.5} max={3} step={.01} style={{width: 80}} />
                    </div>
                  </F>
                </R3>
              </Box>
              <Box title="Markdown">
                <Check checked={mf.markdownEnabled} onChange={v => sm("markdownEnabled", v)} label="Enable markdowns" />
                {mf.markdownEnabled && <div style={{marginTop: 10}}><R3>
                  <F label="Max % of price"><N value={mf.markdownMaxPct} onChange={v => sm("markdownMaxPct", v)} min={0} max={1} step={.05} /></F>
                  <F label="Min net price"><N value={mf.markdownMinNet} onChange={v => sm("markdownMinNet", v)} min={0} step={.01} /></F>
                  <F label=" "><div style={{paddingTop: 16}}><Check checked={mf.markdownAllowNeg} onChange={v => sm("markdownAllowNeg", v)} label="Allow negative margin" /></div></F>
                </R3></div>}
                {mf.markdownEnabled && mf.markdownLadder && mf.markdownLadder.length > 0 && <Box title="Discount ladder">
                  <div style={{display: "grid", gridTemplateColumns: "60px 80px 1fr 50px", gap: "4px 10px", alignItems: "center", fontSize: 12}}>
                    <span style={{fontWeight: 600, color: "var(--muted)", fontSize: 10}}>KIND</span>
                    <span style={{fontWeight: 600, color: "var(--muted)", fontSize: 10}}>VALUE</span>
                    <span style={{fontWeight: 600, color: "var(--muted)", fontSize: 10}}>WEIGHT BAR</span>
                    <span style={{fontWeight: 600, color: "var(--muted)", fontSize: 10, textAlign: "right"}}>WT</span>
                    {mf.markdownLadder.map((ladderStep, idx) => (
                      <React.Fragment key={idx}>
                        <span style={{fontFamily: "var(--mono)", color: "var(--dim)"}}>{ladderStep.kind}</span>
                        <span style={{fontFamily: "var(--mono)", color: "var(--text)"}}>{ladderStep.kind === "none" ? "—" : "$" + ladderStep.value}</span>
                        <div style={{height: 14, background: "var(--border)", borderRadius: 3, overflow: "hidden"}}><div style={{height: "100%", width: `${ladderStep.weight * 100 / .35 * 100}%`, maxWidth: "100%", background: "var(--accent)", borderRadius: 3, opacity: .7}} /></div>
                        <span style={{fontFamily: "var(--mono)", color: "var(--accent)", textAlign: "right"}}>{(ladderStep.weight * 100).toFixed(0)}%</span>
                      </React.Fragment>
                    ))}
                  </div>
                  <div style={{fontSize: 10.5, color: "var(--muted)", marginTop: 6}}>Edit ladder values in the YAML tab.</div>
                </Box>}
              </Box>
            </Section>

            {/* ── Brand Popularity ── */}
            <Section num="B" title="Brand Popularity" defaultOpen={false}>
              <Check checked={mf.brandEnabled} onChange={v => sm("brandEnabled", v)} label="Enable brand popularity rotation" />
              {mf.brandEnabled && <>
                <R2>
                  <F label="Winner boost" help="Multiplier for the 'winning' brand each year."><N value={mf.brandWinnerBoost} onChange={v => sm("brandWinnerBoost", v)} min={1} max={10} step={.1} /></F>
                  <F label="Seed"><N value={mf.brandSeed} onChange={v => sm("brandSeed", v)} min={0} step={1} /></F>
                </R2>
                {mf.brandWeights && Object.keys(mf.brandWeights).length > 0 && <Box title="Brand weight overrides">
                  {Object.entries(mf.brandWeights).map(([brand, weight]) => (
                    <div key={brand} style={{display: "flex", alignItems: "center", gap: 10, marginBottom: 6}}>
                      <span style={{fontSize: 13, width: 160, color: "var(--text)"}}>{brand}</span>
                      <input type="range" min={0} max={1} step={.01} value={weight} onChange={event => { const newWeights = {...mf.brandWeights, [brand]: parseFloat(event.target.value)}; sm("brandWeights", newWeights); }} style={{flex: 1, accentColor: "var(--accent)"}} />
                      <span style={{fontSize: 12, fontFamily: "var(--mono)", color: "var(--accent)", width: 42, textAlign: "right"}}>{(weight * 100).toFixed(0)}%</span>
                    </div>
                  ))}
                </Box>}
              </>}
            </Section>

            {/* ── Returns ── */}
            <Section num="R" title="Returns" defaultOpen={false}>
              <Check checked={mf.retEnabled} onChange={v => sm("retEnabled", v)} label="Enable return modeling" />
              {mf.retEnabled && <>
                <R3>
                  <F label="Lag distribution"><Sel value={mf.retLagDist} onChange={v => sm("retLagDist", v)} options={["triangular", "uniform", "normal"]} /></F>
                  <F label="Lag mode (days)" help="Peak of the triangular distribution."><N value={mf.retLagMode} onChange={v => sm("retLagMode", v)} min={1} max={90} step={1} /></F>
                  <F label="Full-line return %"><N value={mf.retFullLinePct} onChange={v => sm("retFullLinePct", v)} min={0} max={1} step={.01} /></F>
                </R3>
                {mf.retReasons && mf.retReasons.length > 0 && <Box title="Return reasons">
                  <div style={{display: "grid", gridTemplateColumns: "30px 1fr 1fr 50px", gap: "4px 10px", alignItems: "center", fontSize: 12}}>
                    <span style={{fontWeight: 600, color: "var(--muted)", fontSize: 10}}>KEY</span>
                    <span style={{fontWeight: 600, color: "var(--muted)", fontSize: 10}}>REASON</span>
                    <span style={{fontWeight: 600, color: "var(--muted)", fontSize: 10}}>WEIGHT</span>
                    <span style={{fontWeight: 600, color: "var(--muted)", fontSize: 10, textAlign: "right"}}>%</span>
                    {mf.retReasons.map((reason, idx) => (
                      <React.Fragment key={idx}>
                        <span style={{fontFamily: "var(--mono)", color: "var(--muted)"}}>{reason.key}</span>
                        <span style={{color: "var(--text)"}}>{reason.label}</span>
                        <div style={{height: 14, background: "var(--border)", borderRadius: 3, overflow: "hidden"}}><div style={{height: "100%", width: `${reason.weight * 100 / .28 * 100}%`, maxWidth: "100%", background: "var(--accent)", borderRadius: 3, opacity: .7}} /></div>
                        <span style={{fontFamily: "var(--mono)", color: "var(--accent)", textAlign: "right"}}>{(reason.weight * 100).toFixed(0)}%</span>
                      </React.Fragment>
                    ))}
                  </div>
                  <div style={{fontSize: 10.5, color: "var(--muted)", marginTop: 6}}>Edit reasons in the YAML tab.</div>
                </Box>}
              </>}
            </Section>
            </div>
          ) : (<div style={{padding: 40, textAlign: "center", color: "var(--muted)"}}>Loading models...</div>)}
        </div>

        ) : page === "data" ? (
        /* ═══ DATA PREVIEW PAGE ═══ */
        <DataPreview onBack={() => setPage("main")} />

        ) : page === "import" ? (
        /* ═══ SQL IMPORT PAGE ═══ */
        <SqlImport onBack={() => setPage("main")} />

        ) : page === "config" ? (
        /* ═══ CONFIG YAML EDITOR PAGE ═══ */
        <div>
          <button onClick={() => setPage("main")} style={{display: "inline-flex", alignItems: "center", gap: 6, padding: "6px 14px", borderRadius: 7, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--dim)", fontSize: 12.5, cursor: "pointer", fontFamily: "var(--sans)", marginBottom: 18, transition: "all .12s"}} onMouseOver={event => event.currentTarget.style.borderColor = "var(--accent)"} onMouseOut={event => event.currentTarget.style.borderColor = "var(--border)"}>
            {"\u2190"} Back to configuration
          </button>
          <h1 style={{fontSize: 22, fontWeight: 700, letterSpacing: "-.02em", marginBottom: 6}}>Config YAML</h1>
          <p style={{fontSize: 13, color: "var(--dim)", marginTop: 0, marginBottom: 16, lineHeight: 1.5}}>
            Edit the full config below. Applying here will override any preset or form settings. The file on disk is never modified.
          </p>
          {cfgYamlDirty && <div style={{padding: "9px 14px", marginBottom: 14, borderRadius: 8, background: "var(--warnBg)", border: "1px solid rgba(202,138,4,.1)", fontSize: 12, color: "var(--warn)", display: "flex", alignItems: "center", gap: 8}}>
            <span style={{width: 7, height: 7, borderRadius: "50%", background: "var(--warn)", flexShrink: 0}} />
            You have unsaved edits. Press Apply (Ctrl+S) to update in-memory config.
          </div>}
          <YamlEditor value={cfgYaml} onChange={setCfgYaml} filename="config.yaml" dirty={cfgYamlDirty} error={cfgYamlErr} onApply={saveCfgYaml} onReset={resetCfgYaml} onRefresh={refreshCfgYamlFromUI} />
        </div>

        ) : (
        /* ═══ MAIN CONFIG PAGE ═══ */
        <div>
        <h1 style={{fontSize: 22, fontWeight: 700, letterSpacing: "-.02em"}}>Retail Data Generator</h1>
        <p style={{fontSize: 13, color: "var(--dim)", marginTop: 3, marginBottom: 22}}>Configure and generate large, realistic retail datasets</p>

        {renderConfigForm()}

        {/* 6 GENERATE */}
        <Section num="6" title="Generate" defaultOpen={true}>
          <div style={{marginTop: 10, padding: "12px 16px", background: "var(--alt)", borderRadius: 10, border: "1px solid var(--border)", fontSize: 13, color: "var(--dim)", lineHeight: 1.7}}>
            <strong style={{color: "var(--text)"}}>{cfg.salesRows.toLocaleString()}</strong> rows · <strong style={{color: "var(--text)"}}>{cfg.customers.toLocaleString()}</strong> cust · <strong style={{color: "var(--text)"}}>{cfg.products.toLocaleString()}</strong> prod · {cfg.startDate} {"\u2192"} {cfg.endDate} · <Badge>{cfg.format.toUpperCase()}</Badge>
            {cfg.salesOutput !== "sales" && <>{" · "}<Badge>{cfg.salesOutput === "both" ? "Sales + Orders" : "Orders"}</Badge></>}
            {cfg.returnsEnabled && <>{" · "}<Badge variant="success">Returns {(cfg.returnRate * 100).toFixed(1)}%</Badge></>}
            {cfg.budgetEnabled && <>{" · "}<Badge>Budget</Badge></>}
            {cfg.inventoryEnabled && <>{" · "}<Badge>Inventory</Badge></>}
            {cfg.subEnabled && <>{" · "}<Badge>Subscriptions</Badge></>}
            {cfg.wlEnabled && <>{" · "}<Badge>Wishlists</Badge></>}
            {cfg.ccEnabled && <>{" · "}<Badge>Complaints</Badge></>}
          </div>
          <LogViewer logs={logs} isRunning={isRunning} elapsed={elapsed} />
        </Section>
        </div>
        )}
      </div>{/* end inner content padding div */}

      {/* ═══ GENERATE BAR (main page only, sticky inside scroll) ═══ */}
      {page === "main" && cfg && (
        <div style={{position: "sticky", bottom: 0, zIndex: 90, marginTop: "auto", padding: "0 40px 12px", maxWidth: 1120}}>
          {/* Validation panel (slides up) */}
          {validationOpen && (errors.length > 0 || warnings.length > 0) && (
            <div style={{background: "var(--surface)", border: "1px solid var(--border)", borderBottom: "none", borderRadius: "10px 10px 0 0", padding: "12px 20px", maxHeight: 200, overflowY: "auto", animation: "fadeIn .15s ease"}}>
              {errors.map((errorMsg, idx) => <div key={`e${idx}`} style={{padding: "6px 10px", borderRadius: 6, marginBottom: 4, fontSize: 11.5, display: "flex", alignItems: "flex-start", gap: 6, background: "var(--errBg)", color: "var(--err)"}}><span style={{fontWeight: 700, flexShrink: 0}}>&#10005;</span><span>{errorMsg}</span></div>)}
              {warnings.map((warnMsg, idx) => <div key={`w${idx}`} style={{padding: "6px 10px", borderRadius: 6, marginBottom: 4, fontSize: 11.5, display: "flex", alignItems: "flex-start", gap: 6, background: "var(--warnBg)", color: "var(--warn)"}}><span style={{fontWeight: 700, flexShrink: 0}}>&#9888;</span><span>{warnMsg}</span></div>)}
            </div>
          )}
          {/* Bar */}
          <div style={{background: "var(--surface)", border: "1px solid var(--border)", borderRadius: validationOpen && (errors.length > 0 || warnings.length > 0) ? "0 0 10px 10px" : 10, padding: "10px 20px", display: "flex", alignItems: "center", gap: 14, boxShadow: "0 2px 12px var(--shadow)"}}>
            <div style={{flex: 1, display: "flex", alignItems: "center", gap: 12, fontSize: 12, color: "var(--dim)", overflow: "hidden"}}>
              <strong style={{color: "var(--text)", flexShrink: 0}}>{cfg.salesRows.toLocaleString()} rows</strong>
              <span style={{color: "var(--muted)"}}>{cfg.startDate} {"\u2192"} {cfg.endDate}</span>
              {isRunning && <><span style={{width: 7, height: 7, borderRadius: "50%", background: "var(--ok)", animation: "pulse 1.5s infinite", flexShrink: 0}} /><span style={{color: "var(--ok)", fontFamily: "var(--mono)"}}>{elapsed < 60 ? elapsed.toFixed(1) + "s" : Math.floor(elapsed / 60) + "m " + Math.floor(elapsed % 60) + "s"}</span></>}
              {/* Validation badge — clickable */}
              {errors.length > 0 ? (
                <button onClick={() => setValidationOpen(!validationOpen)} style={{background: "none", border: "none", cursor: "pointer", padding: 0, display: "flex", alignItems: "center", gap: 4}}>
                  <Badge variant="error">{errors.length} error{errors.length > 1 ? "s" : ""}</Badge>
                  {warnings.length > 0 && <Badge variant="warning">{warnings.length}</Badge>}
                  <span style={{fontSize: 9, color: "var(--muted)", transform: validationOpen ? "rotate(180deg)" : "none", transition: "transform .15s"}}>{"\u25B2"}</span>
                </button>
              ) : warnings.length > 0 ? (
                <button onClick={() => setValidationOpen(!validationOpen)} style={{background: "none", border: "none", cursor: "pointer", padding: 0, display: "flex", alignItems: "center", gap: 4}}>
                  <Badge variant="warning">{warnings.length} warning{warnings.length > 1 ? "s" : ""}</Badge>
                  <span style={{fontSize: 9, color: "var(--muted)", transform: validationOpen ? "rotate(180deg)" : "none", transition: "transform .15s"}}>{"\u25B2"}</span>
                </button>
              ) : (
                <span style={{display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: "var(--ok)"}}>{"\u2713"} Valid</span>
              )}
              <span style={{fontSize: 10.5, color: "var(--muted)", marginLeft: "auto", flexShrink: 0}}>Ctrl+Enter</span>
            </div>
            {isRunning ?
              <button onClick={cancelGenerate} style={{padding: "8px 20px", borderRadius: 8, border: "1px solid var(--err)", background: "var(--errBg)", color: "var(--err)", fontWeight: 600, fontSize: 12.5, cursor: "pointer", fontFamily: "var(--sans)", whiteSpace: "nowrap"}}>Cancel</button>
            :
              <button onClick={runGenerate} disabled={errors.length > 0} style={{padding: "8px 24px", borderRadius: 8, border: "none", fontSize: 13, fontWeight: 700, cursor: errors.length > 0 ? "not-allowed" : "pointer", fontFamily: "var(--sans)", background: errors.length > 0 ? "var(--alt)" : "var(--accent)", color: errors.length > 0 ? "var(--muted)" : "#fff", whiteSpace: "nowrap", boxShadow: errors.length === 0 ? "0 2px 8px rgba(79,91,213,.2)" : "none"}}>Generate</button>
            }
          </div>
        </div>
      )}
      </div>{/* end main-content */}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
