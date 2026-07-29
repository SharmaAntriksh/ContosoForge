/* ═══ database-import.jsx — Progressive database import wizard ═══ */

function ImportReadiness({ state, readyText, errorText }) {
  const tone = state === true ? "ready" : state === false ? "error" : "loading";
  return (
    <span className={`import-readiness ${tone}`} role="status">
      <span className="import-readiness-dot" aria-hidden="true" />
      {state === true ? readyText : state === false ? errorText : "Checking local support…"}
    </span>
  );
}

function ImportCheck({ id, checked, onChange, title, help, disabled, describedBy }) {
  return (
    <label className="import-check" htmlFor={id}>
      <input id={id} type="checkbox" aria-describedby={describedBy} checked={checked} onChange={onChange} disabled={disabled} />
      <span>
        <strong>{title}</strong>
        {help && <small id={describedBy}>{help}</small>}
      </span>
    </label>
  );
}

function SqlImport({ onBack }) {
  const [step, setStep] = useState(1);
  const [furthestStep, setFurthestStep] = useState(1);
  const [engine, setEngine] = useState("sqlserver");

  /* SQL Server connection settings */
  const [server, setServer] = useState("");
  const [trusted, setTrusted] = useState(true);
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [applyCci, setApplyCci] = useState(false);
  const [dropPk, setDropPk] = useState(false);
  const [verify, setVerify] = useState(false);
  const [odbcDriver, setOdbcDriver] = useState("");

  /* PostgreSQL connection settings */
  const [pgHost, setPgHost] = useState("localhost");
  const [pgPort, setPgPort] = useState("5432");
  const [pgUser, setPgUser] = useState("postgres");
  const [pgPassword, setPgPassword] = useState("");
  const [pgIdentifierCase, setPgIdentifierCase] = useState("snake");
  const [pgLoadWorkers, setPgLoadWorkers] = useState("1");
  const [pgVerify, setPgVerify] = useState(true);

  /* Shared target and dataset settings */
  const [database, setDatabase] = useState("");
  const [datasets, setDatasets] = useState([]);
  const [selectedDataset, setSelectedDataset] = useState("");
  const [datasetsLoading, setDatasetsLoading] = useState(true);

  /* Local dependency readiness */
  const [drivers, setDrivers] = useState([]);
  const [driverAvailable, setDriverAvailable] = useState(null);
  const [driverError, setDriverError] = useState(null);
  const [pgAvailable, setPgAvailable] = useState(null);
  const [pgError, setPgError] = useState(null);

  /* SQL Server discovery */
  const [discoveredServers, setDiscoveredServers] = useState([]);
  const [serversLoading, setServersLoading] = useState(true);

  /* Import job state */
  const [isRunning, setIsRunning] = useState(false);
  const [logs, setLogs] = useState([]);
  const [elapsed, setElapsed] = useState(0);
  const [jobStatus, setJobStatus] = useState(null);
  const [requestError, setRequestError] = useState(null);
  const [idleSecs, setIdleSecs] = useState(0);
  const timerRef = useRef(null);
  const lastLogRef = useRef(Date.now());
  const idleTimerRef = useRef(null);
  const esRef = useRef(null);
  const logsEndRef = useRef(null);

  const isPostgres = engine === "postgres";
  const engineName = isPostgres ? "PostgreSQL" : "SQL Server";
  const selectedData = datasets.find(ds => ds.name === selectedDataset) || null;
  const toDbName = value => value
    ? value.replace(/[^a-zA-Z0-9_]/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "")
    : "";

  const dependencyReady = isPostgres ? pgAvailable === true : driverAvailable === true;
  const connectionReady = isPostgres
    ? dependencyReady && pgHost.trim() && pgPort && pgUser.trim() && database.trim()
    : dependencyReady && server.trim() && database.trim() && (trusted || user.trim());
  const canStart = connectionReady && selectedDataset && !isRunning;

  const stopTimers = (status, elapsedValue) => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    clearInterval(timerRef.current);
    clearInterval(idleTimerRef.current);
    setIdleSecs(0);
    setIsRunning(false);
    if (status) setJobStatus(status);
    if (elapsedValue != null) setElapsed(elapsedValue);
  };

  useEffect(() => () => {
    clearInterval(timerRef.current);
    clearInterval(idleTimerRef.current);
    if (esRef.current) esRef.current.close();
  }, []);

  useEffect(() => {
    window.apiRequest(API + "/datasets")
      .then(data => {
        const csvDatasets = (data.datasets || []).filter(ds => ds.format === "csv");
        setDatasets(csvDatasets);
        if (csvDatasets.length > 0) {
          setSelectedDataset(csvDatasets[0].name);
          setDatabase(toDbName(csvDatasets[0].description));
        }
        setDatasetsLoading(false);
      })
      .catch(error => {
        setRequestError(error.message);
        setDatasetsLoading(false);
      });

    window.apiRequest(API + "/import/drivers")
      .then(data => {
        const sqlDrivers = data.drivers || [];
        setDriverAvailable(Boolean(data.available && sqlDrivers.length));
        setDrivers(sqlDrivers);
        if (sqlDrivers.length) setOdbcDriver(sqlDrivers[0]);
        if (data.error) setDriverError(data.error);
      })
      .catch(error => {
        setDriverError(error.message);
        setDriverAvailable(false);
      });

    window.apiRequest(API + "/import/capabilities")
      .then(data => {
        const postgres = data.postgres || {};
        setPgAvailable(Boolean(postgres.available));
        setPgError(postgres.error || null);
      })
      .catch(error => {
        setPgError(`Readiness check failed: ${error.message}. Restart the web server and try again.`);
        setPgAvailable(false);
      });

    window.apiRequest(API + "/import/servers")
      .then(data => {
        const servers = data.servers || [];
        setDiscoveredServers(servers);
        if (servers.length) setServer(servers[0].server);
        setServersLoading(false);
      })
      .catch(error => {
        setRequestError(error.message);
        setServersLoading(false);
      });
  }, []);

  useEffect(() => {
    if (logsEndRef.current) logsEndRef.current.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  const chooseEngine = nextEngine => {
    if (isRunning) return;
    setEngine(nextEngine);
    setRequestError(null);
    setJobStatus(null);
  };

  const chooseDataset = dataset => {
    if (isRunning) return;
    const previous = datasets.find(ds => ds.name === selectedDataset);
    const previousDefault = previous ? toDbName(previous.description) : "";
    setSelectedDataset(dataset.name);
    setDatabase(current => (
      !current || current === previousDefault ? toDbName(dataset.description) : current
    ));
  };

  const advance = () => {
    const next = Math.min(4, step + 1);
    setStep(next);
    setFurthestStep(current => Math.max(current, next));
  };

  const returnToStep = target => {
    if (!isRunning && target <= furthestStep) setStep(target);
  };

  const startImport = () => {
    const targetServer = isPostgres ? pgHost.trim() : server.trim();
    const targetUser = isPostgres ? pgUser.trim() : (trusted ? null : user.trim());
    if (!canStart) return;

    setLogs([]);
    setJobStatus(null);
    setRequestError(null);
    setElapsed(0);
    setIdleSecs(0);
    lastLogRef.current = Date.now();

    window.apiRequest(API + "/import/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        engine,
        server: targetServer,
        port: isPostgres ? Number(pgPort) : 5432,
        database: database.trim(),
        dataset: selectedDataset,
        trusted: isPostgres ? false : trusted,
        user: targetUser,
        password: isPostgres ? pgPassword : (trusted ? null : password),
        apply_cci: isPostgres ? false : applyCci,
        drop_pk: isPostgres ? false : dropPk,
        verify: isPostgres ? pgVerify : verify,
        odbc_driver: isPostgres ? null : (odbcDriver || null),
        identifier_case: isPostgres ? pgIdentifierCase : "pascal",
        load_workers: isPostgres ? Number(pgLoadWorkers) : 1,
      }),
    })
      .then(() => {
        setIsRunning(true);
        const startTime = Date.now();
        timerRef.current = setInterval(
          () => setElapsed((Date.now() - startTime) / 1000),
          100,
        );
        idleTimerRef.current = setInterval(() => {
          const seconds = Math.floor((Date.now() - lastLogRef.current) / 1000);
          setIdleSecs(previous => previous === seconds ? previous : seconds);
        }, 1000);

        const stream = new EventSource(API + "/import/stream");
        esRef.current = stream;
        stream.onmessage = event => {
          const data = JSON.parse(event.data);
          if (data.type === "log") {
            lastLogRef.current = Date.now();
            setIdleSecs(0);
            setLogs(previous => [...previous, data.line]);
          }
          if (data.type === "status") setElapsed(data.elapsed);
          if (data.type === "end" || data.type === "idle") {
            stopTimers(data.status || "done", data.elapsed || 0);
          }
        };
        stream.onerror = () => stopTimers("failed");
      })
      .catch(error => {
        stopTimers("failed");
        setRequestError(error.message);
        setLogs(previous => [...previous, `ERROR: ${error.message}`]);
      });
  };

  const cancelImport = () => {
    window.apiRequest(API + "/import/cancel", { method: "POST" })
      .catch(error => setRequestError(`Cancellation failed: ${error.message}`));
  };

  const formatElapsed = seconds => seconds < 60
    ? `${seconds.toFixed(1)}s`
    : `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;

  const steps = [
    ["Database", "Choose destination"],
    ["Dataset", "Pick a CSV run"],
    ["Connection", "Enter access details"],
    ["Review", "Confirm and import"],
  ];

  const renderStepOne = () => (
    <section className="import-panel" aria-labelledby="import-step-title">
      <div className="import-panel-heading">
        <span className="import-eyebrow">Step 1 of 4</span>
        <h2 id="import-step-title">Where should the data go?</h2>
        <p>Choose your database platform. You can change this later without losing the other form values.</p>
      </div>

      <div className="import-engine-grid" role="radiogroup" aria-label="Database engine">
        <button
          type="button"
          role="radio"
          aria-checked={!isPostgres}
          className={`import-engine-card${!isPostgres ? " selected" : ""}`}
          onClick={() => chooseEngine("sqlserver")}
          disabled={isRunning}
        >
          <span className="import-engine-mark sql" aria-hidden="true">S</span>
          <span className="import-engine-copy">
            <strong>SQL Server</strong>
            <small>Best for Microsoft BI and Windows environments</small>
            <span className="import-feature-row">
              <span>Windows authentication</span>
              <span>Columnstore options</span>
            </span>
          </span>
          <ImportReadiness
            state={driverAvailable}
            readyText="Driver ready"
            errorText="Driver needed"
          />
        </button>

        <button
          type="button"
          role="radio"
          aria-checked={isPostgres}
          className={`import-engine-card${isPostgres ? " selected" : ""}`}
          onClick={() => chooseEngine("postgres")}
          disabled={isRunning}
        >
          <span className="import-engine-mark postgres" aria-hidden="true">P</span>
          <span className="import-engine-copy">
            <strong>PostgreSQL</strong>
            <small>Portable and friendly for open-source analytics</small>
            <span className="import-feature-row">
              <span>snake_case naming</span>
              <span>Parallel COPY loads</span>
            </span>
          </span>
          <ImportReadiness
            state={pgAvailable}
            readyText="Client ready"
            errorText="Setup needed"
          />
        </button>
      </div>

      {!dependencyReady && (driverAvailable !== null || pgAvailable !== null) && (
        <div className="import-inline-alert" role="alert">
          <strong>{engineName} needs one local dependency.</strong>
          <span>
            {isPostgres
              ? pgError
              : `${driverError || "A SQL Server ODBC driver was not detected."} Install Microsoft ODBC Driver 18, then restart the web UI.`}
          </span>
        </div>
      )}

      <div className="import-panel-actions">
        <span />
        <button type="button" className="import-primary" onClick={advance} disabled={!dependencyReady}>
          Continue to datasets <span aria-hidden="true">→</span>
        </button>
      </div>
    </section>
  );

  const renderStepTwo = () => (
    <section className="import-panel" aria-labelledby="import-step-title">
      <div className="import-panel-heading">
        <span className="import-eyebrow">Step 2 of 4</span>
        <h2 id="import-step-title">Which dataset should be imported?</h2>
        <p>Only CSV runs appear here because database bootstrap scripts are packaged with CSV output.</p>
      </div>

      {datasetsLoading ? (
        <div className="import-empty-state">Loading generated datasets…</div>
      ) : datasets.length === 0 ? (
        <div className="import-empty-state">
          <strong>No CSV datasets found</strong>
          <span>Generate a dataset using CSV output, then return here.</span>
        </div>
      ) : (
        <div className="import-dataset-grid" role="radiogroup" aria-label="Generated CSV dataset">
          {datasets.map(dataset => {
            const selected = dataset.name === selectedDataset;
            return (
              <button
                type="button"
                role="radio"
                aria-checked={selected}
                key={dataset.name}
                className={`import-dataset-card${selected ? " selected" : ""}`}
                onClick={() => chooseDataset(dataset)}
                disabled={isRunning}
              >
                <span className="import-dataset-check" aria-hidden="true">{selected ? "✓" : ""}</span>
                <span className="import-dataset-main">
                  <strong>{dataset.description}</strong>
                  <small>Generated {dataset.date} at {dataset.time}</small>
                </span>
                <span className="import-dataset-meta">
                  <span>{dataset.table_count} tables</span>
                  <span>{dataset.size_mb} MB</span>
                  <span className="import-format-pill">CSV</span>
                </span>
              </button>
            );
          })}
        </div>
      )}

      <div className="import-panel-actions">
        <button type="button" className="import-secondary" onClick={() => returnToStep(1)}>← Back</button>
        <button type="button" className="import-primary" onClick={advance} disabled={!selectedDataset}>
          Continue to connection <span aria-hidden="true">→</span>
        </button>
      </div>
    </section>
  );

  const renderSqlConnection = () => (
    <>
      <div className="import-field">
        <label htmlFor={discoveredServers.length ? "sql-server-choice" : "sql-server"}>Server</label>
        {serversLoading ? (
          <div className="import-input import-loading-field">Discovering SQL Server instances…</div>
        ) : discoveredServers.length ? (() => {
          const known = discoveredServers.some(item => item.server === server);
          return (
            <div className="import-server-row">
              <select
                id="sql-server-choice"
                value={known ? server : "__custom__"}
                onChange={event => setServer(event.target.value === "__custom__" ? "" : event.target.value)}
                className="import-input"
                disabled={isRunning}
              >
                {discoveredServers.map(item => (
                  <option key={item.server} value={item.server}>
                    {item.server}{item.version ? ` (v${item.version.split(".")[0]})` : ""}
                  </option>
                ))}
                <option value="__custom__">Another server…</option>
              </select>
              {!known && (
                <input
                  id="sql-custom-server"
                  aria-label="Custom SQL Server hostname and instance"
                  value={server}
                  onChange={event => setServer(event.target.value)}
                  placeholder="HOSTNAME\\INSTANCE"
                  className="import-input"
                  disabled={isRunning}
                />
              )}
            </div>
          );
        })() : (
          <input
            id="sql-server"
            value={server}
            onChange={event => setServer(event.target.value)}
            placeholder="HOSTNAME\\INSTANCE"
            className="import-input"
            disabled={isRunning}
          />
        )}
      </div>

      <div className="import-field">
        <label>Authentication</label>
        <div className="import-segmented" role="group" aria-label="SQL Server authentication">
          <button type="button" aria-pressed={trusted} onClick={() => setTrusted(true)} disabled={isRunning}>
            Windows
          </button>
          <button type="button" aria-pressed={!trusted} onClick={() => setTrusted(false)} disabled={isRunning}>
            SQL login
          </button>
        </div>
        <small>{trusted ? "Uses your current Windows identity." : "Uses a SQL Server username and password."}</small>
      </div>

      {!trusted && (
        <>
          <div className="import-field">
            <label htmlFor="sql-username">Username</label>
            <input id="sql-username" value={user} onChange={event => setUser(event.target.value)} placeholder="sa" className="import-input" disabled={isRunning} />
          </div>
          <div className="import-field">
            <label htmlFor="sql-password">Password</label>
            <input id="sql-password" type="password" value={password} onChange={event => setPassword(event.target.value)} className="import-input" autoComplete="current-password" disabled={isRunning} />
          </div>
        </>
      )}

      <details className="import-advanced">
        <summary>Advanced SQL Server options</summary>
        <div className="import-advanced-body">
          {drivers.length > 1 && (
            <div className="import-field">
              <label htmlFor="sql-odbc-driver">ODBC driver</label>
              <select id="sql-odbc-driver" value={odbcDriver} onChange={event => setOdbcDriver(event.target.value)} className="import-input" disabled={isRunning}>
                {drivers.map(driver => <option key={driver} value={driver}>{driver}</option>)}
              </select>
            </div>
          )}
          <ImportCheck id="sql-apply-cci" checked={applyCci} onChange={event => setApplyCci(event.target.checked)} title="Apply columnstore indexes" help="Optimizes large analytical scans; adds roughly 30–60 seconds." disabled={isRunning} />
          <ImportCheck id="sql-drop-pk" checked={dropPk} onChange={event => setDropPk(event.target.checked)} title="Drop primary and foreign keys" help="Reduces storage, but removes relational integrity after loading." disabled={isRunning} />
          <ImportCheck id="sql-verify" describedBy="sql-verify-help" checked={verify} onChange={event => setVerify(event.target.checked)} title="Run the verification scorecard" help="Reports PASS or FAIL for each post-import check." disabled={isRunning} />
        </div>
      </details>
    </>
  );

  const renderPostgresConnection = () => (
    <>
      <div className="import-field">
        <label htmlFor="pg-host">Host</label>
        <input id="pg-host" value={pgHost} onChange={event => setPgHost(event.target.value)} placeholder="localhost" className="import-input" disabled={isRunning} />
      </div>
      <div className="import-field">
        <label htmlFor="pg-port">Port</label>
        <input id="pg-port" type="number" min="1" max="65535" value={pgPort} onChange={event => setPgPort(event.target.value)} className="import-input" disabled={isRunning} />
      </div>
      <div className="import-field">
        <label htmlFor="pg-username">Username</label>
        <input id="pg-username" value={pgUser} onChange={event => setPgUser(event.target.value)} placeholder="postgres" className="import-input" disabled={isRunning} />
      </div>
      <div className="import-field">
        <label htmlFor="pg-password">Password</label>
        <input id="pg-password" type="password" value={pgPassword} onChange={event => setPgPassword(event.target.value)} className="import-input" autoComplete="current-password" disabled={isRunning} />
        <small>May be left blank when local trust or a password file is configured.</small>
      </div>

      <ImportCheck
        id="pg-verify"
        describedBy="pg-verify-help"
        checked={pgVerify}
        onChange={event => setPgVerify(event.target.checked)}
        title="Verify row counts after import"
        help="Recommended. Confirms every loaded table and its final row count."
        disabled={isRunning}
      />

      <details className="import-advanced">
        <summary>Advanced PostgreSQL options</summary>
        <div className="import-advanced-body import-form-grid">
          <div className="import-field">
            <label htmlFor="pg-identifier-case">Identifier style</label>
            <select id="pg-identifier-case" value={pgIdentifierCase} onChange={event => setPgIdentifierCase(event.target.value)} className="import-input" disabled={isRunning}>
              <option value="snake">snake_case — recommended</option>
              <option value="pascal">PascalCase — matches generated files</option>
            </select>
            <small>snake_case avoids quoted names in everyday queries.</small>
          </div>
          <div className="import-field">
            <label htmlFor="pg-load-workers">Parallel load connections</label>
            <select id="pg-load-workers" value={pgLoadWorkers} onChange={event => setPgLoadWorkers(event.target.value)} className="import-input" disabled={isRunning}>
              <option value="1">1 — safest default</option>
              <option value="2">2 connections</option>
              <option value="4">4 connections</option>
              <option value="8">8 — large local datasets</option>
            </select>
            <small>Only chunked fact tables are loaded in parallel.</small>
          </div>
        </div>
      </details>
    </>
  );

  const renderStepThree = () => (
    <section className="import-panel" aria-labelledby="import-step-title">
      <div className="import-panel-heading with-status">
        <div>
          <span className="import-eyebrow">Step 3 of 4</span>
          <h2 id="import-step-title">Connect to {engineName}</h2>
          <p>The importer creates a new database and never reuses an existing one.</p>
        </div>
        <ImportReadiness
          state={isPostgres ? pgAvailable : driverAvailable}
          readyText={isPostgres ? "psycopg ready" : `${drivers.length} ODBC driver${drivers.length === 1 ? "" : "s"} found`}
          errorText="Client setup needed"
        />
      </div>

      <div className="import-form-grid">
        {isPostgres ? renderPostgresConnection() : renderSqlConnection()}

        <div className="import-field import-field-wide">
          <label htmlFor="sql-database">New database name</label>
          <input
            id="sql-database"
            value={database}
            onChange={event => setDatabase(event.target.value)}
            placeholder={isPostgres ? "contoso_retail_dw" : "ContosoRetailDW"}
            className="import-input"
            disabled={isRunning}
          />
          <small>This name must not already exist. The wizard creates the database for you.</small>
        </div>
      </div>

      <div className="import-panel-actions">
        <button type="button" className="import-secondary" onClick={() => returnToStep(2)}>← Back</button>
        <button type="button" className="import-primary" onClick={advance} disabled={!connectionReady}>
          Review import <span aria-hidden="true">→</span>
        </button>
      </div>
    </section>
  );

  const reviewOptions = isPostgres
    ? [
      `${pgIdentifierCase === "snake" ? "snake_case" : "PascalCase"} identifiers`,
      `${pgLoadWorkers} load connection${pgLoadWorkers === "1" ? "" : "s"}`,
      pgVerify ? "Row-count verification" : "Verification skipped",
    ]
    : [
      trusted ? "Windows authentication" : `SQL login: ${user}`,
      applyCci ? "Columnstore indexes" : "Relational indexes",
      dropPk ? "Keys removed after load" : "Keys retained",
      verify ? "Verification scorecard" : "Verification skipped",
    ];

  const renderJobOutput = () => (
    <>
      {jobStatus && !isRunning && (
        <div className={`import-job-status ${jobStatus === "done" ? "success" : "failure"}`} role="status">
          <strong>
            {jobStatus === "done"
              ? "Import completed"
              : jobStatus === "cancelled"
                ? "Import cancelled"
                : "Import failed"}
          </strong>
          <span>{formatElapsed(elapsed)}</span>
        </div>
      )}

      {logs.length > 0 && (
        <div className="import-log-card">
          <div className="import-log-header">
            <strong>Import activity</strong>
            <span>{logs.length} lines</span>
          </div>
          <div className="import-log-output" role="log" aria-label="Database import output">
            {logs.map((line, index) => {
              const error = /FAIL|ERROR/i.test(line) && !/0 failed/i.test(line);
              const complete = /DONE|ALL PASSED/i.test(line);
              const warning = /WARN/i.test(line);
              return <div key={index} className={error ? "error" : complete ? "complete" : warning ? "warning" : ""}>{line}</div>;
            })}
            {isRunning && idleSecs >= 2 && (
              <div className="working">Working… {idleSecs}s since the last update</div>
            )}
            <div ref={logsEndRef} />
          </div>
        </div>
      )}
    </>
  );

  const renderStepFour = () => (
    <section className="import-panel" aria-labelledby="import-step-title">
      <div className="import-panel-heading">
        <span className="import-eyebrow">Step 4 of 4</span>
        <h2 id="import-step-title">Review the import</h2>
        <p>Confirm the destination and dataset before the database is created.</p>
      </div>

      <div className="import-review-grid">
        <article className="import-review-card">
          <div className="import-review-card-head">
            <span className={`import-engine-mark ${isPostgres ? "postgres" : "sql"}`} aria-hidden="true">{isPostgres ? "P" : "S"}</span>
            <div>
              <small>Destination</small>
              <strong>{engineName}</strong>
            </div>
            <button type="button" onClick={() => returnToStep(1)} disabled={isRunning}>Edit</button>
          </div>
          <dl>
            <div><dt>Server</dt><dd>{isPostgres ? `${pgHost}:${pgPort}` : server}</dd></div>
            <div><dt>Database</dt><dd>{database}</dd></div>
            <div><dt>User</dt><dd>{isPostgres ? pgUser : trusted ? "Current Windows user" : user}</dd></div>
          </dl>
        </article>

        <article className="import-review-card">
          <div className="import-review-card-head">
            <span className="import-review-dataset-mark" aria-hidden="true">CSV</span>
            <div>
              <small>Dataset</small>
              <strong>{selectedData?.description || "Selected CSV run"}</strong>
            </div>
            <button type="button" onClick={() => returnToStep(2)} disabled={isRunning}>Edit</button>
          </div>
          <dl>
            <div><dt>Generated</dt><dd>{selectedData ? `${selectedData.date} ${selectedData.time}` : "—"}</dd></div>
            <div><dt>Tables</dt><dd>{selectedData?.table_count || "—"}</dd></div>
            <div><dt>Size</dt><dd>{selectedData ? `${selectedData.size_mb} MB` : "—"}</dd></div>
          </dl>
        </article>
      </div>

      <div className="import-behavior">
        <div>
          <small>Import behavior</small>
          <div className="import-chip-row">
            {reviewOptions.map(option => <span key={option}>{option}</span>)}
          </div>
        </div>
        <button type="button" onClick={() => returnToStep(3)} disabled={isRunning}>Edit connection options</button>
      </div>

      <div className="import-safety-note">
        <span aria-hidden="true">i</span>
        <div>
          <strong>A new database named “{database}” will be created.</strong>
          <p>If it already exists, the importer stops without loading data. Passwords are passed privately and are never printed in the command or activity log.</p>
        </div>
      </div>

      {requestError && <div className="import-inline-alert" role="alert"><strong>Import could not start.</strong><span>{requestError}</span></div>}

      <div className="import-launch">
        {isRunning ? (
          <>
            <div>
              <span className="import-running-dot" aria-hidden="true" />
              <strong>Importing into {engineName}</strong>
              <small>{formatElapsed(elapsed)}</small>
            </div>
            <button type="button" className="import-cancel" onClick={cancelImport}>Cancel import</button>
          </>
        ) : (
          <>
            <div>
              <strong>Ready to import</strong>
              <small>This may take several minutes for large datasets.</small>
            </div>
            <button type="button" className="import-primary import-start" onClick={startImport} disabled={!canStart}>
              Import to {engineName} <span aria-hidden="true">→</span>
            </button>
          </>
        )}
      </div>

      {renderJobOutput()}

      {!isRunning && (
        <div className="import-panel-actions review-back">
          <button type="button" className="import-secondary" onClick={() => returnToStep(3)}>← Back to connection</button>
          <span />
        </div>
      )}
    </section>
  );

  return (
    <div className="database-import">
      <button type="button" onClick={onBack} disabled={isRunning} className="import-breadcrumb">
        <span aria-hidden="true">←</span> Configuration
      </button>

      <header className="import-page-header">
        <div>
          <span className="import-eyebrow">Database tools</span>
          <h1>Import a generated dataset</h1>
          <p>A guided path from CSV output to a ready-to-query database.</p>
        </div>
        <span className="import-csv-only">CSV datasets only</span>
      </header>

      <nav className="import-stepper" aria-label="Database import progress">
        {steps.map(([title, description], index) => {
          const number = index + 1;
          const current = number === step;
          const complete = number < step || number < furthestStep;
          const reachable = number <= furthestStep;
          return (
            <button
              type="button"
              key={title}
              className={`${current ? "current" : ""}${complete ? " complete" : ""}`}
              aria-current={current ? "step" : undefined}
              onClick={() => returnToStep(number)}
              disabled={!reachable || isRunning}
            >
              <span className="import-step-number">{complete && !current ? "✓" : number}</span>
              <span>
                <strong>{title}</strong>
                <small>{description}</small>
              </span>
            </button>
          );
        })}
      </nav>

      {requestError && step !== 4 && (
        <div className="import-inline-alert page-alert" role="alert">
          <strong>Something needs attention.</strong>
          <span>{requestError}</span>
        </div>
      )}

      {step === 1 && renderStepOne()}
      {step === 2 && renderStepTwo()}
      {step === 3 && renderStepThree()}
      {step === 4 && renderStepFour()}
    </div>
  );
}
