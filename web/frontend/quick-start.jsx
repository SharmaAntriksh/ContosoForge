/* Beginner-facing guided setup. The full editor remains in app.jsx. */

const TREND_OPTIONS = [
  {value: "steady-growth", label: "Steady growth", preview: "▁▂▃▄▅▆▇", description: "A mature retailer growing predictably year after year."},
  {value: "strong-growth", label: "Strong growth", preview: "▁▂▃▄▆▇█", description: "Fast, sustained expansion with an increasingly large customer base."},
  {value: "gradual-growth", label: "Gradual growth", preview: "▁▂▄▅▆▇▇", description: "A realistic ramp that eventually settles into a stable business."},
  {value: "hockey-stick", label: "Hockey stick", preview: "▁▁▂▃▅▇█", description: "A quiet start followed by explosive growth."},
  {value: "seasonal-with-growth", label: "Seasonal growth", preview: "▂▅▃▆▄▇▅", description: "Visible retail seasons layered over a growing business."},
  {value: "seasonal-dominant", label: "Strong seasonality", preview: "▂▇▂▇▂▇▂", description: "A mostly flat business where seasonal peaks dominate the charts."},
  {value: "recession-recovery", label: "Recession and recovery", preview: "▇▅▂▁▃▅▇", description: "A downturn followed by a measured return to strength."},
  {value: "boom-and-bust", label: "Boom and bust", preview: "▁▄▇█▅▂▁", description: "Rapid success followed by a sharp correction."},
  {value: "volatile", label: "Volatile", preview: "▃▇▂█▁▆▃", description: "Large, irregular swings for stress-testing analytical narratives."},
  {value: "plateau", label: "Growth then plateau", preview: "▁▃▅▇▇▇▇", description: "Early expansion that settles into a mature flat business."},
  {value: "new-market-entry", label: "New market entry", preview: "▁▁▁▂▃▅▇", description: "A long discovery phase followed by accelerating traction."},
  {value: "slow-decline", label: "Slow decline", preview: "▇▆▅▄▃▂▁", description: "Gentle erosion that remains useful for long-range analysis."},
  {value: "decline", label: "Strong decline", preview: "█▇▅▄▂▁▁", description: "A shrinking retailer with visible customer churn."},
  {value: "double-dip", label: "Double dip", preview: "▇▃▅▂▄▆▇", description: "Two downturns separated by a partial recovery."},
  {value: "stagnation", label: "Stagnation", preview: "▄▄▄▄▄▄▄", description: "A deliberately flat baseline with minimal growth or churn."},
];

const GUIDED_SIZES = {
  "quick-sample": {
    label: "Quick sample",
    rows: 100000,
    complexity: "Low",
    description: "Best for learning the workflow and checking schemas.",
  },
  "power-bi-demo": {
    label: "Power BI demo",
    rows: 2000000,
    complexity: "Moderate",
    description: "Enough history and variation for an interactive BI report.",
  },
  "medium-analytics": {
    label: "Medium analytics",
    rows: 10000000,
    complexity: "High",
    description: "A substantial dataset for analytics and engineering practice.",
  },
  "large-stress-test": {
    label: "Large stress test",
    rows: 100000000,
    complexity: "Very high",
    description: "For performance testing. Expect significant time and storage use.",
  },
};

const FEATURE_BUNDLES = {
  core: {
    label: "Core retail",
    description: "Dimensions and sales only—the smallest, clearest starting point.",
    tables: ["Sales", "Customers", "Products", "Stores", "Dates"],
  },
  analytics: {
    label: "Analytics ready",
    description: "Adds returns, budgets, and inventory for richer BI scenarios.",
    tables: ["Sales", "Returns", "Budget", "Inventory", "Dimensions"],
  },
  full: {
    label: "Full business",
    description: "Adds subscriptions, wishlists, and complaints to the analytics bundle.",
    tables: ["Sales", "Returns", "Budget", "Inventory", "Subscriptions", "Wishlists", "Complaints"],
  },
};

function formatGuidedSize(megabytes) {
  if (megabytes >= 1024) return `${(megabytes / 1024).toFixed(megabytes >= 10240 ? 0 : 1)} GB`;
  return `${Math.max(1, Math.round(megabytes))} MB`;
}

function guidedEstimate(draft) {
  const rows = GUIDED_SIZES[draft.size].rows;
  const bytesPerRow = {csv: 220, parquet: 75, deltaparquet: 95}[draft.format];
  const featureMultiplier = {core: 1, analytics: 1.25, full: 1.45}[draft.featureBundle];
  const midpointMb = rows * bytesPerRow * featureMultiplier / (1024 * 1024) + 40;
  return `${formatGuidedSize(midpointMb * 0.7)}–${formatGuidedSize(midpointMb * 1.5)}`;
}

function GuidedChoice({selected, onClick, title, description, meta}) {
  return (
    <button type="button" aria-pressed={selected} onClick={onClick} style={{
      padding: 16, borderRadius: 10, textAlign: "left", cursor: "pointer",
      border: `1px solid ${selected ? "var(--accent)" : "var(--border)"}`,
      background: selected ? "var(--glow)" : "var(--surface)",
      color: "var(--text)", fontFamily: "var(--sans)", transition: "all .12s",
      boxShadow: selected ? "0 0 0 2px var(--glow)" : "none",
    }}>
      <div style={{display: "flex", justifyContent: "space-between", gap: 12}}>
        <strong style={{fontSize: 13.5, color: selected ? "var(--accent)" : "var(--text)"}}>{title}</strong>
        {selected && <span style={{color: "var(--accent)", fontWeight: 700}}>✓</span>}
      </div>
      <div style={{fontSize: 11.5, lineHeight: 1.5, color: "var(--dim)", marginTop: 5}}>{description}</div>
      {meta && <div style={{fontSize: 10.5, color: "var(--muted)", marginTop: 8}}>{meta}</div>}
    </button>
  );
}

function QuickStart({
  draft,
  onDraftChange,
  onApply,
  onGenerate,
  onCancel,
  appliedSummary,
  configSynced,
  syncError,
  isRunning,
  runStatus,
  elapsed,
  logs,
}) {
  const [step, setStep] = useState(0);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState(null);
  const steps = ["Size", "Dates", "Business story", "Output", "Extra tables", "Review"];
  const trend = TREND_OPTIONS.find(option => option.value === draft.trend) || TREND_OPTIONS[0];
  const size = GUIDED_SIZES[draft.size];
  const features = FEATURE_BUNDLES[draft.featureBundle];
  const pbipAvailable = draft.format !== "deltaparquet";

  const update = (patch) => {
    setApplyError(null);
    onDraftChange({...draft, ...patch});
  };

  const apply = async () => {
    setApplying(true);
    setApplyError(null);
    try {
      await onApply(draft);
    } catch (error) {
      setApplyError(error.message);
    } finally {
      setApplying(false);
    }
  };

  const cardGrid = {display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: 10};
  const inputStyle = {width: "100%", boxSizing: "border-box", padding: "9px 10px", borderRadius: 7, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text)", fontFamily: "var(--sans)"};

  return (
    <div>
      <nav aria-label="Guided setup progress" style={{display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 22}}>
        {steps.map((label, index) => (
          <button key={label} type="button" aria-current={step === index ? "step" : undefined} onClick={() => setStep(index)} style={{
            padding: "6px 10px", borderRadius: 20, border: "1px solid var(--border)",
            background: step === index ? "var(--accent)" : index < step ? "var(--glow)" : "var(--surface)",
            color: step === index ? "#fff" : index < step ? "var(--accent)" : "var(--muted)",
            fontSize: 11.5, fontWeight: 600, cursor: "pointer", fontFamily: "var(--sans)",
          }}>{index + 1}. {label}</button>
        ))}
      </nav>

      <div style={{padding: 22, border: "1px solid var(--border)", borderRadius: 12, background: "var(--surface)"}}>
        {step === 0 && <>
          <h2 style={{fontSize: 18, margin: 0}}>How much data do you need?</h2>
          <p style={{fontSize: 12.5, color: "var(--dim)", lineHeight: 1.6}}>Start small unless you already have a performance-testing goal.</p>
          <div style={cardGrid}>{Object.entries(GUIDED_SIZES).map(([key, option]) => (
            <GuidedChoice key={key} selected={draft.size === key} onClick={() => update({size: key})}
              title={option.label} description={option.description}
              meta={`${option.rows.toLocaleString()} sales rows · ${option.complexity} complexity`} />
          ))}</div>
        </>}

        {step === 1 && <>
          <h2 style={{fontSize: 18, margin: 0}}>What period should the business cover?</h2>
          <p style={{fontSize: 12.5, color: "var(--dim)", lineHeight: 1.6}}>Longer periods spread the same sales rows across more months and automatically reduce the recommended customer count.</p>
          <div className="guided-date-grid" style={{display: "grid", gridTemplateColumns: "repeat(2, minmax(180px, 1fr))", gap: 12, maxWidth: 600}}>
            <label htmlFor="guided-start-date" style={{fontSize: 12, color: "var(--dim)"}}>Start date<input id="guided-start-date" type="date" value={draft.startDate} onChange={event => update({startDate: event.target.value})} style={{...inputStyle, display: "block", marginTop: 5}} /></label>
            <label htmlFor="guided-end-date" style={{fontSize: 12, color: "var(--dim)"}}>End date<input id="guided-end-date" type="date" aria-invalid={draft.endDate < draft.startDate} aria-describedby={draft.endDate < draft.startDate ? "guided-date-error" : undefined} value={draft.endDate} onChange={event => update({endDate: event.target.value})} style={{...inputStyle, display: "block", marginTop: 5}} /></label>
          </div>
          {draft.endDate < draft.startDate && <div id="guided-date-error" role="alert" style={{color: "var(--err)", fontSize: 12, marginTop: 10}}>End date must be on or after the start date.</div>}
        </>}

        {step === 2 && <>
          <h2 style={{fontSize: 18, margin: 0}}>What story should the charts tell?</h2>
          <p style={{fontSize: 12.5, color: "var(--dim)", lineHeight: 1.6}}>This trend shapes revenue, customer acquisition, churn, and seasonality together.</p>
          <label htmlFor="guided-trend" className="sr-only">Business trend</label>
          <select id="guided-trend" value={draft.trend} onChange={event => update({trend: event.target.value})} style={{...inputStyle, maxWidth: 420, marginBottom: 12}}>
            {TREND_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
          <div style={{padding: 16, borderRadius: 10, background: "var(--alt)", border: "1px solid var(--border)", maxWidth: 650}}>
            <div style={{fontFamily: "var(--mono)", fontSize: 28, color: "var(--accent)", letterSpacing: 3}}>{trend.preview}</div>
            <strong style={{display: "block", marginTop: 8}}>{trend.label}</strong>
            <div style={{fontSize: 12, color: "var(--dim)", marginTop: 4}}>{trend.description}</div>
          </div>
          <div style={{fontSize: 11.5, lineHeight: 1.6, color: "var(--muted)", marginTop: 12}}>
            Choosing a story replaces manual year-level factors if they exist, so the two controls cannot silently disagree.
            Advanced overrides remain available in Models.{" "}
            <a href="https://github.com/SharmaAntriksh/ContosoForge/blob/main/docs/MODELS_GUIDE.md" target="_blank" rel="noopener" style={{color: "var(--accent)"}}>Business model guide</a>
          </div>
        </>}

        {step === 3 && <>
          <h2 style={{fontSize: 18, margin: 0}}>How will you use the files?</h2>
          <p style={{fontSize: 12.5, color: "var(--dim)", lineHeight: 1.6}}>Choose the format and product catalog together.</p>
          <div style={{fontSize: 11, fontWeight: 700, color: "var(--muted)", textTransform: "uppercase", marginBottom: 8}}>Output format</div>
          <div style={cardGrid}>
            <GuidedChoice selected={draft.format === "parquet"} onClick={() => update({format: "parquet"})} title="Parquet" description="Compact and fast for analytics. Includes a Power BI project." />
            <GuidedChoice selected={draft.format === "csv"} onClick={() => update({format: "csv"})} title="CSV" description="Most compatible and includes SQL loading scripts and a Power BI project." />
            <GuidedChoice selected={draft.format === "deltaparquet"} onClick={() => update({format: "deltaparquet"})} title="Delta Lake" description="Partitioned ACID tables for data engineering. No Power BI project." />
          </div>
          <div style={{fontSize: 11, fontWeight: 700, color: "var(--muted)", textTransform: "uppercase", margin: "18px 0 8px"}}>Product catalog</div>
          <div style={cardGrid}>
            <GuidedChoice selected={draft.catalog === "contoso"} onClick={() => update({catalog: "contoso"})} title="Classic Contoso" description="The original house-brand catalog—familiar and compact." meta="About 2,500 base products" />
            <GuidedChoice selected={draft.catalog === "synthetic"} onClick={() => update({catalog: "synthetic"})} title="Modern synthetic" description="A broader retail catalog with many recognizable categories and brands." meta="About 5,600 base products" />
            <GuidedChoice selected={draft.catalog === "all"} onClick={() => update({catalog: "all"})} title="Combined catalog" description="The fullest product variety for richer category analysis." meta="About 7,900 base products" />
          </div>
        </>}

        {step === 4 && <>
          <h2 style={{fontSize: 18, margin: 0}}>How much business detail should be included?</h2>
          <p style={{fontSize: 12.5, color: "var(--dim)", lineHeight: 1.6}}>All choices include the core dimensions and sales fact.</p>
          <div style={cardGrid}>{Object.entries(FEATURE_BUNDLES).map(([key, option]) => (
            <GuidedChoice key={key} selected={draft.featureBundle === key} onClick={() => update({featureBundle: key})}
              title={option.label} description={option.description} meta={option.tables.join(" · ")} />
          ))}</div>
        </>}

        {step === 5 && <>
          <h2 style={{fontSize: 18, margin: 0}}>Review your dataset</h2>
          <p style={{fontSize: 12.5, color: "var(--dim)", lineHeight: 1.6}}>Nothing is written to config.yaml or models.yaml. Applying stores these choices in server memory for this session; Generate then creates a new timestamped output folder.</p>
          <div style={{display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 10, margin: "16px 0"}}>
            {[
              ["Scale", `${size.label} · ${size.rows.toLocaleString()} rows`],
              ["Dates", `${draft.startDate} → ${draft.endDate}`],
              ["Business story", `${trend.label} ${trend.preview}`],
              ["Files", `${draft.format === "deltaparquet" ? "Delta Lake" : draft.format.toUpperCase()} · ${draft.catalog} products`],
              ["Extra tables", features.label],
              ["Estimated output", guidedEstimate(draft)],
            ].map(([label, value]) => <div key={label} style={{padding: 12, borderRadius: 8, background: "var(--alt)", border: "1px solid var(--border)"}}>
              <div style={{fontSize: 10, color: "var(--muted)", textTransform: "uppercase", fontWeight: 700}}>{label}</div>
              <div style={{fontSize: 12.5, color: "var(--text)", marginTop: 5, lineHeight: 1.45}}>{value}</div>
            </div>)}
          </div>
          <div style={{fontSize: 11.5, color: pbipAvailable ? "var(--ok)" : "var(--warn)", marginBottom: 12}}>
            {pbipAvailable ? "✓ A Power BI .pbip project will be included." : "Delta Lake output does not include a Power BI .pbip project."}
            {" "}Generation complexity: <strong>{size.complexity}</strong>.
          </div>
          {appliedSummary?.manual_factors_cleared && <div style={{padding: 10, borderRadius: 7, background: "var(--warnBg)", color: "var(--warn)", fontSize: 12, marginBottom: 10}}>Manual year-level factors were cleared so the selected trend is the single business story.</div>}
          {(applyError || syncError) && <div role="alert" style={{padding: 10, borderRadius: 7, background: "var(--errBg)", color: "var(--err)", fontSize: 12, marginBottom: 10}}>{applyError || syncError}</div>}
          {!appliedSummary ? (
            <button type="button" onClick={apply} disabled={applying || draft.endDate < draft.startDate} style={{padding: "10px 18px", border: "none", borderRadius: 8, background: "var(--accent)", color: "#fff", fontWeight: 700, cursor: applying ? "wait" : "pointer"}}>{applying ? "Applying..." : "Apply these settings"}</button>
          ) : isRunning ? (
            <button type="button" onClick={onCancel} style={{padding: "10px 18px", borderRadius: 8, border: "1px solid var(--err)", background: "var(--errBg)", color: "var(--err)", fontWeight: 700, cursor: "pointer"}}>Cancel generation</button>
          ) : (
            <button type="button" onClick={onGenerate} disabled={!configSynced} style={{padding: "10px 18px", border: "none", borderRadius: 8, background: configSynced ? "var(--accent)" : "var(--alt)", color: configSynced ? "#fff" : "var(--muted)", fontWeight: 700, cursor: configSynced ? "pointer" : "not-allowed"}}>Generate dataset</button>
          )}
          <div style={{fontSize: 10.5, color: "var(--muted)", marginTop: 10}}>Output-size estimates are directional and vary with enabled facts, compression, and data cardinality.</div>
        </>}
      </div>

      <div style={{display: "flex", justifyContent: "space-between", marginTop: 14}}>
        <button type="button" onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0} style={{padding: "8px 14px", borderRadius: 7, border: "1px solid var(--border)", background: "var(--surface)", color: step === 0 ? "var(--muted)" : "var(--text)", cursor: step === 0 ? "default" : "pointer"}}>Back</button>
        {step < steps.length - 1 && <button type="button" onClick={() => setStep(step + 1)} disabled={step === 1 && draft.endDate < draft.startDate} style={{padding: "8px 16px", borderRadius: 7, border: "none", background: "var(--accent)", color: "#fff", fontWeight: 700, cursor: "pointer"}}>Continue</button>}
      </div>

      {(logs.length > 0 || isRunning) && <div style={{marginTop: 20}}><LogViewer logs={logs} isRunning={isRunning} status={runStatus} elapsed={elapsed} /></div>}
    </div>
  );
}
