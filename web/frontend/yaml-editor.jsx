/* ═══ yaml-editor.jsx — CodeMirror 5 wrapper for YAML editing ═══ */

/*
  Extracts navigable sections from YAML text.
  Handles two patterns:
    1. Top-level keys (no indentation)           → config.yaml style
    2. Single wrapper key with indented children  → models.yaml ("models:" → quantity, pricing, …)
  Skips comment-only lines and blank lines.
*/
function parseSections(text){
  const lines=(text||"").split("\n");
  const sections=[];
  const topRe=/^([a-zA-Z_][\w_-]*)\s*:/;
  const childRe=/^  ([a-zA-Z_][\w_-]*)\s*:/;

  let wrapperOnly=false;
  let firstKey=null;

  /* First pass: find top-level keys */
  for(let i=0;i<lines.length;i++){
    const m=lines[i].match(topRe);
    if(m){
      if(!firstKey)firstKey={name:m[1],line:i};
      sections.push({name:m[1],line:i});
    }
  }

  /*
    If there's exactly one top-level key (like "models:"),
    switch to showing its children instead.
  */
  if(sections.length===1){
    const children=[];
    for(let i=0;i<lines.length;i++){
      const m=lines[i].match(childRe);
      if(m)children.push({name:m[1],line:i});
    }
    if(children.length>1)return children;
  }

  return sections;
}

/* Pretty-print a YAML key for display: underscores → spaces, title case */
function prettyKey(k){
  return k.replace(/_/g," ").replace(/\b\w/g,c=>c.toUpperCase());
}

function YamlEditor({value,onChange,filename,dirty,applied=false,error,onApply,onReset,onRefresh}){
  const generatedId=useId().replace(/:/g,"");
  const editorLabelId=`yaml-editor-label-${generatedId}`;
  const editorHelpId=`yaml-editor-help-${generatedId}`;
  const containerRef=useRef(null);
  const cmRef=useRef(null);
  const editorExitRef=useRef(null);
  const onChangeRef=useRef(onChange);
  const onApplyRef=useRef(onApply);
  const suppressRef=useRef(false);
  const[activeSection,setActiveSection]=useState(null);

  onChangeRef.current=onChange;
  onApplyRef.current=onApply;

  /* ─── Initialize CodeMirror once ─── */
  useEffect(()=>{
    if(!containerRef.current||cmRef.current)return;
    const cm=CodeMirror(containerRef.current,{
      value:value||"",
      mode:"yaml",
      theme:"rdg",
      lineNumbers:true,
      lineWrapping:false,
      indentUnit:2,
      tabSize:2,
      indentWithTabs:false,
      styleActiveLine:true,
      matchBrackets:true,
      foldGutter:true,
      gutters:["CodeMirror-linenumbers","CodeMirror-foldgutter"],
      extraKeys:{
        "Tab":(cm)=>{
          if(cm.somethingSelected()){cm.indentSelection("add");}
          else{cm.replaceSelection("  ","end");}
        },
        "Shift-Tab":(cm)=>cm.indentSelection("subtract"),
        "Ctrl-S":(cm)=>{if(onApplyRef.current)onApplyRef.current();},
        "Cmd-S":(cm)=>{if(onApplyRef.current)onApplyRef.current();},
        "Ctrl-F":"findPersistent",
        "Cmd-F":"findPersistent",
        "Esc":()=>editorExitRef.current?.focus(),
      },
    });
    const inputField=cm.getInputField();
    inputField.id=`yaml-editor-input-${generatedId}`;
    inputField.setAttribute("aria-label",`${filename} YAML editor`);
    inputField.setAttribute("aria-describedby",editorHelpId);
    cm.on("change",()=>{
      if(suppressRef.current)return;
      onChangeRef.current(cm.getValue());
    });
    cmRef.current=cm;
    setTimeout(()=>cm.refresh(),0);
  },[]);

  /* ─── Sync external value (reset, apply from outside) ─── */
  useEffect(()=>{
    const cm=cmRef.current;
    if(!cm)return;
    if(cm.getValue()!==value){
      suppressRef.current=true;
      const scroll=cm.getScrollInfo();
      const cursor=cm.getCursor();
      cm.setValue(value||"");
      const maxLine=cm.lineCount()-1;
      const safeLine=Math.min(cursor.line,maxLine);
      const safeCh=Math.min(cursor.ch,cm.getLine(safeLine).length);
      cm.setCursor({line:safeLine,ch:safeCh});
      cm.scrollTo(scroll.left,scroll.top);
      suppressRef.current=false;
    }
  },[value]);

  /* ─── Resize observer keeps CM sized correctly ─── */
  useEffect(()=>{
    const cm=cmRef.current;if(!cm)return;
    const ro=new ResizeObserver(()=>cm.refresh());
    if(containerRef.current)ro.observe(containerRef.current);
    return()=>ro.disconnect();
  },[]);

  /* ─── Jump to section ─── */
  const jumpTo=useCallback((section)=>{
    const cm=cmRef.current;
    if(!cm||!containerRef.current)return;
    setActiveSection(section.name);

    /* Walk up the DOM and snapshot every scrollable ancestor */
    const saved=[];
    let el=containerRef.current.parentElement;
    while(el){
      if(el.scrollHeight>el.clientHeight){
        saved.push({el,top:el.scrollTop,left:el.scrollLeft});
      }
      el=el.parentElement;
    }
    saved.push({el:null,top:window.scrollY,left:window.scrollX});

    /* Move cursor and scroll only inside the editor */
    cm.setCursor({line:section.line,ch:0},{scroll:false});
    const lineTop=cm.charCoords({line:section.line,ch:0},"local").top;
    cm.scrollTo(null,lineTop-12);
    cm.getInputField().focus({preventScroll:true});

    /* Restore every ancestor to its pre-jump position */
    const restore=()=>{
      for(const s of saved){
        if(s.el){s.el.scrollTop=s.top;s.el.scrollLeft=s.left;}
        else window.scrollTo(s.left,s.top);
      }
    };
    restore();
    requestAnimationFrame(restore);
  },[]);

  const sections=parseSections(value);
  const lineCount=(value||"").split("\n").length;
  const statusColor=dirty?"var(--warn)":applied?"var(--accent)":"var(--ok)";
  const statusText=dirty?"Modified":applied?"Applied":"In sync";
  const tBtn={padding:"6px 16px",borderRadius:6,fontSize:12,fontWeight:500,cursor:"pointer",fontFamily:"var(--sans)",transition:"all .12s",border:"1px solid var(--border)"};

  return(
    <div style={{borderRadius:12,overflow:"hidden",border:"1px solid var(--border)",boxShadow:"0 1px 3px rgba(0,0,0,.04)"}}>

      {/* ─── Toolbar ─── */}
      <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",padding:"0 4px 0 0",background:"var(--alt)",borderBottom:"1px solid var(--border)",flexWrap:"wrap",gap:6}}>
        <div style={{display:"flex",alignItems:"center",gap:0}}>
          {/* File tab */}
          <div id={editorLabelId} style={{display:"flex",alignItems:"center",gap:7,padding:"10px 16px",background:"var(--surface)",borderRight:"1px solid var(--border)",fontSize:12.5,fontWeight:600,color:"var(--text)",fontFamily:"var(--mono)"}}>
            <span style={{width:14,height:14,borderRadius:3,background:"var(--accent)",display:"flex",alignItems:"center",justifyContent:"center",fontSize:8,color:"#fff",fontWeight:700}}>Y</span>
            {filename}
          </div>
          {/* Status pill */}
          <div role="status" aria-live="polite" style={{display:"flex",alignItems:"center",gap:5,padding:"0 14px",fontSize:11,fontWeight:600,color:statusColor}}>
            <span style={{width:6,height:6,borderRadius:"50%",background:statusColor}} />
            {statusText}
          </div>
        </div>
        <div style={{display:"flex",alignItems:"center",gap:6,padding:"6px 8px"}}>
          {onRefresh&&<TBtn style={tBtn} onClick={onRefresh}>Refresh from UI</TBtn>}
          <TBtn buttonRef={editorExitRef} style={tBtn} onClick={onReset}>Reset to disk</TBtn>
          <button type="button" onClick={onApply} disabled={!dirty}
            style={{...tBtn,
              background:dirty?"var(--accent)":"transparent",
              color:dirty?"#fff":"var(--muted)",
              border:dirty?"1px solid var(--accent)":"1px solid var(--border)",
              fontWeight:dirty?600:500,
              cursor:dirty?"pointer":"default"}}>
            {dirty?"Apply":"No changes"}
          </button>
        </div>
      </div>

      {/* ─── Section jump bar ─── */}
      {sections.length>1&&(
        <div style={{display:"flex",alignItems:"center",gap:5,padding:"7px 16px",background:"var(--surface)",borderBottom:"1px solid var(--border)",overflowX:"auto",flexWrap:"nowrap"}}>
          <span style={{fontSize:10.5,color:"var(--muted)",fontWeight:600,letterSpacing:".04em",textTransform:"uppercase",flexShrink:0,marginRight:4}}>Jump</span>
          {sections.map(sec=>{
            const active=activeSection===sec.name;
            return(
              <button type="button" key={sec.name} aria-pressed={active} onClick={()=>jumpTo(sec)}
                style={{
                  padding:"3px 10px",borderRadius:5,fontSize:11,fontWeight:active?600:500,
                  cursor:"pointer",fontFamily:"var(--sans)",transition:"all .12s",whiteSpace:"nowrap",
                  border:active?"1px solid var(--accent)":"1px solid var(--border)",
                  background:active?"var(--glow)":"transparent",
                  color:active?"var(--accent)":"var(--dim)",
                }}
                onMouseOver={e=>{if(!active){e.currentTarget.style.background="var(--alt)";e.currentTarget.style.color="var(--text)";}}}
                onMouseOut={e=>{if(!active){e.currentTarget.style.background="transparent";e.currentTarget.style.color="var(--dim)";}}}
              >
                {prettyKey(sec.name)}
              </button>
            );
          })}
        </div>
      )}

      {/* ─── CodeMirror container ─── */}
      <div ref={containerRef} role="group" aria-labelledby={editorLabelId} style={{minHeight:340,maxHeight:700,overflow:"auto"}} />

      {/* ─── Footer ─── */}
      <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",padding:"7px 16px",background:"var(--alt)",borderTop:"1px solid var(--border)",fontSize:11,color:"var(--muted)",fontFamily:"var(--mono)"}}>
        <span id={editorHelpId}>YAML{dirty?" \u2022 modified":""} {"\u2014"} Esc exits editor {"\u00B7"} Ctrl+S applies</span>
        <span>{lineCount} lines</span>
      </div>

      {/* ─── Parse error banner ─── */}
      {error&&<div role="alert" style={{padding:"9px 14px",fontSize:12.5,background:"var(--errBg)",color:"var(--err)",borderTop:"1px solid rgba(220,38,38,.15)"}}><strong>Parse error:</strong> {error}</div>}
    </div>
  );
}

/* Toolbar button with hover */
function TBtn({style:sx,onClick,children,buttonRef}){
  return(
    <button ref={buttonRef} type="button" onClick={onClick}
      style={{...sx,background:"transparent",color:"var(--dim)"}}
      onMouseOver={e=>{e.currentTarget.style.background="var(--alt)";e.currentTarget.style.color="var(--text)";}}
      onMouseOut={e=>{e.currentTarget.style.background="transparent";e.currentTarget.style.color="var(--dim)";}}>
      {children}
    </button>
  );
}
