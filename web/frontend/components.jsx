/* ═══ components.jsx — shared UI primitives ═══ */
const {useState, useEffect, useRef, useCallback, useMemo, useId} = React;

const API = "/api";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const DIMS = ["customers", "products", "stores", "geography", "promotions", "dates", "currency", "exchange_rates", "employees"];

/* Shared input style (also exported as `iS` for backward compatibility) */
const inputStyle = {
  width: "100%",
  background: "var(--bg)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: "8px 11px",
  fontSize: 13.5,
  color: "var(--text)",
  outline: "none",
  fontFamily: "var(--mono)"
};

const iS = inputStyle;

function Section({num, title, children, defaultOpen = true, badge}) {
  const [open, setOpen] = useState(defaultOpen);
  const generatedId = useId().replace(/:/g, "");
  const triggerId = `section-trigger-${generatedId}`;
  const panelId = `section-panel-${generatedId}`;
  return (
    <div style={{background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10, marginBottom: 14, boxShadow: "0 1px 3px rgba(0,0,0,.04)", overflow: "hidden"}}>
      <button type="button" id={triggerId} aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(!open)} style={{display: "flex", alignItems: "center", gap: 10, padding: "14px 18px", cursor: "pointer", background: "none", border: "none", width: "100%", color: "var(--text)", fontFamily: "var(--sans)"}}>
        <span style={{width: 26, height: 26, borderRadius: 7, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12, fontWeight: 700, fontFamily: "var(--mono)", background: open ? "var(--accent)" : "var(--alt)", color: open ? "#fff" : "var(--muted)", transition: "all .15s"}}>{num}</span>
        <span style={{fontSize: 14.5, fontWeight: 600, flex: 1, textAlign: "left"}}>{title}</span>
        {badge && <Badge variant={badge.v}>{badge.t}</Badge>}
        <span aria-hidden="true" style={{fontSize: 16, color: "var(--muted)", transform: open ? "rotate(0)" : "rotate(-90deg)", transition: "transform .2s"}}>▾</span>
      </button>
      <div
        id={panelId}
        role="region"
        aria-labelledby={triggerId}
        data-section-panel="true"
        hidden={!open}
        style={{padding: "0 18px 18px", borderTop: "1px solid var(--border)"}}
      >
        {children}
      </div>
    </div>
  );
}

function FormField({label, help, children, id}) {
  const generatedId = useId().replace(/:/g, "");
  const controlId = id || `field-${generatedId}`;
  const helpId = `${controlId}-help`;
  const onlyChild = React.Children.count(children) === 1
    ? React.Children.only(children)
    : null;
  const isNativeControl = React.isValidElement(onlyChild)
    && typeof onlyChild.type === "string"
    && ["input", "select", "textarea"].includes(onlyChild.type);
  const isSharedControl = React.isValidElement(onlyChild)
    && [NumberInput, Select].includes(onlyChild.type);
  const canAssociate = Boolean(label?.trim() && (isNativeControl || isSharedControl));
  const renderedChildren = canAssociate
    ? React.cloneElement(onlyChild, {
        id: onlyChild.props.id || controlId,
        "aria-describedby": help
          ? [onlyChild.props["aria-describedby"], helpId].filter(Boolean).join(" ")
          : onlyChild.props["aria-describedby"],
      })
    : children;
  return (
    <div style={{marginTop: 14}}>
      {label && (canAssociate
        ? <label htmlFor={controlId} style={{display: "block", fontSize: 12.5, fontWeight: 500, color: "var(--dim)", marginBottom: 5}}>{label}</label>
        : <div style={{display: "block", fontSize: 12.5, fontWeight: 500, color: "var(--dim)", marginBottom: 5}}>{label}</div>
      )}
      {renderedChildren}
      {help && <div id={helpId} style={{fontSize: 11, color: "var(--muted)", marginTop: 3}}>{help}</div>}
    </div>
  );
}
/* Backward-compatible alias */
const F = FormField;

function NumberInput({value, onChange, min, max, step, style: extraStyle, ...inputProps}) {
  const displayValue = value != null ? value : "";
  return (
    <input
      type="number"
      style={{...inputStyle, ...extraStyle}}
      value={displayValue}
      min={min}
      max={max}
      step={step || 1}
      {...inputProps}
      onChange={event => {
        const parsed = parseFloat(event.target.value);
        onChange(isNaN(parsed) ? (min || 0) : parsed);
      }}
      onFocus={event => {
        event.target.style.borderColor = "var(--focus)";
        event.target.style.boxShadow = "0 0 0 3px var(--glow)";
      }}
      onBlur={event => {
        event.target.style.borderColor = "var(--border)";
        event.target.style.boxShadow = "none";
      }}
    />
  );
}
/* Backward-compatible alias */
const N = NumberInput;

function Select({value, onChange, options, labels, style: extraStyle, ...selectProps}) {
  return (
    <select
      style={{...inputStyle, fontFamily: "var(--sans)", cursor: "pointer", ...extraStyle}}
      value={value}
      onChange={event => onChange(event.target.value)}
      {...selectProps}
    >
      {options.map((option, index) => (
        <option key={option} value={option}>{labels ? labels[index] : option}</option>
      ))}
    </select>
  );
}
/* Backward-compatible alias */
const Sel = Select;

function Checkbox({checked, onChange, label, disabled, id, describedBy}) {
  const generatedId = useId().replace(/:/g, "");
  const controlId = id || `checkbox-${generatedId}`;
  return (
    <label
      htmlFor={controlId}
      style={{display: "flex", alignItems: "center", gap: 7, cursor: disabled ? "default" : "pointer", fontSize: 13, color: disabled ? "var(--muted)" : "var(--text)", fontFamily: "var(--sans)", opacity: disabled ? .6 : 1}}
    >
      <input
        className="accessible-check-input"
        id={controlId}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-describedby={describedBy}
        onChange={event => onChange(event.target.checked)}
      />
      <span className="accessible-check-box" aria-hidden="true" style={{width: 17, height: 17, borderRadius: 5, flexShrink: 0, border: `2px solid ${checked ? "var(--accent)" : "var(--border)"}`, background: checked ? "var(--accent)" : "transparent", display: "flex", alignItems: "center", justifyContent: "center", transition: "all .12s"}}>
        {checked && <span style={{color: "#fff", fontSize: 10, fontWeight: 700}}>✓</span>}
      </span>
      {label}
    </label>
  );
}
/* Backward-compatible alias */
const Check = Checkbox;

function Slider({label, value, onChange, min, max, step, fmt}) {
  const numericValue = Number(value) || 0;
  const generatedId = useId().replace(/:/g, "");
  const controlId = `slider-${generatedId}`;
  return (
    <div style={{marginTop: 10}}>
      <div style={{display: "flex", justifyContent: "space-between", marginBottom: 5}}>
        <label htmlFor={controlId} style={{fontSize: 12, color: "var(--dim)"}}>{label}</label>
        <span id={`${controlId}-value`} style={{fontSize: 12, color: "var(--accent)", fontFamily: "var(--mono)", fontWeight: 600}}>
          {fmt ? fmt(numericValue) : numericValue.toFixed(2)}
        </span>
      </div>
      <input
        id={controlId}
        type="range"
        aria-describedby={`${controlId}-value`}
        min={min}
        max={max}
        step={step}
        value={numericValue}
        onChange={event => onChange(parseFloat(event.target.value))}
        style={{width: "100%", accentColor: "var(--accent)"}}
      />
    </div>
  );
}
/* Backward-compatible alias */
const Sld = Slider;

function Badge({variant = "default", children}) {
  const colors = {
    default: {bg: "var(--glow)", color: "var(--accent)"},
    success: {bg: "var(--okBg)", color: "var(--ok)"},
    warning: {bg: "var(--warnBg)", color: "var(--warn)"},
    error: {bg: "var(--errBg)", color: "var(--err)"},
  }[variant];
  return (
    <span style={{display: "inline-flex", padding: "2px 9px", borderRadius: 6, fontSize: 11.5, fontWeight: 600, fontFamily: "var(--mono)", background: colors.bg, color: colors.color}}>
      {children}
    </span>
  );
}

function GridRow2({children}) {
  return <div className="resp-r2" style={{display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14}}>{children}</div>;
}
function GridRow3({children}) {
  return <div className="resp-r3" style={{display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14}}>{children}</div>;
}
function GridRow4({children}) {
  return <div className="resp-r4" style={{display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 12}}>{children}</div>;
}
/* Backward-compatible aliases */
const R2 = GridRow2;
const R3 = GridRow3;
const R4 = GridRow4;

/* Theme toggle button */
function ThemeToggle() {
  const [dark, setDark] = useState(() => document.documentElement.getAttribute("data-theme") === "dark");
  const toggle = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.setAttribute("data-theme", next ? "dark" : "light");
    try { localStorage.setItem("rdg-theme", next ? "dark" : "light"); } catch (err) {}
  };
  useEffect(() => {
    try {
      const saved = localStorage.getItem("rdg-theme");
      if (saved) {
        document.documentElement.setAttribute("data-theme", saved);
        setDark(saved === "dark");
      }
    } catch (err) {}
  }, []);
  return (
    <button
      type="button"
      onClick={toggle}
      title={dark ? "Switch to light mode" : "Switch to dark mode"}
      aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
      style={{width: 32, height: 32, borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--dim)", fontSize: 15, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", transition: "all .15s", flexShrink: 0}}
      onMouseOver={event => { event.currentTarget.style.borderColor = "var(--accent)"; event.currentTarget.style.color = "var(--accent)"; }}
      onMouseOut={event => { event.currentTarget.style.borderColor = "var(--border)"; event.currentTarget.style.color = "var(--dim)"; }}
    >
      {dark ? "\u2600" : "\u263E"}
    </button>
  );
}

function Box({title, children}) {
  return (
    <div style={{marginTop: 16, padding: "14px 16px", background: "transparent", borderRadius: 10, border: "1px solid var(--border)"}}>
      {title && <div style={{fontSize: 12, fontWeight: 600, color: "var(--dim)", marginBottom: 10}}>{title}</div>}
      {children}
    </div>
  );
}

function Hint({children, accent}) {
  return (
    <div style={{marginTop: 8, padding: "6px 12px", borderLeft: `3px solid ${accent ? "var(--accent)" : "var(--border)"}`, borderRadius: "0 6px 6px 0", background: "var(--alt)", fontSize: 11.5, color: "var(--dim)", lineHeight: 1.5}}>
      {children}
    </div>
  );
}
