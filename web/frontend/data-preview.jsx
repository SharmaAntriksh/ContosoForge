/* ═══ data-preview.jsx — Dataset browser & table preview ═══ */

function DataPreview({ onBack }) {
  const [datasets, setDatasets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedDataset, setSelectedDataset] = useState(null);
  const [tables, setTables] = useState([]);
  const [tablesLoading, setTablesLoading] = useState(false);
  const [selectedTable, setSelectedTable] = useState(null);
  const [preview, setPreview] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [offset, setOffset] = useState(0);
  const [pageSize] = useState(100);
  const [datasetExpanded, setDatasetExpanded] = useState(true);
  const [requestError, setRequestError] = useState(null);

  /* Load datasets on mount */
  useEffect(() => {
    window.apiRequest(API + "/datasets")
      .then(data => { setDatasets(data.datasets || []); setRequestError(null); setLoading(false); })
      .catch(error => { setRequestError(error.message); setLoading(false); });
  }, []);

  /* Load tables when a dataset is selected */
  useEffect(() => {
    if (!selectedDataset) { setTables([]); return; }
    setTablesLoading(true);
    setSelectedTable(null);
    setPreview(null);
    window.apiRequest(API + "/datasets/" + encodeURIComponent(selectedDataset.name) + "/tables")
      .then(data => { setTables(data.tables || []); setRequestError(null); setTablesLoading(false); })
      .catch(error => { setRequestError(error.message); setTablesLoading(false); });
  }, [selectedDataset]);

  /* Load preview when table or offset changes */
  useEffect(() => {
    if (!selectedDataset || !selectedTable) return;
    const controller = new AbortController();
    setPreviewLoading(true);
    window.apiRequest(
      API + "/datasets/" + encodeURIComponent(selectedDataset.name) + "/tables/" + encodeURIComponent(selectedTable.name) + "/preview?offset=" + offset + "&limit=" + pageSize,
      {signal: controller.signal},
    )
      .then(data => { setPreview(data); setRequestError(null); setPreviewLoading(false); })
      .catch(error => {
        if (controller.signal.aborted) return;
        setRequestError(error.message);
        setPreviewLoading(false);
      });
    return () => controller.abort();
  }, [selectedDataset, selectedTable, offset, pageSize]);

  const selectTable = (table) => {
    setSelectedTable(table);
    setOffset(0);
    setPreview(null);
  };

  const totalPages = preview ? Math.ceil(preview.total_rows / pageSize) : 0;
  const currentPage = Math.floor(offset / pageSize) + 1;
  const dimTables = tables.filter(t => t.category === "dimensions");
  const factTables = tables.filter(t => t.category === "facts");
  const atEnd = preview && offset + pageSize >= preview.total_rows;

  /* Format badge */
  const fmtBadge = (fmt) => {
    const colors = { csv: "#e67e22", parquet: "#3498db", delta: "#2ecc71", unknown: "#95a5a6" };
    return { background: colors[fmt] || colors.unknown, color: "#fff", padding: "2px 8px", borderRadius: 4, fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: ".04em" };
  };

  /* Table pill style */
  const pillStyle = (isSel) => ({
    padding: "5px 12px", borderRadius: 20, border: `1px solid ${isSel ? "var(--accent)" : "var(--border)"}`,
    background: isSel ? "var(--glow)" : "var(--surface)", color: isSel ? "var(--accent)" : "var(--text)",
    fontSize: 11.5, fontFamily: "var(--mono)", fontWeight: isSel ? 600 : 400, cursor: "pointer",
    transition: "all .1s", whiteSpace: "nowrap",
  });

  /* Pagination button style */
  const pgBtn = (disabled) => ({
    padding: "5px 10px", borderRadius: 6, border: "1px solid var(--border)", background: "var(--surface)",
    color: disabled ? "var(--muted)" : "var(--text)", fontSize: 12, cursor: disabled ? "default" : "pointer",
    fontFamily: "var(--sans)", opacity: disabled ? 0.5 : 1,
  });

  return (
    <div>
      <button onClick={onBack} style={{display: "inline-flex", alignItems: "center", gap: 6, padding: "6px 14px", borderRadius: 7, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--dim)", fontSize: 12.5, cursor: "pointer", fontFamily: "var(--sans)", marginBottom: 18, transition: "all .12s"}} onMouseOver={e => e.currentTarget.style.borderColor = "var(--accent)"} onMouseOut={e => e.currentTarget.style.borderColor = "var(--border)"}>
        {"\u2190"} Back to configuration
      </button>
      <h1 style={{fontSize: 22, fontWeight: 700, letterSpacing: "-.02em", marginBottom: 4}}>Data Preview</h1>
      <p style={{fontSize: 13, color: "var(--dim)", marginTop: 3, marginBottom: 22}}>Browse generated datasets and preview table contents</p>

      {requestError && <div role="alert" style={{padding: "10px 14px", marginBottom: 16, borderRadius: 8, background: "var(--errBg)", border: "1px solid rgba(239,68,68,.15)", fontSize: 12.5, color: "var(--err)"}}>{requestError}</div>}

      {loading ? (
        <div style={{padding: 40, textAlign: "center", color: "var(--muted)"}}>Loading datasets...</div>
      ) : datasets.length === 0 ? (
        <div style={{padding: 40, textAlign: "center", color: "var(--muted)"}}>No generated datasets found. Run a generation first.</div>
      ) : (
        <>
          {/* ── Dataset selector ── */}
          <div className="dataset-scroll" style={{marginBottom: 16}}>
            <div style={{display: "flex", alignItems: "center", gap: 10, marginBottom: 8}}>
              {selectedDataset && !datasetExpanded && (
                <button onClick={() => setDatasetExpanded(true)} style={{padding: "4px 12px", borderRadius: 6, border: "1px solid var(--accent)", background: "var(--glow)", color: "var(--accent)", fontSize: 11, fontWeight: 600, cursor: "pointer", fontFamily: "var(--sans)", transition: "all .12s"}}
                  onMouseOver={e => { e.currentTarget.style.background = "var(--accent)"; e.currentTarget.style.color = "#fff"; }}
                  onMouseOut={e => { e.currentTarget.style.background = "var(--glow)"; e.currentTarget.style.color = "var(--accent)"; }}>
                  Change
                </button>
              )}
              <div style={{fontSize: 10.5, fontWeight: 700, color: "var(--muted)", letterSpacing: ".09em", textTransform: "uppercase"}}>
                {selectedDataset && !datasetExpanded ? "Dataset" : "Select Dataset"}
              </div>
              {selectedDataset && datasetExpanded && (
                <button onClick={() => setDatasetExpanded(false)} style={{fontSize: 11, color: "var(--muted)", background: "none", border: "none", cursor: "pointer", fontFamily: "var(--sans)", padding: "2px 6px"}}>
                  Collapse
                </button>
              )}
            </div>

            {selectedDataset && !datasetExpanded ? (
              <div className="dataset-grid" style={{display: "grid", gridTemplateColumns: "90px 80px 1fr 90px 70px 70px", gap: 8, alignItems: "center", padding: "9px 14px", borderRadius: 8, border: "1px solid var(--accent)", background: "var(--glow)", fontFamily: "var(--sans)"}}>
                <span style={{fontSize: 11.5, color: "var(--muted)", fontFamily: "var(--mono)"}}>{selectedDataset.date}</span>
                <span style={{fontSize: 11.5, color: "var(--muted)", fontFamily: "var(--mono)"}}>{selectedDataset.time}</span>
                <span style={{fontSize: 12.5, fontWeight: 600, color: "var(--accent)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap"}}>{selectedDataset.description}</span>
                <span style={fmtBadge(selectedDataset.format)}>{selectedDataset.format}</span>
                <span style={{fontSize: 11, color: "var(--muted)", fontFamily: "var(--mono)"}}>{selectedDataset.table_count}</span>
                <span style={{fontSize: 11, color: "var(--muted)", fontFamily: "var(--mono)"}}>{selectedDataset.size_mb} MB</span>
              </div>
            ) : (
              <>
                <div className="dataset-grid" style={{display: "grid", gridTemplateColumns: "90px 80px 1fr 90px 70px 70px", gap: 8, padding: "6px 14px", fontSize: 10, fontWeight: 700, color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".04em"}}>
                  <span>Date</span><span>Time</span><span>Dataset</span><span>Format</span><span>Tables</span><span>Size</span>
                </div>
                <div className="dataset-list" style={{display: "flex", flexDirection: "column", gap: 4}}>
                  {datasets.map(ds => {
                    const isSelected = selectedDataset && selectedDataset.name === ds.name;
                    return (
                      <button type="button" className="dataset-grid" aria-pressed={Boolean(isSelected)} key={ds.name} onClick={() => { setSelectedDataset(ds); setDatasetExpanded(false); }} style={{
                        display: "grid", gridTemplateColumns: "90px 80px 1fr 90px 70px 70px", gap: 8, alignItems: "center",
                        width: "100%", padding: "9px 14px", borderRadius: 8,
                        cursor: "pointer", border: `1px solid ${isSelected ? "var(--accent)" : "var(--border)"}`,
                        background: isSelected ? "var(--glow)" : "var(--surface)", fontFamily: "var(--sans)", transition: "all .12s", textAlign: "left",
                      }} onMouseOver={e => { if (!isSelected) e.currentTarget.style.borderColor = "var(--accent)"; }}
                         onMouseOut={e => { if (!isSelected) e.currentTarget.style.borderColor = "var(--border)"; }}>
                        <span style={{fontSize: 11.5, color: "var(--muted)", fontFamily: "var(--mono)"}}>{ds.date}</span>
                        <span style={{fontSize: 11.5, color: "var(--muted)", fontFamily: "var(--mono)"}}>{ds.time}</span>
                        <span style={{fontSize: 12.5, fontWeight: isSelected ? 600 : 400, color: isSelected ? "var(--accent)" : "var(--text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap"}}>{ds.description}</span>
                        <span style={fmtBadge(ds.format)}>{ds.format}</span>
                        <span style={{fontSize: 11, color: "var(--muted)", fontFamily: "var(--mono)"}}>{ds.table_count}</span>
                        <span style={{fontSize: 11, color: "var(--muted)", fontFamily: "var(--mono)"}}>{ds.size_mb} MB</span>
                      </button>
                    );
                  })}
                </div>
              </>
            )}
          </div>

          {/* ── Table pills ── */}
          {selectedDataset && !datasetExpanded && !tablesLoading && tables.length > 0 && (
            <div style={{marginBottom: 16}}>
              {dimTables.length > 0 && (
                <div style={{marginBottom: 8}}>
                  <span style={{fontSize: 10, fontWeight: 600, color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".06em", marginRight: 8}}>Dimensions</span>
                  <div style={{display: "flex", flexWrap: "wrap", gap: 5, marginTop: 6}}>
                    {dimTables.map(t => (
                      <button key={t.name} onClick={() => selectTable(t)} style={pillStyle(selectedTable && selectedTable.name === t.name)}
                        onMouseOver={e => { if (!(selectedTable && selectedTable.name === t.name)) e.currentTarget.style.borderColor = "var(--accent)"; }}
                        onMouseOut={e => { if (!(selectedTable && selectedTable.name === t.name)) e.currentTarget.style.borderColor = "var(--border)"; }}>
                        {t.name}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {factTables.length > 0 && (
                <div>
                  <span style={{fontSize: 10, fontWeight: 600, color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".06em", marginRight: 8}}>Facts</span>
                  <div style={{display: "flex", flexWrap: "wrap", gap: 5, marginTop: 6}}>
                    {factTables.map(t => (
                      <button key={t.name} onClick={() => selectTable(t)} style={pillStyle(selectedTable && selectedTable.name === t.name)}
                        onMouseOver={e => { if (!(selectedTable && selectedTable.name === t.name)) e.currentTarget.style.borderColor = "var(--accent)"; }}
                        onMouseOut={e => { if (!(selectedTable && selectedTable.name === t.name)) e.currentTarget.style.borderColor = "var(--border)"; }}>
                        {t.name}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
          {!datasetExpanded && tablesLoading && <div style={{padding: 12, color: "var(--muted)", fontSize: 12}}>Loading tables...</div>}

          {/* ── Preview pane (full width) ── */}
          {selectedDataset && !datasetExpanded && selectedTable && (
            <div>
              {/* Table header */}
              <div style={{display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10}}>
                <div style={{display: "flex", alignItems: "center", gap: 10}}>
                  <span style={{fontSize: 15, fontWeight: 700, color: "var(--text)"}}>{selectedTable.name}</span>
                  <span style={fmtBadge(selectedTable.format || selectedDataset.format)}>{selectedTable.format || selectedDataset.format}</span>
                  <span style={{fontSize: 12, color: "var(--muted)"}}>{selectedTable.category}</span>
                </div>
                {preview && (
                  <div style={{display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--dim)"}}>
                    <span style={{fontFamily: "var(--mono)"}}>{preview.total_rows.toLocaleString()} rows</span>
                    <span style={{color: "var(--muted)"}}>{"\u00B7"}</span>
                    <span style={{fontFamily: "var(--mono)"}}>{preview.columns.length} cols</span>
                  </div>
                )}
              </div>

              {previewLoading && !preview ? (
                <div style={{padding: 40, textAlign: "center", color: "var(--muted)", fontSize: 13}}>Loading preview...</div>
              ) : preview ? (
                <>
                  <div style={{border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden"}}>
                    <div style={{overflowX: "auto", maxHeight: 520}}>
                      <table style={{width: "100%", borderCollapse: "collapse", fontSize: 11.5, fontFamily: "var(--mono)"}}>
                        <thead>
                          <tr style={{position: "sticky", top: 0, zIndex: 1}}>
                            <th style={{padding: "8px 10px", background: "var(--alt)", borderBottom: "2px solid var(--border)", fontWeight: 700, color: "var(--dim)", textAlign: "right", fontSize: 10, width: 50, whiteSpace: "nowrap"}}>#</th>
                            {preview.columns.map(col => (
                              <th key={col} style={{padding: "8px 10px", background: "var(--alt)", borderBottom: "2px solid var(--border)", fontWeight: 600, color: "var(--text)", textAlign: "left", whiteSpace: "nowrap"}}>{col}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {preview.rows.map((row, rowIdx) => (
                            <tr key={rowIdx} style={{borderBottom: "1px solid var(--border)"}} onMouseOver={e => e.currentTarget.style.background = "var(--glow)"} onMouseOut={e => e.currentTarget.style.background = "transparent"}>
                              <td style={{padding: "5px 10px", color: "var(--muted)", textAlign: "right", fontSize: 10, whiteSpace: "nowrap"}}>{offset + rowIdx + 1}</td>
                              {row.map((cell, colIdx) => (
                                <td key={colIdx} style={{padding: "5px 10px", color: "var(--text)", whiteSpace: "nowrap", maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis"}}>{cell === "" || cell === null ? <span style={{color: "var(--muted)", fontStyle: "italic"}}>null</span> : String(cell)}</td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>

                  {/* Pagination */}
                  {totalPages > 1 && (
                    <div style={{display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 12, padding: "8px 0"}}>
                      <div style={{display: "flex", gap: 6}}>
                        <button onClick={() => setOffset(0)} disabled={offset === 0} style={pgBtn(offset === 0)}>First</button>
                        <button onClick={() => setOffset(Math.max(0, offset - pageSize))} disabled={offset === 0} style={pgBtn(offset === 0)}>Prev</button>
                      </div>
                      <span style={{fontSize: 12, color: "var(--dim)", fontFamily: "var(--mono)"}}>
                        Page {currentPage.toLocaleString()} of {totalPages.toLocaleString()} {previewLoading && <span style={{color: "var(--muted)"}}>loading...</span>}
                      </span>
                      <div style={{display: "flex", gap: 6}}>
                        <button onClick={() => setOffset(offset + pageSize)} disabled={atEnd} style={pgBtn(atEnd)}>Next</button>
                        <button onClick={() => setOffset((totalPages - 1) * pageSize)} disabled={atEnd} style={pgBtn(atEnd)}>Last</button>
                      </div>
                    </div>
                  )}
                </>
              ) : null}
            </div>
          )}

          {selectedDataset && !datasetExpanded && !selectedTable && !tablesLoading && tables.length > 0 && (
            <div style={{padding: 40, textAlign: "center", color: "var(--muted)", fontSize: 13}}>Select a table above to preview its data</div>
          )}
        </>
      )}
    </div>
  );
}
